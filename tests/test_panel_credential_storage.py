from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from panel import app as panel_app


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    config_path = app_root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "credentials_dir": "vault",
                "oauth_target_instance": "test-primary",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(panel_app, "BASE_DIR", app_root)
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.delenv("CPA_DIR", raising=False)
    return app_root, config_path


def _download_oauth_artifact(client, artifact: str):
    claim = client.post(
        "/api/oauth/export-claim",
        json={
            "artifact": artifact,
            "target_instance": "test-primary",
            "acknowledge_previous_instance_disabled": False,
        },
    )
    assert claim.status_code == 200
    return client.get(claim.get_json()["download_url"])


def test_account_listing_reads_current_storage_and_legacy_root(isolated_storage):
    app_root, _ = isolated_storage
    current_sso = app_root / "vault" / "sso"
    current_sso.mkdir(parents=True)
    (current_sso / "accounts_current.txt").write_text(
        "current@example.com----pw----sso-current\n", encoding="utf-8"
    )
    (app_root / "accounts_legacy.txt").write_text(
        "legacy@example.com----pw----sso-legacy\n", encoding="utf-8"
    )

    files = panel_app.list_account_files()

    assert [path.name for path in files] == [
        "accounts_current.txt",
        "accounts_legacy.txt",
    ]
    assert panel_app.safe_name("accounts_current.txt") == (
        current_sso / "accounts_current.txt"
    )
    assert panel_app.safe_name("accounts_legacy.txt") == (
        app_root / "accounts_legacy.txt"
    )


def test_current_account_file_wins_same_name_legacy_collision(isolated_storage):
    app_root, _ = isolated_storage
    current_sso = app_root / "vault" / "sso"
    current_sso.mkdir(parents=True)
    current = current_sso / "accounts_same.txt"
    legacy = app_root / "accounts_same.txt"
    current.write_text("current----pw----sso\n", encoding="utf-8")
    legacy.write_text("legacy----pw----sso\n", encoding="utf-8")

    files = panel_app.list_account_files()

    assert files == [current]
    assert panel_app.safe_name("accounts_same.txt") == current


def test_cpa_paths_follow_config_without_module_restart(isolated_storage):
    app_root, config_path = isolated_storage
    first = panel_app.current_cpa_paths()
    assert first.directory == (app_root / "vault" / "cpa").resolve()

    config_path.write_text(
        json.dumps({"credentials_dir": "second-vault"}), encoding="utf-8"
    )
    second = panel_app.current_cpa_paths()

    assert second.directory == (app_root / "second-vault" / "cpa").resolve()
    assert second.index_path == second.directory / "index.json"
    assert second.failed_path == second.directory / "failed.jsonl"
    assert second.directory.is_dir()


def test_cpa_listing_and_zip_include_current_and_legacy_files(isolated_storage):
    app_root, _ = isolated_storage
    current_cpa = app_root / "vault" / "cpa"
    legacy_cpa = app_root / "data" / "cpa"
    current_cpa.mkdir(parents=True)
    legacy_cpa.mkdir(parents=True)
    current_payload = {
        "email": "current@example.com",
        "sub": "current-identity",
        "sso": "current-sso",
        "access_token": "current-access",
        "refresh_token": "current-refresh",
        "_authorization_id": "current-authorization",
        "_authorization_generation": 1,
        "_authorized_at": "2026-07-23T01:00:00Z",
    }
    legacy_payload = {
        "email": "legacy@example.com",
        "sub": "legacy-identity",
        "sso": "legacy-sso",
        "access_token": "legacy-access",
        "refresh_token": "legacy-refresh",
        "_authorization_id": "legacy-authorization",
        "_authorization_generation": 1,
        "_authorized_at": "2026-07-23T01:00:00Z",
    }
    current_sso = app_root / "vault" / "sso"
    current_sso.mkdir(parents=True)
    (current_sso / "accounts_active.txt").write_text(
        "current@example.com----pw----current-sso\n"
        "legacy@example.com----pw----legacy-sso\n",
        encoding="utf-8",
    )
    (current_cpa / "xai-current.json").write_text(
        json.dumps(current_payload), encoding="utf-8"
    )
    (legacy_cpa / "xai-legacy.json").write_text(
        json.dumps(legacy_payload), encoding="utf-8"
    )

    files = panel_app.list_cpa_files()
    response = _download_oauth_artifact(
        panel_app.app.test_client(), "cpa.zip"
    )

    assert {path.name for path in files} == {
        "xai-current.json",
        "xai-legacy.json",
    }
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert "xai-current.json" in archive.namelist()
        assert "xai-legacy.json" in archive.namelist()


def test_cpa_zip_excludes_credentials_not_owned_by_current_accounts(
    isolated_storage,
):
    app_root, _ = isolated_storage
    sso_dir = app_root / "vault" / "sso"
    cpa_dir = app_root / "vault" / "cpa"
    sso_dir.mkdir(parents=True)
    cpa_dir.mkdir(parents=True)
    (sso_dir / "accounts_active.txt").write_text(
        "active@example.com----pw----active-sso\n", encoding="utf-8"
    )
    (cpa_dir / "xai-active.json").write_text(
        json.dumps(
            {
                "email": "active@example.com",
                "sub": "active-identity",
                "sso": "active-sso",
                "access_token": "active-access",
                "refresh_token": "active-refresh",
                "_authorization_id": "active-authorization",
                "_authorization_generation": 1,
                "_authorized_at": "2026-07-23T01:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (cpa_dir / "xai-orphan.json").write_text(
        json.dumps(
            {
                "email": "old@example.com",
                "sub": "old-identity",
                "sso": "old-sso",
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "_authorization_id": "old-authorization",
                "_authorization_generation": 1,
                "_authorized_at": "2026-07-23T01:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    response = _download_oauth_artifact(
        panel_app.app.test_client(), "cpa.zip"
    )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert "xai-active.json" in archive.namelist()
        assert "xai-orphan.json" not in archive.namelist()


def test_orphan_cpa_is_archived_and_removed_from_index(
    isolated_storage, monkeypatch
):
    app_root, _ = isolated_storage
    sso_dir = app_root / "vault" / "sso"
    cpa_dir = app_root / "vault" / "cpa"
    sso_dir.mkdir(parents=True)
    cpa_dir.mkdir(parents=True)
    (sso_dir / "accounts_active.txt").write_text(
        "active@example.com----pw----active-sso\n", encoding="utf-8"
    )
    active = cpa_dir / "xai-active.json"
    orphan = cpa_dir / "xai-orphan.json"
    malformed = cpa_dir / "xai-malformed.json"
    active.write_text(
        json.dumps({"email": "active@example.com", "sso": "active-sso"}),
        encoding="utf-8",
    )
    orphan.write_text(
        json.dumps({"email": "old@example.com", "sso": "old-sso"}),
        encoding="utf-8",
    )
    malformed.write_text("not-json", encoding="utf-8")
    index = cpa_dir / "index.json"
    index.write_text(
        json.dumps(
            {
                "items": {
                    panel_app.sso_fingerprint("active-sso"): {
                        "file": active.name
                    },
                    panel_app.sso_fingerprint("old-sso"): {
                        "file": orphan.name
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(panel_app, "_cpa_done", set())

    result = panel_app.archive_orphan_cpa(
        reason="account-delete", timestamp="20260722_120000"
    )

    assert result.archived == 2
    assert active.exists()
    assert not orphan.exists()
    assert not malformed.exists()
    assert (result.archive_dir / "cpa" / orphan.name).is_file()
    assert (result.archive_dir / "cpa" / malformed.name).is_file()
    saved_index = json.loads(index.read_text(encoding="utf-8"))
    assert list(saved_index["items"].values()) == [{"file": active.name}]


def test_deleting_account_file_archives_its_orphan_cpa(isolated_storage):
    app_root, _ = isolated_storage
    sso_dir = app_root / "vault" / "sso"
    cpa_dir = app_root / "vault" / "cpa"
    sso_dir.mkdir(parents=True)
    cpa_dir.mkdir(parents=True)
    account = sso_dir / "accounts_delete.txt"
    account.write_text(
        "delete@example.com----pw----delete-sso\n", encoding="utf-8"
    )
    cpa = cpa_dir / "xai-delete.json"
    cpa.write_text(
        json.dumps({"email": "delete@example.com", "sso": "delete-sso"}),
        encoding="utf-8",
    )

    response = panel_app.app.test_client().post(
        "/api/accounts/delete", json={"files": [account.name]}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["cpa_archived"] == 1
    assert not cpa.exists()
    archive_dir = app_root / "vault" / "archive"
    assert any(path.name == cpa.name for path in archive_dir.rglob("*.json"))


def test_cpa_index_writes_only_to_current_configured_directory(isolated_storage):
    app_root, _ = isolated_storage

    panel_app.save_cpa_index_item("fingerprint", {"file": "xai-test.json"})

    current_index = app_root / "vault" / "cpa" / "index.json"
    legacy_index = app_root / "data" / "cpa" / "index.json"
    assert current_index.is_file()
    assert not legacy_index.exists()
    payload = json.loads(current_index.read_text(encoding="utf-8"))
    assert payload["items"]["fingerprint"]["file"] == "xai-test.json"


def test_cpa_environment_override_remains_explicit_compatibility(
    isolated_storage, tmp_path, monkeypatch
):
    override = tmp_path / "external-cpa"
    monkeypatch.setenv("CPA_DIR", str(override))

    paths = panel_app.current_cpa_paths()

    assert paths.directory == override.resolve()
    assert panel_app.cpa_stats()["dir"] == str(override.resolve())


def test_credentials_status_reports_paths_and_counts_without_contents(
    isolated_storage,
):
    app_root, _ = isolated_storage
    _write = lambda path, content: (
        path.parent.mkdir(parents=True, exist_ok=True),
        path.write_text(content, encoding="utf-8"),
    )
    _write(app_root / "vault" / "sso" / "accounts_one.txt", "private-sso")
    _write(
        app_root / "vault" / "mail" / "mail_credentials_one.txt",
        "private-jwt",
    )
    _write(app_root / "vault" / "cpa" / "xai-one.json", "private-cpa")

    response = panel_app.app.test_client().get("/api/config/credentials")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["configured"] == "vault"
    assert payload["resolved_path"] == str((app_root / "vault").resolve())
    assert payload["stats"] == {
        "sso_files": 1,
        "mail_files": 1,
        "cpa_files": 1,
        "disabled_files": 0,
        "total_files": 3,
        "total_bytes": 33,
    }
    serialized = json.dumps(payload)
    assert "private-sso" not in serialized
    assert "private-jwt" not in serialized
    assert "private-cpa" not in serialized


def test_credentials_status_excludes_oauth_lock_from_counts_and_cpa_total(
    isolated_storage,
):
    app_root, _ = isolated_storage
    cpa_dir = app_root / "vault" / "cpa"
    cpa_dir.mkdir(parents=True, exist_ok=True)
    (cpa_dir / "xai-one.json").write_text("credential", encoding="utf-8")
    (cpa_dir / "oauth_ownership.json").write_text(
        "ownership", encoding="utf-8"
    )
    (cpa_dir / ".oauth_ownership.json.lock").write_bytes(b"\0")

    response = panel_app.app.test_client().get("/api/config/credentials")

    assert response.status_code == 200
    stats = response.get_json()["stats"]
    assert stats["cpa_files"] == 1
    assert stats["total_files"] == 2
    assert stats["total_bytes"] == len("credentialownership")


def test_save_empty_credentials_directory_updates_config_atomically(
    isolated_storage,
):
    app_root, config_path = isolated_storage
    response = panel_app.app.test_client().post(
        "/api/config/credentials", json={"credentials_dir": "empty-vault"}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["configured"] == "empty-vault"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["credentials_dir"] == "empty-vault"
    assert (app_root / "empty-vault" / "sso").is_dir()
    assert not list(app_root.glob(".*config*.tmp"))


def test_failed_credentials_config_save_preserves_workspace_epochs(
    isolated_storage,
    monkeypatch,
):
    app_root, config_path = isolated_storage
    source_lock_path = (
        app_root / "vault" / "cpa" / ".oauth_ownership.json.lock"
    )
    target_lock_path = (
        app_root / "empty-vault" / "cpa" / ".oauth_ownership.json.lock"
    )
    epochs_before = (
        panel_app.interprocess_lock_epoch(source_lock_path),
        panel_app.interprocess_lock_epoch(target_lock_path),
    )

    def fail_save(_config):
        raise OSError("simulated atomic save failure")

    monkeypatch.setattr(panel_app, "save_config_atomic", fail_save)

    response = panel_app.app.test_client().post(
        "/api/config/credentials",
        json={"credentials_dir": "empty-vault"},
    )

    assert response.status_code == 400
    assert json.loads(config_path.read_text(encoding="utf-8"))[
        "credentials_dir"
    ] == "vault"
    assert (
        panel_app.interprocess_lock_epoch(source_lock_path),
        panel_app.interprocess_lock_epoch(target_lock_path),
    ) == epochs_before


def test_save_empty_directory_rejects_other_process_target_workspace_owner(
    isolated_storage,
):
    app_root, config_path = isolated_storage
    target_cpa = app_root / "empty-vault" / "cpa"
    external_lock = panel_app.InterProcessFileLock(
        target_cpa / ".oauth_ownership.json.lock"
    )
    assert external_lock.acquire(blocking=False)
    try:
        response = panel_app.app.test_client().post(
            "/api/config/credentials",
            json={"credentials_dir": "empty-vault"},
        )
    finally:
        external_lock.release()

    assert response.status_code == 409
    assert "另一个程序实例" in response.get_json()["error"]
    assert json.loads(config_path.read_text(encoding="utf-8"))[
        "credentials_dir"
    ] == "vault"


def test_save_directory_requires_migration_when_source_has_credentials(
    isolated_storage,
):
    app_root, _ = isolated_storage
    source = app_root / "vault" / "sso" / "accounts_existing.txt"
    source.parent.mkdir(parents=True)
    source.write_text("private", encoding="utf-8")

    response = panel_app.app.test_client().post(
        "/api/config/credentials", json={"credentials_dir": "new-vault"}
    )

    assert response.status_code == 409
    assert "迁移" in response.get_json()["error"]
    assert source.exists()


def test_save_rejects_nonempty_target_directory(isolated_storage):
    app_root, _ = isolated_storage
    target = app_root / "occupied"
    target.mkdir()
    (target / "unrelated.txt").write_text("keep", encoding="utf-8")

    response = panel_app.app.test_client().post(
        "/api/config/credentials", json={"credentials_dir": "occupied"}
    )

    assert response.status_code == 400
    assert "非空" in response.get_json()["error"]


@pytest.mark.parametrize(
    "endpoint",
    ["/api/config/credentials", "/api/config/credentials/migrate"],
)
def test_credentials_changes_are_rejected_while_registration_runs(
    isolated_storage, monkeypatch, endpoint
):
    monkeypatch.setitem(panel_app._job, "running", True)

    response = panel_app.app.test_client().post(
        endpoint, json={"credentials_dir": "new-vault"}
    )

    assert response.status_code == 409
    assert "注册任务运行中" in response.get_json()["error"]


def test_credential_migration_is_rejected_while_cpa_conversion_is_pending(
    isolated_storage, monkeypatch
):
    monkeypatch.setitem(panel_app._cpa_state, "pending", 1)

    response = panel_app.app.test_client().post(
        "/api/config/credentials/migrate",
        json={"credentials_dir": "new-vault"},
    )

    assert response.status_code == 409
    assert "CPA" in response.get_json()["error"]


def test_credential_migration_rejects_active_cpa_directory_override(
    isolated_storage, tmp_path, monkeypatch
):
    monkeypatch.setenv("CPA_DIR", str(tmp_path / "override"))

    response = panel_app.app.test_client().post(
        "/api/config/credentials/migrate",
        json={"credentials_dir": "new-vault"},
    )

    assert response.status_code == 409
    assert "CPA_DIR" in response.get_json()["error"]


def test_manual_migration_moves_all_legacy_credentials_and_switches_config(
    isolated_storage,
    monkeypatch,
):
    app_root, config_path = isolated_storage
    invalidations = []
    monkeypatch.setattr(
        panel_app,
        "invalidate_account_catalog",
        lambda: invalidations.append(True),
    )
    (app_root / "accounts_legacy.txt").write_text(
        "legacy@example.com----pw----private-sso", encoding="utf-8"
    )
    (app_root / "mail_credentials.txt").write_text(
        "legacy@example.com\tprivate-jwt", encoding="utf-8"
    )
    legacy_cpa = app_root / "data" / "cpa"
    legacy_cpa.mkdir(parents=True)
    (legacy_cpa / "xai-legacy.json").write_text(
        json.dumps({"email": "legacy@example.com", "sso": "private-sso"}),
        encoding="utf-8",
    )

    response = panel_app.app.test_client().post(
        "/api/config/credentials/migrate",
        json={"credentials_dir": "migrated-vault"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["migration"]["copied"] == 3
    assert payload["migration"]["removed"] == 3
    assert payload["migration"]["warnings"] == []
    assert json.loads(config_path.read_text(encoding="utf-8"))[
        "credentials_dir"
    ] == "migrated-vault"
    assert (app_root / "migrated-vault" / "sso" / "accounts_legacy.txt").is_file()
    assert (
        app_root / "migrated-vault" / "mail" / "mail_credentials.txt"
    ).is_file()
    assert (app_root / "migrated-vault" / "cpa" / "xai-legacy.json").is_file()
    assert not (app_root / "accounts_legacy.txt").exists()
    serialized = json.dumps(payload)
    assert "private-sso" not in serialized
    assert "private-jwt" not in serialized
    assert invalidations == [True]


def test_credential_routes_are_registered():
    rules = {rule.rule for rule in panel_app.app.url_map.iter_rules()}
    assert "/api/config/credentials" in rules
    assert "/api/config/credentials/migrate" in rules
