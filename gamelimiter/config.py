"""全局配置：数据目录、轮询参数。

数据放 ProgramData（守护进程 Phase 2 提权 SYSTEM 后仍与 GUI 共享同一路径），
不可写时退回 LOCALAPPDATA。
"""

import os
from pathlib import Path


def _data_dir() -> Path:
    d = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "GameLimiter"
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return d
    except OSError:
        d = Path(os.environ["LOCALAPPDATA"]) / "GameLimiter"
        d.mkdir(parents=True, exist_ok=True)
        return d


DATA_DIR = _data_dir()
DB_PATH = DATA_DIR / "gamelimiter.db"
LOG_PATH = DATA_DIR / "daemon.log"

POLL_INTERVAL = 1.0          # psutil 轮询间隔（秒）
RULE_RELOAD_INTERVAL = 5.0   # 从 SQLite 重载规则的间隔（秒）
WARN_MINUTES = (10, 5, 1)    # 到点前的多级预警时点（分钟）
POPUP_TIMEOUT_MS = 15_000    # 弹窗自动关闭时间
RELAX_DELAY_HOURS = 24.0     # 规则放宽延迟生效（收紧立即）；防冲动核心
WATCHDOG_INTERVAL = 5.0      # watchdog 检查间隔（秒）

# 一段游玩（block）：退出后这么久内再打开算接着玩同一段——额度接着用、不重新发、
# 不查冷却。超过则这一段作废，冷却从最后退出时刻起算
IDLE_GRACE_MINUTES = 60.0
# 单次心跳最多计入的游玩秒数。守护崩掉/机器睡眠的空窗期没人观测到进程在跑，
# 不能算成"你在玩"——cap 掉那段，这是"按真实进程时间计时"的关键
HEARTBEAT_MAX_GAP = 3.0
