"""Browser window mode and Windows native-window helpers."""

from __future__ import annotations

import sys
from dataclasses import dataclass


WINDOW_MODE_HIDDEN = "hidden"
WINDOW_MODE_MINIMIZED = "minimized"
WINDOW_MODE_VISIBLE = "visible"
WINDOW_MODES = {
    WINDOW_MODE_HIDDEN,
    WINDOW_MODE_MINIMIZED,
    WINDOW_MODE_VISIBLE,
}

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
        # The return value describes the previous visibility state, not success.
        self._user32.ShowWindowAsync(int(hwnd), int(command))
        return True

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
            self.api.show_window(ref.hwnd, SW_HIDE)
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
        try:
            style = self.api.get_ex_style(ref.hwnd)
            style = (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
            self.api.set_ex_style(ref.hwnd, style)
            self.api.refresh_frame(ref.hwnd)
            self.api.show_window(ref.hwnd, SW_RESTORE)
            if activate:
                self.api.set_foreground_window(ref.hwnd)
            return WindowControlResult(True, "visible")
        except Exception as exc:
            return WindowControlResult(
                False, "error", code="show_failed", error=str(exc)
            )
