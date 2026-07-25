"""图标提取 + 手写 PNG 编码器的单测。

PNG 是自己用 zlib 编的（为省掉 Pillow 依赖），所以要验到字节结构：块长度、
CRC、IHDR 参数、像素能还原——编错了浏览器只会显示一个破图，不会报错。
"""

import struct
import sys
import zlib
from base64 import b64decode
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamelimiter.icons import _png, extract_icon  # noqa: E402

SYS_EXE = r"C:\Windows\System32\notepad.exe"


def parse_png(data: bytes) -> list[tuple[bytes, bytes]]:
    """拆成 [(块名, 数据)]，顺便校验每块 CRC。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG magic 不对"
    chunks, i = [], 8
    while i < len(data):
        (n,) = struct.unpack(">I", data[i:i + 4])
        tag, body = data[i + 4:i + 8], data[i + 8:i + 8 + n]
        (crc,) = struct.unpack(">I", data[i + 8 + n:i + 12 + n])
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF, f"{tag!r} 块 CRC 不匹配"
        chunks.append((tag, body))
        i += 12 + n
    return chunks


def test_png_encoder():
    w, h = 3, 2
    rgba = bytes([255, 0, 0, 255, 0, 255, 0, 128, 0, 0, 255, 0] * h)
    chunks = parse_png(_png(w, h, rgba))
    assert [c[0] for c in chunks] == [b"IHDR", b"IDAT", b"IEND"], "块顺序/组成不对"
    iw, ih, depth, color, comp, filt, inter = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (iw, ih, depth, color) == (w, h, 8, 6), "IHDR 尺寸/位深/色彩类型不对"
    assert (comp, filt, inter) == (0, 0, 0), "压缩/滤波/隔行参数不对"
    raw = zlib.decompress(chunks[1][1])
    assert len(raw) == h * (1 + w * 4), "扫描行长度不对（每行应多 1 字节滤波标志）"
    assert all(raw[y * (1 + w * 4)] == 0 for y in range(h)), "滤波标志应为 0"
    back = b"".join(raw[y * (1 + w * 4) + 1:(y + 1) * (1 + w * 4)] for y in range(h))
    assert back == rgba, "解码像素与原始不一致"


def test_extract_real_exe():
    uri = extract_icon(SYS_EXE)
    assert uri and uri.startswith("data:image/png;base64,"), "系统 exe 应能提取到图标"
    png = b64decode(uri.split(",", 1)[1])
    chunks = parse_png(png)
    w, h = struct.unpack(">II", chunks[0][1][:8])
    assert w == h and 16 <= w <= 256, f"图标尺寸异常：{w}x{h}"
    rgba = zlib.decompress(chunks[1][1])
    assert any(rgba[4::4]), "整张全透明，alpha 处理有问题"


def test_bad_input_returns_none():
    for bad in [None, "", r"C:\这个文件不存在.exe", r"C:\Windows", __file__]:
        assert extract_icon(bad) is None, f"{bad!r} 应返回 None 而不是抛异常"


if __name__ == "__main__":
    test_png_encoder()
    test_extract_real_exe()
    test_bad_input_returns_none()
    print("test_icons: 全部通过")
