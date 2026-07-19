from __future__ import annotations

from base_mailbox import CFWorkerMailbox, CloudflareTempEmailMailbox
import mail_providers
from mail_providers import (
    MAIL_PROVIDER_CHOICES,
    extra_from_config,
    make_mailbox,
    normalize_provider,
    provider_ready,
    resolved_provider_config,
)


def test_legacy_cloudflare_alias_maps_to_dedicated_provider():
    assert normalize_provider("cloudflare") == "cloudflare_temp_email"
    assert normalize_provider("cloudflare-temp-email") == "cloudflare_temp_email"
    assert normalize_provider("cloudflare_temp_email") == "cloudflare_temp_email"


def test_custom_alias_still_maps_to_generic_cfworker():
    assert normalize_provider("custom") == "cfworker"
    box, provider = make_mailbox(
        {
            "cfworker_api_url": "https://generic.example.com",
            "cfworker_admin_token": "generic-admin",
            "cfworker_domain": "generic.example.com",
        },
        "cfworker",
    )

    assert provider == "cfworker"
    assert isinstance(box, CFWorkerMailbox)


def test_legacy_config_populates_canonical_cloudflare_fields():
    extra = extra_from_config(
        {
            "cloudflare_api_base": "https://mail.example.com",
            "cloudflare_api_key": "legacy-admin",
            "defaultDomains": "example.com",
            "cfworker_custom_auth": "legacy-site-password",
        }
    )

    assert extra["cloudflare_api_base"] == "https://mail.example.com"
    assert extra["cloudflare_admin_password"] == "legacy-admin"
    assert extra["cloudflare_domain"] == "example.com"
    assert extra["cloudflare_site_password"] == "legacy-site-password"


def test_canonical_cloudflare_fields_take_precedence_over_legacy_fields():
    extra = extra_from_config(
        {
            "cloudflare_api_base": "https://mail.example.com",
            "cloudflare_admin_password": "new-admin",
            "cloudflare_api_key": "legacy-admin",
            "cloudflare_domain": "new.example.com",
            "defaultDomains": "legacy.example.com",
            "cloudflare_site_password": "new-site",
            "cfworker_custom_auth": "legacy-site",
        }
    )

    assert extra["cloudflare_admin_password"] == "new-admin"
    assert extra["cloudflare_domain"] == "new.example.com"
    assert extra["cloudflare_site_password"] == "new-site"


def test_make_mailbox_builds_dedicated_cloudflare_adapter():
    box, provider = make_mailbox(
        {
            "cloudflare_api_base": "https://mail.example.com",
            "cloudflare_admin_password": "admin",
            "cloudflare_domain": "example.com",
            "cloudflare_site_password": "site",
        },
        "cloudflare",
    )

    assert provider == "cloudflare_temp_email"
    assert isinstance(box, CloudflareTempEmailMailbox)
    assert box.admin_password == "admin"
    assert box.domain == "example.com"
    assert box.site_password == "site"


def test_cloudflare_provider_requires_all_reference_configuration():
    complete = {
        "cloudflare_api_base": "https://mail.example.com",
        "cloudflare_admin_password": "admin",
        "cloudflare_domain": "example.com",
    }
    assert provider_ready(complete, "cloudflare_temp_email") is True

    for missing in complete:
        incomplete = dict(complete)
        incomplete[missing] = ""
        assert provider_ready(incomplete, "cloudflare_temp_email") is False


def test_provider_choices_expose_only_canonical_cloudflare_id():
    ids = [provider_id for provider_id, _ in MAIL_PROVIDER_CHOICES]

    assert "cloudflare_temp_email" in ids
    assert "cloudflare" not in ids


def test_freemail_environment_fills_missing_configuration():
    resolved = resolved_provider_config(
        {},
        environ={
            "MAIL_WEB_URL": " mail.example.com/ ",
            "ADMIN_NAME": " admin ",
            "ADMIN_PASSWORD": " secret ",
        },
    )

    assert resolved["freemail_api_url"] == "https://mail.example.com"
    assert resolved["freemail_username"] == "admin"
    assert resolved["freemail_password"] == "secret"


def test_explicit_freemail_configuration_wins_over_environment():
    resolved = resolved_provider_config(
        {
            "freemail_api_url": "https://configured.example.com/",
            "freemail_username": "configured-user",
            "freemail_password": "configured-password",
        },
        environ={
            "MAIL_WEB_URL": "https://environment.example.com",
            "ADMIN_NAME": "environment-user",
            "ADMIN_PASSWORD": "environment-password",
        },
    )

    assert resolved["freemail_api_url"] == "https://configured.example.com"
    assert resolved["freemail_username"] == "configured-user"
    assert resolved["freemail_password"] == "configured-password"


def test_freemail_api_url_removes_trailing_api_segments():
    resolved = resolved_provider_config(
        {"freemail_api_url": " https://mail.example.com/api/api/ "},
        environ={},
    )

    assert resolved["freemail_api_url"] == "https://mail.example.com"


def test_freemail_keeps_environment_login_as_runtime_fallback(monkeypatch):
    monkeypatch.setenv("ADMIN_NAME", "environment-user")
    monkeypatch.setenv("ADMIN_PASSWORD", "environment-password")

    box, provider = make_mailbox(
        {
            "freemail_api_url": "https://mail.example.com/api",
            "freemail_admin_token": "stale-token",
            "freemail_username": "configured-user",
            "freemail_password": "stale-password",
        },
        "freemail",
    )

    assert provider == "freemail"
    assert box.api == "https://mail.example.com"
    assert box.fallback_username == "environment-user"
    assert box.fallback_password == "environment-password"


def test_freemail_environment_makes_provider_ready(monkeypatch):
    monkeypatch.setenv("MAIL_WEB_URL", "mail.example.com")

    assert provider_ready({}, "freemail") is True


def test_cleanup_active_cloudflare_address_and_reset_state(monkeypatch):
    calls = []

    class FakeBox:
        def delete_email(self, account):
            calls.append(account)
            return True

    account = object()
    monkeypatch.setattr(mail_providers, "_ACTIVE_BOX", FakeBox())
    monkeypatch.setattr(mail_providers, "_ACTIVE_ACCT", account)
    monkeypatch.setattr(
        mail_providers, "_ACTIVE_PROVIDER", "cloudflare_temp_email", raising=False
    )

    assert mail_providers.cleanup_active_mailbox() is True
    assert calls == [account]
    assert mail_providers._ACTIVE_BOX is None
    assert mail_providers._ACTIVE_ACCT is None
    assert mail_providers._ACTIVE_PROVIDER == ""


def test_cleanup_error_is_redacted_and_state_is_still_reset(monkeypatch):
    logs = []

    class FakeBox:
        def delete_email(self, account):
            raise RuntimeError("private-address-jwt")

    monkeypatch.setattr(mail_providers, "_ACTIVE_BOX", FakeBox())
    monkeypatch.setattr(mail_providers, "_ACTIVE_ACCT", object())
    monkeypatch.setattr(
        mail_providers, "_ACTIVE_PROVIDER", "cloudflare_temp_email", raising=False
    )

    assert mail_providers.cleanup_active_mailbox(log_callback=logs.append) is False
    assert "private-address-jwt" not in " ".join(logs)
    assert mail_providers._ACTIVE_BOX is None
    assert mail_providers._ACTIVE_ACCT is None
    assert mail_providers._ACTIVE_PROVIDER == ""
