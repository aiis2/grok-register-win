from __future__ import annotations

import pytest

import grok_register_ttk as main


class FakePage:
    def __init__(self, url="https://accounts.x.ai/sign-up"):
        self.url = url
        self.closed = False
        self.get_calls = []
        self.js_calls = []

    def close(self):
        self.closed = True

    def get(self, url):
        self.get_calls.append(url)
        self.url = url

    def run_js(self, script):
        self.js_calls.append(script)
        return True


class FakeBrowser:
    def __init__(self, tabs=None, *, disconnected=False, pid=4321):
        self.tabs = list(tabs or [])
        self.disconnected = disconnected
        self.process_id = pid
        self.address = "127.0.0.1:9222"
        self.user_data_path = r"C:\Temp\owned-profile"
        self.clear_cache_calls = []
        self.quit_calls = []

    def get_tabs(self):
        if self.disconnected:
            raise RuntimeError("browser disconnected")
        return [tab for tab in self.tabs if not tab.closed]

    def clear_cache(self, **kwargs):
        self.clear_cache_calls.append(kwargs)

    def quit(self, **kwargs):
        self.quit_calls.append(kwargs)


@pytest.fixture(autouse=True)
def restore_browser_globals(monkeypatch):
    monkeypatch.setattr(main, "browser", None)
    monkeypatch.setattr(main, "page", None)
    monkeypatch.setattr(main, "browser_proxy_bridge", None)
    monkeypatch.setattr(main, "browser_started_with_proxy", False)
    monkeypatch.setattr(
        main,
        "_owned_browser",
        {"pid": None, "address": "", "user_data_path": "", "engine": ""},
        raising=False,
    )


def test_prepare_next_account_reuses_healthy_browser(monkeypatch):
    first = FakePage()
    survivor = FakePage()
    fake = FakeBrowser([first, survivor])
    monkeypatch.setattr(main, "browser", fake)
    monkeypatch.setattr(main, "page", survivor)

    assert main.prepare_browser_for_next_account() is True
    assert fake.quit_calls == []
    assert fake.clear_cache_calls == [{"cache": True, "cookies": True}]
    assert first.closed is True
    assert survivor.closed is False
    assert survivor.url == "about:blank"
    assert main.page is survivor
    assert any("localStorage" in script for script in survivor.js_calls)


def test_prepare_next_account_returns_false_when_browser_disconnected(monkeypatch):
    fake = FakeBrowser([FakePage()], disconnected=True)
    monkeypatch.setattr(main, "browser", fake)
    restart_calls = []
    monkeypatch.setattr(main, "restart_browser", lambda **kwargs: restart_calls.append(kwargs))

    assert main.prepare_browser_for_next_account() is False
    assert restart_calls == []
    assert fake.quit_calls == []


def test_capture_browser_ownership_records_only_chromium():
    fake = FakeBrowser([FakePage()], pid=8877)

    main._capture_browser_ownership(fake, "chromium")
    assert main._owned_browser == {
        "pid": 8877,
        "address": "127.0.0.1:9222",
        "user_data_path": r"C:\Temp\owned-profile",
        "engine": "chromium",
    }

    main._capture_browser_ownership(fake, "camoufox")
    assert main._owned_browser["pid"] is None
    assert main._owned_browser["engine"] == "camoufox"


def test_stop_browser_forces_chromium_quit_and_waits_for_owned_pid(monkeypatch):
    fake = FakeBrowser([FakePage()], pid=4321)
    monkeypatch.setattr(main, "browser", fake)
    monkeypatch.setattr(
        main,
        "_owned_browser",
        {
            "pid": 4321,
            "address": fake.address,
            "user_data_path": fake.user_data_path,
            "engine": "chromium",
        },
    )
    waits = []
    terminated = []
    monkeypatch.setattr(
        main,
        "_wait_for_owned_pid_exit",
        lambda pid, timeout=0: waits.append((pid, timeout)) or True,
    )
    monkeypatch.setattr(
        main, "_terminate_owned_process_tree", lambda pid: terminated.append(pid)
    )

    main.stop_browser()

    assert fake.quit_calls == [{"force": True, "del_data": True}]
    assert waits and waits[0][0] == 4321
    assert terminated == []
    assert main.browser is None
    assert main._owned_browser["pid"] is None


def test_stop_browser_terminates_only_recorded_pid_if_quit_does_not_finish(monkeypatch):
    fake = FakeBrowser([FakePage()], pid=9999)
    monkeypatch.setattr(main, "browser", fake)
    monkeypatch.setattr(
        main,
        "_owned_browser",
        {
            "pid": 2468,
            "address": fake.address,
            "user_data_path": fake.user_data_path,
            "engine": "chromium",
        },
    )
    waits = []
    terminated = []
    wait_results = iter([False, True])
    monkeypatch.setattr(
        main,
        "_wait_for_owned_pid_exit",
        lambda pid, timeout=0: waits.append(pid) or next(wait_results),
    )
    monkeypatch.setattr(
        main, "_terminate_owned_process_tree", lambda pid: terminated.append(pid)
    )

    main.stop_browser()

    assert waits == [2468, 2468]
    assert terminated == [2468]
    assert 9999 not in terminated


def test_next_account_transition_reuses_healthy_browser(monkeypatch):
    monkeypatch.setattr(main, "prepare_browser_for_next_account", lambda **kwargs: True)
    restarts = []
    monkeypatch.setattr(main, "restart_browser", lambda **kwargs: restarts.append(kwargs))

    assert main.transition_browser_for_next_attempt(True) == "reused"
    assert restarts == []


def test_next_account_transition_restarts_once_when_reset_fails(monkeypatch):
    monkeypatch.setattr(main, "prepare_browser_for_next_account", lambda **kwargs: False)
    restarts = []
    monkeypatch.setattr(main, "restart_browser", lambda **kwargs: restarts.append(kwargs))

    assert main.transition_browser_for_next_attempt(True) == "restarted"
    assert len(restarts) == 1


def test_final_round_transition_does_not_touch_browser(monkeypatch):
    resets = []
    restarts = []
    monkeypatch.setattr(
        main,
        "prepare_browser_for_next_account",
        lambda **kwargs: resets.append(kwargs) or True,
    )
    monkeypatch.setattr(main, "restart_browser", lambda **kwargs: restarts.append(kwargs))

    assert main.transition_browser_for_next_attempt(False) == "final"
    assert resets == []
    assert restarts == []


def test_worker_browser_port_scopes_are_disjoint():
    scopes = [main.browser_auto_port_scope(worker_id) for worker_id in range(1, 11)]

    assert scopes[0] == (9600, 10599)
    assert scopes[-1] == (18600, 19599)
    for previous, current in zip(scopes, scopes[1:]):
        assert previous[1] < current[0]


def test_browser_options_isolate_profile_root_by_worker_and_process(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GROK_WORKER_ID", "3")
    monkeypatch.setattr(main.os, "getpid", lambda: 2468)
    monkeypatch.setattr(main.tempfile, "gettempdir", lambda: str(tmp_path))

    options = main.create_browser_options()

    assert options.is_auto_port == (11600, 12599)
    assert options.tmp_path == str(
        tmp_path / "grok-register-win" / "browser" / "w3-p2468"
    )


def test_repeated_account_transitions_never_restart_a_healthy_browser(monkeypatch):
    prepare_calls = []
    restart_calls = []
    monkeypatch.setattr(
        main,
        "prepare_browser_for_next_account",
        lambda **kwargs: prepare_calls.append(kwargs) or True,
    )
    monkeypatch.setattr(
        main, "restart_browser", lambda **kwargs: restart_calls.append(kwargs)
    )

    results = [main.transition_browser_for_next_attempt(True) for _ in range(20)]

    assert results == ["reused"] * 20
    assert len(prepare_calls) == 20
    assert restart_calls == []
