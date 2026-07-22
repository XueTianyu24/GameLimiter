"""Watchdog：与守护进程互保。守护死了拉活守护；守护每轮 tick 也会拉活 watchdog。

运行：GameLimiter.exe --watchdog（由守护进程自动拉起，无需手动）
"""

import logging
import time

from .config import DATA_DIR, WATCHDOG_INTERVAL
from .winutil import DAEMON_MUTEX, WATCHDOG_MUTEX, hold_mutex, mutex_exists, spawn_detached

log = logging.getLogger("gamelimiter.watchdog")


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(DATA_DIR / "watchdog.log", encoding="utf-8")])
    if not hold_mutex(WATCHDOG_MUTEX):
        return
    log.info("watchdog 启动")
    while True:
        if not mutex_exists(DAEMON_MUTEX):
            log.warning("守护进程不在，重新拉起")
            spawn_detached("--daemon")
            time.sleep(3)   # 给它起身时间，避免连环 spawn
        time.sleep(WATCHDOG_INTERVAL)
