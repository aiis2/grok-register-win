# 通用邮箱收件验证设计

日期：2026-07-19  
状态：已批准

## 背景

当前面板的“测试连接”仅针对 Cloudflare Temp Email，且只验证设置端点；它不能证明邮箱能够真正收到并正确提取验证码。项目已经接入多种临时邮箱与固定邮箱，因此需要一个与具体服务商解耦的端到端收件验证能力。

验证必须生成与 Grok 相同格式的测试验证码，向本次测试邮箱发送邮件，再复用现有邮箱适配器的收件箱快照、轮询与验证码提取逻辑。页面通过模态框显示进度、结果和可操作的脱敏错误。

## 目标

- 当前已经实现的所有收件适配器都可以复用同一套收件验证流程。
- 发件能力独立于收件适配器，并支持能力探测与自动选择。
- 优先使用服务商原生发件 API，其次使用配置的 SMTP Relay，最后可选本机直投 MX。
- Freemail 作为第一个原生发件实现，支持环境变量与页面配置。
- 测试复用 `get_email()`、`get_current_ids()`、`wait_for_code()` 和 `GROK_CODE_PATTERN`。
- 验证码、密码、Token 与完整邮箱地址不进入 API 结果、浏览器日志或应用日志。
- 后台异步执行，支持轮询、超时、取消和清理。

## 非目标

- 不提供任意收件人邮件发送功能。
- 不盲目探测用户 API 的未知路径。
- 不把测试发件器扩展为群发或营销邮件系统。
- 不在 Git 中保存 SMTP、Freemail 或其他邮箱密钥。

## 架构

### 收件层

保留现有邮箱适配器作为 `ReceiverAdapter`。收件测试直接创建局部 mailbox 实例，不使用 `mail_providers` 的全局活动邮箱状态，避免与注册任务互相污染。

统一调用：

1. `get_email()` 创建或取得测试邮箱；
2. `get_current_ids()` 记录发信前邮件 ID；
3. 发信完成后调用 `wait_for_code()`；
4. 使用 `GROK_CODE_PATTERN` 提取 `ABC-123` 格式验证码；
5. 对发送值与读取值做精确比较。

固定邮箱适配器使用已有邮箱并忽略旧邮件；临时邮箱在测试后调用适配器清理能力。无法自动清理时返回 warning，而不覆盖主验证结果。

### 发件层

新增独立 `TestSender` 策略注册表。策略必须实现能力探测与单封测试邮件发送，并只能向本次测试生成的地址投递。

自动选择顺序：

1. `provider_native`：使用已知服务商的官方发件接口；
2. `smtp_relay`：使用用户配置的 SMTP 服务，支持 SSL、STARTTLS、明文以及可选认证；
3. `direct_mx`：解析目标域名 MX，并从本机向 25 端口直投。

`direct_mx` 默认关闭，只有用户显式启用后才参与自动选择。它受运营商封禁、云厂商出站规则、反垃圾策略、SPF/DKIM/DMARC 和动态 IP 信誉影响，因此失败必须返回明确诊断，不能自动降级成成功。

### Freemail 原生发件

Freemail 使用已登录会话完成整个测试：

1. `POST /api/login`，读取 `can_send`；
2. `GET /api/domains` 与 `GET /api/generate` 创建邮箱；
3. `POST /api/send` 从测试地址向自身发送验证码；
4. `GET /api/emails` 由现有适配器轮询；
5. `DELETE /api/mailboxes` 清理测试邮箱。

配置优先级为页面请求值、已保存本地配置、环境变量。支持：

- `MAIL_WEB_URL` → `freemail_api_url`
- `ADMIN_NAME` → `freemail_username`
- `ADMIN_PASSWORD` → `freemail_password`

官方协议参考：<https://github.com/idinging/freemail/blob/master/docs/api.md>。

## 后台任务与 API

收件测试采用内存后台任务，避免 HTTP 请求在等待邮件时超时。

- `POST /api/config/email/receive-test`：校验配置并启动任务，返回 `test_id`。
- `GET /api/config/email/receive-test/<test_id>`：返回阶段、状态、脱敏结果或错误。
- `POST /api/config/email/receive-test/<test_id>/cancel`：请求取消并执行清理。
- `POST /api/config/email/test-capabilities`：根据当前选择与表单配置返回收件、原生发件、SMTP 和 Direct MX 能力。

任务阶段：

`checking → creating → snapshotting → sending → waiting → verifying → cleaning → succeeded/failed/cancelled`

同一时间最多运行一个收件测试。已完成任务保留有限时间后从内存清理。注册任务开始时拒绝启动收件测试；收件测试运行时也拒绝启动注册任务，避免共享服务配置产生竞争。

## UI

邮箱服务卡片始终显示“测试收件”按钮。点击后打开模态框，先显示能力探测结果，再启动测试。

模态框展示：

- 当前邮箱服务商；
- 选中的发件方式；
- 脱敏测试邮箱；
- 当前阶段与阶段时间线；
- 总耗时与收件耗时；
- 清理状态与 warning；
- 失败阶段、HTTP/SMTP 状态及脱敏错误内容。

用户可以取消运行中的测试。关闭模态框不会中断任务，再次打开时恢复当前状态。

邮箱服务配置新增通用发件设置：

- `mail_test_sender_mode`: `auto | provider_native | smtp_relay | direct_mx`
- `mail_test_timeout_sec`
- `mail_test_smtp_host`
- `mail_test_smtp_port`
- `mail_test_smtp_security`: `ssl | starttls | plain`
- `mail_test_smtp_username`
- `mail_test_smtp_password`
- `mail_test_smtp_from`
- `mail_test_direct_mx_enabled`

密码字段不由配置查询 API 明文返回；空值表示保留已保存密码。页面只显示“已配置”状态。

## 错误与安全

- 错误按配置、认证、建箱、发信、收件、提码、比对、清理分阶段返回。
- 服务端响应仅保留安全、限长的错误摘要，并删除密码、Authorization、Cookie、Token、验证码和完整邮箱地址。
- 原生 API 只访问适配器声明的固定端点。
- SMTP 只能向本次测试邮箱发送一封邮件。
- Direct MX 的目标主机只能来自本次邮箱域名的 MX 解析结果。
- 测试 ID 使用不可预测随机值；状态接口仍受面板登录保护。
- 单任务、超时和频率限制防止滥用。

## 测试策略

### 自动化测试

- 发件策略选择顺序与强制模式。
- Freemail `can_send` 能力探测、发信负载和错误映射。
- SMTP SSL、STARTTLS、匿名与认证路径。
- Direct MX 默认关闭、MX 解析、端口/SMTP 错误诊断及收件人限制。
- 收件箱旧 ID 快照、Grok 格式提码、精确比对、超时、取消和清理。
- API 任务互斥、状态转换、过期清理和敏感信息脱敏。
- 页面模态框、能力显示、进度轮询、成功与错误状态。

### 浏览器验证

- 桌面与移动端打开模态框无溢出。
- 点击测试后按钮禁用，时间线持续更新。
- 成功时显示脱敏结果，不显示验证码与密钥。
- 认证、发信和超时错误在模态框中可读。
- 浏览器控制台无相关错误。

### 真实集成验证

使用 Windows 用户环境变量连接现有 Freemail 实例，执行一次原生 API 发信、实际收件、Grok 提码和自动清理。真实集成测试不进入默认 CI，未配置密钥时自动跳过。

## 发布

实现完成并通过验证后：

1. 提交所有代码、测试和文档，不提交任何本地配置或凭据；
2. 推送 `aiis2/master`；
3. 使用 `aiis2` 身份创建或更新 GitHub Release；
4. Release 说明汇总 `aiis2` 在本项目中的主要变更，包括 Cloudflare Temp Email、浏览器复用与隔离、并发注册、凭据迁移和通用邮箱收件验证。
