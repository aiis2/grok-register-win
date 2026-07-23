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


class RecoveringProfilePage:
    def __init__(self):
        self.calls = 0

    def run_js(self, script, *args):
        self.calls += 1
        if self.calls == 1:
            return "ready-to-submit"
        if "return 'submitted'" in script:
            return "submitted"
        raise AssertionError("资料页恢复后应直接提交")


class ChromiumShadowPage:
    def __init__(self):
        self.solved = False
        self.button = self.Button(self)
        self.body_shadow = self.ShadowRoot({"tag:input": self.button})
        self.body = self.Body(self.body_shadow)
        self.iframe = self.Iframe(self.body)
        self.wrapper_shadow = self.ShadowRoot({"tag:iframe": self.iframe})
        self.wrapper = self.Wrapper(self.wrapper_shadow)
        self.input = self.Input(self.wrapper)

    class Button:
        def __init__(self, owner):
            self.owner = owner

        def click(self):
            self.owner.solved = True

    class ShadowRoot:
        def __init__(self, elements):
            self.elements = elements

        def ele(self, locator):
            return self.elements.get(locator)

    class Body:
        def __init__(self, shadow_root):
            self.shadow_root = shadow_root

    class Iframe:
        def __init__(self, body):
            self.body = body

        def run_js(self, script):
            return None

        def ele(self, locator):
            return self.body if locator == "tag:body" else None

    class Wrapper:
        def __init__(self, shadow_root):
            self.shadow_root = shadow_root

    class Input:
        def __init__(self, wrapper):
            self.wrapper = wrapper

        def parent(self):
            return self.wrapper

    def ele(self, locator):
        return self.input if locator == "@name=cf-turnstile-response" else None

    def run_js(self, script, *args):
        return "t" * 120 if self.solved else ""


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


def test_profile_submit_invokes_chromium_turnstile_interaction(monkeypatch):
    fake_page = RecoveringProfilePage()
    solved = {"value": False}
    interactions = []

    monkeypatch.setattr(main, "page", fake_page)
    monkeypatch.setattr(main, "build_profile", lambda: ("Adam", "Xie", "secret"))
    monkeypatch.setattr(
        main,
        "inspect_turnstile_state",
        lambda: {
            "present": True,
            "token_len": 837 if solved["value"] else 0,
            "initialized": solved["value"],
            "placeholder": not solved["value"],
        },
    )
    actions = iter(("wait", "restart"))
    monkeypatch.setattr(
        main,
        "turnstile_recovery_action",
        lambda *args, **kwargs: next(actions),
    )

    def interact(*args, **kwargs):
        interactions.append(True)
        solved["value"] = True
        return {"clicked": True, "method": "chromium-shadow", "token_len": 837}

    monkeypatch.setattr(main, "interact_turnstile_widget", interact, raising=False)
    monkeypatch.setattr(main, "sleep_with_cancel", lambda *args, **kwargs: None)

    profile = main.fill_profile_and_submit(timeout=60)

    assert profile == {
        "given_name": "Adam",
        "family_name": "Xie",
        "password": "secret",
    }
    assert interactions == [True]
    assert fake_page.calls == 2


def test_chromium_turnstile_interaction_clicks_nested_shadow_control(monkeypatch):
    fake_page = ChromiumShadowPage()
    monkeypatch.setattr(main, "page", fake_page)

    detail = main.interact_turnstile_widget()

    assert detail == {
        "clicked": True,
        "method": "chromium-shadow",
        "token": "t" * 120,
        "token_len": 120,
    }


def test_account_landing_page_never_retries_the_registration_submit(monkeypatch):
    class AccountLandingPage:
        url = "https://accounts.x.ai/account"

        def __init__(self):
            self.submit_retries = 0

        def run_js(self, script, *args):
            if "final-page-no-submit" in script:
                self.submit_retries += 1
                return "final-page-no-submit:您正在登录 | 返回"
            return ""

    fake_page = AccountLandingPage()
    monkeypatch.setattr(main, "page", fake_page)
    monkeypatch.setattr(main, "refresh_active_page", lambda: None)
    monkeypatch.setattr(main, "dismiss_cookie_and_consent_banners", lambda **kwargs: "")
    monkeypatch.setattr(main, "wait_for_grok_com_landing", lambda **kwargs: True)
    monkeypatch.setattr(
        main,
        "_iter_cookie_sources",
        lambda: [
            (
                fake_page,
                [{"name": "sso", "domain": ".grok.com", "value": "private-sso"}],
            )
        ],
    )

    assert main.wait_for_sso_cookie(timeout=1) == "private-sso"
    assert fake_page.submit_retries == 0


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://accounts.x.ai/sign-up?redirect=grok-com", True),
        ("https://accounts.x.ai/signup", True),
        ("https://accounts.x.ai/register", True),
        ("https://accounts.x.ai/account", False),
        ("https://grok.com/", False),
        ("", False),
    ],
)
def test_signup_flow_url_detection(url, expected):
    assert main.is_signup_flow_url(url) is expected
