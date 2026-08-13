"""端到端：真起 PresentMon 子进程走完整条链路（隔离库，约 20 秒）。

单测 `test_frames.py` 只喂合成/真实 CSV 验聚合；这里补的是**进程那一段**：
挂载 → 采集器真的在跑 → 收尾（等它退出/强杀）→ 聚合 → 入库 → 删原始 CSV。

目标进程选 `dwm.exe`（桌面窗口管理器，一直在）。它出多少帧取决于当时屏幕在不在动，
所以**不断言帧数**，只断言链路走完、异常不外溢 —— 帧数只报告不判定。

未提权时 PresentMon 会因拿不到 ETW 会话而失败；那也是要验的路径：必须优雅降级
（记一条 no_frames，不能崩守护）。

跑法：conda run -n gamelimiter python tests/e2e_frames.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gamelimiter import config

_tmp = Path(tempfile.mkdtemp())
config.DATA_DIR = _tmp                      # 别碰真实 ProgramData
config.DB_PATH = _tmp / "test.db"

from gamelimiter import db, frames

TARGET = "dwm.exe"
CAPTURE_SECONDS = 8

pm = frames.presentmon_path()
print(f"采集器：{pm}")
if pm is None:
    sys.exit("找不到 PresentMon —— 先跑 python scripts/fetch_presentmon.py")

ok, msg = frames.preflight()
print(f"可用性自检：{'✓' if ok else '✗'} {msg}")
if not ok:
    print("→ 本机权限不够，只验降级路径（台式机上守护是 SYSTEM，不受影响）")

conn = db.connect()
g = db.upsert_game(conn, "e2e 假游戏", TARGET, session_minutes=120)
sid = db.open_session(conn, g.id, int(time.time()))

print(f"挂载采集器，目标 {TARGET}，采 {CAPTURE_SECONDS} 秒…")
cap = frames.start(conn, g, sid, sid)
assert cap is not None, "frames.start 返回 None —— 采集器没挂上"
assert cap.csv_path.parent == frames.capture_dir()

time.sleep(2)
alive = cap.alive()
if ok:
    assert alive, "提权够却起来就死了"
    print("采集器在跑 ✓")
else:
    assert not alive, "预期未提权会失败，却还活着？"
    print(f"采集器如期失败退出（exit={cap.proc.returncode}）——错误已落盘，验证能读出来")
    err = frames.read_error(cap.err_path)
    assert "access denied" in err.lower(), f"没读到预期的权限错误：{err!r}"
    print(f"  错误原文：{err[:90]}…")

time.sleep(CAPTURE_SECONDS if ok else 1)
csv_before = cap.csv_path.exists()
size_before = cap.csv_path.stat().st_size if csv_before else 0
print(f"采集中 CSV：exists={csv_before} size={size_before}")

t0 = time.time()
t = frames.finalize_async(cap)
t.join(timeout=frames.STOP_GRACE_SECONDS + 30)
assert not t.is_alive(), "收尾线程没在预期时间内结束"
print(f"收尾耗时 {time.time() - t0:.1f}s")

# 原始 CSV 与 stderr 都必须被删掉——单次 CSV 可达 200MB，留着会把盘吃光
assert not cap.csv_path.exists(), f"原始 CSV 没删：{cap.csv_path}"
assert not cap.err_path.exists(), f"stderr 文件没删：{cap.err_path}"
print("原始 CSV / stderr 已清理 ✓")

rows = db.frame_runs(conn, g.id)
assert len(rows) == 1, f"应恰好入库 1 条，实际 {len(rows)}"
r = rows[0]
assert r["session_id"] == sid and r["block_id"] == sid
assert r["status"] in ("ok", "no_frames", "truncated", "failed"), r["status"]
print(f"入库 ✓ status={r['status']} frames={r['frames']} seconds={r['seconds']}")

s = frames.load_summary(r)
if s.get("frames"):
    for line in frames.describe(s):
        print("   ", line)
    # 真采到帧时，摘要的自洽性要成立
    assert s["fps_avg"] > 0 and s["ft_p50"] > 0
    assert s["fps_low1"] <= s["fps_avg"] + 0.01, (s["fps_low1"], s["fps_avg"])
    assert s["ft_p99"] >= s["ft_p50"] and s["ft_max"] >= s["ft_p99"]
    assert s["bound"] in frames.BOUND_ZH
    blk = db.block_frame_summary(conn, sid)
    assert blk and blk["frames"] == s["frames"]
    print("摘要自洽 ✓ 段级聚合 ✓")
else:
    print(f"没采到帧（status={r['status']}）—— 屏幕没动或未提权，属正常；"
          f"关键是没崩、优雅降级了")
    if not ok:
        # 未提权那条路径必须把原因存下来，否则以后只能看到"没数据"却查不出为什么
        assert "access denied" in (s.get("error") or "").lower(), s
        print(f"  失败原因已入库 ✓：{s['error'][:70]}…")

# ---- 采集器缺失时必须安静降级，不能拦住游戏 ----
import os
os.environ["GAMELIMITER_PRESENTMON"] = str(_tmp / "nope.exe")
os.environ.pop("GAMELIMITER_NO_FRAMES", None)
sid2 = db.open_session(conn, g.id, int(time.time()))
assert frames.start(conn, g, sid2, sid2) is None, "采集器不存在时应返回 None 而不是抛"
print("采集器缺失时优雅降级 ✓")

print("\ne2e_frames: 通过")
