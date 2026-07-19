from __future__ import annotations

import requests
import pytest

from base_mailbox import FreemailMailbox, MailboxAccount


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = {} if payload is None else payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.headers = {}
        self.proxies = {}

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._request("DELETE", url, **kwargs)


def _mailbox(monkeypatch, responses):
    session = FakeSession(responses)
    monkeypatch.setattr(requests, "Session", lambda: session)
    mailbox = FreemailMailbox(
        "https://mail.example.com",
        username="admin",
        password="secret",
    )
    return mailbox, session


def test_login_http_error_is_raised(monkeypatch):
    mailbox, _ = _mailbox(monkeypatch, [FakeResponse(status_code=401)])

    with pytest.raises(requests.HTTPError, match="401"):
        mailbox.probe_send_capability()


def test_probe_reports_login_send_capability_and_reuses_session(monkeypatch):
    mailbox, session = _mailbox(
        monkeypatch,
        [FakeResponse({"success": True, "can_send": 1})],
    )

    first = mailbox.probe_send_capability()
    second = mailbox.probe_send_capability()

    assert first == {
        "available": True,
        "reason": "账号允许发件；服务端发件渠道将在实际发送时验证",
    }
    assert second == first
    assert [call[1] for call in session.calls] == [
        "https://mail.example.com/api/login"
    ]


def test_send_is_blocked_when_login_account_cannot_send(monkeypatch):
    mailbox, session = _mailbox(
        monkeypatch,
        [FakeResponse({"success": True, "can_send": 0})],
    )

    capability = mailbox.probe_send_capability()
    with pytest.raises(RuntimeError, match="未启用发件权限"):
        mailbox.send_test_message(
            sender="sender@example.com",
            recipient="recipient@example.com",
            subject="Grok verification code",
            text="ABC-123",
        )

    assert capability["available"] is False
    assert len(session.calls) == 1


def test_send_posts_one_recipient_and_returns_safe_metadata(monkeypatch):
    mailbox, session = _mailbox(
        monkeypatch,
        [
            FakeResponse({"success": True, "can_send": 1}),
            FakeResponse({"success": True, "id": "message-id", "provider": "resend"}),
        ],
    )

    result = mailbox.send_test_message(
        sender="sender@example.com",
        recipient="recipient@example.com",
        subject="Grok verification code",
        text="ABC-123",
    )

    assert result == {"success": True, "id": "message-id", "provider": "resend"}
    method, url, kwargs = session.calls[-1]
    assert method == "POST"
    assert url == "https://mail.example.com/api/send"
    assert kwargs["json"] == {
        "from": "sender@example.com",
        "fromName": "Grok Register Mail Test",
        "to": "recipient@example.com",
        "subject": "Grok verification code",
        "text": "ABC-123",
    }


def test_send_http_error_is_raised_before_json_parse(monkeypatch):
    mailbox, _ = _mailbox(
        monkeypatch,
        [
            FakeResponse({"success": True, "can_send": 1}),
            FakeResponse(
                {"error": "发送失败: 发件渠道暂不可用"},
                status_code=503,
            ),
        ],
    )

    with pytest.raises(RuntimeError, match="发件渠道暂不可用"):
        mailbox.send_test_message(
            sender="sender@example.com",
            recipient="recipient@example.com",
            subject="subject",
            text="ABC-123",
        )


def test_delete_email_uses_only_generated_address(monkeypatch):
    mailbox, session = _mailbox(
        monkeypatch,
        [
            FakeResponse({"success": True, "can_send": 1}),
            FakeResponse({"success": True, "deleted": True}),
        ],
    )

    assert mailbox.delete_email(
        MailboxAccount(email="generated@example.com", account_id="opaque-token")
    ) is True
    method, url, kwargs = session.calls[-1]
    assert method == "DELETE"
    assert url == "https://mail.example.com/api/mailboxes"
    assert kwargs["params"] == {"address": "generated@example.com"}
    assert "opaque-token" not in str(session.calls)
