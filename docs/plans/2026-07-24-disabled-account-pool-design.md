# Access Denied 禁用账号池设计

## 背景

CPA OAuth 转换当前会把失败追加到 `cpa/failed.jsonl`，但失败账号仍保留在原始
`accounts_*.txt` 中。后续“重新生成账号授权”、CPA 补转、SSO/账号下载、
grok2api、CPA 和 Sub2API 导出仍会重新读取这些账号。

真实 OAuth 验证已经确认：部分账号会在 xAI consent 阶段稳定返回
`Access denied`。这类错误属于账号级拒绝，不应作为系统性 OAuth 协议故障熔断整个
批次，也不应让同一账号在每次刷新时重复消耗请求。

## 目标

1. 仅当 OAuth consent 明确返回 `Access denied`/`access_denied` 时自动禁用账号。
2. 禁用账号进入当前凭据目录中的独立持久池，并且不会自动恢复。
3. 禁用账号不再参与 CPA 补转、SSO 重新授权或任何导出格式。
4. 已有 CPA 凭据同步标记为禁用，防止旧 refresh token 被继续输出。
5. UI 可查看禁用账号，并支持显式“恢复并重新授权”。
6. 禁用池随凭据目录迁移，支持多 OAuth worker 和多个应用实例安全读写。
7. 批量重新授权的预检账号若被拒绝，隔离该账号后继续选择下一个账号预检。

## 非目标

- 不把普通 401、403、超时、Cloudflare、协议变化或临时网络错误自动归为账号禁用。
- 不删除注册批次源文件，不物理重写正在被注册 worker 写入的 `accounts_*.txt`。
- 不把禁用账号写入任何 CPA、Sub2API、grok2api、SSO 或账号导出包。
- 不自动定时恢复禁用账号。
- 不在日志、普通状态 API 或列表页面显示密码、完整 SSO、access token 或 refresh token。

## 方案比较

### 方案 A：只写 CPA JSON 的 `disabled=true`

该方案能阻止部分 CPA 消费者使用旧 token，但原始 SSO 仍会进入重新授权、SSO TXT、
账号 JSON、批次 ZIP 和 grok2api 导出，覆盖范围不足，不采用。

### 方案 B：从账号批次文件物理移走对应行

该方案天然减少部分导出，但注册 worker 可能正在追加同一文件；并发读取、重写和文件
删除会增加凭据损坏风险，也破坏原始批次的审计语义，不采用。

### 方案 C：中央禁用注册表，并对 CPA 做防御性禁用

在凭据根目录建立 `disabled/accounts.json`，以账号身份记录禁用状态。所有账号读取和
导出入口共享该判定；同时把匹配的 CPA 文件标记为 `disabled=true`。源账号行保留，
因此可以人工恢复，也不会在并发注册时重写源文件。采用此方案。

## 存储模型

凭据布局增加：

```text
<credentials_dir>/
  sso/
  mail/
  cpa/
  disabled/
    accounts.json
    .accounts.json.lock
  archive/
```

`accounts.json` 使用版本化对象和按 ID 索引的记录：

```json
{
  "version": 1,
  "updated_at": "2026-07-24T12:00:00Z",
  "accounts": {
    "<opaque-id>": {
      "id": "<opaque-id>",
      "email": "user@example.com",
      "subject": "xai-subject-if-known",
      "sso_fingerprint": "<sha256>",
      "source": "accounts_....txt",
      "raw": "email----password----sso",
      "reason": "access_denied",
      "disabled_at": "2026-07-24T12:00:00Z"
    }
  }
}
```

原始账号行仅保存在受凭据目录约束的禁用池文件中，用于源文件被删除后的人工恢复。
公共 API 只返回 ID、邮箱、来源、原因和时间。

身份匹配按以下顺序构建稳定别名集合：

1. xAI/Web SSO 中可解析的 subject；
2. 规范化邮箱；
3. SSO SHA-256 fingerprint。

任一稳定别名命中都视为同一禁用身份。邮箱匹配可阻止同一被拒账号换发新 SSO 后自动
重新进入队列；只有人工恢复才清除禁用记录。

## 并发与原子性

- 进程内更新由注册表锁串行化。
- 跨进程更新使用 `InterProcessFileLock`。
- JSON 先写同目录临时文件，再用 `os.replace()` 原子替换。
- 读取遇到不存在的文件返回空池；结构损坏时拒绝写入并记录错误，不能用空池覆盖。
- 自动禁用在 CPA 串行提交阶段落盘；预检也通过同一提交路径执行。
- 凭据目录迁移把整个 `disabled` 目录纳入校验、冲突处理和源清理。

## Access denied 分类

新增单一分类函数，仅匹配规范化后的明确账号拒绝信号：

- `Access denied`
- OAuth/redirect 中的 `error=access_denied`

以下错误不得触发禁用：

- `401 Unauthorized`
- `403 Forbidden`
- `token http 401/403`
- Cloudflare/challenge
- consent action 缺失、响应缺少 code
- timeout、连接失败和 5xx

分类结果作为 `_record_cpa_failure()` 的分支。命中后：

1. 把账号原始信息写入禁用注册表；
2. 从 `_cpa_done` 移除对应 fingerprint；
3. 将匹配 CPA 文件原子更新为 `disabled=true`，并写入禁用原因和时间；
4. 继续记录普通失败审计，但日志不输出密钥。

## 队列与批量预检

- `enqueue_cpa_convert()` 入队前检查禁用池并返回 `account disabled`。
- OAuth worker 领取任务后再次检查，覆盖账号在排队期间被另一 worker 禁用的竞态。
- `unique_accounts()`、CPA 补转和重新授权候选只返回活动账号。
- 批量预检遇到账号级 `Access denied` 时，该账号记为失败并进入禁用池，然后从候选中
  选择下一个账号继续预检。
- 只有非账号级预检错误才终止整个批次。
- 如果全部候选都被禁用，批次以“无可授权账号”完成，并返回禁用数量，不反复失败。

## 全导出过滤

统一的活动账号投影覆盖：

- 单个 `accounts_*.txt` 下载及预览；
- `/download/sso.txt`、`/download/merged.txt`；
- `/download/all.zip` 内每个批次和 merged 文件；
- `/download/accounts.json`；
- `/download/grok2api.json`；
- CPA 导出票据、`cpa.zip`；
- `sub2.zip`、`sub2.json`；
- 所有从 `unique_accounts()`、`list_active_cpa_files()` 派生的后续格式。

CPA 活动判定同时要求：

1. CPA `disabled` 不为 true；
2. 对应原始账号仍处于活动池；
3. CPA 身份不命中禁用注册表。

这样即使某个新入口绕过原始账号投影，CPA 的防御性标记仍会阻止输出。

## UI 与恢复

新版账号页面增加禁用池摘要和列表，显示：

- 邮箱；
- 来源批次；
- 禁用原因；
- 禁用时间；
- “恢复并重新授权”按钮。

恢复流程：

1. 在确认对话框中明确说明不会复用旧 CPA；
2. 从禁用注册表移除记录；
3. 保持旧 CPA 的 `disabled=true`；
4. 将保存的 SSO 强制加入重新授权队列；
5. 入队失败时回滚禁用记录；
6. 新授权成功后覆盖 CPA 为 `disabled=false`，账号才重新进入 CPA/Sub2API 导出。

经典 UI 和现有下载地址保持兼容；新版 UI 仅增加控制，不改变现有导航。

## 验证

1. 单元测试覆盖注册表原子写入、合并、匹配、恢复和损坏文件保护。
2. 失败测试证明只有明确 `Access denied` 会禁用。
3. 队列测试验证入队前和 worker 领取后的二次过滤。
4. 批量预检测试验证拒绝账号被跳过、下一个账号继续，以及全部拒绝的终态。
5. 导出矩阵测试验证所有格式均不包含禁用邮箱、密码、SSO 或 CPA token。
6. 迁移测试验证禁用池随目录迁移且失败可回滚。
7. UI/API 测试验证列表不泄密、恢复失败回滚。
8. Playwright 在独立数据根与测试端口的 `#accounts` 页面验证禁用列表、确认框、
   恢复状态、明暗主题和控制台错误，避免读取或修改真实凭据。
9. 完整 pytest、compileall、JavaScript 语法和 `git diff --check` 通过。
