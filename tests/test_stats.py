"""统计聚合单测：跨午夜切分、区间汇总、热力图排布。

用内存 DB 造数据，不碰真实 ProgramData 库。
"""

import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamelimiter import stats  # noqa: E402


def ts(y, mo, d, h=0, mi=0) -> float:
    return datetime(y, mo, d, h, mi).timestamp()


def make_conn(sessions) -> sqlite3.Connection:
    """sessions: [(游戏名, start_ts, end_ts|None)]。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE games (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER,
                               start_ts INTEGER, end_ts INTEGER, end_reason TEXT);
    """)
    names = {}
    for name, s, e in sessions:
        if name not in names:
            names[name] = len(names) + 1
            conn.execute("INSERT INTO games (id, name) VALUES (?,?)", (names[name], name))
        conn.execute("INSERT INTO sessions (game_id, start_ts, end_ts) VALUES (?,?,?)",
                     (names[name], s, e))
    conn.commit()
    return conn


def test_day_slices_across_midnight():
    parts = stats._day_slices(ts(2026, 3, 10, 22, 30), ts(2026, 3, 11, 1, 0))
    assert [d for d, _ in parts] == [date(2026, 3, 10), date(2026, 3, 11)], "应切成两天"
    assert abs(parts[0][1] - 90) < 1e-6 and abs(parts[1][1] - 60) < 1e-6, "两段分钟数不对"
    assert stats._day_slices(ts(2026, 3, 10, 5), ts(2026, 3, 10, 5)) == [], "零长会话应丢弃"
    three = stats._day_slices(ts(2026, 3, 10, 23, 0), ts(2026, 3, 12, 1, 0))
    assert len(three) == 3 and abs(three[1][1] - 24 * 60) < 1e-6, "整跨的中间日应为 1440 分钟"


def test_daily_and_summary():
    conn = make_conn([
        ("永劫无间", ts(2026, 3, 10, 20, 0), ts(2026, 3, 10, 21, 30)),   # 90 分
        ("永劫无间", ts(2026, 3, 10, 23, 0), ts(2026, 3, 11, 0, 30)),    # 60 + 30 跨天
        ("元气骑士", ts(2026, 3, 12, 10, 0), ts(2026, 3, 12, 10, 20)),   # 20 分
    ])
    daily = stats.daily_minutes(conn, date(2026, 3, 1), date(2026, 3, 31))
    assert abs(daily[date(2026, 3, 10)] - 150) < 1e-6, "10 日应为 90+60"
    assert abs(daily[date(2026, 3, 11)] - 30) < 1e-6, "11 日应拿到跨午夜的 30 分"
    assert date(2026, 3, 13) not in daily, "没玩的日子不应出现"

    s = stats.summary(conn, date(2026, 3, 1), date(2026, 3, 31))
    assert abs(s.minutes - 200) < 1e-6 and s.sessions == 3 and s.days_played == 3
    assert s.by_game[0] == ("永劫无间", 180.0), "分游戏应降序且时长正确"
    assert s.hours_text == "3 小时 20 分"

    # 区间裁剪：只看 11 日当天，跨午夜会话只算落在当天的部分
    s2 = stats.summary(conn, date(2026, 3, 11), date(2026, 3, 11))
    assert abs(s2.minutes - 30) < 1e-6 and s2.sessions == 1


def test_open_session_uses_now():
    now = ts(2026, 3, 10, 21, 0)
    conn = make_conn([("永劫无间", ts(2026, 3, 10, 20, 0), None)])
    daily = stats.daily_minutes(conn, date(2026, 3, 10), date(2026, 3, 10), now=now)
    assert abs(daily[date(2026, 3, 10)] - 60) < 1e-6, "进行中的会话应按 now 截断"


def test_levels():
    assert [stats.level_of(m) for m in (0, 1, 29.9, 30, 59, 60, 119, 120, 600)] == \
           [0, 1, 1, 2, 2, 3, 3, 4, 4], "等级分档不对"


def test_heatmap_shape():
    conn = make_conn([("永劫无间", ts(2026, 3, 10, 20, 0), ts(2026, 3, 10, 21, 30))])
    weeks = stats.heatmap(conn, 2026)
    assert all(len(c) == 7 for c in weeks), "每列必须 7 格"
    cells = [c for col in weeks for c in col if c]
    assert len(cells) == 365, f"2026 非闰年应有 365 格，实得 {len(cells)}"
    assert cells[0].day == date(2026, 1, 1) and cells[-1].day == date(2026, 12, 31)
    days = [c.day for c in cells]
    assert days == sorted(days), "日期必须递增"
    # 2026-01-01 是周四 → 首列前 3 格（周一二三）应为占位 None
    assert weeks[0][:3] == [None, None, None] and weeks[0][3].day == date(2026, 1, 1)
    hot = [c for c in cells if c.level > 0]
    assert len(hot) == 1 and hot[0].day == date(2026, 3, 10) and hot[0].level == 3  # 90 分 → 3

    leap = [c for col in stats.heatmap(conn, 2024) for c in col if c]
    assert len(leap) == 366, "闰年应有 366 格"

    marks = stats.month_starts(weeks)
    assert len(marks) == 12 and marks[0][1] == "1月" and marks[-1][1] == "12月"
    assert [i for i, _ in marks] == sorted(set(i for i, _ in marks)), "月份刻度列号应递增不重"


def test_week_month_ranges():
    wed = date(2026, 3, 11)      # 周三
    assert stats.week_range(wed) == (date(2026, 3, 9), wed), "周应从周一起算"
    assert stats.month_range(wed) == (date(2026, 3, 1), wed)


def test_empty_db():
    conn = make_conn([])
    s = stats.summary(conn, date(2026, 3, 1), date(2026, 3, 31))
    assert s.minutes == 0 and s.sessions == 0 and s.by_game == [] and s.hours_text == "0 分钟"
    assert s.avg_per_played_day == 0.0, "没有游玩天数时不应除零"
    assert stats.played_years(conn) == [date.today().year]
    assert all(c.level == 0 for col in stats.heatmap(conn, 2026) for c in col if c)


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print("test_stats: 全部通过")
