"""弹窗通知：零依赖、置顶、自动关闭，不阻塞守护循环。

- 与活动用户同会话：MessageBoxTimeoutW
- SYSTEM/会话不同（Phase 2 提权后守护跑在会话 0，MessageBox 用户看不见）：
  WTSSendMessageW 发到活动控制台会话
置 GAMELIMITER_SILENT=1 可静默（自动化测试用）。
"""

import ctypes
import os
import threading

from .config import POPUP_TIMEOUT_MS

MB_ICONWARNING = 0x30
MB_ICONINFORMATION = 0x40
MB_SYSTEMMODAL = 0x1000
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000


def _same_session_as_user() -> bool:
    k32 = ctypes.windll.kernel32
    sid = ctypes.c_ulong()
    k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid))
    return sid.value == k32.WTSGetActiveConsoleSessionId()


def popup(title: str, text: str, warn: bool = True, timeout_ms: int = POPUP_TIMEOUT_MS):
    if os.environ.get("GAMELIMITER_SILENT") == "1":
        return
    icon = MB_ICONWARNING if warn else MB_ICONINFORMATION

    def _show():
        try:
            if _same_session_as_user():
                flags = icon | MB_SYSTEMMODAL | MB_SETFOREGROUND | MB_TOPMOST
                ctypes.windll.user32.MessageBoxTimeoutW(0, text, title, flags, 0, timeout_ms)
            else:
                sid = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
                resp = ctypes.c_ulong(0)
                ctypes.windll.wtsapi32.WTSSendMessageW(
                    None, sid, title, len(title) * 2, text, len(text) * 2,
                    icon, timeout_ms // 1000, ctypes.byref(resp), False)
        except Exception:
            pass

    threading.Thread(target=_show, daemon=True).start()
