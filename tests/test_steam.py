"""Steam 库解析单元测试（合成夹具，不依赖本机 Steam）。

跑法：conda run -n gamelimiter python tests/test_steam.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gamelimiter import steam

root = Path(tempfile.mkdtemp()) / "Steam"
lib2 = Path(tempfile.mkdtemp()) / "SteamLibrary"

# 主库 + libraryfolders.vdf 指向第二库
(root / "steamapps").mkdir(parents=True)
(lib2 / "steamapps" / "common").mkdir(parents=True)
(root / "steamapps" / "libraryfolders.vdf").write_text(
    f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{str(root).replace(chr(92), chr(92)*2)}"\n\t}}\n'
    f'\t"1"\n\t{{\n\t\t"path"\t\t"{str(lib2).replace(chr(92), chr(92)*2)}"\n\t}}\n}}\n',
    encoding="utf-8")

# 第二库里装了"幻兽帕鲁"（UE 双层 exe 结构 + 干扰项）
game = lib2 / "steamapps" / "common" / "Palworld"
(game / "Pal" / "Binaries" / "Win64").mkdir(parents=True)
(game / "Engine" / "Extras" / "Redist" / "en-us").mkdir(parents=True)
(game / "Palworld.exe").write_bytes(b"x" * 100)                                   # 小启动器
(game / "Pal" / "Binaries" / "Win64" / "Palworld-Win64-Shipping.exe").write_bytes(b"x" * 9000)
(game / "Pal" / "Binaries" / "Win64" / "CrashReportClient.exe").write_bytes(b"x" * 5000)
(game / "Engine" / "Extras" / "Redist" / "en-us" / "vcredist_x64.exe").write_bytes(b"x" * 8000)
(lib2 / "steamapps" / "appmanifest_1623730.acf").write_text(
    '"AppState"\n{\n\t"appid"\t\t"1623730"\n\t"name"\t\t"Palworld"\n'
    '\t"installdir"\t\t"Palworld"\n}\n', encoding="utf-8")

# .url 图标解析
url = root / "Palworld.url"
url.write_text("[InternetShortcut]\nURL=steam://rungameid/1623730\nIconIndex=0\n",
               encoding="utf-8")
assert steam.parse_url_shortcut(str(url)) == 1623730
url2 = root / "NonSteam.url"
url2.write_text("[InternetShortcut]\nURL=steam://rungameid/12345678901234567890\n",
                encoding="utf-8")
assert steam.parse_url_shortcut(str(url2)) is None       # 非 Steam 游戏伪 id
url3 = root / "Web.url"
url3.write_text("[InternetShortcut]\nURL=https://example.com\n", encoding="utf-8")
assert steam.parse_url_shortcut(str(url3)) is None

# 库枚举 + 游戏定位（跨库）
libs = steam.library_dirs(root)
assert root in libs and lib2 in libs, libs
found = steam.find_game(1623730, root)
assert found and found[0] == "Palworld" and found[1] == game, found
assert steam.find_game(999999, root) is None

# exe 候选：Shipping 排第一，干扰项被剔除
cands = steam.candidate_exes(game)
names = [p.name for p in cands]
assert names[0] == "Palworld-Win64-Shipping.exe", names
assert "CrashReportClient.exe" not in names and "vcredist_x64.exe" not in names, names
assert "Palworld.exe" in names   # 启动器保留为次选

print("test_steam: 全部通过")
