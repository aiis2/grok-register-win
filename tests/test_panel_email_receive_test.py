from __future__ import annotations

import copy
import json
import re
import threading

import pytest

from panel import app as panel_app


@pytest.fixture(autouse=True)
def reset_receive_test_state(monkeypatch):
    monkeypatch.setitem(panel_app._job, "running", False)
    with panel_app._email_receive_test_lock:
        panel_app._email_receive_tests.clear()
        panel_app._email_receive_cancel_events.clear()
        panel_app._email_receive_active_id = None
    yield
    with panel_app._email_receive_test_lock:
        panel_app._email_receive_tests.clear()
        panel_app._email_receive_cancel_events.clear()
        panel_app._email_receive_active_id = None


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "email_provider": "freemail",
                "freemail_api_url": "https://stored.example.com",
                "freemail_username": "stored-user",
                "freemail_password": "stored-secret",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    return config_path


class DeferredThread:
    created = []

    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False
        self.__class__.created.append(self)

    def start(self):
        self.started = True


def test_state_reserves_one_active_test_and_returns_unpredictable_id(monkeypatch):
    DeferredThread.created = []
    monkeypatch.setattr(panel_app.threading, "Thread", DeferredThread)

    ok, first = panel_app.start_email_receive_test(
        {"email_provider": "freemail", "freemail_api_url": "https://mail.example.com"}
    )
    second_ok, second = panel_app.start_email_receive_test(
        {"email_provider": "freemail", "freemail_api_url": "https://mail.example.com"}
    )

    assert ok is True
    assert re.fullmatch(r"[0-9a-f]{32}", first["test_id"])
    assert first["status"] == "checking"
    assert first["running"] is True
    assert second_ok is False
    assert "运行" in second["error"]
    assert len(DeferredThread.created) == 1
    assert DeferredThread.created[0].started is True


def test_state_snapshot_is_a_deep_copy(monkeypatch):
    DeferredThread.created = []
    monkeypatch.setattr(panel_app.threading, "Thread", DeferredThread)
    _, started = panel_app.start_email_receive_test(
        {"email_provider": "freemail", "freemail_api_url": "https://mail.example.com"}
    )

    snapshot = panel_app.email_receive_test_snapshot(started["test_id"])
    snapshot["warnings"].append("mutated")

    fresh = panel_app.email_receive_test_snapshot(started["test_id"])
    assert fresh["warnings"] == []


def test_state_cancellation_sets_private_event(monkeypatch):
    DeferredThread.created = []
    monkeypatch.setattr(panel_app.threading, "Thread", DeferredThread)
    _, started = panel_app.start_email_receive_test(
        {"email_provider": "freemail", "freemail_api_url": "https://mail.example.com"}
    )

    ok, snapshot = panel_app.cancel_email_receive_test(started["test_id"])

    assert ok is True
    assert snapshot["cancel_requested"] is True
    assert panel_app._email_receive_cancel_events[started["test_id"]].is_set()


def test_state_prunes_completed_records_after_ttl(monkeypatch):
    test_id = "a" * 32
    with panel_app._email_receive_test_lock:
        panel_app._email_receive_tests[test_id] = {
            "test_id": test_id,
            "running": False,
            "finished_epoch": 100.0,
            "warnings": [],
        }
    monkeypatch.setattr(panel_app.time, "time", lambda: 100.0 + panel_app.EMAIL_RECEIVE_TEST_TTL_SEC + 1)

    assert panel_app.email_receive_test_snapshot(test_id) is None
    assert test_id not in panel_app._email_receive_tests


def test_state_registration_and_receive_test_are_mutually_exclusive(monkeypatch):
    DeferredThread.created = []
    monkeypatch.setattr(panel_app.threading, "Thread", DeferredThread)
    monkeypatch.setitem(panel_app._job, "running", True)

    ok, result = panel_app.start_email_receive_test(
        {"email_provider": "freemail", "freemail_api_url": "https://mail.example.com"}
    )
    assert ok is False
    assert "注册" in result["error"]

    monkeypatch.setitem(panel_app._job, "running", False)
    ok, _ = panel_app.start_email_receive_test(
        {"email_provider": "freemail", "freemail_api_url": "https://mail.example.com"}
    )
    assert ok is True
    register_ok, message = panel_app.start_job(1, concurrency=1)
    assert register_ok is False
    assert "邮箱收件测试" in message


def test_state_thread_start_failure_rolls_back_atomically(monkeypatch):
    class RaisingThread(DeferredThread):
        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(panel_app.threading, "Thread", RaisingThread)

    ok, result = panel_app.start_email_receive_test(
        {"email_provider": "freemail", "freemail_api_url": "https://mail.example.com"}
    )

    assert ok is False
    assert "启动失败" in result["error"]
    assert panel_app._email_receive_active_id is None
    assert panel_app._email_receive_tests == {}
    assert panel_app._email_receive_cancel_events == {}


def test_route_capabilities_uses_environment_fallback_without_secret_json(
    isolated_config, monkeypatch
):
    captured = {}
    monkeypatch.setenv("MAIL_WEB_URL", "https://environment.example.com")
    monkeypatch.setenv("ADMIN_NAME", "environment-user")
    monkeypatch.setenv("ADMIN_PASSWORD", "environment-secret")
    isolated_config.write_text("{}", encoding="utf-8")

    def fake_mailbox(config, provider, **kwargs):
        captured.update(config)
        return object(), provider

    monkeypatch.setattr(panel_app, "make_mailbox", fake_mailbox)
    monkeypatch.setattr(
        panel_app,
        "sender_capabilities",
        lambda config, provider, mailbox: [
            {"mode": "native", "available": True, "reason": ""}
        ],
    )

    response = panel_app.app.test_client().post(
        "/api/config/email/test-capabilities", json={"provider": "freemail"}
    )

    body = response.get_json()
    assert response.status_code == 200
    assert body["provider"] == "freemail"
    assert captured["freemail_api_url"] == "https://environment.example.com"
    serialized = response.get_data(as_text=True)
    assert "environment-secret" not in serialized
    assert "stored-secret" not in serialized


def test_route_start_uses_form_overrides_without_persisting(
    isolated_config, monkeypatch
):
    captured = {}

    def fake_start(config):
        captured.update(copy.deepcopy(config))
        return True, {
            "test_id": "b" * 32,
            "status": "checking",
            "running": True,
            "warnings": [],
        }

    monkeypatch.setattr(panel_app, "start_email_receive_test", fake_start)
    before = isolated_config.read_text(encoding="utf-8")

    response = panel_app.app.test_client().post(
        "/api/config/email/receive-test",
        json={
            "provider": "freemail",
            "freemail_api_url": "https://override.example.com",
            "freemail_password": "override-secret",
            "mail_test_sender_mode": "native",
        },
    )

    assert response.status_code == 202
    assert captured["freemail_api_url"] == "https://override.example.com"
    assert captured["freemail_password"] == "override-secret"
    assert isolated_config.read_text(encoding="utf-8") == before
    assert "override-secret" not in response.get_data(as_text=True)


def test_route_status_and_cancel_return_404_for_unknown_id():
    client = panel_app.app.test_client()

    status = client.get("/api/config/email/receive-test/" + "c" * 32)
    cancel = client.post("/api/config/email/receive-test/" + "c" * 32 + "/cancel")

    assert status.status_code == 404
    assert cancel.status_code == 404
    assert status.get_json()["error"] == "邮箱收件测试不存在或已过期"


def test_route_start_returns_409_when_registration_is_running(isolated_config, monkeypatch):
    monkeypatch.setitem(panel_app._job, "running", True)

    response = panel_app.app.test_client().post(
        "/api/config/email/receive-test", json={"provider": "freemail"}
    )

    assert response.status_code == 409
    assert "注册" in response.get_json()["error"]


def test_routes_require_auth_when_panel_auth_is_enabled(monkeypatch):
    monkeypatch.setattr(panel_app, "PANEL_AUTH", True)

    response = panel_app.app.test_client().post(
        "/api/config/email/test-capabilities", json={}
    )

    assert response.status_code == 401


def test_html_contains_generic_receive_test_controls_and_modal():
    html = panel_app.INDEX_HTML

    for element_id in (
        "btn_email_receive_test",
        "email_receive_test_modal",
        "email_receive_test_timeline",
        "email_receive_test_provider",
        "email_receive_test_sender",
        "email_receive_test_email",
        "email_receive_test_timing",
        "email_receive_test_message",
        "email_receive_test_cancel",
        "email_receive_test_close",
        "mail_test_sender_mode",
        "mail_test_timeout_sec",
        "mail_test_smtp_host",
        "mail_test_smtp_port",
        "mail_test_smtp_security",
        "mail_test_smtp_username",
        "mail_test_smtp_password",
        "mail_test_smtp_from",
        "mail_test_direct_mx_enabled",
        "freemail_username",
        "freemail_password",
    ):
        assert f'id="{element_id}"' in html
    button_tag = re.search(r'<button[^>]+id="btn_email_receive_test"[^>]*>', html)
    assert button_tag is not None
    assert "display:none" not in button_tag.group(0)


def test_html_receive_test_client_contract_is_generic_and_secret_safe():
    html = panel_app.INDEX_HTML

    for function_name in (
        "openEmailReceiveTest",
        "startEmailReceiveTest",
        "pollEmailReceiveTest",
        "cancelEmailReceiveTest",
    ):
        assert f"function {function_name}" in html or f"function {function_name}(" in html
    assert "/api/config/email/test-capabilities" in html
    assert "/api/config/email/receive-test" in html
    assert "/cancel" in html
    for stage in (
        "checking",
        "creating",
        "snapshotting",
        "sending",
        "waiting",
        "verifying",
        "cleaning",
        "succeeded",
    ):
        assert stage in html
    assert "['succeeded','failed','cancelled'].includes" in html
    assert "_set('freemail_password', e.freemail_password)" not in html
    assert "_set('mail_test_smtp_password', e.mail_test_smtp_password)" not in html
    assert ".textContent=test.code" not in html
