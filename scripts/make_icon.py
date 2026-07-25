"""生成应用图标 assets/app.ico（+ 预览 PNG），零第三方依赖。

用有符号距离场（SDF）解析式抗锯齿：每个尺寸独立渲染而不是缩放大图，16px 下
也不会糊成一团。图案 = 明亮青蓝渐变圆角方块 + 白色游戏手柄，手柄上的十字键与
按钮是"挖空"的（露出底色渐变），小尺寸自动省略这些细节。

跑法：python scripts/make_icon.py        产物 assets/app.ico + assets/app_preview.png
改配色/构图后重跑即可；ico 进 git，打包时 build_exe.py 用 --icon 引用。
"""

import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gamelimiter.icons import _png                      # 复用手写 PNG 编码器  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_ICO = ROOT / "assets" / "app.ico"
OUT_PNG = ROOT / "assets" / "app_preview.png"

SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
DETAIL_MIN = 32          # 小于此尺寸省略十字键/按钮细节

TOP = (0x38, 0xBD, 0xF8)         # sky-400
BOTTOM = (0x02, 0x84, 0xC7)      # sky-600
WHITE = (0xFF, 0xFF, 0xFF)


def _rr(px: float, py: float, hw: float, hh: float, r: float) -> float:
    """圆角矩形 SDF（中心在原点）。"""
    qx, qy = abs(px) - hw + r, abs(py) - hh + r
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def _circle(px: float, py: float, cx: float, cy: float, r: float) -> float:
    return math.hypot(px - cx, py - cy) - r


def _gamepad(px: float, py: float, bold: bool = False) -> float:
    """手柄轮廓 SDF：横胶囊主体 + 左右下方两个握把圆，取并集。

    bold 用于 16-24px：细瘦造型在那个尺寸只剩 2-3 像素高，握把和主体会糊成一团
    云朵，认不出是手柄。加厚 + 拉开握把间距后轮廓才立得住。
    """
    if bold:
        # 关键是让 body 顶边明显高过握把顶边：中间那道凹口不到 1.5 像素就看不出来，
        # 白色区域会连成一块椭圆（16px 实测）
        body = _rr(px, py + 0.05, 0.46, 0.25, 0.16)
        grip_r_ = 0.23
        gx, gy = 0.34, 0.17
    else:
        body = _rr(px, py + 0.02, 0.42, 0.16, 0.16)
        grip_r_ = 0.19
        gx, gy = 0.30, 0.10
    return min(body, _circle(px, py, -gx, gy, grip_r_), _circle(px, py, gx, gy, grip_r_))


def _cutouts(px: float, py: float) -> float:
    """挖空部分 SDF：左十字键（横竖两条）+ 右两个圆按钮。"""
    cx, cy = -0.20, -0.02
    dpad = min(_rr(px - cx, py - cy, 0.115, 0.035, 0.02),
               _rr(px - cx, py - cy, 0.035, 0.115, 0.02))
    btn = min(_circle(px, py, 0.16, -0.08, 0.055),
              _circle(px, py, 0.29, 0.04, 0.055))
    return min(dpad, btn)


def _cov(d: float, size: int) -> float:
    """SDF → 覆盖率：半个像素宽度内线性过渡 = 解析抗锯齿。"""
    return min(max(0.5 - d * size / 2.0, 0.0), 1.0)


def render(size: int) -> bytes:
    """渲染一个尺寸，返回 RGBA（top-down）。"""
    detail = size >= DETAIL_MIN
    px_data = bytearray()
    for y in range(size):
        for x in range(size):
            # 像素中心 → [-1, 1] 坐标
            u = (x + 0.5) / size * 2 - 1
            v = (y + 0.5) / size * 2 - 1

            bg_a = _cov(_rr(u, v, 0.94, 0.94, 0.42), size)
            t = (v + 1) / 2                                   # 垂直渐变系数
            r, g, b = (int(round(TOP[i] + (BOTTOM[i] - TOP[i]) * t)) for i in range(3))

            pad_a = _cov(_gamepad(u, v, bold=not detail), size)
            if detail and pad_a > 0:
                pad_a *= 1.0 - _cov(_cutouts(u, v), size)     # 挖空处露出底色
            if pad_a > 0:
                r = int(round(r + (WHITE[0] - r) * pad_a))
                g = int(round(g + (WHITE[1] - g) * pad_a))
                b = int(round(b + (WHITE[2] - b) * pad_a))
            px_data += bytes((r, g, b, int(round(bg_a * 255))))
    return bytes(px_data)


def _dib(size: int, rgba: bytes) -> bytes:
    """RGBA → ICO 内嵌的 BMP DIB（32bpp，bottom-up，附全 0 的 AND 掩码）。

    不用 PNG-in-ICO：老式 RT_ICON 资源对 PNG 的支持要看 Windows 版本，DIB 全兼容。
    """
    hdr = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4,
                      0, 0, 0, 0)
    rows = []
    for y in range(size - 1, -1, -1):                          # bottom-up
        row = rgba[y * size * 4:(y + 1) * size * 4]
        rows.append(bytes(b for i in range(size)
                          for b in (row[i * 4 + 2], row[i * 4 + 1], row[i * 4], row[i * 4 + 3])))
    mask_stride = ((size + 31) // 32) * 4                      # 1bpp 行按 4 字节对齐
    return hdr + b"".join(rows) + b"\x00" * (mask_stride * size)


def build_ico(images: list) -> bytes:
    """[(size, rgba)] → .ico 字节。"""
    blobs = [_dib(s, px) for s, px in images]
    offset = 6 + 16 * len(blobs)
    out = [struct.pack("<HHH", 0, 1, len(blobs))]
    for (s, _), blob in zip(images, blobs):
        out.append(struct.pack("<BBBBHHII", s if s < 256 else 0, s if s < 256 else 0,
                               0, 0, 1, 32, len(blob), offset))
        offset += len(blob)
    return b"".join(out + blobs)


def main():
    OUT_ICO.parent.mkdir(parents=True, exist_ok=True)
    images = [(s, render(s)) for s in SIZES]
    OUT_ICO.write_bytes(build_ico(images))
    big = dict(images)[256]
    OUT_PNG.write_bytes(_png(256, 256, big))
    print(f"生成 {OUT_ICO.relative_to(ROOT)}（{OUT_ICO.stat().st_size / 1024:.0f} KB，"
          f"{len(SIZES)} 个尺寸：{', '.join(map(str, SIZES))}）")
    print(f"预览 {OUT_PNG.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
