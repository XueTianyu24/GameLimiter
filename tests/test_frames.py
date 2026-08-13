"""帧时间采集的聚合层测试（不需要 PresentMon，用合成 CSV 喂）。

重点复现 2026-08-11 那次人工排查的场景：帕鲁 `FrameRateLimit=90` 撞 165 Hz 屏幕，
显卡 CPU 都没吃满、平均帧数好看，眼睛却一顿一顿。这套聚合要能自动认出来。

跑法：conda run -n gamelimiter python tests/test_frames.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gamelimiter import config

config.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"    # 隔离库（须在 db.connect 前）

from gamelimiter import db, frames

TMP = Path(tempfile.mkdtemp())

COLS = ["Application", "ProcessID", "SwapChainAddress", "SyncInterval", "AllowsTearing",
        "PresentMode", "FrameType", "MsBetweenPresents", "MsBetweenDisplayChange",
        "MsCPUBusy", "MsGPUBusy", "MsGPUWait", "MsClickToPhotonLatency"]


def write_csv(name, rows, cols=COLS, bom=False):
    """rows = [dict]，缺的列填 NA。bom=True 模拟 PresentMon 真实产物（带 UTF-8 BOM）。"""
    p = TMP / name
    with p.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join(str(r.get(c, "NA")) for c in cols) + "\n")
    return p


def frame(ft, disp=None, cpu=1.5, gpu=2.0, gw=0.2, mode="Hardware: Independent Flip",
          sync=0, tear=1, ftype="Application", pid="1000", swap="0xA"):
    return {"Application": "game.exe", "ProcessID": pid, "SwapChainAddress": swap,
            "SyncInterval": sync, "AllowsTearing": tear, "PresentMode": mode,
            "FrameType": ftype, "MsBetweenPresents": ft,
            "MsBetweenDisplayChange": ft if disp is None else disp,
            "MsCPUBusy": cpu, "MsGPUBusy": gpu, "MsGPUWait": gw,
            "MsClickToPhotonLatency": 20.0}


# ---- 基础统计口径 ----------------------------------------------------------
# 990 帧 10ms + 10 帧 50ms：平均 10.4ms → 96.2 fps；最慢 1%（10 帧）全是 50ms → 20 fps
rows = [frame(10.0) for _ in range(990)] + [frame(50.0) for _ in range(10)]
s = frames.summarize_csv(write_csv("basic.csv", rows))
assert s["frames"] == 1000, s["frames"]
assert abs(s["fps_avg"] - 96.2) < 0.3, s["fps_avg"]
assert abs(s["fps_low1"] - 20.0) < 0.5, s["fps_low1"]      # 1% low = 最慢 1% 帧的平均帧率
assert s["ft_p50"] == 10.0 and s["ft_max"] == 50.0
assert s["hitches"] == 10, s["hitches"]                    # 超过中位数 2 倍的帧
assert abs(s["seconds"] - 10.4) < 0.1, s["seconds"]
assert s["present_mode"] == "Hardware: Independent Flip"

# ---- 瓶颈定性：显卡吃满 ----------------------------------------------------
s = frames.summarize_csv(write_csv(
    "gpu.csv", [frame(10.0, cpu=3.0, gpu=9.6) for _ in range(500)]))
assert s["bound"] == "gpu", s

# ---- 瓶颈定性：CPU 吃满 ----------------------------------------------------
s = frames.summarize_csv(write_csv(
    "cpu.csv", [frame(10.0, cpu=9.7, gpu=3.0) for _ in range(500)]))
assert s["bound"] == "cpu", s

# ---- 瓶颈定性：帧率被限制（帕鲁那个坑）------------------------------------
# 锁 90 fps：帧时间稳定在 11.11ms，显卡 CPU 都只用了三分之一 —— 谁都没吃满却很整齐
s = frames.summarize_csv(write_csv(
    "capped.csv", [frame(11.11 + (i % 3) * 0.02, cpu=3.4, gpu=3.9) for i in range(600)]))
assert s["bound"] == "capped", s
assert abs(s["fps_avg"] - 90.0) < 0.5, s["fps_avg"]

# ---- 节奏不齐：90 fps 送进 165 Hz 屏 ---------------------------------------
# 165 Hz 一个刷新周期 6.06ms。要凑出 90 fps，约 5/6 的帧占 2 个周期(12.12)、
# 1/6 占 1 个(6.06)，两种值不规则交替 —— 平均帧数好看，眼睛却在顿。
# 必须 tear=0：撕裂关掉画面才按刷新周期量化，这个指标才有物理意义（见下面的对照）
mixed = []
for i in range(600):
    disp = 6.06 if i % 6 == 0 else 12.12
    mixed.append(frame(11.11, disp=disp, cpu=3.4, gpu=3.9, sync=1, tear=0))
s = frames.summarize_csv(write_csv("judder.csv", mixed))
assert s["judder_pct"] > 15, s["judder_pct"]               # 该被认出来
assert s["bound"] == "capped", s["bound"]
assert any("节奏不齐" in ln for ln in frames.describe(s)), frames.describe(s)

# 对照：锁死在 165 Hz 整数分频上（每帧都占 1 个周期）→ 节奏整齐
even = [frame(6.06, disp=6.06, cpu=2.0, gpu=3.0, sync=1, tear=0) for _ in range(600)]
s_even = frames.summarize_csv(write_csv("even.csv", even))
assert s_even["judder_pct"] < 5, s_even["judder_pct"]
assert not any("节奏不齐" in ln for ln in frames.describe(s_even))

# ---- 开着撕裂时「节奏不齐」无意义，必须闭嘴 --------------------------------
# 2026-08-12 实测永劫无间：撕裂 100%、显示间隔中位数 3.70ms，而 165Hz 屏幕周期是
# 6.06ms —— 帧是撕裂着扫出去的，根本不按刷新周期量化。原先会误报 40.1% 节奏不齐
torn = []
for i in range(600):
    torn.append(frame(3.7, disp=(2.4 if i % 3 else 6.1), cpu=3.6, gpu=2.3, sync=0, tear=1))
s_torn = frames.summarize_csv(write_csv("torn.csv", torn))
assert s_torn["tearing_pct"] == 100.0, s_torn["tearing_pct"]
assert s_torn["judder_pct"] is None, s_torn["judder_pct"]        # 不给数，而不是给个假数
assert not any("节奏不齐" in ln for ln in frames.describe(s_torn))
assert any("允许撕裂" in ln for ln in frames.describe(s_torn)), frames.describe(s_torn)

# ---- 卡顿判据要有绝对下限，否则高帧率下虚报 --------------------------------
# 平均 278 fps（中位 3.76ms）时 2× 中位数才 7.5ms，等于把"掉到 133 fps"算成卡顿。
# 实测永劫无间那 60 秒：按 2× 报 61 次/分，按 >16.7ms 报 19 次/分，后者才对得上体感
fast = [frame(3.76, cpu=3.6, gpu=2.3) for _ in range(9000)]
fast += [frame(8.0) for _ in range(60)]        # 掉到 125 fps：不该算卡顿
fast += [frame(40.0) for _ in range(10)]       # 掉到 25 fps：该算
s_fast = frames.summarize_csv(write_csv("fast.csv", fast))
assert s_fast["hitch_ms"] == 16.7, s_fast["hitch_ms"]      # 取绝对下限而非 2×中位数(7.5)
assert s_fast["hitches"] == 10, s_fast["hitches"]          # 只数那 10 帧，不含 8ms 那 60 帧
assert s_fast["worst_frames"][0] == 40.0, s_fast["worst_frames"]

# 低帧率下则回到 2× 中位数（下限不能反过来把真卡顿放过）
slow = [frame(33.3) for _ in range(600)] + [frame(80.0) for _ in range(10)]
s_slow = frames.summarize_csv(write_csv("slow.csv", slow))
assert abs(s_slow["hitch_ms"] - 66.6) < 0.2, s_slow["hitch_ms"]   # 2×33.3，不是 16.7
assert s_slow["hitches"] == 10, s_slow["hitches"]

# 最慢的几帧要留原样，「最狠一次冻了 0.3 秒」比分位数更能说明问题
froze = [frame(6.0) for _ in range(1000)] + [frame(298.0)]
s_froze = frames.summarize_csv(write_csv("froze.csv", froze))
assert s_froze["worst_frames"][0] == 298.0, s_froze["worst_frames"]
assert any("冻了 0.30 秒" in ln for ln in frames.describe(s_froze)), frames.describe(s_froze)

# 最慢那帧要带上发生时刻——逐帧 CSV 会被删掉，只有它能拿去和硬件逐秒采样对齐。
# 1000 帧 x 6ms = 6.0 秒，那一帧就发生在第 6.3 秒（含它自己 0.298s）
assert abs(s_froze["worst_at"][0] - 6.3) < 0.15, s_froze["worst_at"]
assert any("第6.3秒" in ln for ln in frames.describe(s_froze)), frames.describe(s_froze)

# 多个慢帧时时刻要一一对应、按帧时间降序
multi = ([frame(5.0)] * 200 + [frame(120.0)] + [frame(5.0)] * 200 + [frame(300.0)])
sm = frames.summarize_csv(write_csv("worstat.csv", multi))
assert sm["worst_frames"][:2] == [300.0, 120.0], sm["worst_frames"]
assert sm["worst_at"][0] > sm["worst_at"][1], sm["worst_at"]   # 300ms 那帧在后面

# ---- 多交换链：取帧数最多的那条当游戏画面 ----------------------------------
many = ([frame(8.0, pid="1000", swap="0xA") for _ in range(400)]
        + [frame(33.0, pid="1000", swap="0xB") for _ in range(20)])   # 启动器/小窗
s = frames.summarize_csv(write_csv("multi.csv", many))
assert s["frames"] == 400 and s["ft_p50"] == 8.0, s
assert s["extra_swapchains"] == 1, s

# ---- NA / 脏值不能把整次采集搞崩 -------------------------------------------
dirty = [frame(10.0) for _ in range(50)]
dirty += [{"Application": "game.exe", "ProcessID": "1000", "SwapChainAddress": "0xA",
           "MsBetweenPresents": "NA"}]                     # 没有帧时间的行直接跳过
dirty += [frame(10.0, disp="NA", cpu="NA")]                # 个别列 NA 仍要能用
s = frames.summarize_csv(write_csv("dirty.csv", dirty))
assert s["frames"] == 51, s["frames"]

# ---- 生成帧（帧生成开着）---------------------------------------------------
fg = ([frame(6.0, ftype="Application") for _ in range(300)]
      + [frame(6.0, ftype="Repeated") for _ in range(100)])
s = frames.summarize_csv(write_csv("fg.csv", fg))
assert abs(s["generated_pct"] - 25.0) < 0.1, s["generated_pct"]
assert any("生成帧" in ln for ln in frames.describe(s))

# ---- BOM：PresentMon 真实产物带 UTF-8 BOM ----------------------------------
# 2026-08-11 用真实 CSV 才发现的坑：按 utf-8 读会让首列名变成 '﻿Application'，
# 整个表头对不上、静默返回 None（合成 CSV 不带 BOM，测不出来）
s = frames.summarize_csv(write_csv("bom.csv", [frame(10.0) for _ in range(100)], bom=True))
assert s is not None and s["frames"] == 100, s
assert s["present_mode"] == "Hardware: Independent Flip", s     # 首列之外的也要对得上

# ---- 空文件 / 不存在 -------------------------------------------------------
assert frames.summarize_csv(TMP / "nope.csv") is None
assert frames.summarize_csv(write_csv("empty.csv", [])) is None

# ---- 每分钟趋势：越玩越卡 --------------------------------------------------
# 前 3 分钟 8ms（125fps），后 3 分钟 16ms（62fps）
trend_rows = ([frame(8.0) for _ in range(3 * 60 * 125)]
              + [frame(16.0) for _ in range(3 * 60 * 62)])
s = frames.summarize_csv(write_csv("trend.csv", trend_rows))
pm = s["per_minute"]
assert len(pm) >= 6, len(pm)
assert pm[0][1] > 120 and pm[-1][1] < 70, (pm[0], pm[-1])

# ---- DB 往返 + 段级加权合并 ------------------------------------------------
conn = db.connect()
g = db.upsert_game(conn, "帕鲁", "Palworld.exe", session_minutes=120)
s1 = db.open_session(conn, g.id, 1_785_000_000)
s2 = db.open_session(conn, g.id, 1_785_003_000, None, s1)          # 同段第二次运行

db.insert_frame_run(conn, s1, g.id, s1, 1_785_000_000, 1_785_001_000,
                    {"frames": 1000, "seconds": 600.0, "fps_avg": 100.0,
                     "fps_low1": 60.0, "hitches": 10, "ft_max": 40.0}, "ok")
db.insert_frame_run(conn, s2, g.id, s1, 1_785_003_000, 1_785_004_000,
                    {"frames": 3000, "seconds": 600.0, "fps_avg": 140.0,
                     "fps_low1": 100.0, "hitches": 2, "ft_max": 25.0}, "ok")

merged = db.block_frame_summary(conn, s1)
assert merged["runs"] == 2 and merged["frames"] == 4000, merged
# 按帧数加权：(100*1000 + 140*3000) / 4000 = 130
assert abs(merged["fps_avg"] - 130.0) < 0.01, merged["fps_avg"]
assert abs(merged["fps_low1"] - 90.0) < 0.01, merged["fps_low1"]
assert merged["hitches"] == 12 and merged["ft_max"] == 40.0
assert merged["seconds"] == 1200.0
assert abs(merged["hitches_per_min"] - 0.6) < 0.01, merged["hitches_per_min"]

# 只有一条时 = 精确值，不走加权
s3 = db.open_session(conn, g.id, 1_785_009_000)
db.insert_frame_run(conn, s3, g.id, s3, 1_785_009_000, 1_785_009_500,
                    {"frames": 500, "seconds": 100.0, "fps_avg": 77.0}, "ok")
assert db.block_frame_summary(conn, s3)["fps_avg"] == 77.0

# 没采到帧的运行不参与合并，也不该让段级摘要变成 None
s4 = db.open_session(conn, g.id, 1_785_010_000)
db.insert_frame_run(conn, s4, g.id, s4, 1_785_010_000, 1_785_010_100, {}, "no_frames")
assert db.block_frame_summary(conn, s4) is None
assert len(db.frame_runs(conn, g.id)) == 4                 # 但记录本身要留着

# ---- 开关 ------------------------------------------------------------------
assert frames.enabled(conn)                                # 默认开
db.set_setting(conn, frames.SETTING_KEY, "0")
assert not frames.enabled(conn)
db.set_setting(conn, frames.SETTING_KEY, "1")
assert frames.enabled(conn)

# 关掉时 start() 必须直接返回 None（连采集器都不去找）
db.set_setting(conn, frames.SETTING_KEY, "0")
assert frames.start(conn, g, 999, 999) is None
db.set_setting(conn, frames.SETTING_KEY, "1")

# ---- describe 不能在任何摘要上炸 -------------------------------------------
assert frames.describe({}) == ["（没采到帧数据）"]
assert frames.describe({"frames": 0}) == ["（没采到帧数据）"]
for name in ("basic.csv", "gpu.csv", "capped.csv", "judder.csv", "multi.csv"):
    assert frames.describe(frames.summarize_csv(TMP / name))

print("test_frames: 全部通过")
