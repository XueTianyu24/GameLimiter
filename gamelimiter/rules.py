"""三规则引擎：纯函数，不碰 DB / 进程。

(a) cooldown_hours     间隔冷却：now >= 上一段游玩结束 + N 小时 才允许启动
    next_allowed_date  下次可玩日：那天（的第一个允许时段）之前一律打不开。两道门独立
                       叠加——冷却管"同一天别连着再来"，日期管跨天规划
(b) session_minutes  单次最长时长（上限）：一段游玩累计**真实在跑**多少分钟
(c) windows          允许时段：仅时段内允许启动；deadline 不晚于当前时段结束
(d) daily_game_limit **全局**：一天内最多玩几款不同的游戏（不挂在单个游戏上）
(e) daily_minutes    **全局**：一天内所有游戏加起来最多玩多少分钟（跨游戏总额）；
                     周末可另设一档（`daily_minutes_weekend`），未设则全周同一个数
a/b/c 三条按游戏配置、可叠加；deadline 取最早者。规则收紧立即生效（每轮循环重算 deadline）。
d/e 两条是全局的：d 管"开几款"，e 管"总共玩多久"——只限款数挡不住每款都玩到天亮。

规则 b 是**上限**：每次会话可另给一个"本次额度"（`limit_minutes`），与上限取更严者。
额度只能收紧不能放宽——这是防冲动的核心，见 `changes.set_next_session`。

**一段游玩（block）**：额度按进程真实在跑的时间消耗，中途退出即暂停、剩余保留；
退出后 `IDLE_GRACE_MINUTES` 内再打开算接着玩同一段（额度接着用、不查冷却）。
额度耗尽或空闲超时 → 这一段结束，冷却才从最后退出时刻起算。见 `block_alive`。
"""

from dataclasses import dataclass
from datetime import date as dt_date
from datetime import datetime, timedelta
from datetime import time as dt_time
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


def is_weekend(now_ts: float) -> bool:
    """今天算不算周末（周六、周日）。规则 e 的周末档按它选。

    按**当前自然日**判，不按会话开始那天——跨午夜从周五进周六，剩下的时间就该吃周六的额度。
    """
    return datetime.fromtimestamp(now_ts).weekday() >= 5


def day_bounds(now_ts: float) -> tuple[float, float]:
    """now 所在自然日的 [0:00, 次日 0:00)。"""
    d = datetime.fromtimestamp(now_ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp(), (d + timedelta(days=1)).timestamp()


def check_daily_limit(limit: Optional[int], today: dict, game_id: int,
                      now_ts: float) -> StartVerdict:
    """全局规则 (d)：一天内最多玩几款**不同**的游戏。

    `today` = {game_id: 名称}，今天已开过的那几款。已在名单里的不受限（继续玩自己的），
    只挡今天还没碰过的新游戏——否则收紧数值会把当天已经在玩的也一起锁死。
    """
    if not limit or game_id in today or len(today) < limit:
        return StartVerdict(True)
    _, tomorrow = day_bounds(now_ts)
    names = "、".join(today.values())
    return StartVerdict(False, "daily_game_limit",
                        f"今天已经玩过 {len(today)} 款（{names}），达到每天最多 {limit} 款的上限；"
                        f"这款今天还没玩过，明天 0:00 后可以开", tomorrow)


def check_daily_minutes(limit_minutes: Optional[float], used_seconds: float,
                        now_ts: float) -> StartVerdict:
    """全局规则 (e)：一天内所有游戏加起来最多玩多少分钟。

    `used_seconds` = 今天已经玩掉的**真实在跑**秒数（跨游戏累加，见 `db.daily_used_seconds`）。
    与规则 d 不同，这条对"今天已经玩过的那款"照样生效——总额用完就是用完了，
    换回第一款游戏接着玩正是要拦的事。

    `limit_minutes` 是**今天适用的那一档**（周末与平日可以是两个数），由调用方按
    `db.effective_daily_minutes` 选好再传进来——本函数不关心今天是周几。
    """
    if not limit_minutes:
        return StartVerdict(True)
    if used_seconds < limit_minutes * 60:
        return StartVerdict(True)
    _, tomorrow = day_bounds(now_ts)
    return StartVerdict(False, "daily_minutes",
                        f"今天所有游戏加起来已玩 {used_seconds/60:.0f} 分钟，"
                        f"达到每天总时长上限 {limit_minutes:g} 分钟；明天 0:00 后额度重置",
                        tomorrow)


WEEKDAY_ZH = "一二三四五六日"


def unlock_datetime(date_str: str, windows: Optional[list]) -> datetime:
    """「下次可玩日」实际解锁的时刻：那天 0:00，有时段规则则顺延到那天第一个时段起点。"""
    day0 = datetime.combine(dt_date.fromisoformat(date_str), dt_time.min)
    if windows and current_window_end(windows, day0) is None:
        nxt = next_window_start(windows, day0)
        if nxt:
            return nxt
    return day0


def is_observed(game: Game) -> bool:
    """观察模式：只采数据、不施加任何限制。

    给 PVP 这类**强杀会判逃跑**的游戏用——想看它的帧时间与硬件数据，
    但绝不能让守护有任何理由去终止它。绕过点有三处，缺一不可：
    启动检查（本文件 `check_start`）、运行中截止（`session_deadline`）、
    以及全局「每天最多玩几款」（在 `db.games_played_between` 里就不计入）。
    """
    return bool(getattr(game, "monitor_only", False))


def check_start(game: Game, last_end_ts: Optional[int], now_ts: float,
                resuming: bool = False) -> StartVerdict:
    """启动前检查：下次可玩日 + 冷却 + 时段。

    `resuming=True` = 接着上一段没玩完的额度继续玩（见 `block_alive`）→ **跳过冷却**。
    冷却管的是"两段游玩之间隔多久"，中途去吃个饭再回来不该被它咬。
    可玩日与时段是硬边界，续玩照查——22:00 到点就是不许再开。
    """
    if is_observed(game):
        return StartVerdict(True)

    now = datetime.fromtimestamp(now_ts)

    # 规则 a 第一道门：锁到某天（跨天规划）。过期即失效，不用手动清
    if game.next_allowed_date:
        unlock = unlock_datetime(game.next_allowed_date, game.windows)
        if now < unlock:
            return StartVerdict(False, "locked_until_date",
                                f"已锁定到 {unlock.strftime('%m月%d日')}"
                                f"（周{WEEKDAY_ZH[unlock.weekday()]}）"
                                f"{unlock.strftime('%H:%M')}，在那之前打不开",
                                unlock.timestamp())

    if game.cooldown_hours and last_end_ts and not resuming:
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


def effective_limit(cap: Optional[float], chosen: Optional[float]) -> Optional[float]:
    """本次会话实际生效的时长（分钟）：上限与本次额度取更严者，都没有则不限。

    额度可以在"上限=不限"的游戏上单独生效（临时给自己设个数），那也是收紧。
    """
    vals = [v for v in (cap, chosen) if v]
    return min(vals) if vals else None


def block_alive(block: Optional[dict], cap_minutes: Optional[float], now_ts: float,
                idle_grace_minutes: float) -> bool:
    """这一段游玩是否还活着——即"再打开就是接着玩"，而不是开新的一场。

    活着的条件：还在玩，或者（额度没用完 且 退出还没超过 `idle_grace_minutes`）。
    额度耗尽 / 空闲太久 → 这一段结束，冷却从最后退出时刻起算。
    """
    if not block:
        return False
    if block["running"]:
        return True
    limit = effective_limit(cap_minutes, block["limit_minutes"])
    if limit and block["played_seconds"] >= limit * 60:
        return False
    return now_ts - block["last_end_ts"] <= idle_grace_minutes * 60


def block_remaining(cap_minutes: Optional[float], block: Optional[dict]) -> Optional[float]:
    """这一段还剩多少秒额度；不限时长返回 None，用尽返回 0。"""
    limit = effective_limit(cap_minutes, block["limit_minutes"] if block else None)
    if not limit:
        return None
    return max(0.0, limit * 60 - (block["played_seconds"] if block else 0.0))


def session_deadline(game: Game, now_ts: float, played_seconds: float = 0.0,
                     limit_minutes: Optional[float] = None,
                     daily_remaining: Optional[float] = None) -> Optional[tuple[float, str]]:
    """运行中会话的最早强制截止 (deadline_ts, reason)；无 b/c/e 规则返回 None。

    时长按**这一段累计的真实游玩秒数** `played_seconds` 算剩余，不按会话开始的墙钟——
    中途退出的时间不该被计入，守护没观测到的空窗期也不该（见 `config.HEARTBEAT_MAX_GAP`）。
    `limit_minutes` = 本段额度，与游戏上限取更严者。
    `daily_remaining` = 今日总时长还剩多少秒（规则 e），None = 没设这条规则。
    走 deadline 而不是直接杀，是为了让它复用现有的多级预警倒计时——PVP 被无预警强杀会判逃跑。
    时段规则：若 now 已在时段外（时段中途结束/规则收紧），deadline=now 立即到点。
    """
    if is_observed(game):
        return None                 # 观察模式永不设截止 = 守护永远不会去杀它
    cands: list[tuple[float, str]] = []
    limit = effective_limit(game.session_minutes, limit_minutes)
    if limit:
        cands.append((now_ts + limit * 60 - played_seconds, "session_timeout"))
    if game.windows:
        end = current_window_end(game.windows, datetime.fromtimestamp(now_ts))
        cands.append((end.timestamp(), "window_end") if end else (now_ts, "window_end"))
    if daily_remaining is not None:
        cands.append((now_ts + max(0.0, daily_remaining), "daily_minutes"))
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
    "daily_minutes": "今天的游玩总时长已用完",
}
