"""一段游玩（block）的 DB 面 + 守护判定测试。

复现 2026-08-01 的实际场景：上限 60 分钟、冷却 20 小时，玩 31.5 分钟就退出，
47 分钟后回来——应该能接着玩掉剩下的 28.5 分钟，而不是被冷却挡在门外。

跑法：conda run -n gamelimiter python tests/test_blocks.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gamelimiter import config

config.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"   # 隔离测试库（须在 db.connect 前）

from gamelimiter import db, rules

GRACE = config.IDLE_GRACE_MINUTES
conn = db.connect()
T0 = 1_785_000_000                      # 参照时刻，秒
M = 60


def played(game_id):
    b = db.current_block(conn, game_id)
    return b["played_seconds"] / 60


# ---- 场景：上限 60 分钟 + 冷却 20 小时 ----
g = db.upsert_game(conn, "帕鲁", "Palworld.exe", cooldown_hours=20, session_minutes=60)

# 第一次打开，玩 31.5 分钟后自己退出
s1 = db.open_session(conn, g.id, T0)
assert db.current_block(conn, g.id)["running"]
db.heartbeat(conn, s1, 31.5 * M, T0 + 31.5 * M)
db.close_session(conn, s1, int(T0 + 31.5 * M), "self_exit")

blk = db.current_block(conn, g.id)
assert blk["played_seconds"] == 31.5 * M and blk["sessions"] == 1
assert not blk["running"]

# 47 分钟后想再玩：这一段还活着 → 接着玩，冷却不该拦
t_back = T0 + (31.5 + 47) * M
assert rules.block_alive(blk, g.session_minutes, t_back, GRACE)
v = rules.check_start(g, db.last_session_end(conn, g.id), t_back, resuming=True)
assert v.allowed, v
# 对照：若不算续玩，20 小时冷却会挡住（这就是改造前的行为）
assert not rules.check_start(g, db.last_session_end(conn, g.id), t_back).allowed

# 接着玩 = 同一段，额度接着用（剩 28.5 分钟）
left = rules.block_remaining(g.session_minutes, blk)
assert left == 28.5 * M, left
s2 = db.open_session(conn, g.id, int(t_back), blk["limit_minutes"], blk["block_id"])
assert db.block_of(db.last_session(conn, g.id)) == s1        # 段 id 仍是首个会话
dl = rules.session_deadline(g, t_back, blk["played_seconds"])
assert dl == (t_back + 28.5 * M, "session_timeout"), dl      # 只剩 28.5 分钟，不是重发 60

# 玩掉剩下的 28.5 分钟 → 这一段耗尽
db.heartbeat(conn, s2, 28.5 * M, t_back + 28.5 * M)
db.close_session(conn, s2, int(t_back + 28.5 * M), "session_timeout")
blk = db.current_block(conn, g.id)
assert blk["played_seconds"] == 60 * M and blk["sessions"] == 2
assert played(g.id) == 60
assert rules.block_remaining(g.session_minutes, blk) == 0
# 额度用尽 → 这一段结束，冷却从最后退出时刻起算
t_end = t_back + 28.5 * M
assert not rules.block_alive(blk, g.session_minutes, t_end + 60, GRACE)
v = rules.check_start(g, db.last_session_end(conn, g.id), t_end + 60)
assert not v.allowed and v.reason == "cooldown", v
assert v.unlock_ts == t_end + 20 * 3600                       # 20 小时从真实退出时刻算

# ---- 空闲超窗 → 这一段作废，下次是全新的一场 ----
g2 = db.upsert_game(conn, "另一款", "other.exe", session_minutes=60)
s3 = db.open_session(conn, g2.id, T0)
db.heartbeat(conn, s3, 10 * M, T0 + 10 * M)
db.close_session(conn, s3, int(T0 + 10 * M), "self_exit")
blk2 = db.current_block(conn, g2.id)
assert rules.block_alive(blk2, 60, T0 + 10 * M + 59 * M, GRACE)        # 59 分钟：还算
assert not rules.block_alive(blk2, 60, T0 + 10 * M + 61 * M, GRACE)    # 61 分钟：作废
# 新的一场 = 新段，额度重新给满
s4 = db.open_session(conn, g2.id, int(T0 + 10 * M + 61 * M))
assert db.block_of(db.last_session(conn, g2.id)) == s4 != s3
assert db.current_block(conn, g2.id)["played_seconds"] == 0            # 新段从 0 开始

# ---- 心跳缺失（守护崩了/机器睡了）不该算成游玩 ----
g3 = db.upsert_game(conn, "睡眠", "sleep.exe", session_minutes=60)
s5 = db.open_session(conn, g3.id, T0)
db.heartbeat(conn, s5, 5 * M, T0 + 5 * M)      # 只观测到 5 分钟，之后守护挂了
row = db.last_session(conn, g3.id)
assert row["last_seen_ts"] == T0 + 5 * M
# 守护三小时后重启，发现进程早没了 → 结束时间取 last_seen_ts，不是"现在"
db.close_session(conn, s5, int(row["last_seen_ts"]), "daemon_restart")
assert db.current_block(conn, g3.id)["played_seconds"] == 5 * M        # 空窗不计入
assert db.last_session_end(conn, g3.id) == T0 + 5 * M                  # 冷却也不被推后

# ---- 老数据（没有 played_seconds / block_id）退回墙钟差、各自成段 ----
conn.execute("INSERT INTO sessions (game_id, start_ts, end_ts, end_reason) VALUES (?,?,?,?)",
             (g3.id, T0 + 10_000, T0 + 10_000 + 20 * M, "self_exit"))
conn.commit()
old = db.last_session(conn, g3.id)
assert old["played_seconds"] is None and old["block_id"] is None
assert db.session_played(old) == 20 * M         # 退回 end - start
assert db.block_of(old) == old["id"]            # 自己成段
assert db.current_block(conn, g3.id)["played_seconds"] == 20 * M

print("test_blocks: 全部通过")
