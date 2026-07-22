"""强制层配置：SYSTEM 计划任务（开机自启 + 每分钟自愈）。需管理员权限运行。

- 守护进程以 SYSTEM 跑：普通权限任务管理器杀不掉、schtasks 删不掉（都要过 UAC）
- 每分钟自愈任务重复拉起守护（单实例互斥体保证不重复跑），杀掉也 1 分钟内复活
- GUI「初始化本机」按钮经 UAC 调用 --setup-system 到这里

用法（管理员）：GameLimiter.exe --setup-system / --remove-system
"""

import subprocess
import sys
import winreg

from .winutil import is_frozen, project_root

TASK_DAEMON = "GameLimiter-Daemon"
TASK_HEAL = "GameLimiter-Heal"


def _daemon_tr() -> str:
    """计划任务的启动命令（/TR 参数值）。"""
    if is_frozen():
        return f'"{sys.executable}" --daemon'
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    launcher = project_root() / "scripts" / "daemon_start.pyw"
    return f'"{pythonw}" "{launcher}"'


def _schtasks(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True,
                          creationflags=subprocess.CREATE_NO_WINDOW)


def is_configured() -> bool:
    return _schtasks("/Query", "/TN", TASK_DAEMON).returncode == 0


def setup() -> bool:
    tr = _daemon_tr()
    r1 = _schtasks("/Create", "/F", "/TN", TASK_DAEMON, "/TR", tr,
                   "/SC", "ONSTART", "/RU", "SYSTEM", "/RL", "HIGHEST")
    r2 = _schtasks("/Create", "/F", "/TN", TASK_HEAL, "/TR", tr,
                   "/SC", "MINUTE", "/MO", "1", "/RU", "SYSTEM", "/RL", "HIGHEST")
    if r1.returncode or r2.returncode:
        print("创建计划任务失败：", r1.stderr or r1.stdout, r2.stderr or r2.stdout)
        return False
    # 退役 Phase 1 的 HKCU Run 键（如有）
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, "GameLimiterDaemon")
    except FileNotFoundError:
        pass
    _schtasks("/Run", "/TN", TASK_DAEMON)   # 立即拉起 SYSTEM 守护
    print("强制层已配置：SYSTEM 守护自启 + 每分钟自愈")
    return True


def remove() -> bool:
    r1 = _schtasks("/Delete", "/F", "/TN", TASK_DAEMON)
    r2 = _schtasks("/Delete", "/F", "/TN", TASK_HEAL)
    ok = not (r1.returncode and r2.returncode)
    print("已移除强制层计划任务" if ok else f"移除失败：{r1.stderr}{r2.stderr}")
    return ok
