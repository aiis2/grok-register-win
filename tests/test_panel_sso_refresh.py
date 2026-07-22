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
