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

from . import changes, config, db, frames, hardware, procmatch, rules
from .config import LOG_PATH, POLL_INTERVAL, RULE_RELOAD_INTERVAL, WARN_MINUTES
from .notifier import popup
from .winutil import DAEMON_MUTEX, WATCHDOG_MUTEX, hold_mutex, mutex_exists, spawn_detached

log = logging.getLogger("gamelimiter")

SWEEP_INTERVAL = 3600.0     # 落盘数据清理间隔（秒）；启动时先扫一次，之后每小时一次


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
    last_deadline: Optional[float] = None      # 上一轮算出的截止时刻（用于识别 deadline 后移）

    @property
    def block_played(self) -> float:
        """本段累计真实游玩秒数（含之前几次）。"""
        return self.carried_seconds + self.played_seconds


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


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
        self.daily_minutes: Optional[float] = None   # 全局规则 e 的上限，随 reload_games 刷新
        self.daily_used: float = 0.0                 # 今天已玩总秒数，每轮 tick 刷新
        self.active: dict[int, ActiveSession] = {}   # game_id -> ActiveSession
        self.captures: dict[int, frames.Capture] = {}  # game_id -> 帧采集子进程
        self.hw: dict[int, hardware.Sampler] = {}      # game_id -> 硬件采样器
        self.jobs: dict[int, dict] = {}   # game_id -> {job_id, deadline}（手动采集任务）
        self.matcher = procmatch.Matcher()   # 名字/路径/指纹三道识别，缓存跨轮复用
        self.aliased: dict[int, str] = {}     # game_id -> 已告警过的改名，避免同一次反复弹
        self.wake = threading.Event()
        self._last_reload = 0.0
        self._last_wd_spawn = 0.0
        self._last_sweep = 0.0

    # ---- 性能数据采集（旁路，异常一律不外溢） ----

    def _start_capture(self, g: db.Game, sess: ActiveSession,
                       procs: Optional[list] = None):
        """会话开始时调用。

        手动模式（v0.16.0 起的默认）**只在用户下过单时才采**——没下单就一个采集器都不起，
        不占 CPU、不写盘。自动模式保持老行为：开游戏就采。
        """
        try:
            job = db.armed_capture_job(self.conn, g.id, time.time())
            if job is None and db.get_capture_mode(self.conn) != "auto":
                return
        except Exception:
            log.exception("读采集任务异常（不影响限制）")
            return
        self._begin_capture(g, sess, procs, job)

    def _begin_capture(self, g: db.Game, sess: ActiveSession,
                       procs: Optional[list], job: Optional[dict]):
        out_dir = job["out_dir"] if job else None
        keep_raw = bool(job["keep_raw"]) if job else False
        job_id = job["id"] if job else None
        started = False
        try:
            cap = frames.start(self.conn, g, sess.session_id, sess.block_id,
                               out_dir=out_dir, keep_raw=keep_raw, job_id=job_id)
            if cap:
                self.captures[g.id] = cap
                started = True
        except Exception:
            log.exception("帧采集启动异常（不影响限制）")
        try:
            pid = procs[0].pid if procs else None
            hw = hardware.start(self.conn, g, sess.session_id, sess.block_id, pid,
                                out_dir=out_dir, job_id=job_id)
            if hw:
                self.hw[g.id] = hw
                started = True
        except Exception:
            log.exception("硬件采集启动异常（不影响限制）")
        if job is None:
            return
        if not started:
            db.finish_capture_job(self.conn, job_id, "done", "两个采集器都没能启动")
            log.warning("采集任务 %d：采集器没起来（帧采集多半是权限不够）", job_id)
            return
        now = time.time()
        dur = job["duration_minutes"]
        db.start_capture_job(self.conn, job_id, sess.session_id, int(now))
        self.jobs[g.id] = {"job_id": job_id, "deadline": now + dur * 60 if dur else None}
        log.info("采集任务 %d 开始：%s，%s，数据落 %s", job_id, g.name,
                 f"{dur:g} 分钟" if dur else "采到游戏退出",
                 out_dir or "默认目录")

    def _stop_capture(self, game_id: int, note: str = ""):
        """停采集。收尾一律走后台线程，绝不阻塞 tick
        （帧数据聚合上百万行要几秒，卡在这里会让启动拦截失灵）。

        **只停采集，不动会话**——采集时长到点时游戏照玩，限制规则那边毫不知情。
        """
        cap = self.captures.pop(game_id, None)
        if cap is not None:
            try:
                frames.finalize_async(cap)
            except Exception:
                log.exception("帧采集收尾异常（不影响限制）")
        hw = self.hw.pop(game_id, None)
        if hw is not None:
            try:
                hardware.finalize_async(hw)
            except Exception:
                log.exception("硬件采集收尾异常（不影响限制）")
        info = self.jobs.pop(game_id, None)
        if info is not None:
            try:
                db.finish_capture_job(self.conn, info["job_id"], "done", note or "已结束")
                log.info("采集任务 %d 结束（%s）", info["job_id"], note or "已结束")
            except Exception:
                log.exception("采集任务收尾异常（不影响限制）")

    def _reap_dead_frame_captures(self):
        """帧采集器自己先死了（权限不够 / 被杀 / 崩了）→ 只收掉它。

        **不能顺手把硬件采集也停掉**：两个采集器互相独立，帧采集要管理员权限而硬件
        采集不要，普通权限下前者必然起不来。早先这里一并调 `_stop_capture`，结果是
        帧采集一失败，硬件数据也跟着断在第一秒（2026-08-13 e2e 抓到）。
        """
        for gid in [gid for gid, c in self.captures.items()
                    if not c.alive() and gid in self.active]:
            cap = self.captures.pop(gid)
            log.warning("帧采集进程已提前退出（game_id=%d），本次不再采帧；"
                        "硬件采集与采集任务继续", gid)
            try:
                frames.finalize_async(cap)
            except Exception:
                log.exception("帧采集收尾异常（不影响限制）")

    def _tick_capture_jobs(self, now: float, matches: dict):
        """采集任务的状态机。三件事：到点/被取消的停掉、待命超时的作废、
        游戏已经在跑时才下的单立刻挂上。"""
        for gid, info in list(self.jobs.items()):
            dl = info["deadline"]
            if dl and now >= dl:
                self._stop_capture(gid, "采集时长到点")
                continue
            row = db.capture_job(self.conn, info["job_id"])
            if row is not None and row["state"] == "cancelled":
                self._stop_capture(gid, row["note"] or "手动停止")
        db.expire_capture_jobs(self.conn, now)
        for g in self.games:
            sess = self.active.get(g.id)
            if sess is None or g.id in self.jobs:
                continue
            job = db.armed_capture_job(self.conn, g.id, now)
            if job is not None:
                if g.id in self.captures or g.id in self.hw:
                    # 自动模式已经在采了：让任务接管它（时长/停止按钮从此生效）
                    db.start_capture_job(self.conn, job["id"], sess.session_id, int(now))
                    dur = job["duration_minutes"]
                    self.jobs[g.id] = {"job_id": job["id"],
                                       "deadline": now + dur * 60 if dur else None}
                    log.info("采集任务 %d 接管进行中的采集：%s", job["id"], g.name)
                else:
                    m = matches.get(g.id)
                    self._begin_capture(g, sess, m.procs if m else None, job)

    # ---- 规则/名单 ----

    def reload_games(self):
        self.games = db.list_games(self.conn, enabled_only=True)
        self.daily_limit = db.get_daily_game_limit(self.conn)
        # 今天适用的那一档（周末可能与平日不同）；每 5 秒重载一次，跨零点自动切档
        self.daily_minutes = db.effective_daily_minutes(self.conn)
        try:
            n = procmatch.backfill(self.conn, self.games, db.set_exe_fingerprint)
            if n:
                log.info("补齐 %d 款游戏的 exe 指纹（改名/搬移后仍可识别）", n)
        except Exception:
            log.exception("补 exe 指纹异常（不影响限制）")
        self._last_reload = time.time()

    def daily_remaining(self, now: float) -> Optional[float]:
        """今日总时长还剩多少秒；没设规则 e 返回 None。用每轮 tick 开头刷新的快照，
        比 DB 落后至多一次心跳（1 秒），对分钟级的额度无影响。"""
        if not self.daily_minutes:
            return None
        return max(0.0, self.daily_minutes * 60 - self.daily_used)

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
        matches = self.matcher.scan(all_games.values())
        for row in db.open_sessions(self.conn):
            g = all_games.get(row["game_id"])
            m = matches.get(g.id) if g else None
            alive = m.procs if m else None
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
                # 守护重启前的帧数据随旧进程一起没了，从现在重新起采
                self._start_capture(g, self.active[g.id], alive)
            else:
                db.close_session(self.conn, row["id"],
                                 int(row["last_seen_ts"] or row["start_ts"]), "daemon_restart")

    # ---- 每轮处理 ----

    def _alias_alert(self, g: db.Game, m: procmatch.Match):
        """exe 被改名 / 复制到别处运行 → 记一笔并告知。规则照旧生效，不因改名放行。

        只在这一次识别结果变化时报，避免同一个副本每次开都弹。
        """
        if m.kind == "name" or not m.alias:
            self.aliased.pop(g.id, None)
            return
        if self.aliased.get(g.id) == m.alias:
            return
        self.aliased[g.id] = m.alias
        how = "改名后" if m.kind == "path" else "复制到别处并改名后"
        db.log_event(self.conn, g.id, "renamed", f"{m.kind}: {m.alias}")
        log.warning("%s 被%s运行：%s —— 仍按原规则处理", g.name, how, m.alias)
        popup(f"GameLimiter — 认出了改名的 {g.name}",
              f"检测到 {g.name} 被{how}运行（{m.alias}），规则照常生效。", warn=True)

    def _handle_start_attempt(self, g: db.Game, m: procmatch.Match, now: float):
        procs = m.procs
        self._alias_alert(g, m)
        # 观察模式：只开会话挂采集，一条规则都不查（PVP 游戏被强杀会判逃跑，
        # 这条路径上必须连"算一下会不会被拦"都不做）
        if rules.is_observed(g):
            sid = db.open_session(self.conn, g.id, int(now))
            sess = ActiveSession(sid, g.id, int(now), None, block_id=sid, last_seen_ts=now)
            self.active[g.id] = sess
            self._start_capture(g, sess, procs)
            log.info(f"开始观察：{g.name}（观察模式，不施加任何限制）")
            return

        # 上一段游玩还活着？活着 = 接着玩（额度接着用、跳过冷却），不是开新的一场
        block = db.current_block(self.conn, g.id)
        resuming = rules.block_alive(block, g.session_minutes, now, config.IDLE_GRACE_MINUTES)

        # 两条全局规则先判：它们挡的是"今天整体玩太多了"，比冷却/时段更能说明问题。
        # 总时长对续玩同样生效——额度用完了，换回今天玩过的那款接着玩正是要拦的事
        today = db.games_played_between(self.conn, *rules.day_bounds(now))
        verdict = rules.check_daily_limit(self.daily_limit, today, g.id, now)
        if verdict.allowed:
            verdict = rules.check_daily_minutes(self.daily_minutes, self.daily_used, now)
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
        self._start_capture(g, sess, procs)
        dl = rules.session_deadline(g, now, carried, quota,
                                    daily_remaining=self.daily_remaining(now))
        sess.last_deadline = dl[0] if dl else None
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
            self._stop_capture(g.id, "游戏已退出")
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

        dl = rules.session_deadline(g, now, sess.block_played, sess.limit_minutes,
                                    daily_remaining=self.daily_remaining(now))
        if dl is None:
            return
        deadline, reason = dl
        remaining = deadline - now

        # deadline 明显后移（跨零点总额重置、时段规则放宽落地）→ 预警档重新开始计。
        # 不清的话，重置前擦过的档位会被永久标记，真到点时一声不吭就把游戏杀了
        if sess.last_deadline is not None and deadline > sess.last_deadline + 60:
            sess.warned.clear()
        sess.last_deadline = deadline

        if remaining <= 0:
            n = _kill(procs)
            db.close_session(self.conn, sess.session_id, int(now), reason)
            db.log_event(self.conn, g.id, "killed", reason)
            del self.active[g.id]
            self._stop_capture(g.id, "游戏到点被关闭")
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

    def _on_change_applied(self, field: str, game_id: int):
        """待生效变更落地时的回调。拆强制层那条要守护自己收尾——计划任务已经删了，
        但守护和 watchdog 还在跑，不停掉的话"拆除"只拆了一半（重启才失效）。
        """
        if field != changes.REMOVE_SYSTEM:
            return
        log.warning("拆除强制层的 24 小时冷静期到期：计划任务已删除，守护与 watchdog 一并退出")
        db.log_event(self.conn, None, "system_removed", "冷静期到期，强制层已拆除")
        popup("GameLimiter — 强制层已拆除",
              "SYSTEM 自启与每分钟自愈任务已删除，守护进程即将退出。"
              "想重新启用：打开面板点「初始化本机」。")
        from .app import _stop_daemon
        _stop_daemon()          # 先杀 watchdog，否则自己一退它 5 秒内就把守护拉回来
        raise SystemExit(0)

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
            n = changes.apply_due(self.conn, now, on_applied=self._on_change_applied)
            if n:
                log.info(f"应用 {n} 条到期的放宽变更")
            self.reload_games()
            self._refresh_active_limits()
            self._ensure_watchdog(now)
        # 今日已玩总时长每轮重算：跨零点自动归零，多款游戏同时在跑也共用这一份
        self.daily_used = db.daily_used_seconds(self.conn, now) if self.daily_minutes else 0.0
        matches = self.matcher.scan(self.games)
        enabled_ids = set()
        for g in self.games:
            enabled_ids.add(g.id)
            m = matches.get(g.id)
            plist = [p for p in (m.procs if m else []) if p.is_running()]
            sess = self.active.get(g.id)
            if sess is None and plist:
                self._handle_start_attempt(g, procmatch.Match(plist, m.kind, m.alias), now)
            elif sess is not None:
                self._handle_running(g, sess, plist, now)
        # 游戏被禁用/删除但会话还挂着 → 关闭会话，停止跟踪
        for gid in [gid for gid in self.active if gid not in enabled_ids]:
            db.close_session(self.conn, self.active[gid].session_id, int(now), "disabled")
            del self.active[gid]
            self._stop_capture(gid, "游戏已停用")
        self._reap_dead_frame_captures()
        try:
            self._tick_capture_jobs(now, matches)
        except Exception:
            log.exception("采集任务处理异常（不影响限制）")
        self._sweep(now)

    def _sweep(self, now: float):
        """周期清理落盘数据。**不能只在启动时扫**：守护常驻不重启时，崩在采集中途
        留下的几百 MB 孤儿文件会一直躺着（watchdog 10 秒就复活，那时文件还太新）。"""
        if now - self._last_sweep < SWEEP_INTERVAL:
            return
        self._last_sweep = now
        frames.sweep_stale(exclude=[c.csv_path for c in self.captures.values()])
        hardware.sweep_old()

    def run(self):
        db.log_event(self.conn, None, "daemon_start")
        log.info(f"守护进程启动，DB: {self.conn.execute('PRAGMA database_list').fetchone()[2]}")
        self.reload_games()
        self._last_sweep = time.time()
        frames.sweep_stale()        # 清掉崩溃/断电留下的孤儿 CSV（单次可达数百 MB）
        hardware.sweep_old()        # 硬件采样只留最近 N 次（每次几百 KB，留着供事后分析）
        n = db.abandon_running_capture_jobs(self.conn)
        if n:
            log.warning("%d 个采集任务随上次守护退出中断，已标记结束", n)
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
