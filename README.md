# GameLimiter

Windows 11 上的游戏自我防沉迷工具：给自己的游戏加上"打不开、玩不久、到点关"的强制限制，并让**冲动时刻的绕过成本足够高**。

自用项目，作者：**雪天鱼**

## 限制规则（可叠加）

按游戏配置：

| 规则 | 说明 |
|---|---|
| **间隔冷却** | 距上次游玩不满 N 小时 → 打不开 |
| **下次可玩日** | 直接指定下次哪天才能玩（选日期或点「明天 / 后天 / 周六」），那天之前一律打不开。到了那天，仍按下面的时段与时长规则走。与冷却独立叠加，过期自动失效 |
| **单次最长时长** | 单次最多玩 M 分钟，到点强制关闭 |
| **允许时段** | 只有指定时段能打开；时段结束强制关闭 |

外加一条全局的：

| 规则 | 说明 |
|---|---|
| **每天最多玩几款** | 一天内最多开 K 款**不同**的游戏；用满之后当天打不开新游戏（今天已经玩过的那几款不受影响，照常继续） |

- **本次额度**：单次时长是**上限**，每次开玩前还能在卡片上把这一次调得更低（如上限 2 小时、这次只玩 1 小时）。30 分钟一档加减，也可直接输入。**只能调低，不能超过上限**；玩到一半只许再缩短，不许加时
- **启动前拦截**：不满足条件时游戏进程秒杀 + 弹窗告知解锁时间（还没进对局，无代价）
- **运行中限制**：到点前 10 / 5 / 1 分钟多级预警倒计时，给你时间自己退出对局——**不会在 PVP 对局中途偷袭强杀**（避免判逃跑）

## 防绕过设计（诚实说明）

你自己就是管理员，"绝对防绕过"不存在。本工具的目标是把**一时冲动**的绕过成本抬高到冷静下来的程度：

1. **守护进程以 SYSTEM 权限常驻**——任务管理器普通权限杀不掉，动它要过 UAC
2. **双进程 watchdog 互保 + 计划任务每分钟自愈**——杀掉也秒复活
3. **规则放宽延迟 24 小时生效**（核心）——冲动时把规则改松没用，明天才生效，且随时可反悔取消；收紧则立即生效。四条规则一视同仁，包括「每天最多玩几款」

## 安装（三步）

1. 从 [Releases](../../releases) 下载 `GameLimiter.exe`（单文件，免安装免 Python）
2. 双击运行（首次会被 SmartScreen 拦：点"更多信息 → 仍要运行"，属未签名 exe 的正常提示）
3. 点界面右上角 **「初始化本机」**，通过一次 UAC 授权 → 自动配置 SYSTEM 守护自启 + 自愈任务

之后点「添加游戏」，在卡片上直接设置规则即可。GUI 关掉不影响限制生效。

**添加游戏三种方式**：
- **运行中进程**：先启动游戏，从列表挑（最省心，直接命中真实进程）
- **浏览文件**：选游戏 exe、快捷方式(.lnk)，或 **Steam 桌面图标(.url)** —— 会自动解析 Steam 库、定位安装目录并挑出真正的游戏 exe（虚幻引擎游戏如《幻兽帕鲁》的双层 exe 也能正确识别）
- **手动输入**：直接填进程名

> 限制作用于游戏进程本身，与谁启动无关——Steam / Epic / 快捷方式启动的游戏都照常拦截。

## 在线更新

应用启动后会自动检查新版本（检查失败不打扰）；发现新版会弹窗展示更新说明，点 **「立即更新」** 即可自动下载 → 校验 → 换新 → 重启，全程一次 UAC 授权。也可点标题旁的 ⟳ 图标手动检查。网络不畅时可用弹窗里的「打开下载页」手动下载覆盖（配置与游戏清单不受影响）。

## 命令行（可选）

```
GameLimiter.exe --cli list          # 查看游戏与规则
GameLimiter.exe --cli next <exe> 60 # 这次只玩 60 分钟（留空查看，off 清除）
GameLimiter.exe --cli daily 2       # 每天最多玩 2 款游戏（留空查看，off 取消）
GameLimiter.exe --cli set <exe> --until 2026-08-02   # 下次可玩日（也可 +3 / off）
GameLimiter.exe --cli history       # 游玩记录与拦截事件
GameLimiter.exe --cli pending       # 待生效的放宽变更（可 --cancel <id> 反悔）
GameLimiter.exe --remove-system     # 卸载强制层（需管理员）
```

## 从源码运行 / 构建

```bash
pip install psutil wmi "nicegui[native]" pyinstaller
python -m gamelimiter.app                 # GUI
python -m gamelimiter.app --daemon        # 守护进程
python tests/test_rules.py && python tests/test_changes.py

# 打包单 exe（conda 环境注意：需显式收集 Library/bin/ffi-8.dll）
pyinstaller --noconfirm --name GameLimiter --windowed --onefile ^
  --add-data "<site-packages>/nicegui;nicegui" app.py
```

## 技术栈

Python · psutil + WMI 进程监控 · SQLite（WAL）· NiceGUI（WebView2 原生窗口）· 数据存于 `C:\ProgramData\GameLimiter\`

## License

MIT © 雪天鱼 (XueTianyu24)
