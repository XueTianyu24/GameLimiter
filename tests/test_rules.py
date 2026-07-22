"""规则引擎单元测试（纯函数，无 DB / 进程依赖）。

跑法：conda run -n gamelimiter python tests/test_rules.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gamelimiter.db import Game
from gamelimiter.rules import check_start, current_window_end, next_window_start, session_deadline


def g(cooldown=None, session=None, windows=None):
    return Game(1, "测试", "test.exe", None, cooldown, session, windows, True)


def ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


# 冷却
v = check_start(g(cooldown=4), int(ts("2026-07-23 10:00")), ts("2026-07-23 12:00"))
assert not v.allowed and v.reason == "cooldown", v
assert v.unlock_ts == ts("2026-07-23 14:00")
v = check_start(g(cooldown=4), int(ts("2026-07-23 10:00")), ts("2026-07-23 14:01"))
assert v.allowed
assert check_start(g(cooldown=4), None, ts("2026-07-23 12:00")).allowed  # 首次玩

# 时段（普通）
w = ["19:00-23:00"]
assert not check_start(g(windows=w), None, ts("2026-07-23 12:00")).allowed
assert check_start(g(windows=w), None, ts("2026-07-23 19:30")).allowed
assert current_window_end(w, datetime.fromisoformat("2026-07-23 19:30")) \
    == datetime.fromisoformat("2026-07-23 23:00")
assert next_window_start(w, datetime.fromisoformat("2026-07-23 12:00")) \
    == datetime.fromisoformat("2026-07-23 19:00")

# 时段（跨午夜 22:00-01:00）
w2 = ["22:00-01:00"]
assert check_start(g(windows=w2), None, ts("2026-07-23 23:30")).allowed
assert check_start(g(windows=w2), None, ts("2026-07-23 00:30")).allowed   # 昨天时段的尾巴
assert not check_start(g(windows=w2), None, ts("2026-07-23 12:00")).allowed
assert current_window_end(w2, datetime.fromisoformat("2026-07-23 00:30")) \
    == datetime.fromisoformat("2026-07-23 01:00")

# 冷却 + 时段叠加：冷却未到优先报冷却
v = check_start(g(cooldown=4, windows=w), int(ts("2026-07-23 18:00")), ts("2026-07-23 19:30"))
assert not v.allowed and v.reason == "cooldown"

# deadline：单次时长
dl = session_deadline(g(session=90), int(ts("2026-07-23 19:00")), ts("2026-07-23 19:30"))
assert dl == (ts("2026-07-23 20:30"), "session_timeout"), dl

# deadline：时长 vs 时段取最早
dl = session_deadline(g(session=300, windows=w), int(ts("2026-07-23 19:00")), ts("2026-07-23 19:30"))
assert dl == (ts("2026-07-23 23:00"), "window_end"), dl

# deadline：会话中途时段已结束（规则收紧）→ 立即到点
now = ts("2026-07-23 23:30")
dl = session_deadline(g(windows=w), int(ts("2026-07-23 19:00")), now)
assert dl == (now, "window_end"), dl

# 无 b/c 规则 → 无 deadline
assert session_deadline(g(cooldown=4), int(ts("2026-07-23 19:00")), ts("2026-07-23 19:30")) is None

print("test_rules: 全部通过")
