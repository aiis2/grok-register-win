from __future__ import annotations

import json
import threading

import pytest

from panel import app as panel_app


@pytest.fixture
def isolated_oauth_export(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"credentials_dir": "vault"}), encoding="utf-8"
    )
    monkeypatch.setattr(panel_app, "BASE_DIR", tmp_path)
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.delenv(panel_app.CPA_DIR_ENV, raising=False)
    monkeypatch.setattr(panel_app, "PANEL_AUTH", False)
    monkeypatch.setattr(panel_app, "_credential_import_lock", threading.RLock())
    monkeypatch.setattr(panel_app, "_credential_migration_lock", threading.Lock())
    with panel_app._oauth_export_ticket_lock:
        panel_app._oauth_export_tickets.clear()
    layout = panel_app.current_credential_layout()
    (layout.sso_dir / "accounts_export.txt").write_text(
        "one@example.com----password----web-sso-one\n",
        encoding="utf-8",
    )
    panel_app.invalidate_account_catalog()
    return layout


def write_cpa(
    path,
    *,
    refresh_token: str,
    authorized_at: str,
    authorization_id: str,
    sub: str = "identity-one",
    generation: int = 1,
):
    path.write_text(
        json.dumps(
            {
                "email": "one@example.com",
                "sub": sub,
                "sso": "web-sso-one",
                "access_token": f"access-{refresh_token}",
                "refresh_token": refresh_token,
                "id_token": f"id-{refresh_token}",
                "token_type": "Bearer",
                "expired": "2026-07-24T00:00:00Z",
                "base_url": "https://cli-chat-proxy.grok.com/v1",
                "_authorized_at": authorized_at,
                "_authorization_id": authorization_id,
                "_authorization_generation": generation,
            }
        ),
        encoding="utf-8",
    )


def claim_artifact(
    client,
    artifact: str,
    target_instance: str,
    *,
    acknowledge: bool = False,
):
    return client.post(
        "/api/oauth/export-claim",
        json={
            "artifact": artifact,
            "target_instance": target_instance,
            "acknowledge_previous_instance_disabled": acknowledge,
        },
    )


def claim_and_download(client, artifact: str, target_instance: str):
    claim = claim_artifact(client, artifact, target_instance)
    assert claim.status_code == 200
    assert "no-store" in claim.headers.get("Cache-Control", "")
    download = client.get(claim.get_json()["download_url"])
    assert "no-store" in download.headers.get("Cache-Control", "")
    return claim, download


def test_sub2_export_requires_explicit_target_instance(isolated_oauth_export):
    write_cpa(
        isolated_oauth_export.cpa_dir / "xai-one@example.com.json",
        refresh_token="refresh-one",
        authorized_at="2026-07-23T01:00:00Z",
        authorization_id="authorization-one",
    )

    response = claim_artifact(
        panel_app.app.test_client(),
        "sub2.json",
        "",
    )

    assert response.status_code == 400
    assert "目标实例" in response.get_json()["error"]


def test_cpa_export_requires_explicit_target_instance(isolated_oauth_export):
    write_cpa(
        isolated_oauth_export.cpa_dir / "xai-one@example.com.json",
        refresh_token="refresh-one",
        authorized_at="2026-07-23T01:00:00Z",
        authorization_id="authorization-one",
    )

    response = claim_artifact(
        panel_app.app.test_client(),
        "cpa.zip",
        "",
    )

    assert response.status_code == 400
    assert "目标实例" in response.get_json()["error"]


def test_cpa_export_claim_blocks_same_refresh_token_on_second_instance(
    isolated_oauth_export,
):
    write_cpa(
        isolated_oauth_export.cpa_dir / "xai-one@example.com.json",
        refresh_token="refresh-one",
        authorized_at="2026-07-23T01:00:00Z",
        authorization_id="authorization-one",
    )
    client = panel_app.app.test_client()

    first_claim, first = claim_and_download(
        client,
        "cpa.zip",
        "cliproxy-primary",
    )
    conflict = claim_artifact(
        client,
        "cpa.zip",
        "cliproxy-secondary",
    )

    assert first_claim.status_code == 200
    assert first.status_code == 200
    assert conflict.status_code == 409
    assert "重新生成账号授权" in conflict.get_json()["error"]


def test_sub2_export_claim_blocks_same_refresh_token_on_second_instance(
    isolated_oauth_export,
):
    write_cpa(
        isolated_oauth_export.cpa_dir / "xai-one@example.com.json",
        refresh_token="refresh-one",
        authorized_at="2026-07-23T01:00:00Z",
        authorization_id="authorization-one",
    )
    client = panel_app.app.test_client()

    _claim, first = claim_and_download(
        client,
        "sub2.json",
        "sub2api-primary",
    )
    conflict = claim_artifact(
        client,
        "sub2.json",
        "sub2api-secondary",
    )

    assert first.status_code == 200
    account = first.get_json()["accounts"][0]
    assert account["extra"]["grok_register_owner_instance"] == (
        "sub2api-primary"
    )
    assert conflict.status_code == 409
    assert "重新生成账号授权" in conflict.get_json()["error"]
    registry = json.loads(
        (
            isolated_oauth_export.cpa_dir / "oauth_ownership.json"
        ).read_text(encoding="utf-8")
    )
    serialized = json.dumps(registry)
    assert "refresh-one" not in serialized
    assert "one@example.com" not in serialized


def test_reauthorized_export_transfer_requires_shutdown_ack(
    isolated_oauth_export,
):
    cpa_path = isolated_oauth_export.cpa_dir / "xai-one@example.com.json"
    write_cpa(
        cpa_path,
        refresh_token="refresh-old",
        authorized_at="2026-07-23T01:00:00Z",
        authorization_id="authorization-old",
    )
    client = panel_app.app.test_client()
    _claim, first = claim_and_download(
        client,
        "sub2.json",
        "sub2api-primary",
    )
    assert first.status_code == 200
    write_cpa(
        cpa_path,
        refresh_token="refresh-new",
        authorized_at="2026-07-23T02:00:00Z",
        authorization_id="authorization-new",
        generation=2,
    )

    blocked = claim_artifact(
        client,
        "sub2.json",
        "sub2api-secondary",
    )
    transfer_claim = claim_artifact(
        client,
        "sub2.json",
        "sub2api-secondary",
        acknowledge=True,
    )
    transferred = client.get(transfer_claim.get_json()["download_url"])

    assert blocked.status_code == 409
    assert "停用" in blocked.get_json()["error"]
    assert transfer_claim.status_code == 200
    assert transferred.status_code == 200


def test_sub2_export_deduplicates_identity_and_keeps_latest_authorization(
    isolated_oauth_export,
):
    write_cpa(
        isolated_oauth_export.cpa_dir / "xai-one-old.json",
        refresh_token="refresh-old",
        authorized_at="2026-07-23T01:00:00Z",
        authorization_id="authorization-old",
    )
    write_cpa(
        isolated_oauth_export.cpa_dir / "xai-one-new.json",
        refresh_token="refresh-new",
        authorized_at="2026-07-23T02:00:00Z",
        authorization_id="authorization-new",
    )

    _claim, response = claim_and_download(
        panel_app.app.test_client(),
        "sub2.json",
        "sub2api-primary",
    )

    assert response.status_code == 200
    accounts = response.get_json()["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["credentials"]["refresh_token"] == "refresh-new"


def test_oauth_export_preflight_reports_only_counts(
    isolated_oauth_export,
):
    write_cpa(
        isolated_oauth_export.cpa_dir / "xai-one@example.com.json",
        refresh_token="refresh-one",
        authorized_at="2026-07-23T01:00:00Z",
        authorization_id="authorization-one",
    )

    response = panel_app.app.test_client().get(
        "/api/oauth/export-preflight?target_instance=sub2api-primary"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 1
    assert payload["unclaimed"] == 1
    assert payload["can_export"] is True
    serialized = response.get_data(as_text=True)
    assert "refresh-one" not in serialized
    assert "one@example.com" not in serialized


def test_download_get_never_claims_ownership_from_query_parameters(
    isolated_oauth_export,
):
    write_cpa(
        isolated_oauth_export.cpa_dir / "xai-one@example.com.json",
        refresh_token="refresh-one",
        authorized_at="2026-07-23T01:00:00Z",
        authorization_id="authorization-one",
    )

    response = panel_app.app.test_client().get(
        "/download/sub2.json"
        "?target_instance=poisoned-by-get"
        "&acknowledge_previous_instance_disabled=1"
    )

    assert response.status_code == 400
    assert "票据" in response.get_json()["error"]
    assert not (
        isolated_oauth_export.cpa_dir / "oauth_ownership.json"
    ).exists()


def test_export_ticket_is_one_time_and_bound_to_requested_artifact(
    isolated_oauth_export,
):
    write_cpa(
        isolated_oauth_export.cpa_dir / "xai-one@example.com.json",
        refresh_token="refresh-one",
        authorized_at="2026-07-23T01:00:00Z",
        authorization_id="authorization-one",
    )
    client = panel_app.app.test_client()
    claim = claim_artifact(client, "sub2.json", "sub2api-primary")

    assert claim.status_code == 200
    ticket = claim.get_json()["download_url"].split("ticket=", 1)[1]
    wrong_artifact = client.get(f"/download/cpa.zip?ticket={ticket}")
    reused = client.get(claim.get_json()["download_url"])

    assert wrong_artifact.status_code == 409
    assert "不匹配" in wrong_artifact.get_json()["error"]
    assert reused.status_code == 409
    assert "无效或已使用" in reused.get_json()["error"]


def test_export_claim_rejects_legacy_cpa_until_reauthorization(
    isolated_oauth_export,
):
    path = isolated_oauth_export.cpa_dir / "xai-one@example.com.json"
    path.write_text(
        json.dumps(
            {
                "email": "one@example.com",
                "sub": "identity-one",
                "sso": "web-sso-one",
                "access_token": "legacy-access",
                "refresh_token": "legacy-refresh",
            }
        ),
        encoding="utf-8",
    )

    response = claim_artifact(
        panel_app.app.test_client(),
        "sub2.json",
        "sub2api-primary",
    )

    assert response.status_code == 409
    assert "重新生成账号授权" in response.get_json()["error"]
    assert not (
        isolated_oauth_export.cpa_dir / "oauth_ownership.json"
    ).exists()
