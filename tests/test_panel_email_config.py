from __future__ import annotations

import json

import pytest

from panel import app as panel_app


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    return config_path


def _write_config(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_public_config_normalizes_legacy_cloudflare_and_exposes_canonical_fields():
    public = panel_app.email_config_public(
        {
            "email_provider": "cloudflare",
            "cloudflare_api_base": "https://mail.example.com",
            "cloudflare_api_key": "legacy-admin",
            "defaultDomains": "example.com",
            "cfworker_custom_auth": "legacy-site",
        }
    )

    assert public["provider"] == "cloudflare_temp_email"
    assert public["cloudflare_api_base"] == "https://mail.example.com"
    assert public["cloudflare_admin_password"] == "legacy-admin"
    assert public["cloudflare_domain"] == "example.com"
    assert public["cloudflare_site_password"] == "legacy-site"


@pytest.mark.parametrize(
    "missing",
    ["cloudflare_api_base", "cloudflare_admin_password", "cloudflare_domain"],
)
def test_selected_cloudflare_provider_requires_reference_configuration(
    isolated_config, missing
):
    data = {
        "provider": "cloudflare_temp_email",
        "cloudflare_api_base": "https://mail.example.com",
        "cloudflare_admin_password": "admin-secret",
        "cloudflare_domain": "example.com",
        "cloudflare_site_password": "site-secret",
    }
    data[missing] = ""

    with pytest.raises(ValueError, match=missing):
        panel_app.apply_email_config_from_ui(data)


def test_save_writes_canonical_and_compatibility_fields(isolated_config):
    public = panel_app.apply_email_config_from_ui(
        {
            "provider": "cloudflare",
            "cloudflare_api_base": "mail.example.com/",
            "cloudflare_admin_password": "admin-secret",
            "cloudflare_domain": "@Example.COM",
            "cloudflare_site_password": "site-secret",
        }
    )
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))

    assert public["provider"] == "cloudflare_temp_email"
    assert saved["email_provider"] == "cloudflare_temp_email"
    assert saved["email_providers"] == ["cloudflare_temp_email"]
    assert saved["cloudflare_api_base"] == "https://mail.example.com"
    assert saved["cloudflare_admin_password"] == "admin-secret"
    assert saved["cloudflare_domain"] == "example.com"
    assert saved["cloudflare_site_password"] == "site-secret"
    assert saved["cloudflare_api_key"] == "admin-secret"
    assert saved["defaultDomains"] == "example.com"


def test_switching_provider_does_not_erase_cloudflare_settings(isolated_config):
    original = {
        "email_provider": "cloudflare_temp_email",
        "cloudflare_api_base": "https://mail.example.com",
        "cloudflare_admin_password": "admin-secret",
        "cloudflare_domain": "example.com",
        "cloudflare_site_password": "site-secret",
    }
    _write_config(isolated_config, original)

    panel_app.apply_email_config_from_ui(
        {
            "provider": "moemail",
            "moemail_api_url": "https://sall.cc",
            "moemail_api_key": "",
        }
    )
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))

    for key in (
        "cloudflare_api_base",
        "cloudflare_admin_password",
        "cloudflare_domain",
        "cloudflare_site_password",
    ):
        assert saved[key] == original[key]


def test_cloudflare_panel_uses_only_reference_configuration_fields():
    html = panel_app.INDEX_HTML

    for field in (
        "cloudflare_api_base",
        "cloudflare_admin_password",
        "cloudflare_domain",
        "cloudflare_site_password",
    ):
        assert f'id="{field}"' in html
    assert 'id="custom_path_token"' not in html
    assert "testCloudflareEmailConnection()" in html


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def test_connection_probe_is_non_mutating_and_uses_reference_headers(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(200, {"domains": ["example.com"]})

    monkeypatch.setattr("requests.request", fake_request)
    result = panel_app.probe_cloudflare_temp_email(
        {
            "cloudflare_api_base": "https://mail.example.com",
            "cloudflare_admin_password": "admin-secret",
            "cloudflare_domain": "example.com",
            "cloudflare_site_password": "site-secret",
        }
    )

    assert result["ok"] is True
    assert result["endpoint"] == "/open_api/settings"
    assert [call[0] for call in calls] == ["GET"]
    assert all("new_address" not in call[1] for call in calls)
    assert calls[0][2]["headers"]["x-admin-auth"] == "admin-secret"
    assert calls[0][2]["headers"]["x-custom-auth"] == "site-secret"


def test_connection_probe_falls_back_only_when_open_settings_is_missing(monkeypatch):
    calls = []
    responses = iter([FakeResponse(404), FakeResponse(200, {"ok": True})])

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return next(responses)

    monkeypatch.setattr("requests.request", fake_request)
    result = panel_app.probe_cloudflare_temp_email(
        {
            "cloudflare_api_base": "https://mail.example.com",
            "cloudflare_admin_password": "admin-secret",
            "cloudflare_domain": "example.com",
        }
    )

    assert result["ok"] is True
    assert result["endpoint"] == "/api/settings"
    assert calls == [
        "https://mail.example.com/open_api/settings",
        "https://mail.example.com/api/settings",
    ]


def test_connection_probe_does_not_hide_auth_failure(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return FakeResponse(401, {"error": "unauthorized"})

    monkeypatch.setattr("requests.request", fake_request)

    with pytest.raises(RuntimeError, match="HTTP 401"):
        panel_app.probe_cloudflare_temp_email(
            {
                "cloudflare_api_base": "https://mail.example.com",
                "cloudflare_admin_password": "admin-secret",
                "cloudflare_domain": "example.com",
            }
        )
    assert len(calls) == 1


def test_email_test_route_is_registered():
    rules = {rule.rule for rule in panel_app.app.url_map.iter_rules()}
    assert "/api/config/email/test" in rules
