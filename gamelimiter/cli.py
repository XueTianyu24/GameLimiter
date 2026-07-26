"""命令行管理工具（GUI 出来前的配置入口 + 调试用）。

示例：
  python -m gamelimiter.cli add 永劫无间 NarakaBladepoint.exe --cooldown 4 --session 90 --windows 19:00-23:00
  python -m gamelimiter.cli list
  python -m gamelimiter.cli set notepad.exe --session off --cooldown 2
  python -m gamelimiter.cli next NarakaBladepoint.exe 60     # 这次只玩 60 分钟
  python -m gamelimiter.cli remove notepad.exe
  python -m gamelimiter.cli history
"""

import argparse
import sys
import time
from datetime import date, datetime, timedelta

from . import changes, db, rules


def _num_or_off(s):
    if s.lower() in ("off", "none", ""):
        return None
    return float(s)


def _date_or_off(s):
    """'2026-08-02' / '+3'（N 天后）/ off。"""
    s = s.strip()
    if s.lower() in ("off", "none", ""):
        return None
    if s.startswith("+"):
        return (datetime.now().date() + timedelta(days=int(s[1:]))).isoformat()
    return date.fromisoformat(s).isoformat()      # 格式不对直接抛，早报错好过存脏数据


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S") if ts else "-"


def cmd_add(conn, a):
    from . import icons
    g = db.upsert_game(conn, a.name, a.exe, exe_path=a.path,
                       cooldown_hours=a.cooldown, session_minutes=a.session,
                       windows=a.windows or None, icon=icons.extract_icon(a.path))
    print(f"已添加/更新 [{g.id}] {g.name} ({g.exe_name})")


def cmd_list(conn, a):
    games = db.list_games(conn)
    limit = db.get_daily_game_limit(conn)
    today = db.games_played_between(conn, *rules.day_bounds(time.time()))
    print(f"全局：每天最多玩 {f'{limit} 款' if limit else '不限'}；今天已玩 {len(today)} 款")
    if not games:
        print("（无受限游戏）")
        return
    for g in games:
        rules_desc = []
        if g.cooldown_hours:
            rules_desc.append(f"冷却 {g.cooldown_hours:g}h")
        if g.session_minutes:
            rules_desc.append(f"单次最长 {g.session_minutes:g}min")
        if g.windows:
            rules_desc.append(f"时段 {'、'.join(g.windows)}")
        if g.next_session_minutes:
            rules_desc.append(f"下次额度 {g.next_session_minutes:g}min")
        if g.next_allowed_date:
            expired = g.next_allowed_date <= date.today().isoformat()
            rules_desc.append(f"下次可玩日 {g.next_allowed_date}" + ("（已过）" if expired else ""))
        state = "" if g.enabled else "（已停用）"
        last = db.last_session_end(conn, g.id)
        print(f"[{g.id}] {g.name} ({g.exe_name}){state} — "
              f"{'；'.join(rules_desc) or '无规则'}；上次结束 {_fmt_ts(last)}")


def cmd_set(conn, a):
    g = db.get_game(conn, a.exe)
    if not g:
        sys.exit(f"未找到 {a.exe}")
    fields = {}
    if a.cooldown is not None:
        fields["cooldown_hours"] = _num_or_off(a.cooldown)
    if a.session is not None:
        fields["session_minutes"] = _num_or_off(a.session)
    if a.windows is not None:
        fields["windows"] = a.windows or None
    if a.until is not None:
        fields["next_allowed_date"] = _date_or_off(a.until)
    if a.enable is not None:
        fields["enabled"] = int(a.enable)
    applied, delayed = changes.request_changes(conn, g, fields)
    if applied:
        print(f"立即生效：{'、'.join(changes.FIELD_ZH[f] for f in applied)}")
    for f, v, apply_at in delayed:
        print(f"放宽延迟：{changes.FIELD_ZH[f]} → {_fmt_ts(apply_at)} 生效")
    if not applied and not delayed:
        print("无变化")


def cmd_next(conn, a):
    """本次/下次游玩额度：不给值=查看，给数值=设置，off=清除。"""
    g = db.get_game(conn, a.exe)
    if not g:
        sys.exit(f"未找到 {a.exe}")
    sess = db.active_session(conn, g.id)
    if a.minutes is None:
        cap = f"{g.session_minutes:g} 分钟" if g.session_minutes else "不限"
        if sess:
            cur = rules.effective_limit(g.session_minutes, sess["limit_minutes"])
            print(f"{g.name} 游玩中：本次 {f'{cur:g} 分钟' if cur else '不限'}"
                  f"（已玩 {(time.time() - sess['start_ts'])/60:.0f} 分钟，上限 {cap}）")
        else:
            print(f"{g.name}：下次额度 "
                  f"{f'{g.next_session_minutes:g} 分钟' if g.next_session_minutes else '（未设，按上限）'}"
                  f"，上限 {cap}")
        return
    v = _num_or_off(a.minutes)
    ok, msg = (changes.shorten_running_session(conn, g, sess, v) if sess
               else changes.set_next_session(conn, g, v))
    print(msg or "无变化")
    if not ok:
        sys.exit(1)


def cmd_daily(conn, a):
    """全局：一天内最多玩几款不同的游戏。不给值=查看，off=取消。"""
    today = db.games_played_between(conn, *rules.day_bounds(time.time()))
    if a.count is None:
        limit = db.get_daily_game_limit(conn)
        print(f"每天最多玩：{f'{limit} 款' if limit else '不限'}；"
              f"今天已玩 {len(today)} 款" + (f"（{'、'.join(today.values())}）" if today else ""))
        for p in changes.global_pendings(conn):
            print(f"  [{p['id']}] 待生效：{changes.describe_pending(p)}")
        return
    v = _num_or_off(a.count)
    status, _, msg = changes.request_daily_limit(conn, v)
    print(msg or "无变化")


def cmd_remove(conn, a):
    g = db.get_game(conn, a.exe)
    if not g:
        sys.exit(f"未找到 {a.exe}")
    apply_at = changes.request_delete(conn, g)
    print(f"已删除 {g.name}" if apply_at is None
          else f"受限游戏删除属于放宽：{_fmt_ts(apply_at)} 生效")


def cmd_pending(conn, a):
    rows = db.list_pending(conn)
    if not rows:
        print("（无待生效变更）")
        return
    for p in rows:
        g = conn.execute("SELECT name FROM games WHERE id=?", (p["game_id"],)).fetchone()
        who = "全局" if p["game_id"] == changes.GLOBAL_GAME_ID else (g["name"] if g else "?")
        print(f"[{p['id']}] {who} — {changes.describe_pending(p)}")
    if a.cancel:
        changes.cancel_pending(conn, a.cancel)
        print(f"已取消 [{a.cancel}]")


def cmd_history(conn, a):
    rows = conn.execute(
        """SELECT s.*, g.name FROM sessions s JOIN games g ON g.id=s.game_id
           ORDER BY s.id DESC LIMIT ?""", (a.limit,)).fetchall()
    for r in rows:
        dur = f"{(r['end_ts'] - r['start_ts'])/60:.1f}min" if r["end_ts"] else "进行中"
        quota = f"  [额度 {r['limit_minutes']:g}min]" if r["limit_minutes"] else ""
        print(f"{r['name']}  {_fmt_ts(r['start_ts'])} → {_fmt_ts(r['end_ts'])}"
              f"  {dur}  {r['end_reason'] or ''}{quota}")
    rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (a.limit,)).fetchall()
    print("--- 事件 ---")
    for r in rows:
        print(f"{_fmt_ts(r['ts'])}  {r['type']}  {r['detail'] or ''}")


def main():
    ap = argparse.ArgumentParser(prog="gamelimiter.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="添加/覆盖游戏")
    p.add_argument("name")
    p.add_argument("exe", help="进程名，如 NarakaBladepoint.exe")
    p.add_argument("--path", default=None)
    p.add_argument("--cooldown", type=float, default=None, help="间隔冷却（小时）")
    p.add_argument("--session", type=float, default=None, help="单次最长时长（分钟，上限）")
    p.add_argument("--windows", nargs="*", default=None, help="允许时段，如 19:00-23:00")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("list", help="列出游戏与规则")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("set", help="改规则（数值或 off）")
    p.add_argument("exe")
    p.add_argument("--cooldown", default=None)
    p.add_argument("--session", default=None)
    p.add_argument("--windows", nargs="*", default=None)
    p.add_argument("--until", default=None,
                   help="下次可玩日：2026-08-02 / +3（3 天后）/ off；往后推即时，提前延迟 24h")
    p.add_argument("--enable", type=int, choices=(0, 1), default=None)
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("next", help="本次/下次游玩额度（≤上限；游玩中只可缩短）")
    p.add_argument("exe")
    p.add_argument("minutes", nargs="?", default=None, help="分钟数或 off；留空=查看")
    p.set_defaults(fn=cmd_next)

    p = sub.add_parser("daily", help="全局：每天最多玩几款游戏（调小即时，调大延迟 24h）")
    p.add_argument("count", nargs="?", default=None, help="款数或 off；留空=查看")
    p.set_defaults(fn=cmd_daily)

    p = sub.add_parser("remove", help="删除游戏")
    p.add_argument("exe")
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("history", help="会话与事件记录")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("pending", help="查看/取消待生效的放宽变更")
    p.add_argument("--cancel", type=int, default=None, help="取消指定 id")
    p.set_defaults(fn=cmd_pending)

    a = ap.parse_args()
    conn = db.connect()
    a.fn(conn, a)


if __name__ == "__main__":
    main()
