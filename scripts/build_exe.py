"""打包单 exe：自动收集 _ctypes 的真实 DLL 依赖，避免换机 DLL 缺失。

固化正确的打包命令（历史坑：conda 的 _ctypes 链接 ffi.dll 而非 ffi-8.dll，
手敲 --add-binary 易加错名字，开发机能跑、台式机崩）。本脚本用 diagnose_deps
自动按真实 import 名收集，杜绝人为错配。

跑法：python scripts/build_exe.py [--debug]
  默认出 dist/GameLimiter.exe（--windowed 无控制台）
  --debug 出 dist/GameLimiterDbg.exe（有控制台看 traceback）
打包后务必跑最小 PATH 自检：见 USAGE「打包单 exe」。
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from diagnose_deps import ENV, collect_all   # 复用依赖分析

ROOT = Path(__file__).resolve().parent.parent
NICEGUI_DATA = ENV / "Lib" / "site-packages" / "nicegui"


def main():
    debug = "--debug" in sys.argv
    name = "GameLimiterDbg" if debug else "GameLimiter"

    need, missing = collect_all()
    print(f"自动收集 {len(need)} 个 C 扩展依赖 DLL：{', '.join(sorted(need))}")

    cmd = ["pyinstaller", "--noconfirm", "--onefile", "--name", name,
           "--add-data", f"{NICEGUI_DATA};nicegui"]
    if not debug:
        cmd.append("--windowed")
    for loc in sorted(set(need.values())):
        cmd += ["--add-binary", f"{loc};."]
    cmd.append(str(ROOT / "app.py"))

    print("\n" + " ".join(cmd) + "\n")
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode == 0:
        print(f"\n完成：dist/{name}.exe")
        print(f"验证：最小 PATH 下跑 dist/{name}.exe --selftest 应打印 'selftest OK'")
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
