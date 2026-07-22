"""守护进程：进程监控 + 规则执行。

骨架 = psutil 1s 轮询（兜底 + 会话存活跟踪）；WMI Win32_ProcessStartTrace
事件线程加速启动拦截（需管理员/SYSTEM，普通权限下自动退化为纯轮询）。

运行：conda run -n gamelimiter python -m gamelimiter.daemon
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import psutil

from . import changes, db, rules
from .config import LOG_PATH, POLL_INTERVAL, RULE_RELOAD_INTERVAL, WARN_MINUTES
from .notifier import popup
from .winutil import DAEMON_MUTEX, WATCHDOG_MUTEX, hold_mutex, mutex_exists, spawn_detached

log = logging.getLogger("gamelimiter")


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


@dataclass
class ActiveSession:
    session_id: int
    game_id: int
    start_ts: int
    warned: set = field(default_factory=set)   # 已触发的预警分钟档


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _scan_procs(exe_names: set[str]) -> dict[str, list[psutil.Process]]:
    """一次遍历，返回 {exe_name_lower: [Process,...]}（仅受限名单内的）。"""
    found: dict[str, list[psutil.Process]] = {}
    for p in psutil.process_iter(["name"]):
        try:
            n = (p.info["name"] or "").lower()
        except psutil.Error:
            continue
        if n in exe_names:
            found.setdefault(n, []).append(p)
    return found


def _kill(procs: list[psutil.Process]) -> int:
    n = 0
    for p in procs:
        try:
            p.kill()
            n += 1
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)
    return n


def _wmi_watcher(get_names, wake: threading.Event):
    """WMI 进程创建事件 → 置 wake 让主循环立即扫描。失败则退出（纯轮询兜底）。"""
    try:
        import pythoncom
        import wmi
        pythoncom.CoInitialize()
        watcher = wmi.WMI().Win32_ProcessStartTrace.watch_for()
        log.info("WMI 进程创建事件监听已启用")
        while True:
            try:
                evt = watcher(timeout_ms=2000)
            except wmi.x_wmi_timed_out:
                continue
            if (evt.ProcessName or "").lower() in get_names():
                wake.set()
    except Exception as e:
        log.warning(f"WMI 事件监听不可用（{e.__class__.__name__}: {e}），退回纯轮询")


class Daemon:
    def __init__(self):
        self.conn = db.connect()
        self.games: list[db.Game] = []
        self.active: dict[int, ActiveSession] = {}   # game_id -> ActiveSession
        self.wake = threading.Event()
        self._last_reload = 0.0
        self._last_wd_spawn = 0.0

    # ---- 规则/名单 ----

    def reload_games(self):
        self.games = db.list_games(self.conn, enabled_only=True)
        self._last_reload = time.time()

    def exe_names(self) -> set[str]:
        return {g.exe_name.lower() for g in self.games}

    # ---- 启动时收养/清理遗留会话 ----

    def adopt_sessions(self):
        all_games = {g.id: g for g in db.list_games(self.conn)}
        procs = _scan_procs({g.exe_name.lower() for g in all_games.values()})
        for row in db.open_sessions(self.conn):
            g = all_games.get(row["game_id"])
            alive = g and procs.get(g.exe_name.lower())
            if alive:
                self.active[g.id] = ActiveSession(row["id"], g.id, row["start_ts"])
                log.info(f"收养进行中会话：{g.name}（开始于 {_fmt(row['start_ts'])}）")
            else:
                db.close_session(self.conn, row["id"], int(time.time()), "daemon_restart")

    # ---- 每轮处理 ----

    def _handle_start_attempt(self, g: db.Game, procs: list[psutil.Process], now: float):
        verdict = rules.check_start(g, db.last_session_end(self.conn, g.id), now)
        if not verdict.allowed:
            n = _kill(procs)
            db.log_event(self.conn, g.id, "blocked", f"{verdict.reason}: {verdict.detail}")
            log.info(f"拦截 {g.name}（{verdict.reason}），已终止 {n} 个进程")
            popup(f"GameLimiter — {g.name} 已被拦截", verdict.detail)
            return

        sid = db.open_session(self.conn, g.id, int(now))
        self.active[g.id] = ActiveSession(sid, g.id, int(now))
        dl = rules.session_deadline(g, int(now), now)
        log.info(f"会话开始：{g.name}" + (f"，截止 {_fmt(dl[0])}（{dl[1]}）" if dl else ""))
        if dl:
            popup(f"GameLimiter — {g.name}",
                  f"本次会话已开始，最晚 {_fmt(dl[0])[:5]} 结束"
                  f"（{rules.REASON_TEXT[dl[1]]}前会提前预警）", warn=False)

    def _handle_running(self, g: db.Game, sess: ActiveSession,
                        procs: list[psutil.Process], now: float):
        if not procs:
            db.close_session(self.conn, sess.session_id, int(now), "self_exit")
            del self.active[g.id]
            log.info(f"会话结束（自行退出）：{g.name}")
            return

        dl = rules.session_deadline(g, sess.start_ts, now)
        if dl is None:
            return
        deadline, reason = dl
        remaining = deadline - now

        if remaining <= 0:
            n = _kill(procs)
            db.close_session(self.conn, sess.session_id, int(now), reason)
            db.log_event(self.conn, g.id, "killed", reason)
            del self.active[g.id]
            log.info(f"到点终止：{g.name}（{reason}），终止 {n} 个进程")
            popup(f"GameLimiter — {g.name} 已关闭", rules.REASON_TEXT[reason])
            return

        for m in WARN_MINUTES:
            if remaining <= m * 60 and m not in sess.warned:
                sess.warned.add(m)
                db.log_event(self.conn, g.id, "warn", f"{m}min")
                log.info(f"预警：{g.name} 剩余 {remaining/60:.1f} 分钟（{reason}）")
                popup(f"GameLimiter — {g.name} 剩余 {int(remaining/60) + 1} 分钟",
                      f"{rules.REASON_TEXT[reason]}，将于 {_fmt(deadline)[:5]} 强制关闭，"
                      f"请尽快结束当前对局并自行退出")
                break

    def _ensure_watchdog(self, now: float):
        if os.environ.get("GAMELIMITER_NO_WATCHDOG") == "1":
            return
        if not mutex_exists(WATCHDOG_MUTEX) and now - self._last_wd_spawn > 10:
            self._last_wd_spawn = now
            log.info("watchdog 不在，拉起")
            spawn_detached("--watchdog")

    def tick(self):
        now = time.time()
        if now - self._last_reload > RULE_RELOAD_INTERVAL:
            n = changes.apply_due(self.conn, now)
            if n:
                log.info(f"应用 {n} 条到期的放宽变更")
            self.reload_games()
            self._ensure_watchdog(now)
        procs = _scan_procs(self.exe_names())
        enabled_ids = set()
        for g in self.games:
            enabled_ids.add(g.id)
            plist = [p for p in procs.get(g.exe_name.lower(), []) if p.is_running()]
            sess = self.active.get(g.id)
            if sess is None and plist:
                self._handle_start_attempt(g, plist, now)
            elif sess is not None:
                self._handle_running(g, sess, plist, now)
        # 游戏被禁用/删除但会话还挂着 → 关闭会话，停止跟踪
        for gid in [gid for gid in self.active if gid not in enabled_ids]:
            db.close_session(self.conn, self.active[gid].session_id, int(now), "disabled")
            del self.active[gid]

    def run(self):
        db.log_event(self.conn, None, "daemon_start")
        log.info(f"守护进程启动，DB: {self.conn.execute('PRAGMA database_list').fetchone()[2]}")
        self.reload_games()
        self.adopt_sessions()
        threading.Thread(target=_wmi_watcher, args=(self.exe_names, self.wake),
                         daemon=True).start()
        while True:
            self.wake.wait(timeout=POLL_INTERVAL)
            self.wake.clear()
            try:
                self.tick()
            except Exception:
                log.exception("tick 异常（继续运行）")


def main():
    _setup_logging()
    if not hold_mutex(DAEMON_MUTEX):
        log.info("已有守护进程实例在运行，本实例退出")
        return
    Daemon().run()


if __name__ == "__main__":
    main()
