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
from datetime import datetime
from pathlib import Path

import psutil
from nicegui import app, run, ui

from . import changes, config, db, rules, setup_system, steam, updater
from .version import __version__
from .winutil import (DAEMON_MUTEX, is_frozen, mutex_exists, run_elevated,
                      spawn_detached)

conn = db.connect()
PORT = 8788

# ---------------- 数据/状态 ----------------


def game_state(g: db.Game) -> tuple[str, str, str]:
    """返回 (chip文本, chip颜色类, 副注)。颜色类 = tailwind bg/text。"""
    now = time.time()
    sess = conn.execute(
        "SELECT * FROM sessions WHERE game_id=? AND end_ts IS NULL", (g.id,)).fetchone()
    if not g.enabled:
        return "已停用（不受限制）", "bg-gray-100 text-gray-500", ""
    if sess:
        dl = rules.session_deadline(g, sess["start_ts"], now)
        played = (now - sess["start_ts"]) / 60
        if dl:
            t = datetime.fromtimestamp(dl[0]).strftime("%H:%M")
            left = max(0, (dl[0] - now) / 60)
            return (f"游玩中 · {t} 强制结束（剩 {left:.0f} 分钟）",
                    "bg-blue-100 text-blue-700", f"本次已玩 {played:.0f} 分钟")
        return "游玩中 · 无时长限制", "bg-blue-100 text-blue-700", f"本次已玩 {played:.0f} 分钟"
    v = rules.check_start(g, db.last_session_end(conn, g.id), now)
    if v.allowed:
        extra = f"单次最长 {g.session_minutes:g} 分钟" if g.session_minutes else ""
        return "现在可玩", "bg-green-100 text-green-700", extra
    if v.reason == "cooldown":
        t = datetime.fromtimestamp(v.unlock_ts).strftime("%H:%M")
        left = (v.unlock_ts - now) / 60
        left_s = f"{left/60:.1f} 小时" if left > 90 else f"{left:.0f} 分钟"
        return f"冷却中 · {t} 解锁（还差 {left_s}）", "bg-amber-100 text-amber-700", ""
    t = datetime.fromtimestamp(v.unlock_ts).strftime("%H:%M") if v.unlock_ts else "?"
    return f"时段外 · 最近 {t} 开放", "bg-slate-200 text-slate-600", ""


def parse_windows_input(text: str):
    """'19:00-23:00, 22:00-01:00' -> list 或 None；格式错抛 ValueError。"""
    text = text.replace("，", ",").strip()
    if not text:
        return None
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        rules._parse_window(part)   # 校验
        out.append(part)
    return out or None


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


def game_card(g: db.Game):
    with ui.card().classes("w-[340px] rounded-2xl shadow-md p-4 gap-2 bg-white"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(g.name).classes("text-lg font-bold text-slate-800")
            with ui.row().classes("items-center gap-1"):
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

        ui.label(g.exe_name).classes("text-xs text-slate-400 -mt-2")
        chip = ui.label().classes("px-3 py-1 rounded-full text-sm font-medium")
        sub = ui.label().classes("text-xs text-slate-400")

        def upd(gid=g.id, chip=chip, sub=sub):
            fresh = conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
            if not fresh:
                return
            text, color, extra = game_state(db._row_to_game(fresh))
            chip.text = text
            chip.classes(replace=f"px-3 py-1 rounded-full text-sm font-medium {color}")
            sub.text = extra
            sub.visible = bool(extra)
        upd()
        _updaters.append(upd)

        ui.separator()
        with ui.row().classes("w-full gap-2 items-end"):
            cd = ui.number("冷却(小时)", value=g.cooldown_hours, min=0, step=0.5) \
                .classes("w-[92px]").props("dense")
            sm = ui.number("单次(分钟)", value=g.session_minutes, min=0, step=5) \
                .classes("w-[92px]").props("dense")
            wd = ui.input("允许时段", value="、".join(g.windows) if g.windows else "") \
                .classes("flex-grow").props('dense placeholder="如 19:00-23:00"')

        def save(gid=g.id, cd=cd, sm=sm, wd=wd):
            try:
                windows = parse_windows_input(wd.value.replace("、", ","))
            except ValueError:
                ui.notify("时段格式应为 HH:MM-HH:MM，多段用逗号分隔", type="warning")
                return
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
        for el in (cd, sm, wd):
            el.on("blur", save)
            el.on("keydown.enter", save)

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


@ui.refreshable
def games_view():
    _updaters.clear()
    games = db.list_games(conn)
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
    db.upsert_game(conn, name, exe_name, exe_path=exe_path)
    ui.notify(f"已添加 {name}，在卡片上配置规则", type="positive")
    games_view.refresh()
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


# ---------------- 游玩记录 ----------------


@ui.refreshable
def history_view():
    rows = conn.execute(
        """SELECT s.start_ts, s.end_ts, s.end_reason, g.name FROM sessions s
           JOIN games g ON g.id=s.game_id ORDER BY s.id DESC LIMIT 30""").fetchall()
    blocked = conn.execute(
        """SELECT e.ts, e.detail, g.name FROM events e JOIN games g ON g.id=e.game_id
           WHERE e.type='blocked' ORDER BY e.id DESC LIMIT 15""").fetchall()
    reason_zh = {"self_exit": "自行退出", "session_timeout": "时长到点", "window_end": "时段结束",
                 "daemon_restart": "守护重启", "disabled": "已停用", None: "进行中"}
    with ui.column().classes("w-full gap-1"):
        if not rows and not blocked:
            ui.label("暂无记录").classes("text-slate-400")
        for r in rows:
            t0 = datetime.fromtimestamp(r["start_ts"]).strftime("%m-%d %H:%M")
            dur = f"{(r['end_ts'] - r['start_ts'])/60:.0f} 分钟" if r["end_ts"] else "进行中"
            ui.label(f"{t0}  {r['name']}  玩了 {dur}（{reason_zh.get(r['end_reason'], r['end_reason'])}）") \
                .classes("text-sm text-slate-600")
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
            ok = daemon_running()
            badge.text = "守护运行中" if ok else "守护未运行（限制不生效）"
            badge.classes(replace="px-2 py-0.5 rounded-full text-xs font-medium " +
                          ("bg-green-100 text-green-700" if ok else "bg-red-100 text-red-600"))
            start_btn.visible = not ok
            cfg = setup_system.is_configured()
            sys_badge.text = "强制层已启用" if cfg else "强制层未配置"
            sys_badge.classes(replace="px-2 py-0.5 rounded-full text-xs font-medium " +
                              ("bg-green-100 text-green-700" if cfg else "bg-amber-100 text-amber-700"))
            setup_btn.visible = not cfg
            if cfg:
                start_btn.visible = False   # SYSTEM 自愈任务会拉起守护，无需手动
        upd_badge()
        ui.timer(5.0, upd_badge)

        games_view()
        ui.timer(1.0, lambda: [u() for u in list(_updaters)])

        with ui.expansion("游玩记录", icon="history").classes("w-full bg-white rounded-2xl shadow-sm") \
                .on_value_change(lambda e: history_view.refresh() if e.value else None):
            history_view()


def main():
    native = os.environ.get("GAMELIMITER_WEB") != "1"
    ui.run(native=native, title="GameLimiter", window_size=(1180, 800),
           port=PORT, reload=False, show=not native)


if __name__ in {"__main__", "__mp_main__"}:
    main()
