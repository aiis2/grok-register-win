# Windows 有头浏览器隐藏窗口设计

## 背景

当前 Windows 有头 Chromium 已使用 `--start-minimized`，并在 DrissionPage
连接完成后再次调用 Win32/DrissionPage 最小化窗口。这个处理可以缩短窗口停留
时间，但 Chromium 的顶层窗口已经在交互桌面上完成创建，因此仍可能短暂抢占
焦点、闪烁并在任务栏出现。

当前注册架构已经做到“一个并发槽在同一批任务内复用一个浏览器”：正常账号切换
只清理 Cookie、缓存和页面状态，浏览器失联、状态清理失败、Turnstile Profile
污染或单轮硬超时时才重启。任务结束后 CLI worker 和其浏览器退出。本设计不引入
跨任务常驻进程。

## 用户确认的目标

1. 使用标准有头 Chromium，不允许自动降级成无头模式。
2. 默认隐藏浏览器原生窗口，不显示在任务栏，也不主动改变当前前台窗口。
3. Web 面板按 worker 提供“显示浏览器”和“隐藏浏览器”按钮。
4. 显示的是正在注册的同一个 HWND、页面和 Profile，不复制、不重启页面。
5. 当前注册任务完成或停止后立即退出 worker 和浏览器。
6. 正常轮次继续复用浏览器，仅在确认错误时重建，并且重建后仍保持隐藏。

## 方案比较与选择

### 方案 A：同桌面静默创建并隐藏原生窗口（采用）

Chromium 先以 `--silent-launch` 启动有头进程，不创建默认启动窗口；随后通过 CDP
创建 `background=true`、`focus=false` 且初始为最小化状态的标准新窗口。窗口句柄
出现后立即由 Win32 控制器隐藏，并调整任务栏相关扩展样式。用户点击面板按钮时，
恢复同一个窗口；点击隐藏时再次隐藏。

优点：保持有头特征，能够在当前桌面显示同一个实时页面，且无需跨任务驻留。
风险：不同 Chrome/Edge 版本对静默启动和窗口样式的实现可能存在差异，因此必须
设置兼容回退和 Windows 实机发布门槛。

### 方案 B：隔离 Windows Desktop（不采用）

把 Chromium 从创建时放入非交互 Desktop 可以提供更强的视觉隔离，但已有 HWND
不能直接移动回当前 Desktop。查看时需要切换整个 Windows Desktop，不符合“面板
按钮显示当前原生窗口”的交互要求。

### 方案 C：跨任务常驻 worker（不采用）

跨任务保留浏览器只能减少首次启动次数，无法消除错误恢复导致的重启，而且会引入
闲置资源、陈旧 Profile 和配置变化问题。用户已经确认任务结束后立即退出。

## 配置与兼容策略

新增配置 `browser_window_mode`：

- `hidden`：Windows Chromium 默认值，使用方案 A。
- `minimized`：保留当前最小化逻辑，作为显式兼容模式和隐藏启动失败回退。
- `visible`：保持普通有头窗口，便于诊断。

Camoufox 仍为无头引擎，不提供原生窗口控制。非 Windows 平台读取到 `hidden` 时，
统一规范化为 `visible`，沿用普通 Chromium 有头窗口行为。

隐藏模式不得添加 `--headless`。运行时和测试必须验证 User-Agent 不包含
`HeadlessChrome`、DrissionPage 报告有头模式，并且存在属于本任务浏览器 PID 的真实
顶层 HWND。

## Chromium 启动流程

1. 为每个 worker 继续分配独立调试端口范围和临时 Profile。
2. 使用项目内的受控启动适配器调用 Chromium，不修改虚拟环境中的 DrissionPage
   源文件。
3. `hidden` 模式向 Chromium 添加 `--silent-launch`，保留禁用后台计时器和遮挡窗口
   节流的参数，不添加 `--headless`。
4. 启动适配器取得本次创建的精确 Popen PID，并等待浏览器级 CDP 端点。
5. 通过 `Target.createTarget` 创建标准窗口，设置 `newWindow=true`、
   `background=true`、`focus=false` 和 `windowState=minimized`。
6. Win32 窗口控制器按本次浏览器 PID 解析 HWND，立即隐藏并移除任务栏入口。
7. DrissionPage 连接该浏览器和目标页，记录浏览器 PID、启动器 PID、调试地址、
   Profile、HWND、窗口模式和 generation。
8. 任一步不受当前 Chrome/Edge 支持时，关闭本次未完成的精确进程树，再以
   `minimized` 模式启动；日志和面板必须显示发生了兼容回退。

不得对 DrissionPage 的全局安装文件做补丁，也不得按进程名、窗口标题或模糊路径
控制用户自己的 Chrome/Edge。

## 窗口所有权与面板控制

每次成功启动或重启后，CLI 输出无敏感信息的结构化浏览器标记，至少包含：

- worker id
- generation
- browser PID
- HWND
- `hidden|minimized|visible|closed|error` 状态
- 实际窗口模式及是否发生兼容回退

父面板只接受自己当前登记 worker 输出的标记，并把状态写入对应 worker 卡片。
面板新增：

- `POST /api/job/workers/<worker_id>/browser/show`
- `POST /api/job/workers/<worker_id>/browser/hide`
- 可选的“隐藏全部浏览器”操作

执行窗口操作前必须重新验证：

1. 当前任务仍在运行；
2. worker 仍是当前登记的进程；
3. HWND 仍有效；
4. HWND 的 PID 与登记 browser PID 一致；
5. generation 未被一次浏览器重启替换。

显示一个 worker 前，面板先隐藏其他已登记窗口，避免多个浏览器同时覆盖桌面。
“显示”是用户明确触发的操作，因此可以恢复并激活窗口；所有自动启动、恢复和重启
路径只能隐藏，不得调用前台激活 API。隐藏后更新 worker 状态，但不影响页面、PID、
调试端口或 Profile。

## 生命周期与错误处理

- 正常账号切换：继续调用 `prepare_browser_for_next_account()`，PID/HWND 不变。
- 页面或 CDP 失联：关闭精确拥有的进程树，再隐藏启动新浏览器，generation 加一。
- Turnstile Profile 确认污染：允许重建浏览器，但不得自动显示窗口。
- 隐藏控制失败：记录错误并回退到 `minimized`，注册任务可以继续。
- 显示/隐藏请求命中陈旧 HWND：返回 HTTP 409，不触碰任何其他窗口。
- worker 结束、任务停止或超时：清除面板中的窗口登记并终止精确拥有的浏览器树。
- 面板退出或异常时仍由现有 worker 进程树监督负责最终清理。

错误状态只在面板高亮并提供“显示浏览器”按钮，不自动弹出窗口。这样错误恢复不会
再次造成用户当前窗口失焦。

## UI 设计

浏览器引擎旁新增“窗口模式”选择：

- 隐藏运行（推荐）
- 最小化兼容
- 正常显示

运行时锁定该配置。每个 Chromium worker 卡片显示：

- 浏览器状态与 generation
- 浏览器 PID
- 实际模式及兼容回退提示
- “显示浏览器”或“隐藏浏览器”按钮

Camoufox worker 不显示窗口按钮。任务停止或 worker 尚未创建浏览器时按钮禁用。

## 测试与发布门槛

### 单元与集成测试

- 配置规范化、保存、回填以及任务启动快照。
- hidden 模式没有任何 `--headless` 参数，并包含静默启动和后台节流参数。
- CDP 创建目标使用 `background=true`、`focus=false`、`windowState=minimized`。
- Win32 控制器仅操作 PID/HWND 同时匹配的窗口。
- 陈旧 HWND、PID 复用、worker 重启和 generation 变化均拒绝操作。
- 显示一个 worker 时先隐藏其他 worker。
- 浏览器结构化标记能正确更新面板状态。
- 正常多轮复用不重启；失联和确认污染才重启；任务结束必定关闭。

### Windows 实机验证

当前用户正在运行的注册任务不应被停止或干扰。实现完成后使用独立端口/Profile
验证本项目新启动浏览器自身的行为：

1. 高频采样新浏览器及其进程树是否曾成为前台窗口。
2. 采样窗口可见性和任务栏样式，确认 hidden 状态不可见且无任务栏入口。
3. 点击面板显示后确认同一 PID/HWND、URL 和 DOM 状态保持不变。
4. 再次隐藏后确认任务继续运行。
5. 强制一次受控重启，确认新 generation 默认仍为 hidden。
6. 退出测试后确认独立测试 Profile 对应进程数回到零。

若活动注册任务造成前台采样噪声，只判断“本次测试浏览器是否成为前台”，不得把其他
worker 的窗口变化归因于本次测试。不能取得可靠实机证据时不得宣称“零闪烁”。

### 回归与发布

- 浏览器、Turnstile、并发 worker 和面板设置相关测试通过。
- 全量 pytest、Python 编译检查和 `git diff --check` 通过。
- README 说明三种窗口模式、面板控制和兼容回退。
- 新建 release 文档并更新 GitHub Release，说明该功能由 `aiis2` 集成。
- 仅提交任务相关文件，不提交 `config.json`、凭据、日志、临时 Profile 或用户批处理文件。

