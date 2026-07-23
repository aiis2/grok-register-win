# CPA OAuth Queue Performance Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 CPA 的 OAuth 网络兑换改为默认 2、可配置 1–4 的有界并行流水线，并以单提交器保证凭据文件、索引和工作区状态一致。

**Architecture:** 保留 `panel/app.py` 的请求队列和 fingerprint 去重，增加 OAuth 结果队列、多个兑换 worker 和一个串行提交 worker。兑换 worker 只创建独立 Session 并调用 `convert_one()`；提交 worker 独占 CPA 文件、`index.json`、失败记录和最终状态更新。状态由 `pending`、`active_workers`、`commit_pending` 和 `commit_active` 计数派生，凭据切换同时检查请求与提交两个阶段。

**Tech Stack:** Python 3.10+、Flask、`queue.Queue`、`threading`、curl_cffi、pytest、原生 JavaScript。

---

### Task 1: 建立 CPA 并发配置与状态模型

**Files:**
- Modify: `panel/app.py:88-95`
- Modify: `panel/app.py:256-272`
- Modify: `panel/app.py:533-550`
- Test: `tests/test_panel_cpa_pipeline.py`

**Step 1: 写并发规范化失败测试**

新建 `tests/test_panel_cpa_pipeline.py`，覆盖默认值、1–4、越界和非法输入：

```python
@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 2), ("", 2), (1, 1), ("2", 2), (4, 4), (0, 1), (8, 4), ("bad", 2)],
)
def test_normalize_cpa_concurrency_is_bounded(value, expected):
    assert panel_app.normalize_cpa_concurrency(value) == expected
```

增加配置优先级测试：

```python
def test_cpa_concurrency_prefers_environment_over_saved_config(monkeypatch):
    monkeypatch.setattr(panel_app, "load_config", lambda: {"cpa_oauth_concurrency": 3})
    monkeypatch.setenv("CPA_CONCURRENCY", "4")
    assert panel_app.resolve_cpa_concurrency() == 4
```

**Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_cpa_pipeline.py -q
```

Expected: FAIL，提示 `normalize_cpa_concurrency` 尚不存在。

**Step 3: 实现最小配置与状态字段**

在 `panel/app.py` 增加：

```python
CPA_CONCURRENCY_ENV = "CPA_CONCURRENCY"
DEFAULT_CPA_CONCURRENCY = 2
MAX_CPA_CONCURRENCY = 4


def normalize_cpa_concurrency(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_CPA_CONCURRENCY
    return max(1, min(parsed, MAX_CPA_CONCURRENCY))


def resolve_cpa_concurrency(cfg: Optional[dict] = None) -> int:
    override = str(os.environ.get(CPA_CONCURRENCY_ENV) or "").strip()
    source = override if override else (cfg or load_config()).get(
        "cpa_oauth_concurrency", DEFAULT_CPA_CONCURRENCY
    )
    return normalize_cpa_concurrency(source)
```

给 `_cpa_state` 增加 `concurrency`、`active_workers`、`commit_pending` 和
`commit_active`。`credentials_config_public()` 返回保存值、运行值和环境覆盖状态，但
不返回任何凭据。

**Step 4: 运行测试并确认通过**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_cpa_pipeline.py -q
```

Expected: PASS。

**Step 5: 提交**

```powershell
git add panel/app.py tests/test_panel_cpa_pipeline.py
git commit -m "feat: add bounded CPA OAuth concurrency"
```

### Task 2: 以 TDD 实现并行兑换和串行提交

**Files:**
- Modify: `panel/app.py:1300-1405`
- Test: `tests/test_panel_cpa_pipeline.py`
- Modify: `tests/test_panel_sso_refresh.py:12-240`

**Step 1: 写并行重叠和索引完整性失败测试**

在隔离凭据目录中加入 24 个合成任务。`convert_one` 使用计数器和短暂等待记录最大同时
调用数，启动 4 个 OAuth worker 和一个提交器：

```python
def test_parallel_oauth_workers_overlap_but_commit_complete_index(isolated_pipeline):
    tracker = ConcurrentCallTracker(delay=0.03)
    panel_app.convert_one = tracker.convert
    panel_app.start_cpa_worker(concurrency=4)
    enqueue_synthetic_accounts(24)
    wait_for_pipeline_idle()

    payload = json.loads(panel_app.current_cpa_paths().index_path.read_text())
    assert tracker.max_active >= 2
    assert len(payload["items"]) == 24
    assert len(list(panel_app.current_cpa_paths().directory.glob("xai-*.json"))) == 24
```

增加乱序完成测试，确保结果完成顺序不同也不会丢失；增加相同 fingerprint 在
`commit_pending` 阶段仍不能重新入队的测试。

**Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_cpa_pipeline.py -q
```

Expected: FAIL；当前 `start_cpa_worker()` 不接受并发参数，且只有单 worker。

**Step 3: 拆分 OAuth worker 与提交 worker**

增加：

```python
_cpa_result_q: "queue.Queue[Optional[dict]]" = queue.Queue()


def _cpa_oauth_worker_loop(worker_id: int) -> None:
    while True:
        item = _cpa_q.get()
        if item is None:
            _cpa_q.task_done()
            return
        # generation 检查、active_workers +1、独立 convert_one、
        # 结果进入 _cpa_result_q；fingerprint 暂不移除。


def _cpa_commit_worker_loop() -> None:
    while True:
        result = _cpa_result_q.get()
        if result is None:
            _cpa_result_q.task_done()
            return
        # 再次检查 generation；串行写 CPA、index 或 failed.jsonl；
        # 最后更新 done/inflight/ok/fail/commit_pending。
```

`start_cpa_worker(concurrency=None)` 加载索引、解析并发度，启动 N 个
`cpa-oauth-N` daemon 线程和一个 `cpa-commit` daemon 线程。保留
`_cpa_worker_loop()` 作为测试/兼容包装时，应调用新阶段而不是复制业务逻辑。

`CPA_DELAY` 在每个 OAuth worker 内局部执行。提交器不 sleep，避免磁盘阶段人为限速。

**Step 4: 运行管线与既有刷新测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_cpa_pipeline.py tests/test_panel_sso_refresh.py -q
```

Expected: PASS，索引 24/24 且最大并行调用数至少为 2。

**Step 5: 提交**

```powershell
git add panel/app.py tests/test_panel_cpa_pipeline.py tests/test_panel_sso_refresh.py
git commit -m "feat: parallelize CPA OAuth conversion safely"
```

### Task 3: 加固限流退避、失败保留和状态归零

**Files:**
- Modify: `panel/app.py:1300-1405`
- Test: `tests/test_panel_cpa_pipeline.py`
- Test: `tests/test_panel_sso_refresh.py`

**Step 1: 写可重试分类与失败路径测试**

覆盖以下契约：

```python
@pytest.mark.parametrize("message", ["token HTTP 429", "token HTTP 502", "authorize 请求失败: timeout"])
def test_transient_oauth_errors_retry_at_most_twice(message):
    ...

@pytest.mark.parametrize("message", ["SSO 无效或已过期", "Cloudflare 拦截 (HTTP 403)", "consent 响应缺少 code"])
def test_permanent_oauth_errors_do_not_retry(message):
    ...
```

验证最终失败时旧 CPA 字节不变、`failed.jsonl` 只有一条、`pending == 0`、
`active_workers == 0`、`commit_pending == 0`、`commit_active == 0`、
`running is False`、`_cpa_inflight` 为空。

**Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_cpa_pipeline.py tests/test_panel_sso_refresh.py -q
```

Expected: FAIL；当前没有重试分类和多阶段状态归零。

**Step 3: 实现有界重试与派生状态**

增加纯函数：

```python
def is_transient_cpa_error(exc: Exception) -> bool:
    text = str(exc).casefold()
    permanent = ("sso 无效", "cloudflare 拦截", "consent 响应缺少")
    transient = ("http 429", "http 500", "http 502", "http 503", "http 504",
                 "timeout", "timed out", "connection reset")
    return not any(key in text for key in permanent) and any(
        key in text for key in transient
    )
```

兑换最多 3 次，退避等待使用可注入的 `_cpa_sleep()` 方便测试。增加共享冷却截止时间，
仅在 429/短暂 5xx 时延后后续请求，最大冷却不超过 8 秒。

增加 `_refresh_cpa_running_locked()`：

```python
_cpa_state["active"] = _cpa_state["active_workers"] > 0
_cpa_state["running"] = any(
    int(_cpa_state.get(key) or 0) > 0
    for key in ("pending", "active_workers", "commit_pending", "commit_active")
)
```

所有计数变更后在同一锁内调用该函数。

**Step 4: 运行测试并确认通过**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_cpa_pipeline.py tests/test_panel_sso_refresh.py -q
```

Expected: PASS。

**Step 5: 提交**

```powershell
git add panel/app.py tests/test_panel_cpa_pipeline.py tests/test_panel_sso_refresh.py
git commit -m "fix: bound CPA OAuth retries and pipeline state"
```

### Task 4: 保护凭据迁移与工作区 generation

**Files:**
- Modify: `panel/app.py:280-286`
- Modify: `panel/app.py:1119-1185`
- Test: `tests/test_panel_sso_workspace.py`
- Test: `tests/test_panel_credential_storage.py`
- Test: `tests/test_panel_cpa_pipeline.py`

**Step 1: 写多阶段迁移竞态失败测试**

分别令 OAuth worker 活跃、结果等待提交、提交器正在写盘，验证：

```python
assert panel_app.credential_change_blocker() == "CPA 转换仍在运行，完成后才能迁移凭据目录"
with pytest.raises(panel_app.CredentialImportBusy):
    panel_app._begin_cpa_workspace_switch()
```

构造旧 generation 的成功结果，改变 `_cpa_workspace_generation` 后交给提交器，验证新
目录没有生成 CPA 文件或索引，且 inflight 被正确释放。

**Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_sso_workspace.py tests/test_panel_credential_storage.py tests/test_panel_cpa_pipeline.py -q
```

Expected: FAIL；当前只检查布尔 `active` 和请求队列。

**Step 3: 实现统一忙碌判定和双队列 drain**

增加：

```python
def _cpa_pipeline_busy_locked() -> bool:
    return any(
        int(_cpa_state.get(key) or 0) > 0
        for key in ("pending", "active_workers", "commit_pending", "commit_active")
    )
```

`credential_change_blocker()`、`_begin_cpa_workspace_switch()` 和状态 API 共用该函数。
工作区切换只 drain 尚未领取的请求；结果队列非空、兑换活跃或提交活跃时拒绝切换。
恢复切换时重新入队已 drain 的请求并恢复 pending/inflight。

**Step 4: 运行迁移测试并确认通过**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_sso_workspace.py tests/test_panel_credential_storage.py tests/test_panel_cpa_pipeline.py -q
```

Expected: PASS。

**Step 5: 提交**

```powershell
git add panel/app.py tests/test_panel_sso_workspace.py tests/test_panel_credential_storage.py tests/test_panel_cpa_pipeline.py
git commit -m "fix: protect credential workspace during CPA commits"
```

### Task 5: 暴露安全配置与新版 UI 状态

**Files:**
- Modify: `panel/app.py:256-272`
- Modify: `panel/app.py:5585-5631`
- Modify: `panel/templates/index_v2.html`
- Modify: `panel/static/panel-v2.js`
- Test: `tests/test_panel_v2_routes.py`
- Test: `tests/test_panel_cpa_pipeline.py`

**Step 1: 写 API 和 UI 契约失败测试**

API 测试覆盖：

```python
payload = client.get("/api/config/credentials").get_json()
assert payload["cpa_oauth_concurrency"] == 2
assert payload["cpa_runtime_concurrency"] == 2
assert payload["cpa_concurrency_env_override"] is False

response = client.post(
    "/api/config/credentials",
    json={"credentials_dir": "data/credentials", "cpa_oauth_concurrency": 4},
)
assert response.status_code == 200
assert json.loads(config_path.read_text())["cpa_oauth_concurrency"] == 4
```

环境覆盖时 POST 返回保存值和实际运行值，但不覆盖环境变量。UI 契约要求存在
`cpa-oauth-concurrency`、`min="1"`、`max="4"`、重启生效提示，并调用现有
`/api/config/credentials`。

**Step 2: 运行测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_v2_routes.py tests/test_panel_cpa_pipeline.py -q
```

Expected: FAIL；页面和 API 尚未暴露并发配置。

**Step 3: 实现配置保存和状态渲染**

新版凭据区域加入数字输入框：

```html
<input id="cpa-oauth-concurrency" type="number" min="1" max="4" step="1">
```

`renderCredentialConfig()` 填充保存值和运行值；`saveCredentialConfig()` 将规范化后的
并发值与 `credentials_dir` 一起提交。`renderCpaStatus()` 显示：

```text
OAuth 活跃 2/4 · 待兑换 12 · 待提交 1
```

运行中允许编辑保存值，但明确“重启面板生效”；凭据目录修改仍遵守原迁移阻塞规则。
如复用同一 POST 会扩大现有阻塞范围，则把并发保存拆为
`POST /api/config/cpa`，同时保持 UI 只写非敏感整数。

**Step 4: 运行 API、UI 和 JavaScript 检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_v2_routes.py tests/test_panel_cpa_pipeline.py -q
node --check panel/static/panel-v2.js
```

Expected: pytest PASS，`node --check` exit 0。

**Step 5: 提交**

```powershell
git add panel/app.py panel/templates/index_v2.html panel/static/panel-v2.js tests/test_panel_v2_routes.py tests/test_panel_cpa_pipeline.py
git commit -m "feat: configure CPA OAuth concurrency in panel"
```

### Task 6: 性能、真实 OAuth 与完整回归验证

**Files:**
- Create: `tests/bench_cpa_pipeline.py`
- Modify: `README.md`
- Modify: `docs/plans/2026-07-23-cpa-oauth-queue-performance-design.md`

**Step 1: 添加可重复的隔离性能探针**

`tests/bench_cpa_pipeline.py` 使用临时 CPA 目录、24 个合成任务和固定 80ms 兑换延迟，
分别执行 1、2、4 并发，只输出耗时、吞吐、文件数、索引数和最终计数，不输出凭据。

验收条件：

- 三组均为文件 24、索引 24、成功 24、失败 0；
- 2 worker 吞吐至少为 1 worker 的 1.5 倍；
- 4 worker 吞吐至少为 1 worker 的 2.5 倍；
- 最终 pending、active、commit_pending、commit_active、inflight 均为 0。

**Step 2: 运行定向和完整测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_panel_cpa_pipeline.py tests/test_panel_sso_refresh.py tests/test_panel_sso_workspace.py tests/test_panel_credential_storage.py tests/test_panel_v2_routes.py -q
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q panel lib tests
node --check panel/static/panel-v2.js
git diff --check
```

Expected: 全部 PASS/exit 0。

**Step 3: 运行隔离性能对照**

Run:

```powershell
.\.venv\Scripts\python.exe tests/bench_cpa_pipeline.py
```

Expected: 2/4 worker 达到上述吞吐阈值，三个索引均完整。

**Step 4: 等待旧进程自然清空并执行真实 OAuth 验证**

先只读轮询：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/api/cpa/status
```

只有旧进程的 `pending == 0` 且 `active == false` 后，记录 PID、停止项目面板并以新代码
重新启动。选取小批量当前有效 SSO，先并发 1、再并发 2，分别记录：

- 样本数、成功数、按错误类型聚合的失败数；
- 总耗时、每分钟吞吐、中位数和 P95；
- CPA 文件数与索引数的增量；
- 最终队列/线程/临时文件状态。

不打印邮箱、SSO、Access Token、Refresh Token 或 CPA 内容。若并发 2 的成功率低于并发
1，或出现明显新增 429/403，则把默认值回退为 1 并保留可配置能力。

**Step 5: 使用 Playwright 验证新版页面**

访问 `http://127.0.0.1:8787/#credentials`，验证并发输入、保存提示、运行状态实时刷新、
明暗主题、控制台无错误，且现有补转与刷新全部 SSO 操作仍可用。

**Step 6: 更新文档并提交**

README 只记录公开配置：

```text
CPA_CONCURRENCY=1..4（默认 2）
```

设计文档追加最终匿名化性能表和真实验证结论。

```powershell
git add README.md docs/plans/2026-07-23-cpa-oauth-queue-performance-design.md tests/bench_cpa_pipeline.py
git commit -m "docs: document CPA OAuth pipeline performance"
```

**Step 7: 合并准备**

```powershell
git status --short
git log --oneline --decorate -8
```

Expected: 只有用户原有未跟踪文件留在主工作区；功能 worktree 干净。使用
`finishing-a-development-branch` 检查合并方式，并在未收到明确推送要求时只合回本地
`master`，不推送远程。
