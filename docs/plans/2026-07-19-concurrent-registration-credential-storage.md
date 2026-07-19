# Concurrent Registration and Credential Storage Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为 Grok Register Win 增加 1–10 个固定并发注册槽、每槽独立浏览器，并将 SSO、邮箱 JWT 与 CPA 凭据集中到可配置且可安全迁移的目录。

**Architecture:** 面板使用多个长期 CLI 子进程作为固定 worker 槽，每个进程独占浏览器和输出文件，面板只聚合结构化轮次事件。新的 `lib/credential_store.py` 统一解析路径、创建并发安全文件名和执行哈希校验迁移，面板与 CLI 不再直接依赖仓库根目录或固定 `data/cpa`。

**Tech Stack:** Python 3.11、Flask、DrissionPage、Camoufox、pytest、psutil、原生 `pathlib`/`hashlib`/`shutil`/`threading`。

---

所有命令均从工作树 `D:\python_project\grok-register-win\.worktrees\concurrency-credential-storage` 执行，测试解释器为 `D:\python_project\grok-register-win\.venv\Scripts\python.exe`。

### Task 1: 重构当前版本 README 并完成第一次发布

**Files:**
- Modify: `README.md`
- Verify: `.github/workflows/tests.yml`

**Step 1: 建立 README 当前能力清单**

逐项对照 `config.example.json`、`panel/app.py`、`grok_register_ttk.py` 和现有测试，只保留已经实现的 v1.3 能力。不得写入并发或新凭据目录承诺。

**Step 2: 重写 README**

使用以下结构：项目定位与状态徽章、风险声明、核心能力、五分钟快速开始、邮箱/Cloudflare 配置、浏览器复用、产物格式、配置参考、目录结构、故障排查、开发验证、上游归属与许可证。将逐版本长流水账替换为 Releases/提交历史链接。

**Step 3: 验证 README**

Run:

```powershell
git diff --check
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest -q
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m compileall -q grok_register_ttk.py launcher.py panel lib tests
```

Expected: 58 tests pass；compileall 和 diff-check 退出码为 0。

**Step 4: 提交、快进合并并第一次推送**

```powershell
git add README.md docs/plans/2026-07-19-concurrent-registration-credential-storage-design.md docs/plans/2026-07-19-concurrent-registration-credential-storage.md
git commit -m "docs: rebuild project readme"
git -C D:\python_project\grok-register-win merge --ff-only agent/concurrency-credential-storage
git -C D:\python_project\grok-register-win push aiis2 master
```

Expected: `aiis2/master` 与本地 `master` 哈希一致，GitHub Actions `Tests` 成功。

### Task 2: 定义凭据目录契约

**Files:**
- Create: `lib/credential_store.py`
- Create: `tests/test_credential_store.py`
- Modify: `config.example.json`

**Step 1: 写失败测试**

测试希望得到以下 API：

```python
from credential_store import CredentialLayout, resolve_credentials_dir

def test_relative_directory_resolves_under_app_root(tmp_path):
    root = tmp_path / "app"
    layout = CredentialLayout.from_config(root, {"credentials_dir": "vault"})
    assert layout.root == root / "vault"
    assert layout.sso_dir == root / "vault" / "sso"
    assert layout.mail_dir == root / "vault" / "mail"
    assert layout.cpa_dir == root / "vault" / "cpa"

def test_rejects_app_root_and_filesystem_root(tmp_path):
    ...

def test_serializes_internal_path_as_relative_and_external_as_absolute(tmp_path):
    ...
```

同时测试默认 `data/credentials`、目录自动创建、不可写/文件路径拒绝。

**Step 2: 运行 RED**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_credential_store.py -q
```

Expected: 因 `credential_store` 不存在而失败。

**Step 3: 实现最小路径模型**

实现：

```python
@dataclass(frozen=True)
class CredentialLayout:
    app_root: Path
    root: Path
    sso_dir: Path
    mail_dir: Path
    cpa_dir: Path

    @classmethod
    def from_config(cls, app_root: Path, config: Mapping[str, object]) -> "CredentialLayout": ...

def normalize_credentials_setting(app_root: Path, value: str) -> str: ...
def ensure_layout(layout: CredentialLayout) -> CredentialLayout: ...
```

在 `config.example.json` 增加：

```json
"credentials_dir": "data/credentials"
```

**Step 4: 运行 GREEN 并提交**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_credential_store.py -q
git add lib/credential_store.py tests/test_credential_store.py config.example.json
git commit -m "feat: add configurable credential layout"
```

### Task 3: 实现并发安全的 CLI 凭据输出

**Files:**
- Modify: `lib/credential_store.py`
- Modify: `grok_register_ttk.py`
- Modify: `tests/test_credential_store.py`
- Modify: `tests/test_panel_batch_runner.py`

**Step 1: 写失败测试**

覆盖：相同时间戳下不同 worker/PID 的文件名不同；CLI 从 `config.credentials_dir` 写入 `sso/` 与 `mail/`；marker 可含 worker ID 但不含秘密。

希望的 API：

```python
def create_worker_output_paths(
    layout: CredentialLayout,
    worker_id: int,
    pid: int,
    now: datetime | None = None,
    nonce: str | None = None,
) -> WorkerOutputPaths: ...
```

**Step 2: 运行 RED**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_credential_store.py tests/test_panel_batch_runner.py -q
```

Expected: 新 API/环境变量断言失败。

**Step 3: 实现最小改动**

- CLI 读取 `GROK_WORKER_ID`，默认 1；
- `run_registration_cli` 启动时只创建一次 worker 输出路径；
- SSO 写 `layout.sso_dir`；邮箱 JWT 写 `layout.mail_dir`；
- `format_round_marker(..., worker_id=...)` 仅增加整数 worker 字段；
- 删除两个根目录硬编码写入点。

**Step 4: 运行 GREEN 并提交**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_credential_store.py tests/test_panel_batch_runner.py -q
git add lib/credential_store.py grok_register_ttk.py tests/test_credential_store.py tests/test_panel_batch_runner.py
git commit -m "feat: isolate worker credential outputs"
```

### Task 4: 将面板账号与 CPA 操作切换到动态凭据目录

**Files:**
- Modify: `panel/app.py`
- Create: `tests/test_panel_credential_storage.py`

**Step 1: 写失败测试**

在临时 `BASE_DIR`/`CONFIG_PATH` 下测试：

- `list_account_files()` 从当前 `sso/` 读取；
- 首次迁移前兼容读取历史根目录账号并去重；
- CPA 状态、ZIP、Sub2 和删除接口从当前 `cpa/`/`sso/` 工作；
- 目录配置切换后无需重启模块。

**Step 2: 运行 RED**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_panel_credential_storage.py -q
```

Expected: 固定 `CPA_DIR` 与根目录 glob 导致断言失败。

**Step 3: 实现动态路径访问器**

在 `panel/app.py` 中使用：

```python
def current_credential_layout(cfg: dict | None = None) -> CredentialLayout: ...
def current_cpa_paths(cfg: dict | None = None) -> CpaPaths: ...
```

将 `CPA_DIR`、`CPA_INDEX_PATH`、`CPA_FAILED_PATH` 的业务读取替换为调用时解析；保留环境变量 `CPA_DIR` 作为显式兼容覆盖时应在公共状态中标明。

**Step 4: 运行 GREEN 与回归并提交**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_panel_credential_storage.py tests/test_panel_batch_runner.py -q
git add panel/app.py tests/test_panel_credential_storage.py
git commit -m "refactor: route credentials through configured storage"
```

### Task 5: 实现哈希校验迁移事务

**Files:**
- Modify: `lib/credential_store.py`
- Modify: `tests/test_credential_store.py`

**Step 1: 写失败测试**

覆盖：

- 历史 SSO/Mail/CPA 全部迁移；
- 同名同内容跳过并删除来源；
- 同名异内容生成稳定冲突后缀；
- SHA-256 校验失败时配置不切换、来源保留；
- 源删除失败返回 warning；
- 不迁移目录自身或临时文件。

期望入口：

```python
def migrate_credentials(
    app_root: Path,
    current: CredentialLayout,
    target: CredentialLayout,
    *,
    legacy: bool = True,
    verify_file: Callable[[Path, Path], bool] = verify_sha256,
) -> MigrationResult: ...
```

**Step 2: 运行 RED**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_credential_store.py -q
```

**Step 3: 实现复制、校验和冲突策略**

仅在全部目标副本校验成功后返回可提交结果；配置写入由调用者在成功结果后执行。每次复制使用同目录临时文件加 `os.replace`。

**Step 4: 运行 GREEN 并提交**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_credential_store.py -q
git add lib/credential_store.py tests/test_credential_store.py
git commit -m "feat: add verified credential migration"
```

### Task 6: 增加凭据配置与迁移 API

**Files:**
- Modify: `panel/app.py`
- Modify: `tests/test_panel_credential_storage.py`

**Step 1: 写失败测试**

测试以下路由：

```text
GET  /api/config/credentials
POST /api/config/credentials
POST /api/config/credentials/migrate
```

断言：返回解析路径与统计；保存非法/非空目标失败；运行中保存或迁移返回 409；迁移成功后原子保存配置并立即刷新状态；响应不包含凭据内容。

**Step 2: 运行 RED**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_panel_credential_storage.py -q
```

**Step 3: 实现 API 和原子配置保存**

新增 `save_config_atomic`，使用 `config.json.tmp`、flush/fsync 和 `os.replace`。迁移 API 在 `_job_lock` 下先确认没有运行任务，但不得在长时间复制期间持有全局锁；使用独立迁移锁防止重复请求。

**Step 4: 运行 GREEN 并提交**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_panel_credential_storage.py -q
git add panel/app.py tests/test_panel_credential_storage.py
git commit -m "feat: expose credential storage migration api"
```

### Task 7: 定义并发分片与 worker 环境

**Files:**
- Modify: `panel/app.py`
- Modify: `tests/test_panel_batch_runner.py`
- Modify: `config.example.json`

**Step 1: 写失败测试**

希望得到：

```python
def normalize_registration_concurrency(value: object) -> int: ...
def partition_registration_work(total: int, concurrency: int) -> list[WorkerAssignment]: ...
```

断言 1、2、10 边界；0/11/非整数拒绝；`total < concurrency` 时不创建空槽；10/3 分片为 4、3、3 且全局 index 不重叠。`build_cli_batch_env` 必须包含 `GROK_WORKER_ID`。

**Step 2: 运行 RED**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_panel_batch_runner.py -q
```

**Step 3: 实现纯函数并更新示例配置**

在 `config.example.json` 增加 `"register_concurrency": 1`。保持默认单并发行为不变。

**Step 4: 运行 GREEN 并提交**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_panel_batch_runner.py -q
git add panel/app.py tests/test_panel_batch_runner.py config.example.json
git commit -m "feat: define registration worker assignments"
```

### Task 8: 实现固定并发槽监督器

**Files:**
- Modify: `panel/app.py`
- Modify: `tests/test_panel_batch_runner.py`

**Step 1: 写失败测试**

使用 FakeProcess/FakeLauncher 覆盖：

- 3 个 assignment 同时登记 3 个 PID；
- 每个环境有唯一 worker ID 和不重叠 offset/count；
- 一个 worker 超时只终止自己的 PID；
- 其他 worker 结果继续汇总；
- stop 对所有已登记进程各终止一次；
- 汇总结果按全局 index 去重；
- worker 清理超时不制造额外失败账号。

**Step 2: 运行 RED**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_panel_batch_runner.py -q
```

**Step 3: 实现最小并发协调器**

- `_procs: dict[int, Popen]` 替代单一 `_proc`；
- `_job["workers"]` 保存公开状态，不保存敏感数据；
- 每个 assignment 由一个面板线程执行现有 `_run_batch` 监督循环；
- `job_worker` 启动全部槽并 join；
- `_terminate_register_proc` 保持精确 PID 树语义；
- CPA 入队和汇总状态更新继续在锁下执行。

**Step 4: 运行 GREEN 和现有回归并提交**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_panel_batch_runner.py tests/test_browser_lifecycle.py -q
git add panel/app.py tests/test_panel_batch_runner.py
git commit -m "feat: run fixed concurrent registration workers"
```

### Task 9: 增加并发与凭据 UI

**Files:**
- Modify: `panel/app.py`
- Create: `tests/test_panel_registration_settings.py`
- Modify: `tests/test_panel_credential_storage.py`

**Step 1: 写失败测试**

断言 HTML/JS 包含：

- 1–10 数字输入 `register_concurrency`；
- 凭据目录输入、状态与三个子目录统计；
- “保存新路径”和“迁移并切换”按钮；
- 运行中禁用迁移；
- `/api/job/start` 验证并保存 concurrency；
- `/api/job/status` 返回 worker 数组和有效并发数。

**Step 2: 运行 RED**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_panel_registration_settings.py tests/test_panel_credential_storage.py -q
```

**Step 3: 实现 UI 与交互**

目标流程：

```text
首页 -> 设置并发 1..10 -> 开始注册 -> 查看每槽状态
首页 -> 输入凭据目录 -> 查看/保存或迁移 -> 显示计数与结果
```

所有提示使用中文；迁移操作需要浏览器 `confirm` 二次确认并显示不会覆盖文件的说明。

**Step 4: 运行 GREEN 并提交**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_panel_registration_settings.py tests/test_panel_credential_storage.py -q
git add panel/app.py tests/test_panel_registration_settings.py tests/test_panel_credential_storage.py
git commit -m "feat: configure concurrency and credential migration in ui"
```

### Task 10: 完成真实迁移、UI 和浏览器并发验证

**Files:**
- No committed test artifacts

**Step 1: 全量自动验证**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest -q
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m compileall -q grok_register_ttk.py launcher.py panel lib tests
git diff --check
```

**Step 2: 临时目录迁移验证**

在 `%TEMP%` 创建隔离应用数据，放入假 SSO、假邮箱 JWT 和假 CPA 文件，通过 Flask API 执行迁移。校验目标 SHA-256、源文件删除、冲突重命名、下载内容和日志脱敏。测试后只删除精确 QA 临时目录。

**Step 3: UI 验证**

Browser plugin 不可用时记录原因并使用普通 Playwright。验证桌面与移动端：页面身份、非空、无错误覆盖层、控制台健康、并发输入边界、凭据状态、保存与迁移交互，并将截图保存到仓库外。

**Step 4: 真实双 Chromium 生命周期验证**

启动两个隔离 worker 的 Chromium，记录不同根 PID 与 Profile；每槽执行多轮 reset，确认槽内根 PID 复用且进程数不持续增长；停止后确认所属 PID 为零，启动前已有浏览器 PID 未缺失。

### Task 11: 更新最终 README 并完成第二次发布

**Files:**
- Modify: `README.md`
- Modify: `docs/plans/2026-07-19-concurrent-registration-credential-storage.md` only if implementation deviations require correction

**Step 1: 更新 README**

加入：

- v1.4 并发注册说明和资源模型；
- `register_concurrency` 与 `credentials_dir`；
- 新目录结构；
- UI 手动迁移步骤与冲突策略；
- 从旧根目录升级的操作；
- 并发资源建议和停止语义。

**Step 2: 发布前审计**

```powershell
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest -q
D:\python_project\grok-register-win\.venv\Scripts\python.exe -m compileall -q grok_register_ttk.py launcher.py panel lib tests
git diff --check
git status --short
```

扫描不得跟踪 `config.json`、账号文件、邮箱凭据、CPA 真实数据、虚拟环境或浏览器 Profile。

**Step 3: 提交、合并和推送**

```powershell
git add README.md config.example.json grok_register_ttk.py panel/app.py lib/credential_store.py tests
git commit -m "feat: add concurrent registration and credential migration"
git -C D:\python_project\grok-register-win merge --ff-only agent/concurrency-credential-storage
git -C D:\python_project\grok-register-win push aiis2 master
```

如果前面已经按任务拆分提交，最后提交只包含 README 或剩余文档，禁止重复提交已提交文件。

**Step 4: 远端验收**

- `git rev-parse HEAD` 等于 `git ls-remote aiis2 refs/heads/master`；
- `gh repo view aiis2/grok-register-win` 为 PUBLIC/master；
- 最新 GitHub Actions `Tests` 为 success；
- 根工作区仅保留用户原有未跟踪批处理文件。
