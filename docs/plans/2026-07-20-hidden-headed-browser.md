# Hidden Headed Browser Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a Windows-only hidden headed Chromium mode that never intentionally activates or exposes automatic worker windows, while allowing the Web panel to show and hide each worker's exact native browser window on demand.

**Architecture:** Keep the existing per-job worker and within-batch browser reuse model. Add a small Windows window-control/Chromium-bootstrap module, emit ownership markers from each CLI worker, let the panel validate and control only registered PID/HWND pairs, and fall back to the existing minimized mode when the installed Chrome/Edge cannot perform a silent launch.

**Tech Stack:** Python 3.10+, DrissionPage 4.1, Chrome DevTools Protocol, `websocket-client`, Win32 via `ctypes`, Flask, vanilla HTML/JavaScript, pytest, GitHub CLI.

---

## Working rules

- Work on the existing `master` branch because the user explicitly requested merging to and pushing `master`.
- Preserve untracked `setup.bat` and `启动.bat`.
- Never stage `config.json`, credentials, logs, `.venv`, temporary profiles, or runtime output.
- Do not restart or stop the registration task that was already active when this plan was written.
- Use test-first RED/GREEN cycles for every production behavior.
- Run Windows browser experiments with a unique temporary Profile and exact recorded PID; never terminate by image name.

### Task 1: Define browser-window mode configuration

**Files:**

- Create: `lib/browser_window.py`
- Modify: `grok_register_ttk.py:140-205, 990-1030`
- Modify: `panel/app.py:4831-4905`
- Modify: `config.example.json`
- Modify: `tests/test_browser_lifecycle.py`
- Modify: `tests/test_panel_registration_settings.py`

**Step 1: Write failing normalization and option tests**

Add tests expressing the public contract:

```python
@pytest.mark.parametrize(
    ("value", "expected"),
    [("hidden", "hidden"), ("minimized", "minimized"), ("visible", "visible"), ("", "hidden")],
)
def test_normalize_browser_window_mode_on_windows(value, expected):
    assert normalize_browser_window_mode(value, platform="win32") == expected


def test_hidden_mode_is_headed_and_uses_silent_launch(monkeypatch):
    monkeypatch.setattr(main, "get_browser_window_mode", lambda: "hidden")
    options = main.create_browser_options()
    assert "--silent-launch" in options.arguments
    assert not any(arg.startswith("--headless") for arg in options.arguments)
    assert "--start-minimized" not in options.arguments


def test_minimized_mode_retains_compatibility_flag(monkeypatch):
    monkeypatch.setattr(main, "get_browser_window_mode", lambda: "minimized")
    assert "--start-minimized" in main.create_browser_options().arguments
```

Add panel tests proving GET/POST and `/api/job/start` persist `browser_window_mode`.

**Step 2: Run tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_browser_lifecycle.py tests\test_panel_registration_settings.py -k "window_mode or hidden_mode or minimized_mode" -q
```

Expected: FAIL because the normalizer, config field, and mode-aware options do not exist.

**Step 3: Implement the minimal configuration contract**

Create `lib/browser_window.py` with constants and a pure normalizer:

```python
WINDOW_MODE_HIDDEN = "hidden"
WINDOW_MODE_MINIMIZED = "minimized"
WINDOW_MODE_VISIBLE = "visible"


def normalize_browser_window_mode(value, *, platform=sys.platform):
    mode = str(value or "").strip().lower()
    if mode not in {WINDOW_MODE_HIDDEN, WINDOW_MODE_MINIMIZED, WINDOW_MODE_VISIBLE}:
        mode = WINDOW_MODE_HIDDEN if platform == "win32" else WINDOW_MODE_VISIBLE
    if mode == WINDOW_MODE_HIDDEN and platform != "win32":
        return WINDOW_MODE_VISIBLE
    return mode
```

Add `browser_window_mode: "hidden"` to defaults and `config.example.json`. Add
`get_browser_window_mode()` with `GROK_BROWSER_WINDOW_MODE` precedence. Make
`create_browser_options()` choose `--silent-launch`, `--start-minimized`, or neither while keeping the three background-throttling protections for hidden/minimized modes.

Expose and persist the field through `/api/config/browser` and `/api/job/start`; pass a frozen copy through `GROK_BROWSER_WINDOW_MODE` in `build_cli_batch_env()`.

**Step 4: Run tests and verify GREEN**

Run the command from Step 2. Expected: PASS.

**Step 5: Commit**

```powershell
git add lib/browser_window.py grok_register_ttk.py panel/app.py config.example.json tests/test_browser_lifecycle.py tests/test_panel_registration_settings.py
git commit -m "feat: add headed browser window modes"
```

### Task 2: Build exact Win32 PID/HWND ownership controls

**Files:**

- Modify: `lib/browser_window.py`
- Create: `tests/test_browser_window.py`

**Step 1: Write failing ownership tests**

Use an injected fake Win32 API. Test:

```python
def test_hide_rejects_hwnd_owned_by_another_pid():
    api = FakeWindowApi(hwnd_pid={701: 9002})
    controller = WindowsBrowserWindowController(api=api)
    result = controller.hide(BrowserWindowRef(pid=9001, hwnd=701, generation=2))
    assert result.ok is False
    assert result.code == "ownership_changed"
    assert api.show_calls == []


def test_hide_removes_taskbar_style_without_activating():
    api = FakeWindowApi(hwnd_pid={701: 9001}, ex_style=WS_EX_APPWINDOW)
    result = WindowsBrowserWindowController(api=api).hide(
        BrowserWindowRef(pid=9001, hwnd=701, generation=2)
    )
    assert result.ok is True
    assert api.foreground_calls == []
    assert api.show_calls[-1] == (701, SW_HIDE)


def test_show_restores_same_owned_window_only_on_explicit_request():
    api = FakeWindowApi(hwnd_pid={701: 9001}, ex_style=WS_EX_TOOLWINDOW)
    result = WindowsBrowserWindowController(api=api).show(
        BrowserWindowRef(pid=9001, hwnd=701, generation=2), activate=True
    )
    assert result.ok is True
    assert api.foreground_calls == [701]
```

Also test invalid HWND, PID reuse, idempotent hide/show, and selecting only a top-level Chrome window belonging to the exact PID.

**Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_browser_window.py -q
```

Expected: FAIL because the controller and records do not exist.

**Step 3: Implement the Win32 controller**

Add frozen records:

```python
@dataclass(frozen=True)
class BrowserWindowRef:
    worker_id: int = 1
    generation: int = 1
    pid: int = 0
    hwnd: int = 0
    mode: str = WINDOW_MODE_HIDDEN


@dataclass(frozen=True)
class WindowControlResult:
    ok: bool
    state: str
    code: str = ""
    error: str = ""
```

Wrap `IsWindow`, `GetWindowThreadProcessId`, `EnumWindows`, `IsWindowVisible`,
`GetWindowLongPtrW`, `SetWindowLongPtrW`, `SetWindowPos`, `ShowWindowAsync`, and
`SetForegroundWindow` with explicit ctypes arg/restype declarations. `hide()` must
remove `WS_EX_APPWINDOW`, add `WS_EX_TOOLWINDOW`, call `SW_HIDE`, and never call
`SetForegroundWindow`. `show()` reverses those styles, restores the window, and only
activates when its caller passes `activate=True`.

**Step 4: Run tests and verify GREEN**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```powershell
git add lib/browser_window.py tests/test_browser_window.py
git commit -m "feat: control only owned browser windows"
```

### Task 3: Bootstrap a standard headed Chromium window without startup activation

**Files:**

- Modify: `lib/browser_window.py`
- Modify: `grok_register_ttk.py:911-1030, 2536-2789`
- Modify: `tests/test_browser_window.py`
- Modify: `tests/test_browser_lifecycle.py`

**Step 1: Write failing CDP bootstrap tests**

Inject fake Popen, HTTP version polling, WebSocket, and Win32 controller. Assert:

```python
def test_silent_bootstrap_creates_headed_background_minimized_window():
    result = bootstrap_hidden_chromium(
        port=19222,
        browser_path="chrome.exe",
        arguments=["--user-data-dir=X", "--silent-launch"],
        controller=controller,
        popen=popen,
        version_reader=version_reader,
        websocket_factory=websocket_factory,
    )
    assert not any(arg.startswith("--headless") for arg in popen.arguments)
    assert websocket.sent["method"] == "Target.createTarget"
    assert websocket.sent["params"] == {
        "url": "about:blank",
        "newWindow": True,
        "background": True,
        "focus": False,
        "windowState": "minimized",
    }
    assert controller.hidden_pid == result.launcher_pid
```

Add failure tests proving a partially launched exact process tree is terminated and a typed `HiddenLaunchError` is raised without containing command-line secrets.

Add lifecycle tests proving `start_browser()`:

- uses the hidden bootstrap only for Windows Chromium hidden mode;
- records launcher PID, browser PID, HWND, generation and actual mode;
- falls back once to minimized mode when hidden bootstrap fails;
- never falls back to headless mode.

**Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_browser_window.py tests\test_browser_lifecycle.py -k "bootstrap or hidden_launch or fallback" -q
```

Expected: FAIL because the bootstrap and integration do not exist.

**Step 3: Implement the minimal CDP bootstrap**

The bootstrap must:

1. launch the exact executable and arguments passed by DrissionPage plus the selected debugging port;
2. poll `http://127.0.0.1:<port>/json/version` without using environment proxies;
3. connect to `webSocketDebuggerUrl` with origin suppression;
4. issue one `Target.createTarget` request using the parameters asserted above;
5. wait for the exact process window and hide it before returning;
6. return a record containing Popen, launcher PID, target id and HWND.

Add a scoped context manager around `DrissionPage._functions.browser._run_browser`.
Restore the original function in `finally`; do not edit `.venv`. Capture the returned
Popen PID because DrissionPage otherwise discards it.

Extend `_owned_browser` with `launcher_pid`, `hwnd`, `generation`, `requested_mode`,
`actual_mode`, and `fallback`. Make `stop_browser()` terminate only these captured
processes if normal Chromium quit does not complete.

**Step 4: Run tests and verify GREEN**

Run Step 2. Expected: PASS.

**Step 5: Run adjacent regression tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_browser_lifecycle.py tests\test_turnstile_recovery.py tests\test_panel_batch_runner.py -q
```

Expected: all pass.

**Step 6: Commit**

```powershell
git add lib/browser_window.py grok_register_ttk.py tests/test_browser_window.py tests/test_browser_lifecycle.py
git commit -m "feat: launch hidden headed Chromium workers"
```

### Task 4: Publish browser ownership markers to the panel

**Files:**

- Modify: `lib/browser_window.py`
- Modify: `grok_register_ttk.py:2666-2789, 4825-5058`
- Modify: `panel/app.py:1840-2075, 2260-2380`
- Modify: `tests/test_browser_window.py`
- Modify: `tests/test_panel_batch_runner.py`

**Step 1: Write failing marker tests**

Define a stable, secret-free format:

```text
@@GROK_BROWSER_WINDOW worker=3 generation=4 pid=9300 hwnd=701 state=hidden mode=hidden fallback=0
```

Test formatting/parsing, malformed values, unknown states, and that command line,
Profile path, proxy, URL and credentials cannot appear.

Test `supervise_batch_process()` updates only the matching worker browser record and a
new generation replaces the old HWND.

**Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_browser_window.py tests\test_panel_batch_runner.py -k "browser_marker or browser_record" -q
```

Expected: FAIL because browser markers are not consumed.

**Step 3: Implement marker publishing and registry updates**

Emit a marker after every successful Chromium start/restart. Add a parser before the
round-marker parser in `supervise_batch_process()`. Store this shape under each worker:

```python
worker["browser"] = {
    "generation": generation,
    "pid": pid,
    "hwnd": hwnd,
    "state": state,
    "mode": actual_mode,
    "fallback": fallback,
}
```

Reject a lower generation and clear the record when the worker is unregistered or the
task finishes.

**Step 4: Run tests and verify GREEN**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```powershell
git add lib/browser_window.py grok_register_ttk.py panel/app.py tests/test_browser_window.py tests/test_panel_batch_runner.py
git commit -m "feat: report worker browser window ownership"
```

### Task 5: Add safe per-worker show/hide APIs

**Files:**

- Modify: `panel/app.py:1780-1840, 4781-4920`
- Modify: `tests/test_panel_registration_settings.py`

**Step 1: Write failing API tests**

Cover:

- show/hide requires a running job and known worker;
- stale HWND or mismatched PID returns HTTP 409;
- Camoufox and missing-window states return HTTP 409;
- showing W2 hides all other registered valid browser windows first;
- the controller receives only the currently registered generation;
- successful response returns the new public browser state.

Example:

```python
def test_show_worker_browser_hides_other_owned_windows_first(monkeypatch):
    calls = []
    seed_worker_browsers(w1=hidden_ref(1), w2=hidden_ref(2))
    monkeypatch.setattr(panel_app, "control_worker_browser", lambda ref, action: calls.append((ref.worker_id, action)) or ok(action))
    response = panel_app.app.test_client().post("/api/job/workers/2/browser/show")
    assert response.status_code == 200
    assert calls == [(1, "hide"), (2, "show")]
```

**Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_panel_registration_settings.py -k "worker_browser" -q
```

Expected: 404 or missing helper failures.

**Step 3: Implement API validation and control**

Add the show/hide routes and one shared validator. Hold `_job_lock` only while taking a
stable snapshot; perform Win32 calls outside the lock; reacquire it to update state only
if generation/PID/HWND remain unchanged.

Never enumerate or control a window that is not in the current worker registry.

**Step 4: Run tests and verify GREEN**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```powershell
git add panel/app.py tests/test_panel_registration_settings.py
git commit -m "feat: expose safe worker browser controls"
```

### Task 6: Add browser-window controls to the Web UI

**Files:**

- Modify: `panel/app.py:2720-3920`
- Modify: `tests/test_panel_registration_settings.py`

**Step 1: Write failing HTML/JS contract tests**

Assert the page contains:

- `browser_window_mode` select with hidden/minimized/visible values;
- `controlWorkerBrowser(workerId, action)`;
- worker browser PID/mode/fallback status;
- show/hide button generated without `innerHTML` from server values;
- controls disabled for Camoufox, missing browser, inactive worker or request in flight;
- the window-mode select is locked while a job runs.

**Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_panel_registration_settings.py -k "window_controls or browser_window" -q
```

Expected: FAIL because the UI is absent.

**Step 3: Implement the UI**

Add the mode select beside `browser_engine`. Extend `loadBrowserConfig()`,
`saveBrowserEngine()`, `startJob()` and polling. Build worker buttons with
`document.createElement`, `textContent`, and fixed action strings. Display a warning
badge when `fallback=true`.

**Step 4: Run tests and verify GREEN**

Run Step 2. Expected: PASS.

**Step 5: Commit**

```powershell
git add panel/app.py tests/test_panel_registration_settings.py
git commit -m "feat: add worker browser visibility controls"
```

### Task 7: Document the feature and prepare release v1.6.0

**Files:**

- Modify: `README.md`
- Modify: `config.example.json`
- Create: `docs/releases/v1.6.0.md`

**Step 1: Update README**

Document:

- `hidden` is standard headed Chromium, not headless;
- automatic starts/restarts stay hidden;
- the panel can explicitly show/hide the exact active worker;
- `minimized` is the compatibility fallback;
- task completion still releases all worker/browser resources;
- existing within-batch reuse and restart conditions.

**Step 2: Write the aiis2 release notes**

Describe the main `aiis2` changes since v1.5.0, emphasizing hidden headed windows,
safe ownership validation, UI controls, background-throttling protection, Turnstile
recovery compatibility, tests, and the lack of committed secrets.

**Step 3: Verify documentation links and diff**

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors; only task files plus preserved user batch files.

**Step 4: Commit**

```powershell
git add README.md config.example.json docs/releases/v1.6.0.md
git commit -m "docs: prepare v1.6.0 hidden browser release"
```

### Task 8: Complete automated and Windows runtime verification

**Files:**

- Test only; do not persist runtime output.

**Step 1: Run focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_browser_window.py tests\test_browser_lifecycle.py tests\test_turnstile_recovery.py tests\test_panel_batch_runner.py tests\test_panel_registration_settings.py -q
```

Expected: zero failures.

**Step 2: Run the complete test suite and compile checks**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q grok_register_ttk.py panel lib tests
git diff --check HEAD^..HEAD
```

Expected: zero failures and zero compile/diff errors.

**Step 3: Run one isolated Windows hidden-browser probe**

Use an explicit port outside the active worker ranges and a unique temporary Profile.
Record exact launcher PID before creating the target. Verify:

- no command line argument starts with `--headless`;
- browser user agent lacks `HeadlessChrome`;
- a real HWND exists for the exact PID;
- that PID never becomes the foreground PID during hidden launch;
- hidden HWND is not visible and has no `WS_EX_APPWINDOW` taskbar style;
- show makes the same HWND visible;
- URL and a test DOM sentinel remain unchanged across show/hide;
- hide removes it again;
- exact test process tree returns to zero after cleanup.

If an unrelated active worker changes the foreground window, do not count it as this
test browser stealing focus. If hidden bootstrap fails on installed Chromium, verify and
report minimized compatibility fallback instead of claiming hidden mode works.

**Step 4: Validate the panel without restarting the active production task**

Use Flask test client and, only when it can be run on a separate port/process without
touching the active panel, a browser UI check. Confirm configuration controls and worker
buttons render correctly. Do not restart port 8787 while its existing job is active.

**Step 5: Review repository hygiene**

```powershell
git status --short
git log --oneline --decorate -8
git diff aiis2/master...HEAD --stat
```

Expected: no secrets/runtime artifacts; untracked user batch files remain unstaged.

### Task 9: Push master and publish the GitHub release

**Files:** None.

**Step 1: Inspect remote and release state**

```powershell
git remote -v
git ls-remote --heads aiis2 master
gh release list --repo aiis2/grok-register-win --limit 10
```

Confirm the target is the public `aiis2/grok-register-win` repository and determine
whether `v1.6.0` already exists.

**Step 2: Push the verified master branch**

```powershell
git push aiis2 master
```

Expected: remote master advances to local HEAD.

**Step 3: Create or update v1.6.0**

If absent:

```powershell
gh release create v1.6.0 --repo aiis2/grok-register-win --target master --title "v1.6.0 · Windows 隐藏有头浏览器" --notes-file docs/releases/v1.6.0.md
```

If present, update it with `gh release edit` and the same title/notes file.

**Step 4: Verify public state**

```powershell
git ls-remote aiis2 refs/heads/master refs/tags/v1.6.0
gh release view v1.6.0 --repo aiis2/grok-register-win --json url,name,isDraft,isPrerelease,targetCommitish
```

Expected: public master and release/tag point to the verified implementation; release is
neither draft nor prerelease.

