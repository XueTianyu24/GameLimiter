"""Windows 工具：互斥体、自我重启命令、分离进程。"""

import ctypes
import subprocess
import sys
from pathlib import Path

DAEMON_MUTEX = "Global\\GameLimiterDaemon"
WATCHDOG_MUTEX = "Global\\GameLimiterWatchdog"

_SYNCHRONIZE = 0x00100000
_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5


def hold_mutex(name: str) -> bool:
    """创建并持有命名互斥体；已存在返回 False（用于单实例）。"""
    ctypes.windll.kernel32.CreateMutexW(None, False, name)
    return ctypes.windll.kernel32.GetLastError() != _ERROR_ALREADY_EXISTS


def mutex_exists(name: str) -> bool:
    """探测命名互斥体是否存在（跨身份）。

    SYSTEM 守护创建的全局互斥体，默认安全描述符不给普通用户 SYNCHRONIZE：
    OpenMutexW 报 ACCESS_DENIED——但拒绝访问恰恰证明对象存在（不存在报
    FILE_NOT_FOUND），按存在处理，否则 GUI 会把 SYSTEM 守护误判为未运行。
    """
    h = ctypes.windll.kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return ctypes.windll.kernel32.GetLastError() == _ERROR_ACCESS_DENIED


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def self_cmd(*args) -> list[str]:
    """以当前形态（exe / 开发环境）重新调起本应用的命令行。"""
    if is_frozen():
        return [sys.executable, *args]
    return [sys.executable, "-m", "gamelimiter.app", *args]


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def spawn_detached(*args):
    subprocess.Popen(
        self_cmd(*args),
        cwd=None if is_frozen() else project_root(),
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)


def run_elevated(*args, file: str | None = None) -> bool:
    """UAC 提权运行本应用（一键配置/更新用）。返回是否成功发起。

    file 指定要提权的 exe（在线更新时提权的是新下载的 exe）；缺省为当前形态。
    """
    quoted = " ".join(f'"{a}"' if " " in a else a for a in args)
    if file is not None:
        target, params, cwd = file, quoted, None
    elif is_frozen():
        target, params, cwd = sys.executable, quoted, None
    else:
        target = sys.executable
        params = "-m gamelimiter.app " + quoted
        cwd = str(project_root())
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", target, params, cwd, 1)
    return r > 32
