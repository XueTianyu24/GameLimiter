"""手动采集任务：下单 → 接单 → 到点/停止 → 收尾。

v0.16.0 把采集从"开游戏就自动采"改成"点了才采"，本测试钉住这条链路上的关键约定：
  1. 手动模式（新默认）下没下单就不采；下了单才采
  2. 任务状态机：armed → running → done，以及取消 / 过期 / 守护重启中断
  3. 存放目录不可写时回落默认目录，绝不让一次采集整个失败
  4. 保留原始帧数据时文件改名加 raw- 前缀，孤儿清理不能把它当垃圾收走
  5. 孤儿清理只删"1 小时没人动过且不在活跃名单里"的文件

跑法：conda run -n gamelimiter python tests/test_capture.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gamelimiter import config

_TMP = Path(tempfile.mkdtemp())
config.DATA_DIR = _TMP
config.DB_PATH = _TMP / "test.db"

from gamelimiter import db, frames, hardware

conn = db.connect()
NOW = time.time()
HOUR = 3600

# ---- 默认是手动模式（老库没这个键也一样）----------------------------------
assert db.get_capture_mode(conn) == "manual"
db.set_capture_mode(conn, "auto")
assert db.get_capture_mode(conn) == "auto"
db.set_capture_mode(conn, "manual")

g = db.upsert_game(conn, "帕鲁", "Palworld.exe", session_minutes=120)
other = db.upsert_game(conn, "永劫无间", "NarakaBladepoint.exe")

# ---- 没下单 = 没有待命任务，守护据此不采 -----------------------------------
assert db.armed_capture_job(conn, g.id, NOW) is None
assert db.active_capture_job(conn, g.id) is None

# ---- 下单 → armed ----------------------------------------------------------
job_id = db.create_capture_job(conn, g.id, 10.0, r"D:\采集", keep_raw=True,
                               expires_at=int(NOW + 4 * HOUR))
job = db.armed_capture_job(conn, g.id, NOW)
assert job["id"] == job_id and job["state"] == "armed"
assert job["duration_minutes"] == 10.0 and job["out_dir"] == r"D:\采集"
assert job["keep_raw"] == 1
# 只影响这一款游戏
assert db.armed_capture_job(conn, other.id, NOW) is None

# 同一游戏再下一单：旧的待命任务被顶掉，只留最新的一条活跃任务
job2_id = db.create_capture_job(conn, g.id, 30.0, None, keep_raw=False,
                                expires_at=int(NOW + 4 * HOUR))
assert db.capture_job(conn, job_id)["state"] == "cancelled"
assert db.armed_capture_job(conn, g.id, NOW)["id"] == job2_id

# ---- armed → running → done ------------------------------------------------
db.start_capture_job(conn, job2_id, session_id=77, started_ts=int(NOW))
r = db.capture_job(conn, job2_id)
assert r["state"] == "running" and r["session_id"] == 77
assert db.armed_capture_job(conn, g.id, NOW) is None      # 已接单，不该被重复挂上
assert db.active_capture_job(conn, g.id)["id"] == job2_id  # 但 GUI 仍看得到它在跑

db.finish_capture_job(conn, job2_id, "done", "采集时长到点")
r = db.capture_job(conn, job2_id)
assert r["state"] == "done" and r["note"] == "采集时长到点" and r["ended_ts"]
assert db.active_capture_job(conn, g.id) is None
# 收尾是幂等的：已结束的任务不该被后来的收尾改写
db.finish_capture_job(conn, job2_id, "cancelled", "重复收尾")
assert db.capture_job(conn, job2_id)["note"] == "采集时长到点"

# ---- 取消：待命中直接取消；采集中由守护看到状态变化后收尾 -------------------
j3 = db.create_capture_job(conn, g.id, 5.0, None, True, int(NOW + HOUR))
assert db.cancel_capture_job(conn, j3)
assert db.capture_job(conn, j3)["state"] == "cancelled"
assert not db.cancel_capture_job(conn, j3)          # 已结束的不能再取消

j4 = db.create_capture_job(conn, g.id, 5.0, None, True, int(NOW + HOUR))
db.start_capture_job(conn, j4, 78, int(NOW))
assert db.cancel_capture_job(conn, j4, "手动停止")
assert db.capture_job(conn, j4)["state"] == "cancelled"

# ---- 待命超时作废：点了采集却一直没开游戏，不该几天后突然采一场 -------------
j5 = db.create_capture_job(conn, g.id, 5.0, None, True, int(NOW - 1))
assert db.armed_capture_job(conn, g.id, NOW) is None       # 过期的不会被接单
assert db.expire_capture_jobs(conn, NOW) == 1
assert db.capture_job(conn, j5)["state"] == "expired"

# ---- 守护重启：running 的任务永远等不到收尾，启动时如实标记中断 -------------
j6 = db.create_capture_job(conn, g.id, 60.0, None, True, int(NOW + HOUR))
db.start_capture_job(conn, j6, 79, int(NOW))
assert db.abandon_running_capture_jobs(conn) == 1
r = db.capture_job(conn, j6)
assert r["state"] == "done" and "中断" in r["note"]
assert db.abandon_running_capture_jobs(conn) == 0           # 再跑一次不该重复处理

# ---- 存放目录：能写就用它，不能写回落默认目录（守护是 SYSTEM，未必看得见） ----
want = _TMP / "out"
d, reason = config.resolve_capture_dir(str(want), "frames")
assert d == want and reason is None and d.is_dir()
assert not (d / ".gl_write_probe").exists()                 # 探测文件要擦干净

bad = _TMP / "blocked.txt"
bad.write_text("我是个文件，不是目录", encoding="utf-8")
d, reason = config.resolve_capture_dir(str(bad), "frames")
assert d == config.DATA_DIR / "frames" and reason, (d, reason)
assert frames.capture_dir(str(bad)) == config.DATA_DIR / "frames"
assert hardware.capture_dir(str(bad)) == config.DATA_DIR / "hw"
d, reason = config.resolve_capture_dir(None, "hw")
assert d == config.DATA_DIR / "hw" and reason is None

# ---- 保留原始帧数据：改名加 raw- 前缀，孤儿清理不该动它 ---------------------
fdir = frames.capture_dir()
raw = fdir / "s99-123.csv"
raw.write_text("Application,MsBetweenPresents\n", encoding="utf-8")


class _FakeCap:
    csv_path = raw
    keep_raw = True


kept = frames._keep_raw_file(_FakeCap())
assert kept == fdir / "raw-s99-123.csv" and kept.exists()
assert not raw.exists()

# 孤儿清理：改名过的留着，没人管的旧文件删掉，正在写的（在活跃名单里）不动
orphan = fdir / "s100-456.csv"
orphan.write_text("x" * 100, encoding="utf-8")
orphan.with_suffix(".err").write_text("err", encoding="utf-8")
live = fdir / "s101-789.csv"
live.write_text("x", encoding="utf-8")
old = time.time() - 2 * HOUR
for f in (kept, orphan, orphan.with_suffix(".err"), live):
    import os
    os.utime(f, (old, old))

frames.sweep_stale(exclude=[live])
assert kept.exists(), "保留下来的原始数据被孤儿清理误删了"
assert live.exists(), "正在写的采集文件被误删了"
assert not orphan.exists() and not orphan.with_suffix(".err").exists()

# 时间没到的不删（正常采集中的文件 mtime 一直在刷新）
fresh = fdir / "s102-999.csv"
fresh.write_text("x", encoding="utf-8")
frames.sweep_stale()
assert fresh.exists()

# ---- 硬件数据只轮转默认目录，用户自己指定的目录不碰 -------------------------
hdir = hardware.capture_dir()
for i in range(hardware.KEEP_SESSIONS + 3):
    (hdir / f"s{i}-{i}.csv").write_text("t\n", encoding="utf-8")
mine = _TMP / "out" / "s999-999.csv"
mine.write_text("t\n", encoding="utf-8")
hardware.sweep_old()
assert len(list(hdir.glob("s*.csv"))) == hardware.KEEP_SESSIONS
assert mine.exists(), "用户指定目录里的数据不该被轮转清理"

# ---- 列表查询按游戏隔离 -----------------------------------------------------
db.create_capture_job(conn, other.id, None, None, True, int(NOW + HOUR))
assert len(db.capture_jobs(conn, g.id)) >= 6
assert len(db.capture_jobs(conn, other.id)) == 1
assert db.capture_jobs(conn, other.id)[0]["duration_minutes"] is None    # 整场

# ---- 默认存放目录记忆 -------------------------------------------------------
assert db.get_capture_out_dir(conn) is None
db.set_capture_out_dir(conn, r"D:\采集")
assert db.get_capture_out_dir(conn) == r"D:\采集"
db.set_capture_out_dir(conn, "  ")
assert db.get_capture_out_dir(conn) is None      # 空白 = 回到默认目录

print("test_capture: 全部通过")
