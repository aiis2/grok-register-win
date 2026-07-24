# Access Denied Disabled Account Pool Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 OAuth consent 明确拒绝的账号持久隔离到可人工恢复的禁用池，并从所有刷新、CPA 队列和导出格式中排除。

**Architecture:** 新建独立的禁用账号注册表，以跨进程锁和原子 JSON 保存账号身份及恢复材料；`panel/app.py` 在 CPA 失败提交时隔离账号，并让所有账号与 CPA 投影共享禁用判定。凭据布局和迁移包含 `disabled` 目录，新版账号页面通过脱敏 API 查看和执行“恢复并重新授权”。

**Tech Stack:** Python 3.10+、Flask、pytest、`InterProcessFileLock`、原子文件替换、原生 JavaScript、Playwright。

---

### Task 1: 建立禁用账号注册表

**Files:**
- Create: `lib/disabled_account_pool.py`
- Create: `tests/test_disabled_account_pool.py`

**Step 1: 写失败测试**

覆盖空池、按邮箱/subject/fingerprint 匹配、重复禁用合并、公共投影不泄露
`raw`、恢复返回完整内部记录、损坏 JSON 拒绝覆盖和两个实例顺序更新不丢记录。

**Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_disabled_account_pool.py -q
```

Expected: FAIL，提示 `lib.disabled_account_pool` 不存在。

**Step 3: 实现最小注册表**

实现：

- `is_access_denied_error(error) -> bool`
- `DisabledAccountPool.list_public()`
- `DisabledAccountPool.identity_sets()`
- `DisabledAccountPool.matches(...)`
- `DisabledAccountPool.disable(account, error) -> dict`
- `DisabledAccountPool.restore(record_id) -> dict`
- `DisabledAccountPool.put(record) -> None`

更新使用同目录临时文件、`os.replace()` 和 `InterProcessFileLock`。

**Step 4: 运行测试并确认通过**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_disabled_account_pool.py -q
```

Expected: PASS。

### Task 2: 将禁用池纳入凭据布局和迁移

**Files:**
- Modify: `lib/credential_store.py`
- Modify: `tests/test_credential_store.py`
- Modify: `tests/test_panel_registration_settings.py`

**Step 1: 写失败测试**

验证 `CredentialLayout.from_config()` 生成 `disabled_dir`，`ensure_layout()` 创建目录，
迁移复制 `disabled/accounts.json`，迁移校验失败时保留源文件并回滚目标。

**Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_credential_store.py tests/test_panel_registration_settings.py -q
```

Expected: FAIL，提示布局没有 `disabled_dir` 或迁移未包含禁用池。

**Step 3: 实现布局与迁移**

给 `CredentialLayout` 增加 `disabled_dir`；创建布局、统计、迁移源枚举、目标清理都包含
该目录。锁文件不进入统计和普通迁移。

**Step 4: 运行测试并确认通过**

重复 Step 2 命令，Expected: PASS。

### Task 3: 在 CPA 失败和队列边界自动隔离

**Files:**
- Modify: `panel/app.py`
- Modify: `tests/test_panel_cpa_pipeline.py`
- Modify: `tests/test_panel_sso_refresh.py`

**Step 1: 写失败测试**

覆盖：

- 明确 `Access denied` 写入禁用池并把现有 CPA 标记 `disabled=true`；
- 401、403、超时和协议错误不禁用；
- 已禁用账号不能入队；
- 排队后被禁用的任务在 worker 侧跳过；
- 批量预检隔离拒绝账号后继续下一个候选；
- 全部候选拒绝时不反复卡在首个账号。

**Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_cpa_pipeline.py tests/test_panel_sso_refresh.py -q
```

Expected: FAIL，现有失败路径只写 `failed.jsonl`，预检仍直接中止。

**Step 3: 实现自动隔离**

增加当前禁用池工厂、账号身份转换、CPA 原子标记和队列双重检查。
`_record_cpa_failure()` 仅在分类器命中时隔离。重写预检循环，使账号级拒绝继续选择候选，
其他错误保持现有 422 熔断语义。

**Step 4: 运行测试并确认通过**

重复 Step 2 命令，Expected: PASS。

### Task 4: 统一活动账号与全导出过滤

**Files:**
- Modify: `panel/app.py`
- Modify: `panel/account_catalog.py`
- Create: `tests/test_panel_disabled_exports.py`
- Modify: `tests/test_panel_v2_accounts.py`
- Modify: `tests/test_panel_oauth_export_ownership.py`

**Step 1: 写导出矩阵失败测试**

准备一个活动账号和一个禁用账号，并为两者写 CPA。逐一请求或调用：

- 单批次 TXT；
- SSO/merged TXT；
- all ZIP；
- accounts JSON；
- grok2api JSON；
- CPA ZIP；
- Sub2 ZIP/JSON；
- OAuth export preflight/claim。

断言所有活动数据存在，禁用账号的邮箱、密码、SSO、access token 和 refresh token 在
响应体及 ZIP 成员中均不存在。

**Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_disabled_exports.py tests/test_panel_v2_accounts.py tests/test_panel_oauth_export_ownership.py -q
```

Expected: FAIL，现有单文件和 ZIP 会原样输出源账号文件。

**Step 3: 实现统一过滤**

增加未过滤存储读取和活动账号投影，现有业务默认使用活动投影。单文件下载、预览和
ZIP 改为现场写入过滤后的内容。`list_active_cpa_files()` 同时检查注册表和 CPA
`disabled` 字段。AccountCatalog 接收禁用身份集合并从活动列表排除。

**Step 4: 运行测试并确认通过**

重复 Step 2 命令，Expected: PASS。

### Task 5: 提供禁用池 API 和人工恢复

**Files:**
- Modify: `panel/app.py`
- Create: `tests/test_panel_disabled_accounts_api.py`

**Step 1: 写失败测试**

验证：

- `GET /api/disabled-accounts` 分页返回脱敏记录；
- 响应不包含密码、完整 SSO 或 token；
- `POST /api/disabled-accounts/<id>/restore` 删除禁用记录并强制入队；
- 入队失败时恢复原禁用记录；
- 未知 ID 返回 404；
- 旧 CPA 维持 `disabled=true`，成功换票后才恢复导出资格。

**Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_disabled_accounts_api.py -q
```

Expected: FAIL，路由不存在。

**Step 3: 实现 API**

复用现有登录、活动锁、迁移锁和 CPA 工作区保护。恢复操作使用显式 JSON POST，
失败时通过 `put()` 回滚原记录。

**Step 4: 运行测试并确认通过**

重复 Step 2 命令，Expected: PASS。

### Task 6: 在新版账号页面增加禁用池与恢复操作

**Files:**
- Modify: `panel/templates/index_v2.html`
- Modify: `panel/static/panel-v2.js`
- Modify: `panel/static/panel-v2.css`
- Modify: `tests/test_panel_v2_routes.py`

**Step 1: 写失败契约测试**

断言页面包含禁用池计数、表格、空状态和恢复确认控件；JavaScript 使用
`/api/disabled-accounts`、POST restore、现有 `confirmAction()` 和 busy 状态。

**Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_v2_routes.py -q
```

Expected: FAIL，页面尚无禁用池控件。

**Step 3: 实现 UI**

在账号页增加独立禁用池区块。列表只显示脱敏字段；恢复前显示确认框，完成后同时刷新
活动账号、禁用账号和 CPA 状态。复用现有主题 token、按钮和错误提示。

**Step 4: 运行测试并确认通过**

重复 Step 2 命令，Expected: PASS。

### Task 7: 完整回归与浏览器验证

**Files:**
- Verify only

**Step 1: 运行目标测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_disabled_account_pool.py tests/test_credential_store.py tests/test_panel_disabled_exports.py tests/test_panel_disabled_accounts_api.py tests/test_panel_cpa_pipeline.py tests/test_panel_sso_refresh.py tests/test_panel_v2_accounts.py tests/test_panel_v2_routes.py tests/test_panel_oauth_export_ownership.py -q
```

Expected: PASS。

**Step 2: 运行全量测试与静态检查**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q lib panel main.py launcher.py
node --check panel/static/panel-v2.js
git diff --check
```

Expected: 全部退出码 0。

**Step 3: Playwright 验证**

在独立数据根与测试端口的 `#accounts` 页面使用隔离测试数据验证：

- 活动账号列表不显示禁用账号；
- 禁用池显示脱敏信息；
- 恢复确认、成功/失败提示和列表刷新正常；
- 明暗主题正常；
- 浏览器控制台无 error；
- 导出响应不含测试禁用账号的任何密钥材料。

**Step 4: 检查工作树范围**

确认只包含本功能文件和进入本轮前已有改动；不提交用户自有的临时文件。除非用户另行
要求，本计划执行完成后不自动 commit 或 push。
