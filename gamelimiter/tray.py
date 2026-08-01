"""托盘图标（用户身份的独立小进程，`GameLimiter.exe --tray`）。

**为什么不挂在守护进程上**：强制层启用后守护以 SYSTEM 跑在 Session 0，那里
的托盘图标在用户桌面上根本不显示。所以托盘单独一个用户身份进程，只做"看"和
"打开面板"，杀掉它不影响任何限制——限制永远由守护 + 计划任务保证。

零依赖实现（pystray 要拉 Pillow，为一个图标不值当）：注册隐藏窗口收托盘消息
+ Shell_NotifyIcon + 弹出菜单，跑 Win32 消息循环。
"""

import ctypes
import sys
from ctypes import wintypes
from datetime import date

from .winutil import DAEMON_MUTEX, hold_mutex, mutex_exists, spawn_detached

_user32 = ctypes.windll.user32
_shell32 = ctypes.windll.shell32
_kernel32 = ctypes.windll.kernel32

TRAY_MUTEX = "Global\\GameLimiterTray"
GUI_MUTEX = "Global\\GameLimiterGui"

_WM_APP = 0x8000
WM_TRAYICON = _WM_APP + 1
WM_DESTROY, WM_COMMAND, WM_TIMER = 0x0002, 0x0111, 0x0113
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
IDI_APPLICATION = 32512
MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0000, 0x0800, 0x0001
TPM_RIGHTBUTTON, TPM_BOTTOMALIGN = 0x0002, 0x0020

ID_OPEN, ID_QUIT = 1, 2
TIP_TIMER, TIP_INTERVAL_MS = 1, 30_000

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSEX(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class _NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


# 句柄进出必须逐个声明类型，否则 64 位下被截成 32 位（USAGE 坑 6）：
# 实测漏声明 CreateWindowExW 就报 "argument 11: OverflowError"（hInstance 那位）
_user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEX)]
_user32.RegisterClassExW.restype = wintypes.ATOM
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = ctypes.c_ssize_t
_user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
_user32.LoadIconW.restype = wintypes.HICON
_user32.CreatePopupMenu.restype = wintypes.HMENU
_user32.DestroyMenu.argtypes = [wintypes.HMENU]
_user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
_user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
_user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT]
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATA)]
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def _playing_text(conn) -> str:
    """正在游玩的那款 + 剩余时间（本次额度已算进去）；没在玩返回空串。"""
    import time

    from . import db, rules
    now = time.time()
    games = {g.id: g for g in db.list_games(conn)}
    for row in db.open_sessions(conn):
        g = games.get(row["game_id"])
        if not g:
            continue
        block = db.current_block(conn, g.id)
        dl = rules.session_deadline(g, now, block["played_seconds"] if block else 0.0,
                                    row["limit_minutes"])
        return (f"{g.name} 剩 {max(0, (dl[0] - now) / 60):.0f} 分钟" if dl
                else f"{g.name} 游玩中")
    return ""


def _status_text() -> str:
    """托盘 tooltip / 菜单首行：游玩中剩余 + 今日已玩 + 守护状态。"""
    playing = ""
    try:
        from . import db, stats
        conn = db.connect()
        today = date.today()
        s = stats.summary(conn, today, today)
        playing = _playing_text(conn)
        conn.close()
        played = f"今日已玩 {s.hours_text}" if s.minutes else "今日还没玩"
    except Exception:                                    # noqa: BLE001
        played = "今日数据读取失败"
    head = f"{playing}｜" if playing else ""
    return f"{head}{played}｜守护{'运行中' if mutex_exists(DAEMON_MUTEX) else '未运行'}"


def _open_panel():
    """打开 GUI 面板；已经开着就不重复拉（native 窗口重复启动会端口冲突）。"""
    if mutex_exists(GUI_MUTEX):
        return
    spawn_detached()          # 无参数 = GUI 角色


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "GameLimiterTray"


def ensure_autostart() -> bool:
    """把托盘登记到 HKCU Run（当前用户登录即起）。仅打包 exe 下生效。

    走 HKCU 而不是强制层的 SYSTEM 计划任务：托盘要显示在用户桌面，且它是纯查看
    工具——用户想删这个自启随时可以，删了也不影响限制。
    """
    from .winutil import is_frozen
    if not is_frozen():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, f'"{sys.executable}" --tray')
        return True
    except OSError:
        return False


def ensure_running() -> bool:
    """托盘不在就拉起（幂等，互斥体保证不重复）。返回是否发起了启动。"""
    if mutex_exists(TRAY_MUTEX):
        return False
    spawn_detached("--tray")
    return True


class Tray:
    def __init__(self):
        self.hwnd = None
        self.nid = None
        self._proc = WNDPROC(self._wndproc)   # 必须持引用，否则回调被 GC 掉后崩溃

    # ---- Win32 ----

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                _open_panel()
            elif lparam == WM_RBUTTONUP:
                self._popup_menu()
        elif msg == WM_COMMAND:
            cmd = wparam & 0xFFFF
            if cmd == ID_OPEN:
                _open_panel()
            elif cmd == ID_QUIT:
                _user32.DestroyWindow(hwnd)
        elif msg == WM_TIMER and wparam == TIP_TIMER:
            self._update_tip()
        elif msg == WM_DESTROY:
            self._remove_icon()
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup_menu(self):
        menu = _user32.CreatePopupMenu()
        _user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, _status_text())
        _user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        _user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "打开面板")
        _user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "退出托盘（不影响限制）")
        pt = _POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        # 不置前台的话菜单点外面不消失（Win32 老规矩）
        _user32.SetForegroundWindow(self.hwnd)
        _user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
                               pt.x, pt.y, 0, self.hwnd, None)
        _user32.PostMessageW(self.hwnd, 0, 0, 0)   # 收尾，避免菜单残留
        _user32.DestroyMenu(menu)

    def _app_icon(self) -> wintypes.HICON:
        """用应用自己的图标（打包后 sys.executable 就是本 exe）；拿不到退系统默认。"""
        try:
            from .icons import _extract_hicon
            h = _extract_hicon(sys.executable, 32)
            if h:
                return h
        except Exception:                                # noqa: BLE001
            pass
        return _user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))

    def _make_nid(self, flags: int) -> _NOTIFYICONDATA:
        nid = _NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAYICON
        return nid

    def _update_tip(self):
        nid = self._make_nid(NIF_TIP)
        nid.szTip = f"GameLimiter — {_status_text()}"[:127]
        _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _remove_icon(self):
        if self.nid is not None:
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            self.nid = None

    def run(self) -> int:
        if not hold_mutex(TRAY_MUTEX):
            print("托盘已在运行")
            return 0
        hinst = _kernel32.GetModuleHandleW(None)
        cls = _WNDCLASSEX()
        cls.cbSize = ctypes.sizeof(_WNDCLASSEX)
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.lpszClassName = "GameLimiterTrayWnd"
        if not _user32.RegisterClassExW(ctypes.byref(cls)):
            print("注册窗口类失败")
            return 1
        self.hwnd = _user32.CreateWindowExW(0, cls.lpszClassName, "GameLimiter",
                                            0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            print("创建隐藏窗口失败")
            return 1

        nid = self._make_nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.hIcon = self._app_icon()
        nid.szTip = f"GameLimiter — {_status_text()}"[:127]
        if not _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            print("添加托盘图标失败")
            return 1
        self.nid = nid
        _user32.SetTimer(self.hwnd, TIP_TIMER, TIP_INTERVAL_MS, None)

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        return 0


def main() -> int:
    try:
        return Tray().run()
    except Exception as e:                               # noqa: BLE001
        print(f"托盘启动失败：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
