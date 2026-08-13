"""帧时间采集：每次游玩挂一个 PresentMon，游戏退出后聚合成摘要入库。

**这是旁路功能，绝不能影响防沉迷主职**——采集器起不来、跑挂了、CSV 解析失败，
一律吞掉异常记一条日志，守护该拦还是拦。所有对外入口都不抛。

为什么由守护来做（而不是用户自己开工具）：
  1. 守护精确知道游戏何时启动、进程名是什么、何时退出 → 采集器自动挂载/卸载
  2. 守护跑在 SYSTEM 权限下 → PresentMon 需要管理员或 `Performance Log Users` 组，
     否则短命/跨账号进程只显示 <unknown> 且无法按名字定位（2026-08-11 已实测
     SYSTEM 能抓到用户桌面 session 里的进程）
  3. 已有"一段游玩"时间轴 + SQLite → 帧数据挂上去就是现成的历史趋势

数据量是本模块最关键的约束：PresentMon **一帧一行**，165 fps 玩 2 小时 ≈ 119 万行
≈ 200 MB。原始 CSV 一律用完即删，只留约 10 KB 的摘要。因此解析必须**流式**，
不能把 CSV 读进内存。
"""

import csv
import json
import logging
import os
import subprocess
import sys
import threading
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger("gamelimiter")

EXE_NAME = "PresentMon.exe"
SESSION_NAME = "GameLimiter"          # ETW 会话名，避免与用户自己开的 PresentMon 打架
SETTING_KEY = "frame_capture"         # settings 表开关，'0' 关闭

# 会话结束后先等采集器自己退出多久（秒）。它带 --terminate_on_proc_exit，
# 正常情况下游戏一没就自己收尾并冲刷 CSV
STOP_GRACE_SECONDS = 15.0
# 发完 Ctrl+Break 再等多久；还不退就硬杀，尾部可能不全 → 标 truncated
BREAK_GRACE_SECONDS = 5.0
# 卡顿判据：帧时间同时超过「中位数的这么多倍」**和**「绝对下限」才算一次卡顿。
# 只用倍数会在高帧率下严重虚报——2026-08-12 实测永劫无间平均 278 fps（中位 3.76ms），
# 2 倍中位数才 7.5ms，等于把"掉到 133 fps"也算成卡顿，报出 61 次/分；而人根本感觉不到。
# 加上 16.7ms（≈ 掉到 60 fps 以下）的绝对下限后是 19 次/分，与体感对得上
HITCH_FACTOR = 2.0
HITCH_FLOOR_MS = 16.7
# 判定"帧率被限制"的帧时间平稳度阈值：p95/p50 小于它 = 节奏异常整齐
STEADY_RATIO = 1.12
# 判定 CPU/GPU 瓶颈：该部件忙碌时间占帧时间的比例超过它 = 它是瓶颈
BOUND_RATIO = 0.85

BOUND_ZH = {
    "gpu": "显卡吃满",
    "cpu": "CPU 吃满",
    "capped": "帧率被限制",       # 帧率上限 / 垂直同步 —— 显卡 CPU 都没吃满却很平稳
    "mixed": "都没吃满",
    "unknown": "看不出来",
}


# ---------------------------------------------------------------- 采集器定位

def presentmon_path() -> Optional[Path]:
    """找 PresentMon.exe：环境变量 > 打包解压目录 > 开发树 vendor/。找不到返回 None。

    `GAMELIMITER_PRESENTMON` 是**权威覆盖**：设了就只认它，指向的文件不存在就是没有，
    不回落到 vendor/。显式指定却被悄悄换成另一个二进制，比直接报没有更难查。
    """
    env = os.environ.get("GAMELIMITER_PRESENTMON")
    if env:
        p = Path(env)
        return p if p.is_file() else None
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)))
        roots.append(Path(sys.executable).parent)
    roots.append(Path(__file__).resolve().parent.parent / "vendor")
    for r in roots:
        p = r / EXE_NAME
        if p.is_file():
            return p
    return None


def capture_dir(out_dir: Optional[str] = None) -> Path:
    """原始 CSV 的落脚处。默认放应用数据目录（已配好 ACL），不放 %TEMP%。

    `out_dir` 是手动采集时用户指定的目录；不可写自动回落默认目录（见 config）。
    """
    d, reason = config.resolve_capture_dir(out_dir, "frames")
    if reason:
        log.warning("帧数据目录 %s 不可写（%s），本次回落 %s", out_dir, reason, d)
    return d


def enabled(conn) -> bool:
    """采集总开关：环境变量急停 > settings 表 > 默认开。"""
    if os.environ.get("GAMELIMITER_NO_FRAMES") == "1":
        return False
    try:
        from . import db
        v = db.get_setting(conn, SETTING_KEY)
    except Exception:
        return True
    return v != "0"


# ---------------------------------------------------------------- 采集进程

@dataclass
class Capture:
    """一次进程运行对应的一个 PresentMon 子进程。"""
    session_id: int
    game_id: int
    block_id: Optional[int]
    exe_name: str
    csv_path: Path
    proc: subprocess.Popen
    start_ts: float
    keep_raw: bool = False           # 留下原始逐帧 CSV（手动采集默认留，供自己拿去分析）
    job_id: Optional[int] = None     # 对应的采集任务；None = 自动模式下的顺带采集

    @property
    def err_path(self) -> Path:
        return self.csv_path.with_suffix(".err")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def wait(self, seconds: float) -> bool:
        """等它自己退出，返回是否已退出。"""
        deadline = time.time() + seconds
        while self.alive() and time.time() < deadline:
            time.sleep(0.2)
        return not self.alive()

    def break_gently(self) -> bool:
        """给采集器发 Ctrl+Break，让它按正常收尾流程停止录制并冲刷 CSV。

        Windows 上 `Popen.terminate()` 等于 TerminateProcess（硬杀），会丢掉还没落盘的
        尾部数据，所以**不能拿它当"优雅停止"**。控制台程序的优雅停止是控制台事件，
        因此启动时带了 CREATE_NEW_PROCESS_GROUP —— 事件只发给这个组，不会波及守护自己。
        """
        try:
            import ctypes
            CTRL_BREAK_EVENT = 1
            return bool(ctypes.windll.kernel32.GenerateConsoleCtrlEvent(
                CTRL_BREAK_EVENT, self.proc.pid))
        except Exception:
            return False


def start(conn, game, session_id: int, block_id: Optional[int],
          out_dir: Optional[str] = None, keep_raw: bool = False,
          job_id: Optional[int] = None) -> Optional[Capture]:
    """给一次游玩挂上采集器。任何失败都返回 None，不抛。"""
    if not enabled(conn):
        return None
    pm = presentmon_path()
    if pm is None:
        log.debug("帧采集跳过：没找到 %s", EXE_NAME)
        return None
    csv_path = capture_dir(out_dir) / f"s{session_id}-{int(time.time())}.csv"
    cmd = [str(pm),
           "--process_name", game.exe_name,
           "--output_file", str(csv_path),
           "--terminate_on_proc_exit",
           "--no_console_stats",
           "--stop_existing_session",
           "--session_name", SESSION_NAME,
           "--track_frame_type"]
    # stderr 落文件而不是 DEVNULL：采集失败最常见的原因是没提权
    # （`failed to start trace session: access denied`），吞掉就永远查不出为什么没数据。
    # 用文件不用管道——管道写满会把子进程卡死，而这个进程要活整场游戏
    err_path = csv_path.with_suffix(".err")
    try:
        errf = err_path.open("wb")
    except OSError:
        errf = subprocess.DEVNULL
    # CREATE_NEW_PROCESS_GROUP：收尾时要给它发 Ctrl+Break 才能优雅停止（见 break_gently），
    # 独立进程组保证这个事件打不到守护自己身上
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=errf, creationflags=flags)
    except Exception as e:
        log.warning("帧采集启动失败（%s: %s），本次不采集", e.__class__.__name__, e)
        return None
    finally:
        if errf is not subprocess.DEVNULL:
            errf.close()               # 子进程已继承句柄，父进程这边可以关
    log.info("帧采集已挂载：%s（session %d%s）", game.exe_name, session_id,
             f"，任务 {job_id}，原始数据保留" if keep_raw else "")
    return Capture(session_id=session_id, game_id=game.id, block_id=block_id,
                   exe_name=game.exe_name, csv_path=csv_path, proc=proc,
                   start_ts=time.time(), keep_raw=keep_raw, job_id=job_id)


def finalize_async(cap: Capture):
    """后台线程收尾：等采集器退出 → 聚合 → 入库 → 删原始 CSV。

    **必须异步**：聚合上百万行要几秒，守护 tick 是 1 秒一轮，卡在这里会让
    启动拦截失灵。线程里自开一条 DB 连接（sqlite 连接不跨线程）。
    """
    t = threading.Thread(target=_finalize, args=(cap,), daemon=True,
                         name=f"frames-{cap.session_id}")
    t.start()
    return t


def _finalize(cap: Capture):
    status = "ok"
    try:
        # 三段收尾，顺序不能反：游戏刚退出，采集器多半会因 --terminate_on_proc_exit
        # 自己收尾并冲刷 CSV，**先等它**；催不动才发 Ctrl+Break；最后才硬杀。
        # （早先的写法上来就 terminate()，而 Windows 上那就是硬杀，等于每次都丢尾部数据）
        if not cap.wait(STOP_GRACE_SECONDS):
            log.debug("帧采集没有随游戏退出，发 Ctrl+Break 催收尾")
            cap.break_gently()
            if not cap.wait(BREAK_GRACE_SECONDS):
                try:
                    cap.proc.kill()
                except Exception:
                    pass
                status = "truncated"
                log.warning("帧采集催不动，已强杀（尾部数据可能不全）")
        summary = summarize_csv(cap.csv_path)
    except Exception as e:
        log.warning("帧采集收尾失败（%s: %s）", e.__class__.__name__, e)
        summary, status = None, "failed"

    if summary is None:
        summary, status = {}, (status if status == "failed" else "no_frames")
    elif not summary.get("frames"):
        status = "no_frames"

    note = "" if summary.get("frames") else read_error(cap.err_path)
    if note:
        summary = dict(summary)
        summary["error"] = note

    kept = _keep_raw_file(cap) if cap.keep_raw else None
    if kept:
        summary = dict(summary)
        summary["raw_csv"] = str(kept)

    try:
        from . import db
        conn = db.connect()
        db.insert_frame_run(conn, session_id=cap.session_id, game_id=cap.game_id,
                            block_id=cap.block_id, start_ts=int(cap.start_ts),
                            end_ts=int(time.time()), summary=summary, status=status)
        conn.close()
    except Exception as e:
        log.warning("帧摘要入库失败（%s: %s）", e.__class__.__name__, e)

    # 保留原始数据时 csv 已经改名搬走，这里的 unlink 就是空转
    for p in (cap.csv_path, cap.err_path):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass

    if summary.get("frames"):
        log.info("帧采集完成：%s 帧 %d，平均 %.1f fps，1%% low %.1f，%s",
                 cap.exe_name, summary["frames"], summary["fps_avg"],
                 summary["fps_low1"], BOUND_ZH.get(summary.get("bound"), "?"))
    else:
        log.info("帧采集结束但没拿到帧（status=%s）%s", status,
                 f"：{note}" if note else "")


def read_error(path: Path) -> str:
    """读 PresentMon 的 stderr。它输出的是 **UTF-16 LE**（带 BOM），按 utf-8 读会变乱码。"""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""
    for enc in ("utf-16", "utf-8", "mbcs"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        return ""
    # 只留 error 那几行，warning 是常态噪声
    lines = [" ".join(ln.split()) for ln in text.splitlines() if ln.strip()]
    errs = [ln for ln in lines if ln.lower().startswith("error")]
    picked = errs or lines
    return " ".join(picked)[:300]


def preflight() -> tuple[bool, str]:
    """快速试一次能否开 ETW 会话。给 CLI 报"采集器可用吗"，不给守护用。"""
    pm = presentmon_path()
    if pm is None:
        return False, f"没找到 {EXE_NAME}（跑 python scripts/fetch_presentmon.py）"
    out = capture_dir() / "preflight.csv"
    err = out.with_suffix(".err")
    try:
        r = subprocess.run(
            [str(pm), "--output_file", str(out), "--timed", "1", "--terminate_after_timed",
             "--no_console_stats", "--stop_existing_session", "--session_name",
             SESSION_NAME + "Pre"],
            stdout=subprocess.DEVNULL, stderr=err.open("wb"), timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        msg = read_error(err)
        ok = r.returncode == 0
    except Exception as e:
        ok, msg = False, f"{e.__class__.__name__}: {e}"
    finally:
        for p in (out, err):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    if ok:
        return True, "可用"
    if "access denied" in msg.lower():
        return False, ("需要管理员权限（守护以 SYSTEM 跑时自动满足；"
                       "开发机上普通权限跑守护则采不到）")
    return False, msg or "未知错误"


def _keep_raw_file(cap: Capture) -> Optional[Path]:
    """把原始逐帧 CSV 留下来（手动采集用）。

    改名加 `raw-` 前缀：孤儿清理只扫 `s*.csv`，留下来的文件不能被它顺手收走。
    """
    try:
        if not cap.csv_path.exists():
            return None
        dest = cap.csv_path.with_name("raw-" + cap.csv_path.name)
        cap.csv_path.replace(dest)
        log.info("原始帧数据已保留：%s（%.1f MB）", dest, dest.stat().st_size / 1e6)
        return dest
    except OSError as e:
        log.warning("保留原始帧数据失败（%s: %s）", e.__class__.__name__, e)
        return None


def sweep_stale(max_age_hours: float = 1.0, exclude=()):
    """清掉崩溃/断电留下的孤儿 CSV（单次可达数百 MB）。

    `exclude` 是此刻正在写的文件——正常情况下它们 mtime 一直在刷新，但游戏最小化
    不渲染时可能长时间不写，所以由守护把活跃采集的路径显式传进来兜底。

    判据从"24 小时"收到"1 小时没人动过"：守护常驻不重启时这个清理只在启动时跑过一次，
    崩在采集中途留下的大文件会一直躺着（watchdog 10 秒就复活，那时文件还很新扫不掉）。
    现在守护每小时也扫一次，配合这个阈值才真正兜得住。
    """
    try:
        cutoff = time.time() - max_age_hours * 3600
        skip = {Path(p).resolve() for p in exclude}
        for f in capture_dir().glob("s*.csv"):
            try:
                if f.resolve() in skip or f.stat().st_mtime >= cutoff:
                    continue
                size_mb = f.stat().st_size / 1e6
                f.unlink()
                f.with_suffix(".err").unlink(missing_ok=True)
                log.info("清掉孤儿帧数据 %s（%.1f MB）", f.name, size_mb)
            except OSError:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------- 聚合

def _f(s: str) -> Optional[float]:
    """CSV 单元格 → float。PresentMon 用 'NA' 表示该帧没这个值（如没显示出来的帧）。"""
    if not s or s == "NA":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pct(sorted_list, p: float) -> float:
    if not sorted_list:
        return 0.0
    i = int(round((len(sorted_list) - 1) * p))
    return float(sorted_list[i])


def _series_stats(arr) -> dict:
    """一列数的分位数。逐列 sorted() 后立刻丢弃，峰值内存只占一列。"""
    if not len(arr):
        return {}
    s = sorted(arr)
    n = len(s)
    out = {"n": n,
           "p50": _pct(s, 0.50), "p95": _pct(s, 0.95),
           "p99": _pct(s, 0.99), "max": float(s[-1]),
           "avg": sum(s) / n}
    # 1% low / 0.1% low：**最慢那 1% 帧的平均帧率**（业界通用口径，
    # 与 scripts/frametest.html 一致，两边可直接比）
    for key, frac in (("low1", 0.01), ("low01", 0.001)):
        k = max(1, int(n * frac))
        worst = s[-k:]
        avg = sum(worst) / len(worst)
        out[key] = 1000.0 / avg if avg > 0 else 0.0
    return out


class _Bucket:
    """一条 (进程, 交换链) 的采样。游戏可能同时有启动器/主画面两条交换链，
    最后只取帧数最多的那条当作"真正的游戏画面"。"""

    __slots__ = ("ft", "disp", "cpu", "gpu", "gpuwait", "c2p",
                 "modes", "sync", "tear", "generated", "total", "app", "pid")

    def __init__(self, app, pid):
        self.app, self.pid = app, pid
        self.ft = array("f")        # MsBetweenPresents，主口径
        self.disp = array("f")      # MsBetweenDisplayChange，眼睛真正看到的节奏
        self.cpu = array("f")
        self.gpu = array("f")
        self.gpuwait = array("f")
        self.c2p = array("f")
        self.modes: dict = {}
        self.sync: dict = {}
        self.tear = 0
        self.generated = 0
        self.total = 0


def summarize_csv(path: Path) -> Optional[dict]:
    """流式解析 PresentMon CSV → 摘要 dict。文件不存在/没有有效帧返回 None。"""
    path = Path(path)
    if not path.is_file():
        return None
    buckets: dict = {}
    # utf-8-sig：PresentMon 写的 CSV **带 BOM**，用 utf-8 读会让首列名变成
    # '﻿Application'，整个表头对不上（2026-08-11 拿真实产物才发现，合成 CSV 测不出）
    with path.open("r", encoding="utf-8-sig", newline="", errors="replace") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return None
        header = [h.strip().lstrip("﻿") for h in header]
        col = {name: i for i, name in enumerate(header)}
        need = ("Application", "ProcessID", "MsBetweenPresents")
        if any(c not in col for c in need):
            log.warning("PresentMon CSV 缺列（表头：%s），跳过", ",".join(header[:6]))
            return None

        i_app, i_pid = col["Application"], col["ProcessID"]
        i_swap = col.get("SwapChainAddress")
        i_ft = col["MsBetweenPresents"]
        i_disp = col.get("MsBetweenDisplayChange")
        i_cpu = col.get("MsCPUBusy")
        i_gpu = col.get("MsGPUBusy")
        i_gw = col.get("MsGPUWait")
        i_c2p = col.get("MsClickToPhotonLatency")
        i_mode = col.get("PresentMode")
        i_sync = col.get("SyncInterval")
        i_tear = col.get("AllowsTearing")
        i_type = col.get("FrameType")
        ncol = len(header)

        for row in reader:
            if len(row) < ncol:
                continue
            ft = _f(row[i_ft])
            if ft is None or ft <= 0:
                continue
            key = (row[i_pid], row[i_swap] if i_swap is not None else "")
            b = buckets.get(key)
            if b is None:
                b = buckets[key] = _Bucket(row[i_app], row[i_pid])
            b.total += 1
            b.ft.append(ft)
            for idx, arr in ((i_disp, b.disp), (i_cpu, b.cpu),
                             (i_gpu, b.gpu), (i_gw, b.gpuwait), (i_c2p, b.c2p)):
                if idx is not None:
                    v = _f(row[idx])
                    if v is not None and v >= 0:
                        arr.append(v)
            if i_mode is not None:
                b.modes[row[i_mode]] = b.modes.get(row[i_mode], 0) + 1
            if i_sync is not None:
                b.sync[row[i_sync]] = b.sync.get(row[i_sync], 0) + 1
            if i_tear is not None and row[i_tear] == "1":
                b.tear += 1
            if i_type is not None and row[i_type] not in ("Application", "", "NA"):
                b.generated += 1

    if not buckets:
        return None
    b = max(buckets.values(), key=lambda x: x.total)
    return _summarize_bucket(b, extra_swapchains=len(buckets) - 1)


def _summarize_bucket(b: _Bucket, extra_swapchains: int = 0) -> dict:
    ft = _series_stats(b.ft)
    if not ft:
        return {"frames": 0}
    seconds = sum(b.ft) / 1000.0
    p50 = ft["p50"]
    hitch_ms = max(p50 * HITCH_FACTOR, HITCH_FLOOR_MS)
    hitches = sum(1 for v in b.ft if v > hitch_ms)

    cpu = _series_stats(b.cpu)
    gpu = _series_stats(b.gpu)
    gw = _series_stats(b.gpuwait)
    disp = _series_stats(b.disp)
    c2p = _series_stats(b.c2p)

    out = {
        "frames": b.total,
        "seconds": round(seconds, 1),
        "fps_avg": round(1000.0 / ft["avg"], 1) if ft["avg"] > 0 else 0.0,
        "fps_low1": round(ft["low1"], 1),
        "fps_low01": round(ft["low01"], 1),
        "ft_p50": round(p50, 2),
        "ft_p95": round(ft["p95"], 2),
        "ft_p99": round(ft["p99"], 2),
        "ft_max": round(ft["max"], 1),
        "hitches": hitches,
        "hitches_per_min": round(hitches / (seconds / 60.0), 2) if seconds >= 1 else 0.0,
        "hitch_ms": round(hitch_ms, 1),
        # 最慢的几帧原样留着：「最狠一次冻了 0.3 秒」比任何分位数都更能说明问题。
        # **带上发生时刻**——原始逐帧 CSV 聚合完就删了，只有这个时刻能拿去和硬件
        # 采样（`hardware.py` 保留的逐秒 CSV）对齐，看那一刻机器在干什么
        "worst_frames": [w[0] for w in _worst_with_time(b.ft)],
        "worst_at": [w[1] for w in _worst_with_time(b.ft)],
        "present_mode": max(b.modes, key=b.modes.get) if b.modes else None,
        "sync_interval": max(b.sync, key=b.sync.get) if b.sync else None,
        "tearing_pct": round(100.0 * b.tear / b.total, 1),
        "generated_pct": round(100.0 * b.generated / b.total, 1),
        "cpu_busy_p50": round(cpu.get("p50", 0.0), 2),
        "gpu_busy_p50": round(gpu.get("p50", 0.0), 2),
        "gpu_wait_p50": round(gw.get("p50", 0.0), 2),
        "click_to_photon_p50": round(c2p["p50"], 1) if c2p else None,
        "extra_swapchains": extra_swapchains,
    }
    out["bound"] = _classify_bound(out)
    # 撕裂开着时画面根本不按刷新周期量化（实测永劫无间显示间隔中位数 3.70ms，
    # 而 165Hz 屏幕的周期是 6.06ms —— 帧是撕裂着扫出去的），这时谈"节奏齐不齐"没有意义
    out["judder_pct"] = (None if out["tearing_pct"] >= 50
                         else _judder_pct(b.disp, disp.get("p50", 0.0)))
    out["per_minute"] = _per_minute(b.ft)
    return out


def _worst_with_time(ft, k: int = 5) -> list:
    """最慢的 k 帧 [(帧时间ms, 发生在第几秒)]，按帧时间降序。单遍 O(n log k)。

    时刻是关键：逐帧 CSV 聚合完就删了，只有它能拿去跟硬件逐秒采样对齐。
    """
    import heapq
    heap: list = []
    t = 0.0
    for v in ft:
        t += v / 1000.0
        if len(heap) < k:
            heapq.heappush(heap, (v, round(t, 1)))
        elif v > heap[0][0]:
            heapq.heapreplace(heap, (v, round(t, 1)))
    return [[round(v, 1), tt] for v, tt in sorted(heap, key=lambda x: -x[0])]


def _classify_bound(s: dict) -> str:
    """瓶颈定性。

    显卡/CPU 忙碌时间接近整个帧时间 → 它是瓶颈；两者都远没吃满、帧时间却异常平稳
    → 帧率被人为限制（帧率上限或垂直同步）。最后这条正是 2026-08-11 帕鲁那个
    `FrameRateLimit=90` 撞 165 Hz 屏幕的情形，人工排查了一整晚才发现。
    """
    ft = s.get("ft_p50") or 0.0
    if ft <= 0:
        return "unknown"
    r_gpu = (s.get("gpu_busy_p50") or 0.0) / ft
    r_cpu = (s.get("cpu_busy_p50") or 0.0) / ft
    if r_gpu >= BOUND_RATIO:
        return "gpu"
    if r_cpu >= BOUND_RATIO:
        return "cpu"
    steady = (s.get("ft_p95") or ft) / ft
    if steady <= STEADY_RATIO and max(r_gpu, r_cpu) < 0.8:
        return "capped"
    return "mixed"


def _judder_pct(disp, p50: float) -> float:
    """节奏不齐的比例：显示间隔偏离中位数超过 25% 的帧占比。

    帧率不是刷新率的整数分频时（如 90 fps 送进 165 Hz 屏），每帧要么占 1 个刷新
    周期（6.06ms）要么占 2 个（12.12ms），两种值不规则交替 → 这个比例显著抬高。
    锁定在整数分频上（165/82.5/55）时所有间隔一样，比例接近 0。
    **平均帧数好看、眼睛却觉得一顿一顿，看的就是这个数。**
    """
    if not len(disp) or p50 <= 0:
        return 0.0
    lo, hi = p50 * 0.75, p50 * 1.25
    n = sum(1 for v in disp if v < lo or v > hi)
    return round(100.0 * n / len(disp), 1)


def _per_minute(ft, max_points: int = 240) -> list:
    """每分钟一个点 [分钟, 平均 fps, 1% low]，用来看"越玩越卡"这类趋势。"""
    out, bucket, elapsed = [], [], 0.0
    minute = 0
    for v in ft:
        bucket.append(v)
        elapsed += v / 1000.0
        if elapsed >= 60.0:
            out.append(_minute_point(minute, bucket))
            bucket, elapsed = [], 0.0
            minute += 1
            if len(out) >= max_points:
                return out
    if len(bucket) >= 30:                 # 尾巴太短就不单独成点
        out.append(_minute_point(minute, bucket))
    return out


def _minute_point(minute: int, vals: list) -> list:
    s = sorted(vals)
    avg = sum(s) / len(s)
    k = max(1, int(len(s) * 0.01))
    low = sum(s[-k:]) / k
    return [minute, round(1000.0 / avg, 1) if avg > 0 else 0.0,
            round(1000.0 / low, 1) if low > 0 else 0.0]


# ---------------------------------------------------------------- 人话输出

def describe(s: dict) -> list[str]:
    """摘要 → 给人看的几行。CLI 与（第二版的）GUI 共用。"""
    if not s or not s.get("frames"):
        return ["（没采到帧数据）"]
    lines = [f"平均 {s['fps_avg']:g} fps · 1% low {s['fps_low1']:g} · "
             f"0.1% low {s['fps_low01']:g}",
             f"帧时间 中位 {s['ft_p50']:g}ms / p99 {s['ft_p99']:g}ms · "
             f"卡顿 {s['hitches_per_min']:g} 次/分"
             + (f"（判据 >{s['hitch_ms']:g}ms）" if s.get("hitch_ms") else "")]
    worst = s.get("worst_frames")
    if worst:
        at = s.get("worst_at") or []
        pairs = "、".join(f"{v:g}" + (f"(第{at[i]:g}秒)" if i < len(at) else "")
                          for i, v in enumerate(worst))
        lines.append(f"最慢的几帧 {pairs} ms"
                     + (f" —— 最狠一次冻了 {worst[0]/1000:.2f} 秒" if worst[0] >= 100 else ""))

    env = []
    mode = s.get("present_mode")
    if mode:
        env.append(f"画面 {_mode_zh(mode)}")
    sync = s.get("sync_interval")
    if sync is not None:
        env.append("垂直同步 " + ("关" if str(sync) == "0" else f"开（{sync}）"))
    if s.get("tearing_pct", 0) >= 50:
        env.append("允许撕裂")
    if s.get("generated_pct"):
        env.append(f"生成帧 {s['generated_pct']:g}%")
    if env:
        lines.append(" · ".join(env))

    diag = [f"瓶颈 {BOUND_ZH.get(s.get('bound'), '?')}"]
    if (s.get("judder_pct") or 0) >= 10:
        diag.append(f"节奏不齐 {s['judder_pct']:g}%（帧率可能不是刷新率的整数分频）")
    if s.get("click_to_photon_p50"):
        diag.append(f"点击到画面 {s['click_to_photon_p50']:g}ms")
    lines.append(" · ".join(diag))
    return lines


_MODE_ZH = {
    "Hardware: Independent Flip": "独占翻转（最优）",
    "Hardware: Legacy Flip": "独占全屏",
    "Hardware Composed: Independent Flip": "硬件合成翻转",
    "Composed: Flip": "窗口合成（多一层 DWM）",
    "Composed: Copy with GPU GDI": "窗口拷贝（GPU GDI）",
    "Composed: Copy with CPU GDI": "窗口拷贝（CPU GDI，最差）",
    "Hardware: Legacy Copy to front buffer": "独占拷贝",
}


def _mode_zh(mode: str) -> str:
    return f"{_MODE_ZH.get(mode, mode)}" if mode else "?"


def load_summary(row) -> dict:
    """frame_runs 行 → 摘要 dict（JSON 解析失败给空）。"""
    try:
        return json.loads(row["summary"]) if row["summary"] else {}
    except (ValueError, TypeError):
        return {}
