from __future__ import annotations

from dataclasses import dataclass

import pytest

from base_mailbox import CloudflareTempEmailMailbox


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
