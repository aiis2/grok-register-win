# 隐藏有头浏览器启动加固设计

## 背景与问题

现有 `hidden` 模式已经使用标准有头 Chromium，并在 CDP 创建后台最小化窗口后，
按精确 PID/HWND 调用 Win32 API 隐藏窗口和移除任务栏入口。它能够保证后续窗口控制
不会误伤用户自己的浏览器，但浏览器原生窗口从创建到首次 Win32 隐藏之间仍有一个
短暂时间窗。Windows 或 Chromium 在这个时间窗内可能显示窗口、创建任务栏按钮，
甚至把浏览器激活到前台，因此仍会出现闪烁和当前应用失焦。

本次借鉴“在 Chromium 创建首个窗口前就指定屏幕外坐标”的思路，但不复制第三方代码、
不引入外部浏览器管理器，也不以屏幕外坐标替代现有 Win32 所有权控制。目标是把启动前、
CDP 创建和原生窗口接管三个阶段组合成分层防护。

## 用户确认的范围

1. 直接升级已有 `hidden` 模式，不增加新的配置项或 UI 模式。
2. 浏览器仍是标准有头 Chromium，不添加 `--headless`，也不回退成无头模式。
3. 保留每个并发 worker 独享浏览器、批次内健康复用和异常时精确重建的生命周期。
4. 保留面板对同一 PID/HWND 的“显示浏览器”和“隐藏浏览器”能力。
5. 不依赖 BitBrowser 或其他外部 Profile/窗口管理服务。

## 采用方案：分层隐藏

### 第一层：进程创建前的屏幕外初始位置

`bootstrap_hidden_chromium()` 在 `hidden` 模式专用启动参数中规范化并追加
`--window-position=-32000,-32000`。如果调用方已经传入同名参数，先移除旧值再追加项目
控制的值，避免重复参数导致不同 Chromium 版本取值不一致。

Windows 上同时向 `subprocess.Popen` 传入 `STARTUPINFO`，设置
`STARTF_USESHOWWINDOW` 和 `SW_HIDE`。该标志是尽力而为的第一道防线；Chromium 的多进程
窗口创建可能不完全遵循它，因此不能单独作为成功标准。

### 第二层：CDP 后台创建

继续通过 `Target.createTarget` 创建标准原生窗口，并保持：

- `newWindow=true`
- `background=true`
- `focus=false`
- `windowState=minimized`
- `left=-32000`
- `top=-32000`

CDP 参数与启动参数同时指定屏幕外位置，用于覆盖不同 Chromium 版本对首个窗口默认位置
处理的差异。任何参数被浏览器拒绝时，沿用现有精确清理和 `minimized` 兼容回退，不在失败
路径创建第二个不受控隐藏浏览器。

### 第三层：精确 HWND 接管

找到属于本次 Popen PID 的 `Chrome_WidgetWin_*` 顶层 HWND 后：

1. 再次校验 HWND 当前 PID；
2. 添加 `WS_EX_TOOLWINDOW` 并移除 `WS_EX_APPWINDOW`；
3. 使用包含 `SWP_NOACTIVATE`、`SWP_HIDEWINDOW` 和 `SWP_FRAMECHANGED` 的
   `SetWindowPos` 请求隐藏及刷新任务栏样式；
4. 检查 `IsWindowVisible`，只有窗口确实不可见才返回成功；
5. 失败时恢复原扩展样式并触发现有回退流程。

自动启动、重启和隐藏路径均不得调用 `SetForegroundWindow`。

## 显式显示与屏幕位置恢复

屏幕外启动会使首次恢复的窗口仍位于不可见区域。用户在面板明确点击“显示浏览器”时，
控制器先恢复同一 HWND，然后读取 `GetWindowRect`：

- 窗口矩形与 Windows 虚拟桌面有交集时，保留用户上次位置；
- 窗口完全位于虚拟桌面外时，将其无激活地移动到主屏工作区内的安全位置；
- 只有调用方请求 `activate=true` 时，最后才调用 `SetForegroundWindow`。

再次隐藏不把窗口重新移动到屏幕外。这样，后续显式显示可以保留用户调整过的位置；首次
启动期的不可见性仍由 Chromium 初始坐标和 Win32 隐藏共同保证。

## 错误与兼容策略

- 启动参数中出现任何 `--headless*` 继续立即拒绝。
- Windows 隐藏启动失败时终止本次捕获的精确进程树，再按现有逻辑回退到
  `minimized`，不触碰其他 Chrome/Edge 进程。
- `ShowWindowAsync` 的返回值表示窗口调用前的可见状态，不能作为操作是否成功的判断；
  显示结果改用限时 `IsWindowVisible` 校验。
- HWND 消失、PID 所有权变化、Win32 操作失败或显示状态超时均返回结构化错误。
- 非 Windows 平台不会进入 `hidden` 启动路径，现有模式规范化行为保持不变。

## 测试与验收

### 自动化测试

1. `hidden` 启动参数只保留一个项目控制的屏幕外坐标，且没有 `--headless`。
2. Windows Popen 使用隐藏 `STARTUPINFO`；非 Windows 构造逻辑不附加该参数。
3. CDP 创建参数包含后台、无焦点、最小化和屏幕外坐标。
4. 隐藏操作包含 `SWP_NOACTIVATE | SWP_HIDEWINDOW`，不调用前台激活 API。
5. 隐藏失败恢复原任务栏扩展样式。
6. 显示操作不依赖 `ShowWindowAsync` 的历史可见状态返回值。
7. 仅屏幕外窗口在显式显示时被移回可见工作区；已在屏幕内的窗口位置保持不变。
8. PID/HWND 所有权、浏览器复用、失败回退和进程树清理测试继续通过。

### Windows 实机验证

使用独立调试端口和临时 Profile 启动一个测试浏览器，高频采样本次浏览器 HWND：

- 从启动前到隐藏完成期间不得成为前台窗口；
- 隐藏完成后 `IsWindowVisible=false`，并且扩展样式没有 `WS_EX_APPWINDOW`；
- 显式显示后仍是同一 PID/HWND，且窗口位于虚拟桌面内；
- 再次隐藏后窗口不可见，浏览器进程和 CDP 目标仍存活；
- 测试退出后精确进程树和临时 Profile 可清理。

如果操作系统或本机安全策略导致前台采样无法得出确定结论，发布说明只能陈述已降低闪烁
与失焦风险，不能宣称所有 Windows 版本绝对“零抢焦点”。
