# Panel V2 Modern UI Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在完整保留经典面板和全部现有业务能力的前提下，交付默认启用的现代 Panel V2、无敏感字段的服务端账号分页搜索、明暗主题和可断点续传的实时日志。

**Architecture:** 经典页面继续保留在 `panel/app.py`，V2 使用独立 Jinja 模板和本地静态 CSS/JavaScript，并仅通过 Flask API 访问业务能力。先增加不影响 Legacy 的安全只读契约和 SSE，再通过 `?ui=modern` 完成功能等价验证；只有自动化和真实浏览器门槛全部通过，才把 `/` 默认切到 V2，同时永久保留 `?ui=legacy`。

**Tech Stack:** Python 3.10+、Flask、pytest、原生 HTML/CSS/JavaScript、Server-Sent Events、Playwright、Windows PowerShell。

---

## 执行规则

- 每个任务都使用 `@test-driven-development`：先看到目标测试按预期失败，再写最小实现。
- 浏览器验收使用 `@playwright`，完成声明前使用 `@verification-before-completion`。
- 不修改 Legacy 的 `INDEX_HTML` 行为，不删除或重命名任何既有 API。
- 不把密码、SSO、OAuth token 或邮箱密钥写入 V2 响应、DOM、`localStorage`、截图或测试输出。
- 每个任务单独提交；最后切换默认入口必须是独立提交，便于一条提交回退。

### Task 1: 建立安全的账号快照、搜索与分页契约

**Files:**
- Create: `panel/account_catalog.py`
- Create: `tests/test_panel_v2_accounts.py`
- Modify: `panel/app.py:655-730, 5087-5103, 5129-5165, 5188-5327, 5379-5429`

**Step 1: 写账号目录纯逻辑的失败测试**

覆盖以下行为：

- 文件签名包含解析后的绝对路径、大小和 `mtime_ns`；签名不变时复用快照。
- 快照只保留邮箱、来源、来源时间、行序号和 SSO SHA-256 指纹，不保留 password、SSO 或原始行。
- 同一账号在多个文件出现时稳定去重，并优先保留较新的来源。
- `newest`、`oldest`、`email` 排序稳定。
- `q` 只搜索规范化邮箱，`source` 和 `ready|pending` 过滤正确。
- 页大小仅允许 `25|50|100`，非法页码、状态或排序抛出 `AccountQueryError`。

核心断言示例：

```python
page = catalog.query(
    files=[newer, older],
    completed_fingerprints={fingerprint("sso-ready")},
    page=1,
    page_size=25,
    q="ready@",
    source="all",
    status="ready",
    sort="newest",
)
assert page["items"] == [{
    "email": "ready@example.com",
    "source": newer.name,
    "status": "ready",
    "source_mtime": "2026-07-22T10:00:00",
}]
serialized = json.dumps(page)
assert "password" not in serialized
assert "sso-ready" not in serialized
```

**Step 2: 运行测试确认 RED**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_accounts.py
```

Expected: FAIL，因为 `panel.account_catalog` 尚不存在。

**Step 3: 实现最小账号目录模块**

在 `panel/account_catalog.py` 中实现：

```python
class AccountQueryError(ValueError):
    pass

class AccountCatalog:
    def invalidate(self) -> None: ...
    def query(self, files, completed_fingerprints, *, page, page_size,
              q, source, status, sort) -> dict: ...
```

内部记录可保存 SSO 指纹用于 CPA 状态匹配，但 `to_public()` 必须使用字段白名单。快照缓存以文件签名为键；所有排序追加 `email/source/line_index` 稳定键。

**Step 4: 为 Flask 路由写失败测试**

新增 `GET /api/v2/accounts` 测试：

- 未登录时沿用现有 401 契约。
- 默认值为 `page=1&page_size=25&source=all&status=all&sort=newest`。
- 响应包含 `items`、`pagination`、`filters.sources` 和安全的批次文件元数据。
- query string 非法时返回 400 和脱敏错误。
- 响应序列化文本不出现 fixture 中的 password、SSO 和 token。
- 修改账号文件后自动重建；导入、删除和迁移成功后显式调用 `invalidate_account_catalog()`。

**Step 5: 实现路由和失效钩子**

`panel/app.py` 持有单例 `_account_catalog`。读取 `_cpa_done` 时在 `_cpa_lock` 下复制集合；路由只把已验证的查询参数传入目录模块。保留 `/api/accounts` 原样供 Legacy 使用。

在下列成功路径末尾调用统一失效函数：

- `/api/accounts/delete`
- `/api/credentials/import`
- `/api/config/credentials/migrate`

注册写入依赖文件签名自动失效，不向注册子进程增加耦合。

**Step 6: 验证 GREEN 与 Legacy 兼容**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_accounts.py tests/test_panel_credential_storage.py tests/test_panel_sso_workspace.py
```

Expected: PASS；既有 `/api/accounts` 测试无变化。

**Step 7: 提交**

```powershell
git add panel/account_catalog.py panel/app.py tests/test_panel_v2_accounts.py
git commit -m "feat: add safe paginated account catalog"
```

### Task 2: 增加 V2 邮箱安全投影，避免读取已保存密钥

**Files:**
- Modify: `panel/app.py:1456-1568, 1734-1793, 5432-5451`
- Create: `tests/test_panel_v2_email_config.py`

**Step 1: 写安全响应失败测试**

用每一种密钥字段的唯一 canary 写入临时配置，调用 `GET /api/v2/config/email`，断言：

```python
payload = response.get_json()["email"]
body = response.get_data(as_text=True)
assert payload["configured"]["gptmail_api_key"] is True
assert "gptmail-secret-canary" not in body
assert "gptmail_api_key" not in payload["values"]
```

密钥集合至少覆盖 CF Worker、Cloudflare Temp Email、MoeMail、DuckMail、GPTMail、MaliAPI、LuckMail、SkyMail、CloudMail、Freemail、OpenTrashMail、Laoudo 和 SMTP 密码。

**Step 2: 运行测试确认 RED**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_email_config.py
```

Expected: 404 或响应包含 canary。

**Step 3: 实现字段白名单投影**

增加 `_EMAIL_SECRET_FIELDS` 和 `email_config_v2_public()`：

- `values` 只返回服务商、URL、域名、用户名、端口、超时和开关等非密钥字段。
- `configured` 只返回每个密钥字段的布尔值。
- 保留环境变量可用性布尔值，不返回环境变量内容。
- `choices` 和 `hint` 沿用现有配置规范化结果。

新增只读 `GET /api/v2/config/email`；既有 `/api/config/email` 与写入接口保持不变。V2 提交表单时对空密钥字段不发送 key，使现有值得以保留。

**Step 4: 验证 GREEN 和旧邮箱测试**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_email_config.py tests/test_panel_email_config.py tests/test_panel_email_receive_test.py
```

Expected: PASS。

**Step 5: 提交**

```powershell
git add panel/app.py tests/test_panel_v2_email_config.py
git commit -m "feat: expose redacted email settings for panel v2"
```

### Task 3: 建立可续传且有界的日志事件流

**Files:**
- Create: `panel/log_stream.py`
- Create: `tests/test_panel_log_stream.py`
- Modify: `panel/app.py:298-325, 540-550, 3189-3234, 5581-5603`

**Step 1: 写日志缓冲区失败测试**

覆盖：

- `append()` 分配严格递增序号。
- `clear()` 清内容但不回退全局序号。
- `after(sequence)` 只返回更新事件。
- 缓冲区滚动后，过旧游标从当前最旧事件开始，不重复。
- 常见 `password=...`、`sso=...`、`access_token`、`refresh_token`、Bearer 和 URL credential 被替换为 `[REDACTED]`。
- `wait_after()` 在新事件到达时唤醒，在超时时返回空列表供 heartbeat 使用。

**Step 2: 运行测试确认 RED**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_log_stream.py
```

Expected: FAIL，因为日志模块不存在。

**Step 3: 实现线程安全缓冲区并接入 `log_line()`**

实现 `SequencedLogBuffer(maxlen=2000)`，内部使用 `Condition`。`log_line()` 先统一脱敏，再同时写入 Legacy `_logs` 和新缓冲区；`start_job()` 使用 `clear_logs()`，不得直接重置序号。

**Step 4: 写 SSE 路由失败测试**

测试 `GET /api/logs/stream`：

- 复用面板登录保护。
- 优先读取有效 `Last-Event-ID`，否则读取 `after`。
- 非法游标返回 400。
- `Content-Type` 为 `text/event-stream`，响应禁止缓存。
- event 格式包含 `id` 与 JSON `data`。
- 无新数据时生成 `: heartbeat`。
- 同时连接超过上限时返回 429，生成器关闭后释放名额。

**Step 5: 实现 SSE 生成器**

新增 `MAX_LOG_STREAM_CLIENTS = 8` 的非阻塞名额控制。生成器循环输出待补事件，再最多等待心跳周期；在 `finally` 释放连接名额。不要从磁盘读取历史日志，也不要把 `_job` 全量塞入事件。

**Step 6: 验证 GREEN 和任务状态兼容**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_log_stream.py tests/test_panel_batch_runner.py tests/test_panel_registration_settings.py
```

Expected: PASS，`/api/job/status` 仍返回 Legacy `logs` 数组。

**Step 7: 提交**

```powershell
git add panel/log_stream.py panel/app.py tests/test_panel_log_stream.py
git commit -m "feat: stream sequenced panel logs over sse"
```

### Task 4: 建立可逆的 V2 渲染边界、主题和导航外壳

**Files:**
- Create: `panel/templates/index_v2.html`
- Create: `panel/static/panel-v2.css`
- Create: `panel/static/panel-v2.js`
- Create: `tests/test_panel_v2_routes.py`
- Modify: `panel/app.py:46-56, 4666-4700`

**Step 1: 写路由与静态资源失败测试**

断言：

- `/?ui=modern` 渲染带 `data-panel-version="2"` 的 V2。
- `/?ui=legacy` 继续渲染 Legacy 标识和旧控件。
- 此阶段不带参数的 `/` 仍为 Legacy。
- V2 引用 `/static/panel-v2.css` 和 `/static/panel-v2.js`，不引用 CDN。
- 服务端 HTML 包含无需 JavaScript 即可使用的 `/?ui=legacy` 链接。
- 模板源码不包含 `password`、`sso`、`access_token` 或 `refresh_token` 的真实值。

**Step 2: 运行测试确认 RED**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py
```

Expected: FAIL，因为 modern 查询仍渲染 Legacy。

**Step 3: 实现最小渲染边界**

导入 `render_template`，把现有首页上下文提取为 `legacy_index_response()`；`index()` 仅选择渲染器。V2 只由 Jinja 输出非敏感的应用标题和回退链接，不内嵌配置 JSON。

**Step 4: 实现设计 token、响应式壳和预绘制主题**

CSS 使用语义变量：`--surface-*`、`--text-*`、`--border`、`--accent` 和状态色。模板 `<head>` 中的最小同步脚本只读取 `panel-v2-theme` 的 `system|light|dark` 值并设置 `data-theme`；所有其他逻辑延后到静态 JS。

实现吸顶顶栏、横向 section nav、最大 `1440px` 内容区、移动端单列、可见焦点以及 `prefers-reduced-motion`。JS 管理 hash 导航和主题切换，非法 hash 回到 `#overview`。

**Step 5: 添加静态结构断言并验证 GREEN**

测试关键 section、landmark、ARIA label、主题选项、经典入口和空/错误容器。运行：

```powershell
node --check panel/static/panel-v2.js
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py
```

Expected: JavaScript 语法检查与 pytest 均通过。

**Step 6: 提交**

```powershell
git add panel/templates/index_v2.html panel/static/panel-v2.css panel/static/panel-v2.js panel/app.py tests/test_panel_v2_routes.py
git commit -m "feat: add reversible modern panel shell"
```

### Task 5: 迁移概览、注册控制和 Worker 浏览器操作

**Files:**
- Modify: `panel/templates/index_v2.html`
- Modify: `panel/static/panel-v2.css`
- Modify: `panel/static/panel-v2.js`
- Modify: `tests/test_panel_v2_routes.py`
- Modify: `tests/test_panel_registration_settings.py`

**Step 1: 写 V2 功能契约失败测试**

断言 V2 包含并使用：

- `/api/job/status`、`/api/job/start`、`/api/job/stop`
- `/api/config/browser`
- `/api/job/workers/<id>/browser/show|hide`
- 轮数 `1..10000`、并发 `1..10`
- 浏览器引擎和 `hidden|minimized|visible` 窗口模式
- 局部 busy 状态与确认对话框，而不是 `window.confirm()`

**Step 2: 运行测试确认 RED**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py tests/test_panel_registration_settings.py
```

Expected: V2 功能标识缺失。

**Step 3: 实现小型 store 和 API 客户端**

实现 `requestJson()`、分区状态、统一 toast/inline error 和初始并行加载。错误对象只显示后端安全 `error` 字段或通用 HTTP 状态，不注入 HTML。每 2 秒刷新任务状态；页面不可见时降低无意义的 DOM 更新，但任务状态恢复可见后立即刷新。

**Step 4: 实现概览和注册界面**

使用 `textContent` 和显式 DOM 创建 Worker 卡片。开始请求只发送当前控件值；运行期间只锁定注册相关控件。停止操作使用焦点受控确认对话框。Worker 操作以 `worker_id + browser.generation` 作为待处理键，防止重复请求和旧窗口操作覆盖新状态。

**Step 5: 验证 GREEN**

Run:

```powershell
node --check panel/static/panel-v2.js
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py tests/test_panel_registration_settings.py tests/test_browser_window.py
```

Expected: PASS。

**Step 6: 提交**

```powershell
git add panel/templates/index_v2.html panel/static/panel-v2.css panel/static/panel-v2.js tests/test_panel_v2_routes.py tests/test_panel_registration_settings.py
git commit -m "feat: migrate registration controls to panel v2"
```

### Task 6: 迁移账号查询、批次文件和导入导出操作

**Files:**
- Modify: `panel/templates/index_v2.html`
- Modify: `panel/static/panel-v2.css`
- Modify: `panel/static/panel-v2.js`
- Modify: `tests/test_panel_v2_routes.py`
- Modify: `tests/test_panel_v2_accounts.py`

**Step 1: 写账号 UI 契约失败测试**

验证账号分区具备搜索、来源、状态、排序、25/50/100 页大小、上一页/下一页、结果总数、空状态和请求错误重试。验证批次文件区域保留 preview、download、勾选删除，以及 SSO/JSON/CPA/Sub2/grok2api 下载链接和凭据导入文件框。

**Step 2: 运行测试确认 RED**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py tests/test_panel_v2_accounts.py
```

Expected: V2 账号控件缺失。

**Step 3: 实现惰性账号加载和竞态保护**

首次进入 `#accounts` 才请求 `/api/v2/accounts`。搜索使用 250ms 防抖；每个新查询中止旧 `AbortController`，同时增加 request generation，只有最新 generation 可写 store。改变搜索/过滤/排序/页大小时回到第 1 页。

**Step 4: 实现安全渲染和批次操作**

账号行只使用接口白名单字段，全部用 `textContent`。批次文件名经过 `encodeURIComponent` 构造 preview/download URL。删除使用确认对话框，成功后同时刷新目录和概览计数；导入使用 `FormData`，不在浏览器记录文件内容。

**Step 5: 验证 GREEN**

Run:

```powershell
node --check panel/static/panel-v2.js
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py tests/test_panel_v2_accounts.py tests/test_panel_sso_workspace.py
```

Expected: PASS。

**Step 6: 提交**

```powershell
git add panel/templates/index_v2.html panel/static/panel-v2.css panel/static/panel-v2.js tests/test_panel_v2_routes.py tests/test_panel_v2_accounts.py
git commit -m "feat: add searchable paginated accounts to panel v2"
```

### Task 7: 迁移邮箱与凭据设置的全部功能

**Files:**
- Modify: `panel/templates/index_v2.html`
- Modify: `panel/static/panel-v2.css`
- Modify: `panel/static/panel-v2.js`
- Modify: `tests/test_panel_v2_routes.py`
- Modify: `tests/test_panel_v2_email_config.py`
- Modify: `tests/test_panel_credential_storage.py`
- Modify: `tests/test_panel_email_receive_test.py`

**Step 1: 写功能等价失败测试**

邮箱分区必须覆盖所有现有 provider、保存、连接测试、发送能力探测、收件测试、取消测试和状态轮询。高级发件设置必须包含 `auto|native|smtp|direct_mx`、超时、SMTP 和 Direct MX 开关。

凭据分区必须覆盖：

- `/api/config/credentials` 读取和保存空目录。
- `/api/config/credentials/migrate` 手动迁移并切换。
- `/api/credentials/import` TXT/JSON 批量导入。
- `/api/cpa/status` 与 `/api/cpa/backfill`。
- 当前账号范围的所有下载入口。

**Step 2: 运行测试确认 RED**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py tests/test_panel_v2_email_config.py tests/test_panel_email_receive_test.py tests/test_panel_credential_storage.py
```

Expected: V2 邮箱和凭据功能标识缺失。

**Step 3: 实现邮箱表单和安全密钥语义**

通过 `/api/v2/config/email` 填充非密钥字段。密钥输入始终为空，以 `configured[field]` 决定“已保存，留空不修改”提示；提交和测试请求都只加入用户本次实际输入的密钥字段。provider 变化只切换相关字段的可见性，不销毁其他输入值。

收件测试复用现有异步 test ID；V2 使用页面内可访问对话框显示阶段、发送器、验证码匹配和脱敏错误，可取消且关闭后停止前端轮询。

**Step 4: 实现凭据状态和迁移操作**

展示 configured/resolved 路径、可写状态、SSO/mail/CPA 文件数、总字节、legacy 数量和 CPA 队列。普通保存、迁移、导入和 backfill 各自维护 busy 状态；迁移与替换导入必须确认。成功后并行刷新凭据、账号和任务摘要。

**Step 5: 验证 GREEN**

Run:

```powershell
node --check panel/static/panel-v2.js
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py tests/test_panel_v2_email_config.py tests/test_panel_email_config.py tests/test_panel_email_receive_test.py tests/test_panel_credential_storage.py tests/test_panel_sso_workspace.py
```

Expected: PASS，canary 密钥不出现在 V2 GET 响应或模板。

**Step 6: 提交**

```powershell
git add panel/templates/index_v2.html panel/static/panel-v2.css panel/static/panel-v2.js tests/test_panel_v2_routes.py tests/test_panel_v2_email_config.py tests/test_panel_credential_storage.py tests/test_panel_email_receive_test.py
git commit -m "feat: complete mail and credential workflows in panel v2"
```

### Task 8: 接入实时日志、过滤和轮询回退

**Files:**
- Modify: `panel/templates/index_v2.html`
- Modify: `panel/static/panel-v2.css`
- Modify: `panel/static/panel-v2.js`
- Modify: `tests/test_panel_v2_routes.py`
- Modify: `tests/test_panel_log_stream.py`

**Step 1: 写日志 UI 失败测试**

静态契约覆盖 EventSource URL、最后序号、去重集合、暂停、自动滚动、级别/关键词过滤、仅清空本地显示和 fallback 状态。断言 localStorage 白名单不包含配置字段或凭据字段。

**Step 2: 运行测试确认 RED**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py tests/test_panel_log_stream.py
```

Expected: V2 日志功能缺失。

**Step 3: 实现 SSE 客户端与回退状态机**

进入日志分区时连接 `/api/logs/stream?after=<lastSequence>`。按事件 `id` 去重并限制浏览器内存行数；离开分区可保持一个连接，但停止不可见 DOM 重绘。连续失败达到阈值后关闭 EventSource 并从 `/api/job/status` 每 2 秒同步；下一次进入日志或手动重试时恢复 SSE。

暂停只冻结渲染，事件仍进入有界客户端缓冲。清空只清除当前浏览器缓冲，不调用服务端删除。筛选在内存中执行，不修改原始行。

**Step 4: 验证 GREEN**

Run:

```powershell
node --check panel/static/panel-v2.js
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py tests/test_panel_log_stream.py tests/test_panel_batch_runner.py
```

Expected: PASS。

**Step 5: 提交**

```powershell
git add panel/templates/index_v2.html panel/static/panel-v2.css panel/static/panel-v2.js tests/test_panel_v2_routes.py tests/test_panel_log_stream.py
git commit -m "feat: add resilient live logs to panel v2"
```

### Task 9: 自动化、真实浏览器和敏感信息验收

**Files:**
- Modify: `tests/test_panel_v2_routes.py`
- Modify: `tests/test_panel_v2_accounts.py`
- Modify: `tests/test_panel_v2_email_config.py`
- Modify: `tests/test_panel_log_stream.py`
- Create: `output/playwright/panel-v2-desktop.png` (ignored evidence only)
- Create: `output/playwright/panel-v2-mobile.png` (ignored evidence only)

**Step 1: 跑完整自动化回归**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q
node --check panel/static/panel-v2.js
git diff --check
```

Expected: 全部 pytest 通过，JavaScript 语法和 diff 检查通过。

**Step 2: 启动隔离面板**

使用独立 `PANEL_PORT`、临时配置和测试账号目录启动，不读取或显示真实邮箱密钥；等待 `/health` 200。记录精确 PID，验收后只停止该 PID。

**Step 3: Playwright 检查两套 UI 和响应式布局**

在 `1440x1000`、`1280x900`、`768x900`、`390x844` 检查：

- 无页面级横向溢出。
- 六个 hash 分区可导航，刷新保留当前分区。
- system/light/dark 可切换和持久化，无明显首屏主题闪烁。
- `?ui=legacy` 可进入旧界面，旧界面关键控件仍工作。
- 控制台 error 为 0。

**Step 4: Playwright 检查关键流程**

使用测试 fixture 或后端 monkeypatch 进行非破坏性流程：账号搜索/过滤/分页、注册参数校验、Worker 无窗口空态、邮件连接失败显示、凭据路径校验、确认对话框、实时日志追加/暂停/重连。不得对真实邮箱服务发送测试或启动真实注册。

**Step 5: 检查网络与 DOM 的敏感信息**

检查 `/api/v2/accounts`、`/api/v2/config/email` 响应和渲染后的 DOM；canary password/SSO/token 均不得出现。确认 localStorage 只有主题、分区、页大小和日志显示偏好。

**Step 6: 保存证据并提交测试修正**

截图放入被忽略的 `output/playwright/`，不提交用户配置、日志或账号数据。若验收暴露问题，先补回归测试，再修复并重复 Steps 1–5。

```powershell
git add tests/test_panel_v2_routes.py tests/test_panel_v2_accounts.py tests/test_panel_v2_email_config.py tests/test_panel_log_stream.py
git commit -m "test: verify panel v2 feature parity"
```

如果没有产生测试修正，则不创建空提交。

### Task 10: 最后切换默认入口并更新维护文档

**Files:**
- Modify: `panel/app.py:4666-4700`
- Modify: `tests/test_panel_v2_routes.py`
- Modify: `README.md`

**Step 1: 先把默认入口测试改为 V2 并确认 RED**

断言：

```python
assert 'data-panel-version="2"' in client.get("/").get_data(as_text=True)
assert 'data-panel-version="2"' in client.get("/?ui=modern").get_data(as_text=True)
assert 'id="register_concurrency"' in client.get("/?ui=legacy").get_data(as_text=True)
```

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py
```

Expected: 第一个断言 FAIL，因为 `/` 仍是 Legacy。

**Step 2: 用最小变更切换默认入口**

`index()` 仅在 `ui=legacy` 时渲染经典页面；其余情况渲染 V2。V2 页脚和错误边界始终保留 `/?ui=legacy`。

**Step 3: 更新 README**

说明：

- 新版面板的六个分区和明暗主题。
- 账号服务端分页/搜索不会在列表页面暴露凭据。
- 实时日志自动回退机制。
- `/?ui=legacy` 的长期恢复入口。
- 无前端构建步骤，升级和回退方法。

不得记录任何本地邮箱 URL、管理员账号、密码、token 或测试账号。

**Step 4: 最终回归**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q
node --check panel/static/panel-v2.js
git diff --check
git status --short
```

Expected: 全部测试通过；状态只包含计划内文件；经典 UI 仍通过 `?ui=legacy` 可访问。

再次用 Playwright 打开默认 `/` 和 `/?ui=legacy`，确认控制台无错误、主题和响应式截图与 Task 9 一致。

**Step 5: 提交可独立回退的默认切换**

```powershell
git add panel/app.py tests/test_panel_v2_routes.py README.md
git commit -m "feat: make panel v2 the default interface"
```

## 完成定义

- 所有 Task 1–10 的 RED/GREEN 证据成立。
- 完整 pytest、JavaScript 语法、diff 检查与四种视口 Playwright 验收通过。
- 默认 V2 与 `?ui=legacy` 均可操作且控制台无错误。
- V2 网络响应、DOM、浏览器存储和截图均不含测试 canary 密钥。
- 未修改、删除或提交工作区中用户已有的未跟踪文件。
- 推送、合并或发布 Release 需要用户明确要求后再执行。
