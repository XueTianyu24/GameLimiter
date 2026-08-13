"""拉取 Intel PresentMon 控制台版到 vendor/（帧时间采集用）。

为什么不直接把 exe 提交进 git：仓库纪律是"源码/脚本/小料进 git，第三方二进制不进"。
这个脚本带 sha256 校验，等价于把版本钉死，且可复现。

  python scripts/fetch_presentmon.py            # 缺了才下
  python scripts/fetch_presentmon.py --force    # 强制重下

许可证 MIT（github.com/GameTechDev/PresentMon）。校验值取自 2026-08-11 实测下载，
与 GitHub Releases API 报的字节数一致，exe 带 Intel Corporation 有效数字签名。
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

VERSION = "2.5.1"
URL = (f"https://github.com/GameTechDev/PresentMon/releases/download/"
       f"v{VERSION}/PresentMon-{VERSION}-x64.exe")
SHA256 = "9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191"
SIZE = 956_768

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "PresentMon.exe"


def digest(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    force = "--force" in sys.argv
    if DEST.is_file() and not force:
        if digest(DEST) == SHA256:
            print(f"已就位：{DEST}（v{VERSION}，校验通过）")
            return 0
        print("已有文件校验不符，重新下载")

    DEST.parent.mkdir(parents=True, exist_ok=True)
    print(f"下载 {URL}")
    # urllib 在 Windows 上会读注册表里的系统代理（USAGE 坑 9），台式机走 Clash 时也能通
    try:
        with urllib.request.urlopen(URL, timeout=120) as r, DEST.open("wb") as f:
            f.write(r.read())
    except Exception as e:
        print(f"下载失败：{e.__class__.__name__}: {e}")
        print("台式机上若直连不通，确认系统代理开着；或手动下载后放到 vendor/PresentMon.exe")
        return 1

    got_size, got_hash = DEST.stat().st_size, digest(DEST)
    if got_size != SIZE or got_hash != SHA256:
        DEST.unlink(missing_ok=True)
        print(f"校验失败，已删除。size={got_size}（期望 {SIZE}）\n  sha256={got_hash}")
        return 1
    print(f"完成：{DEST}（{got_size} 字节，sha256 校验通过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
