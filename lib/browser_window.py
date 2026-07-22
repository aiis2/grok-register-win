"""Browser window mode and Windows native-window helpers."""

from __future__ import annotations

import sys
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


WINDOW_MODE_HIDDEN = "hidden"
WINDOW_MODE_MINIMIZED = "minimized"
WINDOW_MODE_VISIBLE = "visible"
WINDOW_MODES = {
    WINDOW_MODE_HIDDEN,
    WINDOW_MODE_MINIMIZED,
    WINDOW_MODE_VISIBLE,
}
BROWSER_WINDOW_STATES = {"hidden", "minimized", "visible", "closed", "error"}
_BROWSER_WINDOW_MARKER_RE = re.compile(
    r"@@GROK_BROWSER_WINDOW\s+worker=(\d+)\s+generation=(\d+)\s+"
    r"pid=(\d+)\s+hwnd=(\d+)\s+state=([a-z]+)\s+"
    r"mode=([a-z]+)\s+fallback=([01])"
)

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
SW_HIDE = 0
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
HIDDEN_WINDOW_X = -32000
HIDDEN_WINDOW_Y = -32000
HIDDEN_WINDOW_POSITION_ARGUMENT = (
    f"--window-position={HIDDEN_WINDOW_X},{HIDDEN_WINDOW_Y}"
)


def normalize_browser_window_mode(value, *, platform: str | None = None) -> str:
    """Normalize the configured headed-window mode for the current platform."""
    current_platform = sys.platform if platform is None else str(platform)
    mode = str(value or "").strip().lower()
    if mode not in WINDOW_MODES:
        mode = (
            WINDOW_MODE_HIDDEN
            if current_platform == "win32"
            else WINDOW_MODE_VISIBLE
        )
    if mode == WINDOW_MODE_HIDDEN and current_platform != "win32":
        return WINDOW_MODE_VISIBLE
    return mode


def build_hidden_startupinfo(*, platform: str | None = None):
    """Build a best-effort Windows hint that keeps the first GUI window hidden."""
    current_platform = sys.platform if platform is None else str(platform)
    if current_platform != "win32":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return startupinfo


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


@dataclass(frozen=True)
class HiddenLaunchResult:
    process: object
    launcher_pid: int
    target_id: str
    hwnd: int


class HiddenLaunchError(RuntimeError):
    """Raised when a headed Chromium cannot be bootstrapped invisibly."""


def format_browser_window_marker(
    ref: BrowserWindowRef, *, state: str, fallback: bool = False
) -> str:
    normalized_state = str(state or "").strip().lower()
    normalized_mode = str(ref.mode or "").strip().lower()
    if normalized_state not in BROWSER_WINDOW_STATES:
        raise ValueError(f"unsupported browser window state: {normalized_state}")
    if normalized_mode not in WINDOW_MODES:
        raise ValueError(f"unsupported browser window mode: {normalized_mode}")
    return (
        "@@GROK_BROWSER_WINDOW "
        f"worker={max(1, int(ref.worker_id))} "
        f"generation={max(1, int(ref.generation))} "
        f"pid={max(0, int(ref.pid))} "
        f"hwnd={max(0, int(ref.hwnd))} "
        f"state={normalized_state} mode={normalized_mode} "
        f"fallback={1 if fallback else 0}"
    )


def parse_browser_window_marker(line: str):
    match = _BROWSER_WINDOW_MARKER_RE.search(str(line or ""))
    if not match:
        return None
    worker_id, generation, pid, hwnd = (
        int(value) for value in match.groups()[:4]
    )
    state, mode, fallback = match.groups()[4:]
    if state not in BROWSER_WINDOW_STATES or mode not in WINDOW_MODES:
        return None
    if worker_id < 1 or generation < 1 or pid < 1 or hwnd < 0:
        return None
    return {
        "worker_id": worker_id,
        "generation": generation,
        "pid": pid,
        "hwnd": hwnd,
        "state": state,
        "mode": mode,
        "fallback": fallback == "1",
    }


def terminate_process_tree(pid: int) -> None:
    """Terminate one exact captured process tree, never a process-name match."""
    process_id = int(pid or 0)
    if not process_id:
        return
    try:
        import psutil

        root = psutil.Process(process_id)
        processes = root.children(recursive=True)
        processes.append(root)
        for process in reversed(processes):
            try:
                process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, alive = psutil.wait_procs(processes, timeout=2)
        for process in alive:
            try:
                process.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=2)
    except Exception:
        pass


def _read_cdp_version(port: int) -> dict:
    import requests

    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            f"http://127.0.0.1:{int(port)}/json/version",
            headers={"Connection": "close"},
            timeout=0.5,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}
    finally:
        session.close()


def _open_cdp_websocket(url: str):
    from websocket import create_connection

    return create_connection(str(url), timeout=5, suppress_origin=True)


def resolve_chromium_executable(browser_path: str) -> str:
    """Resolve DrissionPage aliases such as ``chrome`` to an executable."""
    raw = os.path.expandvars(os.path.expanduser(str(browser_path or "").strip()))
    raw = raw.strip('"')
    if not raw:
        raise FileNotFoundError("Chromium executable was not provided")

    supplied = Path(raw)
    if supplied.is_file():
        return str(supplied)
    if supplied.is_dir():
        for name in ("chrome.exe", "msedge.exe", "chromium.exe"):
            candidate = supplied / name
            if candidate.is_file():
                return str(candidate)

    discovered = shutil.which(raw)
    if discovered:
        return str(Path(discovered))

    if sys.platform == "win32":
        program_files = os.environ.get("ProgramFiles", "")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        chrome_candidates = [
            Path(root) / suffix
            for root, suffix in (
                (program_files, "Google/Chrome/Application/chrome.exe"),
                (program_files_x86, "Google/Chrome/Application/chrome.exe"),
                (local_app_data, "Google/Chrome/Application/chrome.exe"),
                (program_files, "Chromium/Application/chrome.exe"),
                (local_app_data, "Chromium/Application/chrome.exe"),
            )
            if root
        ]
        edge_candidates = [
            Path(root) / "Microsoft/Edge/Application/msedge.exe"
            for root in (program_files, program_files_x86, local_app_data)
            if root
        ]
        alias = supplied.name.lower()
        candidates = (
            edge_candidates + chrome_candidates
            if "edge" in alias
            else chrome_candidates + edge_candidates
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)

    raise FileNotFoundError("Chromium executable could not be resolved")


def bootstrap_hidden_chromium(
    *,
    port: int,
    browser_path: str,
    arguments,
    controller=None,
    popen=None,
    version_reader=None,
    websocket_factory=None,
    process_tree_terminator=None,
    executable_resolver=None,
    startupinfo_builder=None,
    timeout: float = 10.0,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> HiddenLaunchResult:
    """Launch headed Chromium silently, create one background native window, hide it."""
    controller = controller or WindowsBrowserWindowController()
    popen = popen or subprocess.Popen
    version_reader = version_reader or _read_cdp_version
    websocket_factory = websocket_factory or _open_cdp_websocket
    process_tree_terminator = process_tree_terminator or terminate_process_tree
    executable_resolver = executable_resolver or resolve_chromium_executable
    startupinfo_builder = startupinfo_builder or build_hidden_startupinfo
    process = None
    websocket = None
    try:
        launch_arguments = [str(item) for item in list(arguments or [])]
        if any(item.startswith("--headless") for item in launch_arguments):
            raise ValueError("headless arguments are forbidden in hidden headed mode")
        if "--silent-launch" not in launch_arguments:
            launch_arguments.append("--silent-launch")
        launch_arguments = [
            item
            for item in launch_arguments
            if not item.startswith("--window-position=")
        ]
        launch_arguments.append(HIDDEN_WINDOW_POSITION_ARGUMENT)

        executable = executable_resolver(str(browser_path))
        command = [
            str(executable),
            f"--remote-debugging-port={int(port)}",
            *launch_arguments,
        ]
        popen_kwargs = {
            "shell": False,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        startupinfo = startupinfo_builder()
        if startupinfo is not None:
            popen_kwargs["startupinfo"] = startupinfo
        process = popen(command, **popen_kwargs)
        launcher_pid = int(getattr(process, "pid", 0) or 0)
        if not launcher_pid:
            raise RuntimeError("spawned Chromium did not expose a PID")

        deadline = monotonic() + max(0.1, float(timeout))
        version = {}
        while monotonic() < deadline:
            version = version_reader(int(port)) or {}
            if version.get("webSocketDebuggerUrl"):
                break
            sleep(0.02)
        websocket_url = str(version.get("webSocketDebuggerUrl") or "")
        if not websocket_url:
            raise RuntimeError("browser CDP endpoint did not become ready")

        websocket = websocket_factory(websocket_url)
        command_payload = {
            "id": 1,
            "method": "Target.createTarget",
            "params": {
                "url": "about:blank",
                "newWindow": True,
                "background": True,
                "focus": False,
                "windowState": "minimized",
                "left": HIDDEN_WINDOW_X,
                "top": HIDDEN_WINDOW_Y,
            },
        }
        websocket.send(json.dumps(command_payload, separators=(",", ":")))
        response = {}
        while monotonic() < deadline:
            response = json.loads(websocket.recv())
            if response.get("id") == 1:
                break
        if response.get("error"):
            raise RuntimeError("Target.createTarget was rejected")
        target_id = str((response.get("result") or {}).get("targetId") or "")
        if not target_id:
            raise RuntimeError("Target.createTarget returned no target id")

        hwnd = 0
        while monotonic() < deadline:
            hwnd = int(controller.find_window_for_pid(launcher_pid) or 0)
            if hwnd:
                break
            sleep(0.01)
        if not hwnd:
            raise RuntimeError("Chromium native window was not found")
        hidden = controller.hide(
            BrowserWindowRef(pid=launcher_pid, hwnd=hwnd, mode=WINDOW_MODE_HIDDEN)
        )
        if not hidden.ok:
            raise RuntimeError("Chromium native window could not be hidden")
        return HiddenLaunchResult(
            process=process,
            launcher_pid=launcher_pid,
            target_id=target_id,
            hwnd=hwnd,
        )
    except HiddenLaunchError:
        raise
    except Exception as exc:
        if process is not None:
            process_tree_terminator(int(getattr(process, "pid", 0) or 0))
        raise HiddenLaunchError(
            f"hidden Chromium launch failed ({type(exc).__name__})"
        ) from exc
    finally:
        if websocket is not None:
            try:
                websocket.close()
            except Exception:
                pass


@contextmanager
def scoped_hidden_chromium_launcher(
    *, controller=None, bootstrap=bootstrap_hidden_chromium
):
    """Temporarily replace DrissionPage's process spawn without editing the package."""
    from DrissionPage._functions import browser as drission_browser

    controller = controller or WindowsBrowserWindowController()
    original_runner = drission_browser._run_browser
    state: dict[str, HiddenLaunchResult | None] = {"result": None}

    def hidden_runner(port, path, arguments):
        result = bootstrap(
            port=int(port),
            browser_path=str(path),
            arguments=arguments,
            controller=controller,
        )
        state["result"] = result
        return result.process

    drission_browser._run_browser = hidden_runner
    try:
        yield state
    except Exception:
        result = state.get("result")
        if result is not None:
            terminate_process_tree(result.launcher_pid)
        raise
    finally:
        drission_browser._run_browser = original_runner


class CtypesWindowsApi:
    """Small typed Win32 surface used by the ownership controller."""

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("browser native-window control requires Windows")

        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._enum_callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetClassNameW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self._user32.GetClassNameW.restype = ctypes.c_int
        self._user32.EnumWindows.argtypes = [
            self._enum_callback_type,
            wintypes.LPARAM,
        ]
        self._user32.EnumWindows.restype = wintypes.BOOL
        self._user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.ShowWindowAsync.restype = wintypes.BOOL
        self._user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self._user32.SetForegroundWindow.restype = wintypes.BOOL
        self._user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self._user32.SetWindowPos.restype = wintypes.BOOL

        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self._get_window_long = self._user32.GetWindowLongPtrW
            self._set_window_long = self._user32.SetWindowLongPtrW
        else:  # pragma: no cover - 32-bit Windows compatibility
            self._get_window_long = self._user32.GetWindowLongW
            self._set_window_long = self._user32.SetWindowLongW
        self._get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
        self._get_window_long.restype = ctypes.c_ssize_t
        self._set_window_long.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_ssize_t,
        ]
        self._set_window_long.restype = ctypes.c_ssize_t

    def is_window(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindow(int(hwnd)))

    def is_window_visible(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindowVisible(int(hwnd)))

    def window_pid(self, hwnd: int) -> int:
        process_id = self._wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(
            int(hwnd), self._ctypes.byref(process_id)
        )
        return int(process_id.value)

    def enum_windows(self) -> list[int]:
        windows: list[int] = []

        @self._enum_callback_type
        def callback(hwnd, _lparam):
            windows.append(int(hwnd))
            return True

        self._user32.EnumWindows(callback, 0)
        return windows

    def class_name(self, hwnd: int) -> str:
        buffer = self._ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(int(hwnd), buffer, len(buffer))
        return str(buffer.value or "")

    def get_ex_style(self, hwnd: int) -> int:
        return int(self._get_window_long(int(hwnd), GWL_EXSTYLE))

    def set_ex_style(self, hwnd: int, style: int) -> None:
        self._ctypes.set_last_error(0)
        previous = self._set_window_long(int(hwnd), GWL_EXSTYLE, int(style))
        if previous == 0 and self._ctypes.get_last_error():
            raise self._ctypes.WinError(self._ctypes.get_last_error())

    def refresh_frame(self, hwnd: int) -> None:
        flags = (
            SWP_NOMOVE
            | SWP_NOSIZE
            | SWP_NOZORDER
            | SWP_NOACTIVATE
            | SWP_FRAMECHANGED
        )
        if not self._user32.SetWindowPos(int(hwnd), 0, 0, 0, 0, 0, flags):
            raise self._ctypes.WinError(self._ctypes.get_last_error())

    def show_window(self, hwnd: int, command: int) -> bool:
        return bool(self._user32.ShowWindowAsync(int(hwnd), int(command)))

    def set_foreground_window(self, hwnd: int) -> bool:
        return bool(self._user32.SetForegroundWindow(int(hwnd)))


class WindowsBrowserWindowController:
    """Show or hide only a window whose current PID matches its captured owner."""

    def __init__(self, *, api=None):
        self.api = api if api is not None else CtypesWindowsApi()

    def _validate(self, ref: BrowserWindowRef) -> WindowControlResult | None:
        if not ref.hwnd or not self.api.is_window(ref.hwnd):
            return WindowControlResult(
                False, "error", code="window_missing", error="browser window is gone"
            )
        if not ref.pid or self.api.window_pid(ref.hwnd) != int(ref.pid):
            return WindowControlResult(
                False,
                "error",
                code="ownership_changed",
                error="browser window ownership changed",
            )
        return None

    def find_window_for_pid(self, pid: int) -> int:
        owner_pid = int(pid or 0)
        if not owner_pid:
            return 0
        for hwnd in self.api.enum_windows():
            try:
                if (
                    self.api.is_window(hwnd)
                    and self.api.window_pid(hwnd) == owner_pid
                    and self.api.class_name(hwnd).startswith("Chrome_WidgetWin_")
                ):
                    return int(hwnd)
            except Exception:
                continue
        return 0

    def hide(self, ref: BrowserWindowRef) -> WindowControlResult:
        invalid = self._validate(ref)
        if invalid:
            return invalid
        try:
            if not self.api.show_window(ref.hwnd, SW_HIDE):
                raise RuntimeError("ShowWindowAsync could not queue hide")
            style = self.api.get_ex_style(ref.hwnd)
            style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            self.api.set_ex_style(ref.hwnd, style)
            self.api.refresh_frame(ref.hwnd)
            return WindowControlResult(True, "hidden")
        except Exception as exc:
            return WindowControlResult(
                False, "error", code="hide_failed", error=str(exc)
            )

    def show(
        self, ref: BrowserWindowRef, *, activate: bool = True
    ) -> WindowControlResult:
        invalid = self._validate(ref)
        if invalid:
            return invalid
        original_style = None
        try:
            original_style = self.api.get_ex_style(ref.hwnd)
            visible_style = (original_style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
            self.api.set_ex_style(ref.hwnd, visible_style)
            self.api.refresh_frame(ref.hwnd)
            if not self.api.show_window(ref.hwnd, SW_RESTORE):
                raise RuntimeError("ShowWindowAsync could not queue restore")
            if activate:
                self.api.set_foreground_window(ref.hwnd)
            return WindowControlResult(True, "visible")
        except Exception as exc:
            if original_style is not None:
                try:
                    self.api.set_ex_style(ref.hwnd, original_style)
                    self.api.refresh_frame(ref.hwnd)
                except Exception:
                    pass
            return WindowControlResult(
                False, "error", code="show_failed", error=str(exc)
            )
