"""端到端验证「一段游玩」：真守护 + 真进程（notepad 当假游戏），隔离库。

压缩版的 2026-08-01 实际场景：上限 30 秒（= 生产的 60 分钟）、冷却 1 小时。
玩 12 秒 → 关掉 → 立刻再开，应当「接着玩」剩下的 18 秒，而不是被冷却挡住、
也不是重新发满 30 秒。

跑法：conda 环境的 python tests/e2e_block.py
（会起真守护、开关 notepad，约 45 秒；数据写 .tmp/ 下的隔离库，不碰 C:\\ProgramData）
"""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp" / "e2e_block_data"
TMP.mkdir(parents=True, exist_ok=True)
os.environ["ProgramData"] = str(TMP)          # 隔离库（大小写必须一致，见 USAGE 坑 8）
os.environ["GAMELIMITER_SILENT"] = "1"
os.environ["GAMELIMITER_NO_WATCHDOG"] = "1"
sys.path.insert(0, str(ROOT))

from gamelimiter import config, db, rules      # noqa: E402

PY = sys.executable
CAP_SECONDS = 30
fails = []


def check(label, cond, extra=""):
    print(f"{'  OK ' if cond else '  !! '} {label}{'  ' + extra if extra else ''}")
    if not cond:
        fails.append(label)


def ps(cmd):
    subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                   capture_output=True, check=False)


def start_notepad():
    ps("Start-Process notepad")


def kill_notepad():
    ps("Stop-Process -Name notepad -Force -ErrorAction SilentlyContinue")


def block(conn, gid):
    return db.current_block(conn, gid)


def main():
    print(f"DB: {config.DB_PATH}")
    kill_notepad()
    conn = db.connect()
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM events")
    conn.commit()
    g = db.upsert_game(conn, "假游戏", "notepad.exe",
                       cooldown_hours=1, session_minutes=CAP_SECONDS / 60)

    daemon = subprocess.Popen([PY, "-m", "gamelimiter.daemon"], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)

        print("\n[1] 开玩 12 秒后自己关掉")
        start_notepad()
        time.sleep(12)
        kill_notepad()
        time.sleep(3)
        b = block(conn, g.id)
        check("会话已结束", not b["running"])
        check("按真实在跑时间计时（≈12 秒）", 9 <= b["played_seconds"] <= 15,
              f"played={b['played_seconds']:.1f}s")
        check("只有 1 次会话", b["sessions"] == 1)

        print("\n[2] 立刻再打开 —— 应当接着玩，而不是被冷却挡住")
        alive = rules.block_alive(b, g.session_minutes, time.time(), config.IDLE_GRACE_MINUTES)
        check("这一段还活着", alive)
        left = rules.block_remaining(g.session_minutes, b)
        check("剩余额度 ≈18 秒", 15 <= left <= 21, f"left={left:.1f}s")
        check("不续玩的话冷却会拦（改造前的行为）",
              not rules.check_start(g, db.last_session_end(conn, g.id), time.time()).allowed)

        start_notepad()
        time.sleep(4)
        b2 = block(conn, g.id)
        check("进程没被杀掉（冷却已跳过）", b2["running"])
        check("并入同一段（不是新开一场）", b2["block_id"] == b["block_id"],
              f"block={b2['block_id']}")
        check("同段第 2 次会话", b2["sessions"] == 2)
        check("额度接着扣，没重新发满", b2["played_seconds"] > b["played_seconds"],
              f"played={b2['played_seconds']:.1f}s")

        print("\n[3] 玩满剩余额度 → 到点强杀")
        deadline = time.time() + max(0.0, left) + 6
        while time.time() < deadline and block(conn, g.id)["running"]:
            time.sleep(1)
        b3 = block(conn, g.id)
        check("已被终止", not b3["running"])
        check("本段累计 ≈30 秒 = 上限", CAP_SECONDS - 4 <= b3["played_seconds"] <= CAP_SECONDS + 4,
              f"played={b3['played_seconds']:.1f}s")
        killed = conn.execute(
            "SELECT detail FROM events WHERE type='killed' ORDER BY id DESC LIMIT 1").fetchone()
        check("终止原因 = 时长到点", killed and killed["detail"] == "session_timeout",
              killed["detail"] if killed else "无 killed 事件")
        check("额度耗尽 → 这一段结束",
              not rules.block_alive(b3, g.session_minutes, time.time(),
                                    config.IDLE_GRACE_MINUTES))

        print("\n[4] 再想开 —— 这次该被冷却挡住了")
        kill_notepad()
        time.sleep(1)
        start_notepad()
        time.sleep(4)
        blocked = conn.execute(
            "SELECT detail FROM events WHERE type='blocked' ORDER BY id DESC LIMIT 1").fetchone()
        check("被拦截且原因是冷却", blocked and blocked["detail"].startswith("cooldown"),
              blocked["detail"][:40] if blocked else "无 blocked 事件")
        check("notepad 已被终止", not block(conn, g.id)["running"])
    finally:
        kill_notepad()
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()

    print("\n--- 会话明细 ---")
    for r in conn.execute("SELECT * FROM sessions ORDER BY id"):
        print(f"  会话{r['id']} 段{db.block_of(r)}  真实游玩 {db.session_played(r):.1f}s  "
              f"{r['end_reason']}")
    print("\n" + ("e2e_block: 全部通过" if not fails else f"e2e_block: {len(fails)} 项失败 → {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
