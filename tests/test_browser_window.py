from __future__ import annotations

from lib.browser_window import (
    SW_HIDE,
    SW_RESTORE,
    WS_EX_APPWINDOW,
    WS_EX_TOOLWINDOW,
    BrowserWindowRef,
    WindowsBrowserWindowController,
)


class FakeWindowApi:
    def __init__(
        self,
        *,
        hwnd_pid=None,
        ex_styles=None,
        visible=None,
        class_names=None,
        windows=None,
    ):
        self.hwnd_pid = dict(hwnd_pid or {})
        self.ex_styles = dict(ex_styles or {})
        self.visible = dict(visible or {})
        self.class_names = dict(class_names or {})
        self.windows = list(windows or self.hwnd_pid)
        self.show_calls = []
        self.foreground_calls = []
        self.style_calls = []
        self.frame_calls = []

    def is_window(self, hwnd):
        return int(hwnd) in self.hwnd_pid

    def window_pid(self, hwnd):
        return int(self.hwnd_pid.get(int(hwnd), 0))

    def enum_windows(self):
        return list(self.windows)

    def class_name(self, hwnd):
        return self.class_names.get(int(hwnd), "Chrome_WidgetWin_1")

    def get_ex_style(self, hwnd):
        return int(self.ex_styles.get(int(hwnd), 0))

    def set_ex_style(self, hwnd, style):
        self.ex_styles[int(hwnd)] = int(style)
        self.style_calls.append((int(hwnd), int(style)))

    def refresh_frame(self, hwnd):
        self.frame_calls.append(int(hwnd))

    def show_window(self, hwnd, command):
        hwnd = int(hwnd)
        command = int(command)
        self.show_calls.append((hwnd, command))
        self.visible[hwnd] = command != SW_HIDE
        return True

    def is_window_visible(self, hwnd):
        return bool(self.visible.get(int(hwnd), False))

    def set_foreground_window(self, hwnd):
        self.foreground_calls.append(int(hwnd))
        return True


def test_hide_rejects_hwnd_owned_by_another_pid():
    api = FakeWindowApi(hwnd_pid={701: 9002}, visible={701: True})
    controller = WindowsBrowserWindowController(api=api)

    result = controller.hide(BrowserWindowRef(pid=9001, hwnd=701, generation=2))

    assert result.ok is False
    assert result.code == "ownership_changed"
    assert api.show_calls == []


def test_hide_removes_taskbar_style_without_activating():
    api = FakeWindowApi(
        hwnd_pid={701: 9001},
        ex_styles={701: WS_EX_APPWINDOW},
        visible={701: True},
    )
    controller = WindowsBrowserWindowController(api=api)

    result = controller.hide(BrowserWindowRef(pid=9001, hwnd=701, generation=2))

    assert result.ok is True
    assert result.state == "hidden"
    assert api.show_calls[-1] == (701, SW_HIDE)
    assert api.foreground_calls == []
    assert api.ex_styles[701] & WS_EX_TOOLWINDOW
    assert not api.ex_styles[701] & WS_EX_APPWINDOW
    assert api.frame_calls == [701]


def test_show_restores_same_owned_window_only_on_explicit_request():
    api = FakeWindowApi(
        hwnd_pid={701: 9001},
        ex_styles={701: WS_EX_TOOLWINDOW},
        visible={701: False},
    )
    controller = WindowsBrowserWindowController(api=api)

    result = controller.show(
        BrowserWindowRef(pid=9001, hwnd=701, generation=2), activate=True
    )

    assert result.ok is True
    assert result.state == "visible"
    assert api.show_calls[-1] == (701, SW_RESTORE)
    assert api.foreground_calls == [701]
    assert api.ex_styles[701] & WS_EX_APPWINDOW
    assert not api.ex_styles[701] & WS_EX_TOOLWINDOW


def test_show_can_restore_without_activation():
    api = FakeWindowApi(hwnd_pid={701: 9001}, visible={701: False})
    controller = WindowsBrowserWindowController(api=api)

    result = controller.show(
        BrowserWindowRef(pid=9001, hwnd=701, generation=2), activate=False
    )

    assert result.ok is True
    assert api.foreground_calls == []


def test_invalid_hwnd_is_rejected_without_win32_mutation():
    api = FakeWindowApi()
    controller = WindowsBrowserWindowController(api=api)

    result = controller.hide(BrowserWindowRef(pid=9001, hwnd=701, generation=2))

    assert result.ok is False
    assert result.code == "window_missing"
    assert api.show_calls == []
    assert api.style_calls == []


def test_find_window_selects_only_chrome_top_level_window_for_exact_pid():
    api = FakeWindowApi(
        hwnd_pid={701: 9001, 702: 9002, 703: 9001},
        class_names={
            701: "Chrome_RenderWidgetHostHWND",
            702: "Chrome_WidgetWin_1",
            703: "Chrome_WidgetWin_1",
        },
        windows=[701, 702, 703],
    )
    controller = WindowsBrowserWindowController(api=api)

    assert controller.find_window_for_pid(9001) == 703
    assert controller.find_window_for_pid(7777) == 0
