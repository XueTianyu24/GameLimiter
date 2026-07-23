"""统一入口：单个 GameLimiter.exe 按参数分角色。

  （无参数）        GUI 仪表盘
  --daemon          守护进程
  --watchdog        watchdog（守护进程自动拉起）
  --setup-system    配置强制层（SYSTEM 计划任务，需管理员）
  --remove-system   移除强制层（需管理员）
  --stop-daemon     停止 watchdog + 守护（调试用；SYSTEM 化后需管理员才杀得动）
  --cli ...         命令行管理透传，如 GameLimiter.exe --cli list
  --selftest        自检：加载所有关键原生依赖后退出（打包后验证自包含用）
"""

import sys


def _selftest():
    """加载全部原生扩展依赖，验证打包 exe 在干净机器上自包含。

    覆盖历史踩过的坑：conda 的 _ctypes 依赖 ffi.dll 漏收集时，import ctypes 就炸。
    """
    import ctypes  # noqa: F401  最易漏的（_ctypes → ffi.dll）
    import sqlite3  # noqa: F401
    import psutil  # noqa: F401
    from nicegui import ui  # noqa: F401  拉起 nicegui.native → ctypes 全链
    from . import changes, cli, daemon, db, gui, rules, setup_system, steam, watchdog  # noqa: F401
    print("selftest OK")


def _stop_daemon():
    import psutil
    me = psutil.Process().pid
    marks = ("--watchdog", "--daemon", "gamelimiter.daemon", "daemon_start")
    victims = {"--watchdog": [], "--daemon": []}
    for p in psutil.process_iter(["cmdline", "exe"]):
        try:
            if p.pid == me:
                continue
            cl = " ".join(p.info["cmdline"] or [])
            if not any(m in cl for m in marks):
                continue
            if "GameLimiter" not in cl and "gamelimiter" not in cl:
                continue
            victims["--watchdog" if "--watchdog" in cl else "--daemon"].append(p)
        except psutil.Error:
            continue
    for group in ("--watchdog", "--daemon"):    # 先杀 watchdog 防复活
        for p in victims[group]:
            try:
                p.kill()
                print(f"stopped {group} pid={p.pid}")
            except psutil.Error as e:
                print(f"kill pid={p.pid} 失败：{e}")


def main():
    args = sys.argv[1:]
    if "--daemon" in args:
        from .daemon import main as m
        m()
    elif "--watchdog" in args:
        from .watchdog import main as m
        m()
    elif "--setup-system" in args:
        from .setup_system import setup
        sys.exit(0 if setup() else 1)
    elif "--remove-system" in args:
        from .setup_system import remove
        sys.exit(0 if remove() else 1)
    elif "--selftest" in args:
        _selftest()
    elif "--stop-daemon" in args:
        _stop_daemon()
    elif "--cli" in args:
        sys.argv = [sys.argv[0]] + args[args.index("--cli") + 1:]
        from .cli import main as m
        m()
    else:
        from .gui import main as m
        m()


if __name__ in {"__main__", "__mp_main__"}:
    main()
