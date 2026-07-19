from __future__ import annotations

import smtplib

import pytest

from email_test_senders import (
    DirectMxTestSender,
    SenderUnavailableError,
    SmtpRelayTestSender,
    choose_test_sender,
)


class NativeMailbox:
    def __init__(self, available=True):
        self.available = available
        self.sent = []

    def probe_send_capability(self):
        return {
            "available": self.available,
            "reason": "native disabled" if not self.available else "",
        }

    def send_test_message(self, **kwargs):
        self.sent.append(kwargs)
        return {"success": True, "provider": "native"}


def _smtp_config(**overrides):
    config = {
        "mail_test_sender_mode": "auto",
        "mail_test_smtp_host": "smtp.example.com",
        "mail_test_smtp_port": 587,
        "mail_test_smtp_security": "starttls",
        "mail_test_smtp_username": "",
        "mail_test_smtp_password": "",
        "mail_test_smtp_from": "sender@example.com",
        "mail_test_direct_mx_enabled": False,
    }
    config.update(overrides)
    return config


def test_choose_prefers_available_provider_native_sender():
    sender = choose_test_sender(_smtp_config(), "freemail", NativeMailbox(True))

    assert sender.mode == "native"


def test_choose_falls_back_to_complete_smtp_configuration():
    sender = choose_test_sender(_smtp_config(), "freemail", NativeMailbox(False))

    assert sender.mode == "smtp"


def test_choose_uses_direct_mx_only_when_explicitly_enabled():
    config = _smtp_config(
        mail_test_smtp_host="",
        mail_test_smtp_from="",
        mail_test_direct_mx_enabled=True,
    )

    sender = choose_test_sender(config, "unknown", object())

    assert sender.mode == "direct_mx"


def test_choose_reports_all_unavailable_reasons():
    config = _smtp_config(
        mail_test_smtp_host="",
        mail_test_smtp_from="",
        mail_test_direct_mx_enabled=False,
    )

    with pytest.raises(SenderUnavailableError) as caught:
        choose_test_sender(config, "unknown", object())

    message = str(caught.value)
    assert "原生" in message
    assert "SMTP" in message
    assert "Direct MX" in message


def test_forced_native_never_falls_back_to_smtp():
    config = _smtp_config(mail_test_sender_mode="native")

    with pytest.raises(SenderUnavailableError, match="native disabled"):
        choose_test_sender(config, "freemail", NativeMailbox(False))


def test_forced_smtp_does_not_probe_or_choose_native():
    config = _smtp_config(mail_test_sender_mode="smtp")

    sender = choose_test_sender(config, "freemail", NativeMailbox(True))

    assert sender.mode == "smtp"


class FakeSmtpClient:
    def __init__(self, *, login_error=None, rcpt_code=250):
        self.calls = []
        self.login_error = login_error
        self.rcpt_code = rcpt_code

    def ehlo(self):
        self.calls.append(("ehlo",))
        return 250, b"ok"

    def starttls(self):
        self.calls.append(("starttls",))
        return 220, b"ready"

    def login(self, username, password):
        self.calls.append(("login", username, password))
        if self.login_error:
            raise self.login_error

    def send_message(self, message, from_addr, to_addrs):
        self.calls.append(("send_message", message, from_addr, to_addrs))
        return {}

    def quit(self):
        self.calls.append(("quit",))


class RecordingSmtpFactories:
    def __init__(self, clients=None):
        self.clients = list(clients or [FakeSmtpClient()])
        self.connections = []

    def _connect(self, kind, host, port, timeout):
        client = self.clients.pop(0)
        self.connections.append((kind, host, port, timeout, client))
        return client

    def SMTP(self, host, port, timeout):
        return self._connect("smtp", host, port, timeout)

    def SMTP_SSL(self, host, port, timeout):
        return self._connect("ssl", host, port, timeout)


@pytest.mark.parametrize(
    ("security", "connection_kind", "uses_starttls"),
    [("ssl", "ssl", False), ("starttls", "smtp", True), ("plain", "smtp", False)],
)
def test_smtp_security_modes_and_single_recipient(
    security, connection_kind, uses_starttls
):
    factories = RecordingSmtpFactories()
    sender = SmtpRelayTestSender(
        _smtp_config(mail_test_smtp_security=security),
        smtp_factory=factories,
    )

    result = sender.send(recipient="generated@example.com", code="ABC-123")

    kind, host, port, timeout, client = factories.connections[0]
    assert (kind, host, port) == (connection_kind, "smtp.example.com", 587)
    assert timeout <= 30
    assert (("starttls",) in client.calls) is uses_starttls
    assert not any(call[0] == "login" for call in client.calls)
    send_call = next(call for call in client.calls if call[0] == "send_message")
    message, from_addr, recipients = send_call[1:]
    assert from_addr == "sender@example.com"
    assert recipients == ["generated@example.com"]
    assert message["To"] == "generated@example.com"
    assert "Verification code: ABC-123" in message.get_content()
    assert result["mode"] == "smtp"


def test_smtp_authenticates_only_with_complete_credentials():
    factories = RecordingSmtpFactories()
    sender = SmtpRelayTestSender(
        _smtp_config(
            mail_test_smtp_username="sender-user",
            mail_test_smtp_password="sender-password",
        ),
        smtp_factory=factories,
    )

    sender.send(recipient="generated@example.com", code="ABC-123")

    client = factories.connections[0][-1]
    assert ("login", "sender-user", "sender-password") in client.calls


def test_smtp_capability_rejects_partial_authentication():
    sender = SmtpRelayTestSender(
        _smtp_config(mail_test_smtp_username="sender-user", mail_test_smtp_password="")
    )

    capability = sender.capability()

    assert capability.available is False
    assert "同时配置" in capability.reason


def test_smtp_authentication_error_is_readable_without_credentials():
    error = smtplib.SMTPAuthenticationError(535, b"denied")
    factories = RecordingSmtpFactories([FakeSmtpClient(login_error=error)])
    sender = SmtpRelayTestSender(
        _smtp_config(
            mail_test_smtp_username="sensitive-user",
            mail_test_smtp_password="sensitive-password",
        ),
        smtp_factory=factories,
    )

    with pytest.raises(RuntimeError) as caught:
        sender.send(recipient="generated@example.com", code="ABC-123")

    message = str(caught.value)
    assert "认证失败" in message
    assert "sensitive-user" not in message
    assert "sensitive-password" not in message


class FakeMxRecord:
    def __init__(self, preference, exchange):
        self.preference = preference
        self.exchange = exchange


class FakeResolver:
    def __init__(self, records=None, error=None):
        self.records = records or []
        self.error = error
        self.calls = []

    def resolve(self, domain, record_type):
        self.calls.append((domain, record_type))
        if self.error:
            raise self.error
        return self.records


class FakeDirectMxClient(FakeSmtpClient):
    def __init__(self, *, rcpt_code=250, starttls=False):
        super().__init__(rcpt_code=rcpt_code)
        self.advertise_starttls = starttls

    def has_extn(self, name):
        self.calls.append(("has_extn", name))
        return self.advertise_starttls and name.lower() == "starttls"

    def mail(self, sender):
        self.calls.append(("mail", sender))
        return 250, b"ok"

    def rcpt(self, recipient):
        self.calls.append(("rcpt", recipient))
        return self.rcpt_code, b"accepted" if self.rcpt_code < 300 else b"rejected"

    def data(self, content):
        self.calls.append(("data", content))
        return 250, b"queued"


def test_direct_mx_is_disabled_by_default():
    sender = DirectMxTestSender({}, resolver=FakeResolver())

    assert sender.capability().available is False
    assert "未启用" in sender.capability().reason


def test_direct_mx_derives_domain_sorts_hosts_and_tries_until_accepted():
    resolver = FakeResolver(
        [FakeMxRecord(20, "mx2.example.com."), FakeMxRecord(10, "mx1.example.com.")]
    )
    factories = RecordingSmtpFactories(
        [FakeDirectMxClient(rcpt_code=550), FakeDirectMxClient(starttls=True)]
    )
    sender = DirectMxTestSender(
        {"mail_test_direct_mx_enabled": True},
        resolver=resolver,
        smtp_factory=factories,
    )

    result = sender.send(recipient="generated@Recipient.Example", code="ABC-123")

    assert resolver.calls == [("recipient.example", "MX")]
    assert [(item[1], item[2]) for item in factories.connections] == [
        ("mx1.example.com", 25),
        ("mx2.example.com", 25),
    ]
    accepted = factories.connections[-1][-1]
    assert ("rcpt", "generated@Recipient.Example") in accepted.calls
    data = next(call[1] for call in accepted.calls if call[0] == "data")
    assert "Verification code: ABC-123" in data
    assert ("starttls",) in accepted.calls
    assert result["mode"] == "direct_mx"


@pytest.mark.parametrize(
    "recipient",
    ["missing-at", "two@@example.com", "header@example.com\r\nBcc:x@example.com"],
)
def test_direct_mx_rejects_malformed_recipient(recipient):
    sender = DirectMxTestSender(
        {"mail_test_direct_mx_enabled": True}, resolver=FakeResolver()
    )

    with pytest.raises(ValueError, match="收件地址"):
        sender.send(recipient=recipient, code="ABC-123")


def test_direct_mx_returns_aggregate_dns_diagnostic():
    sender = DirectMxTestSender(
        {"mail_test_direct_mx_enabled": True},
        resolver=FakeResolver(error=RuntimeError("dns unavailable")),
    )

    with pytest.raises(RuntimeError) as caught:
        sender.send(recipient="generated@example.com", code="ABC-123")

    assert "MX 查询失败" in str(caught.value)
