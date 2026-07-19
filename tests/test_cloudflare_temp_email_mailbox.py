from __future__ import annotations

from dataclasses import dataclass

import pytest

from base_mailbox import CloudflareTempEmailMailbox, MailboxAccount


@dataclass
class FakeResponse:
    status_code: int
    payload: object
    text: str = ""

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_create_address_uses_cloudflare_admin_contract(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(
            200,
            {
                "address": "alice@example.com",
                "jwt": "address-jwt",
                "address_id": 7,
            },
        )

    monkeypatch.setattr("requests.request", fake_request)
    box = CloudflareTempEmailMailbox(
        api_base="https://mail.example.com/",
        admin_password="admin-secret",
        domain="example.com",
        site_password="site-secret",
    )

    account = box.get_email()

    assert account.email == "alice@example.com"
    assert account.account_id == "address-jwt"
    assert account.extra == {"address_id": 7, "jwt": "address-jwt"}
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert (method, url) == (
        "POST",
        "https://mail.example.com/admin/new_address",
    )
    assert kwargs["headers"]["x-admin-auth"] == "admin-secret"
    assert kwargs["headers"]["x-custom-auth"] == "site-secret"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["json"]["domain"] == "example.com"
    assert kwargs["json"]["enablePrefix"] is False
    assert kwargs["json"]["name"]
    assert kwargs["timeout"] == 15


def test_create_address_normalizes_scheme_and_domain(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(200, {"address": "bob@example.com", "jwt": "jwt"})

    monkeypatch.setattr("requests.request", fake_request)
    box = CloudflareTempEmailMailbox(
        api_base="mail.example.com/",
        admin_password="admin",
        domain=" @Example.COM ",
    )

    box.get_email()

    assert calls[0][1] == "https://mail.example.com/admin/new_address"
    assert calls[0][2]["json"]["domain"] == "example.com"
    assert "x-custom-auth" not in calls[0][2]["headers"]


@pytest.mark.parametrize(
    ("kwargs", "missing_name"),
    [
        (
            {"api_base": "", "admin_password": "admin", "domain": "example.com"},
            "API Base",
        ),
        (
            {
                "api_base": "https://mail.example.com",
                "admin_password": "",
                "domain": "example.com",
            },
            "Admin",
        ),
        (
            {
                "api_base": "https://mail.example.com",
                "admin_password": "admin",
                "domain": "",
            },
            "domain",
        ),
    ],
)
def test_create_address_validates_required_configuration(kwargs, missing_name):
    box = CloudflareTempEmailMailbox(**kwargs)

    with pytest.raises(RuntimeError, match=missing_name):
        box.get_email()


def test_create_address_error_does_not_leak_configured_secrets(monkeypatch):
    def fake_request(method, url, **kwargs):
        return FakeResponse(403, {"error": "forbidden"}, text="forbidden")

    monkeypatch.setattr("requests.request", fake_request)
    box = CloudflareTempEmailMailbox(
        api_base="https://mail.example.com",
        admin_password="top-secret-admin",
        domain="example.com",
        site_password="top-secret-site",
    )

    with pytest.raises(RuntimeError) as exc_info:
        box.get_email()

    message = str(exc_info.value)
    assert "top-secret-admin" not in message
    assert "top-secret-site" not in message
    assert "HTTP 403" in message


def test_poll_parsed_mails_uses_address_jwt_and_skips_existing_ids(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(
            200,
            {
                "results": [
                    {"id": 1, "subject": "OLD-111 xAI confirmation code"},
                    {
                        "id": 2,
                        "subject": "UTF-6PW xAI confirmation code",
                        "text": "Use this confirmation code.",
                    },
                ]
            },
        )

    monkeypatch.setattr("requests.request", fake_request)
    box = CloudflareTempEmailMailbox(
        api_base="https://mail.example.com",
        admin_password="admin-secret",
        domain="example.com",
        site_password="site-secret",
    )
    account = MailboxAccount(
        email="alice@example.com",
        account_id="address-jwt",
        extra={"jwt": "address-jwt"},
    )

    code = box.wait_for_code(
        account,
        timeout=1,
        before_ids={"1"},
        code_pattern=r"[A-Z0-9]{3}-[A-Z0-9]{3}",
    )

    assert code == "UTF-6PW"
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert (method, url) == (
        "GET",
        "https://mail.example.com/api/parsed_mails",
    )
    assert kwargs["params"] == {"limit": 10, "offset": 0}
    assert kwargs["headers"]["Authorization"] == "Bearer address-jwt"
    assert kwargs["headers"]["x-custom-auth"] == "site-secret"
    assert "x-admin-auth" not in kwargs["headers"]


def test_poll_parsed_mails_ignores_mail_before_otp_timestamp(monkeypatch):
    def fake_request(method, url, **kwargs):
        return FakeResponse(
            200,
            {
                "results": [
                    {
                        "id": 1,
                        "created_at": "2026-07-19T00:00:00+00:00",
                        "subject": "OLD-111 xAI confirmation code",
                    },
                    {
                        "id": 2,
                        "created_at": "2026-07-19T00:01:00+00:00",
                        "subject": "NEW-222 xAI confirmation code",
                    },
                ]
            },
        )

    monkeypatch.setattr("requests.request", fake_request)
    box = CloudflareTempEmailMailbox(
        api_base="https://mail.example.com",
        admin_password="admin",
        domain="example.com",
    )
    account = MailboxAccount("alice@example.com", "address-jwt")

    code = box.wait_for_code(
        account,
        timeout=1,
        code_pattern=r"[A-Z0-9]{3}-[A-Z0-9]{3}",
        otp_sent_at=1784419230.0,
    )

    assert code == "NEW-222"


def test_poll_falls_back_to_raw_mails_only_when_parsed_endpoint_missing(monkeypatch):
    calls = []
    responses = iter(
        [
            FakeResponse(404, {"error": "not found"}, "not found"),
            FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 9,
                            "raw": (
                                "Subject: xAI confirmation\r\n"
                                "Content-Type: text/plain\r\n\r\n"
                                "Your verification code is RAW-123"
                            ),
                        }
                    ]
                },
            ),
        ]
    )

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return next(responses)

    monkeypatch.setattr("requests.request", fake_request)
    box = CloudflareTempEmailMailbox(
        api_base="https://mail.example.com",
        admin_password="admin",
        domain="example.com",
    )

    code = box.wait_for_code(
        MailboxAccount("alice@example.com", "address-jwt"),
        timeout=1,
        code_pattern=r"[A-Z0-9]{3}-[A-Z0-9]{3}",
    )

    assert code == "RAW-123"
    assert [call[1] for call in calls] == [
        "https://mail.example.com/api/parsed_mails",
        "https://mail.example.com/api/mails",
    ]


def test_poll_retries_rate_limit_without_falling_back(monkeypatch):
    calls = []
    responses = iter(
        [
            FakeResponse(429, {"error": "rate limited"}, "rate limited"),
            FakeResponse(
                200,
                {"results": [{"id": 4, "subject": "TRY-789 xAI confirmation code"}]},
            ),
        ]
    )

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return next(responses)

    monkeypatch.setattr("requests.request", fake_request)
    box = CloudflareTempEmailMailbox(
        api_base="https://mail.example.com",
        admin_password="admin",
        domain="example.com",
    )
    monkeypatch.setattr(box, "_sleep_with_checkpoint", lambda seconds: None)

    code = box.wait_for_code(
        MailboxAccount("alice@example.com", "address-jwt"),
        timeout=1,
        code_pattern=r"[A-Z0-9]{3}-[A-Z0-9]{3}",
    )

    assert code == "TRY-789"
    assert len(calls) == 2
    assert all(call[1].endswith("/api/parsed_mails") for call in calls)


def test_get_current_ids_uses_parsed_mailbox(monkeypatch):
    def fake_request(method, url, **kwargs):
        return FakeResponse(200, {"results": [{"id": 12}, {"id": "13"}]})

    monkeypatch.setattr("requests.request", fake_request)
    box = CloudflareTempEmailMailbox(
        api_base="https://mail.example.com",
        admin_password="admin",
        domain="example.com",
    )

    ids = box.get_current_ids(MailboxAccount("alice@example.com", "address-jwt"))

    assert ids == {"12", "13"}


def test_poll_does_not_fallback_on_server_error(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(500, {"error": "broken"}, "broken")

    monkeypatch.setattr("requests.request", fake_request)
    box = CloudflareTempEmailMailbox(
        api_base="https://mail.example.com",
        admin_password="admin-secret",
        domain="example.com",
    )

    with pytest.raises(RuntimeError, match="HTTP 500"):
        box.wait_for_code(
            MailboxAccount("alice@example.com", "address-jwt"),
            timeout=1,
            code_pattern=r"[A-Z0-9]{3}-[A-Z0-9]{3}",
        )

    assert len(calls) == 1


def test_delete_address_prefers_address_jwt(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(200, {"success": True})

    monkeypatch.setattr("requests.request", fake_request)
    box = CloudflareTempEmailMailbox(
        api_base="https://mail.example.com",
        admin_password="admin-secret",
        domain="example.com",
        site_password="site-secret",
    )
    account = MailboxAccount(
        "alice@example.com",
        "address-jwt",
        {"jwt": "address-jwt", "address_id": 7},
    )

    assert box.delete_email(account) is True
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert (method, url) == (
        "DELETE",
        "https://mail.example.com/api/delete_address",
    )
    assert kwargs["headers"]["Authorization"] == "Bearer address-jwt"
    assert kwargs["headers"]["x-custom-auth"] == "site-secret"
    assert "x-admin-auth" not in kwargs["headers"]


@pytest.mark.parametrize("user_status", [403, 404, 405])
def test_delete_address_falls_back_to_admin_id(monkeypatch, user_status):
    calls = []
    responses = iter(
        [
            FakeResponse(user_status, {"error": "disabled"}, "disabled"),
            FakeResponse(200, {"success": True}),
        ]
    )

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return next(responses)

    monkeypatch.setattr("requests.request", fake_request)
    box = CloudflareTempEmailMailbox(
        api_base="https://mail.example.com",
        admin_password="admin-secret",
        domain="example.com",
    )

    assert box.delete_email(
        MailboxAccount(
            "alice@example.com",
            "address-jwt",
            {"jwt": "address-jwt", "address_id": 7},
        )
    ) is True
    assert [call[1] for call in calls] == [
        "https://mail.example.com/api/delete_address",
        "https://mail.example.com/admin/delete_address/7",
    ]
    assert calls[1][2]["headers"]["x-admin-auth"] == "admin-secret"


def test_delete_address_does_not_hide_unrelated_server_failure(monkeypatch):
    def fake_request(method, url, **kwargs):
        return FakeResponse(500, {"error": "broken"}, "broken")

    monkeypatch.setattr("requests.request", fake_request)
    box = CloudflareTempEmailMailbox(
        api_base="https://mail.example.com",
        admin_password="admin-secret",
        domain="example.com",
    )

    with pytest.raises(RuntimeError, match="HTTP 500"):
        box.delete_email(
            MailboxAccount(
                "alice@example.com",
                "address-jwt",
                {"jwt": "address-jwt", "address_id": 7},
            )
        )
