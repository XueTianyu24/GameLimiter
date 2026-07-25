"""游玩统计聚合：周 / 月汇总 + 全年热力图。

数据源是 sessions 表。跨午夜的会话（22:00 玩到 01:30）按自然日切开分摊，
不整段算到开始那天——否则热力图上"哪天玩了多久"会失真。

纯查询 + 纯计算，不碰 UI，便于单测。
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

LEVEL_THRESHOLDS = (0, 30, 60, 120)   # 分钟：>0→1，≥30→2，≥60→3，≥120→4
WEEKDAY_LABELS = ("一", "二", "三", "四", "五", "六", "日")
MONTH_LABELS = ("1月", "2月", "3月", "4月", "5月", "6月",
                "7月", "8月", "9月", "10月", "11月", "12月")


@dataclass
class Summary:
    minutes: float = 0.0
    sessions: int = 0
    days_played: int = 0
    by_game: list = field(default_factory=list)   # [(游戏名, 分钟)]，降序

    @property
    def hours_text(self) -> str:
        h, m = divmod(int(round(self.minutes)), 60)
        return f"{h} 小时 {m} 分" if h else f"{m} 分钟"

    @property
    def avg_per_played_day(self) -> float:
        return self.minutes / self.days_played if self.days_played else 0.0


def _day_slices(start_ts: float, end_ts: float) -> list[tuple[date, float]]:
    """会话时间段 → [(自然日, 该日分钟数)]，跨午夜自动切分。"""
    if end_ts <= start_ts:
        return []
    out = []
    cur = datetime.fromtimestamp(start_ts)
    end = datetime.fromtimestamp(end_ts)
    while cur < end:
        midnight = datetime.combine(cur.date() + timedelta(days=1), datetime.min.time())
        chunk_end = min(midnight, end)
        out.append((cur.date(), (chunk_end - cur).total_seconds() / 60.0))
        cur = chunk_end
    return out


def _sessions(conn, d0: date, d1: date, now: Optional[float] = None):
    """[d0, d1] 内有交集的会话 → [(游戏名, 起, 止)]，进行中的按 now 截断。"""
    now = now if now is not None else datetime.now().timestamp()
    lo = datetime.combine(d0, datetime.min.time()).timestamp()
    hi = datetime.combine(d1 + timedelta(days=1), datetime.min.time()).timestamp()
    rows = conn.execute(
        """SELECT g.name AS name, s.start_ts, s.end_ts FROM sessions s
           JOIN games g ON g.id = s.game_id
           WHERE s.start_ts < ? AND COALESCE(s.end_ts, ?) > ?""",
        (hi, now, lo)).fetchall()
    return [(r["name"], r["start_ts"], r["end_ts"] if r["end_ts"] else now) for r in rows]


def daily_minutes(conn, d0: date, d1: date, now: Optional[float] = None) -> dict:
    """{自然日: 分钟}，只含有游玩的日子。"""
    out: dict = {}
    for _, s, e in _sessions(conn, d0, d1, now):
        for day, mins in _day_slices(s, e):
            if d0 <= day <= d1:
                out[day] = out.get(day, 0.0) + mins
    return out


def summary(conn, d0: date, d1: date, now: Optional[float] = None) -> Summary:
    """区间汇总：总时长、会话数、有游玩的天数、分游戏时长。"""
    per_game: dict = {}
    days: dict = {}
    count = 0
    for name, s, e in _sessions(conn, d0, d1, now):
        slices = [(day, m) for day, m in _day_slices(s, e) if d0 <= day <= d1]
        if not slices:
            continue
        count += 1
        for day, m in slices:
            per_game[name] = per_game.get(name, 0.0) + m
            days[day] = days.get(day, 0.0) + m
    return Summary(minutes=sum(days.values()), sessions=count, days_played=len(days),
                   by_game=sorted(per_game.items(), key=lambda kv: -kv[1]))


def level_of(minutes: float) -> int:
    """分钟 → 热力等级 0-4（0 = 没玩，1 = 玩了但不到 30 分钟）。"""
    if minutes <= 0:
        return 0
    return 1 + sum(1 for t in LEVEL_THRESHOLDS[1:] if minutes >= t)


@dataclass
class HeatCell:
    day: date
    minutes: float
    level: int
    in_year: bool          # 补齐首尾周用的占位格（属于相邻年）


def heatmap(conn, year: int, now: Optional[float] = None) -> list[list[Optional[HeatCell]]]:
    """全年热力图：按周分列，每列 7 格（周一→周日）；首尾周不足处补 None。

    返回 weeks[列][行]，直接对应 CSS grid 的一列一周布局。
    """
    jan1, dec31 = date(year, 1, 1), date(year, 12, 31)
    mins = daily_minutes(conn, jan1, dec31, now)
    start = jan1 - timedelta(days=jan1.weekday())        # 回退到所在周的周一
    weeks: list[list[Optional[HeatCell]]] = []
    cur = start
    while cur <= dec31:
        col: list[Optional[HeatCell]] = []
        for _ in range(7):
            if jan1 <= cur <= dec31:
                m = mins.get(cur, 0.0)
                col.append(HeatCell(cur, m, level_of(m), True))
            else:
                col.append(None)
            cur += timedelta(days=1)
        weeks.append(col)
    return weeks


def month_starts(weeks: list) -> list[tuple[int, str]]:
    """热力图上方的月份刻度：[(列号, '3月')]，每月取该月首次出现的列。"""
    seen, out = set(), []
    for i, col in enumerate(weeks):
        for cell in col:
            if cell and cell.day.month not in seen:
                seen.add(cell.day.month)
                out.append((i, MONTH_LABELS[cell.day.month - 1]))
                break
    return out


def played_years(conn) -> list[int]:
    """有游玩记录的年份（降序）；始终含今年。"""
    rows = conn.execute("SELECT MIN(start_ts) AS lo, MAX(start_ts) AS hi FROM sessions").fetchone()
    this_year = date.today().year
    if not rows or not rows["lo"]:
        return [this_year]
    lo = datetime.fromtimestamp(rows["lo"]).year
    hi = max(datetime.fromtimestamp(rows["hi"]).year, this_year)
    return list(range(hi, lo - 1, -1))


def week_range(today: Optional[date] = None) -> tuple[date, date]:
    """本周一 → 今天。"""
    today = today or date.today()
    return today - timedelta(days=today.weekday()), today


def month_range(today: Optional[date] = None) -> tuple[date, date]:
    """本月 1 号 → 今天。"""
    today = today or date.today()
    return today.replace(day=1), today
