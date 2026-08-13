"""观察模式：只采数据、不施加任何限制。

给永劫无间这类 PVP 游戏用——强杀会判逃跑扣分，所以这条路径上守护**不能有任何
理由**去终止它。绕过点有三处，本测试逐一钉死：
  1. 启动检查 check_start
  2. 运行中截止 session_deadline（返回 None = 永不强杀）
  3. 全局「每天最多玩几款」——既不被它拦，也不占别人的名额

同时验证开关本身仍受变更管制：打开观察模式 = 卸限制 = 放宽 = 延迟 24h；关掉 = 收紧 = 立即。

跑法：conda run -n gamelimiter python tests/test_monitor.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gamelimiter import config

config.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"

from gamelimiter import changes, db, rules

conn = db.connect()
T0 = 1_785_000_000
HOUR = 3600

# ---- 一款处处都会被拦的游戏，打开观察模式后应当处处放行 --------------------
g = db.upsert_game(conn, "永劫无间", "NarakaBladepoint.exe",
                   cooldown_hours=20, session_minutes=30,
                   windows=["19:00-20:00"])
db.update_rules(conn, g.id, next_allowed_date="2099-01-01")   # 锁到遥远的未来
g = db.get_game(conn, "NarakaBladepoint.exe")
assert not g.monitor_only

# 限制模式下：四条门任意一条都能拦住（这里最先撞上"下次可玩日"）
v = rules.check_start(g, T0 - HOUR, T0)
assert not v.allowed and v.reason == "locked_until_date", v
assert rules.session_deadline(g, T0) is not None      # 会被强杀

# 打开观察模式
db.update_rules(conn, g.id, monitor_only=1)
g = db.get_game(conn, "NarakaBladepoint.exe")
assert g.monitor_only and rules.is_observed(g)

# 1) 启动检查：无条件放行，连刚玩完都放行
assert rules.check_start(g, T0 - 60, T0).allowed
assert rules.check_start(g, T0 - 60, T0, resuming=False).allowed
# 2) 截止时间：必须是 None —— 守护据此永不强杀
assert rules.session_deadline(g, T0) is None
assert rules.session_deadline(g, T0, played_seconds=99999, limit_minutes=1) is None

# ---- 3) 全局「每天最多玩 1 款」既拦不住它，它也不占别人的名额 ---------------
db.set_daily_game_limit(conn, 1)
other = db.upsert_game(conn, "帕鲁", "Palworld.exe", session_minutes=60)

# 观察模式的游戏玩了一整场
s = db.open_session(conn, g.id, T0)
db.heartbeat(conn, s, 3600, T0 + 3600)
db.close_session(conn, s, T0 + 3600, "self_exit")

today = db.games_played_between(conn, *rules.day_bounds(T0 + 3700))
assert g.id not in today, "观察模式的游戏不该占每天款数的名额"
assert today == {}, today

# 于是帕鲁仍然开得了（名额没被吃掉）
v = rules.check_daily_limit(1, today, other.id, T0 + 3700)
assert v.allowed, v

# 反过来：帕鲁玩掉唯一名额后，观察模式的游戏也不该被款数上限拦
s2 = db.open_session(conn, other.id, T0 + 4000)
db.close_session(conn, s2, T0 + 5000, "self_exit")
today = db.games_played_between(conn, *rules.day_bounds(T0 + 5100))
assert other.id in today and len(today) == 1, today
assert not rules.check_daily_limit(1, today, 999, T0 + 5100).allowed      # 别的新游戏被拦
assert rules.check_start(g, T0 + 3600, T0 + 5100).allowed                 # 观察模式仍放行

# ---- 开关本身受变更管制 ----------------------------------------------------
g2 = db.upsert_game(conn, "另一款", "other.exe", cooldown_hours=5, session_minutes=30)
# 打开观察模式 = 卸掉全部限制 = 放宽 → 延迟 24h
applied, delayed = changes.request_changes(conn, g2, {"monitor_only": 1})
assert not applied and len(delayed) == 1, (applied, delayed)
f, v, apply_at = delayed[0]
assert f == "monitor_only" and v == 1
assert abs(apply_at - (time.time() + config.RELAX_DELAY_HOURS * 3600)) < 5
assert not db.get_game(conn, "other.exe").monitor_only          # 还没生效

p = db.list_pending(conn, g2.id)[0]
assert "观察模式" in changes.describe_pending(p), changes.describe_pending(p)

# 到期后落地
changes.apply_due(conn, apply_at + 1)
g2 = db.get_game(conn, "other.exe")
assert g2.monitor_only

# 关掉观察模式 = 恢复限制 = 收紧 → 立即生效
applied, delayed = changes.request_changes(conn, g2, {"monitor_only": 0})
assert applied == {"monitor_only": 0} and not delayed, (applied, delayed)
assert not db.get_game(conn, "other.exe").monitor_only
# 限制立刻回来
g2 = db.get_game(conn, "other.exe")
assert not rules.check_start(g2, int(time.time()) - 60, time.time()).allowed
assert rules.session_deadline(g2, time.time()) is not None

# ---- 观察模式的游戏可以直接删（本就不受限制，删它不是放宽）------------------
g3 = db.upsert_game(conn, "只看的", "watch.exe")
db.update_rules(conn, g3.id, monitor_only=1)
g3 = db.get_game(conn, "watch.exe")
assert changes.request_delete(conn, g3) is None          # 立即删，不排队
assert db.get_game(conn, "watch.exe") is None

# 对照：受限游戏删除仍走 24h
g4 = db.upsert_game(conn, "受限的", "limited.exe", cooldown_hours=3)
assert changes.request_delete(conn, db.get_game(conn, "limited.exe")) is not None
assert db.get_game(conn, "limited.exe") is not None

# ---- 老库兼容：没有 monitor_only 列的旧行读出来是 False ---------------------
conn.execute("INSERT INTO games (name, exe_name, created_at, updated_at) VALUES (?,?,?,?)",
             ("老游戏", "old.exe", T0, T0))
conn.commit()
old = db.get_game(conn, "old.exe")
assert old.monitor_only is False
assert not rules.is_observed(old)
assert rules.check_start(old, None, T0).allowed          # 无规则本就放行，但不是观察模式

print("test_monitor: 全部通过")
