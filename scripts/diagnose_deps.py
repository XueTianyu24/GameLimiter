"""诊断 conda C 扩展(.pyd)的 DLL 依赖树，找出打包需手动收集的非系统 DLL。

conda 的 Python DLL 布局非标准（标准库扩展的依赖 DLL——ffi.dll / sqlite3.dll /
libssl 等——放 Library\bin，且 ffi 无版本号），PyInstaller 逐个漏收集 → exe 换机
即 "DLL load failed while importing _ctypes/_sqlite3/..."（开发机能借系统 PATH，
干净机器全缺）。本脚本用 pefile 递归读 **所有** .pyd 的 import 闭包，收集需带的
环境 DLL，杜绝打地鼠。

跑法：python scripts/diagnose_deps.py
"""

import sys
from pathlib import Path

import pefile

ENV = Path(sys.executable).parent
SEARCH = [ENV, ENV / "DLLs", ENV / "Library" / "bin", ENV / "Library" / "mingw-w64" / "bin"]

# 系统自带 / PyInstaller 必收集的，无需手动
SYSTEM_PREFIX = ("api-ms-win", "kernel32", "ntdll", "user32", "msvcrt", "advapi32",
                 "ole32", "shell32", "ws2_32", "rpcrt4", "gdi32", "combase",
                 "python3", "vcruntime", "ucrtbase")


def find_dll(name: str):
    for d in SEARCH:
        p = d / name
        if p.exists():
            return p
    return None


def deps(pe_path: Path) -> list[str]:
    pe = pefile.PE(str(pe_path), fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    out = []
    for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        out.append(e.dll.decode().lower())
    pe.close()
    return out


def walk(roots, _state=None):
    """从一个或多个 .pyd/.dll 入口，递归收集非系统依赖 DLL 的定位。"""
    if isinstance(roots, (str, Path)):
        roots = [roots]
    seen, need_collect, missing = set(), {}, set()
    stack = [Path(r) for r in roots]
    while stack:
        cur = stack.pop()
        try:
            cur_deps = deps(cur)
        except Exception:
            continue
        for dep in cur_deps:
            if dep in seen:
                continue
            seen.add(dep)
            if dep.startswith(SYSTEM_PREFIX):
                continue
            loc = find_dll(dep)
            if loc:
                need_collect[dep] = loc
                stack.append(loc)
            else:
                missing.add(dep)
    return need_collect, missing


def extension_pyds() -> list[Path]:
    """环境里所有 C 扩展 .pyd（标准库 DLLs + 关键第三方包）的入口。"""
    roots = list((ENV / "DLLs").glob("*.pyd"))
    sp = ENV / "Lib" / "site-packages"
    for pkg in ("psutil", "win32", "win32com", "pythoncom", "pywin32_system32"):
        roots += list((sp / pkg).glob("*.pyd")) + list((sp / pkg).glob("*.dll"))
    # pywin32 的 pythoncomXX.dll / pywintypesXX.dll 常在 site-packages 根
    roots += list(sp.glob("pywintypes*.dll")) + list(sp.glob("pythoncom*.dll"))
    return [p for p in roots if p.exists()]


def collect_all():
    return walk(extension_pyds())


def main():
    roots = extension_pyds()
    print(f"环境 {ENV}\n扫描 {len(roots)} 个 C 扩展的依赖闭包\n")
    need, missing = collect_all()
    print(f"需收集的环境 DLL（{len(need)} 个）：")
    for name, loc in sorted(need.items()):
        print(f"  {name:28} {loc}")
    if missing:
        print("\n未在环境内定位到（视为系统 DLL）：")
        print("  " + ", ".join(sorted(missing)))


if __name__ == "__main__":
    main()
