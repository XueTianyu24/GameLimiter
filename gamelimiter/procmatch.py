"""进程识别：认出"正在跑的这个进程是不是那款受限游戏"。

三道识别，由便宜到贵，命中即止：

1. **exe 文件名** —— 登记时记下的那个名字（绝大多数情况走这条，行为同 v0.17.0 之前）
2. **exe 全路径** —— 文件被改了名，但还在原来的位置
3. **文件指纹**（大小 + 首尾各 1 MB 的 sha256）—— 连目录一起复制到别处再改名

后两条是给"冲动上来改个文件名就能开"堵路的：以前只比文件名，复制一份改成
`a.exe` 就完全脱管。诚实边界照旧——自己是管理员，真要绕总有办法（改登记、换台机器），
这里堵的是**最短的那条路**，而且绕过动作会被记下来（events 表 `renamed` + 弹窗告知）。

开销：`process_iter(["name", "exe"])` 实测比只取 name 只贵 0.1 ms（400 进程的机器上
两者都约 1.5 ms），所以路径匹配每轮全量做，不必缓存 pid。指纹那条只在
"名字和路径都没命中、且大小恰好等于某款受限游戏"时才真去读文件，稳态下等于不跑；
读到的大小/指纹按路径缓存，同一个 exe 只算一次。
"""

import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import psutil

log = logging.getLogger("gamelimiter.procmatch")

CHUNK = 1 << 20                    # 指纹取首尾各 1 MB
MIN_FINGERPRINT_SIZE = 4 << 20     # 小于 4 MB 的不做指纹识别：小文件撞大小的概率高，也不像游戏本体
_WINDIR = os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows"))


def _norm(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        return os.path.normcase(os.path.abspath(path))
    except OSError:
        return None


def fingerprint(path: str) -> tuple[Optional[int], Optional[str]]:
    """(字节数, 指纹)；读不到返回 (None, None)。

    指纹 = 首 1 MB + 尾 1 MB + 大小 的 sha256。不整文件哈希是因为游戏本体动辄几百 MB，
    而首尾两段加大小已经足够把"同一个 exe 的副本"和"另一个碰巧一样大的 exe"分开。
    """
    try:
        size = os.path.getsize(path)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            h.update(f.read(CHUNK))
            if size > 2 * CHUNK:
                f.seek(-CHUNK, os.SEEK_END)
                h.update(f.read(CHUNK))
        h.update(str(size).encode())
        return size, h.hexdigest()
    except OSError as e:
        log.debug("取指纹失败 %s：%s", path, e)
        return None, None


@dataclass
class Match:
    """一款游戏这一轮扫到的进程。"""
    procs: list = field(default_factory=list)
    kind: str = "name"            # name（文件名命中）/ path（改了名）/ fingerprint（复制走再改名）
    alias: Optional[str] = None   # 非 name 命中时，实际跑的是什么（进程名 + 路径），用于告警


class Matcher:
    """按名字 / 路径 / 指纹匹配进程。缓存跨轮复用，守护进程持有一个实例。"""

    def __init__(self):
        self._size: dict[str, Optional[int]] = {}
        self._hash: dict[str, Optional[str]] = {}

    def _size_of(self, path: str) -> Optional[int]:
        if path not in self._size:
            try:
                self._size[path] = os.path.getsize(path)
            except OSError:
                self._size[path] = None
        return self._size[path]

    def _hash_of(self, path: str) -> Optional[str]:
        if path not in self._hash:
            self._hash[path] = fingerprint(path)[1]
        return self._hash[path]

    def scan(self, games) -> dict[int, Match]:
        """一次遍历进程表，返回 {game_id: Match}（只含扫到进程的游戏）。"""
        by_name: dict[str, object] = {}
        by_path: dict[str, object] = {}
        by_size: dict[int, list] = {}
        for g in games:
            by_name.setdefault(g.exe_name.lower(), g)
            p = _norm(g.exe_path)
            if p:
                by_path.setdefault(p, g)
            if g.exe_size and g.exe_hash and g.exe_size >= MIN_FINGERPRINT_SIZE:
                by_size.setdefault(g.exe_size, []).append(g)

        out: dict[int, Match] = {}
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                name = (proc.info["name"] or "").lower()
                exe = proc.info["exe"]
            except psutil.Error:
                continue
            g = by_name.get(name)
            kind = "name"
            if g is None:
                path = _norm(exe)
                # 系统目录里的东西一律不当游戏：省掉几百次 stat，也免得误伤系统进程
                if not path or path.startswith(_WINDIR):
                    continue
                g = by_path.get(path)
                kind = "path"
                if g is None and by_size:
                    cands = by_size.get(self._size_of(path) or -1)
                    if not cands:
                        continue
                    digest = self._hash_of(path)
                    g = next((c for c in cands if c.exe_hash == digest), None)
                    kind = "fingerprint"
                if g is None:
                    continue
            m = out.get(g.id)
            if m is None:
                m = out[g.id] = Match()
            m.procs.append(proc)
            # 一款游戏同时被多条命中时，报最可疑的那条（改名/搬移优先于正常命中）
            if kind != "name" and m.kind == "name":
                m.kind = kind
                m.alias = f"{proc.info['name']}（{exe}）"
        return out


def backfill(conn, games, setter) -> int:
    """给还没有指纹的游戏补上（登记于 v0.18.0 之前的老数据）。返回补了几个。

    只对 exe_path 还指得到文件的游戏补；补不上就算了（下次再试，代价只是少一道识别）。
    """
    n = 0
    for g in games:
        if g.exe_hash or not g.exe_path:
            continue
        size, digest = fingerprint(g.exe_path)
        if digest:
            setter(conn, g.id, size, digest)
            g.exe_size, g.exe_hash = size, digest
            n += 1
    return n
