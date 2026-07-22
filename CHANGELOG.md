# CHANGELOG

> 维护规则：每个节点性 commit 提交前 append 一条，与代码一起 `git add`。时间倒序（最新在上），条目不含自身 hash。

## [v0.5.0] — 2026-07-23

**改了什么**
- **规则放宽延迟 24h 生效**（防冲动核心）：收紧立即、放宽（含停用/删除受限游戏）进待生效队列，守护到期落地；GUI 卡片显示待生效条目可随时取消，CLI 有 `pending` 命令。GUI/CLI 全部改动强制走 `changes.py` 管制
- **双进程 watchdog 互保**：守护拉起 watchdog，互相探测互斥体、死了秒拉活；`--stop-daemon` 先杀 watchdog 防复活
- **SYSTEM 强制层**：`--setup-system`（需管理员）建 SYSTEM 计划任务（开机自启 + 每分钟自愈）并退役 Run 键；GUI「初始化本机」按钮经 UAC 一键配置，头部显示强制层状态
- **弹窗支持 SYSTEM 会话**：守护跑会话 0 时自动改用 WTSSendMessageW 发到用户会话
- **单 exe 打包**：统一入口 `app.py`（无参 GUI / --daemon / --watchdog / --cli / --setup-system…），PyInstaller onefile

**关键改动文件**
- `gamelimiter/changes.py`（变更管制）、`gamelimiter/watchdog.py`、`gamelimiter/setup_system.py`、`gamelimiter/winutil.py`、`gamelimiter/app.py` + 根 `app.py`（打包入口）
- `gamelimiter/daemon.py`（应用到期变更 + 拉 watchdog）、`notifier.py`（WTS）、`gui.py`、`cli.py`、`db.py`（pending_changes 表）、`tests/test_changes.py`

**设计决策**
- 收紧/放宽判定按字段独立：冷却越长/单次越短/时段覆盖为子集/启用 = 收紧；时段比较用"一天内可玩分钟集合"的子集关系（天然支持跨午夜）
- 改主意变严时自动撤销该字段的待生效放宽；取消放宽申请随时允许（保持更严现状）
- 守护/watchdog 存活探测统一走命名互斥体；GUI/CLI 也用它，与启动方式（exe/dev/SYSTEM）无关
- conda + PyInstaller 打包必须显式收集 `Library\bin\ffi-8.dll`（不在 PATH 时 `_ctypes` 依赖不会被自动发现，exe 启动即 DLL load failed）

**验证**
- `tests/test_changes.py` 全过（分类矩阵 + 收紧即时/放宽延迟落库 + 到期应用 + 延迟删除）
- watchdog 实测：杀守护 → 10 秒内复活；`--stop-daemon` 干净停两者
- GUI 截图核对：强制层徽标 + 初始化按钮 + 卡片待生效条目
- 单 exe：web 模式 HTTP 200 + `--daemon` 互斥体 + `--stop-daemon`（见下）

## [v0.4.0] — 2026-07-23

**改了什么**
- 开机自启（普通权限版）：`scripts/autostart.py` 注册 HKCU Run 键 → pythonw 无窗口拉起 `scripts/daemon_start.pyw`
- 守护进程全局单实例（命名互斥体 `Global\GameLimiterDaemon`），自启 + 手动启动不重复跑
- GUI 守护状态检测改为探测互斥体（与启动方式无关，替代 cmdline 匹配）
- **Phase 1 MVP 至此闭环**；已实际部署：守护在跑 + 自启已注册

**关键改动文件**
- `scripts/autostart.py`、`scripts/daemon_start.pyw`、`gamelimiter/daemon.py`（互斥体）、`gamelimiter/gui.py`（检测）

**设计决策**
- 普通权限下 `schtasks /SC ONLOGON` 需要管理员，Phase 1 用 HKCU Run 键即可；Phase 2 升级 SYSTEM 计划任务后本方式退役
- 单实例/存活检测统一走互斥体：一处定义（daemon），GUI 零成本探测（OpenMutexW）

**验证**
- pyw launcher 拉起守护 → 互斥体检测 True；第二实例启动即退出（日志确认）；Run 键注册/status 正常

## [v0.3.0] — 2026-07-23

**改了什么**
- NiceGUI 卡片式仪表盘（native 桌面窗口，明亮清爽）：每游戏一张卡片，状态实时倒计时（游玩中/可玩/冷却/时段外），规则在卡片上直接改（零层级）
- 添加游戏三通道：运行中进程挑选（只列有窗口的）/ 文件选择（exe 或 .lnk 快捷方式自动解析）/ 手动输入
- 守护状态徽标 + 一键启动守护；游玩记录查看 + 清空入口

**关键改动文件**
- `gamelimiter/gui.py` — 全部 GUI；`python -m gamelimiter.gui`（`GAMELIMITER_WEB=1` 走浏览器，端口 8788）

**设计决策**
- 状态倒计时用"每秒只改 label 文字"的闭包更新器，不整卡重建——避免 1s 定时刷新把正在编辑的输入框炸掉；整卡重建只在增删/开关/保存时触发
- 规则输入 blur/回车即存、立即生效（Phase 2 再加放宽延迟 24h）
- GUI 与守护进程独立：GUI 关掉限制照常生效，共享 ProgramData 下同一 SQLite（WAL）

**验证**
- web 模式 HTTP 200 + Edge 无头截图核对布局；native 桌面窗口模式启动正常（HTTP 200）
- 状态 chip 计算正确（时段外显示"最近 19:00 开放"）

## [v0.2.0] — 2026-07-23

**改了什么**
- 守护进程可用：psutil 1s 轮询 + WMI 事件加速（普通权限自动退化纯轮询）+ 三规则引擎 + SQLite 记录
- 启动前拦截（冷却/时段外 → 秒杀 + 弹窗告知解锁时间）+ 运行中 10/5/1 分钟多级预警 + 到点终止
- CLI 管理工具（add / list / set / remove / history）；conda 环境 `gamelimiter`（py3.12）

**关键改动文件**
- `gamelimiter/rules.py` — 三规则纯函数引擎（冷却/单次时长/允许时段，支持跨午夜时段、规则叠加取最早 deadline）
- `gamelimiter/daemon.py` — 主循环 + 会话跟踪 + 拦截/预警/终止 + 遗留会话收养
- `gamelimiter/db.py` — WAL 模式 SQLite（games / sessions / events）
- `gamelimiter/notifier.py` — MessageBoxTimeoutW 置顶自动关闭弹窗；`gamelimiter/cli.py`、`gamelimiter/config.py`、`tests/test_rules.py`

**设计决策**
- 规则引擎纯函数化（不碰 DB/进程），单测零依赖；deadline 每轮循环从当前规则重算 → 规则收紧立即生效
- 数据放 `C:\ProgramData\GameLimiter\`：Phase 2 守护进程提权 SYSTEM 后仍与用户态 GUI 共享同一路径
- 弹窗用 MessageBoxTimeoutW 线程（零依赖、置顶、15s 自动关闭），不阻塞守护循环

**验证**
- 单测 `tests/test_rules.py` 全过（含跨午夜时段、叠加、收紧立即生效）
- notepad.exe 端到端：时段外拦截 ✅ / 6 秒单次时长到点终止（含 10/5/1 预警事件）✅ / 冷却期拦截 ✅，events/sessions 记录正确

## [v0.1.0] — 2026-07-23

**改了什么**
- 项目文档骨架初始化（project-doc-management 代码工程模板）：CLAUDE.md / 速览.md / USAGE.md / CHANGELOG.md / README.md / 1_worklog/
- 收录 2026-07-22 调研定稿 `调研与方案.md`

**关键改动文件**
- `调研与方案.md` — 工具调研（无现成工具覆盖三规则，自研）+ 双进程架构 + 技术选型 + MVP 两阶段计划

**设计决策**
- 双进程分离：守护进程常驻（WMI 事件 + psutil 兜底 + SQLite），GUI（NiceGUI native）按需打开，平时零占用
- 运行中限制走多级预警倒计时而非直接强杀（PVP 游戏强杀判逃跑）；限制尽量前置到启动拦截
- 强制层：SYSTEM 计划任务常驻 + 双进程 watchdog + 规则放宽延迟 24h 生效（收紧立即生效）

**验证**
- 纯文档，无代码
