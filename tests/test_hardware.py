"""硬件采集：聚合口径、异常判读、开关，以及**采样器自身的开销**。

最后一条是重点：这东西跑在游戏旁边，测量本身不能让游戏更卡。

跑法：conda run -n gamelimiter python tests/test_hardware.py
"""

import csv
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gamelimiter import config

_tmp = Path(tempfile.mkdtemp())
config.DATA_DIR = _tmp
config.DB_PATH = _tmp / "test.db"

from gamelimiter import db, hardware

TMP = Path(tempfile.mkdtemp())


def write_csv(name, rows):
    p = TMP / name
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(hardware.COLUMNS)
        for r in rows:
            w.writerow([r.get(c, "") for c in hardware.COLUMNS])
    return p


def sample(t=0, cpu=20, core=40, mem=16000, gpu=60, temp=65, power=120, clock=2800,
           throttle="0x0000000000000000", ws=4000, faults=300, other=2):
    return {"t": t, "cpu_total": cpu, "cpu_max_core": core, "cpu_max_id": 3,
            "other_cpu": other,
            "mem_avail_mb": mem, "mem_pct": 50, "disk_read_mbs": 1.0, "disk_write_mbs": 0.5,
            "game_cpu": 300, "game_ws_mb": ws, "game_read_mbs": 2.0, "game_faults_s": faults,
            "game_handles": 2000,
            "gpu_util": gpu, "gpu_mem_mb": 5000, "gpu_temp": temp, "gpu_power": power,
            "gpu_clock": clock, "gpu_throttle": throttle}


# ---- 基础聚合 --------------------------------------------------------------
rows = [sample(t=i, cpu=20 + i % 10, gpu=50 + i % 20, temp=60 + i % 5) for i in range(120)]
s = hardware.summarize_csv(write_csv("basic.csv", rows))
assert s["samples"] == 120, s["samples"]
assert s["seconds"] == 120.0
assert s["cpu_total"]["min"] == 20 and s["cpu_total"]["max"] == 29, s["cpu_total"]
assert s["gpu_util"]["max"] == 69, s["gpu_util"]
assert s["gpu_temp"]["max"] == 64
assert s["gpu_throttle_samples"] == 0
assert s["flags"] == [], s["flags"]              # 一切正常 → 无异常标记

# ---- GPU 降频要被抓出来 ----------------------------------------------------
rows = ([sample() for _ in range(50)]
        + [sample(throttle="0x0000000000000004", clock=1800) for _ in range(20)])
s = hardware.summarize_csv(write_csv("throttle.csv", rows))
assert s["gpu_throttle_samples"] == 20, s["gpu_throttle_samples"]
assert any("降频" in f for f in s["flags"]), s["flags"]

# ---- 内存吃紧 --------------------------------------------------------------
s = hardware.summarize_csv(write_csv(
    "mem.csv", [sample(mem=1500 if i > 40 else 16000) for i in range(60)]))
assert any("内存吃紧" in f for f in s["flags"]), s["flags"]

# ---- GPU 高温 --------------------------------------------------------------
s = hardware.summarize_csv(write_csv("hot.csv", [sample(temp=86) for _ in range(30)]))
assert any("温度高" in f for f in s["flags"]), s["flags"]

# ---- 有核长期满载 ----------------------------------------------------------
s = hardware.summarize_csv(write_csv("busy.csv", [sample(core=99) for _ in range(30)]))
assert any("满载" in f for f in s["flags"]), s["flags"]

# ---- 干扰探测：非游戏占用高 → 该提示（谁在抢要另外单独排查）----------------
s = hardware.summarize_csv(write_csv("other.csv", [sample(cpu=60, other=40) for _ in range(40)]))
assert s["other_cpu"]["p95"] == 40, s["other_cpu"]
assert any("吃 CPU" in f for f in s["flags"]), s["flags"]
assert any("非游戏占用" in ln for ln in hardware.describe(s))

# 对照：游戏自己吃满、别的东西没动 → 不该提示
s = hardware.summarize_csv(write_csv("selfbusy.csv", [sample(cpu=60, other=3) for _ in range(40)]))
assert not any("吃 CPU" in f for f in s["flags"]), s["flags"]

# ---- 空文件 / 不存在 -------------------------------------------------------
assert hardware.summarize_csv(TMP / "nope.csv") is None
assert hardware.summarize_csv(write_csv("empty.csv", [])) is None
assert hardware.describe({}) == ["（没有硬件采样）"]

# ---- 缺列（GPU 不可用时那几列全空）也要能聚合 ------------------------------
nogpu = [{**sample(), "gpu_util": "", "gpu_temp": "", "gpu_power": "",
          "gpu_clock": "", "gpu_mem_mb": "", "gpu_throttle": ""} for _ in range(30)]
s = hardware.summarize_csv(write_csv("nogpu.csv", nogpu))
assert s["samples"] == 30 and "gpu_util" not in s, s.keys()
assert s["cpu_total"]["avg"] == 20                    # CPU 侧照常
assert hardware.describe(s)                            # 不炸

# ---- DB 往返 ---------------------------------------------------------------
conn = db.connect()
g = db.upsert_game(conn, "永劫无间", "NarakaBladepoint.exe")
sid = db.open_session(conn, g.id, 1_785_000_000)
db.insert_hw_run(conn, sid, g.id, sid, 1_785_000_000, 1_785_000_600,
                 samples=600, csv_path="/x/y.csv", summary=s)
r = db.hw_runs(conn, g.id)[0]
assert r["samples"] == 600 and r["csv_path"] == "/x/y.csv"
assert hardware.load_summary(r)["samples"] == 30

# ---- 开关 ------------------------------------------------------------------
assert hardware.enabled(conn)
db.set_setting(conn, hardware.SETTING_KEY, "0")
assert not hardware.enabled(conn)
assert hardware.start(conn, g, 1, 1) is None           # 关了就不该挂起来
db.set_setting(conn, hardware.SETTING_KEY, "1")

# ---- 保留策略：只留最近 N 次 -----------------------------------------------
d = hardware.capture_dir()
for i in range(8):
    (d / f"s{i}-{1_785_000_000 + i}.csv").write_text("x", encoding="utf-8")
    time.sleep(0.01)
hardware.sweep_old(keep=3)
left = list(d.glob("s*.csv"))
assert len(left) == 3, [p.name for p in left]

# ---- 真采一轮：验证采样器能跑、CSV 结构对、以及它自己的开销 ----------------
# 「游戏」用一个**独立子进程**烧 CPU，测试进程自己保持安静 —— 这样 me.cpu_times()
# 量到的才纯粹是采样器线程。（先前把烧 CPU 的循环写在测试进程主线程里，
# 量出 82.8% 的"采样开销"，其实全是负载本身，测量方法自己错了。）
print("\n起一个子进程当'游戏'烧 CPU，真采 7 秒，量采样器自身开销…")
import psutil
import subprocess

burner = subprocess.Popen(
    [sys.executable, "-c",
     "import time\nt=time.time()+9\nx=0\nwhile time.time()<t: x=(x+1)%1000003"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    time.sleep(0.5)
    me = psutil.Process()
    cpu_before = sum(me.cpu_times()[:2])
    t0 = time.time()

    s_obj = hardware.start(conn, g, 999, 999, pid=burner.pid)
    assert s_obj is not None
    time.sleep(7)
    hardware.finalize_async(s_obj).join(timeout=30)

    wall = time.time() - t0
    cpu_used = sum(me.cpu_times()[:2]) - cpu_before
finally:
    burner.terminate()
    try:
        burner.wait(timeout=10)
    except Exception:
        burner.kill()

ncpu = psutil.cpu_count() or 1
print(f"  采样器自身开销：{cpu_used:.3f} CPU 秒 / {wall:.1f} 秒墙钟"
      f" = 单核的 {cpu_used/wall*100:.2f}%，全部 {ncpu} 核的 {cpu_used/wall*100/ncpu:.3f}%")
assert cpu_used / wall < 0.05, f"采样开销过高：{cpu_used/wall*100:.1f}% of one core"

run = db.hw_runs(conn, g.id)[0]
assert run["session_id"] == 999, run["session_id"]
summ = hardware.load_summary(run)
print(f"  采到 {run['samples']} 个点，CSV={run['csv_path']}")
assert run["samples"] >= 3, run["samples"]
assert summ.get("cpu_total"), summ
# 游戏进程的 CPU 必须真的被测到。2026-08-13 第一次真机采集 723 个点的 game_cpu
# 全是 0——因为每次采样都新建 psutil.Process，而 cpu_percent() 算的是"距上次在
# **同一对象**上调用"的增量，新对象永远返回 0.0。那个子进程是满载的，读数必须显著非零
with Path(run["csv_path"]).open(encoding="utf-8") as f:
    got = [r["game_cpu"] for r in csv.DictReader(f) if r["game_cpu"] not in ("", None)]
assert got, "game_cpu 一列全空"
assert any(float(v) > 50 for v in got), f"game_cpu 没测到那个满载的子进程：{got}"
# 非游戏占用要把游戏那份扣掉，不能等于总占用
with Path(run["csv_path"]).open(encoding="utf-8") as f:
    rows2 = [r for r in csv.DictReader(f) if r["other_cpu"] and r["cpu_total"]]
assert any(float(r["other_cpu"]) < float(r["cpu_total"]) - 1 for r in rows2), \
    "other_cpu 没有扣掉游戏那份"
# 原始 CSV 必须留着（这是这个功能的意义：事后能捞出来分析）
assert Path(run["csv_path"]).is_file(), "硬件原始 CSV 不该被删"
with Path(run["csv_path"]).open(encoding="utf-8") as f:
    hdr = next(csv.reader(f))
assert hdr == hardware.COLUMNS, hdr
for line in hardware.describe(summ):
    print("   ", line)

print("\ntest_hardware: 全部通过")
