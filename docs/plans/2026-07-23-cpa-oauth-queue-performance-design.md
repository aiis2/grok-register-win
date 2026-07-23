# CPA OAuth Queue 并行性能设计

## 背景

面板当前使用一个全局 `queue.Queue` 和单个 `cpa-worker` 线程，把 Web SSO 顺序兑换为
CLIProxyAPI xAI OAuth/CPA 凭据。注册可以并发执行，但 CPA 兑换始终串行；当批量注册或
“刷新全部 SSO”快速产生任务时，注册完成后仍会留下较长的 CPA 队列尾延迟。

现有单 worker 同时承担两类职责：

1. 调用 `convert_one()` 完成 authorize、consent、token 三段网络请求；
2. 写入 CPA JSON、更新 `index.json`、追加失败记录并维护队列状态。

网络请求占主要耗时，而落盘使用共享文件。这两类工作需要拆开设置不同的并发边界。

## 基线证据

在不重复兑换凭据的前提下，对当前运行队列最近 120 个真实完成事件统计：

- 120 个成功、0 个失败；
- 总耗时 584 秒，吞吐 12.23 个/分钟；
- 平均完成间隔 4.91 秒，中位数 5 秒，P95 为 8 秒；
- 注册任务已经空闲时，CPA 仍有 136 条待处理，预计单 worker 尾延迟约 11 分钟。

`lib/sso2cpa_core.py` 的每次 `convert_one()` 都创建独立 Session，并生成独立 PKCE
verifier、state 和 nonce。因此 OAuth 网络阶段没有共享 Cookie/Session，可以并行。

使用临时凭据目录和合成 OAuth 延迟运行现有 worker：

| worker 数 | 中位吞吐 | CPA 文件 | 完整索引 |
| --- | ---: | ---: | ---: |
| 1 | 9.63/秒 | 24/24 | 24/24 |
| 2 | 19.23/秒 | 24/24 | 最低 18/24 |
| 4 | 37.48/秒 | 24/24 | 最低 15/24 |

实验确认网络阶段可以接近线性扩展，也确认不能直接启动多个现有 worker：
`save_cpa_index_item()` 对 `index.json` 执行未加锁的读取—修改—覆盖，多个线程会互相
覆盖索引。当前布尔值 `active` 也会在任一线程完成时过早变成 false，使凭据迁移误判
CPA 已空闲。

## 目标

1. OAuth 网络兑换支持有界并行，默认并发 2，可配置 1–4。
2. CPA 文件、索引、失败记录和状态更新保持一致，不因并行丢项或交叉写入。
3. 保留 SSO fingerprint 去重、强制刷新、失败保留旧 CPA 和工作区 generation 隔离。
4. 凭据导入/迁移只有在待处理、活跃兑换和待提交都为零时才能开始。
5. 状态 API 和新版 UI 能看到配置并发度、活跃 worker 和待提交数量。
6. 上游限流或 Cloudflare 拦截时有界退避，不以无上限重试放大请求。
7. 并发度设回 1 时行为与现有串行模式兼容。

## 非目标

- 不把 OAuth 客户端重写为 asyncio/httpx。
- 不共享不同账号的 HTTP Session、Cookie、PKCE 参数或 token。
- 不提高到无上限并发，也不让 CPA 并发跟随注册并发直接增长到 10。
- 不在失败时删除或覆盖旧 CPA 文件。
- 不在 API、日志或页面回显 SSO、Access Token、Refresh Token 或密码。

## 方案选择

### 方案 A：直接启动多个现有 worker

网络吞吐可以提升，但 `index.json` 已在隔离实验中稳定丢项，`active` 状态和迁移保护也
会发生竞态，因此拒绝。

### 方案 B：并行 OAuth + 串行提交

使用固定大小的 OAuth worker 池并行调用 `convert_one()`，每个完成结果进入提交队列。
一个提交器按顺序完成文件命名、原子写入、索引更新、失败记录和最终状态变更。

该方案保留当前 curl_cffi 同步客户端，改动集中在队列调度和状态模型，能获得主要网络
并行收益，同时把共享落盘边界保持为单写者。采用此方案。

### 方案 C：全异步 OAuth 客户端

需要替换或包装 curl_cffi Session，重新验证 TLS impersonation、代理、Cookie 和重定向
行为，风险和测试范围明显大于当前性能问题，不采用。

## 队列流水线

```text
enqueue_cpa_convert
        |
        v
OAuth 请求队列 -- fingerprint/generation --> N 个 OAuth worker (1..4)
        |                                      每项独立 Session/PKCE
        v
提交结果队列 <------------------------------ success / failure
        |
        v
单提交器 --> CPA JSON --> index.json --> failed.jsonl --> 状态/日志
```

### 请求项

请求项继续携带 `email`、`sso`、`password`、`source`、`fp`、`force` 和
`workspace_generation`。SSO 只存在于进程内任务和最终凭据文件，不进入状态 API。

### 结果项

OAuth worker 产生内部结果：

- 原始请求项；
- 成功时的 CPA entry；
- 失败时的异常类型和已脱敏错误；
- 开始/完成时间及尝试次数；
- worker 标识。

结果项只有提交器消费。提交器在写入前再次比较 `workspace_generation`，旧工作区结果
必须丢弃，不能落入新凭据目录。

## 状态模型

将 `active: bool` 扩展为以下计数：

- `concurrency`：配置的 OAuth worker 数；
- `active_workers`：正在执行 `convert_one()` 的 worker 数；
- `pending`：尚未被 OAuth worker 领取的请求数；
- `commit_pending`：已完成 OAuth、等待提交的结果数；
- `running`：上述三个计数任一非零；
- `ok`、`fail`：已完成提交的累计结果；
- `last_error`、`last_ok_email`：保持现有兼容字段；
- `active`：兼容旧 UI，派生为 `active_workers > 0`。

所有计数和 `_cpa_done`、`_cpa_inflight` 仍由 `_cpa_lock` 保护。任务只有完成或失败提交后
才从 `_cpa_inflight` 移除，避免“网络已结束但尚未写盘”期间被重复入队。

## 工作区切换

凭据导入和迁移必须满足：

- `active_workers == 0`；
- `commit_pending == 0`；
- 提交器没有正在写盘；
- 请求队列可安全 drain。

开始切换时递增 generation 并清空尚未领取的请求。OAuth worker 已经领取的任务不能被
强杀；切换入口应返回忙，而不是等待或切换。提交器写盘前再次检查 generation，形成
第二道保护。

`credential_change_blocker()` 和上传/迁移 API 使用统一的
`cpa_pipeline_is_busy()` 判定，避免各处分别读取不完整字段。

## 并发配置

- 新增 `cpa_oauth_concurrency`，有效范围 1–4，默认 2。
- 环境变量 `CPA_CONCURRENCY` 作为显式部署覆盖；无覆盖时读取应用配置。
- 配置在启动时决定 worker 池大小。本轮不实现在线缩放，修改后提示重启面板生效，
  避免动态增减线程与 sentinel 造成额外状态复杂度。
- 新版配置页面显示当前值和运行值；旧版 API 与现有 CPA 操作保持兼容。

## 限流与错误处理

- 每个 worker 完成一项后应用 `CPA_DELAY`，延迟是 worker 局部的，不阻塞其他 worker。
- 对 HTTP 429、短暂 5xx、连接超时使用至多两次指数退避；Cloudflare challenge/403、
  SSO 失效和 OAuth 协议错误不盲目重试。
- 如果一定窗口内出现连续限流，后续 worker 共享一个有上限的冷却截止时间。
- 失败结果由提交器追加到 `failed.jsonl`；旧 CPA 文件保持字节不变。
- 错误内容继续经过日志脱敏，不记录 token 或完整 SSO。

## UI 与 API

`/api/cpa/status` 和 `/api/job/status` 的 `cpa` 字段增加：

```json
{
  "concurrency": 2,
  "active_workers": 1,
  "pending": 12,
  "commit_pending": 0,
  "running": true
}
```

新版“凭据存储 → CPA 转换”增加 1–4 的 OAuth 并发配置，并在状态文本显示
“活跃/并发、待兑换、待提交”。保存使用现有配置保存路径，不新增单独的敏感配置文件。
经典 UI 继续显示兼容状态，不移除现有功能。

## 测试方案

1. 先写失败测试，证明多个 OAuth worker 的调用实际重叠，且吞吐高于串行基线。
2. 并发处理 24 个合成任务，验证 CPA 文件和索引均为 24/24，且 JSON 始终有效。
3. 验证同 fingerprint 在请求、兑换和待提交阶段都不能重复入队。
4. 验证成功原子替换、失败保留旧 CPA、失败日志串行追加。
5. 验证 `active_workers`、`commit_pending`、`running` 在乱序完成时仍准确。
6. 验证活跃兑换或待提交期间拒绝迁移；旧 generation 结果不能写入新工作区。
7. 验证并发配置默认值、1–4 边界、环境覆盖和 UI/API 契约。
8. 运行隔离 1/2/4 worker 性能对照，要求 2 worker 相对 1 worker 有明确提升且索引完整。
9. 当前旧队列自然清空后重启面板，以小批量真实 SSO 比较并发 1 与 2 的成功率和耗时；
   不输出邮箱或凭据。
10. 验证最终请求队列、提交队列、活跃线程、临时文件和待迁移状态全部归零。
11. 完整 pytest、Python compileall、JavaScript 语法和 `git diff --check` 通过。

## 回退

把 `cpa_oauth_concurrency` 设为 1 即可恢复串行网络兑换，同时继续使用更安全的单提交器
和计数状态。若真实 OAuth 出现限流升高，先回退并发度，无需回滚凭据格式或迁移数据。
