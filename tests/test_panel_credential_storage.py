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
        json.dumps({"credentials_dir": "vault"}), encoding="utf-8"
    )
    monkeypatch.setattr(panel_app, "BASE_DIR", app_root)
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.delenv("CPA_DIR", raising=False)
    return app_root, config_path


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
    current_payload = {"email": "current@example.com", "sso": "current-sso"}
    legacy_payload = {"email": "legacy@example.com", "sso": "legacy-sso"}
    (current_cpa / "xai-current.json").write_text(
        json.dumps(current_payload), encoding="utf-8"
    )
    (legacy_cpa / "xai-legacy.json").write_text(
        json.dumps(legacy_payload), encoding="utf-8"
    )

    files = panel_app.list_cpa_files()
    response = panel_app.app.test_client().get("/download/cpa.zip")

    assert {path.name for path in files} == {
        "xai-current.json",
        "xai-legacy.json",
    }
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert "xai-current.json" in archive.namelist()
        assert "xai-legacy.json" in archive.namelist()


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
