# SSO 重新授权多样本预检设计

## 背景

当前 `POST /api/cpa/reauthorize`（兼容别名
`POST /api/cpa/refresh-all`）会固定选择一个候选账号，同步完成
Web SSO 到 CPA OAuth 凭据的真实兑换与原子提交。该账号出现
Access denied 时会继续尝试其他账号，但 timeout、Cloudflare、协议变化等
其他错误会立即终止整个批次。

单次兑换虽然为 HTTP 请求设置了超时，但一次兑换包含 authorize、consent
脚本发现、session prime、consent submit、token exchange，以及最多三次外层
重试。脚本发现会逐个请求同源脚本，等待 consent action 全局锁也没有截止时间，
因此现有实现不能保证预检在有限总时长内结束。

## 目标

- 不再让单个账号代表整个账号池。
- 样本覆盖不同本地批次时间、不同文件和不同序号。
- 一个账号卡住或失效时，仍能继续验证其他样本。
- 明确限制单样本和整个预检的耗时。
- 只有足够证据时才把失败判断为批次级系统故障。
- 保留现有原子提交、旧 CPA 失败保护、身份去重和 Access denied 禁用能力。
- API 和日志不得泄露邮箱、密码、完整 SSO 或 OAuth Token。

## 非目标

- 不续期 x.ai Web SSO Cookie；本功能仍是使用有效 Web SSO 重新执行 OAuth
  authorization-code + PKCE 交换。
- 不引入新的异步预检服务、独立进程池或前端状态机。
- 不用随机抽样；相同候选快照必须生成可复现的样本计划。
- 不改变普通 CPA 后台队列的并发数和常规重试策略。

## 候选快照

重授权候选在内部补充以下元数据：

- `_source_mtime_ns`：来源文件修改时间，作为本地批次时间代理。
- `_source_line_index`：账号在来源文件内的零基序号。
- `_stable_identity`：沿用 `sub -> email -> SSO fingerprint` 的稳定身份。

current 与 legacy 凭据目录先解析为同一份文件快照，再按以下稳定键全局排列：

```text
source_mtime_ns DESC, source_name ASC, source_line_index ASC, stable_identity ASC
```

之后再执行禁用池过滤、稳定身份去重和 `limit` 截断。这里的时间只表示本地凭据
批次时间，不宣称是账号在 x.ai 的真实创建时间。

## 抽样策略

对 newest-first 的 `N` 个候选先生成不可变样本计划，不能在每次 `pop()` 后重新
计算索引。

基础样本锚点：

```text
oldest = N - 1
newest = 0
middle = (N - 1) // 2
```

去重后按 `oldest -> newest -> middle` 执行：

- `N=1 -> [0]`
- `N=2 -> [1, 0]`
- `N=3 -> [2, 0, 1]`
- `N=5 -> [4, 0, 2]`

若有足够来源文件，middle 锚点优先替换为距离中位序号最近、尚未采样的来源。
若三个基础样本全部失败且错误不构成一致系统证据，再尝试 1/4 和 3/4 序号的
备用样本，整个预检最多五个不同稳定身份。

所有基础样本都会执行，以获得真实的多样本结果。备用样本只在零成功且结论混杂
时执行。

## 决策规则

预检用于判断 OAuth 协议链路是否可用，而不是要求账号池中大多数账号有效：

- 任意样本完整兑换并提交成功：协议链路可用，基础样本执行结束后释放剩余批量。
- 明确 Access denied：只禁用实际被拒绝的账号，继续其他样本。
- SSO 过期、未知账号错误、网络错误或 timeout：记录该样本失败，不禁用账号，
  继续其他样本。
- 三个不同稳定身份出现相同已知系统错误签名且没有成功样本：批次级熔断。
- 三个不同账号均 Access denied 且没有成功样本：沿用 Access denied 熔断。
- 三个基础样本全失败但错误混杂：执行最多两个备用样本。
- 五个样本仍无成功：返回 `422 preflight_failed`，剩余账号不入队。

一次成功足以证明当前 OAuth 接口仍可完成端到端交换，避免因两个过期账号把
整个账号池误判为协议故障。后台队列仍会使用现有连续系统错误熔断保护后续批量。

## 时间预算

默认时间预算：

```text
单样本最大预算：60 秒
整个预检最大预算：120 秒
```

路由创建共享的绝对 `time.monotonic()` deadline。每个样本的 deadline 不能超过
批次 deadline，并根据尚未执行的基础样本保留公平时间片。deadline 逐层透传：

```text
route
  -> _run_cpa_reauthorization_preflight
    -> _convert_cpa_with_retry
      -> convert_one
        -> sso_to_token
          -> _discover_consent_action_id
```

所有 HTTP 请求、cooldown、指数退避、consent action 锁等待和脚本遍历都使用同一
剩余预算。到期抛出带阶段信息的 `ConversionDeadlineExceeded`，统一映射为
`oauth_preflight_timeout`。该样本不再重试、不进入禁用池、不增加
Access denied streak。

不使用线程 watchdog：Windows 无法安全终止正在运行的 Python 线程，超时返回后
线程仍可能继续写 CPA、修改计数或持有锁。若未来证明底层 native HTTP 调用不遵守
timeout，再把纯 `convert_one` 隔离到可强制终止的子进程，父进程继续负责状态与提交。

## 状态与计数

- 每个成功样本已真实生成并原子提交 CPA，不再进入后台队列。
- 每个失败样本保留旧 CPA，也不在同一批次重复入队。
- `queued = preflight_succeeded + bulk_queued`，不再硬编码 `1 + bulk_queued`。
- 最终门禁失败时，已经成功提交的样本不回滚；响应明确剩余批量未释放。
- `run_ok + run_fail + run_skipped` 必须最终等于 `run_total`。

API 的 `preflight` 只返回聚合字段：

```json
{
  "ok": true,
  "planned": 3,
  "attempted": 3,
  "succeeded": 1,
  "failed": 2,
  "timed_out": 1,
  "account_denied": 0,
  "distinct_sources": 3,
  "systemic_signature": ""
}
```

不得返回样本邮箱、完整 SSO、密码、Token 或来源文件路径。

## 测试策略

- 采样器矩阵覆盖 `N=1/2/3/5`、无重复、列表不变和来源修正。
- current/legacy 多文件使用显式 `mtime_ns` 验证真正的全局时间排序。
- 单文件多行验证头、尾、中序号覆盖。
- 首样本 timeout 后仍会尝试不同时间/序号样本并允许成功释放批量。
- 三账号 Access denied、三账号相同系统签名、混合失败和备用样本。
- deadline 覆盖请求、脚本遍历、锁等待、cooldown、backoff，且不误报协议变化。
- 成功样本不重复入队，聚合计数和 run 状态一致。
- 两个路由别名共享契约，所有响应分支不泄露敏感信息。
