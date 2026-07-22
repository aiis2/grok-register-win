# Browser Taskbar Hiding and SSO Refresh Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Correctly hide the real headed Chromium main window from the Windows taskbar and add a safe, explicit full-SSO OAuth/CPA refresh operation to both panel interfaces.

**Architecture:** Rank exact-PID Chromium HWND candidates so `Chrome_WidgetWin_1` is selected ahead of internal helper windows, then hide and re-scan during a bounded settle period. Reuse the existing serial CPA worker for full refresh, but make force bypass only completed-state deduplication, preserve inflight deduplication, and atomically replace CPA files only after successful conversion.

**Tech Stack:** Python 3.10+, Flask, ctypes/Win32, DrissionPage CDP bootstrap, vanilla HTML/CSS/JavaScript, pytest, Git/GitHub CLI.

---

### Task 1: Select the real Chromium main HWND

**Files:**
- Modify: `lib/browser_window.py:645-724`
- Test: `tests/test_browser_window.py`

**Step 1: Write the failing tests**

Add tests where `enum_windows()` returns an internal hidden `Chrome_WidgetWin_0` before a visible `Chrome_WidgetWin_1`. Assert that `find_window_for_pid()` returns the `_1` HWND. Add a compatibility case where no `_1` exists and the controller selects the largest visible, non-tool Chromium window for the exact PID. Include another PID and assert it is never selected.

```python
def test_find_window_prefers_real_chromium_main_window():
    api = FakeWindowApi(
        hwnd_pid={701: 9001, 702: 9001, 703: 7777},
        class_names={
            701: "Chrome_WidgetWin_0",
            702: "Chrome_WidgetWin_1",
            703: "Chrome_WidgetWin_1",
        },
        visibility={701: False, 702: True, 703: True},
        ex_styles={701: WS_EX_TOOLWINDOW, 702: 0, 703: 0},
        rects={701: (0, 0, 0, 0), 702: (-32000, -32000, -31801, -31966)},
    )
    assert WindowsBrowserWindowController(api=api).find_window_for_pid(9001) == 702
```

**Step 2: Run the tests and verify RED**

Run:

```powershell
& D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_browser_window.py -k "find_window" -q
```

Expected: the new test fails because the current method returns the first `_0` candidate.

**Step 3: Implement deterministic candidate ranking**

Add `find_windows_for_pid()` and make `find_window_for_pid()` return its first item. Filter by exact PID and Chromium class. Rank exact `Chrome_WidgetWin_1` first, then visible non-tool positive-area compatibility candidates; exclude hidden internal `_0` windows from fallback selection.

```python
def find_windows_for_pid(self, pid: int) -> list[int]:
    candidates = []
    for hwnd in self.api.enum_windows():
        if self.api.window_pid(hwnd) != pid:
            continue
        class_name = self.api.class_name(hwnd)
        if not class_name.startswith("Chrome_WidgetWin_"):
            continue
        style = self.api.get_ex_style(hwnd)
        rect = self.api.window_rect(hwnd)
        area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
        is_main = class_name == "Chrome_WidgetWin_1"
        is_fallback = self.api.is_window_visible(hwnd) and not style & WS_EX_TOOLWINDOW and area > 0
        if not (is_main or is_fallback):
            continue
        candidates.append(((0 if is_main else 1, 0 if self.api.is_window_visible(hwnd) else 1, -area, hwnd), hwnd))
    return [hwnd for _score, hwnd in sorted(candidates)]
```

**Step 4: Run tests and verify GREEN**

Run the Task 1 test command, then all `tests/test_browser_window.py`.

**Step 5: Commit**

```powershell
git add lib/browser_window.py tests/test_browser_window.py
git commit -m "fix: select the real Chromium main window"
```

### Task 2: Hide delayed or replaced Chromium main windows

**Files:**
- Modify: `lib/browser_window.py:288-416`
- Test: `tests/test_browser_window.py`

**Step 1: Write a failing bootstrap test**

Extend the fake controller so successive `find_windows_for_pid()` calls expose one main HWND and then a delayed replacement. Assert bootstrap hides both exact-PID main HWNDs, returns the final current HWND, and never calls `show` or a foreground API.

```python
def test_hidden_bootstrap_hides_main_windows_until_settled():
    controller = SequencedController([[702], [702, 704], [702, 704]])
    result = bootstrap_hidden_chromium(..., controller=controller, settle_time=0.02)
    assert controller.hidden_hwnds == [702, 704]
    assert result.hwnd == 704
```

**Step 2: Run the test and verify RED**

Expected: `bootstrap_hidden_chromium()` hides one HWND and returns before the delayed replacement appears.

**Step 3: Implement bounded settle scanning**

Add `settle_time` with a small production default. After the first eligible main window, repeatedly enumerate exact-PID main candidates until no new HWND appears for the settle interval or the existing global deadline is reached. Hide each new HWND once and verify no candidate remains visible. Return the last preferred main HWND.

**Step 4: Run browser-window and lifecycle tests**

```powershell
& D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_browser_window.py tests/test_browser_lifecycle.py -q
```

**Step 5: Commit**

```powershell
git add lib/browser_window.py tests/test_browser_window.py
git commit -m "fix: settle hidden Chromium main windows"
```

### Task 3: Add safe full-SSO refresh queue semantics

**Files:**
- Modify: `panel/app.py:1193-1367`
- Create: `tests/test_panel_sso_refresh.py`

**Step 1: Write failing queue tests**

Cover:

- `force=True` bypasses `_cpa_done` but still rejects `_cpa_inflight`;
- unique local SSO records are queued up to a 10000 limit;
- result contains `total`, `queued`, `skipped` and reason counts;
- no response/result exposes raw SSO or passwords.

```python
def test_force_refresh_never_duplicates_inflight(monkeypatch):
    panel_app._cpa_inflight.add(panel_app.sso_fingerprint("sso-one"))
    queued, reason = panel_app.enqueue_cpa_convert("one@example.com", "sso-one", force=True)
    assert queued is False
    assert reason == "already queued"
```

**Step 2: Run tests and verify RED**

```powershell
& D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_panel_sso_refresh.py -q
```

Expected: force currently bypasses inflight protection and the bulk helper does not exist.

**Step 3: Implement minimal queue changes**

Split deduplication checks in `enqueue_cpa_convert()`:

```python
if fp in _cpa_inflight:
    return False, "already queued"
if not force and fp in _cpa_done:
    return False, "already converted"
```

Add `enqueue_all_sso_refresh(limit=1000)` using the existing account parser and serial worker. Clamp the limit to `1..10000`.

**Step 4: Verify GREEN and run workspace tests**

Run the new test file plus `tests/test_panel_sso_workspace.py` and `tests/test_panel_credential_storage.py`.

**Step 5: Commit**

```powershell
git add panel/app.py tests/test_panel_sso_refresh.py
git commit -m "feat: add safe full SSO refresh queue"
```

### Task 4: Preserve old CPA on failure and atomically replace on success

**Files:**
- Modify: `panel/app.py:1266-1367`
- Test: `tests/test_panel_sso_refresh.py`

**Step 1: Write failing worker tests**

Create an isolated `CpaPaths`, pre-write an existing CPA canary, and run one worker item followed by the sentinel:

- when `convert_one()` raises, the canary file remains byte-for-byte unchanged;
- when conversion succeeds, `_write_json_atomic()` is used and the final JSON contains the refreshed values;
- temporary files are absent afterward.

**Step 2: Run tests and verify RED**

Expected: success currently calls `Path.write_text()` directly, so the atomic-write assertion fails.

**Step 3: Use the existing atomic JSON helper**

Replace direct CPA output writes with:

```python
_write_json_atomic(path, entry)
```

Do not delete or truncate an existing CPA anywhere in the exception path.

**Step 4: Verify GREEN**

Run `tests/test_panel_sso_refresh.py` and all credential tests.

**Step 5: Commit**

```powershell
git add panel/app.py tests/test_panel_sso_refresh.py
git commit -m "fix: preserve CPA during SSO refresh failures"
```

### Task 5: Expose the refresh API with concurrency guards

**Files:**
- Modify: `panel/app.py:5879-5903`
- Test: `tests/test_panel_sso_refresh.py`

**Step 1: Write failing route tests**

Cover POST `/api/cpa/refresh-all` for:

- authenticated success with `limit=10000`;
- invalid limits normalized safely;
- no usable SSO returns 400;
- registration running returns 409;
- credential import or migration lock busy returns 409;
- response contains only counts/message/CPA public stats.

**Step 2: Verify RED**

Expected: route returns 404.

**Step 3: Implement the route**

Acquire locks in the established order: `_credential_import_lock`, `_activity_lock`, then `_credential_migration_lock`, all non-blocking. Re-check registration after acquiring `_activity_lock`. Release in reverse order in `finally`. Queue through `enqueue_all_sso_refresh()` and log count-only metadata.

**Step 4: Verify GREEN**

Run the new tests and the existing panel registration/workspace tests.

**Step 5: Commit**

```powershell
git add panel/app.py tests/test_panel_sso_refresh.py
git commit -m "feat: expose guarded SSO refresh API"
```

### Task 6: Add explicit refresh controls to modern and legacy UI

**Files:**
- Modify: `panel/templates/index_v2.html:378-413`
- Modify: `panel/static/panel-v2.js:866-965,1642-1669`
- Modify: `panel/app.py` legacy `INDEX_HTML`
- Test: `tests/test_panel_v2_routes.py`
- Test: `tests/test_panel_registration_settings.py`

**Step 1: Write failing UI contract tests**

Assert both interfaces contain a visible refresh control, explanatory copy that this does not create a new Web SSO, the safe failure policy, a confirmation step, and a request to `/api/cpa/refresh-all`. Assert the V2 handler renders API error text through the existing inline error component.

**Step 2: Verify RED**

Run the two targeted test files; expect missing control/handler failures.

**Step 3: Implement V2 UI**

Add `#cpa-refresh-all` next to backfill. Reuse `#cpa-backfill-limit` but relabel it “处理上限” and change max to 10000. `refreshAllSso()` must call `confirmAction()`, set the credentials busy group, POST `{limit}`, show counts, refresh CPA/job state, and always restore controls.

**Step 4: Implement legacy UI**

Add a clearly named button and `refreshAllSso()` using `window.confirm`, the same endpoint and limit. Keep existing IDs and functions unchanged.

**Step 5: Verify GREEN and JavaScript syntax**

```powershell
& D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest tests/test_panel_v2_routes.py tests/test_panel_registration_settings.py tests/test_panel_sso_refresh.py -q
node --check panel/static/panel-v2.js
```

**Step 6: Commit**

```powershell
git add panel/app.py panel/templates/index_v2.html panel/static/panel-v2.js tests/test_panel_v2_routes.py tests/test_panel_registration_settings.py
git commit -m "feat: add SSO refresh controls to the panel"
```

### Task 7: Run native and browser verification

**Files:**
- Modify if needed: `tests/test_browser_window.py`
- Do not commit: temporary Chrome Profile, CDP state, screenshots or probe output

**Step 1: Run the full automated suite**

```powershell
& D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest -q
node --check panel/static/panel-v2.js
& D:\python_project\grok-register-win\.venv\Scripts\python.exe -m compileall -q grok_register_ttk.py launcher.py panel lib tests
git diff --check
```

**Step 2: Run an isolated Windows hidden-headed probe**

Use a temporary Profile and unused CDP port. Sample all exact-process-tree `Chrome_WidgetWin_*` windows from launch through settle. Require:

- captured HWND class is `Chrome_WidgetWin_1` or documented compatibility fallback;
- captured HWND is not visible after hidden startup;
- `WS_EX_TOOLWINDOW` present and `WS_EX_APPWINDOW` absent;
- no visible `Chrome_WidgetWin_1` remains for the target PID;
- the launched browser never becomes the sampled foreground window;
- no `--headless` argument and UA contains no `HeadlessChrome`;
- explicit non-activating show and re-hide reuse the same PID/HWND;
- exact process tree and temporary Profile are cleaned afterward.

**Step 3: Run isolated panel browser checks**

Start the panel with temporary `GROK_REGISTER_DIR`, `PANEL_AUTH=0`, `AUTO_CPA=0` and an unused port. In Playwright:

- open `/`, navigate to Credentials, confirm refresh button is visible;
- click and cancel confirmation, proving no request is made;
- with an empty account workspace, accept and verify the inline backend error;
- open `/?ui=legacy` and verify the equivalent control;
- check desktop and 390px layout overflow and console errors.

**Step 4: Commit only regression-test adjustments if required**

### Task 8: Document and publish v1.9.0

**Files:**
- Modify: `README.md`
- Create: `docs/releases/v1.9.0.md`
- Test: appropriate documentation contract test

**Step 1: Write the release documentation test**

Require the README and release note to describe actual main-window selection, no-taskbar postcondition, full SSO refresh, 10000 maximum, failure preservation, and legacy fallback without naming external reference repositories or including secrets.

**Step 2: Verify RED, then update docs**

Update the version badge to `v1.9.0`, browser mode wording, credential section and troubleshooting. Add `docs/releases/v1.9.0.md` with `aiis2` changes and verification evidence.

**Step 3: Run final verification and commit**

```powershell
& D:\python_project\grok-register-win\.venv\Scripts\python.exe -m pytest -q
node --check panel/static/panel-v2.js
& D:\python_project\grok-register-win\.venv\Scripts\python.exe -m compileall -q grok_register_ttk.py launcher.py panel lib tests
git diff --check
git status -sb
git add README.md docs/releases/v1.9.0.md tests
git commit -m "docs: prepare v1.9.0 release"
```

**Step 4: Merge, push and release**

From the clean `master` worktree, fetch `aiis2/master`, confirm no divergence, fast-forward merge `codex/browser-hide-sso-refresh`, rerun the complete suite, push `master`, wait for GitHub Actions Tests success, create annotated tag `v1.9.0`, push it and publish the verified release notes with `gh release create --repo aiis2/grok-register-win`.
