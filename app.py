# PyInstaller 打包入口（nicegui-pack --onefile app.py）
import multiprocessing

from gamelimiter.app import main

if __name__ in {"__main__", "__mp_main__"}:
    multiprocessing.freeze_support()
    main()
