from __future__ import annotations

import re

import pytest

from base_mailbox import MailboxAccount
from mailbox_core import GROK_CODE_PATTERN
from email_receive_test import (
    ReceiveTestCancelled,
    ReceiveTestError,
    run_email_receive_test,
    sanitize_receive_test_error,
)


class FakeMailbox:
    def __init__(self, received_code="ABC-123", *, delete_result=True, delete_error=None):
        self.received_code = received_code
        self.delete_result = delete_result
        self.delete_error = delete_error
        self.calls = []
        self.account = MailboxAccount(
            email="private-local@example.com", account_id="account-token"
        )

    def get_email(self):
        self.calls.append(("get_email",))
        return self.account

    def get_current_ids(self, account):
        self.calls.append(("get_current_ids", account))
        return {"old-1", "old-2"}

    def wait_for_code(self, account, **kwargs):
        self.calls.append(("wait_for_code", account, kwargs))
        return self.received_code

    def delete_email(self, account):
        self.calls.append(("delete_email", account))
        if self.delete_error:
            raise self.delete_error
        return self.delete_result


class FakeSender:
    mode = "native"

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def send(self, *, recipient, code):
        self.calls.append((recipient, code))
        if self.error:
            raise self.error
        return {"mode": self.mode, "success": True}


def _run(box=None, sender=None, **kwargs):
    box = box or FakeMailbox()
    sender = sender or FakeSender()
    stages = []
    result = run_email_receive_test(
        {
            "email_provider": "freemail",
            "freemail_api_url": "https://mail.example.com",
            "mail_test_timeout_sec": 45,
        },
        on_stage=lambda stage, detail: stages.append((stage, detail)),
        cancelled=lambda: False,
        mailbox_factory=lambda config, provider, **factory_kwargs: (box, "freemail"),
        sender_selector=lambda config, provider, mailbox: sender,
        code_factory=lambda: "ABC-123",
        **kwargs,
    )
    return result, stages, box, sender


def test_success_runs_exact_stage_order_and_reuses_receiver_contract():
    result, stages, box, sender = _run()

    assert [stage for stage, _ in stages] == [
        "checking",
        "creating",
        "snapshotting",
        "sending",
        "waiting",
        "verifying",
        "cleaning",
        "succeeded",
    ]
    assert sender.calls == [("private-local@example.com", "ABC-123")]
    wait_call = next(call for call in box.calls if call[0] == "wait_for_code")
    assert wait_call[2]["before_ids"] == {"old-1", "old-2"}
    assert wait_call[2]["code_pattern"] == GROK_CODE_PATTERN
    assert wait_call[2]["timeout"] == 45
    assert result["ok"] is True
    assert result["email"] == "p***@example.com"
    assert result["provider"] == "freemail"
    assert result["sender_mode"] == "native"
    assert result["cleanup"] == "deleted"
    assert "ABC-123" not in str(result)
    assert "private-local@example.com" not in str(result)


def test_send_error_is_sanitized_and_cleanup_is_attempted_once():
    box = FakeMailbox()
    sender = FakeSender(RuntimeError("password=hunter2 code ABC-123 private-local@example.com"))

    with pytest.raises(ReceiveTestError) as caught:
        _run(box=box, sender=sender)

    assert caught.value.stage == "sending"
    text = str(caught.value)
    assert "hunter2" not in text
    assert "ABC-123" not in text
    assert "private-local" not in text
    assert sum(call[0] == "delete_email" for call in box.calls) == 1


def test_wrong_or_missing_code_fails_verification_and_cleans_up():
    for received in ("", "ZZZ-999"):
        box = FakeMailbox(received_code=received)

        with pytest.raises(ReceiveTestError) as caught:
            _run(box=box)

        assert caught.value.stage in ("waiting", "verifying")
        assert not re.search(r"[A-Z0-9]{3}-[A-Z0-9]{3}", str(caught.value))
        assert sum(call[0] == "delete_email" for call in box.calls) == 1


@pytest.mark.parametrize(
    ("cancel_stage", "cleanup_count"),
    [
        ("checking", 0),
        ("creating", 0),
        ("snapshotting", 1),
        ("sending", 1),
        ("waiting", 1),
        ("verifying", 1),
        ("cleaning", 1),
    ],
)
def test_cancel_at_each_stage_still_cleans_created_mailbox(cancel_stage, cleanup_count):
    box = FakeMailbox()
    active = {"cancelled": False}
    stages = []

    def on_stage(stage, detail):
        stages.append(stage)
        if stage == cancel_stage:
            active["cancelled"] = True

    with pytest.raises(ReceiveTestCancelled) as caught:
        run_email_receive_test(
            {
                "email_provider": "freemail",
                "freemail_api_url": "https://mail.example.com",
            },
            on_stage=on_stage,
            cancelled=lambda: active["cancelled"],
            mailbox_factory=lambda config, provider, **kwargs: (box, "freemail"),
            sender_selector=lambda config, provider, mailbox: FakeSender(),
            code_factory=lambda: "ABC-123",
        )

    assert caught.value.stage == cancel_stage
    assert stages[-1] == "cancelled"
    assert sum(call[0] == "delete_email" for call in box.calls) == cleanup_count


def test_cleanup_failure_is_warning_after_success():
    result, _, _, _ = _run(box=FakeMailbox(delete_result=False))

    assert result["ok"] is True
    assert result["cleanup"] == "failed"
    assert result["warnings"] == ["测试邮箱清理失败"]


def test_cleanup_failure_is_appended_to_primary_error():
    box = FakeMailbox(
        delete_error=RuntimeError("cookie=session-secret private-local@example.com")
    )
    sender = FakeSender(RuntimeError("primary send failure"))

    with pytest.raises(ReceiveTestError) as caught:
        _run(box=box, sender=sender)

    message = str(caught.value)
    assert "primary send failure" in message
    assert "清理" in message
    assert "session-secret" not in message
    assert "private-local" not in message


def test_error_sanitizer_redacts_credentials_cookie_email_and_codes():
    message = sanitize_receive_test_error(
        "Bearer bearer-secret Cookie=session-cookie password=plain-secret "
        "code ABC-123 private-local@example.com",
        config={
            "freemail_password": "configured-secret",
            "mail_test_smtp_password": "smtp-secret",
        },
        email="private-local@example.com",
    )

    for secret in (
        "bearer-secret",
        "session-cookie",
        "plain-secret",
        "ABC-123",
        "private-local",
        "configured-secret",
        "smtp-secret",
    ):
        assert secret not in message
