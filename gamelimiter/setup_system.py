"""强制层配置：SYSTEM 计划任务（开机自启 + 每分钟自愈）。需管理员权限运行。

- 守护进程以 SYSTEM 跑：普通权限任务管理器杀不掉、schtasks 删不掉（都要过 UAC）
- 每分钟自愈任务重复拉起守护（单实例互斥体保证不重复跑），杀掉也 1 分钟内复活
- GUI「初始化本机」按钮经 UAC 调用 --setup-system 到这里

用法（管理员）：GameLimiter.exe --setup-system
拆除走 `--remove-system`，但**只是提交申请**：与停用游戏/放宽规则同一条 24 小时队列，
到期后由守护进程调本模块的 `remove_now()` 落地（见 changes.request_remove_system）。
"""

import os
import subprocess
import sys
import winreg

from .winutil import is_frozen, project_root

TASK_DAEMON = "GameLimiter-Daemon"
TASK_HEAL = "GameLimiter-Heal"

# 绝对路径调用：进程环境的 PATH 未必含 System32（计划任务/提权上下文继承来的
# 环境可能被裁剪），靠 PATH 找 schtasks 会抛 FileNotFoundError
SCHTASKS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                        "System32", "schtasks.exe")


def _daemon_tr() -> str:
    """计划任务的启动命令（/TR 参数值）。"""
    if is_frozen():
        return f'"{sys.executable}" --daemon'
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    launcher = project_root() / "scripts" / "daemon_start.pyw"
    return f'"{pythonw}" "{launcher}"'


def _schtasks(*args) -> subprocess.CompletedProcess:
    return subprocess.run([SCHTASKS, *args], capture_output=True, text=True,
                          errors="ignore", creationflags=subprocess.CREATE_NO_WINDOW)


_DENIED_MARKS = ("拒绝访问", "denied")   # 中英文系统各一


def is_configured() -> bool:
    """强制层是否已配置（SYSTEM 守护任务是否存在）。

    普通权限查询 SYSTEM 建的任务会被 ACL 挡回「错误: 拒绝访问。」——同
    winutil.mutex_exists，拒绝访问恰恰证明任务存在（不存在报「系统找不到指定
    的文件」），按已配置处理。否则台式机重启后 GUI 会把跑得好好的强制层报成
    未配置、诱导重复初始化（v0.7.4 实测踩坑）。
    """
    try:
        r = _schtasks("/Query", "/TN", TASK_DAEMON)
    except OSError:      # schtasks 都调不起来，无从判定
        return False
    if r.returncode == 0:
        return True
    out = ((r.stderr or "") + (r.stdout or "")).lower()
    return any(m in out for m in _DENIED_MARKS)


USERS_SID = "*S-1-5-32-545"   # BUILTIN\Users 的 SID 直写，不受系统语言影响


def grant_users_write() -> bool:
    """数据目录（含已有文件）授予 Users 修改权，幂等、尽力而为。

    ProgramData 默认 ACL 下文件只有创建者可写：SYSTEM 守护先创建的 daemon.log /
    SQLite -wal 会把用户身份的 GUI / 手动守护锁在门外（PermissionError，台式机
    实测踩坑）。SYSTEM/管理员调用可修复已有文件；普通用户调用至少让目录上的
    继承规则生效（此后新建文件对 Users 可写）。
    """
    from . import config
    r = subprocess.run(["icacls", str(config.DATA_DIR), "/grant",
                        f"{USERS_SID}:(OI)(CI)M", "/T", "/C", "/Q"],
                       capture_output=True, text=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return r.returncode == 0


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
    grant_users_write()   # SYSTEM 将创建 log/db 文件，先保证用户身份也写得动
    _schtasks("/Run", "/TN", TASK_DAEMON)   # 立即拉起 SYSTEM 守护
    print("强制层已配置：SYSTEM 守护自启 + 每分钟自愈")
    return True


def remove_now() -> bool:
    """真的删掉两个计划任务。**不要直接调**——拆强制层要走 24h 冷静期，
    入口是 `changes.request_remove_system`，到期后由守护进程（SYSTEM 身份）调到这里。
    """
    r1 = _schtasks("/Delete", "/F", "/TN", TASK_DAEMON)
    r2 = _schtasks("/Delete", "/F", "/TN", TASK_HEAL)
    ok = not (r1.returncode and r2.returncode)
    print("已移除强制层计划任务" if ok else f"移除失败：{r1.stderr}{r2.stderr}")
    return ok
