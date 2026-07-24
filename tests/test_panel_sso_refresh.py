from __future__ import annotations

import json
import queue
import threading

import pytest

from panel import app as panel_app


@pytest.fixture
def isolated_refresh_state(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"credentials_dir": "vault"}), encoding="utf-8"
    )
    monkeypatch.setattr(panel_app, "BASE_DIR", tmp_path)
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.delenv(panel_app.CPA_DIR_ENV, raising=False)
    monkeypatch.setattr(panel_app, "_credential_import_lock", threading.RLock())
    monkeypatch.setattr(panel_app, "_credential_migration_lock", threading.Lock())
    monkeypatch.setattr(panel_app, "_activity_lock", threading.Lock())
    monkeypatch.setattr(panel_app, "_cpa_lock", threading.Lock())
    monkeypatch.setattr(panel_app, "_cpa_q", queue.Queue())
    monkeypatch.setattr(panel_app, "_cpa_result_q", queue.Queue())
    monkeypatch.setattr(panel_app, "_cpa_done", set())
    monkeypatch.setattr(panel_app, "_cpa_inflight", set())
    monkeypatch.setattr(panel_app, "_cpa_workspace_generation", 7)
    monkeypatch.setattr(panel_app, "_CPA_CORE_OK", True)
    monkeypatch.setattr(panel_app, "convert_one", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(panel_app, "CPA_DELAY", 0)
    monkeypatch.setattr(panel_app, "PANEL_AUTH", False)
    monkeypatch.setitem(panel_app._job, "running", False)
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
    panel_app._logs.clear()
    return tmp_path


def _run_one_worker_item(*, email: str, sso: str, password: str = ""):
    fingerprint = panel_app.sso_fingerprint(sso)
    cpa_directory = panel_app._cpa_workspace_directory(
        panel_app.current_cpa_paths().directory
    )
    panel_app._cpa_inflight.add(fingerprint)
    panel_app._cpa_q.put(
        {
            "email": email,
            "sso": sso,
            "password": password,
            "source": "manual-refresh",
            "fp": fingerprint,
            "force": True,
            "workspace_generation": panel_app._cpa_workspace_generation,
            "workspace_directory": str(cpa_directory),
            "workspace_epoch": panel_app.interprocess_lock_epoch(
                panel_app._cpa_workspace_lock_path(cpa_directory)
            ),
        }
    )
    panel_app._cpa_q.put(None)
    panel_app._cpa_worker_loop()


def test_force_refresh_bypasses_completed_but_never_duplicates_inflight(
    isolated_refresh_state,
):
    sso = "existing-web-sso"
    fingerprint = panel_app.sso_fingerprint(sso)
    panel_app._cpa_done.add(fingerprint)

    first = panel_app.enqueue_cpa_convert(
        email="one@example.com",
        sso=sso,
        source="manual-refresh",
        force=True,
    )
    second = panel_app.enqueue_cpa_convert(
        email="one@example.com",
        sso=sso,
        source="manual-refresh",
        force=True,
    )

    assert first == (True, "queued")
    assert second == (False, "already queued")
    assert panel_app._cpa_q.qsize() == 1
    assert panel_app._cpa_state["pending"] == 1


def test_enqueue_all_sso_refresh_queues_every_available_sso_with_force(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {
                "email": "one@example.com",
                "password": "pw-one",
                "sso": "sso-one",
                "source": "accounts_one.txt",
            },
            {
                "email": "missing@example.com",
                "password": "pw-missing",
                "sso": "",
                "source": "accounts_missing.txt",
            },
            {
                "email": "two@example.com",
                "password": "pw-two",
                "sso": "sso-two",
                "source": "accounts_two.txt",
            },
        ],
    )
    queued = []

    def capture_enqueue(**record):
        queued.append(record)
        return True, "queued"

    monkeypatch.setattr(panel_app, "enqueue_cpa_convert", capture_enqueue)

    count = panel_app.enqueue_all_sso_refresh(limit=10000)

    assert count == 2
    assert queued == [
        {
            "email": "one@example.com",
            "sso": "sso-one",
            "password": "pw-one",
            "source": "accounts_one.txt",
            "force": True,
        },
        {
            "email": "two@example.com",
            "sso": "sso-two",
            "password": "pw-two",
            "source": "accounts_two.txt",
            "force": True,
        },
    ]


def test_enqueue_all_sso_refresh_honors_requested_limit(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {"email": "one@example.com", "sso": "sso-one"},
            {"email": "two@example.com", "sso": "sso-two"},
        ],
    )
    queued = []
    monkeypatch.setattr(
        panel_app,
        "enqueue_cpa_convert",
        lambda **record: (queued.append(record) is None, "queued"),
    )

    assert panel_app.enqueue_all_sso_refresh(limit=1) == 1
    assert len(queued) == 1


def test_enqueue_all_sso_refresh_deduplicates_same_identity(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {
                "email": "same@example.com",
                "password": "pw-old",
                "sso": "sso-old",
                "source": "accounts_old.txt",
            },
            {
                "email": "SAME@example.com",
                "password": "pw-new",
                "sso": "sso-new",
                "source": "accounts_new.txt",
            },
        ],
    )
    queued = []
    monkeypatch.setattr(
        panel_app,
        "enqueue_cpa_convert",
        lambda **record: (queued.append(record) is None, "queued"),
    )

    assert panel_app.enqueue_all_sso_refresh(limit=10000) == 1
    assert len(queued) == 1
    assert queued[0]["email"].casefold() == "same@example.com"


def test_reauthorization_identity_prefers_stable_session_subject(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "decode_sso_meta",
        lambda _sso: {"sub": "stable-session-subject"},
    )
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {"email": "old@example.com", "sso": "sso-old"},
            {"email": "new@example.com", "sso": "sso-new"},
        ],
    )

    candidates, duplicates = panel_app._reauthorization_candidates(10000)

    assert len(candidates) == 1
    assert duplicates == 1


def test_reauthorization_identity_ignores_rotating_session_ids_for_same_email(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "decode_sso_meta",
        lambda sso: {
            "session_id": f"session-{sso}",
            "sid": f"sid-{sso}",
        },
    )
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {"email": "same@example.com", "sso": "sso-old"},
            {"email": "SAME@example.com", "sso": "sso-new"},
        ],
    )

    candidates, duplicates = panel_app._reauthorization_candidates(10000)

    assert len(candidates) == 1
    assert duplicates == 1


def test_refresh_worker_failure_preserves_existing_cpa_byte_for_byte(
    isolated_refresh_state, monkeypatch
):
    paths = panel_app.current_cpa_paths()
    existing = paths.directory / "xai-one@example.com.json"
    canary = b'{"email":"one@example.com","access_token":"old-canary"}\n'
    existing.write_bytes(canary)
    monkeypatch.setattr(
        panel_app,
        "convert_one",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("refresh exchange failed")
        ),
    )

    _run_one_worker_item(email="one@example.com", sso="existing-web-sso")

    assert existing.read_bytes() == canary
    assert panel_app._cpa_state["fail"] == 1
    assert "refresh exchange failed" in panel_app._cpa_state["last_error"]
    assert not list(paths.directory.glob(".*.tmp"))


def test_refresh_worker_atomically_replaces_existing_cpa_on_success(
    isolated_refresh_state, monkeypatch
):
    paths = panel_app.current_cpa_paths()
    existing = paths.directory / "xai-one@example.com.json"
    existing.write_text(
        json.dumps(
            {
                "email": "one@example.com",
                "sso": "existing-web-sso",
                "access_token": "old-access-token",
            }
        ),
        encoding="utf-8",
    )
    refreshed = {
        "email": "one@example.com",
        "sso": "existing-web-sso",
        "access_token": "new-access-token",
        "refresh_token": "new-refresh-token",
        "auth_kind": "oauth",
    }
    monkeypatch.setattr(panel_app, "convert_one", lambda *_args, **_kwargs: dict(refreshed))
    atomic_calls = []
    write_json_atomic = panel_app._write_json_atomic

    def track_atomic_write(path, payload):
        atomic_calls.append((path, dict(payload)))
        write_json_atomic(path, payload)

    monkeypatch.setattr(panel_app, "_write_json_atomic", track_atomic_write)

    _run_one_worker_item(
        email="one@example.com",
        sso="existing-web-sso",
        password="known-password",
    )

    payload = json.loads(existing.read_text(encoding="utf-8"))
    assert payload["access_token"] == "new-access-token"
    assert payload["refresh_token"] == "new-refresh-token"
    assert payload["password"] == "known-password"
    assert any(path == existing for path, _payload in atomic_calls)
    assert panel_app._cpa_state["ok"] == 1
    assert panel_app._cpa_state["fail"] == 0
    assert not list(paths.directory.glob(".*.tmp"))


def test_refresh_worker_increments_authorization_generation(
    isolated_refresh_state, monkeypatch
):
    paths = panel_app.current_cpa_paths()
    existing = paths.directory / "xai-one@example.com.json"
    existing.write_text(
        json.dumps(
            {
                "email": "one@example.com",
                "sso": "existing-web-sso",
                "sub": "identity-one",
                "access_token": "old-access-token",
                "refresh_token": "old-refresh-token",
                "_authorization_generation": 6,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        panel_app,
        "convert_one",
        lambda *_args, **_kwargs: {
            "email": "one@example.com",
            "sso": "existing-web-sso",
            "sub": "identity-one",
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "auth_kind": "oauth",
        },
    )

    _run_one_worker_item(email="one@example.com", sso="existing-web-sso")

    payload = json.loads(existing.read_text(encoding="utf-8"))
    assert payload["_authorization_generation"] == 7
    assert payload["_authorization_id"]
    assert payload["_authorized_at"]


def test_refresh_worker_preserves_generation_when_identity_output_path_changes(
    isolated_refresh_state, monkeypatch
):
    paths = panel_app.current_cpa_paths()
    old_path = paths.directory / "xai-old@example.com.json"
    old_path.write_text(
        json.dumps(
            {
                "email": "old@example.com",
                "sso": "existing-web-sso",
                "sub": "identity-one",
                "access_token": "old-access-token",
                "refresh_token": "old-refresh-token",
                "_authorization_generation": 6,
                "_authorization_id": "authorization-six",
                "_authorized_at": "2026-07-23T01:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        panel_app,
        "convert_one",
        lambda *_args, **_kwargs: {
            "email": "new@example.com",
            "sso": "existing-web-sso",
            "sub": "identity-one",
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "auth_kind": "oauth",
        },
    )

    _run_one_worker_item(email="old@example.com", sso="existing-web-sso")

    new_path = paths.directory / "xai-new@example.com.json"
    payload = json.loads(new_path.read_text(encoding="utf-8"))
    assert payload["_authorization_generation"] == 7
    assert payload["_authorization_id"] != "authorization-six"


def test_refresh_worker_generation_keeps_increasing_when_email_path_changes_back(
    isolated_refresh_state, monkeypatch
):
    paths = panel_app.current_cpa_paths()
    old_path = paths.directory / "xai-old@example.com.json"
    old_path.write_text(
        json.dumps(
            {
                "email": "old@example.com",
                "sso": "existing-web-sso",
                "sub": "identity-one",
                "access_token": "access-six",
                "refresh_token": "refresh-six",
                "_authorization_generation": 6,
                "_authorization_id": "authorization-six",
                "_authorized_at": "2026-07-23T01:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    output = {
        "email": "new@example.com",
        "sso": "existing-web-sso",
        "sub": "identity-one",
        "access_token": "access-seven",
        "refresh_token": "refresh-seven",
        "auth_kind": "oauth",
    }
    monkeypatch.setattr(
        panel_app,
        "convert_one",
        lambda *_args, **_kwargs: dict(output),
    )

    _run_one_worker_item(email="old@example.com", sso="existing-web-sso")
    new_path = paths.directory / "xai-new@example.com.json"
    assert json.loads(new_path.read_text(encoding="utf-8"))[
        "_authorization_generation"
    ] == 7

    output.update(
        {
            "email": "old@example.com",
            "access_token": "access-eight",
            "refresh_token": "refresh-eight",
        }
    )
    _run_one_worker_item(email="new@example.com", sso="existing-web-sso")

    payload = json.loads(old_path.read_text(encoding="utf-8"))
    assert payload["_authorization_generation"] == 8
    assert payload["refresh_token"] == "refresh-eight"


def test_refresh_worker_reloads_generation_after_another_process_commits(
    isolated_refresh_state, monkeypatch
):
    paths = panel_app.current_cpa_paths()
    old_path = paths.directory / "xai-old@example.com.json"
    old_path.write_text(
        json.dumps(
            {
                "email": "old@example.com",
                "sso": "existing-web-sso",
                "sub": "identity-one",
                "access_token": "access-six",
                "refresh_token": "refresh-six",
                "_authorization_generation": 6,
                "_authorization_id": "authorization-six",
                "_authorized_at": "2026-07-23T01:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    identity_probe = {"sub": "identity-one"}
    assert panel_app._find_previous_authorization_for_identity(identity_probe)[
        "_authorization_generation"
    ] == 6

    external_path = paths.directory / "xai-external@example.com.json"
    external_path.write_text(
        json.dumps(
            {
                "email": "external@example.com",
                "sso": "existing-web-sso",
                "sub": "identity-one",
                "access_token": "access-seven",
                "refresh_token": "refresh-seven",
                "_authorization_generation": 7,
                "_authorization_id": "authorization-seven",
                "_authorized_at": "2026-07-23T02:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    workspace_lock = panel_app.InterProcessFileLock(
        paths.directory / ".oauth_ownership.json.lock"
    )
    assert workspace_lock.acquire(blocking=False)
    try:
        panel_app._bump_cpa_workspace_version(workspace_lock.path)
    finally:
        workspace_lock.release()

    monkeypatch.setattr(
        panel_app,
        "convert_one",
        lambda *_args, **_kwargs: {
            "email": "old@example.com",
            "sso": "existing-web-sso",
            "sub": "identity-one",
            "access_token": "access-eight",
            "refresh_token": "refresh-eight",
            "auth_kind": "oauth",
        },
    )

    _run_one_worker_item(email="old@example.com", sso="existing-web-sso")

    payload = json.loads(old_path.read_text(encoding="utf-8"))
    assert payload["_authorization_generation"] == 8
    assert payload["refresh_token"] == "refresh-eight"


def test_refresh_worker_never_exchanges_oauth_while_other_instance_owns_workspace(
    isolated_refresh_state, monkeypatch
):
    paths = panel_app.current_cpa_paths()
    existing = paths.directory / "xai-one@example.com.json"
    canary = b'{"email":"one@example.com","access_token":"old-canary"}\n'
    existing.write_bytes(canary)
    calls = []
    monkeypatch.setattr(
        panel_app,
        "convert_one",
        lambda *_args, **_kwargs: calls.append(True),
    )
    external_lock = panel_app.InterProcessFileLock(
        paths.directory / ".oauth_ownership.json.lock"
    )
    assert external_lock.acquire(blocking=False)
    try:
        _run_one_worker_item(
            email="one@example.com",
            sso="existing-web-sso",
        )
    finally:
        external_lock.release()

    assert calls == []
    assert existing.read_bytes() == canary
    assert panel_app._cpa_state["fail"] == 1
    assert "另一个程序实例" in panel_app._cpa_state["last_error"]


def test_queued_refresh_is_skipped_after_external_same_directory_import_epoch(
    isolated_refresh_state, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        panel_app,
        "convert_one",
        lambda *_args, **_kwargs: calls.append(True),
    )
    queued, reason = panel_app.enqueue_cpa_convert(
        email="old@example.com",
        sso="old-web-sso",
        source="old-batch",
        force=True,
    )
    assert queued, reason
    paths = panel_app.current_cpa_paths()
    external_lock = panel_app.InterProcessFileLock(
        paths.directory / ".oauth_ownership.json.lock"
    )
    assert external_lock.acquire(blocking=False)
    try:
        external_lock.bump_epoch()
    finally:
        external_lock.release()

    panel_app._cpa_q.put(None)
    panel_app._cpa_worker_loop()

    assert calls == []
    assert not list(paths.directory.glob("xai-*.json"))
    assert not panel_app._cpa_inflight
    assert panel_app._cpa_state["fail"] == 0


def test_queued_refresh_is_skipped_after_external_directory_switch(
    isolated_refresh_state, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        panel_app,
        "convert_one",
        lambda *_args, **_kwargs: calls.append(True),
    )
    queued, reason = panel_app.enqueue_cpa_convert(
        email="old@example.com",
        sso="old-web-sso",
        source="old-workspace",
        force=True,
    )
    assert queued, reason
    panel_app.CONFIG_PATH.write_text(
        json.dumps({"credentials_dir": "migrated-vault"}),
        encoding="utf-8",
    )

    panel_app._cpa_q.put(None)
    panel_app._cpa_worker_loop()

    assert calls == []
    assert not list(
        (isolated_refresh_state / "migrated-vault" / "cpa").glob(
            "xai-*.json"
        )
    )
    assert not panel_app._cpa_inflight
    assert panel_app._cpa_state["fail"] == 0


def test_refresh_all_route_queues_available_sso_without_exposing_secrets(
    isolated_refresh_state, monkeypatch
):
    secret = "sso-secret-that-must-not-leak"
    password = "password-secret-that-must-not-leak"
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {
                "email": "one@example.com",
                "password": password,
                "sso": secret,
                "source": "accounts_one.txt",
            },
            {"email": "empty@example.com", "sso": ""},
        ],
    )
    requested_limits = []
    monkeypatch.setattr(
        panel_app,
        "enqueue_all_sso_refresh",
        lambda limit: (requested_limits.append(limit) or 1),
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/refresh-all", json={"limit": 10000}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["total"] == 1
    assert payload["queued"] == 1
    assert payload["skipped"] == 0
    assert payload["cpa"]["pending"] == 0
    assert requested_limits == [10000]
    serialized = response.get_data(as_text=True)
    assert secret not in serialized
    assert password not in serialized


def test_refresh_all_route_rejects_other_process_before_queueing(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {
                "email": "one@example.com",
                "password": "password",
                "sso": "web-sso-one",
                "source": "accounts_one.txt",
            }
        ],
    )
    paths = panel_app.current_cpa_paths()
    external_lock = panel_app.InterProcessFileLock(
        paths.directory / ".oauth_ownership.json.lock"
    )
    assert external_lock.acquire(blocking=False)
    try:
        response = panel_app.app.test_client().post(
            "/api/cpa/reauthorize",
            json={"limit": 10000},
        )
    finally:
        external_lock.release()

    assert response.status_code == 409
    assert "另一个程序实例" in response.get_json()["error"]
    assert panel_app._cpa_q.empty()
    assert not panel_app._cpa_inflight


def test_reauthorize_route_deduplicates_stable_identity_and_uses_new_wording(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {"email": "same@example.com", "sso": "sso-old"},
            {"email": "SAME@example.com", "sso": "sso-new"},
        ],
    )
    monkeypatch.setattr(
        panel_app,
        "enqueue_all_sso_refresh",
        lambda limit: 1,
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/reauthorize", json={"limit": 10000}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 1
    assert payload["queued"] == 1
    assert payload["duplicates_skipped"] == 1
    assert "重新生成账号授权" in payload["message"]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(0, 1), (-50, 1), (50000, 10000), ("invalid", 10000)],
)
def test_refresh_all_route_normalizes_limit(
    isolated_refresh_state, monkeypatch, requested, expected
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [{"email": "one@example.com", "sso": "sso-one"}],
    )
    limits = []
    monkeypatch.setattr(
        panel_app,
        "enqueue_all_sso_refresh",
        lambda limit: (limits.append(limit) or 1),
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/refresh-all", json={"limit": requested}
    )

    assert response.status_code == 200
    assert limits == [expected]


def test_refresh_all_route_rejects_workspace_without_sso(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(panel_app, "unique_accounts", lambda: [])

    response = panel_app.app.test_client().post(
        "/api/cpa/refresh-all", json={"limit": 10000}
    )

    assert response.status_code == 400
    assert "SSO" in response.get_json()["error"]


def test_refresh_all_route_rejects_running_registration(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setitem(panel_app._job, "running", True)
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [{"email": "one@example.com", "sso": "sso-one"}],
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/refresh-all", json={"limit": 10000}
    )

    assert response.status_code == 409
    assert "注册" in response.get_json()["error"]


@pytest.mark.parametrize("busy_lock", ["import", "migration"])
def test_refresh_all_route_rejects_busy_credential_change(
    isolated_refresh_state, monkeypatch, busy_lock
):
    lock = threading.Lock()
    assert lock.acquire(blocking=False)
    if busy_lock == "import":
        monkeypatch.setattr(panel_app, "_credential_import_lock", lock)
    else:
        monkeypatch.setattr(panel_app, "_credential_migration_lock", lock)
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [{"email": "one@example.com", "sso": "sso-one"}],
    )
    try:
        response = panel_app.app.test_client().post(
            "/api/cpa/refresh-all", json={"limit": 10000}
        )
    finally:
        lock.release()

    assert response.status_code == 409
    assert "凭据" in response.get_json()["error"]
