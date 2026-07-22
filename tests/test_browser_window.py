from __future__ import annotations

import lib.browser_window as browser_window
from lib.browser_window import (
    SW_HIDE,
    SW_RESTORE,
    WS_EX_APPWINDOW,
    WS_EX_TOOLWINDOW,
    BrowserWindowRef,
    HiddenLaunchError,
    HiddenLaunchResult,
    WindowControlResult,
    WindowsBrowserWindowController,
    bootstrap_hidden_chromium,
    format_browser_window_marker,
    parse_browser_window_marker,
    resolve_chromium_executable,
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
        window_rects=None,
        virtual_rect=(0, 0, 1920, 1080),
        work_area=(0, 0, 1920, 1040),
        show_return=True,
        show_changes_visibility=True,
        hide_return=True,
        keep_visible_after_hide=False,
        move_return=True,
        place_return=True,
    ):
        self.hwnd_pid = dict(hwnd_pid or {})
        self.ex_styles = dict(ex_styles or {})
        self.visible = dict(visible or {})
        self.class_names = dict(class_names or {})
        self.windows = list(windows or self.hwnd_pid)
        self.window_rects = dict(window_rects or {})
        for hwnd in self.hwnd_pid:
            self.window_rects.setdefault(int(hwnd), (100, 100, 900, 700))
        self.virtual_rect = tuple(virtual_rect)
        self.work_area = tuple(work_area)
        self.show_return = bool(show_return)
        self.show_changes_visibility = bool(show_changes_visibility)
        self.hide_return = bool(hide_return)
        self.keep_visible_after_hide = bool(keep_visible_after_hide)
        self.move_return = bool(move_return)
        self.place_return = bool(place_return)
        self.show_calls = []
        self.foreground_calls = []
        self.style_calls = []
        self.frame_calls = []
        self.hide_no_activate_calls = []
        self.move_calls = []
        self.place_calls = []

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
        if self.show_changes_visibility:
            self.visible[hwnd] = command != SW_HIDE
        return self.show_return

    def is_window_visible(self, hwnd):
        return bool(self.visible.get(int(hwnd), False))

    def set_foreground_window(self, hwnd):
        self.foreground_calls.append(int(hwnd))
        return True

    def hide_window_no_activate(self, hwnd):
        hwnd = int(hwnd)
        self.hide_no_activate_calls.append(hwnd)
        if self.hide_return and not self.keep_visible_after_hide:
            self.visible[hwnd] = False
        return self.hide_return

    def window_rect(self, hwnd):
        return tuple(self.window_rects[int(hwnd)])

    def virtual_screen_rect(self):
        return self.virtual_rect

    def primary_work_area(self):
        return self.work_area

    def move_window_no_activate(self, hwnd, x, y):
        hwnd = int(hwnd)
        x = int(x)
        y = int(y)
        self.move_calls.append((hwnd, x, y))
        if self.move_return:
            left, top, right, bottom = self.window_rects[hwnd]
            self.window_rects[hwnd] = (
                x,
                y,
                x + max(1, right - left),
                y + max(1, bottom - top),
            )
        return self.move_return

    def place_window_no_activate(self, hwnd, x, y, width, height):
        hwnd = int(hwnd)
        values = (hwnd, int(x), int(y), int(width), int(height))
        self.place_calls.append(values)
        if self.place_return:
            self.window_rects[hwnd] = (
                int(x),
                int(y),
                int(x) + int(width),
                int(y) + int(height),
            )
        return self.place_return


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
    assert api.show_calls == []
    assert api.hide_no_activate_calls == [701]
    assert api.foreground_calls == []
    assert api.ex_styles[701] & WS_EX_TOOLWINDOW
    assert not api.ex_styles[701] & WS_EX_APPWINDOW
    assert api.frame_calls == []


def test_show_restores_same_owned_window_only_on_explicit_request():
    api = FakeWindowApi(
        hwnd_pid={701: 9001},
        ex_styles={701: WS_EX_TOOLWINDOW},
        visible={701: False},
        show_return=False,
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
    assert api.move_calls == []


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


def test_hide_rolls_back_taskbar_style_when_no_activate_hide_fails():
    api = FakeWindowApi(
        hwnd_pid={701: 9001},
        ex_styles={701: WS_EX_APPWINDOW},
        visible={701: True},
        hide_return=False,
    )
    controller = WindowsBrowserWindowController(api=api)

    result = controller.hide(BrowserWindowRef(pid=9001, hwnd=701))

    assert result.ok is False
    assert result.code == "hide_failed"
    assert api.ex_styles[701] == WS_EX_APPWINDOW
    assert api.hide_no_activate_calls == [701]


def test_hide_fails_and_rolls_back_style_when_window_remains_visible():
    api = FakeWindowApi(
        hwnd_pid={701: 9001},
        ex_styles={701: WS_EX_APPWINDOW},
        visible={701: True},
        keep_visible_after_hide=True,
    )
    controller = WindowsBrowserWindowController(api=api, visibility_timeout=0)

    result = controller.hide(BrowserWindowRef(pid=9001, hwnd=701))

    assert result.ok is False
    assert result.code == "hide_failed"
    assert api.ex_styles[701] == WS_EX_APPWINDOW


def test_hide_seeds_restorable_bounds_for_startup_hidden_zero_size_window():
    api = FakeWindowApi(
        hwnd_pid={701: 9001},
        ex_styles={701: WS_EX_APPWINDOW},
        visible={701: False},
        window_rects={701: (0, 0, 0, 0)},
    )
    controller = WindowsBrowserWindowController(api=api)

    result = controller.hide(BrowserWindowRef(pid=9001, hwnd=701))

    assert result.ok is True
    assert api.place_calls == [(701, -32000, -32000, 1280, 800)]
    assert api.window_rects[701] == (-32000, -32000, -30720, -31200)


def test_show_rolls_back_taskbar_style_when_window_remains_hidden():
    api = FakeWindowApi(
        hwnd_pid={701: 9001},
        ex_styles={701: WS_EX_TOOLWINDOW},
        visible={701: False},
        show_changes_visibility=False,
    )
    controller = WindowsBrowserWindowController(api=api, visibility_timeout=0)

    result = controller.show(BrowserWindowRef(pid=9001, hwnd=701))

    assert result.ok is False
    assert result.code == "show_failed"
    assert api.ex_styles[701] == WS_EX_TOOLWINDOW


def test_show_moves_initial_offscreen_window_into_primary_work_area():
    api = FakeWindowApi(
        hwnd_pid={701: 9001},
        ex_styles={701: WS_EX_TOOLWINDOW},
        visible={701: False},
        window_rects={701: (-32000, -32000, -30720, -31200)},
        virtual_rect=(0, 0, 1920, 1080),
        work_area=(0, 0, 1920, 1040),
    )
    controller = WindowsBrowserWindowController(api=api)

    result = controller.show(
        BrowserWindowRef(pid=9001, hwnd=701, generation=2), activate=True
    )

    assert result.ok is True
    assert api.move_calls == [(701, 64, 64)]
    assert api.foreground_calls == [701]


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


def test_find_window_prefers_real_chromium_main_window_over_internal_window():
    api = FakeWindowApi(
        hwnd_pid={701: 9001, 702: 9001, 703: 7777},
        class_names={
            701: "Chrome_WidgetWin_0",
            702: "Chrome_WidgetWin_1",
            703: "Chrome_WidgetWin_1",
        },
        visible={701: False, 702: True, 703: True},
        ex_styles={701: WS_EX_TOOLWINDOW, 702: 0, 703: 0},
        windows=[701, 703, 702],
        window_rects={
            701: (0, 0, 0, 0),
            702: (-32000, -32000, -31801, -31966),
            703: (0, 0, 900, 700),
        },
    )
    controller = WindowsBrowserWindowController(api=api)

    assert controller.find_window_for_pid(9001) == 702


def test_find_window_falls_back_to_largest_visible_non_tool_chromium_window():
    api = FakeWindowApi(
        hwnd_pid={701: 9001, 702: 9001, 703: 9001, 704: 7777},
        class_names={
            701: "Chrome_WidgetWin_0",
            702: "Chrome_WidgetWin_2",
            703: "Chrome_WidgetWin_3",
            704: "Chrome_WidgetWin_1",
        },
        visible={701: False, 702: True, 703: True, 704: True},
        ex_styles={701: WS_EX_TOOLWINDOW, 702: 0, 703: 0, 704: 0},
        windows=[701, 702, 704, 703],
        window_rects={
            701: (0, 0, 0, 0),
            702: (0, 0, 400, 300),
            703: (0, 0, 900, 700),
            704: (0, 0, 1200, 900),
        },
    )
    controller = WindowsBrowserWindowController(api=api)

    assert controller.find_window_for_pid(9001) == 703


class FakeProcess:
    def __init__(self, pid=9300):
        self.pid = pid


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, payload):
        import json

        self.sent.append(json.loads(payload))

    def recv(self):
        return '{"id":1,"result":{"targetId":"target-1"}}'

    def close(self):
        self.closed = True


class FakeBootstrapController:
    def __init__(self, hwnd=701):
        self.hwnd = hwnd
        self.hidden_refs = []

    def find_window_for_pid(self, pid):
        return self.hwnd

    def hide(self, ref):
        self.hidden_refs.append(ref)
        return WindowControlResult(True, "hidden")


def test_silent_bootstrap_creates_headed_background_minimized_window():
    process = FakeProcess()
    websocket = FakeWebSocket()
    controller = FakeBootstrapController()
    popen_calls = []

    def popen(arguments, **kwargs):
        popen_calls.append((list(arguments), dict(kwargs)))
        return process

    result = bootstrap_hidden_chromium(
        port=19222,
        browser_path="chrome.exe",
        arguments=[
            "--user-data-dir=X",
            "--silent-launch",
            "--window-position=10,20",
        ],
        controller=controller,
        popen=popen,
        version_reader=lambda _port: {
            "webSocketDebuggerUrl": "ws://127.0.0.1:19222/devtools/browser/id"
        },
        websocket_factory=lambda _url: websocket,
        executable_resolver=lambda value: value,
    )

    launched_arguments = popen_calls[0][0]
    assert launched_arguments[0] == "chrome.exe"
    assert "--remote-debugging-port=19222" in launched_arguments
    assert "--silent-launch" in launched_arguments
    assert launched_arguments.count("--window-position=-32000,-32000") == 1
    assert "--window-position=10,20" not in launched_arguments
    assert not any(arg.startswith("--headless") for arg in launched_arguments)
    assert websocket.sent == [
        {
            "id": 1,
            "method": "Target.createTarget",
            "params": {
                "url": "about:blank",
                "newWindow": True,
                "background": True,
                "focus": False,
                "windowState": "minimized",
                "left": -32000,
                "top": -32000,
            },
        }
    ]
    assert websocket.closed is True
    assert result == HiddenLaunchResult(
        process=process,
        launcher_pid=9300,
        target_id="target-1",
        hwnd=701,
    )
    assert controller.hidden_refs[0].pid == 9300
    assert controller.hidden_refs[0].hwnd == 701


def test_silent_bootstrap_passes_hidden_startup_info_to_popen():
    process = FakeProcess()
    websocket = FakeWebSocket()
    startupinfo = object()
    popen_calls = []

    bootstrap_hidden_chromium(
        port=19225,
        browser_path="chrome.exe",
        arguments=["--user-data-dir=X"],
        controller=FakeBootstrapController(),
        popen=lambda arguments, **kwargs: popen_calls.append(
            (list(arguments), dict(kwargs))
        )
        or process,
        version_reader=lambda _port: {
            "webSocketDebuggerUrl": "ws://127.0.0.1:19225/devtools/browser/id"
        },
        websocket_factory=lambda _url: websocket,
        executable_resolver=lambda value: value,
        startupinfo_builder=lambda: startupinfo,
    )

    assert popen_calls[0][1]["startupinfo"] is startupinfo


def test_silent_bootstrap_resolves_drission_chrome_alias_before_spawn():
    process = FakeProcess()
    websocket = FakeWebSocket()
    popen_calls = []

    result = bootstrap_hidden_chromium(
        port=19224,
        browser_path="chrome",
        arguments=["--user-data-dir=X"],
        controller=FakeBootstrapController(),
        popen=lambda arguments, **kwargs: popen_calls.append(
            (list(arguments), dict(kwargs))
        )
        or process,
        version_reader=lambda _port: {
            "webSocketDebuggerUrl": "ws://127.0.0.1:19224/devtools/browser/id"
        },
        websocket_factory=lambda _url: websocket,
        executable_resolver=lambda value: (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe"
            if value == "chrome"
            else value
        ),
    )

    assert result.launcher_pid == process.pid
    assert popen_calls[0][0][0] == (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )


def test_resolve_chromium_executable_finds_standard_windows_install(
    tmp_path, monkeypatch
):
    executable = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(browser_window.sys, "platform", "win32")
    monkeypatch.setattr(browser_window.shutil, "which", lambda _value: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramFiles(x86)", "")
    monkeypatch.setenv("LOCALAPPDATA", "")

    assert resolve_chromium_executable("chrome") == str(executable)


def test_failed_bootstrap_terminates_only_spawned_process_and_redacts_arguments():
    process = FakeProcess(pid=9400)
    terminated = []

    try:
        bootstrap_hidden_chromium(
            port=19223,
            browser_path="chrome.exe",
            arguments=["--proxy-server=http://user:super-secret@example.test:8080"],
            controller=FakeBootstrapController(),
            popen=lambda *_args, **_kwargs: process,
            version_reader=lambda _port: (_ for _ in ()).throw(
                RuntimeError("connection failed super-secret")
            ),
            process_tree_terminator=lambda pid: terminated.append(pid),
        )
    except HiddenLaunchError as exc:
        assert "super-secret" not in str(exc)
    else:  # pragma: no cover - explicit assertion gives a clearer failure
        raise AssertionError("HiddenLaunchError was not raised")

    assert terminated == [9400]


def test_browser_window_marker_round_trip_is_stable_and_secret_free():
    marker = format_browser_window_marker(
        BrowserWindowRef(
            worker_id=3,
            generation=4,
            pid=9300,
            hwnd=701,
            mode="hidden",
        ),
        state="hidden",
        fallback=False,
    )

    event = parse_browser_window_marker(f"[12:00:00] {marker}")

    assert marker == (
        "@@GROK_BROWSER_WINDOW worker=3 generation=4 pid=9300 hwnd=701 "
        "state=hidden mode=hidden fallback=0"
    )
    assert event == {
        "worker_id": 3,
        "generation": 4,
        "pid": 9300,
        "hwnd": 701,
        "state": "hidden",
        "mode": "hidden",
        "fallback": False,
    }
    lowered = marker.lower()
    assert "profile" not in lowered
    assert "proxy" not in lowered
    assert "token" not in lowered
    assert "url" not in lowered


def test_browser_window_marker_rejects_malformed_or_unknown_state():
    assert parse_browser_window_marker("not a marker") is None
    assert (
        parse_browser_window_marker(
            "@@GROK_BROWSER_WINDOW worker=1 generation=1 pid=2 hwnd=3 "
            "state=surprise mode=hidden fallback=0"
        )
        is None
    )
