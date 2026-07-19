from __future__ import annotations

from base_mailbox import CFWorkerMailbox, CloudflareTempEmailMailbox
from mail_providers import (
    MAIL_PROVIDER_CHOICES,
    extra_from_config,
    make_mailbox,
    normalize_provider,
    provider_ready,
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
