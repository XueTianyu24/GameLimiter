"""NiceGUI 卡片式仪表盘：明亮清爽、零层级操作。

- 每游戏一张卡片：状态一眼可见（游玩中/可玩/冷却/时段外），规则直接在卡片上改
- 添加游戏：运行中进程挑选 / 文件选择（exe 或 .lnk 快捷方式）/ 手动输入
- 游玩记录有查看 + 清空入口

运行：python -m gamelimiter.gui   （置 GAMELIMITER_WEB=1 走浏览器模式）
"""

import os
import sys
import time
import webbrowser
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import psutil
from nicegui import app, run, ui

from . import (changes, config, db, frames, icons, procmatch, rules, setup_system,
               stats, steam, updater)
from .version import __version__
from .winutil import (DAEMON_MUTEX, is_frozen, mutex_exists, run_elevated,
                      spawn_detached)

conn = db.connect()
PORT = 8788

# ---------------- 数据/状态 ----------------


def game_state(g: db.Game) -> tuple[str, str, str, bool]:
    """返回 (chip文本, chip颜色类, 副注, 是否游玩中)。颜色类 = tailwind bg/text。"""
    now = time.time()
    sess = db.active_session(conn, g.id)
    block = db.current_block(conn, g.id)
    if not g.enabled:
        return "已停用（不受限制）", "bg-gray-100 text-gray-500", "", bool(sess)
    if sess:
        played = block["played_seconds"] if block else 0.0
        dl = rules.session_deadline(g, now, played, sess["limit_minutes"],
                                    daily_remaining=db.daily_remaining_seconds(conn, now))
        note = f"本段已玩 {played/60:.0f} 分钟"
        if block and block["sessions"] > 1:
            note += f"（分 {block['sessions']} 次）"
        if sess["limit_minutes"]:
            note += f" · 本次额度 {sess['limit_minutes']:g} 分钟"
        if dl:
            t = datetime.fromtimestamp(dl[0]).strftime("%H:%M")
            left = max(0, (dl[0] - now) / 60)
            return (f"游玩中 · {t} 强制结束（剩 {left:.0f} 分钟）",
                    "bg-blue-100 text-blue-700", note, True)
        return "游玩中 · 无时长限制", "bg-blue-100 text-blue-700", note, True

    # 上一段还没玩完 → 现在再打开是"接着玩"，用剩下的额度，不查冷却
    resuming = rules.block_alive(block, g.session_minutes, now, config.IDLE_GRACE_MINUTES)
    # 全局总时长用完时每张卡片都该照实说，而不是各自显示"现在可玩"
    daily_cap = db.effective_daily_minutes(conn, now)      # 今天适用的那一档（平日/周末）
    daily_used = db.daily_used_seconds(conn, now)
    daily = rules.check_daily_minutes(daily_cap, daily_used, now)
    v = (rules.check_start(g, db.last_session_end(conn, g.id), now, resuming=resuming)
         if daily.allowed else daily)
    cap_txt = f"单次最长 {g.session_minutes:g} 分钟" if g.session_minutes else "单次时长不限"
    if v.allowed and resuming:
        left = rules.block_remaining(g.session_minutes, block)
        idle_left = config.IDLE_GRACE_MINUTES - (now - block["last_end_ts"]) / 60
        note = (f"本段已玩 {block['played_seconds']/60:.0f} 分钟"
                f"，{idle_left:.0f} 分钟内再打开算同一段（之后这段作废、进入冷却）")
        return (f"可接着玩 · 本段还剩 {left/60:.0f} 分钟" if left else "可接着玩 · 无时长限制",
                "bg-teal-100 text-teal-700", note, False)
    if v.allowed:
        extra = (f"下次限 {g.next_session_minutes:g} 分钟（{cap_txt}）"
                 if g.next_session_minutes else
                 (cap_txt if g.session_minutes else ""))
        return "现在可玩", "bg-green-100 text-green-700", extra, False
    if v.reason == "daily_minutes":
        return ("今日总时长已用完 · 明天 0:00 重置", "bg-rose-100 text-rose-700",
                f"今天所有游戏加起来已玩 {daily_used/60:.0f} 分钟"
                f"（上限 {daily_cap:g} 分钟）", False)
    if v.reason == "locked_until_date":
        u = datetime.fromtimestamp(v.unlock_ts)
        days = (u.date() - datetime.fromtimestamp(now).date()).days
        return (f"锁定中 · {u.strftime('%m-%d')}（周{rules.WEEKDAY_ZH[u.weekday()]}）"
                f"{u.strftime('%H:%M')} 开放",
                "bg-violet-100 text-violet-700",
                f"还有 {days} 天" if days > 0 else "今天晚些时候开放", False)
    if v.reason == "cooldown":
        t = datetime.fromtimestamp(v.unlock_ts).strftime("%H:%M")
        left = (v.unlock_ts - now) / 60
        left_s = f"{left/60:.1f} 小时" if left > 90 else f"{left:.0f} 分钟"
        return f"冷却中 · {t} 解锁（还差 {left_s}）", "bg-amber-100 text-amber-700", "", False
    t = datetime.fromtimestamp(v.unlock_ts).strftime("%H:%M") if v.unlock_ts else "?"
    return f"时段外 · 最近 {t} 开放", "bg-slate-200 text-slate-600", "", False


def daemon_running() -> bool:
    """探测守护进程的命名互斥体（与启动方式无关）。"""
    return mutex_exists(DAEMON_MUTEX)


def start_daemon():
    spawn_detached("--daemon")
    ui.notify("守护进程已启动", type="positive")


def windowed_processes() -> dict[str, str]:
    """有可见窗口的进程 {exe_name: exe_path}（挑游戏用，避开一堆系统进程）。"""
    import win32gui
    import win32process
    pids = set()

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            pids.add(pid)
    win32gui.EnumWindows(cb, None)
    out = {}
    for pid in pids:
        try:
            p = psutil.Process(pid)
            out[p.name()] = p.exe() or ""
        except psutil.Error:
            continue
    return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))


def resolve_lnk(path: str) -> str:
    if path.lower().endswith(".lnk"):
        import win32com.client
        return win32com.client.Dispatch("WScript.Shell").CreateShortCut(path).Targetpath
    return path


# ---------------- 卡片 ----------------

_updaters: list = []   # 每秒刷新的闭包（只改状态文字，不重建输入框）


def game_avatar(g: db.Game):
    """游戏图标；没提取到就退回首字母色块（名称首字，中文名直接显示该字）。"""
    box = "w-9 h-9 rounded-lg shrink-0"
    if g.icon:
        # 原生 <img> 而非 ui.image：后者是 Quasar q-img，走 CSS background-image，
        # 长 data URI 塞进 url() 渲染不出来（实测空白框）
        ui.html(f'<img src="{g.icon}" alt="" '
                f'style="width:36px;height:36px;object-fit:contain;border-radius:8px">') \
            .classes("shrink-0 leading-none")
    else:
        ui.label(g.name[:1].upper()).classes(
            f"{box} bg-sky-100 text-sky-600 font-bold flex items-center justify-center")


def quick_dates(today: date) -> list[tuple[str, date]]:
    """卡片上的快捷「下次可玩日」：明天 / 后天 / 下一个周六（今天就是周六则下周六）。"""
    sat = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    return [("明天", today + timedelta(1)), ("后天", today + timedelta(2)), ("周六", sat)]


CAPTURE_ARM_HOURS = 4.0        # 下单后待命多久没等到游戏就作废


def capture_status(gid: int) -> tuple[str, bool]:
    """(状态文字, 有没有活跃任务)。给卡片每秒刷新用。"""
    job = db.active_capture_job(conn, gid)
    if job is None:
        return "", False
    if job["state"] == "armed":
        left = (job["expires_at"] - time.time()) / 3600
        return f"采集待命中 · 打开游戏就开始（还有 {left:.1f} 小时）", True
    if job["duration_minutes"] and job["started_ts"]:
        left = max(0.0, job["started_ts"] + job["duration_minutes"] * 60 - time.time())
        return f"采集中 · 还剩 {int(left // 60)}:{int(left % 60):02d}", True
    return "采集中 · 一直采到游戏退出", True


def open_capture_dialog(g: db.Game):
    """下一单性能采集：时长 + 存放目录 + 要不要留原始数据。"""
    with ui.dialog() as dlg, ui.card().classes("w-[440px] rounded-2xl gap-3"):
        ui.label(f"采集「{g.name}」的性能数据").classes("text-lg font-bold text-slate-800")
        ui.label("下单后打开游戏就开始采，到点自动停（游戏照玩，不受影响）。"
                 "采集是旁路记录，不改任何限制规则。").classes("text-xs text-slate-500")

        ui.label("采多久").classes("text-sm font-medium text-slate-600 -mb-1")
        mins = ui.number(value=10, min=0, step=5, suffix="分钟") \
            .classes("w-full").props("dense")
        ui.toggle({5: "5 分钟", 10: "10 分钟", 30: "30 分钟", 60: "1 小时", 0: "整场"},
                  value=10, on_change=lambda e: mins.set_value(e.value)) \
            .props("dense unelevated toggle-color=indigo size=sm")
        ui.label("填 0 = 一直采到游戏退出").classes("text-xs text-slate-400 -mt-1")

        ui.label("数据存放到").classes("text-sm font-medium text-slate-600 -mb-1")
        with ui.row().classes("w-full items-center gap-1 flex-nowrap"):
            out = ui.input(value=db.get_capture_out_dir(conn) or "",
                           placeholder=str(frames.capture_dir())) \
                .classes("flex-grow").props("dense") \
                .tooltip("留空 = 默认数据目录。写盘的是 SYSTEM 身份的守护进程，"
                         "用户级映射的网络盘它看不到，会自动回落默认目录")

            async def pick_dir():
                win = app.native.main_window
                if win is None:
                    ui.notify("浏览器模式不支持目录选择，请直接输入路径", type="warning")
                    return
                import webview
                try:
                    res = await run.io_bound(win.create_file_dialog,
                                             webview.FileDialog.FOLDER)
                except Exception as e:
                    ui.notify(f"打开目录选择框失败：{e}", type="negative")
                    return
                if res:
                    out.set_value(res[0])
            ui.button("浏览…", on_click=pick_dir).props("flat dense size=sm color=grey")
        keep = ui.switch("保留原始逐帧数据", value=True).props("dense color=indigo") \
            .tooltip("一小时约 260 MB。关掉则只留约 10 KB 的摘要")
        ui.label("硬件数据（1 Hz，两小时约 700 KB）一律保留").classes("text-xs text-slate-400")

        def submit():
            minutes = float(mins.value or 0) or None
            path = (out.value or "").strip()
            db.set_capture_out_dir(conn, path)
            db.create_capture_job(conn, g.id, minutes, path or None, keep.value,
                                  int(time.time() + CAPTURE_ARM_HOURS * 3600))
            dlg.close()
            how_long = f"{minutes:g} 分钟" if minutes else "整场"
            if not daemon_running():
                ui.notify("已下单，但守护进程没在跑——它不启动就不会采集", type="warning")
            elif db.active_session(conn, g.id):
                ui.notify(f"开始采集（{how_long}）", type="positive")
            else:
                ui.notify(f"已下单（{how_long}）：{CAPTURE_ARM_HOURS:g} 小时内打开游戏就开始采",
                          type="positive")
            games_view.refresh()

        with ui.row().classes("w-full justify-end gap-2 mt-1"):
            ui.button("取消", on_click=dlg.close).props("flat color=grey")
            ui.button("开始采集", on_click=submit).props("unelevated color=indigo")
    dlg.open()


def game_card(g: db.Game):
    with ui.card().classes("w-[340px] rounded-2xl shadow-md p-4 gap-2 bg-white"):
        with ui.row().classes("w-full items-center justify-between flex-nowrap"):
            with ui.row().classes("items-center gap-2.5 min-w-0 flex-nowrap"):
                game_avatar(g)
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(g.name).classes(
                        "text-lg font-bold text-slate-800 leading-tight truncate")
                    ui.label(g.exe_name).classes("text-xs text-slate-400 leading-tight truncate")
            with ui.row().classes("items-center gap-1 flex-nowrap"):
                sw = ui.switch(value=g.enabled)
                sw.props("dense color=green").tooltip("启用/停用限制")

                def on_toggle(e, gid=g.id):
                    fresh = db._row_to_game(
                        conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone())
                    _, delayed = changes.request_changes(conn, fresh, {"enabled": int(e.value)})
                    if delayed:
                        t = datetime.fromtimestamp(delayed[0][2]).strftime("%m-%d %H:%M")
                        ui.notify(f"停用属于放宽，{t} 才生效（防冲动）", type="warning")
                    games_view.refresh()
                sw.on_value_change(on_toggle)

                def confirm_delete(gid=g.id, name=g.name):
                    with ui.dialog() as d, ui.card():
                        ui.label(f"删除「{name}」？受限游戏的删除延迟 24 小时生效（防冲动）。")
                        with ui.row():
                            ui.button("取消", on_click=d.close).props("flat")

                            def do():
                                fresh = db._row_to_game(conn.execute(
                                    "SELECT * FROM games WHERE id=?", (gid,)).fetchone())
                                apply_at = changes.request_delete(conn, fresh)
                                d.close()
                                if apply_at:
                                    t = datetime.fromtimestamp(apply_at).strftime("%m-%d %H:%M")
                                    ui.notify(f"已登记删除，{t} 生效；期间可随时取消", type="warning")
                                games_view.refresh()
                            ui.button("删除", color="red", on_click=do)
                    d.open()
                ui.button(icon="delete_outline", on_click=confirm_delete) \
                    .props("flat dense round color=grey")

        chip = ui.label().classes("px-3 py-1 rounded-full text-sm font-medium")
        sub = ui.label().classes("text-xs text-slate-400")

        # 本次/下次游玩额度：规则 b 的一次性收紧（≤上限；游玩中只许缩短）
        sess0 = db.active_session(conn, g.id)
        q_init = (sess0["limit_minutes"] if sess0 else g.next_session_minutes) or g.session_minutes
        with ui.row().classes("w-full items-center gap-1 flex-nowrap"):
            q_label = ui.label().classes("text-xs text-slate-500 shrink-0")
            ui.button(icon="remove", on_click=lambda: bump(-30)) \
                .props("flat dense round size=sm color=grey").tooltip("减 30 分钟")
            q = ui.number(value=q_init, min=0, step=30).classes("w-[62px]").props("dense") \
                .tooltip("本次实际能玩多久（可直接输入；不超过上限）")
            ui.button(icon="add", on_click=lambda: bump(30)) \
                .props("flat dense round size=sm color=grey").tooltip("加 30 分钟")
            ui.label("分钟").classes("text-xs text-slate-500 shrink-0")
            ui.space()
            q_full = ui.button("用满上限", on_click=lambda: (q.set_value(None), commit_quota())) \
                .props("flat dense size=sm color=grey")

        def commit_quota(gid=g.id, q=q):
            fresh = db._row_to_game(
                conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone())
            sess = db.active_session(conn, gid)
            val = q.value
            if sess:
                ok, msg = changes.shorten_running_session(conn, fresh, sess, val)
            else:
                if fresh.session_minutes and val and val >= fresh.session_minutes:
                    val = None                      # 顶到上限 = 不额外限制
                ok, msg = changes.set_next_session(conn, fresh, val)
            if not msg:                             # 值没变（blur 空转）→ 别重建卡片
                return
            ui.notify(msg, type="positive" if ok else "warning")
            games_view.refresh()                    # 重建卡片：被拒时输入框自动回填真实值

        def bump(d, q=q):
            q.value = max(0, round((q.value or 0) + d))
            commit_quota()
        q.on("blur", commit_quota)
        q.on("keydown.enter", commit_quota)

        # 性能采集：点了才采（v0.16.0 起）。按钮/倒计时/停止三态由下面的 upd 每秒切换
        with ui.row().classes("w-full items-center gap-1 flex-nowrap"):
            cap_btn = ui.button("采集性能数据", icon="insights",
                                on_click=lambda g=g: open_capture_dialog(g)) \
                .props("flat dense size=sm color=indigo") \
                .tooltip("记录这次游玩的帧时间与硬件状态，可选时长与存放目录")
            cap_txt = ui.label().classes("text-xs text-indigo-600 truncate")
            ui.space()

            def stop_capture(gid=g.id):
                job = db.active_capture_job(conn, gid)
                if job and db.cancel_capture_job(conn, job["id"]):
                    ui.notify("已停止采集" + ("，数据正在收尾入库" if job["state"] == "running"
                                              else "（还没开始，直接取消）"), type="positive")
                games_view.refresh()
            cap_stop = ui.button("停止", on_click=stop_capture) \
                .props("flat dense size=sm color=red")

        def upd(gid=g.id, chip=chip, sub=sub, q_label=q_label, q_full=q_full,
                cap_btn=cap_btn, cap_txt=cap_txt, cap_stop=cap_stop,
                seen={"in": bool(sess0)}):
            fresh = conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
            if not fresh:
                return
            text, color, extra, in_sess = game_state(db._row_to_game(fresh))
            chip.text = text
            chip.classes(replace=f"px-3 py-1 rounded-full text-sm font-medium {color}")
            sub.text = extra
            sub.visible = bool(extra)
            q_label.text = "本次玩" if in_sess else "下次玩"
            q_full.visible = not in_sess            # 游玩中不许放回上限
            cap_text, capturing = capture_status(gid)
            cap_txt.text = cap_text
            cap_txt.visible = capturing
            cap_btn.visible = not capturing
            cap_stop.visible = capturing
            if in_sess != seen["in"]:
                seen["in"] = in_sess                # 会话开/关 → 额度框的值与语义都变了
                games_view.refresh()
                global_rule_view.refresh()          # 今日已玩款数也跟着变
        upd()
        _updaters.append(upd)

        multi_windows = bool(g.windows and len(g.windows) > 1)
        cur = g.windows[0] if (g.windows and not multi_windows) else None
        cur_start, cur_end = (cur.split("-") if cur else (None, None))

        ui.separator()
        with ui.row().classes("w-full gap-2 items-end"):
            cd = ui.number("冷却(小时)", value=g.cooldown_hours, min=0, step=0.5) \
                .classes("w-[92px]").props("dense")
            sm = ui.number("最长(分钟)", value=g.session_minutes, min=0, step=30) \
                .classes("w-[92px]").props("dense") \
                .tooltip("单次最长游玩时长（上限）；每次可在上面单独调低本次额度")
            if multi_windows:
                # 多时段只能 CLI 设置；GUI 只读展示，避免下拉误覆盖丢数据
                ui.label("时段 " + "、".join(g.windows) + "（多段，用 CLI 修改）") \
                    .classes("text-xs text-slate-500 flex-grow self-center")
                w_start = w_end = None
            else:
                # 半小时档 + 当前值（CLI 可能设过非整档时间）
                t_opts = sorted({f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)}
                                | ({cur_start, cur_end} - {None}))
                w_start = ui.select(["不限"] + t_opts, value=cur_start or "不限",
                                    label="时段开始").classes("w-[110px]").props("dense")
                w_end = ui.select(t_opts, value=cur_end or "23:00", label="时段结束") \
                    .classes("w-[110px]").props("dense")
                w_end.visible = cur is not None

        def save(gid=g.id, cd=cd, sm=sm, w_start=w_start, w_end=w_end):
            if w_start is None:                     # 多时段卡片：时段不动，只存数值项
                windows = g.windows
            elif w_start.value == "不限":
                windows = None
            else:
                if w_start.value == w_end.value:
                    ui.notify("开始与结束不能相同（跨午夜时段选结束 < 开始即可）",
                              type="warning")
                    return
                windows = [f"{w_start.value}-{w_end.value}"]
            fresh = db._row_to_game(
                conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone())
            applied, delayed = changes.request_changes(conn, fresh, {
                "cooldown_hours": cd.value or None,
                "session_minutes": sm.value or None,
                "windows": windows})
            if not applied and not delayed:
                return
            if delayed and applied:
                ui.notify("收紧项已生效；放宽项 24 小时后生效", type="warning")
            elif delayed:
                t = datetime.fromtimestamp(delayed[0][2]).strftime("%m-%d %H:%M")
                ui.notify(f"放宽属于防冲动管制，{t} 生效", type="warning")
            else:
                ui.notify("规则已保存并立即生效", type="positive")
            games_view.refresh()
        for el in (cd, sm):
            el.on("blur", save)
            el.on("keydown.enter", save)
        if w_start is not None:
            def on_start_change(w_start=w_start, w_end=w_end):
                w_end.visible = w_start.value != "不限"
                save()
            w_start.on_value_change(on_start_change)
            w_end.on_value_change(save)

        # 规则 a 第二道门：下次可玩日（跨天规划）。到了那天仍按时段/时长规则走
        today = date.today()
        locked = g.next_allowed_date and g.next_allowed_date > today.isoformat()
        with ui.row().classes("w-full items-center gap-1 flex-nowrap"):
            ui.label("下次可玩").classes("text-xs text-slate-500 shrink-0")
            # 菜单必须建在 input 的上下文里，否则它锚到整行、弹出位置会跑偏
            with ui.input(value=g.next_allowed_date or "", placeholder="不限") \
                    .classes("w-[108px]").props("dense") as d_in:
                d_in.tooltip("那天之前一律打不开；到了那天按允许时段开放。"
                             "往后推立即生效，提前要等 24 小时")
                with ui.menu().props("no-parent-event") as d_menu:
                    ui.date(value=g.next_allowed_date or None,
                            on_change=lambda e: (d_menu.close(), save_date(e.value)))
                with d_in.add_slot("append"):
                    ui.icon("edit_calendar").classes("cursor-pointer text-slate-400") \
                        .on("click", d_menu.open)
            if not g.next_allowed_date:
                ui.label("· 还没定下次").classes("text-xs text-slate-400")
            elif not locked:
                ui.label("· 已过期，不限").classes("text-xs text-slate-400")
        with ui.row().classes("w-full items-center gap-1 flex-nowrap -mt-1"):
            for label, dd in quick_dates(today):
                ui.button(label, on_click=lambda dd=dd: save_date(dd.isoformat())) \
                    .props("flat dense size=sm color=grey")
            ui.space()
            ui.button("清除", on_click=lambda: save_date(None)) \
                .props("flat dense size=sm color=grey")

        def save_typed(d_in=d_in):
            """手敲日期的兜底通道（不依赖日历弹窗）。"""
            v = (d_in.value or "").strip()
            if v:
                try:
                    v = date.fromisoformat(v).isoformat()
                except ValueError:
                    ui.notify("日期格式应为 2026-08-02", type="warning")
                    games_view.refresh()
                    return
            save_date(v or None)
        d_in.on("blur", save_typed)
        d_in.on("keydown.enter", save_typed)

        def save_date(v, gid=g.id):
            fresh = db._row_to_game(
                conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone())
            applied, delayed = changes.request_changes(
                conn, fresh, {"next_allowed_date": v or None})
            if not applied and not delayed:
                return
            if delayed:
                t = datetime.fromtimestamp(delayed[0][2]).strftime("%m-%d %H:%M")
                ui.notify(f"提前解锁属于放宽，{t} 生效（期间可随时取消）", type="warning")
            else:
                ui.notify(f"已锁定到 {v}，那天之前打不开" if v else "已取消下次可玩日",
                          type="positive")
            games_view.refresh()

        pendings = db.list_pending(conn, g.id)
        if pendings:
            with ui.column().classes("w-full gap-1 mt-1"):
                for p in pendings:
                    with ui.row().classes("w-full items-center gap-1"):
                        ui.icon("hourglass_top").classes("text-amber-500 text-sm")
                        ui.label(changes.describe_pending(p)) \
                            .classes("text-xs text-amber-600 flex-grow")

                        def cancel(pid=p["id"]):
                            changes.cancel_pending(conn, pid)
                            ui.notify("已取消该放宽申请", type="positive")
                            games_view.refresh()
                        ui.button("取消", on_click=cancel).props("flat dense size=sm color=grey")


def backfill_icons(games: list[db.Game]) -> bool:
    """给缺图标的游戏补提取（CLI 添加的、或 v0.8.0 之前就存在的）。返回是否有更新。

    只在 exe_path 存在时尝试；取不到就保持 NULL（卡片退回首字母块），下次打开
    再试一次——路径可能是临时失效（游戏盘没挂载等）。
    """
    changed = False
    for g in games:
        if g.icon or not g.exe_path:
            continue
        uri = icons.extract_icon(g.exe_path)
        if uri:
            db.set_icon(conn, g.id, uri)
            g.icon = uri
            changed = True
    return changed


@ui.refreshable
def global_rule_view():
    """全局规则 d（每天最多玩几款）+ e（每天游玩总时长），不挂在单个游戏卡片上。

    没有任何游戏时通常整块不画；但只要有待生效的全局申请（如拆除强制层）就得画出来，
    否则那条申请没有地方能取消。
    """
    pendings = changes.global_pendings(conn)
    if not db.list_games(conn) and not pendings:
        return
    now = time.time()
    limit = db.get_daily_game_limit(conn)
    minutes = db.get_daily_minutes(conn)
    weekend = db.get_daily_minutes_weekend(conn)
    is_weekend = rules.is_weekend(now)
    eff = (weekend or minutes) if is_weekend else minutes
    used = db.daily_used_seconds(conn, now) / 60
    today = db.games_played_between(conn, *rules.day_bounds(now))
    with ui.card().classes("w-full rounded-2xl shadow-sm p-3 gap-1 bg-white"):
        with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
            ui.icon("today").classes("text-xl text-sky-500")
            ui.label("每天最多玩").classes("text-sm text-slate-600 shrink-0")
            n = ui.number(value=limit, min=0, step=1, placeholder="不限") \
                .classes("w-[64px]").props("dense") \
                .tooltip("一天内最多能开几款不同的游戏；空 = 不限。调小立即生效，调大延迟 24 小时")
            ui.label("款游戏").classes("text-sm text-slate-600 shrink-0")
            if today:
                hit = limit and len(today) >= limit
                ui.label(f"· 今天已玩 {len(today)} 款：{'、'.join(today.values())}"
                         + ("（已用满，新游戏今天打不开）" if hit else "")) \
                    .classes("text-xs " + ("text-amber-600" if hit else "text-slate-400"))
            else:
                ui.label("· 今天还没玩").classes("text-xs text-slate-400")
            ui.space()
            ui.button("不限", on_click=lambda: (n.set_value(None), save_limit())) \
                .props("flat dense size=sm color=grey")

        def save_limit(n=n):
            status, _, msg = changes.request_daily_limit(conn, n.value)
            if status == "nochange":
                return
            ui.notify(msg, type="positive" if status == "applied" else "warning")
            global_rule_view.refresh()
            games_view.refresh()
        n.on("blur", save_limit)
        n.on("keydown.enter", save_limit)

        with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
            ui.icon("hourglass_bottom").classes("text-xl text-sky-500")
            ui.label("每天总共玩").classes("text-sm text-slate-600 shrink-0")
            ui.label("平日").classes("text-xs text-slate-400 shrink-0")
            m = ui.number(value=minutes, min=0, step=10, placeholder="不限") \
                .classes("w-[84px]").props("dense") \
                .tooltip("周一至周五，所有游戏加起来一天最多玩多少分钟；空 = 不限。"
                         "用完后正在玩的那局也会走预警倒计时后关闭。调小立即生效，调大延迟 24 小时")
            ui.label("分钟 · 周末").classes("text-xs text-slate-400 shrink-0")
            w = ui.number(value=weekend, min=0, step=10, placeholder="同平日") \
                .classes("w-[84px]").props("dense") \
                .tooltip("周六日单独一档；空 = 沿用平日的数。"
                         "改动同样是调小立即生效、调大延迟 24 小时")
            ui.label("分钟").classes("text-sm text-slate-600 shrink-0")
            today_zh = "今天是周末" if is_weekend else "今天是平日"
            if eff:
                left = max(0.0, eff - used)
                ui.label(f"· {today_zh}（上限 {eff:g} 分钟），已玩 {used:.0f} 分钟，"
                         f"还剩 {left:.0f} 分钟"
                         + ("（已用满，今天都打不开了）" if left <= 0 else "")) \
                    .classes("text-xs " + ("text-amber-600" if left <= 0 else "text-slate-400"))
            else:
                ui.label(f"· {today_zh}，今天已玩 {used:.0f} 分钟") \
                    .classes("text-xs text-slate-400")
            ui.space()
            ui.button("不限", on_click=lambda: (m.set_value(None), save_minutes())) \
                .props("flat dense size=sm color=grey")

        def _save_minutes(box, for_weekend: bool):
            status, _, msg = changes.request_daily_minutes(conn, box.value, for_weekend)
            if status == "nochange":
                return
            ui.notify(msg, type="positive" if status == "applied" else "warning")
            global_rule_view.refresh()
            games_view.refresh()

        def save_minutes(m=m):
            _save_minutes(m, False)

        def save_weekend(w=w):
            _save_minutes(w, True)
        m.on("blur", save_minutes)
        m.on("keydown.enter", save_minutes)
        w.on("blur", save_weekend)
        w.on("keydown.enter", save_weekend)

        for p in pendings:
            with ui.row().classes("w-full items-center gap-1"):
                ui.icon("hourglass_top").classes("text-amber-500 text-sm")
                ui.label(changes.describe_pending(p)).classes("text-xs text-amber-600 flex-grow")

                def cancel(pid=p["id"]):
                    changes.cancel_pending(conn, pid)
                    ui.notify("已取消该放宽申请", type="positive")
                    global_rule_view.refresh()
                ui.button("取消", on_click=cancel).props("flat dense size=sm color=grey")


@ui.refreshable
def games_view():
    _updaters.clear()
    games = db.list_games(conn)
    backfill_icons(games)
    if not games:
        with ui.column().classes("w-full items-center py-16 gap-2"):
            ui.icon("sports_esports").classes("text-6xl text-slate-300")
            ui.label("还没有受限游戏，点右上角「添加游戏」开始").classes("text-slate-400")
        return
    with ui.row().classes("w-full gap-4"):
        for g in games:
            game_card(g)


# ---------------- 添加游戏 ----------------


def add_game(name: str, exe_name: str, exe_path: str = None):
    name = (name or "").strip() or Path(exe_name).stem
    exe_name = (exe_name or "").strip()
    if not exe_name.lower().endswith(".exe"):
        ui.notify("进程名需以 .exe 结尾", type="warning")
        return False
    g = db.upsert_game(conn, name, exe_name, exe_path=exe_path,
                       icon=icons.extract_icon(exe_path))
    # 记下 exe 指纹：以后文件被改名或复制到别处，仍认得出是这款游戏（见 procmatch）
    procmatch.backfill(conn, [g], db.set_exe_fingerprint)
    ui.notify(f"已添加 {name}，在卡片上配置规则", type="positive")
    games_view.refresh()
    global_rule_view.refresh()      # 第一款游戏加进来时这条全局规则才出现
    return True


def open_add_dialog():
    with ui.dialog() as dlg, ui.card().classes("w-[460px] rounded-2xl"):
        ui.label("添加受限游戏").classes("text-lg font-bold")
        with ui.tabs().classes("w-full") as tabs:
            t1 = ui.tab("运行中进程")
            t2 = ui.tab("选文件/快捷方式")
            t3 = ui.tab("手动输入")
        with ui.tab_panels(tabs, value=t1).classes("w-full"):
            with ui.tab_panel(t1):
                sel = ui.select({}, label="有窗口的进程", with_input=True).classes("w-full")

                def refresh_procs():
                    procs = windowed_processes()
                    sel.set_options({f"{n}|{p}": n for n, p in procs.items()})
                refresh_procs()
                with ui.row().classes("w-full justify-between"):
                    ui.button("刷新列表", on_click=refresh_procs).props("flat")

                    def add_from_proc():
                        if not sel.value:
                            return
                        exe_name, exe_path = sel.value.split("|", 1)
                        if add_game(Path(exe_name).stem, exe_name, exe_path):
                            dlg.close()
                    ui.button("添加", on_click=add_from_proc)
            with ui.tab_panel(t2):
                ui.label("选择游戏 exe、快捷方式(.lnk) 或 Steam 桌面图标(.url)") \
                    .classes("text-sm text-slate-500")

                def add_from_steam(picked: str) -> bool:
                    """Steam .url 图标 → 库解析 → 挑 exe（多候选弹选择框）。"""
                    appid = steam.parse_url_shortcut(picked)
                    if appid is None:
                        ui.notify("不是 Steam 商店游戏的图标，请改用「运行中进程」方式添加",
                                  type="warning")
                        return False
                    found = steam.find_game(appid)
                    if not found:
                        ui.notify("本机 Steam 库中未找到该游戏（未安装或库不在本机）",
                                  type="warning")
                        return False
                    name, install_dir = found
                    cands = steam.candidate_exes(install_dir)
                    if not cands:
                        ui.notify(f"{install_dir} 下没找到可用 exe，请用「运行中进程」方式",
                                  type="warning")
                        return False
                    if len(cands) == 1:
                        return add_game(name, cands[0].name, str(cands[0]))
                    with ui.dialog() as pick_dlg, ui.card().classes("w-[420px]"):
                        ui.label(f"「{name}」找到多个 exe，选真正的游戏进程：") \
                            .classes("font-bold")
                        opts = {str(p): f"{p.name}（{p.stat().st_size/1e6:.0f} MB）"
                                for p in cands[:8]}
                        sel2 = ui.select(opts, value=str(cands[0])).classes("w-full")
                        ui.label("已按可能性排序，第一个通常就是对的") \
                            .classes("text-xs text-slate-400")
                        with ui.row():
                            ui.button("取消", on_click=pick_dlg.close).props("flat")

                            def confirm():
                                p = Path(sel2.value)
                                if add_game(name, p.name, str(p)):
                                    pick_dlg.close()
                                    dlg.close()
                            ui.button("添加", on_click=confirm)
                    pick_dlg.open()
                    return False   # 由选择框负责收尾

                async def pick():
                    win = app.native.main_window
                    if win is None:
                        ui.notify("浏览器模式不支持文件选择，请用其他两种方式", type="warning")
                        return
                    import webview
                    try:
                        # 描述串只能含 \w 和空格（pywebview 过滤器正则），"、/" 等标点会 ValueError
                        res = await run.io_bound(
                            win.create_file_dialog, webview.FileDialog.OPEN,
                            directory=str(Path.home() / "Desktop"),
                            file_types=("游戏或快捷方式 (*.exe;*.lnk;*.url)",))
                    except Exception as e:
                        ui.notify(f"打开文件选择框失败：{e}", type="negative")
                        return
                    if not res:
                        return
                    picked = res[0]
                    if picked.lower().endswith(".url"):
                        if add_from_steam(picked):
                            dlg.close()
                        return
                    target = resolve_lnk(picked)
                    if not target.lower().endswith(".exe"):
                        ui.notify("快捷方式目标不是 exe", type="warning")
                        return
                    if add_game(Path(target).stem, Path(target).name, target):
                        dlg.close()
                ui.button("浏览…", on_click=pick)
            with ui.tab_panel(t3):
                name_in = ui.input("显示名（可留空）").classes("w-full").props("dense")
                exe_in = ui.input("进程名", placeholder="如 NarakaBladepoint.exe") \
                    .classes("w-full").props("dense")

                def add_manual():
                    if add_game(name_in.value, exe_in.value):
                        dlg.close()
                ui.button("添加", on_click=add_manual)
    dlg.open()


# ---------------- 游玩统计 ----------------

# 明亮青蓝色阶（与主色 sky-500 同族），level 0-4
_HEAT_COLORS = ("#eef2f7", "#bae6fd", "#7dd3fc", "#38bdf8", "#0284c7")
_CELL, _GAP, _LABEL_W = 12, 3, 22       # 格子 / 间距 / 星期标签列宽（px）
_heat_year: list = []                   # 当前查看年份，闭包外持有便于 refresh


def _heat_cell_html(cell) -> str:
    if cell is None:
        return f'<div style="width:{_CELL}px;height:{_CELL}px"></div>'
    txt = f"{cell.day.month}月{cell.day.day}日 · " + (
        f"{cell.minutes:.0f} 分钟" if cell.minutes else "没玩")
    return (f'<div title="{escape(txt)}" style="width:{_CELL}px;height:{_CELL}px;'
            f'border-radius:2px;background:{_HEAT_COLORS[cell.level]}"></div>')


def heatmap_html(weeks: list) -> str:
    """整张热力图一次性出 HTML：365 个独立 NiceGUI 元素太重，且要走 WebSocket 同步。"""
    n = len(weeks)
    months = "".join(
        f'<div style="grid-column:{i + 1};white-space:nowrap">{label}</div>'
        for i, label in stats.month_starts(weeks))
    weekdays = "".join(
        f'<div style="line-height:{_CELL}px">{stats.WEEKDAY_LABELS[i] if i % 2 == 0 else ""}</div>'
        for i in range(7))
    cells = "".join(_heat_cell_html(c) for col in weeks for c in col)
    legend = "".join(f'<div style="width:{_CELL}px;height:{_CELL}px;border-radius:2px;'
                     f'background:{c}"></div>' for c in _HEAT_COLORS)
    return f"""
<div style="overflow-x:auto;padding-bottom:4px">
  <div style="display:inline-block">
    <div style="display:grid;grid-template-columns:repeat({n},{_CELL}px);gap:{_GAP}px;
                margin-left:{_LABEL_W + _GAP}px;font-size:10px;color:#94a3b8;margin-bottom:3px">
      {months}
    </div>
    <div style="display:flex;gap:{_GAP}px">
      <div style="display:grid;grid-template-rows:repeat(7,{_CELL}px);gap:{_GAP}px;
                  width:{_LABEL_W}px;font-size:9px;color:#94a3b8;text-align:right">{weekdays}</div>
      <div style="display:grid;grid-auto-flow:column;grid-template-rows:repeat(7,{_CELL}px);
                  gap:{_GAP}px">{cells}</div>
    </div>
    <div style="display:flex;align-items:center;gap:{_GAP}px;justify-content:flex-end;
                margin-top:6px;font-size:10px;color:#94a3b8">
      <span>少</span>{legend}<span>多</span>
    </div>
  </div>
</div>"""


def _summary_card(title: str, s, note: str):
    with ui.column().classes("gap-0.5 px-4 py-3 rounded-xl bg-slate-50 min-w-[190px]"):
        ui.label(title).classes("text-xs text-slate-400")
        ui.label(s.hours_text).classes("text-2xl font-bold text-slate-800 leading-tight")
        ui.label(note).classes("text-xs text-slate-400")


@ui.refreshable
def stats_view():
    today = date.today()
    year = _heat_year[0] if _heat_year else today.year
    week = stats.summary(conn, *stats.week_range(today))
    month = stats.summary(conn, *stats.month_range(today))
    with ui.column().classes("w-full gap-3"):
        with ui.row().classes("gap-3 items-stretch flex-wrap"):
            _summary_card("本周（周一起）", week,
                          f"{week.sessions} 次 · 玩了 {week.days_played} 天")
            _summary_card("本月", month,
                          f"{month.sessions} 次 · 日均 {month.avg_per_played_day / 60:.1f} 小时"
                          if month.days_played else f"{month.sessions} 次")
            if month.by_game:
                with ui.column().classes("gap-1.5 px-4 py-3 rounded-xl bg-slate-50 grow "
                                         "min-w-[280px] justify-center"):
                    ui.label("本月分布").classes("text-xs text-slate-400")
                    for name, mins in month.by_game[:3]:
                        pct = mins / month.minutes * 100 if month.minutes else 0
                        with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                            ui.label(name).classes(
                                "text-sm text-slate-600 truncate w-[104px] shrink-0")
                            # 底槽 + 填充条：百分比相对底槽，不受左右文字宽度干扰
                            with ui.element("div").classes("h-1.5 rounded-full bg-slate-200 grow"):
                                ui.element("div").classes("h-1.5 rounded-full bg-sky-400") \
                                    .style(f"width:{max(pct, 2):.0f}%")
                            ui.label(f"{mins / 60:.1f}h").classes(
                                "text-xs text-slate-400 w-[42px] text-right shrink-0")

        year_sum = stats.summary(conn, date(year, 1, 1), date(year, 12, 31))
        with ui.row().classes("w-full items-center gap-2"):
            years = stats.played_years(conn)
            ui.select(years, value=year, on_change=lambda e: (_heat_year.clear(),
                                                             _heat_year.append(e.value),
                                                             stats_view.refresh())) \
                .props("dense borderless").classes("text-sm")
            ui.label(f"年 · 共 {year_sum.hours_text} · {year_sum.days_played} 天有游玩") \
                .classes("text-sm text-slate-500 -ml-1")
        ui.html(heatmap_html(stats.heatmap(conn, year)))


# ---------------- 游玩记录 ----------------


@ui.refreshable
def history_view():
    rows = conn.execute(
        """SELECT s.*, g.name FROM sessions s
           JOIN games g ON g.id=s.game_id ORDER BY s.id DESC LIMIT 30""").fetchall()
    blocked = conn.execute(
        """SELECT e.ts, e.detail, g.name FROM events e JOIN games g ON g.id=e.game_id
           WHERE e.type='blocked' ORDER BY e.id DESC LIMIT 15""").fetchall()
    reason_zh = {"self_exit": "自行退出", "session_timeout": "时长到点", "window_end": "时段结束",
                 "daemon_restart": "守护重启", "disabled": "已停用", None: "进行中"}
    with ui.column().classes("w-full gap-1"):
        if not rows and not blocked:
            ui.label("暂无记录").classes("text-slate-400")
        prev_block = None
        for r in rows:
            t0 = datetime.fromtimestamp(r["start_ts"]).strftime("%m-%d %H:%M")
            dur = f"{db.session_played(r)/60:.0f} 分钟" if r["end_ts"] else "进行中"
            quota = f"，本次额度 {r['limit_minutes']:g} 分钟" if r["limit_minutes"] else ""
            # 同一段游玩里的第 2 次起标出来，免得看着像"冷却没生效又开了一把"
            same = db.block_of(r) == prev_block
            prev_block = db.block_of(r)
            ui.label(f"{'└ 接着玩  ' if same else ''}{t0}  {r['name']}  玩了 {dur}"
                     f"（{reason_zh.get(r['end_reason'], r['end_reason'])}{quota}）") \
                .classes("text-sm " + ("text-slate-400 pl-3" if same else "text-slate-600"))
        if blocked:
            ui.label("最近拦截").classes("text-sm font-bold text-slate-500 mt-2")
            for r in blocked:
                t0 = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M")
                ui.label(f"{t0}  {r['name']}  {r['detail'].split(':', 1)[-1].strip()}") \
                    .classes("text-xs text-slate-400")

        def confirm_clear():
            with ui.dialog() as d, ui.card():
                ui.label("清空全部游玩记录与事件？不可恢复。")
                with ui.row():
                    ui.button("取消", on_click=d.close).props("flat")

                    def do():
                        conn.execute("DELETE FROM sessions WHERE end_ts IS NOT NULL")
                        conn.execute("DELETE FROM events")
                        conn.commit()
                        d.close()
                        history_view.refresh()
                        ui.notify("已清空", type="positive")
                    ui.button("清空", color="red", on_click=do)
            d.open()
        ui.button("清空记录", on_click=confirm_clear).props("flat dense color=grey").classes("mt-1")


# ---------------- 在线更新 ----------------

_IGNORE_FILE = config.DATA_DIR / "update_ignore.txt"
_upd_prog = {"done": 0, "total": 1}


def _ignored_tag() -> str:
    try:
        return _IGNORE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def open_update_dialog(info: dict):
    with ui.dialog() as d, ui.card().classes("w-[480px] gap-2"):
        ui.label(f"发现新版本 {info['tag']}").classes("text-lg font-bold text-slate-800")
        ui.label(f"当前 v{__version__}").classes("text-xs text-slate-400")
        if info["notes"]:
            with ui.element("div").classes(
                    "w-full max-h-48 overflow-y-auto bg-slate-50 rounded-lg p-2"):
                ui.markdown(info["notes"])
        prog = ui.linear_progress(value=0, show_value=False).classes("w-full")
        prog.visible = False
        status = ui.label("").classes("text-xs text-slate-500")
        ui.timer(0.2, lambda: prog.set_value(
            round(_upd_prog["done"] / max(1, _upd_prog["total"]), 3)))

        async def do_update():
            if not is_frozen():
                ui.notify("开发环境不支持一键更新，请用打包 exe", type="warning")
                return
            if not info["asset_url"]:
                ui.notify("该版本 Release 无 exe 附件，请打开下载页手动更新", type="warning")
                return
            btn_row.visible = False
            prog.visible = True
            _upd_prog.update(done=0, total=info["asset_size"] or 1)
            dest = Path(sys.executable).with_name("GameLimiter.new.exe")

            def cb(done, total):   # io 线程回调只写 dict，UI 由 timer 拉
                _upd_prog.update(done=done, total=total or _upd_prog["total"])

            try:
                status.text = "下载中…"
                await run.io_bound(updater.download, info["asset_url"], dest, cb)
                status.text = "校验新版本（--selftest，首次解包约 10-60 秒）…"
                if not await run.io_bound(updater.verify_exe, dest):
                    raise OSError("新版本自检未通过（文件可能损坏），已中止")
                status.text = "请在 UAC 弹窗中确认，应用将自动重启进新版…"
                if not run_elevated("--apply-update", sys.executable, file=str(dest)):
                    raise OSError("未通过 UAC 授权")
                app.shutdown()
            except Exception as e:                     # noqa: BLE001 反馈到界面
                ui.notify(f"更新失败：{e}", type="negative")
                status.text = ""
                prog.visible = False
                btn_row.visible = True

        def ignore():
            try:
                _IGNORE_FILE.write_text(info["tag"], encoding="utf-8")
            except OSError:
                pass
            d.close()

        with ui.row().classes("w-full justify-end") as btn_row:
            ui.button("稍后", on_click=d.close).props("flat color=grey")
            ui.button("忽略此版本", on_click=ignore).props("flat color=grey")
            ui.button("打开下载页",
                      on_click=lambda: webbrowser.open(info["page"])).props("flat")
            ui.button(f"立即更新（{info['asset_size'] / 1e6:.0f} MB）",
                      icon="download", on_click=do_update).props("color=sky-500")
    d.open()


# ---------------- 页面 ----------------


@ui.page("/")
def main_page():
    ui.dark_mode(False)
    ui.query("body").classes("bg-slate-50")
    with ui.column().classes("w-full max-w-[1120px] mx-auto p-6 gap-4"):
        with ui.row().classes("w-full items-center gap-3"):
            ui.icon("sports_esports").classes("text-3xl text-sky-500")
            ui.label("GameLimiter").classes("text-2xl font-bold text-slate-800")
            ui.label(f"v{__version__}").classes("text-xs text-slate-400 self-end mb-1")

            async def check_updates(manual: bool = True):
                try:
                    info = await run.io_bound(updater.check_latest)
                except Exception as e:               # noqa: BLE001 网络失败
                    if manual:
                        ui.notify(f"检查更新失败：{e}", type="warning")
                    return
                if info and (manual or info["tag"] != _ignored_tag()):
                    open_update_dialog(info)
                elif manual and not info:
                    ui.notify(f"已是最新版本 v{__version__}", type="positive")
            ui.button(icon="system_update_alt", on_click=check_updates) \
                .props("flat dense round size=sm color=grey").tooltip("检查更新")
            ui.timer(3.0, lambda: check_updates(manual=False), once=True)

            badge = ui.label().classes("px-2 py-0.5 rounded-full text-xs font-medium")
            start_btn = ui.button("启动守护", on_click=lambda: (start_daemon(), ui.timer(2.0, upd_badge, once=True))) \
                .props("dense color=orange")
            sys_badge = ui.label().classes("px-2 py-0.5 rounded-full text-xs font-medium")

            def do_setup():
                if run_elevated("--setup-system"):
                    ui.notify("请在 UAC 弹窗中确认；配置约需几秒", type="info")
                    ui.timer(6.0, upd_badge, once=True)
                else:
                    ui.notify("已取消（未通过 UAC 授权）", type="warning")
            setup_btn = ui.button("初始化本机", icon="security", on_click=do_setup) \
                .props("dense color=deep-orange").tooltip(
                    "配置强制层：SYSTEM 守护自启 + 每分钟自愈（需管理员授权一次）")
            ui.space()
            ui.button("添加游戏", icon="add", on_click=open_add_dialog).props("rounded color=sky-500")

        def upd_badge():
            # 探测两条都不许抛：任一异常都会中断刷新，徽标停在旧值/空白，
            # 「初始化本机」按钮跟着常驻，看起来与真的未配置一模一样
            try:
                ok = daemon_running()
            except Exception:                       # noqa: BLE001
                ok = False
            badge.text = "守护运行中" if ok else "守护未运行（限制不生效）"
            badge.classes(replace="px-2 py-0.5 rounded-full text-xs font-medium " +
                          ("bg-green-100 text-green-700" if ok else "bg-red-100 text-red-600"))
            start_btn.visible = not ok
            try:
                cfg = setup_system.is_configured()
            except Exception:                       # noqa: BLE001
                cfg = False
            sys_badge.text = "强制层已启用" if cfg else "强制层未配置"
            sys_badge.classes(replace="px-2 py-0.5 rounded-full text-xs font-medium " +
                              ("bg-green-100 text-green-700" if cfg else "bg-amber-100 text-amber-700"))
            setup_btn.visible = not cfg
            if cfg:
                start_btn.visible = False   # SYSTEM 自愈任务会拉起守护，无需手动
        upd_badge()
        ui.timer(5.0, upd_badge)

        global_rule_view()
        games_view()
        ui.timer(1.0, lambda: [u() for u in list(_updaters)])

        with ui.expansion("游玩统计", icon="insights", value=True) \
                .classes("w-full bg-white rounded-2xl shadow-sm") \
                .on_value_change(lambda e: stats_view.refresh() if e.value else None):
            stats_view()

        with ui.expansion("游玩记录", icon="history").classes("w-full bg-white rounded-2xl shadow-sm") \
                .on_value_change(lambda e: history_view.refresh() if e.value else None):
            history_view()


def main():
    import multiprocessing

    from . import tray
    from .winutil import hold_mutex
    # native 模式下 NiceGUI 会 spawn 一个子进程跑 webview，子进程重新导入模块并
    # 再次执行 main()——单实例检查若不挡在主进程内，会把自己的窗口进程掐死
    if multiprocessing.current_process().name == "MainProcess":
        if not hold_mutex(tray.GUI_MUTEX):
            print("GameLimiter 面板已在运行")
            return
        tray.ensure_autostart()     # 幂等，只在打包 exe 下写 HKCU Run
        tray.ensure_running()
    native = os.environ.get("GAMELIMITER_WEB") != "1"
    # favicon 用 emoji：native 窗口图标来自 exe 自身（assets/app.ico），web 模式
    # 才需要它，而 ico 没打进 exe，给路径反而在打包后失效
    ui.run(native=native, title="GameLimiter", favicon="🎮", window_size=(1180, 800),
           port=PORT, reload=False, show=not native)


if __name__ in {"__main__", "__mp_main__"}:
    main()
