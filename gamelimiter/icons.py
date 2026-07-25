"""从 exe 提取图标 → PNG data URI（零第三方依赖）。

Win32 取图标位图（PrivateExtractIcons 优先，可指定 64×64 比 ExtractIconEx 的
32×32 在高分屏更清晰），再用 zlib 手写 PNG 编码——避免为几 KB 的图标引入
Pillow 让 onefile exe 平白涨 4MB（每次在线更新都是全量下载）。

结果直接进 DB 的 games.icon 列（data URI 文本，<img src> 可直接用），不落地
成文件：省掉 ProgramData 的 ACL 麻烦（v0.7.1 踩过）和 NiceGUI 静态目录配置。
"""

import base64
import ctypes
import struct
import zlib
from ctypes import wintypes

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

ICON_SIZE = 64          # 提取尺寸；GUI 显示 ~36px，高分屏放大仍清晰
_BI_RGB = 0
_DIB_RGB_COLORS = 0


class _ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_shell32 = ctypes.windll.shell32

# 必须显式声明：64 位下 ctypes 默认按 c_int 传参/收返回值，句柄会被截断成 32 位
# （表现为 GetObjectW 报 "int too long to convert"、或拿到无效 HDC 静默失败）
_user32.PrivateExtractIconsW.argtypes = [
    wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.UINT),
    wintypes.UINT, wintypes.DWORD]
_user32.PrivateExtractIconsW.restype = wintypes.UINT
_shell32.ExtractIconExW.argtypes = [
    wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.HICON),
    ctypes.POINTER(wintypes.HICON), wintypes.UINT]
_shell32.ExtractIconExW.restype = wintypes.UINT
_user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(_ICONINFO)]
_user32.GetIconInfo.restype = wintypes.BOOL
_user32.DestroyIcon.argtypes = [wintypes.HICON]
_user32.DestroyIcon.restype = wintypes.BOOL
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int
_gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
_gdi32.GetObjectW.restype = ctypes.c_int
_gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                             ctypes.c_void_p, ctypes.POINTER(_BITMAPINFOHEADER), wintypes.UINT]
_gdi32.GetDIBits.restype = ctypes.c_int
_gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
_gdi32.DeleteObject.restype = wintypes.BOOL


def _extract_hicon(path: str, size: int):
    """取一个 HICON；调用方负责 DestroyIcon。"""
    hicon = wintypes.HICON()
    # 未文档化但 Win95 起稳定存在，唯一能指定任意尺寸的取图标 API
    n = _user32.PrivateExtractIconsW(ctypes.c_wchar_p(path), 0, size, size,
                                     ctypes.byref(hicon), None, 1, 0)
    if n == 1 and hicon:
        return hicon
    large = wintypes.HICON()
    if ctypes.windll.shell32.ExtractIconExW(ctypes.c_wchar_p(path), 0,
                                            ctypes.byref(large), None, 1) == 1:
        return large if large else None
    return None


def _bitmap_bgra(hbm) -> tuple[int, int, bytes] | None:
    """HBITMAP → (宽, 高, BGRA 字节, top-down)。"""
    bm = _BITMAP()
    if not _gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm)):
        return None
    w, h = bm.bmWidth, bm.bmHeight
    if w <= 0 or h <= 0:
        return None
    hdr = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=w,
                            biHeight=-h,        # 负 = top-down，省得自己翻行
                            biPlanes=1, biBitCount=32, biCompression=_BI_RGB)
    buf = ctypes.create_string_buffer(w * h * 4)
    hdc = _user32.GetDC(None)
    try:
        got = _gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(hdr), _DIB_RGB_COLORS)
    finally:
        _user32.ReleaseDC(None, hdc)
    return (w, h, buf.raw) if got else None


def _rgba_from_icon(hicon) -> tuple[int, int, bytes] | None:
    ii = _ICONINFO()
    if not _user32.GetIconInfo(hicon, ctypes.byref(ii)):
        return None
    try:
        color = _bitmap_bgra(ii.hbmColor) if ii.hbmColor else None
        if color is None:
            return None
        w, h, bgra = color
        px = bytearray(bgra)
        px[0::4], px[2::4] = px[2::4], px[0::4]     # BGRA → RGBA
        if not any(px[3::4]):
            # 老式图标（XP 前）无 alpha 通道：掩码位图 1=透明，据此补
            mask = _bitmap_bgra(ii.hbmMask) if ii.hbmMask else None
            if mask and mask[0] == w:
                mp = mask[2]
                for i in range(w * h):
                    px[i * 4 + 3] = 0 if mp[i * 4] else 255
            else:
                px[3::4] = b"\xff" * (w * h)
        return w, h, bytes(px)
    finally:
        for hbm in (ii.hbmColor, ii.hbmMask):
            if hbm:
                _gdi32.DeleteObject(hbm)


def _png(w: int, h: int, rgba: bytes) -> bytes:
    """最小 PNG 编码器：8 位 RGBA、无滤波（图标小，压缩率差异可忽略）。"""
    stride = w * 4
    raw = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride] for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def extract_icon(exe_path: str | None, size: int = ICON_SIZE) -> str | None:
    """exe 路径 → PNG data URI；取不到（路径失效/无图标资源）返回 None。

    整条链路吞异常：图标纯属锦上添花，任何环节出岔都不该挡住添加游戏。
    """
    if not exe_path:
        return None
    try:
        hicon = _extract_hicon(exe_path, size)
        if not hicon:
            return None
        try:
            got = _rgba_from_icon(hicon)
        finally:
            _user32.DestroyIcon(hicon)
        if not got:
            return None
        w, h, rgba = got
        return "data:image/png;base64," + base64.b64encode(_png(w, h, rgba)).decode()
    except Exception:                              # noqa: BLE001
        return None
