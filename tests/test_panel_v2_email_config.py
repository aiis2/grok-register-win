from __future__ import annotations

import json

import pytest

from panel import app as panel_app


SECRET_FIELDS = {
    "cfworker_admin_token",
    "cfworker_custom_auth",
    "cloudflare_admin_password",
    "cloudflare_site_password",
    "moemail_api_key",
    "gptmail_api_key",
    "duckmail_bearer",
    "duckmail_api_key",
    "maliapi_api_key",
    "luckmail_api_key",
    "skymail_token",
    "cloudmail_admin_password",
    "freemail_admin_token",
    "freemail_password",
    "opentrashmail_password",
    "laoudo_auth",
    "mail_test_smtp_password",
}


@pytest.fixture
def isolated_email_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.setattr(panel_app, "PANEL_AUTH", False)
    for name in ("MAIL_WEB_URL", "ADMIN_NAME", "ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    return config_path


def test_v2_email_config_returns_only_whitelisted_values_and_secret_flags(
    isolated_email_config,
):
    config = {
        "email_provider": "gptmail",
        "email_failover": True,
        "gptmail_base_url": "https://mail.example.test",
        "gptmail_domain": "example.test",
        "mail_test_smtp_host": "smtp.example.test",
        "mail_test_smtp_port": 2525,
    }
    canaries = {}
    for index, field in enumerate(sorted(SECRET_FIELDS)):
        canary = f"secret-canary-{index}-{field}"
        config[field] = canary
        canaries[field] = canary
    isolated_email_config.write_text(json.dumps(config), encoding="utf-8")

    response = panel_app.app.test_client().get("/api/v2/config/email")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    email = payload["email"]
    assert email["provider"] == "gptmail"
    assert email["values"]["gptmail_base_url"] == "https://mail.example.test"
    assert email["values"]["gptmail_domain"] == "example.test"
    assert email["values"]["mail_test_smtp_port"] == 2525
    assert email["configured"] == {field: True for field in sorted(SECRET_FIELDS)}
    assert not SECRET_FIELDS.intersection(email["values"])
    body = response.get_data(as_text=True)
    for canary in canaries.values():
        assert canary not in body


def test_v2_email_config_marks_missing_secrets_without_inventing_values(
    isolated_email_config,
):
    isolated_email_config.write_text(
        json.dumps(
            {
                "email_provider": "freemail",
                "freemail_api_url": "https://stored.example.test",
                "freemail_username": "stored-user",
            }
        ),
        encoding="utf-8",
    )

    response = panel_app.app.test_client().get("/api/v2/config/email")

    assert response.status_code == 200
    email = response.get_json()["email"]
    assert email["values"]["freemail_api_url"] == "https://stored.example.test"
    assert email["values"]["freemail_username"] == "stored-user"
    assert email["configured"] == {
        field: False for field in sorted(SECRET_FIELDS)
    }


def test_v2_email_config_exposes_environment_availability_not_environment_values(
    isolated_email_config, monkeypatch
):
    env_url = "https://environment-url-canary.example.test"
    env_user = "environment-user-canary"
    env_password = "environment-password-canary"
    monkeypatch.setenv("MAIL_WEB_URL", env_url)
    monkeypatch.setenv("ADMIN_NAME", env_user)
    monkeypatch.setenv("ADMIN_PASSWORD", env_password)
    isolated_email_config.write_text(
        json.dumps({"email_provider": "freemail"}), encoding="utf-8"
    )

    response = panel_app.app.test_client().get("/api/v2/config/email")

    assert response.status_code == 200
    email = response.get_json()["email"]
    assert email["environment"] == {
        "freemail_url_available": True,
        "freemail_username_available": True,
        "freemail_password_available": True,
    }
    assert email["values"]["freemail_api_url"] == ""
    assert email["values"]["freemail_username"] == ""
    body = response.get_data(as_text=True)
    assert env_url not in body
    assert env_user not in body
    assert env_password not in body


def test_v2_email_config_uses_existing_api_login_guard(
    isolated_email_config, monkeypatch
):
    monkeypatch.setattr(panel_app, "PANEL_AUTH", True)

    response = panel_app.app.test_client().get("/api/v2/config/email")

    assert response.status_code == 401
    assert response.get_json() == {"ok": False, "error": "unauthorized"}


def test_v2_email_save_retains_omitted_secret_and_returns_redacted_projection(
    isolated_email_config,
):
    secret = "saved-secret-must-not-return"
    isolated_email_config.write_text(
        json.dumps(
            {
                "email_provider": "gptmail",
                "gptmail_base_url": "https://old.example.test",
                "gptmail_api_key": secret,
            }
        ),
        encoding="utf-8",
    )

    response = panel_app.app.test_client().post(
        "/api/v2/config/email",
        json={
            "provider": "gptmail",
            "email_failover": True,
            "gptmail_base_url": "https://new.example.test",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["email"]["values"]["gptmail_base_url"] == (
        "https://new.example.test"
    )
    assert payload["email"]["configured"]["gptmail_api_key"] is True
    assert secret not in response.get_data(as_text=True)
    saved = json.loads(isolated_email_config.read_text(encoding="utf-8"))
    assert saved["gptmail_api_key"] == secret


def test_v2_cloudflare_connection_test_uses_saved_secret_without_returning_it(
    isolated_email_config, monkeypatch
):
    secret = "cloudflare-connection-secret-canary"
    isolated_email_config.write_text(
        json.dumps(
            {
                "email_provider": "cloudflare_temp_email",
                "cloudflare_api_base": "https://mail.example.test",
                "cloudflare_admin_password": secret,
                "cloudflare_domain": "example.test",
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    def fake_probe(config):
        captured.update(config)
        return {"ok": True, "message": "连接成功"}

    monkeypatch.setattr(panel_app, "probe_cloudflare_temp_email", fake_probe)

    response = panel_app.app.test_client().post(
        "/api/v2/config/email/test",
        json={
            "provider": "cloudflare_temp_email",
            "cloudflare_api_base": "https://mail.example.test",
            "cloudflare_domain": "example.test",
        },
    )

    assert response.status_code == 200
    assert captured["cloudflare_admin_password"] == secret
    assert secret not in response.get_data(as_text=True)
