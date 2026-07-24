from __future__ import annotations

import io
import json
import threading
import zipfile

import pytest

from panel import app as panel_app


ACTIVE_EMAIL = "active@example.com"
ACTIVE_PASSWORD = "active-password"
ACTIVE_SSO = "active-web-sso"
ACTIVE_ACCESS = "active-access-token"
ACTIVE_REFRESH = "active-refresh-token"

DISABLED_EMAIL = "disabled@example.com"
DISABLED_PASSWORD = "disabled-password-canary"
DISABLED_SSO = "disabled-web-sso-canary"
DISABLED_ACCESS = "disabled-access-token-canary"
DISABLED_REFRESH = "disabled-refresh-token-canary"


def _write_cpa(
    path,
    *,
    email: str,
    sso: str,
    subject: str,
    access: str,
    refresh: str,
) -> None:
    path.write_text(
        json.dumps(
            {
                "email": email,
                "sub": subject,
                "sso": sso,
                "access_token": access,
                "refresh_token": refresh,
                "id_token": f"id-{subject}",
                "token_type": "Bearer",
                "expired": "2026-07-25T00:00:00Z",
                "base_url": "https://cli-chat-proxy.grok.com/v1",
                "disabled": False,
                "_authorized_at": "2026-07-24T01:00:00Z",
                "_authorization_id": f"authorization-{subject}",
                "_authorization_generation": 1,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def isolated_disabled_exports(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    config_path = app_root / "config.json"
    config_path.write_text(
        json.dumps({"credentials_dir": "vault"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(panel_app, "BASE_DIR", app_root)
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.delenv(panel_app.CPA_DIR_ENV, raising=False)
    monkeypatch.setattr(panel_app, "PANEL_AUTH", False)
    monkeypatch.setattr(panel_app, "_credential_import_lock", threading.RLock())
    monkeypatch.setattr(panel_app, "_credential_migration_lock", threading.Lock())
    with panel_app._oauth_export_ticket_lock:
        panel_app._oauth_export_tickets.clear()

    layout = panel_app.current_credential_layout()
    (layout.sso_dir / "accounts_batch.txt").write_text(
        "\n".join(
            (
                f"{ACTIVE_EMAIL}----{ACTIVE_PASSWORD}----{ACTIVE_SSO}",
                f"{DISABLED_EMAIL}----{DISABLED_PASSWORD}----{DISABLED_SSO}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _write_cpa(
        layout.cpa_dir / "xai-active.json",
        email=ACTIVE_EMAIL,
        sso=ACTIVE_SSO,
        subject="subject-active",
        access=ACTIVE_ACCESS,
        refresh=ACTIVE_REFRESH,
    )
    _write_cpa(
        layout.cpa_dir / "xai-disabled.json",
        email=DISABLED_EMAIL,
        sso=DISABLED_SSO,
        subject="subject-disabled",
        access=DISABLED_ACCESS,
        refresh=DISABLED_REFRESH,
    )
    (layout.cpa_dir / "failed.jsonl").write_text(
        json.dumps(
            {
                "email": DISABLED_EMAIL,
                "fp": panel_app.sso_fingerprint(DISABLED_SSO),
                "error": "consent failed: Access denied",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    panel_app.current_disabled_account_pool().disable(
        {
            "email": DISABLED_EMAIL,
            "password": DISABLED_PASSWORD,
            "sso": DISABLED_SSO,
            "subject": "subject-disabled",
            "source": "accounts_batch.txt",
            "raw": (
                f"{DISABLED_EMAIL}----{DISABLED_PASSWORD}"
                f"----{DISABLED_SSO}"
            ),
        },
        "Access denied",
    )
    panel_app.invalidate_account_catalog()
    return layout


def _assert_disabled_secrets_absent(text: str) -> None:
    for secret in (
        DISABLED_EMAIL,
        DISABLED_PASSWORD,
        DISABLED_SSO,
        DISABLED_ACCESS,
        DISABLED_REFRESH,
        "subject-disabled",
    ):
        assert secret not in text


def _zip_text(response) -> str:
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        return "\n".join(
            archive.read(name).decode("utf-8", errors="replace")
            for name in archive.namelist()
        )


def _claim_and_download(client, artifact: str):
    claim = client.post(
        "/api/oauth/export-claim",
        json={
            "artifact": artifact,
            "target_instance": "disabled-filter-test",
            "acknowledge_previous_instance_disabled": False,
        },
    )
    assert claim.status_code == 200, claim.get_data(as_text=True)
    return client.get(claim.get_json()["download_url"])


@pytest.mark.parametrize(
    "path",
    [
        "/preview/accounts_batch.txt",
        "/download/accounts_batch.txt",
        "/download/sso.txt",
        "/download/merged.txt",
        "/download/accounts.json",
        "/download/grok2api.json",
    ],
)
def test_plain_and_json_exports_exclude_disabled_accounts(
    isolated_disabled_exports,
    path,
):
    response = panel_app.app.test_client().get(path)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert ACTIVE_EMAIL in body
    _assert_disabled_secrets_absent(body)


def test_all_zip_rewrites_every_account_member_without_disabled_lines(
    isolated_disabled_exports,
):
    response = panel_app.app.test_client().get("/download/all.zip")

    assert response.status_code == 200
    body = _zip_text(response)
    assert ACTIVE_EMAIL in body
    assert ACTIVE_PASSWORD in body
    assert ACTIVE_SSO in body
    _assert_disabled_secrets_absent(body)


@pytest.mark.parametrize("artifact", ["cpa.zip", "sub2.zip"])
def test_oauth_zip_exports_exclude_disabled_cpa_credentials(
    isolated_disabled_exports,
    artifact,
):
    response = _claim_and_download(panel_app.app.test_client(), artifact)

    assert response.status_code == 200
    body = _zip_text(response)
    assert ACTIVE_EMAIL in body
    assert ACTIVE_ACCESS in body
    assert ACTIVE_REFRESH in body
    _assert_disabled_secrets_absent(body)


def test_sub2_json_excludes_disabled_cpa_credentials(
    isolated_disabled_exports,
):
    response = _claim_and_download(
        panel_app.app.test_client(),
        "sub2.json",
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert ACTIVE_EMAIL in body
    assert ACTIVE_ACCESS in body
    assert ACTIVE_REFRESH in body
    _assert_disabled_secrets_absent(body)


def test_account_api_and_catalog_only_project_active_accounts(
    isolated_disabled_exports,
):
    client = panel_app.app.test_client()

    legacy = client.get("/api/accounts")
    modern = client.get("/api/v2/accounts?page=1&page_size=25")

    assert legacy.status_code == 200
    assert modern.status_code == 200
    assert [item["email"] for item in legacy.get_json()["accounts"]] == [
        ACTIVE_EMAIL
    ]
    payload = modern.get_json()
    assert [item["email"] for item in payload["items"]] == [ACTIVE_EMAIL]
    assert payload["summary"]["total_accounts"] == 1
    assert payload["files"][0]["count"] == 1
    _assert_disabled_secrets_absent(legacy.get_data(as_text=True))
    _assert_disabled_secrets_absent(modern.get_data(as_text=True))
