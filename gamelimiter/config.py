"""全局配置：数据目录、轮询参数。

数据放 ProgramData（守护进程 Phase 2 提权 SYSTEM 后仍与 GUI 共享同一路径），
不可写时退回 LOCALAPPDATA。
"""

import os
from pathlib import Path
from typing import Optional


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


def resolve_capture_dir(out_dir: Optional[str], default_sub: str) -> tuple[Path, Optional[str]]:
    """采集数据的落脚目录：优先用户指定的，用不了就回落 `DATA_DIR/<default_sub>`。

    返回 (目录, 回落原因)；回落原因非 None = 用户指定的那个目录没法用。

    **实际执笔的是 SYSTEM 身份的守护进程**，不是选目录的那个 GUI：用户级映射的网络盘
    SYSTEM 根本看不见，OneDrive 之类也可能拒写。这种时候宁可把数据写回默认目录，
    也不能让一次采集因为目录问题整个泡汤——数据采回来了才有得谈。
    """
    reason = None
    if out_dir:
        d = Path(out_dir)
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".gl_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return d, None
        except OSError as e:
            reason = f"{e.__class__.__name__}: {e}"
    d = DATA_DIR / default_sub
    d.mkdir(parents=True, exist_ok=True)
    return d, reason

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
