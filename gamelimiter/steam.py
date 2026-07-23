"""Steam 库解析：桌面 .url 图标 → appid → 本机安装目录 → 游戏 exe 候选。

Steam 桌面图标是 INI 格式的 .url 文件（URL=steam://rungameid/<appid>），
不是 .lnk，解析链路：注册表找 Steam 根 → libraryfolders.vdf 列全部库 →
appmanifest_<appid>.acf 取游戏名与 installdir → 目录下挑真实 exe。
"""

import re
import winreg
from pathlib import Path
from typing import Optional

# 排除明显不是游戏本体的 exe / 目录
_JUNK_NAME = ("crashhandler", "crashreport", "easyanticheat", "vcredist", "dxsetup",
              "dotnetfx", "oalinst", "setup", "unins", "install", "redist")
_JUNK_DIR = ("_commonredist", "easyanticheat", "directx", "redist", "vcredist")


def steam_root() -> Optional[Path]:
    for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
        try:
            with winreg.OpenKey(hive, key) as k:
                val, _ = winreg.QueryValueEx(
                    k, "SteamPath" if hive == winreg.HKEY_CURRENT_USER else "InstallPath")
            p = Path(val)
            if p.exists():
                return p
        except OSError:
            continue
    return None


def library_dirs(root: Optional[Path] = None) -> list[Path]:
    root = root or steam_root()
    if not root:
        return []
    libs = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        text = vdf.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = Path(m.group(1).replace("\\\\", "\\"))
            if p.exists() and p not in libs:
                libs.append(p)
    return libs


def parse_url_shortcut(path: str) -> Optional[int]:
    """.url 文件 → appid；非 steam 链接或非 Steam 商店游戏（64 位伪 id）返回 None。"""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = re.search(r"URL=steam://rungameid/(\d+)", text)
    if not m:
        return None
    appid = int(m.group(1))
    return appid if appid < 2**32 else None   # 超 32 位 = "非 Steam 游戏"快捷方式


def find_game(appid: int, root: Optional[Path] = None) -> Optional[tuple[str, Path]]:
    """在所有 Steam 库中找 appid，返回 (游戏名, 安装目录)。"""
    for lib in library_dirs(root):
        acf = lib / "steamapps" / f"appmanifest_{appid}.acf"
        if not acf.exists():
            continue
        text = acf.read_text(encoding="utf-8", errors="ignore")
        name = re.search(r'"name"\s+"([^"]+)"', text)
        installdir = re.search(r'"installdir"\s+"([^"]+)"', text)
        if not installdir:
            continue
        d = lib / "steamapps" / "common" / installdir.group(1)
        if d.exists():
            return (name.group(1) if name else installdir.group(1), d)
    return None


def candidate_exes(install_dir: Path) -> list[Path]:
    """安装目录下的游戏 exe 候选，最可能的排最前。

    排序：UE Shipping 版（真实长驻进程）> 与目录同名 > 体积大者。
    """
    dirname = install_dir.name.lower().replace(" ", "")
    cands = []
    for p in install_dir.rglob("*.exe"):
        if len(p.relative_to(install_dir).parts) > 4:
            continue
        low = p.name.lower()
        if any(j in low for j in _JUNK_NAME):
            continue
        if any(j in part.lower() for part in p.parent.parts for j in _JUNK_DIR):
            continue
        cands.append(p)

    def key(p: Path):
        low = p.name.lower()
        return (0 if "-win64-shipping" in low or "-shipping" in low else 1,
                0 if low.removesuffix(".exe").replace(" ", "") == dirname else 1,
                -p.stat().st_size)
    return sorted(cands, key=key)
