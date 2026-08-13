"""游玩期间的硬件采集：1 Hz 记录 CPU / 内存 / 磁盘 / GPU / 游戏进程 / 干扰进程。

与 `frames.py` 的分工：帧数据回答「卡不卡」，硬件数据回答「当时机器在什么状态」。
两边共用同一个 session_id，事后可以按相对秒对齐（2026-08-12 手工排查时正是这么干的）。

**保留策略与 frames 相反**：帧数据一帧一行，一小时 270 MB，必须用完即删；
硬件采样 1 Hz，两小时才约 700 KB —— **原样留着**，这样"帮我看看昨天那局"
可以直接把 CSV 捞出来分析，不用重新复现。

**这是旁路功能，绝不影响防沉迷主职**：任何异常吞掉记日志。采样跑在独立线程里，
不占守护的 tick。

已知缺口（v1 不做）：Windows 上 psutil 拿不到 CPU 睿频状态与温度
（`cpu_freq()` 只返回标称值、`sensors_temperatures()` 为空），
要判 CPU 降频得走性能计数器，留给 v2。GPU 侧的降频原因 nvidia-smi 有，已采。
"""

import csv
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import psutil

from . import config

log = logging.getLogger("gamelimiter")

SETTING_KEY = "hw_capture"          # settings 表开关，'0' 关闭
SAMPLE_INTERVAL = 1.0               # 秒
KEEP_SESSIONS = 30                  # 保留最近多少次的原始 CSV
GPU_QUERY = ("utilization.gpu,utilization.memory,memory.used,temperature.gpu,"
             "power.draw,clocks.current.graphics,clocks_throttle_reasons.active")

# 每秒采这些。**列的选择是被开销逼出来的**（2026-08-12 实测，428 个进程的机器）：
#   · psutil 全表取任何 CPU/内存字段 = 3.3 秒（`process_iter` 对这些字段没有批量优化），
#     所以"谁在抢资源"不做常驻扫描 —— 改用零成本的 `other_cpu` 探测有没有干扰，
#     真有再单独跑一次进程排查
#   · 单进程的 `num_threads()` 一个调用就要 16 ms（Windows 上要枚举全系统线程表），
#     其余 cpu_percent/memory_info/io_counters/num_handles 全是 0.0 ms → 只砍它
# 现在一次采样约 0.8 ms。
COLUMNS = ["t", "cpu_total", "cpu_max_core", "cpu_max_id", "other_cpu",
           "mem_avail_mb", "mem_pct", "disk_read_mbs", "disk_write_mbs",
           "game_cpu", "game_ws_mb", "game_read_mbs", "game_faults_s", "game_handles",
           "gpu_util", "gpu_mem_mb", "gpu_temp", "gpu_power", "gpu_clock", "gpu_throttle"]


def enabled(conn) -> bool:
    if os.environ.get("GAMELIMITER_NO_HW") == "1":
        return False
    try:
        from . import db
        return db.get_setting(conn, SETTING_KEY) != "0"
    except Exception:
        return True


def capture_dir(out_dir: Optional[str] = None) -> Path:
    """逐秒 CSV 的落脚处；`out_dir` 是手动采集时用户指定的目录，不可写自动回落。"""
    d, reason = config.resolve_capture_dir(out_dir, "hw")
    if reason:
        log.warning("硬件数据目录 %s 不可写（%s），本次回落 %s", out_dir, reason, d)
    return d


# ---------------------------------------------------------------- GPU 遥测

class _GpuReader:
    """常驻一个 `nvidia-smi -lms` 进程流式读遥测。

    **不能每秒起一次 nvidia-smi**——每秒一次进程创建正好是给游戏添堵的那种开销。
    这里一个进程读到底，一个小线程把最新一行存下来，采样线程直接取。
    """

    def __init__(self, interval_ms: int = 1000):
        self.latest: Optional[dict] = None
        self.proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._interval_ms = interval_ms

    def start(self) -> bool:
        try:
            self.proc = subprocess.Popen(
                ["nvidia-smi", f"--query-gpu={GPU_QUERY}",
                 "--format=csv,noheader,nounits", f"-lms={self._interval_ms}"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            log.debug("GPU 遥测不可用（%s），本次只采 CPU/内存/磁盘", e.__class__.__name__)
            return False
        self._thread = threading.Thread(target=self._pump, daemon=True, name="hw-gpu")
        self._thread.start()
        return True

    def _pump(self):
        try:
            for line in self.proc.stdout:
                if self._stop.is_set():
                    break
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 7:
                    continue
                def num(x):
                    try:
                        return float(x)
                    except ValueError:
                        return None
                self.latest = {
                    "gpu_util": num(parts[0]), "gpu_memutil": num(parts[1]),
                    "gpu_mem_mb": num(parts[2]), "gpu_temp": num(parts[3]),
                    "gpu_power": num(parts[4]), "gpu_clock": num(parts[5]),
                    "gpu_throttle": parts[6],
                }
        except Exception:
            pass

    def stop(self):
        self._stop.set()
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()      # 遥测进程无缓冲要冲刷，硬杀无妨
        except Exception:
            pass


# ---------------------------------------------------------------- 采样器

class Sampler:
    def __init__(self, session_id: int, game_id: int, block_id: Optional[int],
                 exe_name: str, pid: Optional[int], csv_path: Path):
        self.session_id = session_id
        self.game_id = game_id
        self.block_id = block_id
        self.exe_name = exe_name
        self.pid = pid
        self.csv_path = csv_path
        self.start_ts = time.time()
        self.samples = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._gpu = _GpuReader()
        self._ncpu = psutil.cpu_count() or 1
        self._proc: Optional[psutil.Process] = None   # 缓存的游戏进程对象，见 _game_proc

    # -- 生命周期 --

    def start(self):
        self._gpu.start()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"hw-{self.session_id}")
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._gpu.stop()

    # -- 采样循环 --

    def _game_proc(self) -> Optional[psutil.Process]:
        """跟住游戏进程；它换了 pid（重开）就按 exe 名重新找。

        **必须缓存这个 Process 对象**：`cpu_percent()` 算的是"距上次在**同一个对象**上
        调用"的增量，每次新建对象则永远返回 0.0。2026-08-13 第一次真机采集就栽在这——
        723 个采样点的 game_cpu 全是 0。
        """
        p = self._proc
        if p is not None:
            try:
                if p.is_running():
                    return p
            except psutil.Error:
                pass
            self._proc = None
        try:
            if self.pid:
                p = psutil.Process(self.pid)
                if p.is_running():
                    self._proc = p
                    p.cpu_percent()          # 建基线，下一轮才有意义的读数
                    return p
        except psutil.Error:
            pass
        want = self.exe_name.lower()
        for cand in psutil.process_iter(["name"]):
            try:
                if (cand.info["name"] or "").lower() == want:
                    self.pid = cand.pid
                    self._proc = cand
                    cand.cpu_percent()
                    return cand
            except psutil.Error:
                continue
        return None

    def _run(self):
        try:
            fh = self.csv_path.open("w", encoding="utf-8", newline="")
        except OSError as e:
            log.warning("硬件采集无法写文件（%s），本次不采", e)
            return
        try:
            w = csv.writer(fh)
            w.writerow(COLUMNS)
            psutil.cpu_percent(percpu=True)          # 建基线
            last_disk = psutil.disk_io_counters()
            last_io, last_faults = None, None
            last_t = time.time()
            while not self._stop.is_set():
                if self._stop.wait(SAMPLE_INTERVAL):
                    break
                try:
                    now = time.time()
                    dt = max(1e-3, now - last_t)
                    last_t = now
                    cores = psutil.cpu_percent(percpu=True)
                    mx = max(cores) if cores else 0.0
                    mem = psutil.virtual_memory()
                    disk = psutil.disk_io_counters()
                    dr = (disk.read_bytes - last_disk.read_bytes) / dt / 1e6
                    dw = (disk.write_bytes - last_disk.write_bytes) / dt / 1e6
                    last_disk = disk

                    gp = self._game_proc()
                    g_cpu = g_ws = g_read = g_faults = g_hnd = None
                    if gp is not None:
                        try:
                            g_cpu = gp.cpu_percent()
                            mi = gp.memory_info()
                            g_ws = mi.rss / 1e6
                            faults = getattr(mi, "num_page_faults", None)
                            if faults is not None and last_faults is not None:
                                g_faults = max(0, faults - last_faults) / dt
                            last_faults = faults
                            io = gp.io_counters()
                            if last_io is not None:
                                g_read = max(0, io.read_bytes - last_io) / dt / 1e6
                            last_io = io.read_bytes
                            g_hnd = gp.num_handles()
                        except psutil.Error:
                            pass
                    g = self._gpu.latest or {}

                    # 全机 CPU 减掉游戏自己那份 = 别的东西吃掉多少。
                    # 进程的 cpu_percent 是"相对一个核"，要除以核数才能和总占用同尺度
                    cpu_total = sum(cores) / len(cores) if cores else None
                    other = None
                    if cpu_total is not None:
                        other = max(0.0, cpu_total - (g_cpu or 0.0) / self._ncpu)

                    w.writerow([
                        round(now - self.start_ts, 1),
                        _r(cpu_total, 1), round(mx, 1), cores.index(mx) if cores else "",
                        _r(other, 1),
                        int(mem.available / 1e6), mem.percent,
                        round(dr, 2), round(dw, 2),
                        _r(g_cpu, 1), _r(g_ws, 0), _r(g_read, 2), _r(g_faults, 0),
                        g_hnd if g_hnd is not None else "",
                        _r(g.get("gpu_util"), 0), _r(g.get("gpu_mem_mb"), 0),
                        _r(g.get("gpu_temp"), 0), _r(g.get("gpu_power"), 1),
                        _r(g.get("gpu_clock"), 0), g.get("gpu_throttle", ""),
                    ])
                    self.samples += 1
                    if self.samples % 30 == 0:
                        fh.flush()
                except Exception:
                    continue          # 单次采样失败不该终止整场采集
        finally:
            try:
                fh.close()
            except Exception:
                pass


def _r(v, nd):
    return "" if v is None else (round(v, nd) if nd else int(v))


# ---------------------------------------------------------------- 对外入口

def start(conn, game, session_id: int, block_id: Optional[int],
          pid: Optional[int] = None, out_dir: Optional[str] = None,
          job_id: Optional[int] = None) -> Optional[Sampler]:
    if not enabled(conn):
        return None
    try:
        # 落到用户指定目录时天然不参与"只留最近 30 次"的轮转——sweep_old 只扫默认目录，
        # 用户自己指定的目录里的数据归用户管，我们不去删
        path = capture_dir(out_dir) / f"s{session_id}-{int(time.time())}.csv"
        s = Sampler(session_id, game.id, block_id, game.exe_name, pid, path)
        s.start()
        log.info("硬件采集已挂载：%s（session %d%s）", game.exe_name, session_id,
                 f"，任务 {job_id}" if job_id else "")
        return s
    except Exception as e:
        log.warning("硬件采集启动失败（%s: %s）", e.__class__.__name__, e)
        return None


def finalize_async(s: Sampler):
    t = threading.Thread(target=_finalize, args=(s,), daemon=True,
                         name=f"hw-fin-{s.session_id}")
    t.start()
    return t


def _finalize(s: Sampler):
    try:
        s.stop()
        summary = summarize_csv(s.csv_path) or {}
    except Exception as e:
        log.warning("硬件采集收尾失败（%s: %s）", e.__class__.__name__, e)
        summary = {}
    try:
        from . import db
        conn = db.connect()
        db.insert_hw_run(conn, session_id=s.session_id, game_id=s.game_id,
                         block_id=s.block_id, start_ts=int(s.start_ts),
                         end_ts=int(time.time()), samples=summary.get("samples", 0),
                         csv_path=str(s.csv_path), summary=summary)
        conn.close()
    except Exception as e:
        log.warning("硬件摘要入库失败（%s: %s）", e.__class__.__name__, e)
    if summary.get("samples"):
        log.info("硬件采集完成：%d 个采样点，%s", summary["samples"],
                 "、".join(summary.get("flags") or ["无异常"]))
    sweep_old()


def sweep_old(keep: int = KEEP_SESSIONS):
    """只保留最近 keep 次的原始 CSV（每次约几百 KB，留着是为了事后分析）。"""
    try:
        files = sorted(capture_dir().glob("s*.csv"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        for f in files[keep:]:
            f.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 聚合

def _stats(vals: list) -> dict:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return {}
    n = len(v)
    return {"avg": round(sum(v) / n, 1), "p50": v[n // 2],
            "p95": v[min(n - 1, int(n * 0.95))], "max": v[-1], "min": v[0]}


def summarize_csv(path: Path) -> Optional[dict]:
    path = Path(path)
    if not path.is_file():
        return None
    cols: dict[str, list] = {c: [] for c in COLUMNS}
    throttles: dict[str, int] = {}
    n = 0
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            n += 1
            for c in COLUMNS:
                if c in ("gpu_throttle", "t"):
                    continue
                v = row.get(c, "")
                if v == "":
                    continue
                try:
                    cols[c].append(float(v))
                except ValueError:
                    pass
            th = row.get("gpu_throttle", "")
            if th and th not in ("0x0000000000000000", ""):
                throttles[th] = throttles.get(th, 0) + 1
    if not n:
        return None

    out = {"samples": n, "seconds": n * SAMPLE_INTERVAL}
    for c in ("cpu_total", "cpu_max_core", "other_cpu", "mem_avail_mb", "disk_read_mbs",
              "game_cpu", "game_ws_mb", "game_read_mbs", "game_faults_s",
              "gpu_util", "gpu_temp", "gpu_power", "gpu_clock", "gpu_mem_mb"):
        st = _stats(cols[c])
        if st:
            out[c] = st
    out["gpu_throttle_samples"] = sum(throttles.values())
    out["flags"] = _flags(out)
    return out


def _flags(s: dict) -> list:
    """把摘要压成几条人能一眼看懂的结论。"""
    f = []
    if s.get("gpu_throttle_samples"):
        f.append(f"GPU 降频 {s['gpu_throttle_samples']} 次采样")
    mem = s.get("mem_avail_mb", {})
    if mem and mem.get("min", 1e9) < 2000:
        f.append(f"内存吃紧（最低剩 {mem['min']:.0f} MB）")
    t = s.get("gpu_temp", {})
    if t and t.get("max", 0) >= 83:
        f.append(f"GPU 温度高（峰值 {t['max']:.0f}°C）")
    cpu = s.get("cpu_max_core", {})
    if cpu and cpu.get("p95", 0) >= 95:
        f.append("有 CPU 核长时间满载")
    # 零成本的"有没有别的东西在抢 CPU"探测。要知道是谁，再单独跑一次进程排查——
    # psutil 全表取每进程 CPU 要 3.3 秒，不能常驻扫（见 COLUMNS 注释）
    other = s.get("other_cpu", {})
    if other and other.get("p95", 0) >= 25:
        f.append(f"游戏之外还有东西在吃 CPU（非游戏占用 p95 {other['p95']:.0f}%）")
    return f


def describe(s: dict) -> list[str]:
    if not s or not s.get("samples"):
        return ["（没有硬件采样）"]
    def rng(key, unit, nd=0):
        v = s.get(key)
        if not v:
            return None
        return f"{v['avg']:.{nd}f}{unit}（峰值 {v['max']:.{nd}f}）"
    lines = []
    gpu = [x for x in (rng("gpu_util", "%"), rng("gpu_temp", "°C"),
                       rng("gpu_power", "W"), rng("gpu_clock", "MHz")) if x]
    if gpu:
        lines.append("GPU 占用 " + gpu[0] + (" · 温度 " + gpu[1] if len(gpu) > 1 else "")
                     + (" · 功耗 " + gpu[2] if len(gpu) > 2 else ""))
    cpu = s.get("cpu_total", {})
    mc = s.get("cpu_max_core", {})
    if cpu:
        lines.append(f"CPU 总占用 {cpu['avg']:.0f}%（峰值 {cpu['max']:.0f}%）"
                     + (f" · 最忙单核峰值 {mc['max']:.0f}%" if mc else ""))
    mem = s.get("mem_avail_mb", {})
    if mem:
        lines.append(f"可用内存 {mem['avg']/1000:.1f} GB（最低 {mem['min']/1000:.1f} GB）")
    g = s.get("game_ws_mb", {})
    if g:
        lines.append(f"游戏内存 {g['avg']/1000:.1f} GB（峰值 {g['max']/1000:.1f} GB）"
                     + (f" · 缺页峰值 {s['game_faults_s']['max']:.0f}/秒"
                        if s.get("game_faults_s") else ""))
    other = s.get("other_cpu", {})
    if other:
        lines.append(f"非游戏占用 平均 {other['avg']:.0f}%（峰值 {other['max']:.0f}%）")
    flags = s.get("flags")
    lines.append("异常：" + ("、".join(flags) if flags else "未发现"))
    return lines


def load_summary(row) -> dict:
    try:
        return json.loads(row["summary"]) if row["summary"] else {}
    except (ValueError, TypeError):
        return {}
