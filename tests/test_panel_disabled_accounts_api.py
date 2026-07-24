from __future__ import annotations

import json
import queue
import threading

import pytest

from panel import app as panel_app


EMAIL = "restore@example.com"
PASSWORD = "restore-password-secret"
SSO = "restore-web-sso-secret"


@pytest.fixture
def isolated_disabled_api(tmp_path, monkeypatch):
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
    monkeypatch.setattr(panel_app, "AUTO_CPA", True)
    monkeypatch.setattr(panel_app, "_CPA_CORE_OK", True)
    monkeypatch.setattr(panel_app, "_credential_import_lock", threading.RLock())
    monkeypatch.setattr(panel_app, "_credential_migration_lock", threading.Lock())
    monkeypatch.setattr(panel_app, "_activity_lock", threading.Lock())
    monkeypatch.setattr(panel_app, "_cpa_lock", threading.Lock())
    monkeypatch.setattr(panel_app, "_cpa_q", queue.Queue())
    monkeypatch.setattr(panel_app, "_cpa_result_q", queue.Queue())
    monkeypatch.setattr(panel_app, "_cpa_done", set())
    monkeypatch.setattr(panel_app, "_cpa_inflight", set())
    monkeypatch.setattr(panel_app, "_cpa_workspace_generation", 5)
    monkeypatch.setitem(panel_app._job, "running", False)
    monkeypatch.setattr(
        panel_app,
        "_cpa_state",
        {
            "enabled": True,
            "core_ok": True,
            "pending": 0,
            "active_workers": 0,
            "commit_pending": 0,
            "commit_active": 0,
            "running": False,
            "active": False,
            "ok": 0,
            "fail": 0,
        },
    )
    layout = panel_app.current_credential_layout()
    account_file = layout.sso_dir / "accounts_restore.txt"
    account_file.write_text(
        f"{EMAIL}----{PASSWORD}----{SSO}\n",
        encoding="utf-8",
    )
    cpa_path = layout.cpa_dir / f"xai-{EMAIL}.json"
    cpa_path.write_text(
        json.dumps(
            {
                "email": EMAIL,
                "sso": SSO,
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "disabled": True,
                "_disabled_reason": "access_denied",
            }
        ),
        encoding="utf-8",
    )
    record = panel_app.current_disabled_account_pool().disable(
        {
            "email": EMAIL,
            "password": PASSWORD,
            "sso": SSO,
            "source": account_file.name,
            "raw": f"{EMAIL}----{PASSWORD}----{SSO}",
        },
        "consent failed: Access denied",
    )
    panel_app.invalidate_account_catalog()
    return {
        "layout": layout,
        "record": record,
        "cpa_path": cpa_path,
    }


def test_disabled_accounts_api_is_paginated_and_never_exposes_secrets(
    isolated_disabled_api,
):
    response = panel_app.app.test_client().get(
        "/api/disabled-accounts?page=1&page_size=25"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["pagination"] == {
        "page": 1,
        "page_size": 25,
        "total": 1,
        "total_pages": 1,
    }
    assert payload["items"][0]["email"] == EMAIL
    assert payload["items"][0]["reason"] == "access_denied"
    body = response.get_data(as_text=True)
    for secret in (PASSWORD, SSO, "old-access", "old-refresh", "raw", "error"):
        assert secret not in body


def test_restore_removes_quarantine_queues_reauthorization_and_keeps_old_cpa_disabled(
    isolated_disabled_api,
    monkeypatch,
):
    queued = []
    fingerprint = panel_app.sso_fingerprint(SSO)
    panel_app._cpa_done.add(fingerprint)
    monkeypatch.setattr(
        panel_app,
        "enqueue_cpa_convert",
        lambda **kwargs: (
            queued.append(kwargs) is None,
            "queued",
        ),
    )
    record_id = isolated_disabled_api["record"]["id"]

    response = panel_app.app.test_client().post(
        f"/api/disabled-accounts/{record_id}/restore",
        json={},
    )

    assert response.status_code == 200
    assert response.get_json()["queued"] is True
    assert queued == [
        {
            "email": EMAIL,
            "password": PASSWORD,
            "sso": SSO,
            "source": "accounts_restore.txt",
            "force": True,
        }
    ]
    assert panel_app.current_disabled_account_pool().list_public() == []
    assert fingerprint not in panel_app._cpa_done
    old_cpa = json.loads(
        isolated_disabled_api["cpa_path"].read_text(encoding="utf-8")
    )
    assert old_cpa["disabled"] is True
    body = response.get_data(as_text=True)
    assert PASSWORD not in body
    assert SSO not in body


def test_restore_rolls_back_quarantine_when_reauthorization_cannot_queue(
    isolated_disabled_api,
    monkeypatch,
):
    monkeypatch.setattr(
        panel_app,
        "enqueue_cpa_convert",
        lambda **_kwargs: (False, "pipeline unavailable"),
    )
    record_id = isolated_disabled_api["record"]["id"]

    response = panel_app.app.test_client().post(
        f"/api/disabled-accounts/{record_id}/restore",
        json={},
    )

    assert response.status_code == 409
    assert "pipeline unavailable" in response.get_json()["error"]
    restored_pool = panel_app.current_disabled_account_pool().list_public()
    assert [item["id"] for item in restored_pool] == [record_id]


def test_restore_unknown_disabled_account_returns_404(isolated_disabled_api):
    response = panel_app.app.test_client().post(
        "/api/disabled-accounts/not-found/restore",
        json={},
    )

    assert response.status_code == 404
    assert response.get_json()["ok"] is False


def test_restore_requires_json_post(isolated_disabled_api):
    record_id = isolated_disabled_api["record"]["id"]

    response = panel_app.app.test_client().post(
        f"/api/disabled-accounts/{record_id}/restore"
    )

    assert response.status_code == 400
    assert panel_app.current_disabled_account_pool().matches(email=EMAIL)
