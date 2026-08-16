"""端到端验证全局规则 e（每天游玩总时长）：真守护 + 真进程（notepad 当假游戏），隔离库。

压缩版：总额 40 秒（= 生产的几小时），游戏本身**一条规则都不设**——
所以任何终止/拦截都只可能来自规则 e，不会跟单次时长/冷却混淆。

  [1] 预先塞 25 秒的历史游玩（模拟"今天已经玩过别的游戏"）
  [2] 开玩 → 应当只剩 15 秒，到点被终止，理由 daily_minutes
  [3] 再开 → 总额已用完，启动即被拦截（今天玩过的这款也照拦）

跑法：conda 环境的 python tests/e2e_daily.py
⚠ 必须用 PowerShell 工具跑（Bash 工具里起不来 notepad，见 USAGE 测试技巧）
（约 40 秒；数据写 .tmp/ 下的隔离库，不碰 C:\\ProgramData）
"""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp" / "e2e_daily_data"
TMP.mkdir(parents=True, exist_ok=True)
os.environ["ProgramData"] = str(TMP)          # 隔离库（大小写必须一致，见 USAGE 坑 8）
os.environ["GAMELIMITER_SILENT"] = "1"
os.environ["GAMELIMITER_NO_WATCHDOG"] = "1"
sys.path.insert(0, str(ROOT))

from gamelimiter import db, config, rules      # noqa: E402

PY = sys.executable
BUDGET_SECONDS = 40        # 今天的总额
SEEDED_SECONDS = 25        # 开测前就已经玩掉的
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


def main():
    print(f"DB: {config.DB_PATH}")
    kill_notepad()
    conn = db.connect()
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM events")
    conn.commit()
    # 假游戏本身零规则：能拦能杀的只可能是规则 e
    g = db.upsert_game(conn, "假游戏", "notepad.exe")
    other = db.upsert_game(conn, "别的游戏", "e2e_other.exe")
    db.set_daily_minutes(conn, BUDGET_SECONDS / 60)

    print(f"\n[1] 预置：今天已经玩掉 {SEEDED_SECONDS} 秒（另一款游戏）")
    now = time.time()
    sid = db.open_session(conn, other.id, int(now - 120))
    db.heartbeat(conn, sid, SEEDED_SECONDS, now - 120 + SEEDED_SECONDS)
    db.close_session(conn, sid, int(now - 120 + SEEDED_SECONDS), "self_exit")
    used = db.daily_used_seconds(conn)
    check("今日已玩累加到位", abs(used - SEEDED_SECONDS) < 2, f"used={used:.1f}s")
    left0 = db.daily_remaining_seconds(conn)
    check(f"剩余额度 ≈{BUDGET_SECONDS - SEEDED_SECONDS} 秒",
          abs(left0 - (BUDGET_SECONDS - SEEDED_SECONDS)) < 2, f"left={left0:.1f}s")

    daemon = subprocess.Popen([PY, "-m", "gamelimiter.daemon"], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)

        print("\n[2] 开玩 —— 该在剩余额度用完时被终止（而不是玩满整个总额）")
        t0 = time.time()
        start_notepad()
        time.sleep(3)
        b = db.current_block(conn, g.id)
        check("会话已开始（总额还有剩，不该拦）", b and b["running"])

        deadline = t0 + (BUDGET_SECONDS - SEEDED_SECONDS) + 15
        while time.time() < deadline and db.current_block(conn, g.id)["running"]:
            time.sleep(1)
        elapsed = time.time() - t0
        b2 = db.current_block(conn, g.id)
        check("已被终止", not b2["running"])
        check(f"存活 ≈{BUDGET_SECONDS - SEEDED_SECONDS} 秒（不是整个 {BUDGET_SECONDS} 秒）",
              abs(elapsed - (BUDGET_SECONDS - SEEDED_SECONDS)) < 8, f"{elapsed:.1f}s")
        killed = conn.execute(
            "SELECT detail FROM events WHERE type='killed' ORDER BY id DESC LIMIT 1").fetchone()
        check("终止原因 = 今日总时长用完", killed and killed["detail"] == "daily_minutes",
              killed["detail"] if killed else "无 killed 事件")
        warned = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE type='warn'").fetchone()["c"]
        check("终止前有预警（PVP 不能无预警强杀）", warned >= 1, f"{warned} 条")

        print("\n[3] 再开 —— 总额用完，今天玩过的这款也该被拦")
        kill_notepad()
        time.sleep(1)
        start_notepad()
        time.sleep(4)
        blocked = conn.execute(
            "SELECT detail FROM events WHERE type='blocked' ORDER BY id DESC LIMIT 1").fetchone()
        check("被拦截且原因是总时长", blocked and blocked["detail"].startswith("daily_minutes"),
              blocked["detail"][:50] if blocked else "无 blocked 事件")
        check("notepad 已被终止", not db.current_block(conn, g.id)["running"])
        v = rules.check_daily_minutes(db.get_daily_minutes(conn),
                                      db.daily_used_seconds(conn), time.time())
        check("解锁时刻 = 明天 0:00", v.unlock_ts == rules.day_bounds(time.time())[1])
    finally:
        kill_notepad()
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()

    print("\n--- 会话明细 ---")
    for r in conn.execute("SELECT * FROM sessions ORDER BY id"):
        print(f"  会话{r['id']}  真实游玩 {db.session_played(r):.1f}s  {r['end_reason']}")
    print(f"  今日合计 {db.daily_used_seconds(conn):.1f}s / 总额 {BUDGET_SECONDS}s")
    print("\n" + ("e2e_daily: 全部通过" if not fails else f"e2e_daily: {len(fails)} 项失败 → {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
