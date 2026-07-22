# Registration Log Performance Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将现代面板的注册和日志合并为同一页面，并通过有界、批量日志渲染消除大量注册后打开日志造成的长时间主线程卡死。

**Architecture:** 保留服务端 2000 条日志缓冲和现有 SSE/轮询协议，在浏览器端增加默认 300 条的可视窗口、100ms 批量调度和 180ms 搜索防抖。现代面板使用 `#register` 作为规范分区，将旧 `#logs` 映射到同一分区并定位日志控制台；经典界面不变。

**Tech Stack:** Python 3.10+、Flask/Jinja、原生 HTML/CSS/JavaScript、pytest、Playwright CLI、GitHub CLI。

---

## 执行规则

- 使用 `@test-driven-development`：每项行为先增加失败测试并确认 RED，再写最小实现并确认 GREEN。
- 使用 `@systematic-debugging` 保留 49.3 秒 Long Task 的根因证据，不通过降低服务端日志上限掩盖问题。
- 使用 `@playwright` 进行 2000 条历史日志、实时突发、桌面和移动端验收。
- 发布前使用 `@verification-before-completion`，合并时使用 `@finishing-a-development-branch`，推送和 Release 使用 GitHub 发布流程。
- 仅暂存本任务文件；真实配置、日志、账号和凭据不得进入提交。

### Task 1: 锁定合并导航和性能契约

**Files:**
- Modify: `tests/test_panel_v2_routes.py`

**Step 1: 写失败测试**

增加以下断言：

```python
assert 'data-section-link="register">注册与日志</a>' in html
assert 'data-section-link="logs"' not in html
register = html.split('id="section-register"', 1)[1]
assert register.index('id="registration-form"') < register.index('id="logs-output"')
assert 'id="section-logs"' not in html
```

读取 `panel-v2.js` 并验证 `LOG_VISIBLE_STEP = 300`、`LOG_RENDER_INTERVAL_MS = 100`、旧 `logs` hash 映射、`scheduleLogRender()`、`loadOlderLogs()` 与 `showLatestLogs()` 存在；提取 `appendLogEvent()` 函数体，断言其中没有直接调用 `renderLogs()`。

**Step 2: 运行测试确认 RED**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py
```

Expected: FAIL，因为仍有独立日志导航和逐事件同步渲染。

**Step 3: 提交 RED 测试**

```powershell
git add tests/test_panel_v2_routes.py
git commit -m "test: define combined registration log contract"
```

### Task 2: 合并注册与日志页面

**Files:**
- Modify: `panel/templates/index_v2.html`
- Modify: `panel/static/panel-v2.css`
- Modify: `panel/static/panel-v2.js`
- Test: `tests/test_panel_v2_routes.py`

**Step 1: 修改模板结构**

- 将注册导航文本改为“注册与日志”，删除独立日志导航。
- 把日志标题、错误区域、工具栏和输出区域移动到 `section-register` 的 Worker 区域之后。
- 用 `registration-log-section` 和 `registration-log-console` 提供页面间距与深链锚点。
- 增加 `logs-load-older` 和 `logs-show-latest` 按钮；把元信息改为动态计数和“浏览器最多保留 2000 条”。

**Step 2: 增加 hash 兼容**

在 JavaScript 中将 `logs` 作为兼容请求映射到 `register`。`showSection()` 接收定位意图：旧 `#logs` 或概览“查看日志”会在显示、渲染和建立 SSE 后滚动到 `registration-log-console`；已保存的旧分区偏好自动升级为 `register`。

**Step 3: 增加响应式样式**

日志区域占注册布局下方全宽；桌面与移动端工具栏允许换行，按钮和搜索框不造成页面级横向溢出。日志输出维持独立滚动区域。

**Step 4: 运行导航契约测试确认部分 GREEN**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py
```

Expected: 仅性能调度相关的新断言仍失败，模板合并断言通过。

### Task 3: 实现有界批量日志渲染

**Files:**
- Modify: `panel/static/panel-v2.js`
- Test: `tests/test_panel_v2_routes.py`

**Step 1: 增加状态和常量**

```javascript
const LOG_VISIBLE_STEP = 300;
const LOG_RENDER_INTERVAL_MS = 100;
const LOG_SEARCH_DEBOUNCE_MS = 180;
```

`state.logs` 增加 `visibleLimit`、`renderTimer`、`renderFrame`、`renderPending` 和 `searchTimer`。默认可视上限为 300。

**Step 2: 将完整渲染限制在窗口内**

`renderLogs()` 先计算全部匹配项，再使用：

```javascript
const rendered = visible.slice(-Math.min(state.logs.visibleLimit, visible.length));
```

只为 `rendered` 创建节点；计数显示当前显示、匹配和总保留数量。根据剩余匹配数量更新“加载更早”和“回到最新”的隐藏、禁用状态。

**Step 3: 实现批量调度**

`appendLogEvent()` 只更新去重缓冲并调用 `scheduleLogRender()`。调度器用一个 100ms timer 合并事件，再用一个 `requestAnimationFrame` 执行 `renderLogs()`；已有 timer/frame 时不得重复安排。暂停或不在注册分区时只标记待同步。

清空、筛选、加载更早、回到最新和恢复暂停使用立即同步；轮询回退使用同一有界渲染入口。页面隐藏或卸载时清理 timer/frame。

**Step 4: 防抖搜索并绑定窗口操作**

搜索输入先清除旧 timer，180ms 后更新 query、重置 `visibleLimit` 并同步。级别变化也重置到 300。加载更早每次增加 300；回到最新恢复 300 并滚动到底部。

**Step 5: 运行测试确认 GREEN**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py
node --check panel/static/panel-v2.js
```

Expected: PASS。

**Step 6: 提交实现**

```powershell
git add panel/templates/index_v2.html panel/static/panel-v2.css panel/static/panel-v2.js tests/test_panel_v2_routes.py
git commit -m "perf: bound and batch live log rendering"
```

### Task 4: 更新文档和 v1.10.0 Release 说明

**Files:**
- Modify: `README.md`
- Create: `docs/releases/v1.10.0.md`
- Modify: `tests/test_panel_v2_routes.py`

**Step 1: 写失败的发布文档测试**

断言 README 指向 `v1.10.0`，Release 说明包含 aiis2、注册与日志合并、2000 条内存缓冲、300 条默认可视窗口、批量刷新、旧 `#logs` 兼容和 Playwright 性能结果；禁止出现真实服务器、邮箱域名、凭据和参考仓库标识。

**Step 2: 运行测试确认 RED**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py
```

Expected: FAIL，因为 v1.10.0 文档尚不存在。

**Step 3: 更新 README 和 Release 草稿**

README 更新版本徽章和日志性能说明。`docs/releases/v1.10.0.md` 只描述本项目功能、根因、兼容性、安全边界和验证证据，不记录外部参考来源或真实环境配置。

**Step 4: 运行测试确认 GREEN 并提交**

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q tests/test_panel_v2_routes.py
git add README.md docs/releases/v1.10.0.md tests/test_panel_v2_routes.py
git commit -m "docs: prepare v1.10.0 release"
```

### Task 5: Playwright 性能与功能验收

**Files:**
- No repository files; screenshots and measurements go to `%TEMP%`.

**Step 1: 启动隔离性能面板**

使用临时 `GROK_REGISTER_DIR`，预装 2000 条日志并启动本地 Flask。不得使用或修改真实配置、账号或凭据。

**Step 2: 测量历史回放**

用 Playwright CLI 从 `#register` 加载合并页并等待日志计数达到 2000，记录：

- 页面就绪耗时；
- Long Task 数、最长时长和总时长；
- `.log-line` 节点数量；
- 页面是否仍可点击主题、筛选和暂停控件。

Expected: 默认节点不超过 300，页面约 1 秒内就绪，最长 Long Task 小于 200ms。

**Step 3: 验证突发、筛选和窗口操作**

向同一进程追加一批日志，验证批次计数最终一致且 UI 可响应；搜索、级别筛选、暂停/恢复、加载更早、回到最新、重新连接和清空本地显示均正确。

**Step 4: 验证桌面和移动端**

在 1440×1000 和 390×844 下截图到 `%TEMP%`，验证无页面级横向溢出、日志区域独立滚动、注册控件和日志工具栏可用；检查控制台错误。

### Task 6: 完整验证、合并 master 并发布

**Files:**
- No additional source files unless verification reveals defects.

**Step 1: 运行新鲜完整验证**

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q
node --check panel/static/panel-v2.js
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m compileall -q grok_register_ttk.py launcher.py panel lib tests
git diff --check
```

Expected: 全部退出 0。

**Step 2: 合并并在 master 重跑验证**

按用户已明确选择的本地合并方式将功能分支合并到 `master`，不触碰根目录未跟踪文件。合并后重复完整 pytest 和语法检查。

**Step 3: 推送并发布 v1.10.0**

检查 `gh auth status`、远程指向和 scoped diff，推送 `master` 到 `aiis2`。创建带有 `docs/releases/v1.10.0.md` 内容的公开 GitHub Release，检查 tag、master 与 Release target 指向同一提交，并等待 GitHub Actions 通过。

### Task 7: 真实注册故障排查与 1000 轮耐久验证

**Files:**
- Do not commit generated logs, account records, credentials, config or test exports.

**Step 1: 做脱敏就绪审计**

只输出配置项是否存在、路径、数量和布尔状态，不输出代理、邮箱密钥、密码、SSO 或 token。确认邮箱提供商、代理、浏览器模式、凭据目录和导出目标可用。

**Step 2: 复现并定位当前注册失败**

先运行 1 轮、1 并发；按阶段追踪创建邮箱、浏览器注册、验证码、账号落盘和 CPA 换票。若失败，先定位并修复根因，补自动化测试、重新发布补丁版本后再进行耐久测试。

**Step 3: 分级放量**

在单轮成功后运行 10 轮，再以最多 10 并发启动 1000 轮。持续记录总轮次、成功、失败、活跃 Worker、浏览器/进程数、日志缓冲与页面响应。异常率或资源泄漏明显时停止放量并保留脱敏证据，禁止盲目重复制造失败账户。

**Step 4: 验证四条产物链路**

- 日志：Playwright 打开合并页，节点上限、搜索、筛选和实时刷新正常；
- 注册记录：API 总数和新生成批次文件、成功/失败计数一致；
- SSO 刷新：触发后台刷新并等待终态，成功原子替换、失败保留旧 CPA；
- sub2api 导出：生成导出数据并验证 schema、数量、邮箱去重、OAuth 字段完整性和 JSON 可解析，不在控制台回显具体凭据。

**Step 5: 汇总耐久结果**

报告计划轮数、实际完成、成功率、失败阶段分布、峰值进程/浏览器数量、日志性能、SSO 刷新结果、注册记录一致性和 sub2api 导出校验。未完成 1000 轮时必须明确实际数字和阻塞原因，不得将部分运行描述为完成。
