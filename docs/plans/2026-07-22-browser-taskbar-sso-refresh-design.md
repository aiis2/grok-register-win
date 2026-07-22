# 隐藏有头浏览器与 SSO 全量换票设计

## 背景

当前 Windows `hidden` 模式已经使用屏幕外启动、`STARTUPINFO/SW_HIDE`、
`WS_EX_TOOLWINDOW`、移除 `WS_EX_APPWINDOW` 和无激活隐藏，但真实运行时仍可能在
任务栏留下 Chromium 入口。新版面板同时只提供“补转缺失 CPA”，没有针对全部已有
SSO 重新换取 OAuth/CPA 凭据的明确操作。

## 运行时证据与根因

使用独立 Chrome、临时 Profile 和未占用 CDP 端口执行原生探针后，同一浏览器 PID
同时存在多个 `Chrome_WidgetWin_*` 顶层窗口：

- 当前选择器首先命中隐藏的 `Chrome_WidgetWin_0` 内部窗口；
- 真正的浏览器主窗口是仍处于可见状态的 `Chrome_WidgetWin_1`；
- 主窗口虽然位于 `-32000,-32000`，但 Windows Shell 仍会为可见顶层窗口保留任务栏
  入口；
- 对实际 `Chrome_WidgetWin_1` 执行现有隐藏控制后，其可见状态变为 false，添加
  `WS_EX_TOOLWINDOW`、移除 `WS_EX_APPWINDOW`，同一 PID 下可见浏览器窗口归零。

因此问题不是有头模式本身，而是 HWND 选择策略过宽，隐藏了内部窗口而非主窗口。

## 目标

1. Windows Chromium `hidden` 模式不显示浏览器窗口，也不留下运行中的任务栏入口。
2. 自动启动、隐藏和复查路径不主动抢占前台焦点。
3. 显示/隐藏操作仍复用同一 PID 与主 HWND，不重建浏览器或丢失页面状态。
4. 新版和经典界面均提供“刷新全部 SSO 换票”，支持最多 10000 个账号。
5. 换票成功后原子替换 CPA；失败时保留原 CPA，并记录可诊断错误。

## 非目标

- 不改成 headless，也不引入第三方浏览器管理器。
- 不操作其他 PID、其他 Profile 或用户自己的 Chrome/Edge 窗口。
- 不生成新的 Web SSO；“刷新”仅使用现有 SSO 重新换取 OAuth/CPA 凭据。
- 不并行执行大量换票；继续复用单 worker 队列，避免触发上游限流。

## 浏览器窗口设计

### 主窗口识别

`WindowsBrowserWindowController` 对目标 PID 的候选窗口进行排序：

1. `Chrome_WidgetWin_1` 优先于内部 `Chrome_WidgetWin_0`；
2. 可见窗口优先；
3. 非 `WS_EX_TOOLWINDOW` 窗口优先；
4. 有有效面积的窗口优先；
5. 最后使用稳定的 HWND 顺序保证结果可重复。

窗口所有权继续使用 PID + HWND 双重验证。没有合格主窗口时返回 0，由现有隐藏启动
失败和最小化兼容回退处理。

### 隐藏稳定期

首次隐藏主窗口后，在一个短且有上限的稳定期内重新枚举相同 PID 的
`Chrome_WidgetWin_1`。若 Chromium 延迟创建或替换了主窗口，则对新 HWND 应用同样的
无激活隐藏。稳定期只关注精确 PID，不按进程名或标题模糊匹配。

最终成功条件为：已捕获至少一个主窗口，且该 PID 下不存在可见的
`Chrome_WidgetWin_1`。隐藏结果返回最终主 HWND，供面板后续显示/隐藏复用。

### 显示与再次隐藏

手动显示恢复 `WS_EX_APPWINDOW`、移除 `WS_EX_TOOLWINDOW`，使用
`SW_SHOWNOACTIVATE` 或显式用户请求下的 `SW_RESTORE`，并在需要时把屏幕外窗口移回工作
区。再次隐藏仍使用同一 HWND；如果主窗口被 Chromium 替换，控制层先按当前 PID 重新
解析主 HWND，再执行操作。

## SSO 全量换票设计

### 后端队列

新增 `enqueue_all_sso_refresh(limit)`，遍历当前凭据工作区的唯一账号：

- 最多处理 1–10000 个带有效 SSO 的账号；
- 已在 `_cpa_inflight` 中的 SSO 始终跳过，防止重复点击造成重复任务；
- `force=True` 只绕过 `_cpa_done`，不会绕过 inflight 去重；
- 返回 `total`、`queued`、`skipped` 及跳过原因统计；
- 继续串行使用现有 CPA worker。

CPA 成功输出改用现有 `_write_json_atomic()`。转换失败只更新失败计数和
`failed.jsonl`，不删除或覆盖已有 CPA 文件。

### API

新增 `POST /api/cpa/refresh-all`：

```json
{"limit": 1000}
```

成功返回：

```json
{
  "ok": true,
  "total": 120,
  "queued": 118,
  "skipped": 2,
  "message": "已将 118 个 SSO 加入重新换票队列"
}
```

以下情况拒绝执行并返回明确错误：注册任务运行中、凭据导入/迁移切换中、转换核心不可
用、当前工作区没有有效 SSO。重复点击时只跳过已在执行的项，不重复入队。

### UI

新版“凭据存储 → CPA 转换”增加“刷新全部 SSO 换票”按钮，与现有“扫描并补转缺失
CPA”并列。点击后使用现有确认模态框说明：

- 不生成新的 Web SSO；
- 会对当前账号池全部 SSO 重新换取 OAuth/CPA；
- 成功原子替换，失败保留旧 CPA；
- 大批量任务在后台串行执行，可在实时日志中查看结果。

经典界面同步增加同等按钮，避免 `?ui=legacy` 回退后丢失功能。两套界面共用同一 API
和后端状态，不复制业务逻辑。

## 错误处理与并发边界

- 注册运行中禁止全量刷新，避免账号文件持续变化。
- 凭据导入或迁移持锁时返回 409，不等待锁造成请求挂起。
- CPA worker 已运行时允许追加未在 inflight 的账号；现有任务不会重复排队。
- API 不返回 SSO、密码、Access Token 或 Refresh Token。
- 页面只显示计数和脱敏错误；详细信息进入现有脱敏日志流。

## 验证方案

1. 单元测试证明窗口排序优先选择真实 `Chrome_WidgetWin_1`，而不是先枚举到的内部窗口。
2. 单元测试证明隐藏稳定期会处理延迟出现的主窗口，且不触碰其他 PID。
3. Windows 独立 Chrome/Profile 探针验证隐藏后：主 HWND 不可见、带
   `WS_EX_TOOLWINDOW`、不带 `WS_EX_APPWINDOW`，目标 PID 下可见主窗口为 0。
4. 原生探针验证显示与再次隐藏复用同一 PID/HWND，自动路径前台窗口采样为 0。
5. API 测试覆盖 10000 上限、空工作区、注册忙、重复 inflight、成功统计和敏感字段
   不回显。
6. worker 测试覆盖成功原子替换与失败保留旧 CPA。
7. 新版与经典 UI 契约测试覆盖按钮、确认说明和 API 调用。
8. 真实浏览器验证新版按钮可见、确认/取消、错误展示、队列状态刷新与控制台无错误。
9. 完整 pytest、JavaScript 语法、Python compileall 和 `git diff --check` 全部通过。

## 发布

完成后快进合并到 `master`，推送 `aiis2/grok-register-win`，等待 GitHub Actions 通过，
再发布 `v1.9.0`。Release 只描述本项目新增的窗口识别、任务栏隐藏和 SSO 全量换票能力，
不包含真实凭据或外部参考说明。
