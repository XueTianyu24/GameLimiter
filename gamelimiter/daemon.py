"""守护进程：进程监控 + 规则执行。

骨架 = psutil 1s 轮询（兜底 + 会话存活跟踪）；WMI Win32_ProcessStartTrace
事件线程加速启动拦截（需管理员/SYSTEM，普通权限下自动退化为纯轮询）。

运行：conda run -n gamelimiter python -m gamelimiter.daemon
"""

import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil

from . import changes, config, db, rules
from .config import LOG_PATH, POLL_INTERVAL, RULE_RELOAD_INTERVAL, WARN_MINUTES
from .notifier import popup
from .winutil import DAEMON_MUTEX, WATCHDOG_MUTEX, hold_mutex, mutex_exists, spawn_detached

log = logging.getLogger("gamelimiter")


def _setup_logging():
    """日志进数据目录；无写权限（SYSTEM 先建了 log 的机器上用户身份跑）时
    退回 LOCALAPPDATA——守护崩死 = 完全没限制，比日志分裂严重得多。"""
    fallback = None
    # 轮转：SYSTEM 计划任务每分钟拉一次守护做自愈，日志只涨不消，实测三天 780KB
    def _handler(path):
        return RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")

    try:
        fh = _handler(LOG_PATH)
    except PermissionError:
        alt_dir = Path(os.environ.get("LOCALAPPDATA", ".")) / "GameLimiter"
        alt_dir.mkdir(parents=True, exist_ok=True)
        fallback = alt_dir / "daemon.log"
        fh = _handler(fallback)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[fh, logging.StreamHandler()],
    )
    if fallback:
        log.warning("daemon.log 无写权限（SYSTEM 先建所致），本次日志写 %s；"
                    "SYSTEM 守护启动会自动修复 ACL", fallback)


@dataclass
class ActiveSession:
    session_id: int
    game_id: int
    start_ts: int
    limit_minutes: Optional[float] = None      # 本段额度（≤上限），None=用满上限
    block_id: Optional[int] = None             # 所属游玩段
    carried_seconds: float = 0.0               # 本段之前几次已用掉的游玩秒数
    played_seconds: float = 0.0                # 本次会话累计的真实在跑秒数
    last_seen_ts: float = 0.0                  # 最后一次观测到进程存活的时刻
    warned: set = field(default_factory=set)   # 已触发的预警分钟档

    @property
    def block_played(self) -> float:
        """本段累计真实游玩秒数（含之前几次）。"""
        return self.carried_seconds + self.played_seconds


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
        self.daily_limit: Optional[int] = None       # 全局规则 d，随 reload_games 刷新
        self.active: dict[int, ActiveSession] = {}   # game_id -> ActiveSession
        self.wake = threading.Event()
        self._last_reload = 0.0
        self._last_wd_spawn = 0.0

    # ---- 规则/名单 ----

    def reload_games(self):
        self.games = db.list_games(self.conn, enabled_only=True)
        self.daily_limit = db.get_daily_game_limit(self.conn)
        self._last_reload = time.time()

    def exe_names(self) -> set[str]:
        return {g.exe_name.lower() for g in self.games}

    # ---- 启动时收养/清理遗留会话 ----

    def adopt_sessions(self):
        """守护重启后接管遗留会话。

        进程没了的：结束时间取 `last_seen_ts`（最后一次确认它还活着的时刻），
        **不是现在**——守护没跑的那段时间不该算成游玩，也不该推后冷却起算点。
        """
        now = time.time()
        all_games = {g.id: g for g in db.list_games(self.conn)}
        procs = _scan_procs({g.exe_name.lower() for g in all_games.values()})
        for row in db.open_sessions(self.conn):
            g = all_games.get(row["game_id"])
            alive = g and procs.get(g.exe_name.lower())
            if alive:
                siblings = [r for r in db.block_rows(self.conn, db.block_of(row))
                            if r["id"] != row["id"]]
                self.active[g.id] = ActiveSession(
                    row["id"], g.id, row["start_ts"], row["limit_minutes"],
                    block_id=db.block_of(row),
                    carried_seconds=sum(db.session_played(r) for r in siblings),
                    played_seconds=row["played_seconds"] or 0.0,
                    last_seen_ts=now)          # 空窗期不补计，从现在重新开始心跳
                log.info(f"收养进行中会话：{g.name}（开始于 {_fmt(row['start_ts'])}）")
            else:
                db.close_session(self.conn, row["id"],
                                 int(row["last_seen_ts"] or row["start_ts"]), "daemon_restart")

    # ---- 每轮处理 ----

    def _handle_start_attempt(self, g: db.Game, procs: list[psutil.Process], now: float):
        # 上一段游玩还活着？活着 = 接着玩（额度接着用、跳过冷却），不是开新的一场
        block = db.current_block(self.conn, g.id)
        resuming = rules.block_alive(block, g.session_minutes, now, config.IDLE_GRACE_MINUTES)

        # 全局款数上限先判：它挡的是"今天又开一款新的"，比冷却/时段更能说明问题
        today = db.games_played_between(self.conn, *rules.day_bounds(now))
        verdict = rules.check_daily_limit(self.daily_limit, today, g.id, now)
        if verdict.allowed:
            verdict = rules.check_start(g, db.last_session_end(self.conn, g.id), now,
                                        resuming=resuming)
        if not verdict.allowed:
            n = _kill(procs)
            db.log_event(self.conn, g.id, "blocked", f"{verdict.reason}: {verdict.detail}")
            log.info(f"拦截 {g.name}（{verdict.reason}），已终止 {n} 个进程")
            popup(f"GameLimiter — {g.name} 已被拦截", verdict.detail)
            return

        if resuming:
            block_id, quota = block["block_id"], block["limit_minutes"]
            carried = block["played_seconds"]
        else:
            # 新的一段：此刻才消费本次额度（含内存里的 games 缓存，否则秒退再进会重复用）
            block_id, quota, carried = None, g.next_session_minutes, 0.0
            if quota is not None:
                db.set_next_session(self.conn, g.id, None)
                g.next_session_minutes = None
        sid = db.open_session(self.conn, g.id, int(now), quota, block_id)
        sess = ActiveSession(sid, g.id, int(now), quota,
                             block_id=block_id or sid, carried_seconds=carried,
                             last_seen_ts=now)
        self.active[g.id] = sess
        dl = rules.session_deadline(g, now, carried, quota)
        head = "接着上一段玩" if resuming else "会话开始"
        log.info(f"{head}：{g.name}" + (f"（本段额度 {quota:g} 分钟）" if quota else "")
                 + (f"，已玩 {carried/60:.1f} 分钟" if carried else "")
                 + (f"，截止 {_fmt(dl[0])}（{dl[1]}）" if dl else ""))
        if dl:
            if resuming:
                left = max(0.0, (dl[0] - now) / 60)
                popup(f"GameLimiter — {g.name}",
                      f"接着上一段玩：本段已玩 {carried/60:.0f} 分钟，还剩 {left:.0f} 分钟，"
                      f"最晚 {_fmt(dl[0])[:5]} 结束", warn=False)
            else:
                quota_note = f"本次额度 {quota:g} 分钟；" if quota else ""
                popup(f"GameLimiter — {g.name}",
                      f"{quota_note}本次会话已开始，最晚 {_fmt(dl[0])[:5]} 结束"
                      f"（{rules.REASON_TEXT[dl[1]]}前会提前预警）", warn=False)

    def _handle_running(self, g: db.Game, sess: ActiveSession,
                        procs: list[psutil.Process], now: float):
        if not procs:
            # 结束时间取最后一次看到进程的时刻，不是现在——两者在正常轮询下只差 1 秒，
            # 但守护空窗后重新扫到"进程已没了"时能差出几小时，那段不该算游玩、也不该推后冷却
            db.close_session(self.conn, sess.session_id, int(sess.last_seen_ts), "self_exit")
            del self.active[g.id]
            played = sess.block_played / 60
            left = rules.block_remaining(g.session_minutes,
                                         {"limit_minutes": sess.limit_minutes,
                                          "played_seconds": sess.block_played})
            log.info(f"会话结束（自行退出）：{g.name}，本段已玩 {played:.1f} 分钟"
                     + (f"，剩余额度 {left/60:.1f} 分钟（{config.IDLE_GRACE_MINUTES:g} 分钟内"
                        f"再打开可接着玩）" if left else ""))
            return

        # 心跳：只累计**这一轮真实观测到**的时间，守护崩掉/机器睡眠的空窗被 cap 掉
        sess.played_seconds += min(max(0.0, now - sess.last_seen_ts), config.HEARTBEAT_MAX_GAP)
        sess.last_seen_ts = now
        db.heartbeat(self.conn, sess.session_id, sess.played_seconds, now)

        dl = rules.session_deadline(g, now, sess.block_played, sess.limit_minutes)
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

        # 一次把所有已跨过的档标记掉：额度短于最大档时（如本次只玩 5 分钟），
        # 逐档触发会连着几秒弹三次窗——游戏里被连弹是灾难
        crossed = [m for m in WARN_MINUTES if remaining <= m * 60]
        if crossed and any(m not in sess.warned for m in crossed):
            sess.warned.update(crossed)
            db.log_event(self.conn, g.id, "warn", f"{min(crossed)}min")
            log.info(f"预警：{g.name} 剩余 {remaining/60:.1f} 分钟（{reason}）")
            popup(f"GameLimiter — {g.name} 剩余 {int(remaining/60) + 1} 分钟",
                  f"{rules.REASON_TEXT[reason]}，将于 {_fmt(deadline)[:5]} 强制关闭，"
                  f"请尽快结束当前对局并自行退出")

    def _refresh_active_limits(self):
        """把 GUI/CLI 对进行中会话的额度改动（只可能是缩短）读回内存。"""
        for row in db.open_sessions(self.conn):
            sess = self.active.get(row["game_id"])
            if sess and sess.session_id == row["id"] and sess.limit_minutes != row["limit_minutes"]:
                log.info(f"会话额度更新：session {sess.session_id} → {row['limit_minutes']} 分钟")
                sess.limit_minutes = row["limit_minutes"]

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
            self._refresh_active_limits()
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
        # debug 级：计划任务每分钟拉一次自愈，这是**正常**状态，不该占 INFO 把日志刷满
        log.debug("已有守护进程实例在运行，本实例退出")
        return
    from .setup_system import grant_users_write
    grant_users_write()   # SYSTEM 身份启动时顺手修数据目录 ACL（自愈已踩坑的机器）
    Daemon().run()


if __name__ == "__main__":
    main()
