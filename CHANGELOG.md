# CHANGELOG

> 维护规则：每个节点性 commit 提交前 append 一条，与代码一起 `git add`。时间倒序（最新在上），条目不含自身 hash。

## [v0.9.1] — 2026-07-25

**改了什么**
- 应用自定义图标：明亮青蓝渐变圆角方块 + 白色游戏手柄（十字键/按钮挖空露底色），替掉 PyInstaller 默认图标。exe / 任务栏 / 托盘（`tray._app_icon` 从 exe 自身提取）一套统一
- 新增 `scripts/make_icon.py` 程序化生成 `assets/app.ico`：SDF 解析式抗锯齿，8 个尺寸（16-256）各自独立渲染而非缩放大图；16-24px 换加厚造型——细瘦手柄在那个尺寸只剩 2-3 像素高，握把与主体会糊成一团云朵
- web 模式页签图标用 emoji（native 窗口图标来自 exe 本身；ico 没打进 exe，给路径打包后会失效）

**关键改动文件**
- 新增 `scripts/make_icon.py` + `assets/app.ico` / `assets/app_preview.png`；`scripts/build_exe.py`（`--icon`）、`gamelimiter/gui.py`（favicon）

**验证**
- 从打包好的 exe 反向 `extract_icon()` 提取，拿到的就是新图标（64×64）——即托盘实际使用的那条链路
- 逐尺寸放大目检：24px 起手柄轮廓清晰，16px 中间凹口可辨（该尺寸的物理极限）
- 最小 PATH selftest 通过

## [v0.9.0] — 2026-07-25

**改了什么**
- 托盘图标（新模块 `tray.py`，`--tray` 角色）：右键菜单显示「今日已玩 + 守护状态」，可打开面板 / 退出托盘；GUI 启动时幂等登记 HKCU Run 自启并拉起托盘
  - **托盘必须是用户身份的独立进程**：强制层启用后守护以 SYSTEM 跑在 Session 0，那里的托盘图标在用户桌面上根本不显示。托盘只做「看」，杀掉不影响任何限制
  - 零依赖实现（pystray 要拉 Pillow）：隐藏窗口 + `Shell_NotifyIcon` + 弹出菜单 + Win32 消息循环
- 游玩统计（新模块 `stats.py` + GUI「游玩统计」面板，默认展开）：本周 / 本月时长、会话数、有游玩天数、日均，本月分游戏分布条
- 全年热力图：GitHub 风格 53 列 × 7 行，颜色按当日时长分 5 档（明亮青蓝色阶），带月份刻度、星期标签、图例与逐格 tooltip；年份可切换（下拉列出有记录的年份）
- 跨午夜的会话按自然日切开分摊（22:00→01:30 记 90 分钟给前一天、90 分钟给后一天），否则热力图「哪天玩了多久」会失真
- GUI 单实例（`GameLimiterGui` 互斥体）：托盘据此不重复拉面板；检查只在主进程做——native 模式 NiceGUI 会 spawn 子进程重新执行 `main()`，不挡住会把自己的窗口进程掐死

**关键改动文件**
- 新增 `gamelimiter/stats.py` / `gamelimiter/tray.py` / `tests/test_stats.py`；`gamelimiter/gui.py`（stats_view + heatmap_html + main 单实例）、`gamelimiter/app.py`（`--tray` 角色）

**验证**
- `tests/test_stats.py`：跨午夜切分（含整跨日 1440 分钟）、区间裁剪、进行中会话按 now 截断、等级分档、热力图格数（平年 365 / 闰年 366）与首列占位、月份刻度、空库不除零
- GUI 截图：本周/本月卡片 + 分布条 + 全年热力图（231 条造数据，深浅分布正常，月份刻度对齐）
- 托盘：开发版与打包版 exe 均能起、互斥体单实例生效（二次启动打印「托盘已在运行」）；native GUI 二次启动打印「面板已在运行」且不影响首个窗口
- 五个测试文件全过；最小 PATH selftest 通过，体积仍 47.6MB（零新依赖）

## [v0.8.0] — 2026-07-25

**改了什么**
- 游戏卡片显示真实图标：添加游戏时从 exe 提取图标存 `games.icon`（PNG data URI），卡片改「图标 + 名称 / exe 名」两行版式，取不到图标退回首字母色块
- 老数据自动补：`db._migrate()` 给老库 ALTER 出 icon 列，GUI 每次渲染对缺图标且有 exe_path 的游戏补提取一次（CLI 加的、v0.8.0 前就存在的都能补上）
- 新模块 `icons.py`：`PrivateExtractIcons`(64×64，比 `ExtractIconEx` 的 32×32 在高分屏清晰) → `GetIconInfo`/`GetDIBits` 取 BGRA → zlib 手写 PNG 编码。**不引入 Pillow**：图标才几 KB，为它让 onefile exe 涨 4MB 不划算（在线更新是全量下载）
- 图标进 DB 不落地成文件：绕开 ProgramData 的 ACL 坑（v0.7.1）和 NiceGUI 静态目录配置

**关键改动文件**
- 新增 `gamelimiter/icons.py` + `tests/test_icons.py`；`gamelimiter/db.py`（icon 列 + `_migrate` + `set_icon`）、`gamelimiter/gui.py`（`game_avatar` / `backfill_icons` / 卡片版式）、`gamelimiter/cli.py`（add 时提取）

**验证**
- `tests/test_icons.py`：PNG 逐块 CRC/IHDR/扫描行校验 + 像素往返一致；真实 exe 提取到非全透明图标；坏输入（None/空/不存在/目录/非 exe）全返回 None 不抛异常
- GUI 截图三态齐活：提取成功（记事本）、backfill 补上（资源管理器进来时 icon 为 NULL）、无 exe_path 退回首字母块
- 迁移幂等：老 schema 库 connect 后长出 icon 列，二次 connect 不报错
- 四个测试文件全过；最小 PATH selftest 通过后发版

## [v0.7.4] — 2026-07-25

**改了什么**
- 修复台式机重启后 GUI 误报「强制层未配置」+ 常驻「初始化本机」按钮：`is_configured()` 只看 `schtasks /Query` 返回码，而普通权限查 SYSTEM 建的任务被 ACL 挡回「错误: 拒绝访问。」（rc=1）→ 判成任务不存在。与 v0.7.3 的互斥体误报同族：拒绝访问恰证明存在，只有「系统找不到指定的文件」才是真没配
- schtasks 改绝对路径 `%SystemRoot%\System32\schtasks.exe` 调用并兜 OSError：靠 PATH 找、且这行原本无 try/except，一旦 FileNotFoundError 会静默打断整个徽标刷新，表现与"未配置"无法区分
- `upd_badge()` 两条探测各加异常兜底；`updater.apply_update` 的换 exe 后任务恢复判定改用同一个 `is_configured()`

**关键改动文件**
- `gamelimiter/setup_system.py`（SCHTASKS 常量 + is_configured 三态判定）、`gamelimiter/gui.py`（upd_badge）、`gamelimiter/updater.py`（_schtasks / configured）

**验证**
- 分支单测：rc=0 / 中文「拒绝访问」/ 英文「Access is denied」→ True；「系统找不到指定的文件」→ False；本机（任务确不存在）→ False
- 台式机现场取证：`schtasks /Query /TN GameLimiter-Daemon` 普通权限报拒绝访问 rc=1，管理员查得到「计划任务状态: 已启用」→ 确认误报而非强制层丢失
- `tests/test_rules.py` / `tests/test_changes.py` 全过；最小 PATH selftest 通过后发版

## [v0.7.3] — 2026-07-25

**改了什么**
- 修复强制层启用后 GUI 误报「守护未运行（限制不生效）」：SYSTEM 守护创建的全局互斥体默认不给普通用户 SYNCHRONIZE，GUI `OpenMutexW` 吃 ACCESS_DENIED 被当成不存在
- `mutex_exists` 把 ACCESS_DENIED 判为存在（拒绝访问恰证明对象存在；不存在时报 FILE_NOT_FOUND），与 v0.7.1 的 ACL 坑同族（SYSTEM/用户跨身份可见性）

**关键改动文件**
- `gamelimiter/winutil.py`（mutex_exists）

**验证**
- 单测：不存在 → False（FILE_NOT_FOUND 不被误判）；跨进程持有 → True
- SYSTEM 场景待台式机升级后确认徽标转绿；最小 PATH selftest 通过后发版

## [v0.7.2] — 2026-07-24

**改了什么**
- 允许时段从手输文本改为「时段开始/时段结束」两个下拉（半小时档 + CLI 设过的非整档值并入选项）；开始选「不限」= 清除时段规则并隐藏结束框；跨午夜天然支持（结束 < 开始）
- 开始=结束时拦截并提示；多时段（CLI 设的）卡片降级为只读展示防误覆盖；删除 GUI 侧手输解析 `parse_windows_input`

**关键改动文件**
- `gamelimiter/gui.py`（game_card 时段控件 + 事件绑定）

**验证**
- GUI web 截图：有时段卡片双下拉回填正确、无时段卡片显示「不限」且结束框隐藏
- 顺带发现删除带规则游戏走 24h 放宽延迟（设计如此）；开发机测试数据 SQL 直清

## [v0.7.1] — 2026-07-24

**改了什么**
- 修复台式机守护启动崩溃 `PermissionError: daemon.log`：ProgramData 默认 ACL 下文件只有创建者可写，SYSTEM 守护先建的 `daemon.log` / SQLite `-wal` 把用户身份的进程锁在门外
- 三层修复：① `setup_system.grant_users_write()` 用 icacls 给数据目录授 Users(SID 直写) 修改权（含已有文件），初始化本机时执行；② SYSTEM 守护每次启动顺手自愈 ACL（已配强制层的机器升级后自动痊愈，无需重新初始化）；③ `daemon.log` 仍写不动时日志退回 LOCALAPPDATA，守护不再崩死（守护崩 = 完全没限制，比日志分裂严重）

**关键改动文件**
- `gamelimiter/setup_system.py`（grant_users_write）、`gamelimiter/daemon.py`（启动自愈 + 日志兜底）

**验证**
- 本机实测：grant 后 `icacls` 显示 `Users:(OI)(CI)(M)`；LOG_PATH 指向 System32 复现 PermissionError → 正确退回 LOCALAPPDATA 并告警
- 三套单测全过；最小 PATH `--selftest` 通过后发版

## [v0.7.0] — 2026-07-24

**改了什么**
- 在线更新：GUI 启动 3 秒后后台静默检查 GitHub Releases（失败不打扰），发现新版弹更新卡片（版本 + 更新说明 + 大小）；标题旁新增版本号显示 + 手动「检查更新」按钮
- 一键更新链路：下载新 exe 到同目录（进度条）→ 新 exe `--selftest` 自校验防半截文件 → UAC 提权 `--apply-update`：停自愈任务 → 杀旧 exe 全部进程 → 旧 exe 改名 `.old.exe` 留回退 → 新 exe 顶上 → 恢复任务拉起守护 + 新 GUI
- 兜底：检查失败静默/手动时提示；下载或校验失败给「打开下载页」；「忽略此版本」持久化（数据目录 update_ignore.txt）
- 新增 `--version`；`run_elevated` 支持提权任意 exe + 参数带空格加引号

**关键改动文件**
- `gamelimiter/updater.py`（新增）、`gamelimiter/version.py`（新增）、`gamelimiter/gui.py`、`gamelimiter/app.py`、`gamelimiter/winutil.py`

**设计决策**
- 参考 ClaudeDeck（tauri-updater：查→弹窗→下载→装→重启 + 兜底开下载页），适配单 exe：计划任务 `/TR` 指向 exe 绝对路径 → **原地换文件任务不用重配**；Windows 允许重命名运行中的 exe，新 exe 自己执行换文件（无需额外 updater 程序）
- 换文件前必须先 DISABLE 自愈任务再杀进程（否则每分钟自愈会在换文件窗口内复活守护占住旧 exe）
- 更新过程写 `update.log` 到 exe 同目录（提权进程无控制台，排查靠它）；开发环境禁用 apply（sys.executable 是 python）

**验证**
- 版本比较单测 + 真实 API 双向（0.7.0→None；模拟 0.5.0→发现 v0.6.2 含 asset）
- GUI web 模式截图：版本号 + 检查按钮渲染正确；更新对话框（说明/按钮/进度条）渲染正确
- 打包后本机换文件 E2E + 最小 PATH selftest（见 worklog）

## [v0.6.2] — 2026-07-24

**改了什么**
- 修复「添加游戏 → 选文件/快捷方式 → 浏览」点击无反应：`file_types` 描述串含"、"（pywebview 过滤器正则只允许 `\w` 和空格）→ `create_file_dialog` 抛 ValueError → windowed exe 无控制台异常不可见
- 描述串改为"游戏或快捷方式 (*.exe;*.lnk;*.url)"；`webview.OPEN_DIALOG`（已废弃）换 `webview.FileDialog.OPEN`；`pick()` 加异常兜底 `ui.notify`，以后此类错误至少弹通知

**关键改动文件**
- `gamelimiter/gui.py`（pick 函数）

**验证**
- `parse_file_type` 新串通过（旧串复现 ValueError；"、/" 均非法、纯中文+空格合法）
- 最小 PATH `--selftest` 通过后发版

## [v0.6.1] — 2026-07-24

**改了什么**
- 修复台式机（无 conda 干净机器）启动即崩 `ImportError: DLL load failed while importing _ctypes`：conda 把标准库 C 扩展依赖的 DLL 放 `Library\bin`（且 ffi 无版本号），PyInstaller 逐个漏收集；开发机能从系统 PATH 借到所以没暴露
- 打包脚本化：`scripts/diagnose_deps.py` 用 pefile 递归扫描全部 C 扩展(.pyd)的 import 闭包，一次揪出全部 10 个缺失 DLL（ffi/sqlite3/openssl/bz2/lzma/expat/zlib/tcl/tk）；`scripts/build_exe.py` 自动收集重打包，杜绝手敲错名和打地鼠
- exe 新增 `--selftest` 自检钩子：加载全部关键原生依赖后打印 `selftest OK`，配合"最小 PATH"（只留 System32）在开发机模拟干净台式机验证

**关键改动文件**
- `scripts/diagnose_deps.py`（新增）、`scripts/build_exe.py`（新增）、`gamelimiter/app.py`（--selftest）、`USAGE.md`（打包配方 + 坑 3）

**设计决策**
- 不手动指定 DLL 清单而是按真实 import 名自动收集——第一次只修 ffi.dll 结果又卡 sqlite3，证明打地鼠不可靠，必须全量闭包扫描
- 验证手段固化为"最小 PATH + --selftest"，发版前必跑，不再依赖台式机来回试错

**验证**
- 最小 PATH 下旧 exe 复现台式机同款 `_ctypes` 崩溃（证明验证手段有效）
- 全量收集重打包后（49.9MB），最小 PATH 下 `--selftest` 通过（exit 0 + `selftest OK`）

## [v0.6.0] — 2026-07-23

**改了什么**
- Steam 游戏支持：GUI「选文件」新增识别 Steam 桌面图标(.url) → 解析 appid → 扫本机所有 Steam 库定位安装目录 → 智能挑真实游戏 exe（多候选弹选择框，已排序）
- 处理虚幻引擎双层 exe：`Palworld.exe`(启动器) vs `Palworld-Win64-Shipping.exe`(真实长驻进程)——Shipping 版优先；剔除 crashhandler/vcredist/EAC 等干扰 exe
- 非 Steam 商店游戏的 .url（64 位伪 id）与网页 .url 给出明确引导，让用户改用「运行中进程」方式

**关键改动文件**
- `gamelimiter/steam.py`（注册表找根 + libraryfolders.vdf 多库 + appmanifest.acf + exe 候选排序）
- `gamelimiter/gui.py`（.url 分支 + 多候选选择对话框）、`tests/test_steam.py`（合成夹具全覆盖）

**设计决策**
- 限制作用于游戏进程本身，与启动器无关（Steam 拉起的游戏照常被 exe 进程名监控/查杀）；Steam 解析只解决"怎么把正确 exe 加进清单"
- exe 候选排序键：UE Shipping 版 > 与安装目录同名 > 体积大者；rglob 限 4 层深 + 目录/文件名双重 junk 过滤
- 多候选时不自动决定，弹选择框（已按可能性排序，默认选第一个）——避免误选启动器/子工具

**验证**
- `tests/test_steam.py` 全过（.url 解析含伪 id/网页排除、跨库定位、UE 双层 exe 排序、干扰项剔除）
- 本机真实 Steam 库实测：注册表定位 + acf 解析 + exe 候选全链路通

## [v0.5.1] — 2026-07-23

**改了什么**
- 开源发布基建：公开仓 XueTianyu24/GameLimiter 上线（main + tag v0.5.0 + Release draft 含单 exe）
- `scripts/export_public.py`：白名单导出到平级工作副本 + 敏感串 grep 门禁（本机用户名/私人称呼），脚本自身不导出
- `publish/`：公开版 README（安装三步 + 防绕过说明 + SmartScreen 提示）与 MIT LICENSE 的真相源

**设计决策**
- 开发仓与公开仓分离：开发文档含本机路径/私人信息，公开仓只收代码 + CHANGELOG + publish 文档，全新独立历史；公开 commit 用仓库级身份 XueTianyu24 + noreply 邮箱

**验证**
- 导出脚本跑通（clone + 同步 + 门禁通过）；`git add` 后 status 归零证明内容与已推送版本一致；Release asset 上传成功

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
