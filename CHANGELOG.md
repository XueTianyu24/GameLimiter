# CHANGELOG

> 维护规则：每个节点性 commit 提交前 append 一条，与代码一起 `git add`。时间倒序（最新在上），条目不含自身 hash。

## [v0.18.0] 补三个漏：拆强制层要等 24h / 改名不再脱管 / 周末单独一档 — 2026-08-19

用户问"还有什么真正需要补的"，通读代码找出五个缺口，先做其中三个（另两个是预警加声音、
未登记游戏发现，留下一版）。三条都不改已有规则语义，只补漏。

### 1. 拆除强制层也走 24 小时冷静期

**纪律不一致**：停用一款游戏、删掉一款游戏、放宽任何一条规则都要等 24 小时，
唯独 `--remove-system`——一条命令把 SYSTEM 自启 + 每分钟自愈整套卸掉——是**立刻生效**的。
冲动时最短的那条路反而没设防。

- `--remove-system` 改成**只提交申请**，进的是和放宽规则同一条 `pending_changes` 队列
  （全局项借 `game_id=0`，field `__remove_system__`）；到期后由守护进程调
  `setup_system.remove_now()` 落地——守护是 SYSTEM 身份，删得动自己的计划任务
- **重复申请不重新计时**：等了 20 小时再点一次，剩的还是 4 小时。冷静期要是能被
  "再点一次"刷新，那它就不是冷静期
- 落地后守护把 watchdog 和自己一并停掉。否则"拆除"只拆一半——任务没了但进程还在跑，
  得等重启才真的失效
- 删不掉（当前身份权限不够）时**申请留着、5 分钟后重试**，不默默作废；也不每 5 秒刷一次日志
- `apply_due` 加 `on_applied(field, game_id)` 回调：拆完要自杀这件事只有守护自己做得了，
  而 `changes` 层不该知道守护的存在
- 诚实边界照旧：自己是管理员，真急着拆就手动
  `schtasks /Delete /TN GameLimiter-Daemon /F`——这里堵的是"应用自带一条命令就拆干净"

### 2. exe 改个名就脱管 —— 加路径与文件指纹两道识别

原来 `_scan_procs` **只比 exe 文件名**：把游戏复制一份改成 `a.exe` 就完全不在管制内，
成本 5 秒，比"改规则等 24 小时"低得多。新增 `procmatch.py`，三道识别命中即止：

1. **exe 文件名** —— 老行为，绝大多数情况走这条
2. **exe 全路径** —— 改了名但还在原地（`games.exe_path` 早就存了，之前从没用于匹配）
3. **文件指纹** —— 大小 + 首尾各 1 MB 的 sha256，连目录一起复制走再改名也认得出

- 靠**后两条命中时记一条 `renamed` 事件 + 弹窗告知**，规则照旧生效。绕过动作被看见，
  比悄悄成功更能提高冲动成本
- **开销实测**：`process_iter(["name","exe"])` 比只取 name 只贵 0.1 ms（400 进程的机器上
  两者都约 1.5 ms），所以路径匹配每轮全量做、不必缓存 pid。指纹只在"名字和路径都没命中、
  且大小恰好等于某款受限游戏"时才真读盘，大小/指纹按路径缓存 → 稳态等于不跑
- **不误伤**：小于 4 MB 的不做指纹识别（小文件撞大小太容易，也不像游戏本体）；
  系统目录（`%SystemRoot%`）下的进程直接跳过；大小相同但内容不同必须指纹也对得上才算
- 老数据在守护 `reload_games` 时自动补指纹（`procmatch.backfill`），GUI 添加游戏时当场补

### 3. 规则 e 拆成平日 / 周末两档

一个数必然只能二选一：按平日设，周末太紧；按周末设，平日太松。加
`daily_minutes_weekend`（周六日），**留空 = 沿用平日**，不是"不限"。

- 因此判收紧/放宽要比**两档实际生效的分钟数**：平日 60 / 周末 180 时清掉周末档，
  周末反而从 180 缩到 60 —— 那是收紧，该立即生效。直接套"空值=不限"会把它误判成放宽
- 上限用 `db.effective_daily_minutes(now)` 按当天是周几选，已玩时长的口径完全不变；
  跨午夜从周五进周六，剩下的时间吃周六的额度（deadline 每轮重算，已有的"deadline
  后移就清预警档"逻辑正好覆盖）
- `--cli daily --minutes-weekend N|off`；GUI 全局卡片一行放两个输入框

### 顺带

- 全局规则卡片原来"一款游戏都没有就整块不画"，会让拆除强制层的申请无处可取消 → 改成
  有全局待生效项时照样画
- `_safe_console` 从 `cli.py` 搬到 `winutil.py` 改名 `safe_console`，`app.main()` 也调它。
  `--remove-system` 这类**非 `--cli` 分支同样 print 中文**，而坑 13 的 GBK 崩 + `--windowed`
  弹窗挂死只在 `--cli` 里防住了。打包版实测：修前输出乱码，修后正常

**改动文件**：新增 `gamelimiter/procmatch.py` + `tests/test_procmatch.py`；
`db.py`（`games.exe_size/exe_hash` 两列 + 迁移、`set_exe_fingerprint`、
`daily_minutes_weekend` 设置项 + `effective_daily_minutes`）、`rules.py`（`is_weekend`）、
`changes.py`（`request_remove_system`、`request_daily_minutes(weekend=)`、
`apply_due` 特判拆除 + `on_applied` 回调、`GLOBAL_FIELDS` 加周末档）、
`setup_system.py`（`remove()` → `remove_now()`，明确"不要直接调"）、
`app.py`（`--remove-system` 转为申请 + 控制台编码兜底）、`winutil.py`（`safe_console`）、`daemon.py`（改用 `procmatch.Matcher`、
改名告警、今日适用上限、拆除落地后自我收尾）、`cli.py`、`gui.py`

**验证**：单测 13 个全过（新增 `test_procmatch.py` 9 组：三道识别 / 大小相同内容不同不误认 /
系统目录跳过 / 小文件不走指纹 / 拿不到路径时退回名字 / 指纹补齐幂等；`test_daily.py`
加周末档 / `test_changes.py` 加拆除队列，含"重复申请不重新计时"与"删不掉就重试"）；
四个 e2e 全过（`e2e_block` / `e2e_daily` / `e2e_capture` / `e2e_frames`，验证换成
`procmatch` 后守护的拦截、续玩、总额、采集链路都没走样）；GUI 隔离库无头截图确认
平日/周末两个输入框与拆除申请那一行

## [v0.17.0] 新增规则 e：每天游玩总时长 — 2026-08-16

> 已发 Release（08-16，`dist/GameLimiter.exe` 50.8 MB，最小 PATH `--selftest` 过；
> 公开仓 `main` c50a0eb + tag v0.17.0；已验"冒充 v0.16.0 能查到新版"）。
> 公开 README 同步加了规则 e 那一行（真相源 `publish/README.md`）。

用户发现的缺口：全局规则只有「每天最多玩几款」（数款数），时长限制全挂在单个游戏上。
**今天帕鲁 2 小时 + 永劫无间 2 小时 = 4 小时，一条规则都不会响**——限款数根本挡不住
每款都玩到天亮。补上跨游戏的总额。

**语义**

- **口径沿用规则 b**：算进程**真实在跑**的秒数（`sessions.played_seconds`），不按墙钟。
  中途退出的时间、守护没观测到的空窗期都不计
- **两处执行**：启动前拦截（理由 `daily_minutes`，解锁 = 明天 0:00）；运行中把
  「今日剩余额度」作为第三个候选喂进 `session_deadline`，与单次时长/时段一起取最早。
  **走 deadline 而不是直接杀**，是为了复用现有 10/5/1 分钟预警倒计时——PVP 被无预警
  强杀会判逃跑，这条不能省
- **对续玩、对今天已经玩过的那款照样生效**（与规则 d 相反）。总额用完了，换回第一款
  接着玩正是要拦的事
- **观察模式的游戏不计入**（第四个绕过点，与规则 d 一致）：它本就不受任何限制，
  让它吃掉总额会把别的游戏挤没
- 变更管制同规则 d：调小立即生效、调大延迟 24h，pending 借 `game_id=0`

**两个不显然的实现点**

1. **跨午夜的会话按墙钟重叠比例分摊到两天**。真实游玩秒数只有一个总数、没有按分钟落库
   的时间轴，没法精确切分；误差只发生在"跨零点那一场"且只有中途退出过才会偏，为此加
   一张逐分钟表不值当——按近似算，把这件事写在函数注释里
2. **deadline 后移时要清掉已触发的预警档**（`ActiveSession.last_deadline`）。过零点额度
   重置会让 deadline 往后跳，重置前擦过的 10/5/1 档若保持标记，真到点时会一声不吭
   直接杀进程

**改动文件**：`rules.py`（`check_daily_minutes` + `session_deadline` 加 `daily_remaining`
参数 + `REASON_TEXT`）、`db.py`（`daily_minutes` 设置项 + `played_seconds_between` /
`daily_used_seconds` / `daily_remaining_seconds` + `sessions(start_ts)` 索引）、
`changes.py`（`request_daily_minutes` + `GLOBAL_FIELDS` 派发表，原来 `apply_due` 对全局
pending 硬编码调 `set_daily_game_limit`，加第二条全局规则会串台）、`daemon.py`（两处执行点
+ 每轮刷新今日已玩）、`cli.py`（`daily --minutes`，`list` 头一行带上）、`gui.py`（全局卡片
第二行 + 用完时每张游戏卡片照实显示）、`tray.py`（剩余时间口径跟上）

**验证**：单测 12 个全过（新增 `tests/test_daily.py`：判定 / deadline 取最早 / 跨游戏累加 /
跨午夜分摊 / 变更管制两条队列互不覆盖）；新增 `tests/e2e_daily.py` 真守护 11 项全过——
预置 25 秒历史 + 总额 40 秒 → 开玩存活 18.4 秒（不是整个 40 秒）→ `killed=daily_minutes`
且终止前有预警 → 再开被 `blocked=daily_minutes` 拦下。GUI 两种状态无头截图确认

## [工具] 导出隐私门禁漏掉新增文件 — 2026-08-13

发 v0.16.0 时实测发现：`export_public.py` 的门禁用 `git grep` 查敏感串，而
**`git grep` 默认只看已跟踪文件**——这次新增的六个 `scripts/deploy_*.ps1`（往台式机
scp 换版用，含本机用户名与机器布局）在公开仓工作副本里全是未跟踪状态，门禁一声没吭
就放行了。**门禁要拦的恰恰是"这次新加的东西"，却正好对它瞎**。

- 门禁改用 `git grep --untracked`（对照实测：老写法 0 命中、新写法 1 命中）
- `deploy*.ps1` 进 `EXPORT_IGNORE`（与 `diag_*` / `probe_*` 同理：本机专用、对公开用户零价值）
- 导出后**另做一次不依赖 git 的全树 grep 复核**，不把门禁当唯一防线

**影响文件**：`scripts/export_public.py`

## [v0.16.0] — 2026-08-13

**采集从"开游戏就自动采"改成"点了才采"**（用户提出：不想开着游戏就一直被采，
而且要能自己定采多久、数据落哪儿）

先把体积这件事说清楚：**原来也撑不爆硬盘**。帧数据一小时 260 MB 但聚合完立即删
（约 10 KB 摘要入库），硬件数据 1 Hz 两小时才 700 KB、只留最近 30 次，稳态占用约 20 MB。
真正的问题不是体积，是**采集变成了一个隐形的常驻行为**——用户不知道它在跑、
占多少、什么时候清。所以这版改的是**知情权与控制权**，顺带解决另一件事：
手动下单才能取到**干净样本**（08-13 那份数据就毁在会话开头撞上了部署）。

**改了什么**

- **`capture_jobs` 表 + `capture_mode` 设置**：GUI/CLI 下单，守护接单。两边是不同身份的
  独立进程（守护是 SYSTEM、GUI 是用户），只能靠 SQLite 通信——沿用现成的通道，不新建 IPC。
  状态机 `armed →（游戏在跑）running →（到点/游戏退出/手动停）done`，
  另有 `expired`（待命 4 小时没等到游戏）/ `cancelled`
- **默认 `manual`**：不下单就一个采集器都不起。老库升级后同样是 manual，
  这是**行为变更**——升级后不点采集就没有数据。想回到老行为：`--cli capture --mode auto`
- **可选时长**：到点只停采集，**游戏照玩**（`_stop_capture` 与会话彻底解耦）
- **可选存放目录**：`frames.capture_dir(out_dir)` / `hardware.capture_dir(out_dir)`，
  写前做 mkdir + 写探测，不可写**回落默认目录并记 warning**——执笔的是 SYSTEM 守护，
  用户级映射的网络盘它根本看不见，宁可落回默认也不能让一次采集整个泡汤
- **手动采集默认保留原始逐帧 CSV**（自动模式仍是聚合后即删）。保留时改名加 `raw-` 前缀，
  免得被孤儿清理当垃圾收走；路径进摘要的 `raw_csv`，`--cli frames` 会显示
- **GUI**：卡片上一行「采集性能数据」按钮 →弹窗选时长（5/10/30/60 预设 + 自定义 + 整场）、
  存放目录（记住上次的选择 + 原生目录选择框）、要不要留原始数据；
  采集中原地变成倒计时 + 停止按钮
- **CLI**：`capture <exe> --minutes 10 --out D:\x [--whole|--no-keep-raw|--stop]` /
  `capture --mode manual|auto`。台式机是 ssh 部署的，没 CLI 等于远程用不了

**在线更新失败会把强制层关在那儿**（决定走 Release + 应用内更新之后才审出来的）

`apply_update` 第一步为防复活先 `/DISABLE` 了守护任务与自愈任务，可它的失败出口
（旧 exe 被占用 15 秒没放开 / 任何异常）都是直接 `return 1` —— 机器就停在
**"计划任务已停 + 守护已杀" = 防护关着**的中间态，用户只看到"更新失败"，
不会知道防护也一起没了，而且没人会去恢复。这与 v0.15.1 记的坑 16 是同一类危险，
区别在于 scp 部署时我在旁边盯着，改走用户自己点的在线更新就没人兜底了。

- 每条失败出口都 `_restore_tasks()`：重新 `/ENABLE` 两个任务 + `/Run` 拉起守护，
  失败也如实记进 `update.log`（不静默）
- 补了最坏的一格：**旧 exe 已改名 `.old`、新 exe 又没顶上去** —— 原先这一格
  会让计划任务指向的路径上一个文件都没有，守护再也起不来。现在把旧版原路放回
- `tests/test_updater.py` 钉住 5 条路径（正常 / 旧 exe 被占用 / 换名成功但顶替失败 /
  中途抛异常 / 没配强制层的机器）。这些手工没法验——要管理员 + 打包 exe + 制造文件占用

**顺带修的两个真问题**

1. **帧采集器一死，硬件采集跟着断**（e2e 抓到）：原先 tick 里发现 PresentMon 提前退出就
   调 `_stop_capture` 把两个采集器一起收了。可帧采集要管理员权限、硬件采集不要，
   开发机上前者必然起不来 —— 于是硬件数据也断在第一秒。改为只收帧采集那一个
2. **孤儿文件的清理有窗口**：清理只在守护启动时跑一次、且只删 24 小时以上的。
   守护崩在采集中途留下的几百 MB 文件，watchdog 10 秒就复活（那时文件还太新扫不掉），
   之后守护不重启它就一直躺着。改为**每小时也扫一次 + 判据收到"1 小时没人动过"**，
   并由守护把正在写的文件显式排除（游戏最小化不渲染时 mtime 可能长时间不动）

**关键改动文件**：`db.py`（建表 + 任务 CRUD）、`daemon.py`（任务状态机 + 独立收帧采集 +
周期清理）、`frames.py` / `hardware.py`（目录可指定 + 保留原始）、`config.py`
（`resolve_capture_dir` 写探测与回落）、`cli.py`（`capture` 子命令）、`gui.py`（按钮 + 弹窗）

**验证**：10 个单测全过（新增 `tests/test_capture.py`：状态机 / 目录回落 / 保留原始不被
误删 / 轮转边界）；新增 `tests/e2e_capture.py` 真守护端到端 18 项全过——没下单确实一条
记录都没有、游戏在跑时下单 0.5 秒接单、12 秒到点停采而**游戏仍在跑**、手动停止即时收尾、
数据确实落在指定目录（12 个采样点）；`e2e_block.py` 无回归；GUI 无头截图核对了
三种状态（空闲 / 待命中 / 弹窗版式）

## [v0.15.1] — 2026-08-13

**首次真机采集暴露的三个问题**（台式机升到 v0.15.0 后，永劫无间一段 12.1 分钟的真实
游玩：帧采集拿到 **117802 帧**、硬件采集 **723 个采样点**，功能本身跑通了，但数据一看就
发现三处不对）

1. **`game_cpu` 一整列全是 0** —— 723 个采样点无一例外，`other_cpu` 也就等于总占用。
   根因：`_game_proc()` 每次采样都新建 `psutil.Process`，而 `cpu_percent()` 算的是
   "距上次在**同一个对象**上调用"的增量，新对象永远返回 0.0。改为缓存进程对象，
   进程没了才重新解析。测试加了"必须测到那个满载子进程"的断言
2. **帧摘要只留了最慢几帧的数值，没留时刻** —— 逐帧 CSV 聚合完就删了，没有时刻就
   没法跟硬件逐秒采样对齐，而"对齐"正是这两个功能配套的意义。新增 `worst_at`
   （单遍 O(n log k) 堆求解），展示成「3657.9(第 694 秒)」
3. **`--cli` 输出重定向到文件时中文全变成 `?`** —— `--windowed` 打包的 exe 在 ssh 下
   必须重定向才拿得到输出（坑 10），而那时 Python 挑的编码可能是 ASCII。改为
   **重定向时强制 UTF-8、接真控制台时才跟随控制台编码**（后者仍保留 errors="replace"，
   见 v0.14.0 的 GBK 坑）

**顺带修了测试自己的一个方法错误**：给 `game_cpu` 加断言时，我把烧 CPU 的循环写在
测试进程主线程里，于是"采样器开销"量出 82.8% —— 全是负载本身。改成**独立子进程当
负载**、测试进程保持安静，量得 **0.89% 单核**（与 v0.15.0 的 0.78% 一致）。
测量方法本身也会骗人，这条值得记住。

## [v0.15.0] — 2026-08-12

**改了什么**

新增**游玩期间的硬件采集**，以及为它配套的**观察模式**。

起因是 08-12 排查永劫无间卡顿：手工敲了一晚上 PowerShell 探针（GPU 遥测、CPU 睿频、
磁盘延迟、缺页、干扰进程），每次都要临时写脚本、临时 ssh。把它固化进应用，
以后"帮我看看昨天那局"直接把数据捞出来就行。

- **`gamelimiter/hardware.py`**：挂在会话上（与帧采集同一套生命周期），1 Hz 记录
  CPU 每核占用 / 内存 / 磁盘读写 / 游戏进程的 CPU·内存·IO·缺页·句柄 / GPU 占用·显存·
  温度·功耗·时钟·**降频原因**
  - GPU 遥测常驻**一个** `nvidia-smi -lms` 进程流式读取。**不能每秒起一次进程**——
    那种开销正是给游戏添堵的东西
  - 摘要落 `hw_runs` 表，并把结论压成几条人话标记（GPU 降频 / 内存吃紧 / GPU 高温 /
    有核长期满载 / 游戏之外有东西在吃 CPU）
- **保留策略与帧数据相反**：帧数据一小时 270 MB，用完即删；硬件采样两小时才约
  700 KB，**原样保留最近 30 次**，这样事后能直接捞原始 CSV 分析
- **观察模式 `monitor_only`**：登记进来只采数据，**不施加任何限制**。给永劫无间这类
  **强杀会判逃跑扣分**的 PVP 游戏用。绕过点三处，缺一不可：`check_start` 无条件放行、
  `session_deadline` 返回 None（守护据此永不强杀）、`games_played_between` 不计入
  （既不被"每天最多几款"拦，也不占别人的名额）
  - 开关本身仍受变更管制：打开 = 卸掉限制 = **放宽 → 延迟 24h**；关掉 = 收紧 → 立即
  - 全新登记的游戏直接 `--monitor` 是立即生效的（它本来就不受任何限制，不构成放宽）；
    已存在的受限游戏改观察模式才走 24h
- CLI：`hw [exe] [--paths]` 看记录、`--on/--off` 开关；`add --monitor`、`set --monitor 0|1`

**采集开销是这次设计的主要约束**（实测逼出来的，428 进程的机器）

第一版实测**吃掉 62.8% 的单核**，完全不能跟游戏并排跑。profiling 定位到两处，
都不是想当然能猜到的：

1. **`Process.num_threads()` 单个调用就要 16 ms**（Windows 上要枚举全系统线程表），
   而 `cpu_percent` / `memory_info` / `io_counters` / `num_handles` 全是 0.0 ms → 只砍它
2. **psutil 全表取任何按进程的 CPU/内存字段要 3.3 秒**——`process_iter(['name'])` 只要
   1.8 ms 是因为名字能批量取，CPU/内存**没有**批量优化。所以"谁在抢资源"的常驻扫描
   根本不可行 → 改成零成本的 `other_cpu`（全机占用减去游戏那份）**探测**有没有干扰，
   真有再单独跑一次进程排查

改完 **0.78% 的单核**（全部 20 核的 0.039%），降了 80 倍。测试里钉了 <5% 的硬门槛。

**关键改动文件**
- 新增 `gamelimiter/hardware.py`、`tests/test_hardware.py`、`tests/test_monitor.py`
- `gamelimiter/db.py`（新表 `hw_runs` + `games.monitor_only` 迁移列 + `games_played_between`
  排除观察模式）、`gamelimiter/rules.py`（`is_observed` + 两处绕过）、
  `gamelimiter/changes.py`（`monitor_only` 的收紧/放宽判定与描述）、
  `gamelimiter/daemon.py`（观察模式分支 + 硬件采样挂载/收尾）、`gamelimiter/cli.py`

**验证**
- 九个测试文件全过。`test_monitor.py` 逐一钉死三个绕过点：一款处处会被拦的游戏
  （锁到 2099 年 + 20h 冷却 + 30min 上限 + 1 小时时段）打开观察模式后 `check_start`
  无条件放行、`session_deadline` 恒为 None、既不占也不被"每天最多 1 款"拦；
  开关的 24h 冷静期双向正确；观察模式游戏可直接删而受限游戏仍排队；老库无该列读出 False
- `test_hardware.py` 覆盖聚合口径、四类异常判读、缺 GPU 时的降级、DB 往返、保留策略，
  并**真起一个采样器量它自己的开销**（这条是硬门槛，就是它抓出了 62.8%）
- 守护端到端 `e2e_block.py` 16 项全过，两个采集器同时挂载/收尾，限制逻辑不受影响

## [v0.14.0] — 2026-08-11

**改了什么**

新增**帧时间采集**：每次游玩自动挂一个 PresentMon，游戏退出后聚合成摘要入库，
把"这次玩得卡不卡"变成和游玩时长并列的历史数据。

起因是 08-11 排查台式机玩帕鲁"巨卡"：查了一整晚硬件、系统、后台负载，全部无罪，
最后发现真凶是游戏里一行 `FrameRateLimit=90` 撞 165 Hz 屏幕（非整数分频 → 持续微顿挫）。
**这种问题躺多久都不会被发现，因为没有任何手段回答"卡不卡"。**

- **为什么由 GameLimiter 做**：它精确知道游戏何时启动、进程名、何时退出（WMI 进程事件）
  → 采集器自动挂载/卸载，零手工开关；守护跑 SYSTEM 权限 → 满足 PresentMon 对
  管理员/`Performance Log Users` 的要求；已有"段游玩"时间轴 + SQLite → 挂上去就是趋势
- **工具**：Intel PresentMon 2.5.1 控制台版（MIT）。单 exe **0.91 MB**，直接打进 onefile，
  不联网不安装。**不进 git**，由 `scripts/fetch_presentmon.py` 带 sha256 校验拉取
- **摘要内容**：平均 fps / 1% low / 0.1% low、帧时间 p50·p95·p99·max、卡顿次数（超过中位数
  2 倍的帧，与 `scripts/frametest.html` 同口径可横向比）、画面模式、垂直同步、生成帧占比、
  点击到画面延迟、每分钟趋势
- **两个自动判读**（正是这次人工排查的结论，以后自动给出）：
  - **瓶颈定性**：显卡/CPU 忙碌时间占帧时间 ≥85% → 谁吃满了；两者都没吃满而帧时间异常
    平稳（p95/p50 ≤ 1.12）→ **帧率被限制**
  - **节奏不齐**：显示间隔偏离中位数超 25% 的帧占比。帧率不是刷新率整数分频时
    （90 fps 送进 165 Hz）每帧占 1 或 2 个刷新周期不规则交替，这个数会显著抬高
    ——**平均帧数好看、眼睛却一顿一顿，看的就是它**
- **数据量取舍（本次设计的核心约束）**：PresentMon 一帧一行，165 fps 玩 2 小时 ≈ 119 万行
  ≈ 200 MB。原始 CSV 落应用数据目录、**用完即删**，只留约 10 KB 摘要；解析全程流式，
  逐列 sort 后立即丢弃，峰值内存只占一列
- **与「一段游玩」对接**：采集按"一次进程运行"（`frame_runs` 一行），展示按"段"聚合
  （段内多次运行按帧数加权合并）
- **绝不影响防沉迷主职**：采集器起不来/跑挂/解析失败一律吞异常记日志；收尾走后台线程
  （聚合上百万行要几秒，卡在 tick 里会让启动拦截失灵）
- CLI：`frames [exe]` 看记录、`--check` 试跑看权限够不够、`--on/--off` 开关
- 第一版**只做采集 + 聚合 + CLI**，GUI 展示与趋势图放第二版

**关键改动文件**
- 新增 `gamelimiter/frames.py`（采集进程生命周期 + 流式聚合 + 判读 + 人话输出）、
  `scripts/fetch_presentmon.py`、`tests/test_frames.py`、`tests/e2e_frames.py`
- `gamelimiter/db.py`（新表 `frame_runs` + `insert_frame_run`/`frame_runs`/`block_frame_summary`）、
  `gamelimiter/daemon.py`（四条会话结束路径全部挂上收尾 + 采集器早死兜底 + 启动清孤儿 CSV）、
  `gamelimiter/cli.py`、`gamelimiter/app.py`（selftest 报采集器路径）、`scripts/build_exe.py`
- 排查工具沉淀：`scripts/diag_*.ps1`（5 个）、`scripts/frametest.html`、`scripts/probe_presentmon*.ps1`

**验证**
- 七个测试文件全过。`test_frames.py` 用合成 CSV 覆盖：统计口径、三种瓶颈定性
  （含帕鲁那个"帧率被限制"）、节奏不齐检测与对照组、多交换链取主画面、NA/脏值、
  生成帧、每分钟趋势、DB 往返与段级加权合并、开关
- **真实产物验证**（这一步抓出两个合成 CSV 测不出的 bug）：
  1. PresentMon 写的 CSV **带 UTF-8 BOM**，按 utf-8 读会让首列名变成 `﻿Application`、
     整个表头对不上、静默返回 None → 改 `utf-8-sig`，并补了 BOM 回归测试
  2. 收尾顺序写反了：原先上来就 `terminate()`，而 Windows 上那**就是硬杀**，等于每次
     会话结束都先硬杀再等它优雅退出，尾部数据必丢 → 改成
     先等自退（15s）→ 发 Ctrl+Break（需 `CREATE_NEW_PROCESS_GROUP`）→ 最后才硬杀
- 台式机实测（探针 `probe_presentmon*.ps1`，ssh 提权跑）：
  - **SYSTEM（session 0）能抓到用户桌面 session 1 的进程** —— 这是整个设计的前提，
    实测 `whoami = nt authority\system` 下抓到 `msedgewebview2`(session=1) 137 帧
  - 生产完全一致的参数下真产出 CSV（302 行），拉回本地喂生产解析器结果自洽
  - PresentMon exe 带 **Intel Corporation 有效数字签名**，火绒全程未拦
  - **零帧时 PresentMon 根本不建 CSV 文件**（代码按"文件不存在 = no_frames"处理，正确）
- 守护端到端 `e2e_block.py` 16 项全过：未提权时采集失败被 1 秒内察觉、失败原因入库、
  无残留 CSV，**限制逻辑完全不受影响**
- **打包版验证**（第三个只有跑 exe 才暴露的 bug）：`--cli frames --check` 抛
  `UnicodeEncodeError: 'gbk' codec can't encode character '✗'`——打包 exe 的 stdout 走
  GBK，而 `--windowed` 下未捕获异常会弹对话框把进程**挂死**（5 分钟超时才被杀）。
  改成纯文字 + `cli.main()` 开头 `stdout.reconfigure(errors="replace")` 兜底，
  并写了个字面量 GBK 扫描确认没有漏网的。进 USAGE 坑 13
- **真实游戏标定**（2026-08-12，台式机上永劫无间实战中采 60 秒，16702 帧 / 4.33 MB）：
  暴露出两个判读指标标定错误，**都是只有真高帧率游戏才会显形的**
  1. **卡顿判据只用「2× 中位数」在高帧率下严重虚报**：平均 278 fps（中位 3.76ms）时
     2× 才 7.5ms，等于把"掉到 133 fps"也算卡顿，报 61 次/分；人根本感觉不到。
     加 16.7ms（≈ 掉到 60 fps 以下）绝对下限后是 19 次/分，与体感对得上。
     低帧率下仍回落到 2× 中位数，不会把真卡顿放过
  2. **开着撕裂时「节奏不齐」是误报**：实测显示间隔中位数 3.70ms，而 165Hz 屏幕周期
     是 6.06ms —— 帧撕裂着扫出去，根本不按刷新周期量化，原先误报 40.1%。
     现在 `tearing_pct ≥ 50` 时该指标直接给 `None`（不给假数），并在展示里标「允许撕裂」
- 新增 `worst_frames`（最慢 5 帧原样保留）：「最狠一次冻了 0.30 秒」比任何分位数都更
  能说明问题——那次实测最慢一帧 298ms
- **顺带发现的运维坑**：强杀 PresentMon 会留下**仍在活跃录制的孤儿 ETW 会话**，
  它继续抢 ETW 缓冲，导致后续采集丢事件（实测丢 48427 个）甚至完全拿不到 CSV。
  生产参数里的 `--stop_existing_session` 对同名会话可自愈；清理用
  `logman stop <名> -ets`
- 打包产物 **50.7 MB**（PresentMon 只加了约 1 MB）；最小 PATH 自检通过；
  `--selftest` 报 `presentmon: ...\_MEI283202\PresentMon.exe`，
  `--cli frames --check` 能真正拉起 `_MEI` 里的采集器并正确报权限不足
  → **打进 onefile 后可被找到且可执行**已验；火绒是否拦截仍待台式机

## [v0.13.0] — 2026-08-01

**改了什么**

计时改判为「按进程真实在跑的时间算」。起因是 08-01 现场：上限 60 分钟，玩了 31.5 分钟就关掉游戏，47 分钟后想接着玩——被 20 小时冷却挡住，剩下的 28.5 分钟额度凭空作废。系统把"关了一次游戏"当成"这一场玩满了"。

- **新概念「一段游玩」(block)**：一段 = 若干次进程起停，中间空闲 ≤ `IDLE_GRACE_MINUTES`（60 分钟）就算同一段
  - 额度按**真实在跑时间**消耗，中途退出即暂停、剩余保留；再打开接着用同一份额度，**不重新发满、不查冷却**
  - 这一段结束的判据只有两个：**额度耗尽**，或**空闲超 60 分钟**。结束后冷却才从**最后退出时刻**起算
  - 允许时段与下次可玩日是硬边界，续玩照查——22:00 到点就是不许再开
  - 全局「每天最多几款」天然不受影响：续玩的是今天已经玩过的那款，本来就放行
- **心跳计时**：守护每轮把观测到的在跑时间累计进 `sessions.played_seconds`，单轮最多计入 `HEARTBEAT_MAX_GAP`（3 秒）
  - **cap 是关键**：守护崩掉/机器睡眠的空窗期没人观测到进程在跑，不能算成"你在玩"。改造前那段全被计入，睡一觉醒来额度就没了
  - 会话结束时间取 `last_seen_ts`（最后一次确认进程存活的时刻）而非"现在"：守护重启后发现进程早没了，不会把空窗算成游玩，也不会把冷却起算点推后几小时
- **顺带**：`daemon.log` 加轮转（2MB × 3）+ "已有守护进程实例在运行"降到 debug 级——SYSTEM 计划任务每分钟拉一次自愈，这条正常状态把日志刷到 780KB
- GUI：新增青色状态「可接着玩 · 本段还剩 28 分钟」，副注给出这段何时作废；游玩记录里同段第 2 次起标「└ 接着玩」，免得看着像冷却失效
- CLI：`next <exe>` 查看时会报告"上一段没玩完"及剩余额度与作废倒计时；`history` 显示真实游玩时长与段号

**关键改动文件**
- `gamelimiter/db.py`（`sessions` 加 `block_id`/`played_seconds`/`last_seen_ts` 三个迁移列 + `current_block`/`heartbeat`/`session_played`）、`gamelimiter/rules.py`（`block_alive`/`block_remaining`，`session_deadline` 改按累计游玩秒数算剩余，`check_start` 加 `resuming` 跳过冷却）、`gamelimiter/daemon.py`（心跳 + 续段判定 + 收养用 `last_seen_ts` + 日志轮转）、`gamelimiter/changes.py`（缩短额度的下限按段累计算）、`gamelimiter/gui.py` / `cli.py` / `tray.py`、`tests/test_blocks.py`（新增）
- **老数据兼容**：`played_seconds` 为空退回墙钟差，`block_id` 为空各自成段——历史记录照常显示，不做回填

**验证**
- 六个测试文件全过。新增 `test_blocks.py` 直接复现 08-01 场景：玩 31.5 分钟退出 → 47 分钟后 `block_alive` 为真、`check_start(resuming=True)` 放行（对照：不续玩则被冷却拦）→ 剩余额度 28.5 分钟、deadline 不是重发 60 → 玩满 60 分钟后这段结束、冷却从真实退出时刻起算
- 覆盖边界：空闲 59 分钟算同一段 / 61 分钟作废、额度耗尽即结束（哪怕刚退出）、本次额度比上限更严时按额度判耗尽、不限时长只受空闲窗口约束、心跳缺失不计入且不推后冷却、老数据退回墙钟差
- 端到端（真守护 + notepad，上限压到 30 秒）：玩 11.1 秒关掉 → 立刻再开被并入同一段（不是新场）、剩余 18.9 秒接着扣 → 累计 30.4 秒到点强杀 `session_timeout` → 再开被 `cooldown` 拦截。脚本 `tests/e2e_block.py`
- GUI 截图三态：可接着玩（青色，含作废倒计时）/ 冷却中 / 现在可玩

## [v0.12.0] — 2026-07-26

**改了什么**
- 规则 a 加第二道门「**下次可玩日**」：玩完直接指定下次哪天能玩（卡片上选日期或点「明天 / 后天 / 周六」），那天之前一律打不开；到了那天，能不能开、开多久仍由允许时段与单次最长决定
  - **与 `cooldown_hours` 独立叠加、不替代**：冷却管"同一天别连着再来一把"（小时级），日期管跨天规划。只想用日期就把冷却清空
  - **过期即失效，不用手动清**：锁到 8/2，8/2 玩过之后这条自动不再约束，回到冷却兜底
  - 解锁时刻取"那天 0:00"，有时段规则则顺延到那天第一个时段起点（跨午夜时段覆盖 00:00 时仍是零点）
  - 判定排在冷却之前：跨天锁定比"还差几小时"更能说明问题
  - **冷静期照旧**：往后推 = 收紧立即生效；提前或清除 = 放宽，24 小时后生效、期间可取消。**已知局限**：因为延迟是固定 24 小时，"周三想把周六改成今天"最快周四解锁——挡得住当下冲动，挡不住提前一天决定破戒。没做成"提前解锁必须等到原定日期"，那样设错日期会把自己彻底锁死、没有后悔通道
- GUI：卡片加「下次可玩」行（日期框 + 日历弹窗 + 明天/后天/周六/清除快捷键），锁定期状态为紫色徽标「锁定中 · 07-30（周四）19:00 开放 · 还有 4 天」；日期可直接手敲（不依赖日历弹窗），格式错误就地提示
- CLI：`set <exe> --until 2026-08-02 | +3 | off`；`list` 显示下次可玩日与是否已过

**关键改动文件**
- `gamelimiter/rules.py`（`unlock_datetime` + `check_start` 日期门）、`gamelimiter/db.py`（`games.next_allowed_date` 迁移列）、`gamelimiter/changes.py`（ISO 日期串直接比大小判收紧/放宽）、`gamelimiter/gui.py`（卡片日期行 + 锁定态徽标）、`gamelimiter/cli.py`（`--until`，支持 `+N` 天）、`tests/test_rules.py` + `tests/test_changes.py`

**验证**
- 五个测试文件全过；新增覆盖：锁定期内被拦且 `unlock_ts` 正确、有时段时顺延到时段起点、跨午夜时段仍是零点、到了那天/日期已过放行、日期门优先于冷却报告而日期到了仍报冷却、四种收紧/放宽分类、提前入队 + 往后推撤销申请、只设日期的游戏删除也走延迟
- CLI 实测：`--until +3` 立即生效 → `+1`（提前）入队 24h → `+5`（往后）立即生效并撤销那条提前申请
- 端到端（隔离库）：锁到 3 天后 → notepad 启动被拦杀并记 `locked_until_date` 事件；把日期改成今天 → 正常放行
- GUI 截图：紫色锁定徽标 + 「还有 4 天」+ 日期行与快捷键；未设时显示「还没定下次」

## [v0.11.0] — 2026-07-26

**改了什么**
- 新增**全局规则 (d)「每天最多玩几款游戏」**——前三条都是按游戏配的，这是第一条跨游戏的总量规则
  - 语义：一天内最多开几款**不同**的游戏，自然日 0:00 起算。**今天已经玩过的那几款不受影响**（继续玩自己的），只挡今天还没碰过的新游戏——否则把数值调小会连当天正在玩的一起锁死
  - 跨午夜的会话两头都算（凌晨 1 点还在玩，就占今天一个名额），与统计口径一致；进行中会话按"到此刻为止"算，否则查未来区间会一直命中
  - 判定放在冷却/时段之前：挡的是"今天又开一款新的"，这个理由比冷却更能说明问题
  - **改这条同样受冷静期管制**：调小 / 从不限改成 N = 收紧，立即生效；调大 / 取消 = 放宽，24 小时后生效，期间可随时取消
- 存储加 `settings` 表（key-value 全局设置）；全局项在 `pending_changes` 里借 `game_id=0` 落座（`games.id` 从 1 起，不会撞），`apply_due` / `describe_pending` 分流处理
- GUI 顶部新增全局规则条：「每天最多玩 [N] 款游戏 · 今天已玩 X 款：A、B」，用满时转琥珀色提示"新游戏今天打不开"，待生效放宽就地显示可取消
- CLI 加 `daily [N|off]`（留空查看，含待生效项）；`list` 顶部显示全局限制与今日已玩；`pending` 里全局项显示为「全局」

**关键改动文件**
- `gamelimiter/rules.py`（`day_bounds` + `check_daily_limit`）、`gamelimiter/db.py`（`settings` 表 + `get/set_daily_game_limit` + `games_played_between`）、`gamelimiter/changes.py`（`GLOBAL_GAME_ID` + `request_daily_limit` + 全局分流）、`gamelimiter/daemon.py`（启动前置判定）、`gamelimiter/gui.py`（`global_rule_view`）、`gamelimiter/cli.py`、`tests/test_rules.py` + `tests/test_changes.py`

**验证**
- 五个测试文件全过；新增覆盖：已玩过的那款照常放行、新游戏被拦、上限收紧到小于今日已玩数不追溯锁死、`unlock_ts` 为次日 0:00、收紧即时 / 放宽入队 / 到期落地 / 改主意变严撤销申请、跨午夜会话两天都算而未来区间不命中
- 端到端（隔离库）：上限 1 款 → 记事本放行、画图被拦且进程被杀（events 记 `daily_game_limit`）、记事本二次启动照常放行
- GUI 截图：全局条显示「每天最多玩 2 款 · 今天已玩 1 款：记事本」+ 待生效「改 3 款，07-27 13:53 生效」及取消按钮

## [v0.10.0] — 2026-07-26

**改了什么**
- 规则 b 从「单次时长」改为「**单次最长时长（上限）+ 本次额度**」：每次开玩前可把这一次单独调低（上限 2 小时、这次只玩 1 小时），30 分钟一档加减或直接输入
  - **交互放在启动前，不在游戏启动时弹窗选**：独占全屏下 `MessageBox` 可能压根不显示，且强制层启用后守护跑在 SYSTEM 会话 0，跨会话只能用 `WTSSendMessage`（做不了多选）。预设方案完全绕开这颗雷
  - 额度只能 ≤ 上限 → 永远不构成放宽 → 立即生效，不进 24h 待生效队列；改上限本身照旧走延迟
  - **游玩中只许再缩短，不许加时**（"玩到一半改回 3 小时"正是要拦的冲动），且缩短后至少留 10 分钟预警缓冲——无预警强杀 PVP 会判逃跑
  - 额度在守护开会话时消费掉（同时清内存缓存，避免秒退再进重复用），快照进 `sessions.limit_minutes`；GUI/CLI 中途改动由守护每 5 秒读回
- 预警去重修复：额度短于最大预警档时（如本次只玩 5 分钟），10/5/1 三档会连着三秒各弹一次窗——改为一次标记掉所有已跨过的档，只弹最紧急的那条。游戏里被连弹三次是灾难，短额度让这个老问题变成高频
- 托盘 tooltip / 菜单首行加「正在玩什么 · 剩余多少分钟」（额度已算进去）

**关键改动文件**
- `gamelimiter/rules.py`（`effective_limit` + `session_deadline(..., limit_minutes)`）、`gamelimiter/changes.py`（`set_next_session` / `shorten_running_session`）、`gamelimiter/db.py`（`games.next_session_minutes` + `sessions.limit_minutes` 两个迁移列）、`gamelimiter/daemon.py`（消费额度 / 会话内刷新 / 预警去重）、`gamelimiter/gui.py`（卡片额度行）、`gamelimiter/cli.py`（`next` 子命令）、`gamelimiter/tray.py`、`tests/test_rules.py` + `tests/test_changes.py`

**验证**
- 五个测试文件全过；新增用例覆盖 `effective_limit` 取严、额度超上限被拒且不改动现值、加时/取消/缓冲不足三种拒绝、额度消费后落到会话行
- 端到端（隔离库 `PROGRAMDATA` 覆盖 + notepad）：上限 2 分钟 / 本次额度 1 分钟 → 日志 `会话开始：记事本（本次额度 1 分钟），截止 13:11:26`，13:11:26 准时终止，走的是额度不是上限
- 端到端（预警档压成 1 分钟跑快版）：会话中途 3 → 1.5 分钟被守护读回并重算 deadline，实际玩 1.50 分钟后 `session_timeout` 终止；同一会话内加时请求被拒
- GUI 截图：可玩卡片显示「下次玩 −60+ 分钟 · 用满上限」，游玩中卡片显示「本次玩 −90+ 分钟」且隐藏「用满上限」，状态副注「本次已玩 41 分钟 · 本次额度 90 分钟」，剩余时间按额度算

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
