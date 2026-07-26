"""规则引擎单元测试（纯函数，无 DB / 进程依赖）。

跑法：conda run -n gamelimiter python tests/test_rules.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gamelimiter.db import Game
from gamelimiter.rules import (check_daily_limit, check_start, current_window_end, day_bounds,
                               effective_limit, next_window_start, session_deadline,
                               unlock_datetime)


def g(cooldown=None, session=None, windows=None, until=None):
    return Game(1, "测试", "test.exe", None, cooldown, session, windows, True,
                next_allowed_date=until)


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

# 本次额度与上限取更严者
assert effective_limit(120, 60) == 60        # 额度更短 → 用额度
assert effective_limit(120, 180) == 120      # 额度超上限（不该发生）→ 上限兜底
assert effective_limit(120, None) == 120     # 没设额度 → 用满上限
assert effective_limit(None, 60) == 60       # 上限不限时额度照样生效
assert effective_limit(None, None) is None
assert effective_limit(120, 0) == 120        # 0 视同未设

# deadline：本次额度 60 分钟压过 120 上限
start = int(ts("2026-07-23 19:00"))
dl = session_deadline(g(session=120), start, ts("2026-07-23 19:30"), 60)
assert dl == (ts("2026-07-23 20:00"), "session_timeout"), dl
# 额度与时段仍取最早
dl = session_deadline(g(session=300, windows=w), start, ts("2026-07-23 19:30"), 60)
assert dl == (ts("2026-07-23 20:00"), "session_timeout"), dl
dl = session_deadline(g(session=300, windows=["19:00-19:30"]), start, ts("2026-07-23 19:10"), 60)
assert dl == (ts("2026-07-23 19:30"), "window_end"), dl
# 上限不限、只给额度
dl = session_deadline(g(), start, ts("2026-07-23 19:10"), 45)
assert dl == (ts("2026-07-23 19:45"), "session_timeout"), dl

# 规则 a 第二道门：下次可玩日
mon = ts("2026-07-26 20:00")                                   # 参照“今天”
v = check_start(g(until="2026-08-02"), None, mon)
assert not v.allowed and v.reason == "locked_until_date", v
assert v.unlock_ts == ts("2026-08-02 00:00") and "08月02日" in v.detail, v.detail
# 有时段规则 → 解锁时刻顺延到那天第一个时段起点
v = check_start(g(until="2026-08-02", windows=w), None, mon)
assert v.unlock_ts == ts("2026-08-02 19:00"), v
assert unlock_datetime("2026-08-02", w) == datetime.fromisoformat("2026-08-02 19:00")
# 跨午夜时段覆盖 00:00 → 那天零点就解锁
assert unlock_datetime("2026-08-02", w2) == datetime.fromisoformat("2026-08-02 00:00")
# 到了那天 / 日期已过 → 不再拦
assert check_start(g(until="2026-08-02"), None, ts("2026-08-02 09:00")).allowed
assert check_start(g(until="2026-07-01"), None, mon).allowed
# 日期门优先于冷却报告（更能说明问题）
v = check_start(g(until="2026-08-02", cooldown=4), int(ts("2026-07-26 19:00")), mon)
assert v.reason == "locked_until_date", v
# 日期到了但冷却没到 → 照常报冷却
v = check_start(g(until="2026-07-26", cooldown=4), int(ts("2026-07-26 19:00")), mon)
assert v.reason == "cooldown", v

# 全局规则 d：每天最多玩几款
now = ts("2026-07-26 21:00")
assert day_bounds(now) == (ts("2026-07-26 00:00"), ts("2026-07-27 00:00"))
two = {1: "永劫无间", 2: "帕鲁"}
assert check_daily_limit(None, two, 3, now).allowed            # 未设 = 不限
assert check_daily_limit(3, two, 3, now).allowed               # 还没到上限
assert check_daily_limit(2, two, 1, now).allowed               # 今天玩过的那款照常开
v = check_daily_limit(2, two, 3, now)                          # 新游戏 → 拦
assert not v.allowed and v.reason == "daily_game_limit", v
assert v.unlock_ts == ts("2026-07-27 00:00") and "永劫无间、帕鲁" in v.detail, v.detail
# 收紧到比今天已玩款数还小：已玩过的不追溯锁死，只挡新的
assert check_daily_limit(1, two, 2, now).allowed
assert not check_daily_limit(1, two, 3, now).allowed
assert check_daily_limit(2, {}, 1, now).allowed                # 今天还没玩

print("test_rules: 全部通过")
