from __future__ import annotations

import json
import os
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


def test_reauthorization_batch_queue_items_enable_sensitive_log_redaction(
    isolated_refresh_state,
):
    queued, reason = panel_app.enqueue_cpa_convert(
        email="private@example.com",
        sso="private-batch-sso",
        source="accounts_private.txt",
        force=True,
        batch_id="reauthorize-batch",
    )

    assert queued, reason
    item = panel_app._cpa_q.get_nowait()
    panel_app._cpa_q.task_done()
    assert item["redact_sensitive_logs"] is True


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


@pytest.mark.parametrize(
    ("size", "expected_indices"),
    [
        (1, [0]),
        (2, [1, 0]),
        (3, [2, 0, 1]),
        (5, [4, 0, 2, 1, 3]),
    ],
)
def test_reauthorization_preflight_samples_cover_time_and_order_anchors(
    isolated_refresh_state,
    size,
    expected_indices,
):
    candidates = [
        {
            "email": f"user-{index}@example.com",
            "sso": f"sso-{index}",
            "source": "one-source.txt",
            "_stable_identity": f"email:user-{index}@example.com",
        }
        for index in range(size)
    ]
    original = [dict(candidate) for candidate in candidates]

    planned = panel_app._plan_reauthorization_preflight_samples(candidates)

    assert [item["_candidate_index"] for item in planned] == expected_indices
    assert candidates == original
    assert len(
        {item["_stable_identity"] for item in planned}
    ) == len(planned)


def test_reauthorization_preflight_middle_anchor_prefers_distinct_source(
    isolated_refresh_state,
):
    candidates = [
        {
            "email": f"user-{index}@example.com",
            "sso": f"sso-{index}",
            "source": source,
            "_stable_identity": f"email:user-{index}@example.com",
        }
        for index, source in enumerate(
            ["new.txt", "middle.txt", "new.txt", "old.txt", "old.txt"]
        )
    ]

    planned = panel_app._plan_reauthorization_preflight_samples(candidates)

    assert [item["_candidate_index"] for item in planned[:3]] == [4, 0, 1]
    assert len({item["source"] for item in planned[:3]}) == 3


def test_reauthorization_sample_deadline_reserves_fair_primary_share(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(panel_app.time, "monotonic", lambda: 100.0)

    assert panel_app._reauthorization_sample_deadline(
        220.0,
        samples_remaining=3,
    ) == 140.0
    assert panel_app._reauthorization_sample_deadline(
        220.0,
        samples_remaining=1,
    ) == 160.0


def test_reauthorization_candidates_sort_current_and_legacy_files_globally(
    isolated_refresh_state,
):
    current_dir = panel_app.current_credential_layout().sso_dir
    current_dir.mkdir(parents=True, exist_ok=True)
    current_older = current_dir / "accounts_current_older.txt"
    legacy_newer = isolated_refresh_state / "accounts_legacy_newer.txt"
    current_older.write_text(
        "older@example.com----pw----sso-older\n"
        "older-second@example.com----pw----sso-older-second\n",
        encoding="utf-8",
    )
    legacy_newer.write_text(
        "newer@example.com----pw----sso-newer\n",
        encoding="utf-8",
    )
    os.utime(current_older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(legacy_newer, ns=(2_000_000_000, 2_000_000_000))

    candidates, duplicates = panel_app._reauthorization_candidates(10000)

    assert duplicates == 0
    assert [item["email"] for item in candidates] == [
        "newer@example.com",
        "older@example.com",
        "older-second@example.com",
    ]
    assert [item["_source_line_index"] for item in candidates] == [0, 0, 1]
    assert candidates[0]["_source_mtime_ns"] == 2_000_000_000
    assert candidates[-1]["_source_mtime_ns"] == 1_000_000_000


def test_reauthorization_candidates_keep_newest_exact_duplicate_source(
    isolated_refresh_state,
):
    current_dir = panel_app.current_credential_layout().sso_dir
    current_dir.mkdir(parents=True, exist_ok=True)
    current_older = current_dir / "accounts_current_duplicate.txt"
    legacy_newer = isolated_refresh_state / "accounts_legacy_duplicate.txt"
    line = "same@example.com----pw----same-sso\n"
    current_older.write_text(line, encoding="utf-8")
    legacy_newer.write_text(line, encoding="utf-8")
    os.utime(current_older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(legacy_newer, ns=(2_000_000_000, 2_000_000_000))

    candidates, duplicates = panel_app._reauthorization_candidates(10000)

    assert duplicates == 0
    assert len(candidates) == 1
    assert candidates[0]["source"] == legacy_newer.name
    assert candidates[0]["_source_mtime_ns"] == 2_000_000_000


def test_reauthorization_preflight_forwards_sample_deadline(
    isolated_refresh_state, monkeypatch
):
    seen = {}

    class Lease:
        directory = isolated_refresh_state / "vault" / "cpa"

        @staticmethod
        def epoch():
            return 1

        @staticmethod
        def release():
            return None

    monkeypatch.setattr(
        panel_app,
        "_acquire_current_cpa_workspace_lease",
        lambda: Lease(),
    )

    def convert(sso, email, *, deadline=None):
        seen.update(sso=sso, email=email, deadline=deadline)
        return {"email": email, "sso": sso}, None, 1

    monkeypatch.setattr(panel_app, "_convert_cpa_with_retry", convert)
    monkeypatch.setattr(
        panel_app,
        "_commit_cpa_result",
        lambda _result: (True, ""),
    )

    result = panel_app._run_cpa_reauthorization_preflight(
        {"email": "one@example.com", "sso": "sso-one"},
        "batch-one",
        deadline=123.0,
    )

    assert result == (True, "")
    assert seen == {
        "sso": "sso-one",
        "email": "one@example.com",
        "deadline": 123.0,
    }


def test_reauthorization_preflight_redacts_success_identity_from_logs_and_state(
    isolated_refresh_state, monkeypatch
):
    email = "private-success@example.com"
    access_token = "private-access-token"
    batch_id = panel_app._begin_cpa_run("reauthorize", 1)
    monkeypatch.setattr(
        panel_app,
        "_convert_cpa_with_retry",
        lambda sso, converted_email, **_kwargs: (
            {
                "email": converted_email,
                "sso": sso,
                "access_token": access_token,
                "refresh_token": "private-refresh-token",
                "auth_kind": "oauth",
            },
            None,
            1,
        ),
    )

    result = panel_app._run_cpa_reauthorization_preflight(
        {"email": email, "sso": "private-success-sso"},
        batch_id,
    )

    assert result == (True, "")
    logs = "\n".join(panel_app._logs)
    assert email not in logs
    assert access_token not in logs
    assert panel_app._cpa_state["last_ok_email"] == ""


def test_reauthorization_preflight_redacts_failure_log_and_jsonl(
    isolated_refresh_state, monkeypatch
):
    email = "private-failure@example.com"
    raw_error = "transport failed with refresh-secret-must-not-leak"
    batch_id = panel_app._begin_cpa_run("reauthorize", 1)
    monkeypatch.setattr(
        panel_app,
        "_convert_cpa_with_retry",
        lambda *_args, **_kwargs: (
            None,
            RuntimeError(raw_error),
            1,
        ),
    )

    ok, _error = panel_app._run_cpa_reauthorization_preflight(
        {"email": email, "sso": "private-failure-sso"},
        batch_id,
    )

    assert ok is False
    logs = "\n".join(panel_app._logs)
    failed_log = panel_app.current_cpa_paths().failed_path.read_text(
        encoding="utf-8"
    )
    assert email not in logs
    assert raw_error not in logs
    assert email not in failed_log
    assert raw_error not in failed_log
    assert raw_error not in panel_app._cpa_state["last_error"]


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
    email = "one@example.com"
    secret = "sso-secret-that-must-not-leak"
    password = "password-secret-that-must-not-leak"
    raw_error = "upstream-error-with-private-response"
    panel_app._cpa_state["last_ok_email"] = email
    panel_app._cpa_state["last_error"] = raw_error
    panel_app._cpa_state["run_last_error"] = raw_error
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {
                "email": email,
                "password": password,
                "sso": secret,
                "source": "accounts_one.txt",
            },
            {"email": "empty@example.com", "sso": ""},
        ],
    )
    preflight_emails = []
    monkeypatch.setattr(
        panel_app,
        "_run_cpa_reauthorization_preflight",
        lambda candidate, _batch_id, **_kwargs: (
            preflight_emails.append(candidate["email"]) or True,
            "",
        ),
        raising=False,
    )
    monkeypatch.setattr(
        panel_app,
        "enqueue_reauthorization_candidates",
        lambda _candidates, _batch_id: 0,
        raising=False,
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/refresh-all", json={"limit": 10000}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["total"] == 1
    assert payload["queued"] == 1
    assert payload["failed"] == 0
    assert payload["skipped"] == 0
    assert (
        payload["queued"] + payload["failed"] + payload["skipped"]
        == payload["total"]
    )
    assert payload["cpa"]["pending"] == 0
    assert payload["preflight"]["planned"] == 1
    assert payload["preflight"]["attempted"] == 1
    assert payload["preflight"]["succeeded"] == 1
    assert preflight_emails == ["one@example.com"]
    serialized = response.get_data(as_text=True)
    assert email not in serialized
    assert secret not in serialized
    assert password not in serialized
    assert raw_error not in serialized
    assert str(panel_app.current_cpa_paths().directory) not in serialized


def test_reauthorize_preflight_stale_skip_is_not_also_counted_failed(
    isolated_refresh_state,
    monkeypatch,
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {
                "email": "stale@example.com",
                "sso": "stale-preflight-sso",
            }
        ],
    )

    def stale_preflight(_candidate, batch_id, **_kwargs):
        with panel_app._cpa_lock:
            panel_app._record_cpa_batch_skip_locked(batch_id)
        return False, "OAuth 凭据目录已切换"

    monkeypatch.setattr(
        panel_app,
        "_run_cpa_reauthorization_preflight",
        stale_preflight,
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/reauthorize",
        json={"limit": 10000},
    )

    assert response.status_code == 422
    cpa = response.get_json()["cpa"]
    assert cpa["run_total"] == 1
    assert cpa["run_ok"] == 0
    assert cpa["run_fail"] == 0
    assert cpa["run_skipped"] == 1
    assert cpa["run_ok"] + cpa["run_fail"] + cpa["run_skipped"] == 1


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
        "_run_cpa_reauthorization_preflight",
        lambda _candidate, _batch_id, **_kwargs: (True, ""),
        raising=False,
    )
    monkeypatch.setattr(
        panel_app,
        "enqueue_reauthorization_candidates",
        lambda _candidates, _batch_id: 0,
        raising=False,
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
    limits = []
    monkeypatch.setattr(
        panel_app,
        "_reauthorization_candidates",
        lambda limit: (
            limits.append(limit)
            or ([{"email": "one@example.com", "sso": "sso-one"}], 0)
        ),
    )
    monkeypatch.setattr(
        panel_app,
        "_run_cpa_reauthorization_preflight",
        lambda _candidate, _batch_id, **_kwargs: (True, ""),
        raising=False,
    )
    monkeypatch.setattr(
        panel_app,
        "enqueue_reauthorization_candidates",
        lambda _candidates, _batch_id: 0,
        raising=False,
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/refresh-all", json={"limit": requested}
    )

    assert response.status_code == 200
    assert limits == [expected]


def test_reauthorize_route_quarantines_access_denied_preflight_and_continues(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {"email": f"user-{index}@example.com", "sso": f"sso-{index}"}
            for index in range(4)
        ],
    )
    preflight_calls = []
    bulk_calls = []

    def preflight(candidate, _batch_id, **_kwargs):
        preflight_calls.append(candidate["email"])
        if len(preflight_calls) == 1:
            return False, "consent 失败: Access denied"
        return True, ""

    monkeypatch.setattr(
        panel_app,
        "_run_cpa_reauthorization_preflight",
        preflight,
        raising=False,
    )
    monkeypatch.setattr(
        panel_app,
        "enqueue_reauthorization_candidates",
        lambda candidates, batch_id: (
            bulk_calls.append((list(candidates), batch_id))
            or len(candidates)
        ),
        raising=False,
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/reauthorize",
        json={"limit": 10000},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["preflight"]["ok"] is True
    assert payload["queued"] == 3
    assert payload["failed"] == 1
    assert payload["skipped"] == 0
    assert payload["disabled"] == 1
    assert (
        payload["queued"] + payload["failed"] + payload["skipped"]
        == payload["total"]
    )
    assert preflight_calls == [
        "user-3@example.com",
        "user-0@example.com",
        "user-1@example.com",
    ]
    assert [item["email"] for item in bulk_calls[0][0]] == [
        "user-2@example.com",
    ]
    assert payload["preflight"]["attempted"] == 3
    assert payload["preflight"]["succeeded"] == 2
    assert payload["preflight"]["account_denied"] == 1


def test_reauthorize_route_quarantines_all_access_denied_candidates(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {"email": f"user-{index}@example.com", "sso": f"sso-{index}"}
            for index in range(2)
        ],
    )
    preflight_calls = []
    bulk_calls = []

    def preflight(candidate, _batch_id, **_kwargs):
        preflight_calls.append(candidate["email"])
        return False, "consent 失败: Access denied"

    monkeypatch.setattr(
        panel_app,
        "_run_cpa_reauthorization_preflight",
        preflight,
        raising=False,
    )
    monkeypatch.setattr(
        panel_app,
        "enqueue_reauthorization_candidates",
        lambda candidates, batch_id: bulk_calls.append(
            (list(candidates), batch_id)
        ),
        raising=False,
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/reauthorize",
        json={"limit": 10000},
    )

    assert response.status_code == 422
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["preflight"]["ok"] is False
    assert payload["queued"] == 0
    assert payload["failed"] == 2
    assert payload["disabled"] == 2
    assert payload["skipped"] == 0
    assert (
        payload["queued"] + payload["failed"] + payload["skipped"]
        == payload["total"]
    )
    assert bulk_calls == []
    assert preflight_calls == [
        "user-1@example.com",
        "user-0@example.com",
    ]
    assert payload["cpa"]["run_status"] == "preflight_failed"


def test_reauthorize_route_does_not_count_failed_quarantine_as_disabled(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [{"email": "one@example.com", "sso": "sso-one"}],
    )
    monkeypatch.setattr(
        panel_app,
        "_run_cpa_reauthorization_preflight",
        lambda _candidate, _batch_id, **_kwargs: (
            False,
            "consent 失败: Access denied",
        ),
    )
    monkeypatch.setattr(
        panel_app,
        "account_is_disabled",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        panel_app,
        "_quarantine_access_denied_account",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("disabled pool unavailable")
        ),
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/reauthorize",
        json={"limit": 10000},
    )

    assert response.status_code == 422
    payload = response.get_json()
    assert payload["disabled"] == 0
    assert payload["preflight"]["account_denied"] == 1
    assert payload["failed"] == 1
    assert payload["skipped"] == 0


def test_reauthorize_preflight_stops_after_three_access_denials(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {"email": f"user-{index}@example.com", "sso": f"sso-{index}"}
            for index in range(5)
        ],
    )
    preflight_calls = []

    def preflight(candidate, _batch_id, **_kwargs):
        preflight_calls.append(candidate["email"])
        return False, "consent 失败: Access denied"

    monkeypatch.setattr(
        panel_app,
        "_run_cpa_reauthorization_preflight",
        preflight,
        raising=False,
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/reauthorize",
        json={"limit": 10000},
    )

    assert response.status_code == 422
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["disabled"] == 3
    assert payload["queued"] == 0
    assert payload["failed"] == 3
    assert payload["skipped"] == 2
    assert (
        payload["queued"] + payload["failed"] + payload["skipped"]
        == payload["total"]
    )
    assert preflight_calls == [
        "user-4@example.com",
        "user-0@example.com",
        "user-2@example.com",
    ]
    assert payload["cpa"]["circuit_open"] is True
    assert payload["cpa"]["run_status"] == "paused"
    assert (
        payload["cpa"]["run_error_signature"]
        == "oauth_access_denied"
    )


def test_reauthorize_route_still_aborts_on_systemic_preflight_failure(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {"email": f"user-{index}@example.com", "sso": f"sso-{index}"}
            for index in range(3)
        ],
    )
    monkeypatch.setattr(
        panel_app,
        "_run_cpa_reauthorization_preflight",
        lambda _candidate, _batch_id, **_kwargs: (
            False,
            "consent 响应缺少 code",
        ),
    )
    bulk_calls = []
    monkeypatch.setattr(
        panel_app,
        "enqueue_reauthorization_candidates",
        lambda candidates, batch_id: bulk_calls.append(
            (list(candidates), batch_id)
        ),
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/reauthorize",
        json={"limit": 10000},
    )

    assert response.status_code == 422
    payload = response.get_json()
    assert payload["preflight"]["ok"] is False
    assert payload["preflight"]["attempted"] == 3
    assert payload["preflight"]["systemic_signature"] == (
        "consent_missing_code"
    )
    assert payload["queued"] == 0
    assert payload["failed"] == 3
    assert payload["skipped"] == 0
    assert (
        payload["queued"] + payload["failed"] + payload["skipped"]
        == payload["total"]
    )
    assert bulk_calls == []
    assert payload["cpa"]["run_status"] == "paused"


def test_reauthorize_route_continues_after_oldest_sample_timeout(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {"email": f"user-{index}@example.com", "sso": f"sso-{index}"}
            for index in range(4)
        ],
    )
    preflight_calls = []
    deadlines = []
    bulk_calls = []

    def preflight(candidate, _batch_id, *, deadline=None):
        preflight_calls.append(candidate["email"])
        deadlines.append(deadline)
        if len(preflight_calls) == 1:
            return (
                False,
                "conversion deadline exceeded (stage=authorize)",
            )
        return True, ""

    monkeypatch.setattr(
        panel_app,
        "_run_cpa_reauthorization_preflight",
        preflight,
    )
    monkeypatch.setattr(
        panel_app,
        "enqueue_reauthorization_candidates",
        lambda candidates, _batch_id: (
            bulk_calls.append(list(candidates)) or len(candidates)
        ),
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/reauthorize",
        json={"limit": 10000},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert preflight_calls == [
        "user-3@example.com",
        "user-0@example.com",
        "user-1@example.com",
    ]
    assert all(isinstance(deadline, float) for deadline in deadlines)
    assert payload["preflight"]["succeeded"] == 2
    assert payload["preflight"]["timed_out"] == 1
    assert payload["failed"] == 1
    assert payload["skipped"] == 0
    assert (
        payload["queued"] + payload["failed"] + payload["skipped"]
        == payload["total"]
    )
    assert [item["email"] for item in bulk_calls[0]] == [
        "user-2@example.com"
    ]


def test_reauthorize_route_uses_reserve_sample_for_mixed_failures(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {"email": f"user-{index}@example.com", "sso": f"sso-{index}"}
            for index in range(5)
        ],
    )
    errors = [
        "conversion deadline exceeded (stage=authorize)",
        "SSO 无效或已过期（跳到登录页）",
        "consent 响应缺少 code",
    ]
    preflight_calls = []
    bulk_calls = []

    def preflight(candidate, _batch_id, **_kwargs):
        preflight_calls.append(candidate["email"])
        if errors:
            return False, errors.pop(0)
        return True, ""

    monkeypatch.setattr(
        panel_app,
        "_run_cpa_reauthorization_preflight",
        preflight,
    )
    monkeypatch.setattr(
        panel_app,
        "enqueue_reauthorization_candidates",
        lambda candidates, _batch_id: (
            bulk_calls.append(list(candidates)) or len(candidates)
        ),
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/reauthorize",
        json={"limit": 10000},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert preflight_calls == [
        "user-4@example.com",
        "user-0@example.com",
        "user-2@example.com",
        "user-1@example.com",
    ]
    assert payload["preflight"]["attempted"] == 4
    assert payload["preflight"]["succeeded"] == 1
    assert payload["preflight"]["failed"] == 3
    assert payload["failed"] == 3
    assert payload["skipped"] == 0
    assert (
        payload["queued"] + payload["failed"] + payload["skipped"]
        == payload["total"]
    )
    assert [item["email"] for item in bulk_calls[0]] == [
        "user-3@example.com"
    ]


def test_reauthorize_route_preserves_circuit_opened_while_bulk_enqueues(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {"email": f"user-{index}@example.com", "sso": f"sso-{index}"}
            for index in range(4)
        ],
    )
    monkeypatch.setattr(
        panel_app,
        "_run_cpa_reauthorization_preflight",
        lambda _candidate, _batch_id, **_kwargs: (True, ""),
    )

    def open_circuit(_candidates, batch_id):
        with panel_app._cpa_lock:
            assert panel_app._cpa_state["run_id"] == batch_id
            panel_app._cpa_state["circuit_open"] = True
            panel_app._cpa_state["circuit_scope"] = "batch"
            panel_app._cpa_state["circuit_reason"] = "synthetic circuit"
            panel_app._cpa_state["run_status"] = "paused"
        return 0

    monkeypatch.setattr(
        panel_app,
        "enqueue_reauthorization_candidates",
        open_circuit,
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/reauthorize",
        json={"limit": 10000},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["cpa"]["circuit_open"] is True
    assert payload["cpa"]["run_status"] == "paused"


def test_reauthorize_route_stops_after_five_mixed_failed_samples(
    isolated_refresh_state, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "unique_accounts",
        lambda: [
            {"email": f"user-{index}@example.com", "sso": f"sso-{index}"}
            for index in range(6)
        ],
    )
    errors = iter(
        [
            "conversion deadline exceeded (stage=authorize)",
            "SSO 无效或已过期（跳到登录页）",
            "consent 响应缺少 code",
            "authorize 被 Cloudflare 拦截 (HTTP 403)",
            "token HTTP 401",
        ]
    )
    preflight_calls = []
    remaining_budgets = []
    bulk_calls = []

    def preflight(candidate, _batch_id, **_kwargs):
        preflight_calls.append(candidate["email"])
        return False, next(errors)

    monkeypatch.setattr(
        panel_app,
        "_run_cpa_reauthorization_preflight",
        preflight,
    )
    monkeypatch.setattr(
        panel_app,
        "_reauthorization_sample_deadline",
        lambda _deadline, *, samples_remaining: (
            remaining_budgets.append(samples_remaining) or float("inf")
        ),
    )
    monkeypatch.setattr(
        panel_app,
        "enqueue_reauthorization_candidates",
        lambda candidates, _batch_id: bulk_calls.append(list(candidates)),
    )

    response = panel_app.app.test_client().post(
        "/api/cpa/reauthorize",
        json={"limit": 10000},
    )

    assert response.status_code == 422
    payload = response.get_json()
    assert preflight_calls == [
        "user-5@example.com",
        "user-0@example.com",
        "user-2@example.com",
        "user-1@example.com",
        "user-3@example.com",
    ]
    assert payload["preflight"]["attempted"] == 5
    assert payload["preflight"]["failed"] == 5
    assert payload["preflight"]["systemic_signature"] == ""
    assert payload["failed"] == 5
    assert payload["skipped"] == 1
    assert (
        payload["queued"] + payload["failed"] + payload["skipped"]
        == payload["total"]
    )
    assert payload["cpa"]["run_status"] == "preflight_failed"
    assert payload["cpa"]["run_fail"] == 5
    assert payload["cpa"]["run_skipped"] == 1
    assert remaining_budgets == [3, 2, 1, 2, 1]
    assert bulk_calls == []


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
