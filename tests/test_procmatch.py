"""进程识别单测：名字 / 路径 / 指纹三道识别 + 指纹补齐。

跑法：conda 环境的 python tests/test_procmatch.py
进程表用假进程注入（monkeypatch process_iter），文件是真的临时文件——指纹要真读盘。
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gamelimiter import config

config.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"   # 隔离测试库（须在 db.connect 前）

from gamelimiter import db, procmatch

TMP = Path(tempfile.mkdtemp())
BIG = 5 << 20        # 5 MB：超过 MIN_FINGERPRINT_SIZE，才走指纹那道


def make_exe(name: str, size: int = BIG, seed: bytes = b"A") -> Path:
    p = TMP / name
    p.write_bytes(seed * 64 + os.urandom(0) + b"\0" * (size - 64))
    return p


class FakeProc:
    """只提供 procmatch 用到的那点接口。"""

    def __init__(self, name, exe=None, pid=1000):
        self.info = {"name": name, "exe": exe}
        self.pid = pid


def scan(matcher, games, procs):
    procmatch.psutil.process_iter = lambda attrs=None: iter(procs)
    return matcher.scan(games)


real_iter = procmatch.psutil.process_iter

# ---- 指纹本身 ----
game_exe = make_exe("RealGame.exe")
size, digest = procmatch.fingerprint(str(game_exe))
assert size == BIG and len(digest) == 64
assert procmatch.fingerprint(str(game_exe)) == (size, digest)          # 稳定可重复

copied = TMP / "totally_innocent.exe"
shutil.copy(game_exe, copied)
assert procmatch.fingerprint(str(copied)) == (size, digest)            # 副本同指纹

other = make_exe("Other.exe", seed=b"B")
assert procmatch.fingerprint(str(other))[1] != digest                  # 内容不同 → 指纹不同
assert procmatch.fingerprint(str(TMP / "nope.exe")) == (None, None)    # 读不到不抛

# ---- 三道识别 ----
conn = db.connect()
g = db.upsert_game(conn, "测试游戏", "RealGame.exe", exe_path=str(game_exe))
assert procmatch.backfill(conn, [g], db.set_exe_fingerprint) == 1
g = db.get_game(conn, "RealGame.exe")
assert g.exe_size == BIG and g.exe_hash == digest
assert procmatch.backfill(conn, [g], db.set_exe_fingerprint) == 0      # 已有指纹不重算

m = procmatch.Matcher()

# 1) 文件名命中（老行为）
res = scan(m, [g], [FakeProc("RealGame.exe", str(game_exe))])
assert set(res) == {g.id} and res[g.id].kind == "name" and res[g.id].alias is None

# 名字命中时不看路径：从别处启动的同名 exe 照样算这款游戏
res = scan(m, [g], [FakeProc("realgame.exe", r"D:\somewhere\else\realgame.exe")])
assert res[g.id].kind == "name"

# 2) 改了名但还在原地 → 路径命中
renamed = TMP / "notagame.exe"
shutil.copy(game_exe, renamed)      # 只为让路径存在；识别看的是登记的 exe_path
res = scan(m, [g], [FakeProc("notagame.exe", str(game_exe))])
assert res[g.id].kind == "path" and "notagame.exe" in res[g.id].alias

# 3) 连目录一起复制走再改名 → 指纹命中
res = scan(m, [g], [FakeProc("totally_innocent.exe", str(copied))])
assert res[g.id].kind == "fingerprint", res[g.id]
assert "totally_innocent.exe" in res[g.id].alias

# 4) 内容不同的 exe 不会被误认（大小相同也要指纹对得上）
same_size_other = make_exe("SameSize.exe", seed=b"C")
assert os.path.getsize(same_size_other) == g.exe_size
res = scan(m, [g], [FakeProc("SameSize.exe", str(same_size_other))])
assert res == {}, res

# 5) 系统目录一律不当游戏（免得误伤系统进程，也省掉几百次 stat）
res = scan(m, [g], [FakeProc("svchost.exe", os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"), "System32", "svchost.exe"))])
assert res == {}

# 6) 拿不到路径的进程只能靠名字（psutil 权限不够时 exe 为 None）
res = scan(m, [g], [FakeProc("mystery.exe", None)])
assert res == {}
res = scan(m, [g], [FakeProc("RealGame.exe", None)])
assert res[g.id].kind == "name"

# 7) 小于 4 MB 的不做指纹识别：小文件撞大小太容易，也不像游戏本体
small = make_exe("Small.exe", size=1 << 20, seed=b"D")
gs = db.upsert_game(conn, "小工具", "Small.exe", exe_path=str(small))
procmatch.backfill(conn, [gs], db.set_exe_fingerprint)
gs = db.get_game(conn, "Small.exe")
assert gs.exe_hash                                            # 指纹照样记下来了
small_copy = TMP / "small_renamed.exe"
shutil.copy(small, small_copy)
assert scan(m, [gs], [FakeProc("small_renamed.exe", str(small_copy))]) == {}

# 8) 同一款游戏被多条命中时，报最可疑的那条
res = scan(m, [g], [FakeProc("RealGame.exe", str(game_exe), pid=1),
                    FakeProc("totally_innocent.exe", str(copied), pid=2)])
assert len(res[g.id].procs) == 2 and res[g.id].kind == "fingerprint"

# 9) 没有 exe_path 的游戏（手动只填了进程名）仍然靠名字工作
gn = db.upsert_game(conn, "无路径", "NoPath.exe")
assert scan(m, [gn], [FakeProc("NoPath.exe", r"E:\x\NoPath.exe")])
assert procmatch.backfill(conn, [gn], db.set_exe_fingerprint) == 0     # 没路径就补不了，不报错

procmatch.psutil.process_iter = real_iter
print("test_procmatch: 全部通过")
