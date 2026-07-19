from __future__ import annotations

import pytest

import grok_register_ttk as main


class ProfilePage:
    def __init__(self):
        self.calls = 0

    def run_js(self, script, *args):
        self.calls += 1
        if self.calls == 1:
            return "ready-to-submit"
        raise AssertionError("卡住的 Turnstile 不应继续点击提交按钮")


def test_turnstile_recovery_waits_resets_once_then_restarts():
    stalled = {
        "present": True,
        "token_len": 0,
        "initialized": False,
        "placeholder": True,
    }

    assert main.turnstile_recovery_action(stalled, waited=5, reset_waited=None) == "wait"
    assert (
        main.turnstile_recovery_action(
            stalled,
            waited=main.TURNSTILE_RESET_AFTER_SEC,
            reset_waited=None,
        )
        == "reset"
    )
    assert (
        main.turnstile_recovery_action(
            stalled,
            waited=main.TURNSTILE_RESET_AFTER_SEC + 2,
            reset_waited=2,
        )
        == "wait"
    )
    assert (
        main.turnstile_recovery_action(
            stalled,
            waited=main.TURNSTILE_RESET_AFTER_SEC
            + main.TURNSTILE_RESET_GRACE_SEC,
            reset_waited=main.TURNSTILE_RESET_GRACE_SEC,
        )
        == "restart"
    )


@pytest.mark.parametrize(
    "state",
    [
        {"present": False, "token_len": 0},
        {"present": True, "token_len": 80},
        {"present": True, "token_len": 900},
    ],
)
def test_turnstile_recovery_is_ready_without_a_pending_challenge(state):
    assert main.turnstile_recovery_action(state, waited=999, reset_waited=999) == "ready"


def test_profile_submit_turnstile_stall_resets_once_then_requests_fresh_browser(
    monkeypatch,
):
    fake_page = ProfilePage()
    monkeypatch.setattr(main, "page", fake_page)
    monkeypatch.setattr(main, "build_profile", lambda: ("Adam", "Xie", "secret"))
    monkeypatch.setattr(
        main,
        "inspect_turnstile_state",
        lambda: {
            "present": True,
            "token_len": 0,
            "initialized": False,
            "placeholder": True,
        },
    )
    actions = iter(("reset", "restart"))
    monkeypatch.setattr(
        main,
        "turnstile_recovery_action",
        lambda *args, **kwargs: next(actions),
    )
    resets = []
    monkeypatch.setattr(
        main,
        "reset_turnstile_widget",
        lambda: resets.append(True) or {"ok": True, "widget_id_found": True},
    )
    monkeypatch.setattr(main, "sleep_with_cancel", lambda *args, **kwargs: None)

    with pytest.raises(main.TurnstileRetryNeeded, match="未初始化"):
        main.fill_profile_and_submit(timeout=60)

    assert resets == [True]
    assert fake_page.calls == 1


def test_profile_submit_waits_for_grace_when_turnstile_api_is_not_ready(monkeypatch):
    fake_page = ProfilePage()
    monkeypatch.setattr(main, "page", fake_page)
    monkeypatch.setattr(main, "build_profile", lambda: ("Adam", "Xie", "secret"))
    monkeypatch.setattr(
        main,
        "inspect_turnstile_state",
        lambda: {
            "present": True,
            "token_len": 0,
            "initialized": False,
            "placeholder": True,
        },
    )
    actions = iter(("reset", "restart"))
    action_calls = []

    def next_action(*args, **kwargs):
        action_calls.append(kwargs)
        return next(actions)

    monkeypatch.setattr(main, "turnstile_recovery_action", next_action)
    monkeypatch.setattr(
        main,
        "reset_turnstile_widget",
        lambda: {
            "ok": False,
            "widget_id_found": True,
            "error": "reset-unavailable",
        },
    )
    monkeypatch.setattr(main, "sleep_with_cancel", lambda *args, **kwargs: None)

    with pytest.raises(main.TurnstileRetryNeeded, match="内未产生 token"):
        main.fill_profile_and_submit(timeout=60)

    assert len(action_calls) == 2
    assert fake_page.calls == 1
