"""全局规则 e（每天游玩总时长）单测：判定 + 跨游戏累加 + 变更管制。

跑法：conda 环境的 python tests/test_daily.py
"""

import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gamelimiter import config

config.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"   # 隔离测试库（须在 db.connect 前）

from gamelimiter import changes, db, rules
from gamelimiter.db import Game


def ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def g(session=None, windows=None, monitor=False):
    return Game(1, "测试", "test.exe", None, None, session, windows, True,
                monitor_only=monitor)


# ---- 判定：今天总共玩了多久 vs 上限 ----
now = ts("2026-08-16 21:00")
assert rules.check_daily_minutes(None, 9999 * 60, now).allowed        # 未设 = 不限
assert rules.check_daily_minutes(120, 60 * 60, now).allowed           # 玩了 60 分钟，上限 120
assert rules.check_daily_minutes(120, 119.9 * 60, now).allowed        # 差一点，还能开
v = rules.check_daily_minutes(120, 120 * 60, now)
assert not v.allowed and v.reason == "daily_minutes", v
assert v.unlock_ts == ts("2026-08-17 00:00"), v                       # 明天 0:00 重置
assert "120" in v.detail, v.detail
# 与规则 d 的关键区别：今天已经玩过的那款照样拦（总额是总额）
assert not rules.check_daily_minutes(120, 200 * 60, now).allowed

# ---- deadline：总额作为第三个候选，与单次时长/时段取最早 ----
dl = rules.session_deadline(g(), now, 0, None, daily_remaining=30 * 60)
assert dl == (ts("2026-08-16 21:30"), "daily_minutes"), dl
dl = rules.session_deadline(g(session=90), now, 0, None, daily_remaining=30 * 60)
assert dl == (ts("2026-08-16 21:30"), "daily_minutes"), dl            # 总额更紧
dl = rules.session_deadline(g(session=20), now, 0, None, daily_remaining=30 * 60)
assert dl == (ts("2026-08-16 21:20"), "session_timeout"), dl          # 单次更紧
dl = rules.session_deadline(g(windows=["19:00-21:10"]), now, 0, None, daily_remaining=30 * 60)
assert dl == (ts("2026-08-16 21:10"), "window_end"), dl               # 时段更紧
dl = rules.session_deadline(g(), now, 0, None, daily_remaining=0)
assert dl == (now, "daily_minutes"), dl                               # 已用完 → 立即到点
# 观察模式永不设截止（第四个绕过点：PVP 强杀会判逃跑）
assert rules.session_deadline(g(monitor=True), now, 0, None, daily_remaining=0) is None
# 不传 daily_remaining = 老行为，一条 b/c 规则都没有就没有 deadline
assert rules.session_deadline(g(), now, 0, None) is None

# ---- 今日已玩总时长：跨游戏累加，口径 = 真实在跑时间 ----
conn = db.connect()
gA = db.upsert_game(conn, "A", "a.exe")
gB = db.upsert_game(conn, "B", "b.exe")
gM = db.upsert_game(conn, "观察", "m.exe")
db.update_rules(conn, gM.id, monitor_only=1)
day0, day1 = rules.day_bounds(time.time())


def played(game, start, minutes, wall_minutes=None):
    """造一条已结束的会话：真实在跑 minutes 分钟，墙钟跨度 wall_minutes 分钟。"""
    wall = (wall_minutes or minutes) * 60
    sid = db.open_session(conn, game.id, int(start))
    db.heartbeat(conn, sid, minutes * 60, start + wall)
    db.close_session(conn, sid, int(start + wall), "self_exit")


# A 玩 40 分钟（中途挂机，墙钟 50 分钟）+ B 玩 25 分钟 + 观察模式 100 分钟
played(gA, day0 + 3600, 40, wall_minutes=50)
played(gB, day0 + 7200, 25)
played(gM, day0 + 12000, 100)
used = db.played_seconds_between(conn, day0, day1)
assert abs(used - 65 * 60) < 1, used / 60      # 40+25；挂机的 10 分钟与观察模式都不算

# 跨午夜：按墙钟重叠比例分摊到两天，两天加起来仍是总数
conn.execute("DELETE FROM sessions")
conn.commit()
played(gA, day0 - 3600, 90, wall_minutes=120)  # 昨天 23:00 → 今天 01:00
assert abs(db.played_seconds_between(conn, day0, day1) - 45 * 60) < 1
assert abs(db.played_seconds_between(conn, day0 - 86400, day0) - 45 * 60) < 1

# 进行中的会话按"到此刻为止"算
conn.execute("DELETE FROM sessions")
conn.commit()
noon = day0 + 12 * 3600
sid = db.open_session(conn, gA.id, int(noon - 600))
db.heartbeat(conn, sid, 600, noon)
assert abs(db.daily_used_seconds(conn, noon) - 600) < 2

# 剩余额度
assert db.daily_remaining_seconds(conn, noon) is None           # 未设上限 → None（且不查库）
db.set_daily_minutes(conn, 30)
assert abs(db.daily_remaining_seconds(conn, noon) - 20 * 60) < 2
db.set_daily_minutes(conn, 5)
assert db.daily_remaining_seconds(conn, noon) == 0              # 超了不给负数
db.set_daily_minutes(conn, None)

# ---- 变更管制：与规则 d 同一套纪律 ----
T = changes.is_tightening
assert T("daily_minutes", None, 120)          # 从不限到 120 分钟 = 收紧
assert T("daily_minutes", 180, 120)
assert not T("daily_minutes", 120, 180)       # 加时 = 放宽
assert not T("daily_minutes", 120, None)      # 取消 = 放宽

assert db.get_daily_minutes(conn) is None
assert changes.request_daily_minutes(conn, 120)[0] == "applied"   # 收紧立即生效
assert db.get_daily_minutes(conn) == 120
assert changes.request_daily_minutes(conn, 120)[0] == "nochange"
status, apply_at, _ = changes.request_daily_minutes(conn, 180)    # 放宽入队，不立即改
assert status == "delayed" and db.get_daily_minutes(conn) == 120
assert "总时长放宽到 180 分钟" in changes.describe_pending(changes.global_pendings(conn)[0])
assert changes.apply_due(conn, time.time()) == 0                  # 未到期不落地
assert changes.apply_due(conn, apply_at + 1) == 1
assert db.get_daily_minutes(conn) == 180 and not changes.global_pendings(conn)

# 两条全局规则各走各的队列，落地时按 field 派发、互不覆盖
assert changes.request_daily_limit(conn, 2)[0] == "applied"
assert changes.request_daily_limit(conn, 5)[0] == "delayed"
assert changes.request_daily_minutes(conn, 240)[0] == "delayed"
assert len(changes.global_pendings(conn)) == 2
assert changes.apply_due(conn, time.time() + 25 * 3600) == 2
assert db.get_daily_game_limit(conn) == 5 and db.get_daily_minutes(conn) == 240

# 改主意变严 → 撤销待生效的放宽
changes.request_daily_minutes(conn, 300)
assert changes.request_daily_minutes(conn, 60)[0] == "applied"
assert db.get_daily_minutes(conn) == 60
assert not [p for p in changes.global_pendings(conn) if p["field"] == "daily_minutes"]
# 取消限制 = 放宽，也要等
assert changes.request_daily_minutes(conn, None)[0] == "delayed"
assert db.get_daily_minutes(conn) == 60
assert "取消「每天游玩总时长」限制" in changes.describe_pending(
    [p for p in changes.global_pendings(conn) if p["field"] == "daily_minutes"][0])

print("test_daily: 全部通过")
