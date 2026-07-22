from __future__ import annotations

import io
import json
import queue
import threading
from pathlib import Path

import pytest

from panel import app as panel_app


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    config_path = app_root / "config.json"
    config_path.write_text(
        json.dumps({"credentials_dir": "vault"}), encoding="utf-8"
    )
    monkeypatch.setattr(panel_app, "BASE_DIR", app_root)
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.delenv("CPA_DIR", raising=False)
    monkeypatch.setattr(panel_app, "_credential_import_lock", threading.RLock())
    monkeypatch.setattr(panel_app, "_credential_migration_lock", threading.Lock())
    monkeypatch.setattr(panel_app, "_activity_lock", threading.Lock())
    monkeypatch.setattr(panel_app, "_cpa_q", queue.Queue())
    monkeypatch.setattr(panel_app, "_cpa_done", set())
    monkeypatch.setattr(panel_app, "_cpa_inflight", set())
    monkeypatch.setattr(
        panel_app,
        "_cpa_state",
        {
            "enabled": True,
            "core_ok": True,
            "core_error": "",
            "pending": 0,
            "ok": 0,
            "fail": 0,
            "running": False,
            "active": False,
            "last_error": "",
            "last_ok_email": "",
        },
    )
    monkeypatch.setitem(panel_app._job, "running", False)
    monkeypatch.setattr(panel_app, "email_receive_test_is_running", lambda: False)
    panel_app._logs.clear()
    return app_root


def _upload(client, content: bytes, filename: str = "batch.txt"):
    return client.post(
        "/api/credentials/import",
        data={"file": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


def test_upload_batch_stages_archives_and_queues_without_echoing_sso(
    isolated_workspace, monkeypatch
):
    app_root = isolated_workspace
    sso_dir = app_root / "vault" / "sso"
    cpa_dir = app_root / "vault" / "cpa"
    sso_dir.mkdir(parents=True)
    cpa_dir.mkdir(parents=True)
    old_account = sso_dir / "accounts_old.txt"
    old_cpa = cpa_dir / "xai-old.json"
    old_index = cpa_dir / "index.json"
    old_account.write_text(
        "old@example.com----old-password----old-sso\n", encoding="utf-8"
    )
    old_cpa.write_text(
        json.dumps({"email": "old@example.com", "sso": "old-sso"}),
        encoding="utf-8",
    )
    old_index.write_text(json.dumps({"items": {}}), encoding="utf-8")
    queued = []

    def capture_enqueue(**record):
        queued.append(record)
        return True, "queued"

    monkeypatch.setattr(panel_app, "enqueue_cpa_convert", capture_enqueue)
    invalidations = []
    monkeypatch.setattr(
        panel_app,
        "invalidate_account_catalog",
        lambda: invalidations.append(True),
    )
    secret = "sso-super-secret"
    password = "password-super-secret"

    response = _upload(
        panel_app.app.test_client(),
        (
            f"new@example.com----{password}----{secret}\n"
            f"new@example.com----ignored----{secret}\n"
        ).encode(),
        "..\\..\\unsafe batch.txt",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload == {
        "ok": True,
        "batch_id": payload["batch_id"],
        "source_name": "unsafe_batch.txt",
        "parsed": 1,
        "queued": 1,
        "skipped": 0,
        "archived": 3,
        "drained": 0,
    }
    assert len(payload["batch_id"]) == 32
    serialized = response.get_data(as_text=True)
    assert secret not in serialized
    assert password not in serialized
    assert secret not in "\n".join(panel_app._logs)
    assert password not in "\n".join(panel_app._logs)
    assert queued == [
        {
            "email": "new@example.com",
            "sso": secret,
            "password": password,
            "source": "unsafe_batch.txt",
            "force": True,
        }
    ]
    live_files = list(sso_dir.glob("accounts_import_*.txt"))
    assert len(live_files) == 1
    assert live_files[0].read_text(encoding="utf-8") == (
        f"new@example.com----{password}----{secret}\n"
    )
    assert not old_account.exists()
    assert not old_cpa.exists()
    assert not old_index.exists()
    archive = app_root / "vault" / "archive"
    assert any(path.name == old_account.name for path in archive.rglob("*.txt"))
    assert any(path.name == old_cpa.name for path in archive.rglob("*.json"))
    assert any(path.name == old_index.name for path in archive.rglob("*.json"))
    assert not list((app_root / "vault").glob(".staging-*"))
    assert invalidations == [True]


def test_upload_json_preserves_password_and_deduplicates_sso(
    isolated_workspace, monkeypatch
):
    queued = []
    monkeypatch.setattr(
        panel_app,
        "enqueue_cpa_convert",
        lambda **record: (queued.append(record) is None, "queued"),
    )
    body = json.dumps(
        [
            {"email": "one@example.com", "password": "pw-one", "sso": "sso-one"},
            {"email": "duplicate@example.com", "password": "ignored", "sso": "sso-one"},
            {"email": "two@example.com", "password": "pw-two", "token": "sso-two"},
        ]
    ).encode()

    response = _upload(panel_app.app.test_client(), body, "accounts.json")

    assert response.status_code == 200
    assert response.get_json()["parsed"] == 2
    assert [item["password"] for item in queued] == ["pw-one", "pw-two"]
    live = next(
        (isolated_workspace / "vault" / "sso").glob("accounts_import_*.txt")
    )
    assert live.read_text(encoding="utf-8").splitlines() == [
        "one@example.com----pw-one----sso-one",
        "two@example.com----pw-two----sso-two",
    ]


@pytest.mark.parametrize(
    ("content", "filename", "message"),
    [
        (b"", "empty.txt", "没有可导入"),
        (b'[{"email":"missing@example.com"}]', "missing.json", "没有可导入"),
        (b"sso-value", "accounts.csv", "TXT 或 JSON"),
    ],
)
def test_upload_rejects_empty_missing_sso_and_unsupported_extension(
    isolated_workspace, content, filename, message
):
    response = _upload(panel_app.app.test_client(), content, filename)

    assert response.status_code == 400
    assert message in response.get_json()["error"]
    assert not list((isolated_workspace / "vault" / "sso").glob("accounts_*.txt"))


def test_upload_enforces_30_mib_limit(isolated_workspace, monkeypatch):
    assert panel_app.MAX_CREDENTIAL_IMPORT_BYTES == 30 * 1024 * 1024
    monkeypatch.setattr(panel_app, "MAX_CREDENTIAL_IMPORT_BYTES", 16)

    response = _upload(panel_app.app.test_client(), b"x" * 17)

    assert response.status_code == 413
    assert "30 MiB" in response.get_json()["error"]


@pytest.mark.parametrize("busy_kind", ["registration", "email", "cpa"])
def test_upload_busy_states_return_409_without_changing_live_workspace(
    isolated_workspace, monkeypatch, busy_kind
):
    old = isolated_workspace / "vault" / "sso" / "accounts_old.txt"
    old.parent.mkdir(parents=True)
    old.write_text("old@example.com----pw----old-sso\n", encoding="utf-8")
    if busy_kind == "registration":
        monkeypatch.setitem(panel_app._job, "running", True)
    elif busy_kind == "email":
        monkeypatch.setattr(panel_app, "email_receive_test_is_running", lambda: True)
    else:
        monkeypatch.setitem(panel_app._cpa_state, "active", True)

    response = _upload(
        panel_app.app.test_client(),
        b"new@example.com----pw----new-sso",
    )

    assert response.status_code == 409
    assert old.read_text(encoding="utf-8").startswith("old@example.com")
    assert not list(old.parent.glob("accounts_import_*.txt"))
    assert not list((isolated_workspace / "vault").glob(".staging-*"))


def test_upload_rejects_concurrent_migration_without_mutation(
    isolated_workspace,
):
    old = isolated_workspace / "vault" / "sso" / "accounts_old.txt"
    old.parent.mkdir(parents=True)
    old.write_text("old@example.com----pw----old-sso\n", encoding="utf-8")
    assert panel_app._credential_migration_lock.acquire(blocking=False)
    try:
        response = _upload(
            panel_app.app.test_client(),
            b"new@example.com----pw----new-sso",
        )
    finally:
        panel_app._credential_migration_lock.release()

    assert response.status_code == 409
    assert old.exists()
    assert not list((isolated_workspace / "vault").glob(".staging-*"))


def test_upload_rejects_another_import_without_reading_or_mutating_workspace(
    isolated_workspace, monkeypatch
):
    class BusyImportLock:
        def acquire(self, blocking=True):
            return False

    old = isolated_workspace / "vault" / "sso" / "accounts_old.txt"
    old.parent.mkdir(parents=True)
    old.write_text("old@example.com----pw----old-sso\n", encoding="utf-8")
    monkeypatch.setattr(panel_app, "_credential_import_lock", BusyImportLock())

    response = _upload(
        panel_app.app.test_client(),
        b"new@example.com----pw----new-sso",
    )

    assert response.status_code == 409
    assert "导入" in response.get_json()["error"]
    assert old.exists()
    assert not list((isolated_workspace / "vault").glob(".staging-*"))


def test_staging_failure_keeps_existing_workspace_and_redacts_secret(
    isolated_workspace, monkeypatch
):
    old = isolated_workspace / "vault" / "sso" / "accounts_old.txt"
    old.parent.mkdir(parents=True)
    old.write_text("old@example.com----pw----old-sso\n", encoding="utf-8")
    secret = "must-not-leak"

    def fail_staging(*_args, **_kwargs):
        raise OSError(f"cannot write {secret}")

    monkeypatch.setattr(panel_app, "_write_import_staging", fail_staging)

    response = _upload(
        panel_app.app.test_client(),
        f"new@example.com----pw----{secret}".encode(),
    )

    assert response.status_code == 500
    assert old.exists()
    assert secret not in response.get_data(as_text=True)
    assert secret not in "\n".join(panel_app._logs)


def test_activation_failure_rolls_archived_files_back(
    isolated_workspace, monkeypatch
):
    layout = panel_app.current_credential_layout()
    old_account = layout.sso_dir / "accounts_old.txt"
    old_cpa = layout.cpa_dir / "xai-old.json"
    old_account.write_text("old@example.com----pw----old-sso\n", encoding="utf-8")
    old_cpa.write_text("{}", encoding="utf-8")
    records = [
        {"email": "new@example.com", "password": "pw", "sso": "new-sso"}
    ]
    batch_id = "a" * 32
    staged = panel_app._write_import_staging(layout, records, batch_id)

    def fail_replace(_source: Path, _destination: Path):
        raise OSError("activation failed")

    with pytest.raises(panel_app.CredentialImportError):
        panel_app.activate_credential_import(
            layout,
            staged,
            layout.sso_dir / "accounts_import_failed.txt",
            [("sso", old_account), ("cpa", old_cpa)],
            batch_id=batch_id,
            timestamp="20260722_120000",
            replace_file=fail_replace,
        )

    assert old_account.exists()
    assert old_cpa.exists()
    assert staged.exists()
    assert not (layout.sso_dir / "accounts_import_failed.txt").exists()


def test_pending_cpa_jobs_are_drained_before_batch_activation(
    isolated_workspace, monkeypatch
):
    panel_app._cpa_inflight.add(panel_app.sso_fingerprint("old-queued-sso"))
    panel_app._cpa_state["pending"] = 1
    panel_app._cpa_state["running"] = True
    panel_app._cpa_q.put(
        {
            "email": "old@example.com",
            "sso": "old-queued-sso",
            "password": "",
            "source": "old",
            "fp": panel_app.sso_fingerprint("old-queued-sso"),
            "force": True,
        }
    )
    monkeypatch.setattr(panel_app, "enqueue_cpa_convert", lambda **_record: (True, "queued"))

    response = _upload(
        panel_app.app.test_client(),
        b"new@example.com----pw----new-sso",
    )

    assert response.status_code == 200
    assert response.get_json()["drained"] == 1
    assert panel_app._cpa_q.empty()
    assert not panel_app._cpa_inflight


def test_credential_import_route_is_registered():
    rules = {rule.rule for rule in panel_app.app.url_map.iter_rules()}
    assert "/api/credentials/import" in rules
