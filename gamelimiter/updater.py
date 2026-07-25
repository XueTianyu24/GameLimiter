"""在线更新：GitHub Releases 检查 / 下载 / 原地换 exe。

流程（GUI 触发，仅打包 exe 支持一键更新）：
1. check_latest()：查最新 Release 并与本地版本比较（启动时后台静默查，失败不打扰）
2. download()：下载新 exe 到旧 exe 同目录 GameLimiter.new.exe（带进度回调）
3. verify_exe()：跑新 exe --selftest，防半截下载 / 损坏文件顶上去
4. GUI 发起 UAC：新 exe --apply-update "<旧exe路径>"，随后 GUI 自身退出
5. apply_update()（在新 exe 内、管理员权限）：停自愈任务 → 杀旧 exe 全部进程
   （守护/watchdog/GUI）→ 旧 exe 改名 .old 留作回退 → 自己改名顶上（Windows
   允许重命名运行中的 exe）→ 恢复任务并拉起守护 → 启动新 GUI

设计约束：计划任务 /TR 指向 exe 绝对路径，原地换文件后任务不需重配。
更新过程写 update.log 到 exe 同目录（提权进程无控制台，出错靠它排查）。
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .version import __version__

REPO = "XueTianyu24/GameLimiter"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPO}/releases/latest"

_NOWIN = subprocess.CREATE_NO_WINDOW


def _ver_tuple(s: str):
    try:
        return tuple(int(x) for x in s.strip().lstrip("vV").split(".")[:3])
    except ValueError:
        return None


def check_latest(timeout: float = 8.0):
    """查最新 Release。有新版返回 dict，无新版返回 None；网络失败抛异常。"""
    req = urllib.request.Request(API_LATEST, headers={
        "User-Agent": f"GameLimiter/{__version__}",
        "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    latest, cur = _ver_tuple(data.get("tag_name", "")), _ver_tuple(__version__)
    if not latest or not cur or latest <= cur:
        return None
    asset = next((a for a in data.get("assets", [])
                  if a.get("name", "").lower().endswith(".exe")), None)
    return {
        "tag": data.get("tag_name", ""),
        "notes": data.get("body") or "",
        "page": data.get("html_url") or RELEASE_PAGE,
        "asset_url": asset["browser_download_url"] if asset else None,
        "asset_size": asset["size"] if asset else 0,
    }


def download(url: str, dest: Path, progress_cb=None, timeout: float = 30.0):
    """流式下载到 dest（先写 .part 再改名）。progress_cb(done, total)。"""
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={
        "User-Agent": f"GameLimiter/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(256 * 1024):
            f.write(chunk)
            done += len(chunk)
            if progress_cb:
                progress_cb(done, total)
    if total and part.stat().st_size != total:
        part.unlink(missing_ok=True)
        raise OSError(f"下载不完整（{part.stat().st_size}/{total}）")
    dest.unlink(missing_ok=True)
    part.rename(dest)


def verify_exe(path: Path, timeout: float = 240.0) -> bool:
    """跑新 exe --selftest 验证完整可用（onefile 首次解包慢，超时给足）。"""
    try:
        r = subprocess.run([str(path), "--selftest"], capture_output=True,
                           text=True, timeout=timeout, creationflags=_NOWIN)
        return r.returncode == 0 and "selftest OK" in (r.stdout or "")
    except (OSError, subprocess.TimeoutExpired):
        return False


# ---------------- --apply-update（新 exe 内、管理员） ----------------


def _log(f, msg: str):
    f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    f.flush()


def _schtasks(*args):
    from .setup_system import SCHTASKS
    return subprocess.run([SCHTASKS, *args], capture_output=True, text=True,
                          errors="ignore", creationflags=_NOWIN)


def apply_update(target_str: str) -> int:
    """把自己（新 exe）顶替到 target 路径。返回退出码。"""
    from .winutil import is_frozen
    if not is_frozen():
        print("--apply-update 仅打包 exe 支持（开发环境 sys.executable 是 python）")
        return 1
    target = Path(target_str)
    me = Path(sys.executable)
    with open(target.parent / "update.log", "a", encoding="utf-8") as log:
        _log(log, f"== apply-update {__version__}: {me.name} -> {target.name}")
        try:
            from .setup_system import TASK_DAEMON, TASK_HEAL
            # 1 停自愈任务，防换文件窗口内复活守护
            for tn in (TASK_HEAL, TASK_DAEMON):
                _schtasks("/Change", "/TN", tn, "/DISABLE")
            # 2 杀所有从旧 exe 跑的进程（守护/watchdog/GUI；提权后 SYSTEM 也杀得动）
            import psutil
            for p in psutil.process_iter(["exe"]):
                try:
                    if p.info["exe"] and Path(p.info["exe"]) == target:
                        p.kill()
                        _log(log, f"killed pid={p.pid}")
                except psutil.Error:
                    continue
            # 3 换文件：旧改名 .old 留回退，自己顶上（重试等锁释放）
            old = target.with_name(target.stem + ".old.exe")
            try:
                old.unlink(missing_ok=True)   # 清上一轮的回退文件
            except OSError:
                old = target.with_name(target.stem + f".old{int(time.time())}.exe")
            for i in range(15):
                try:
                    target.rename(old)
                    break
                except OSError:
                    time.sleep(1.0)
            else:
                _log(log, "FAIL: 旧 exe 一直被占用，放弃")
                return 1
            me.rename(target)
            _log(log, "swapped OK")
            # 4 恢复任务并拉起守护；未配置强制层则直接起守护进程
            from .setup_system import is_configured
            configured = is_configured()
            if configured:
                for tn in (TASK_DAEMON, TASK_HEAL):
                    _schtasks("/Change", "/TN", tn, "/ENABLE")
                _schtasks("/Run", "/TN", TASK_DAEMON)
            else:
                subprocess.Popen([str(target), "--daemon"],
                                 creationflags=subprocess.DETACHED_PROCESS | _NOWIN)
            # 5 启动新 GUI
            subprocess.Popen([str(target)],
                             creationflags=subprocess.DETACHED_PROCESS | _NOWIN)
            _log(log, "done")
            return 0
        except Exception as e:                      # noqa: BLE001 全兜底进日志
            _log(log, f"FAIL: {type(e).__name__}: {e}")
            return 1
