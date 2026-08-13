"""端到端验证「手动采集」：真守护 + 真进程（notepad 当假游戏），隔离库。

v0.16.0 的核心改动是采集从"开游戏就采"改成"点了才采"，这条链路只有真守护跑起来
才验得了。四件事：
  1. 没下单 → 一个采集器都不起（这是本次改动的全部意义）
  2. 游戏已经在跑时下单 → 守护 1 秒内接单开采
  3. 时长到点 → 采集停、**游戏照跑**（采集与限制彻底解耦）
  4. 手动停止 → 采集立刻收尾入库，数据落在指定目录里

帧采集需要管理员权限，开发机上普通权限起不来（会记 no_frames），所以这里以硬件
采集为准——它普通权限就能跑。

跑法：conda 环境的 python tests/e2e_capture.py   （约 40 秒）
"""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp" / "e2e_capture_data"
OUT = ROOT / ".tmp" / "e2e_capture_out"
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)
os.environ["ProgramData"] = str(TMP)          # 隔离库（大小写必须一致，见 USAGE 坑 8）
os.environ["GAMELIMITER_SILENT"] = "1"
os.environ["GAMELIMITER_NO_WATCHDOG"] = "1"
sys.path.insert(0, str(ROOT))

from gamelimiter import db  # noqa: E402

PY = sys.executable
fails = []


def check(label, cond, extra=""):
    print(f"{'  OK ' if cond else '  !! '} {label}{'  ' + extra if extra else ''}")
    if not cond:
        fails.append(label)


def ps(cmd):
    subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                   capture_output=True, check=False)


def notepad_alive() -> bool:
    import psutil
    return any((p.info["name"] or "").lower() == "notepad.exe"
               for p in psutil.process_iter(["name"]))


def wait_until(cond, timeout=20.0, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def main():
    print(f"DB: {db.config.DB_PATH}\nOUT: {OUT}")
    ps("Stop-Process -Name notepad -Force -ErrorAction SilentlyContinue")
    for f in OUT.glob("*.csv"):
        f.unlink()
    conn = db.connect()
    for t in ("sessions", "events", "hw_runs", "frame_runs", "capture_jobs"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    # 无任何限制，免得 notepad 被规则杀掉干扰采集验证
    g = db.upsert_game(conn, "假游戏", "notepad.exe")
    assert db.get_capture_mode(conn) == "manual", "默认应当是手动模式"

    daemon = subprocess.Popen([PY, "-m", "gamelimiter.daemon"], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)

        print("\n[1] 手动模式下开游戏但没下单 —— 不该有任何采集")
        ps("Start-Process notepad")
        time.sleep(6)
        check("会话已开始", db.active_session(conn, g.id) is not None)
        check("没有硬件采集记录", conn.execute("SELECT COUNT(*) c FROM hw_runs")
              .fetchone()["c"] == 0)
        check("没有帧采集记录", conn.execute("SELECT COUNT(*) c FROM frame_runs")
              .fetchone()["c"] == 0)
        check("没有采集任务", db.active_capture_job(conn, g.id) is None)

        print("\n[2] 游戏正在跑时下单 12 秒 —— 守护应当立刻接单")
        job_id = db.create_capture_job(conn, g.id, 0.2, str(OUT), keep_raw=True,
                                       expires_at=int(time.time() + 3600))
        t0 = time.time()
        picked = wait_until(lambda: db.capture_job(conn, job_id)["state"] == "running", 8)
        check("采集任务已被接单（running）", picked, f"{time.time()-t0:.1f}s")
        r = db.capture_job(conn, job_id)
        check("记下了所属会话", r["session_id"] == db.active_session(conn, g.id)["id"])

        print("\n[3] 到点后采集停止，但游戏照跑")
        done = wait_until(lambda: db.capture_job(conn, job_id)["state"] == "done", 25)
        check("采集任务已结束", done, f"共 {time.time()-t0:.1f}s")
        r = db.capture_job(conn, job_id)
        check("结束原因 = 采集时长到点", r["note"] == "采集时长到点", str(r["note"]))
        check("采集时长≈12 秒", 10 <= (r["ended_ts"] - r["started_ts"]) <= 18,
              f"{r['ended_ts'] - r['started_ts']}s")
        check("游戏仍在跑（采集停 ≠ 游戏停）", notepad_alive())
        check("会话仍开着", db.active_session(conn, g.id) is not None)

        got_hw = wait_until(lambda: conn.execute("SELECT COUNT(*) c FROM hw_runs")
                            .fetchone()["c"] == 1, 15)
        check("硬件数据已入库", got_hw)
        hw = conn.execute("SELECT * FROM hw_runs ORDER BY id DESC LIMIT 1").fetchone()
        if hw:
            check("采样点数≈12（1 Hz）", 8 <= (hw["samples"] or 0) <= 16,
                  f"samples={hw['samples']}")
            check("原始数据落在指定目录里", str(OUT) in (hw["csv_path"] or ""),
                  hw["csv_path"] or "")
            check("原始 CSV 确实存在", Path(hw["csv_path"]).exists() if hw["csv_path"]
                  else False)

        print("\n[4] 再下一单整场的，中途手动停止")
        job2 = db.create_capture_job(conn, g.id, None, str(OUT), keep_raw=False,
                                     expires_at=int(time.time() + 3600))
        check("接单（整场）", wait_until(
            lambda: db.capture_job(conn, job2)["state"] == "running", 8))
        time.sleep(4)
        db.cancel_capture_job(conn, job2, "手动停止")
        stopped = wait_until(lambda: conn.execute("SELECT COUNT(*) c FROM hw_runs")
                             .fetchone()["c"] == 2, 15)
        check("停止后数据已收尾入库", stopped)
        check("游戏还在跑", notepad_alive())
        check("任务状态 = cancelled", db.capture_job(conn, job2)["state"] == "cancelled")

        print("\n[5] 关掉游戏 —— 没有下单，之后不该再采")
        ps("Stop-Process -Name notepad -Force -ErrorAction SilentlyContinue")
        time.sleep(3)
        ps("Start-Process notepad")
        time.sleep(6)
        check("仍然只有 2 条硬件记录",
              conn.execute("SELECT COUNT(*) c FROM hw_runs").fetchone()["c"] == 2)
    finally:
        ps("Stop-Process -Name notepad -Force -ErrorAction SilentlyContinue")
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()

    print("\n--- 采集任务 ---")
    for r in conn.execute("SELECT * FROM capture_jobs ORDER BY id"):
        dur = f"{r['duration_minutes']:g}min" if r["duration_minutes"] else "整场"
        print(f"  任务{r['id']}  {dur}  {r['state']}  {r['note'] or ''}")
    print("--- 落盘文件 ---")
    for f in sorted(OUT.glob("*")):
        print(f"  {f.name}  {f.stat().st_size/1024:.1f} KB")

    print("\n" + ("e2e_capture: 全部通过" if not fails
                  else f"e2e_capture: {len(fails)} 项失败 → {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
