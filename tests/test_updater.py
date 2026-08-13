"""在线更新换文件那一步的失败回滚。

`apply_update` 为了防止换文件窗口内守护复活，**第一步就把计划任务停了**。所以它的每一条
失败出口都必须把任务恢复回去——否则机器停在"计划任务已停 + 守护已杀"的中间态，
用户只看到"更新失败"，不会知道**强制层也一起关着了**，而且没人会去恢复。

这些路径手工没法验（要管理员 + 打包 exe + 制造文件占用），只能在这里钉住。

跑法：conda run -n gamelimiter python tests/test_updater.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gamelimiter import config

_TMP = Path(tempfile.mkdtemp())
config.DB_PATH = _TMP / "test.db"

from gamelimiter import setup_system, updater, winutil

TASKS = (setup_system.TASK_DAEMON, setup_system.TASK_HEAL)


class _R:
    def __init__(self, rc=0):
        self.returncode, self.stdout, self.stderr = rc, "", ""


calls = []


def fake_schtasks(*args):
    calls.append(args)
    return _R(0)


class _FakeSubprocess:
    """换完文件那步会真的去启动新 exe；测试里的"exe"是个文本文件，拦下来记一笔就好。"""
    import subprocess as _sp
    DETACHED_PROCESS, CREATE_NO_WINDOW = _sp.DETACHED_PROCESS, _sp.CREATE_NO_WINDOW
    launched = []

    @staticmethod
    def Popen(cmd, **kw):
        _FakeSubprocess.launched.append(cmd)


# 换文件失败要重试 15 次 × 1 秒，测试里不等
updater.time.sleep = lambda s: None
updater._schtasks = fake_schtasks
updater.subprocess = _FakeSubprocess
winutil.is_frozen = lambda: True
setup_system.is_configured = lambda: True

_real_rename = Path.rename
_fail = set()


def fake_rename(self, dst):
    if str(self) in _fail:
        raise OSError(32, "另一个程序正在使用此文件")
    return _real_rename(self, dst)


Path.rename = fake_rename


def setup(name: str):
    """造一对 exe：target = 装着的旧版，me = 下载来的新版。"""
    d = _TMP / name
    d.mkdir(parents=True, exist_ok=True)
    target, me = d / "GameLimiter.exe", d / "GameLimiter.new.exe"
    target.write_text("old", encoding="utf-8")
    me.write_text("new", encoding="utf-8")
    sys.executable = str(me)          # apply_update 用它认"我是谁"
    calls.clear()
    _fail.clear()
    _FakeSubprocess.launched.clear()
    return target, me


def did(verb: str, tn: str) -> bool:
    return any(a[:2] == ("/Change", "/TN") and a[2] == tn and a[3] == verb for a in calls)


def log_of(target: Path) -> str:
    return (target.parent / "update.log").read_text(encoding="utf-8")


# ---- 1) 正常路径：换成功 → 恢复任务 + 拉守护 -------------------------------
target, me = setup("ok")
rc = updater.apply_update(str(target))
assert rc == 0, rc
assert target.read_text(encoding="utf-8") == "new", "新版没顶上去"
assert target.with_name("GameLimiter.old.exe").read_text(encoding="utf-8") == "old", "旧版没留作回退"
assert did("/DISABLE", setup_system.TASK_DAEMON) and did("/ENABLE", setup_system.TASK_DAEMON)
assert ("/Run", "/TN", setup_system.TASK_DAEMON) in calls, "没把守护拉起来"
assert [str(target)] in _FakeSubprocess.launched, "没启动新版 GUI"
assert "swapped OK" in log_of(target) and "done" in log_of(target)

# ---- 2) 旧 exe 一直被占用：旧版原地不动，但任务必须恢复 ---------------------
target, me = setup("locked")
_fail.add(str(target))                       # target.rename(old) 一直失败
rc = updater.apply_update(str(target))
assert rc == 1, rc
assert target.read_text(encoding="utf-8") == "old", "旧版不该被动过"
assert me.exists(), "新版下载文件不该丢"
for tn in TASKS:
    assert did("/ENABLE", tn), f"{tn} 没恢复 —— 强制层会一直关着"
assert ("/Run", "/TN", setup_system.TASK_DAEMON) in calls, "没把守护拉回来"
assert "回滚" in log_of(target)

# ---- 3) 最坏的一格：旧版已改名、新版没顶上 → 必须把旧版放回 -----------------
target, me = setup("half")
_fail.add(str(me))                           # me.rename(target) 失败
rc = updater.apply_update(str(target))
assert rc == 1, rc
assert target.exists(), "计划任务指向的路径空了 —— 守护再也起不来"
assert target.read_text(encoding="utf-8") == "old", "旧版没被还原"
assert not target.with_name("GameLimiter.old.exe").exists(), ".old 该被还原回去，不该留着"
for tn in TASKS:
    assert did("/ENABLE", tn), f"{tn} 没恢复"
assert "旧 exe 已还原" in log_of(target)

# ---- 4) 中途抛异常：一样要恢复 ---------------------------------------------
target, me = setup("boom")
_boom = setup_system.is_configured
setup_system.is_configured = lambda: (_ for _ in ()).throw(RuntimeError("炸了"))
try:
    rc = updater.apply_update(str(target))
finally:
    setup_system.is_configured = _boom
assert rc == 1, rc
assert "FAIL" in log_of(target)
# is_configured 自己就是炸点，回滚里也会撞上它 → 至少要如实记进日志，不能静默吞掉
assert "回滚异常" in log_of(target) or "回滚" in log_of(target)

# ---- 5) 没配强制层的机器：没有任务可恢复，也不该报错 -----------------------
target, me = setup("noforce")
setup_system.is_configured = lambda: False
_fail.add(str(target))
rc = updater.apply_update(str(target))
setup_system.is_configured = lambda: True
assert rc == 1, rc
assert not any(a[:1] == ("/Run",) for a in calls), "没配强制层却去拉计划任务"

Path.rename = _real_rename
print("test_updater: 全部通过")
