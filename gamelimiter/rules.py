"""三规则引擎：纯函数，不碰 DB / 进程。

(a) cooldown_hours   间隔冷却：now >= 上次会话结束 + N 小时 才允许启动
(b) session_minutes  单次时长：deadline = 会话开始 + N 分钟
(c) windows          允许时段：仅时段内允许启动；deadline 不晚于当前时段结束
三规则可叠加；deadline 取最早者。规则收紧立即生效（每轮循环重算 deadline）。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .db import Game

DAY_MIN = 24 * 60


@dataclass
class StartVerdict:
    allowed: bool
    reason: str = ""            # cooldown / outside_window
    detail: str = ""            # 给弹窗看的中文说明
    unlock_ts: Optional[float] = None


def _parse_window(w: str) -> tuple[int, int]:
    """'19:00-23:00' -> (分钟起, 分钟止)。止<=起视为跨午夜。"""
    a, b = w.split("-")
    h1, m1 = map(int, a.strip().split(":"))
    h2, m2 = map(int, b.strip().split(":"))
    return h1 * 60 + m1, h2 * 60 + m2


def _concrete_intervals(windows: list[str], now: datetime):
    """把时段展开为 now 前后一天内的具体 (start_dt, end_dt) 区间。"""
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for w in windows:
        s, e = _parse_window(w)
        dur = e - s if e > s else e - s + DAY_MIN
        for off in (-1, 0, 1):
            st = day0 + timedelta(days=off, minutes=s)
            yield st, st + timedelta(minutes=dur)


def current_window_end(windows: list[str], now: datetime) -> Optional[datetime]:
    """now 落在某允许时段内则返回该时段结束时刻，否则 None。"""
    for st, en in _concrete_intervals(windows, now):
        if st <= now < en:
            return en
    return None


def next_window_start(windows: list[str], now: datetime) -> Optional[datetime]:
    starts = [st for st, _ in _concrete_intervals(windows, now) if st > now]
    return min(starts) if starts else None


def check_start(game: Game, last_end_ts: Optional[int], now_ts: float) -> StartVerdict:
    """启动前检查：冷却 + 时段。"""
    now = datetime.fromtimestamp(now_ts)

    if game.cooldown_hours and last_end_ts:
        unlock = last_end_ts + game.cooldown_hours * 3600
        if now_ts < unlock:
            t = datetime.fromtimestamp(unlock).strftime("%m-%d %H:%M")
            return StartVerdict(False, "cooldown",
                                f"间隔冷却中：距上次游玩不足 {game.cooldown_hours:g} 小时，"
                                f"{t} 后可再次打开", unlock)

    if game.windows:
        if current_window_end(game.windows, now) is None:
            nxt = next_window_start(game.windows, now)
            t = nxt.strftime("%m-%d %H:%M") if nxt else "（无下个时段）"
            return StartVerdict(False, "outside_window",
                                f"当前不在允许时段（{'、'.join(game.windows)}），"
                                f"最近可玩时间：{t}",
                                nxt.timestamp() if nxt else None)

    return StartVerdict(True)


def session_deadline(game: Game, start_ts: int, now_ts: float) -> Optional[tuple[float, str]]:
    """运行中会话的最早强制截止 (deadline_ts, reason)；无 b/c 规则返回 None。

    时段规则：若 now 已在时段外（时段中途结束/规则收紧），deadline=now 立即到点。
    """
    cands: list[tuple[float, str]] = []
    if game.session_minutes:
        cands.append((start_ts + game.session_minutes * 60, "session_timeout"))
    if game.windows:
        end = current_window_end(game.windows, datetime.fromtimestamp(now_ts))
        cands.append((end.timestamp(), "window_end") if end else (now_ts, "window_end"))
    return min(cands) if cands else None


def coverage(windows: Optional[list[str]]) -> frozenset[int]:
    """允许时段覆盖的一天内分钟集合；无时段规则 = 全天可玩。用于收紧/放宽判定。"""
    if not windows:
        return frozenset(range(DAY_MIN))
    cov = set()
    for w in windows:
        s, e = _parse_window(w)
        dur = e - s if e > s else e - s + DAY_MIN
        cov.update((s + i) % DAY_MIN for i in range(dur))
    return frozenset(cov)


REASON_TEXT = {
    "session_timeout": "本次游玩时长已到",
    "window_end": "允许时段已结束",
}
