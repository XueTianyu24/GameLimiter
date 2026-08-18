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

from . import changes, config, db, frames, hardware, rules
from .winutil import DAEMON_MUTEX, mutex_exists, safe_console


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


def _fmt_minutes(m):
    return f"{m:g} 分钟" if m else "不限"


def cmd_add(conn, a):
    from . import icons
    existed = db.get_game(conn, a.exe)
    g = db.upsert_game(conn, a.name, a.exe, exe_path=a.path,
                       cooldown_hours=a.cooldown, session_minutes=a.session,
                       windows=a.windows or None, icon=icons.extract_icon(a.path))
    if a.monitor:
        if existed:
            # 已存在的游戏改观察模式 = 卸掉限制 = 放宽，必须走 24h 冷静期
            applied, delayed = changes.request_changes(conn, existed, {"monitor_only": 1})
            for f, v, at in delayed:
                print(f"放宽延迟：改为观察模式 → {_fmt_ts(at)} 生效")
            if applied:
                print("已改为观察模式")
        else:
            # 全新登记的游戏本来就不受任何限制，转观察模式不构成放宽 → 立即生效
            db.update_rules(conn, g.id, monitor_only=1)
            g = db.get_game(conn, a.exe)
    tag = "（观察模式：只采数据，不施加任何限制）" if g.monitor_only else ""
    print(f"已添加/更新 [{g.id}] {g.name} ({g.exe_name}){tag}")


def cmd_list(conn, a):
    games = db.list_games(conn)
    now = time.time()
    limit = db.get_daily_game_limit(conn)
    eff = db.effective_daily_minutes(conn, now)
    today = db.games_played_between(conn, *rules.day_bounds(now))
    used = db.daily_used_seconds(conn, now) / 60
    print(f"全局：每天最多玩 {f'{limit} 款' if limit else '不限'}"
          f"、今天（{'周末' if rules.is_weekend(now) else '平日'}）总时长 {_fmt_minutes(eff)}；"
          f"今天已玩 {len(today)} 款 / {used:.0f} 分钟")
    if not games:
        print("（无受限游戏）")
        return
    for g in games:
        if g.monitor_only:
            last = db.last_session_end(conn, g.id)
            print(f"[{g.id}] {g.name} ({g.exe_name}) — 观察模式：只采集帧与硬件数据，"
                  f"不施加任何限制、不占每天款数名额；上次结束 {_fmt_ts(last)}")
            continue
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
    if a.monitor is not None:
        fields["monitor_only"] = int(a.monitor)
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
        block = db.current_block(conn, g.id)
        if sess:
            cur = rules.effective_limit(g.session_minutes, sess["limit_minutes"])
            print(f"{g.name} 游玩中：本次 {f'{cur:g} 分钟' if cur else '不限'}"
                  f"（本段已玩 {block['played_seconds']/60:.0f} 分钟，上限 {cap}）")
        elif rules.block_alive(block, g.session_minutes, time.time(), config.IDLE_GRACE_MINUTES):
            left = rules.block_remaining(g.session_minutes, block)
            idle_left = config.IDLE_GRACE_MINUTES - (time.time() - block["last_end_ts"]) / 60
            print(f"{g.name}：上一段没玩完——已玩 {block['played_seconds']/60:.0f} 分钟，"
                  f"还剩 {f'{left/60:.0f} 分钟' if left else '不限'}；"
                  f"{idle_left:.0f} 分钟内再打开算接着玩（不查冷却）")
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
    """全局每日规则：最多几款（位置参数）+ 总时长上限（--minutes）。不给值=查看。"""
    now = time.time()
    changed = False
    for value, weekend in ((a.minutes, False), (a.minutes_weekend, True)):
        if value is not None:
            changed = True
            print(changes.request_daily_minutes(conn, _num_or_off(value), weekend)[2] or "无变化")
    if a.count is not None:
        changed = True
        print(changes.request_daily_limit(conn, _num_or_off(a.count))[2] or "无变化")
    if changed:
        return

    today = db.games_played_between(conn, *rules.day_bounds(now))
    limit = db.get_daily_game_limit(conn)
    print(f"每天最多玩：{f'{limit} 款' if limit else '不限'}；"
          f"今天已玩 {len(today)} 款" + (f"（{'、'.join(today.values())}）" if today else ""))
    weekend = db.get_daily_minutes_weekend(conn)
    eff = db.effective_daily_minutes(conn, now)
    used = db.daily_used_seconds(conn, now) / 60
    print(f"每天总时长：平日 {_fmt_minutes(db.get_daily_minutes(conn))} / "
          f"周末 {_fmt_minutes(weekend) if weekend else '同平日'}；"
          f"今天是{'周末' if rules.is_weekend(now) else '平日'}，"
          f"适用 {_fmt_minutes(eff)}，已玩 {used:.0f} 分钟"
          + (f"，还剩 {max(0.0, eff - used):.0f} 分钟" if eff else ""))
    for p in changes.global_pendings(conn):
        print(f"  [{p['id']}] 待生效：{changes.describe_pending(p)}")


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


def cmd_frames(conn, a):
    """帧时间记录：每次游玩采到的 fps / 1% low / 卡顿 / 瓶颈。"""
    if a.on or a.off:
        db.set_setting(conn, frames.SETTING_KEY, "1" if a.on else "0")
        print("帧时间采集已" + ("开启" if a.on else "关闭") + "（下次开游戏生效）")
        return

    state = "开" if frames.enabled(conn) else "关"
    pm = frames.presentmon_path()
    print(f"帧时间采集：{state}；采集器 {pm if pm else '未找到（跑 python scripts/fetch_presentmon.py）'}")
    if a.check:
        ok, msg = frames.preflight()
        print(f"可用性自检：{'可用' if ok else '不可用'} —— {msg}")

    game = None
    if a.exe:
        game = db.get_game(conn, a.exe)
        if not game:
            sys.exit(f"未找到 {a.exe}")
    rows = db.frame_runs(conn, game.id if game else None, limit=a.limit)
    if not rows:
        print("（还没有帧时间记录——玩一次就有了）")
        return

    names = {g.id: g.name for g in db.list_games(conn)}
    for r in rows:
        s = frames.load_summary(r)
        head = (f"{names.get(r['game_id'], '?')}  {_fmt_ts(r['start_ts'])} → "
                f"{_fmt_ts(r['end_ts'])}  段{r['block_id']}")
        if not s.get("frames"):
            why = f"：{s['error']}" if s.get("error") else ""
            print(f"\n{head}  —— 没采到帧（{r['status']}）{why}")
            continue
        print(f"\n{head}  {s['seconds']/60:.1f}min  {s['frames']} 帧"
              + ("  [尾部可能不全]" if r["status"] == "truncated" else ""))
        for line in frames.describe(s):
            print("  " + line)
        if s.get("raw_csv"):
            print("  原始逐帧数据 " + s["raw_csv"])
        trend = s.get("per_minute") or []
        if len(trend) >= 6:                      # 够长才看得出"越玩越卡"
            head_fps = sum(p[1] for p in trend[:3]) / 3
            tail_fps = sum(p[1] for p in trend[-3:]) / 3
            drop = (head_fps - tail_fps) / head_fps * 100 if head_fps else 0
            if abs(drop) >= 5:
                word = "越玩越卡" if drop > 0 else "越玩越顺"
                print(f"  趋势 {word}：开头 {head_fps:.0f} fps → 结尾 {tail_fps:.0f} fps"
                      f"（{abs(drop):.0f}%）")


def cmd_hw(conn, a):
    """游玩期间的硬件采集记录（1 Hz：CPU / 内存 / 磁盘 / GPU / 游戏进程 / 干扰进程）。"""
    if a.on or a.off:
        db.set_setting(conn, hardware.SETTING_KEY, "1" if a.on else "0")
        print("硬件采集已" + ("开启" if a.on else "关闭") + "（下次开游戏生效）")
        return

    print(f"硬件采集：{'开' if hardware.enabled(conn) else '关'}；"
          f"原始数据目录 {hardware.capture_dir()}")
    game = None
    if a.exe:
        game = db.get_game(conn, a.exe)
        if not game:
            sys.exit(f"未找到 {a.exe}")
    rows = db.hw_runs(conn, game.id if game else None, limit=a.limit)
    if not rows:
        print("（还没有硬件采集记录——玩一次就有了）")
        return
    names = {g.id: g.name for g in db.list_games(conn)}
    for r in rows:
        s = hardware.load_summary(r)
        print(f"\n{names.get(r['game_id'], '?')}  {_fmt_ts(r['start_ts'])} → "
              f"{_fmt_ts(r['end_ts'])}  段{r['block_id']}  {r['samples']} 个采样点")
        for line in hardware.describe(s):
            print("  " + line)
        if a.paths:
            print("  原始数据 " + (r["csv_path"] or "-"))


CAPTURE_ARM_HOURS = 4.0        # 下单后待命多久没等到游戏就作废


def _job_line(r, names) -> str:
    dur = f"{r['duration_minutes']:g} 分钟" if r["duration_minutes"] else "采到游戏退出"
    where = r["out_dir"] or "默认目录"
    head = f"[{r['id']}] {names.get(r['game_id'], '?')}  {dur}  → {where}"
    if r["state"] == "armed":
        left = (r["expires_at"] - time.time()) / 3600
        return f"{head}  待命中（{left:.1f} 小时内开游戏就采）"
    if r["state"] == "running":
        if r["duration_minutes"]:
            left = r["started_ts"] + r["duration_minutes"] * 60 - time.time()
            return f"{head}  采集中，还剩 {max(0, left)/60:.1f} 分钟"
        return f"{head}  采集中"
    zh = {"done": "已结束", "cancelled": "已取消", "expired": "已过期"}
    return (f"{head}  {zh.get(r['state'], r['state'])}"
            f"  {_fmt_ts(r['started_ts'])} → {_fmt_ts(r['ended_ts'])}"
            + (f"  {r['note']}" if r["note"] else ""))


def cmd_capture(conn, a):
    """手动采集：下单 → 守护接单 → 到点自动收尾。不下单就不采（v0.16.0 起的默认）。"""
    if a.mode:
        db.set_capture_mode(conn, a.mode)
        print("采集模式已设为 " + ("自动（开游戏就采）" if a.mode == "auto" else
                                   "手动（下单了才采）"))
        return

    names = {g.id: g.name for g in db.list_games(conn)}
    mode = db.get_capture_mode(conn)
    if not a.exe:
        print(f"采集模式：{'自动（开游戏就采）' if mode == 'auto' else '手动（点了才采）'}"
              f"；默认存放目录 {db.get_capture_out_dir(conn) or frames.capture_dir()}")
        rows = db.capture_jobs(conn, limit=a.limit)
        if not rows:
            print("（还没有采集任务——`capture <exe> --minutes 10` 下一单）")
        for r in rows:
            print("  " + _job_line(r, names))
        return

    g = db.get_game(conn, a.exe)
    if not g:
        sys.exit(f"未找到 {a.exe}（先用 add 登记；只想采不想限就 add --monitor）")

    if a.stop:
        job = db.active_capture_job(conn, g.id)
        if not job:
            print(f"{g.name}：当前没有采集任务")
            return
        db.cancel_capture_job(conn, job["id"])
        print(f"已停止采集任务 [{job['id']}]" +
              ("（采集器 1 秒内收尾）" if job["state"] == "running" else "（待命中，直接取消）"))
        return

    if a.minutes is None and not a.whole:
        job = db.active_capture_job(conn, g.id)
        print(f"{g.name}：" + (_job_line(job, names) if job else "当前没有采集任务"))
        for r in db.capture_jobs(conn, g.id, limit=a.limit):
            if not job or r["id"] != job["id"]:
                print("  " + _job_line(r, names))
        return

    minutes = None if a.whole else float(a.minutes)
    if minutes is not None and minutes <= 0:
        sys.exit("采集时长要大于 0（想采整场用 --whole）")
    out_dir = a.out if a.out is not None else db.get_capture_out_dir(conn)
    if a.out is not None:
        db.set_capture_out_dir(conn, a.out)          # 记住这次的选择，下次默认用它
    job_id = db.create_capture_job(conn, g.id, minutes, out_dir, not a.no_keep_raw,
                                   int(time.time() + CAPTURE_ARM_HOURS * 3600))
    running = db.active_session(conn, g.id) is not None
    print(f"采集任务 [{job_id}] 已下单：{g.name}，"
          + (f"{minutes:g} 分钟" if minutes else "采到游戏退出")
          + f"，数据落 {out_dir or frames.capture_dir()}")
    print("  " + ("游戏正在跑，守护 1 秒内开始采集" if running else
                  f"{CAPTURE_ARM_HOURS:g} 小时内打开游戏就开始采；到时没开就作废"))
    if not a.no_keep_raw:
        est = f"约 {minutes * 260 / 60:.0f} MB" if minutes else "一小时约 260 MB"
        print(f"  保留原始逐帧数据（{est}）；不想留加 --no-keep-raw")
    if not mutex_exists(DAEMON_MUTEX):
        print("  警告：守护进程没在跑，它不启动就不会采集")
    if mode == "auto":
        print("  注意：当前是自动模式，不下单也会采（`capture --mode manual` 改回手动）")
    if frames.enabled(conn) and frames.presentmon_path() is None:
        print("  警告：没找到 PresentMon.exe，帧数据采不到（硬件数据不受影响）")


def cmd_history(conn, a):
    rows = conn.execute(
        """SELECT s.*, g.name FROM sessions s JOIN games g ON g.id=s.game_id
           ORDER BY s.id DESC LIMIT ?""", (a.limit,)).fetchall()
    for r in rows:
        dur = f"{db.session_played(r)/60:.1f}min" if r["end_ts"] else "进行中"
        quota = f"  [额度 {r['limit_minutes']:g}min]" if r["limit_minutes"] else ""
        print(f"{r['name']}  {_fmt_ts(r['start_ts'])} → {_fmt_ts(r['end_ts'])}"
              f"  {dur}  段{db.block_of(r)}  {r['end_reason'] or ''}{quota}")
    rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (a.limit,)).fetchall()
    print("--- 事件 ---")
    for r in rows:
        print(f"{_fmt_ts(r['ts'])}  {r['type']}  {r['detail'] or ''}")


def main():
    safe_console()
    ap = argparse.ArgumentParser(prog="gamelimiter.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="添加/覆盖游戏")
    p.add_argument("name")
    p.add_argument("exe", help="进程名，如 NarakaBladepoint.exe")
    p.add_argument("--path", default=None)
    p.add_argument("--cooldown", type=float, default=None, help="间隔冷却（小时）")
    p.add_argument("--session", type=float, default=None, help="单次最长时长（分钟，上限）")
    p.add_argument("--windows", nargs="*", default=None, help="允许时段，如 19:00-23:00")
    p.add_argument("--monitor", action="store_true",
                   help="观察模式：只采帧与硬件数据，不施加任何限制（PVP 游戏用）")
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
    p.add_argument("--monitor", type=int, choices=(0, 1), default=None,
                   help="观察模式开关：1=只观察不限制（放宽，延迟 24h）；0=恢复限制（立即）")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("next", help="本次/下次游玩额度（≤上限；游玩中只可缩短）")
    p.add_argument("exe")
    p.add_argument("minutes", nargs="?", default=None, help="分钟数或 off；留空=查看")
    p.set_defaults(fn=cmd_next)

    p = sub.add_parser("daily", help="全局每日规则：最多几款 + 总时长（调小即时，调大延迟 24h）")
    p.add_argument("count", nargs="?", default=None, help="款数或 off；留空=查看")
    p.add_argument("--minutes", default=None,
                   help="平日（周一至周五）所有游戏加起来最多玩多少分钟，或 off")
    p.add_argument("--minutes-weekend", default=None,
                   help="周末（周六日）单独的分钟数；off = 周末沿用平日的数")
    p.set_defaults(fn=cmd_daily)

    p = sub.add_parser("remove", help="删除游戏")
    p.add_argument("exe")
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("history", help="会话与事件记录")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("frames", help="帧时间记录（fps / 1% low / 卡顿 / 瓶颈）")
    p.add_argument("exe", nargs="?", default=None, help="留空=所有游戏")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--on", action="store_true", help="开启采集")
    p.add_argument("--off", action="store_true", help="关闭采集")
    p.add_argument("--check", action="store_true", help="试跑一次采集器，看当前权限够不够")
    p.set_defaults(fn=cmd_frames)

    p = sub.add_parser("hw", help="硬件采集记录（CPU / 内存 / 磁盘 / GPU / 干扰进程）")
    p.add_argument("exe", nargs="?", default=None, help="留空=所有游戏")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--paths", action="store_true", help="显示原始 CSV 路径（供导出分析）")
    p.add_argument("--on", action="store_true", help="开启采集")
    p.add_argument("--off", action="store_true", help="关闭采集")
    p.set_defaults(fn=cmd_hw)

    p = sub.add_parser("capture", help="手动采集：下单一次采集（时长 / 存放目录自选）")
    p.add_argument("exe", nargs="?", default=None, help="留空=看模式与任务列表")
    p.add_argument("--minutes", type=float, default=None, help="采多少分钟")
    p.add_argument("--whole", action="store_true", help="一直采到游戏退出")
    p.add_argument("--out", default=None, help="数据存放目录（记住作为下次默认）")
    p.add_argument("--no-keep-raw", action="store_true",
                   help="不保留原始逐帧 CSV（默认保留，一小时约 260MB）")
    p.add_argument("--stop", action="store_true", help="停止/取消该游戏的采集任务")
    p.add_argument("--mode", choices=("manual", "auto"), default=None,
                   help="manual=点了才采（默认）；auto=开游戏就自动采")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(fn=cmd_capture)

    p = sub.add_parser("pending", help="查看/取消待生效的放宽变更")
    p.add_argument("--cancel", type=int, default=None, help="取消指定 id")
    p.set_defaults(fn=cmd_pending)

    a = ap.parse_args()
    conn = db.connect()
    a.fn(conn, a)


if __name__ == "__main__":
    main()
