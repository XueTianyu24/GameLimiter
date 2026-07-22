"""开机自启注册/卸载（Phase 1 普通权限版：HKCU Run 键，无需管理员）。

Phase 2 强制层会升级为 SYSTEM 计划任务，届时本脚本的 Run 键方式退役。

用法：
  python scripts/autostart.py install
  python scripts/autostart.py uninstall
  python scripts/autostart.py status
"""

import sys
import winreg
from pathlib import Path

KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "GameLimiterDaemon"
PYTHONW = Path(sys.executable).parent / "pythonw.exe"
LAUNCHER = Path(__file__).resolve().parent / "daemon_start.pyw"
CMD = f'"{PYTHONW}" "{LAUNCHER}"'


def install():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, CMD)
    print(f"已注册开机自启：{CMD}")


def uninstall():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, NAME)
        print("已取消开机自启")
    except FileNotFoundError:
        print("未注册，无需取消")


def status():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
            val, _ = winreg.QueryValueEx(k, NAME)
        print(f"已注册：{val}")
    except FileNotFoundError:
        print("未注册")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"install": install, "uninstall": uninstall, "status": status}[action]()
