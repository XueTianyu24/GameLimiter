# 开机自启入口：pythonw 无窗口拉起守护进程（Run 键不能设 cwd，这里自补 sys.path）
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamelimiter.daemon import main

main()
