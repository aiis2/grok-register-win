# Hidden Headed Browser Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Upgrade the existing Windows `hidden` headed-Chromium mode so its first native window is created offscreen, never intentionally activated during automatic paths, remains absent from the taskbar, and can still be restored safely by an explicit panel action.

**Architecture:** Add an early offscreen/hidden launch layer to the existing CDP bootstrap, then strengthen the exact PID/HWND Win32 controller with verifiable no-activate visibility operations. Keep the current browser lifecycle, reuse, fallback, and panel API contracts unchanged; only normalize an offscreen window back into the desktop during an explicit show request.

**Tech Stack:** Python 3.10+, subprocess/STARTUPINFO, Chromium DevTools Protocol, ctypes Win32 APIs, pytest.

---

### Task 1: Lock the hidden launch contract with failing tests

**Files:**
- Modify: `tests/test_browser_window.py`
- Test: `tests/test_browser_window.py`

**Step 1: Extend the existing bootstrap test**

Assert that the spawned command contains exactly one project-controlled offscreen switch and that CDP receives matching initial bounds:

```python
assert launched_arguments.count("--window-position=-32000,-32000") == 1
assert websocket.sent[0]["params"] == {
    "url": "about:blank",
    "newWindow": True,
    "background": True,
    "focus": False,
    "windowState": "minimized",
    "left": -32000,
    "top": -32000,
}
```

Pass a conflicting `--window-position=10,20` input and verify it is removed.

**Step 2: Add a Windows startup-info test**

Capture the Popen keyword arguments and assert:

```python
startupinfo = popen_calls[0][1]["startupinfo"]
assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW
assert startupinfo.wShowWindow == subprocess.SW_HIDE
```

Keep the test platform-independent by injecting or monkeypatching the startup-info builder where needed.

**Step 3: Run the focused tests and verify RED**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest tests/test_browser_window.py -k "silent_bootstrap or startup_info" -q
```

Expected: failures because the offscreen switch, CDP coordinates, and Popen startup information are not yet supplied.

**Step 4: Commit only the failing tests if a checkpoint is needed**

Do not make a standalone failing-test commit on the shared feature branch unless execution must pause.

---

### Task 2: Implement the pre-window offscreen launch layer

**Files:**
- Modify: `lib/browser_window.py:17-42`
- Modify: `lib/browser_window.py:238-338`
- Test: `tests/test_browser_window.py`

**Step 1: Define owned hidden-position constants and a startup-info helper**

Add constants and a small helper whose non-Windows result is `None`:

```python
HIDDEN_WINDOW_X = -32000
HIDDEN_WINDOW_Y = -32000
HIDDEN_WINDOW_POSITION_ARGUMENT = (
    f"--window-position={HIDDEN_WINDOW_X},{HIDDEN_WINDOW_Y}"
)


def build_hidden_startupinfo(*, platform=None):
    current_platform = sys.platform if platform is None else str(platform)
    if current_platform != "win32":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return startupinfo
```

**Step 2: Normalize the hidden command before Popen**

Remove existing `--window-position` values, append the owned value exactly once, and continue rejecting every `--headless*` argument:

```python
launch_arguments = [
    item
    for item in launch_arguments
    if not item.startswith("--window-position=")
]
launch_arguments.append(HIDDEN_WINDOW_POSITION_ARGUMENT)
```

Build Popen keyword arguments once and attach `startupinfo` only when the helper returns a value.

**Step 3: Reinforce the first CDP window position**

Add `left` and `top` to `Target.createTarget` while preserving the existing background/focus/minimized settings.

**Step 4: Run the focused launch tests and verify GREEN**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest tests/test_browser_window.py -k "silent_bootstrap or startup_info or failed_bootstrap" -q
```

Expected: all selected tests pass, including process-tree cleanup and secret-redaction coverage.

**Step 5: Commit the launch layer**

```powershell
git add -- lib/browser_window.py tests/test_browser_window.py
git commit -m "fix: hide headed Chromium before first window"
```

---

### Task 3: Lock native hide/show behavior with failing tests

**Files:**
- Modify: `tests/test_browser_window.py`
- Test: `tests/test_browser_window.py`

**Step 1: Expand `FakeWindowApi`**

Track window rectangles, virtual-screen bounds, primary work area, and no-activate position calls:

```python
def hide_window_no_activate(self, hwnd):
    self.hide_no_activate_calls.append(int(hwnd))
    self.visible[int(hwnd)] = False
    return True

def window_rect(self, hwnd):
    return self.window_rects[int(hwnd)]

def virtual_screen_rect(self):
    return self.virtual_rect

def primary_work_area(self):
    return self.work_area

def move_window_no_activate(self, hwnd, x, y):
    self.move_calls.append((int(hwnd), int(x), int(y)))
    return True
```

**Step 2: Add hide verification and rollback tests**

Verify that automatic hide:

- mutates only the owned HWND;
- uses the no-activate hide operation;
- never calls `set_foreground_window`;
- returns `hide_failed` and restores the original extended style if the operation fails or the window remains visible.

**Step 3: Add explicit-show positioning tests**

Cover both cases:

```python
# Initial/minimized offscreen rectangle is normalized before activation.
assert api.move_calls == [(701, 64, 64)]
assert api.foreground_calls == [701]

# An already visible-desktop rectangle is preserved across hide/show.
assert api.move_calls == []
```

Also make `show_window()` return `False` while updating visibility, proving that `ShowWindowAsync`'s previous-state return value is not treated as failure.

**Step 4: Run the controller tests and verify RED**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest tests/test_browser_window.py -k "hide or show" -q
```

Expected: failures because the controller still uses the old async return-value contract and has no offscreen normalization.

---

### Task 4: Implement verifiable no-activate HWND control

**Files:**
- Modify: `lib/browser_window.py:32-42`
- Modify: `lib/browser_window.py:388-508`
- Modify: `lib/browser_window.py:511-590`
- Test: `tests/test_browser_window.py`

**Step 1: Extend the typed Win32 surface**

Bind `GetWindowRect`, `GetSystemMetrics`, and `SystemParametersInfoW`, and add a SetWindowPos helper using:

```python
SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER |
SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_HIDEWINDOW
```

The helper must return the Win32 BOOL and must never call a foreground API.

**Step 2: Add rectangle and desktop helpers**

Expose immutable `(left, top, right, bottom)` tuples for the HWND, virtual desktop, and primary work area. Use a pure intersection helper:

```python
def rectangles_intersect(first, second):
    return (
        first[0] < second[2]
        and first[2] > second[0]
        and first[1] < second[3]
        and first[3] > second[1]
    )
```

The safe explicit-show origin is the primary work area's left/top plus 64 DIP, clamped inside the work area.

If a browser created with Windows `SW_HIDE` reports a zero-sized initial rectangle,
seed an offscreen `1280x800` rectangle with `SetWindowPos` and `SWP_NOACTIVATE`
before hiding it. This preserves the early hidden-startup defense while ensuring
that a later explicit show restores a usable native window.

**Step 3: Strengthen `hide()`**

After ownership validation, remember the original style, apply the tool-window style, issue the no-activate hide, and verify `IsWindowVisible` is false. On any failure restore the original style before returning `hide_failed`.

**Step 4: Correct and strengthen `show()`**

Do not interpret a zero `ShowWindowAsync` return as an error. Restore the visible taskbar style, request restore, verify eventual visibility, move the window only if its rectangle does not intersect the virtual desktop, then call `SetForegroundWindow` only when `activate=True`.

**Step 5: Run controller tests and verify GREEN**

Run:

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest tests/test_browser_window.py -k "hide or show" -q
```

Expected: all selected tests pass.

**Step 6: Commit the native controller layer**

```powershell
git add -- lib/browser_window.py tests/test_browser_window.py
git commit -m "fix: prevent hidden browser window activation"
```

---

### Task 5: Run lifecycle and full regression verification

**Files:**
- Verify: `tests/test_browser_window.py`
- Verify: `tests/test_browser_lifecycle.py`
- Verify: `tests/test_browser_runtime.py`
- Verify: all `tests/`

**Step 1: Run browser-focused regression tests**

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest tests/test_browser_window.py tests/test_browser_lifecycle.py tests/test_browser_runtime.py -q
```

Expected: PASS with no browser lifecycle or fallback regression.

**Step 2: Run static validation**

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m compileall -q lib grok_register_ttk.py panel
git diff --check
```

Expected: both commands exit 0.

**Step 3: Run the full suite**

```powershell
& 'D:\python_project\grok-register-win\.venv\Scripts\python.exe' -m pytest -q
```

Expected: the repository's complete test suite passes.

---

### Task 6: Perform an isolated Windows headed-browser probe

**Files:**
- Reuse: `lib/browser_window.py`
- Do not commit: temporary profile/probe output

**Step 1: Resolve an installed Chrome or Edge executable**

Use `resolve_chromium_executable("chrome")`; if unavailable, try the existing configured Chromium path. Do not alter application configuration.

**Step 2: Start a probe with an unused local CDP port and temporary Profile**

Record the foreground HWND/PID immediately before launch. Sample foreground ownership while `bootstrap_hidden_chromium()` starts the browser.

**Step 3: Verify hidden state**

Require all of the following for the captured HWND:

- PID ownership matches the returned launcher PID;
- `IsWindowVisible` is false;
- `WS_EX_TOOLWINDOW` is present;
- `WS_EX_APPWINDOW` is absent;
- the captured browser PID was never the sampled foreground PID.

**Step 4: Verify explicit show and re-hide**

Call `show(..., activate=True)`, confirm the same HWND is inside the virtual desktop, then call `hide()` and confirm it is invisible again without changing PID/HWND.

**Step 5: Clean up exact resources**

Terminate only the captured process tree, wait for exit, and remove only the explicitly created temporary Profile directory after verifying its resolved absolute path is under the temporary directory.

**Step 6: Record residual limits honestly**

If high-frequency foreground sampling cannot prove the entire process-creation interval, report the measured sample interval and state that the change reduces the risk rather than claiming an absolute platform-wide guarantee.

---

### Task 7: Finalize the task-related commit

**Files:**
- Modify if needed: `docs/plans/2026-07-22-hidden-headed-browser-hardening-design.md`
- Modify if needed: `docs/plans/2026-07-22-hidden-headed-browser-hardening-plan.md`

**Step 1: Inspect scope**

```powershell
git status --short
git diff --stat
git diff --check
```

Expected: only the hidden-browser hardening files are present; no configuration, credential, log, Profile, or user batch files are staged.

**Step 2: Commit any remaining verified documentation changes**

```powershell
git add -- docs/plans/2026-07-22-hidden-headed-browser-hardening-design.md docs/plans/2026-07-22-hidden-headed-browser-hardening-plan.md
git commit -m "docs: document hidden browser hardening"
```

Skip the commit if there are no remaining changes.
