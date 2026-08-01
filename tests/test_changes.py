"""变更管制单元测试：收紧/放宽分类 + 延迟落地。

跑法：conda run -n gamelimiter python tests/test_changes.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import tempfile

from gamelimiter import config

config.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"   # 隔离测试库（须在 db.connect 前）

from gamelimiter import changes, db, rules

# ---- 分类 ----
T = changes.is_tightening
assert T("cooldown_hours", None, 4)          # 加冷却 = 收紧
assert T("cooldown_hours", 2, 4)
assert not T("cooldown_hours", 4, 2)         # 减冷却 = 放宽
assert not T("cooldown_hours", 4, None)
assert T("session_minutes", None, 90)        # 加时长上限 = 收紧
assert T("session_minutes", 90, 60)
assert not T("session_minutes", 60, 90)
assert not T("session_minutes", 90, None)
assert T("windows", None, ["19:00-23:00"])   # 加时段限制 = 收紧
assert T("windows", ["19:00-23:00"], ["20:00-22:00"])        # 子集 = 收紧
assert not T("windows", ["20:00-22:00"], ["19:00-23:00"])    # 超集 = 放宽
assert not T("windows", ["19:00-23:00"], None)
assert not T("windows", ["19:00-23:00"], ["12:00-13:00"])    # 换到别的时段也算放宽（新增可玩分钟）
assert T("windows", ["22:00-01:00"], ["22:00-00:00"])        # 跨午夜子集
assert T("enabled", 0, 1)                    # 启用 = 收紧
assert not T("enabled", 1, 0)                # 停用 = 放宽
assert T("next_allowed_date", None, "2026-08-02")            # 加锁 = 收紧
assert T("next_allowed_date", "2026-08-02", "2026-08-05")    # 锁得更晚 = 收紧
assert not T("next_allowed_date", "2026-08-05", "2026-08-02")  # 提前 = 放宽
assert not T("next_allowed_date", "2026-08-02", None)        # 取消 = 放宽

# ---- 落库流程 ----
conn = db.connect()
g = db.upsert_game(conn, "测试", "t.exe", cooldown_hours=4, session_minutes=90)

applied, delayed = changes.request_changes(conn, g, {"cooldown_hours": 6, "session_minutes": 120})
assert list(applied) == ["cooldown_hours"] and len(delayed) == 1   # 收紧即生效，放宽入队
g = db.get_game(conn, "t.exe")
assert g.cooldown_hours == 6 and g.session_minutes == 90
assert len(db.list_pending(conn, g.id)) == 1

# 改主意变严 → 撤销放宽申请
applied, delayed = changes.request_changes(conn, g, {"session_minutes": 60})
assert applied == {"session_minutes": 60} and not delayed
assert not db.list_pending(conn, g.id)

# 未到期不落地；到期落地
changes.request_changes(conn, db.get_game(conn, "t.exe"), {"session_minutes": 120})
assert changes.apply_due(conn, time.time()) == 0
assert changes.apply_due(conn, time.time() + 25 * 3600) == 1
assert db.get_game(conn, "t.exe").session_minutes == 120

# 受限游戏删除 = 延迟；到期真删
g = db.get_game(conn, "t.exe")
apply_at = changes.request_delete(conn, g)
assert apply_at is not None and db.get_game(conn, "t.exe") is not None
changes.apply_due(conn, apply_at + 1)
assert db.get_game(conn, "t.exe") is None

# 未受限游戏删除 = 立即
g2 = db.upsert_game(conn, "无规则", "t2.exe")
assert changes.request_delete(conn, g2) is None
assert db.get_game(conn, "t2.exe") is None

# ---- 本次游玩额度 ----
g = db.upsert_game(conn, "额度", "q.exe", session_minutes=120)

ok, _ = changes.set_next_session(conn, g, 60)                  # ≤上限 → 立即生效
assert ok and db.get_game(conn, "q.exe").next_session_minutes == 60
ok, msg = changes.set_next_session(conn, db.get_game(conn, "q.exe"), 180)
assert not ok and "不能超过" in msg                             # 超上限 → 拒
assert db.get_game(conn, "q.exe").next_session_minutes == 60   # 且不改动现值
ok, _ = changes.set_next_session(conn, db.get_game(conn, "q.exe"), None)   # 清除
assert ok and db.get_game(conn, "q.exe").next_session_minutes is None

# 上限不限的游戏也能设额度（那也是收紧）
g3 = db.upsert_game(conn, "无上限", "q3.exe")
assert changes.set_next_session(conn, g3, 45)[0]
assert db.get_game(conn, "q3.exe").next_session_minutes == 45

# 额度在开会话时被消费（守护逻辑的 DB 面）
g = db.get_game(conn, "q.exe")
changes.set_next_session(conn, g, 90)
now = time.time()
sid = db.open_session(conn, g.id, int(now - 30 * 60), 90)
db.heartbeat(conn, sid, 30 * 60, now)   # 已玩 30 分钟——按心跳算，不是按 start_ts 的墙钟差
db.set_next_session(conn, g.id, None)
assert db.get_game(conn, "q.exe").next_session_minutes is None
sess = db.active_session(conn, g.id)
assert sess["id"] == sid and sess["limit_minutes"] == 90

# 游玩中：只许缩短，且要留够预警缓冲
g = db.get_game(conn, "q.exe")
assert not changes.shorten_running_session(conn, g, sess, 120, now)[0]     # 加时 → 拒
assert not changes.shorten_running_session(conn, g, sess, 90, now)[0]      # 不变 → 拒
assert not changes.shorten_running_session(conn, g, sess, None, now)[0]    # 取消 → 拒
ok, msg = changes.shorten_running_session(conn, g, sess, 35, now)          # 已玩30+预警10=40
assert not ok and "最短可设 40" in msg, msg
assert changes.shorten_running_session(conn, g, sess, 60, now)[0]
assert db.active_session(conn, g.id)["limit_minutes"] == 60
db.close_session(conn, sid, int(now), "self_exit")

# ---- 下次可玩日 ----
gd = db.upsert_game(conn, "锁日期", "d.exe", session_minutes=90)
applied, delayed = changes.request_changes(conn, gd, {"next_allowed_date": "2026-08-02"})
assert applied == {"next_allowed_date": "2026-08-02"} and not delayed   # 加锁 = 收紧即时
assert db.get_game(conn, "d.exe").next_allowed_date == "2026-08-02"
# 往后推也是收紧
applied, _ = changes.request_changes(conn, db.get_game(conn, "d.exe"),
                                     {"next_allowed_date": "2026-08-05"})
assert applied == {"next_allowed_date": "2026-08-05"}
# 提前 = 放宽 → 入队不立即改
_, delayed = changes.request_changes(conn, db.get_game(conn, "d.exe"),
                                     {"next_allowed_date": "2026-07-27"})
assert delayed and db.get_game(conn, "d.exe").next_allowed_date == "2026-08-05"
assert "提前到 2026-07-27" in changes.describe_pending(db.list_pending(conn, gd.id)[0])
# 改主意又往后推 → 撤销那条提前申请
changes.request_changes(conn, db.get_game(conn, "d.exe"), {"next_allowed_date": "2026-08-09"})
assert not db.list_pending(conn, gd.id)
# 只设了日期的游戏也算受限，删除要走延迟
assert changes.request_delete(conn, db.upsert_game(
    conn, "只锁日期", "d2.exe", cooldown_hours=None)) is None      # 无任何规则 → 立即删
gd3 = db.upsert_game(conn, "只锁日期", "d3.exe")
changes.request_changes(conn, gd3, {"next_allowed_date": "2026-08-02"})
assert changes.request_delete(conn, db.get_game(conn, "d3.exe")) is not None
db.remove_game(conn, gd3.id)      # 清掉这条待删申请，免得污染后面的 apply_due 计数

# ---- 全局规则 d：每天最多玩几款 ----
assert changes.is_tightening("daily_game_limit", None, 2)      # 从不限到 2 款 = 收紧
assert changes.is_tightening("daily_game_limit", 3, 2)
assert not changes.is_tightening("daily_game_limit", 2, 3)     # 放宽
assert not changes.is_tightening("daily_game_limit", 2, None)  # 取消 = 放宽

assert db.get_daily_game_limit(conn) is None
assert changes.request_daily_limit(conn, 2)[0] == "applied"    # 收紧立即生效
assert db.get_daily_game_limit(conn) == 2
assert changes.request_daily_limit(conn, 2)[0] == "nochange"
status, apply_at, _ = changes.request_daily_limit(conn, 4)     # 放宽入队，不立即改
assert status == "delayed" and db.get_daily_game_limit(conn) == 2
assert len(changes.global_pendings(conn)) == 1
assert "每天最多玩 4 款" in changes.describe_pending(changes.global_pendings(conn)[0])
assert changes.apply_due(conn, time.time()) == 0               # 未到期不落地
assert changes.apply_due(conn, apply_at + 1) == 1              # 到期落地
assert db.get_daily_game_limit(conn) == 4 and not changes.global_pendings(conn)
# 改主意变严 → 撤销待生效的放宽
changes.request_daily_limit(conn, 6)
assert changes.request_daily_limit(conn, 1)[0] == "applied"
assert db.get_daily_game_limit(conn) == 1 and not changes.global_pendings(conn)
# 取消限制 = 放宽，也要等
assert changes.request_daily_limit(conn, None)[0] == "delayed"
assert db.get_daily_game_limit(conn) == 1

# 今日已玩款数（跨午夜的会话两天都算）
conn.execute("DELETE FROM sessions")
gA = db.upsert_game(conn, "A", "a.exe")
gB = db.upsert_game(conn, "B", "b.exe")
day0, day1 = rules.day_bounds(time.time())
db.open_session(conn, gA.id, int(day0 + 3600))                 # 今天，未结束
db.close_session(conn, db.open_session(conn, gB.id, int(day0 - 7200)),
                 int(day0 + 1800), "self_exit")                # 昨晚跨到今天凌晨
db.close_session(conn, db.open_session(conn, gB.id, int(day0 - 7200)),
                 int(day0 - 3600), "self_exit")                # 纯昨天，不算
played = db.games_played_between(conn, day0, day1)
assert set(played) == {gA.id, gB.id}, played
assert db.games_played_between(conn, day1, day1 + 86400) == {}  # 明天还是空的

print("test_changes: 全部通过")
