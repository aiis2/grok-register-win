from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from panel import app as panel_app


@pytest.fixture
def isolated_pipeline(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"credentials_dir": "vault"}), encoding="utf-8"
    )
    cpa_dir = tmp_path / "vault" / "cpa"
    monkeypatch.setattr(panel_app, "BASE_DIR", tmp_path)
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.setenv(panel_app.CPA_DIR_ENV, str(cpa_dir))
    monkeypatch.delenv(panel_app.CPA_CONCURRENCY_ENV, raising=False)
    monkeypatch.setattr(panel_app, "_credential_import_lock", threading.RLock())
    monkeypatch.setattr(panel_app, "_cpa_lock", threading.Lock())
    monkeypatch.setattr(panel_app, "_cpa_q", queue.Queue())
    monkeypatch.setattr(panel_app, "_cpa_result_q", queue.Queue(), raising=False)
    monkeypatch.setattr(panel_app, "_cpa_done", set())
    monkeypatch.setattr(panel_app, "_cpa_inflight", set())
    monkeypatch.setattr(panel_app, "_cpa_workspace_generation", 11)
    monkeypatch.setattr(panel_app, "AUTO_CPA", True)
    monkeypatch.setattr(panel_app, "_CPA_CORE_OK", True)
    monkeypatch.setattr(panel_app, "CPA_DELAY", 0)
    monkeypatch.setattr(panel_app, "PANEL_AUTH", False)
    monkeypatch.setattr(
        panel_app,
        "_cpa_state",
        {
            "enabled": True,
            "core_ok": True,
            "core_error": "",
            "concurrency": 2,
            "pending": 0,
            "active_workers": 0,
            "commit_pending": 0,
            "commit_active": 0,
            "ok": 0,
            "fail": 0,
            "running": False,
            "active": False,
            "last_error": "",
            "last_ok_email": "",
        },
    )
    panel_app._logs.clear()
    return cpa_dir


def _enqueue_synthetic(count: int) -> None:
    for index in range(count):
        queued, reason = panel_app.enqueue_cpa_convert(
            email=f"bench-{index}@example.invalid",
            sso=f"synthetic-sso-{index}",
            source="isolated-pipeline-test",
            force=True,
        )
        assert queued, reason


def _stop_pipeline(workers, committer) -> None:
    for _ in workers:
        panel_app._cpa_q.put(None)
    panel_app._cpa_q.join()
    for worker in workers:
        worker.join(timeout=2)
        assert not worker.is_alive()
    panel_app._cpa_result_q.put(None)
    panel_app._cpa_result_q.join()
    committer.join(timeout=2)
    assert not committer.is_alive()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 2),
        ("", 2),
        (1, 1),
        ("2", 2),
        (4, 4),
        (0, 1),
        (8, 4),
        ("bad", 2),
    ],
)
def test_normalize_cpa_concurrency_is_bounded(value, expected):
    assert panel_app.normalize_cpa_concurrency(value) == expected


def test_cpa_concurrency_prefers_environment_over_saved_config(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"cpa_oauth_concurrency": 3}), encoding="utf-8"
    )
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.setenv("CPA_CONCURRENCY", "4")

    assert panel_app.resolve_cpa_concurrency() == 4


def test_cpa_concurrency_uses_saved_config_without_environment(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"cpa_oauth_concurrency": 3}), encoding="utf-8"
    )
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.delenv("CPA_CONCURRENCY", raising=False)

    assert panel_app.resolve_cpa_concurrency() == 3


def test_parallel_oauth_workers_overlap_but_commit_complete_index(
    isolated_pipeline, monkeypatch
):
    call_lock = threading.Lock()
    active = 0
    max_active = 0

    def convert(sso, email="", proxy=""):
        nonlocal active, max_active
        with call_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            sequence = int(sso.rsplit("-", 1)[-1])
            time.sleep(0.025 + (sequence % 3) * 0.005)
            return {
                "email": email,
                "sso": sso,
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "auth_kind": "oauth",
            }
        finally:
            with call_lock:
                active -= 1

    monkeypatch.setattr(panel_app, "convert_one", convert)
    _enqueue_synthetic(24)
    workspace_epoch = panel_app.interprocess_lock_epoch(
        isolated_pipeline / ".oauth_ownership.json.lock"
    )

    workers, committer = panel_app._start_cpa_pipeline_threads(4)
    panel_app._cpa_q.join()
    panel_app._cpa_result_q.join()

    index = json.loads(
        panel_app.current_cpa_paths().index_path.read_text(encoding="utf-8")
    )
    assert max_active >= 2
    assert len(index["items"]) == 24
    assert len(list(isolated_pipeline.glob("xai-*.json"))) == 24
    first_cpa = json.loads(
        next(iter(isolated_pipeline.glob("xai-*.json"))).read_text(
            encoding="utf-8"
        )
    )
    assert first_cpa["disabled"] is False
    assert "_disabled_reason" not in first_cpa
    assert "_disabled_at" not in first_cpa
    assert panel_app._cpa_state["ok"] == 24
    assert panel_app._cpa_state["pending"] == 0
    assert panel_app._cpa_state["active_workers"] == 0
    assert panel_app._cpa_state["commit_pending"] == 0
    assert panel_app._cpa_state["commit_active"] == 0
    assert not panel_app._cpa_inflight
    assert (
        panel_app.interprocess_lock_epoch(
            isolated_pipeline / ".oauth_ownership.json.lock"
        )
        == workspace_epoch
    )

    _stop_pipeline(workers, committer)


def test_local_oauth_workers_share_workspace_lock_until_last_commit_releases(
    isolated_pipeline,
):
    coordinator = panel_app._CPAWorkspaceLeaseCoordinator()
    first = coordinator.acquire(isolated_pipeline)
    second = coordinator.acquire(isolated_pipeline)
    external = panel_app.InterProcessFileLock(
        isolated_pipeline / ".oauth_ownership.json.lock"
    )

    assert external.acquire(blocking=False) is False
    first.release()
    assert external.acquire(blocking=False) is False
    second.release()
    assert external.acquire(blocking=False) is True
    external.release()


def test_fingerprint_remains_inflight_until_serial_commit(
    isolated_pipeline, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "convert_one",
        lambda sso, email="", proxy="": {
            "email": email,
            "sso": sso,
            "access_token": "synthetic-access",
            "refresh_token": "synthetic-refresh",
            "auth_kind": "oauth",
        },
    )
    sso = "synthetic-pending-commit"
    first = panel_app.enqueue_cpa_convert(
        email="pending@example.invalid",
        sso=sso,
        source="isolated-pipeline-test",
        force=True,
    )
    fingerprint = panel_app.sso_fingerprint(sso)
    panel_app._cpa_q.put(None)

    worker = threading.Thread(
        target=panel_app._cpa_oauth_worker_loop, args=(1,), daemon=True
    )
    worker.start()
    panel_app._cpa_q.join()
    worker.join(timeout=2)

    second = panel_app.enqueue_cpa_convert(
        email="pending@example.invalid",
        sso=sso,
        source="isolated-pipeline-test",
        force=True,
    )
    assert first == (True, "queued")
    assert second == (False, "already queued")
    assert fingerprint in panel_app._cpa_inflight
    assert panel_app._cpa_state["commit_pending"] == 1

    panel_app._cpa_result_q.put(None)
    committer = threading.Thread(
        target=panel_app._cpa_commit_worker_loop, daemon=True
    )
    committer.start()
    panel_app._cpa_result_q.join()
    committer.join(timeout=2)

    assert fingerprint not in panel_app._cpa_inflight
    assert fingerprint in panel_app._cpa_done
    assert panel_app._cpa_state["commit_pending"] == 0


@pytest.mark.parametrize(
    "message",
    [
        "token HTTP 429",
        "token HTTP 500",
        "token HTTP 502",
        "token HTTP 503",
        "token HTTP 504",
        "authorize 请求失败: timeout",
        "consent 请求失败: connection reset",
    ],
)
def test_transient_cpa_error_classification(message):
    assert panel_app.is_transient_cpa_error(RuntimeError(message))


@pytest.mark.parametrize(
    "message",
    [
        "SSO 无效或已过期（跳到登录页）",
        "authorize 被 Cloudflare 拦截 (HTTP 403)",
        "consent 响应缺少 code",
        "token HTTP 400",
    ],
)
def test_permanent_cpa_error_classification(message):
    assert not panel_app.is_transient_cpa_error(RuntimeError(message))


def test_transient_oauth_failure_retries_twice_then_succeeds(
    isolated_pipeline, monkeypatch
):
    calls = 0
    sleeps = []

    def convert(sso, email="", proxy=""):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("token HTTP 429")
        return {
            "email": email,
            "sso": sso,
            "access_token": "synthetic-access",
            "refresh_token": "synthetic-refresh",
            "auth_kind": "oauth",
        }

    monkeypatch.setattr(panel_app, "convert_one", convert)
    monkeypatch.setattr(panel_app, "_cpa_sleep", sleeps.append, raising=False)
    monkeypatch.setattr(
        panel_app, "_wait_for_cpa_cooldown", lambda: None, raising=False
    )
    monkeypatch.setattr(
        panel_app, "_extend_cpa_cooldown", lambda _seconds: None, raising=False
    )

    entry, error, attempts = panel_app._convert_cpa_with_retry(
        "synthetic-sso", "retry@example.invalid"
    )

    assert error is None
    assert entry["email"] == "retry@example.invalid"
    assert attempts == 3
    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_permanent_oauth_failure_is_not_retried(
    isolated_pipeline, monkeypatch
):
    calls = 0

    def convert(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("SSO 无效或已过期（跳到登录页）")

    monkeypatch.setattr(panel_app, "convert_one", convert)
    monkeypatch.setattr(panel_app, "_cpa_sleep", lambda _seconds: None, raising=False)

    entry, error, attempts = panel_app._convert_cpa_with_retry(
        "synthetic-sso", "expired@example.invalid"
    )

    assert entry is None
    assert "SSO 无效" in str(error)
    assert attempts == 1
    assert calls == 1


def test_identical_systemic_batch_failures_open_circuit_and_drain_remainder(
    isolated_pipeline,
):
    batch_id = "reauthorize-batch"
    panel_app._cpa_state.update(
        {
            "run_id": batch_id,
            "run_kind": "reauthorize",
            "run_status": "running",
            "run_total": 8,
            "run_queued": 8,
            "run_ok": 0,
            "run_fail": 0,
            "run_skipped": 0,
            "run_error_signature": "",
            "run_error_streak": 0,
            "run_last_error": "",
            "circuit_open": False,
        }
    )
    for index in range(3, 8):
        item = {
            "email": f"queued-{index}@example.invalid",
            "sso": f"queued-sso-{index}",
            "fp": panel_app.sso_fingerprint(f"queued-sso-{index}"),
            "batch_id": batch_id,
        }
        panel_app._cpa_inflight.add(item["fp"])
        panel_app._cpa_q.put(item)
        panel_app._cpa_state["pending"] += 1

    for index in range(3):
        item = {
            "email": f"failed-{index}@example.invalid",
            "sso": f"failed-sso-{index}",
            "fp": panel_app.sso_fingerprint(f"failed-sso-{index}"),
            "batch_id": batch_id,
        }
        panel_app._cpa_inflight.add(item["fp"])
        panel_app._record_cpa_failure(
            item,
            item["fp"],
            RuntimeError(
                "consent 页面协议已变化：未找到 "
                "submitOAuth2Consent Server Action"
            ),
            persist=False,
        )

    assert panel_app._cpa_state["circuit_open"] is True
    assert panel_app._cpa_state["run_status"] == "paused"
    assert panel_app._cpa_state["run_fail"] == 3
    assert panel_app._cpa_state["run_skipped"] == 5
    assert panel_app._cpa_state["pending"] == 0
    assert panel_app._cpa_q.empty()
    assert not panel_app._cpa_inflight


def test_three_distinct_access_denials_open_batch_policy_circuit(
    isolated_pipeline,
):
    batch_id = "account-specific-denial"
    panel_app._cpa_state.update(
        {
            "run_id": batch_id,
            "run_status": "running",
            "run_total": 3,
            "run_fail": 0,
            "run_skipped": 0,
            "run_error_signature": "",
            "run_error_streak": 0,
            "circuit_open": False,
        }
    )

    for index in range(3):
        sso = f"denied-sso-{index}"
        item = {
            "email": f"denied-{index}@example.invalid",
            "sso": sso,
            "fp": panel_app.sso_fingerprint(sso),
            "batch_id": batch_id,
        }
        panel_app._record_cpa_failure(
            item,
            item["fp"],
            RuntimeError("consent 失败: Access denied"),
            persist=False,
        )

    assert panel_app._cpa_state["run_fail"] == 3
    assert panel_app._cpa_state["circuit_open"] is True
    assert panel_app._cpa_state["run_status"] == "paused"
    assert (
        panel_app._cpa_state["run_error_signature"]
        == "oauth_access_denied"
    )


def test_three_unbatched_access_denials_pause_and_drain_automatic_cpa(
    isolated_pipeline,
):
    for index in range(2):
        sso = f"queued-after-denial-{index}"
        item = {
            "email": f"queued-{index}@example.invalid",
            "sso": sso,
            "fp": panel_app.sso_fingerprint(sso),
            "batch_id": "",
        }
        panel_app._cpa_inflight.add(item["fp"])
        panel_app._cpa_q.put(item)
        panel_app._cpa_state["pending"] += 1

    for index in range(3):
        sso = f"automatic-denied-{index}"
        item = {
            "email": f"denied-{index}@example.invalid",
            "sso": sso,
            "fp": panel_app.sso_fingerprint(sso),
            "batch_id": "",
        }
        panel_app._cpa_inflight.add(item["fp"])
        panel_app._record_cpa_failure(
            item,
            item["fp"],
            RuntimeError("consent 失败: Access denied"),
            persist=False,
        )

    assert panel_app._cpa_state["circuit_open"] is True
    assert (
        panel_app._cpa_state["circuit_reason"]
        == "OAuth 服务端连续拒绝，自动 CPA 已暂停"
    )
    assert panel_app._cpa_state["pending"] == 0
    assert panel_app._cpa_q.empty()
    assert not panel_app._cpa_inflight

    queued, reason = panel_app.enqueue_cpa_convert(
        email="new@example.invalid",
        sso="new-sso-after-circuit",
        source="register",
    )

    assert queued is False
    assert reason == "oauth access denied circuit open"


def test_worker_skips_automatic_item_observed_after_policy_circuit(
    isolated_pipeline, monkeypatch
):
    calls = 0

    def convert(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"access_token": "must-not-run"}, None, 1

    monkeypatch.setattr(panel_app, "_convert_cpa_with_retry", convert)
    panel_app._cpa_state.update(
        {
            "circuit_open": True,
            "circuit_scope": "automatic",
            "circuit_reason": "OAuth 服务端连续拒绝，自动 CPA 已暂停",
        }
    )
    sso = "raced-automatic-sso"
    fingerprint = panel_app.sso_fingerprint(sso)
    workspace_lease = panel_app._acquire_current_cpa_workspace_lease()
    workspace_directory = str(workspace_lease.directory)
    workspace_epoch = workspace_lease.epoch()
    workspace_lease.release()
    panel_app._cpa_inflight.add(fingerprint)
    panel_app._cpa_state["pending"] = 1
    panel_app._cpa_q.put(
        {
            "email": "raced@example.invalid",
            "sso": sso,
            "fp": fingerprint,
            "batch_id": "",
            "force": False,
            "workspace_generation": 11,
            "workspace_directory": workspace_directory,
            "workspace_epoch": workspace_epoch,
        }
    )
    panel_app._cpa_q.put(None)

    worker = threading.Thread(
        target=panel_app._cpa_oauth_worker_loop,
        args=(1,),
    )
    worker.start()
    panel_app._cpa_q.join()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert calls == 0
    assert fingerprint not in panel_app._cpa_inflight
    assert panel_app._cpa_state["pending"] == 0
    assert panel_app._cpa_result_q.empty()


def test_access_denied_quarantines_account_and_disables_existing_cpa(
    isolated_pipeline,
):
    email = "denied@example.invalid"
    sso = "denied-web-sso"
    fingerprint = panel_app.sso_fingerprint(sso)
    cpa_path = isolated_pipeline / "xai-denied.json"
    isolated_pipeline.mkdir(parents=True, exist_ok=True)
    cpa_path.write_text(
        json.dumps(
            {
                "email": email,
                "sso": sso,
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "disabled": False,
            }
        ),
        encoding="utf-8",
    )
    panel_app._cpa_done.add(fingerprint)
    panel_app._cpa_inflight.add(fingerprint)

    panel_app._record_cpa_failure(
        {
            "email": email,
            "password": "password-secret",
            "sso": sso,
            "source": "accounts_denied.txt",
        },
        fingerprint,
        RuntimeError("consent 失败: Access denied"),
        cpa_paths=panel_app.current_cpa_paths(),
    )

    public = panel_app.current_disabled_account_pool().list_public()
    assert [item["email"] for item in public] == [email]
    disabled_cpa = json.loads(cpa_path.read_text(encoding="utf-8"))
    assert disabled_cpa["disabled"] is True
    assert disabled_cpa["_disabled_reason"] == "access_denied"
    assert disabled_cpa["_disabled_at"]
    assert fingerprint not in panel_app._cpa_done
    assert fingerprint not in panel_app._cpa_inflight


@pytest.mark.parametrize(
    "error",
    [
        "401 Client Error: Unauthorized",
        "token http 403",
        "consent 响应缺少 code",
        "Cloudflare challenge",
        "request timeout",
    ],
)
def test_non_account_specific_failures_never_quarantine(
    isolated_pipeline,
    error,
):
    sso = f"sso-{error}"
    panel_app._record_cpa_failure(
        {
            "email": "transient@example.invalid",
            "sso": sso,
            "source": "accounts_transient.txt",
        },
        panel_app.sso_fingerprint(sso),
        RuntimeError(error),
        persist=False,
    )

    assert panel_app.current_disabled_account_pool().list_public() == []


def test_disabled_account_is_rejected_before_queueing(isolated_pipeline):
    email = "disabled@example.invalid"
    sso = "disabled-sso"
    panel_app.current_disabled_account_pool().disable(
        {
            "email": email,
            "sso": sso,
            "source": "accounts_disabled.txt",
        },
        "Access denied",
    )

    result = panel_app.enqueue_cpa_convert(
        email=email,
        sso=sso,
        source="manual-refresh",
        force=True,
    )

    assert result == (False, "account disabled")
    assert panel_app._cpa_q.empty()
    assert not panel_app._cpa_inflight


def test_worker_rechecks_quarantine_after_item_was_queued(
    isolated_pipeline,
    monkeypatch,
):
    email = "late-disabled@example.invalid"
    sso = "late-disabled-sso"
    calls = []
    monkeypatch.setattr(
        panel_app,
        "convert_one",
        lambda *_args, **_kwargs: calls.append("converted"),
    )
    assert panel_app.enqueue_cpa_convert(
        email=email,
        sso=sso,
        source="manual-refresh",
        force=True,
    ) == (True, "queued")
    panel_app.current_disabled_account_pool().disable(
        {"email": email, "sso": sso, "source": "manual-refresh"},
        "Access denied",
    )
    panel_app._cpa_q.put(None)

    workers, committer = panel_app._start_cpa_pipeline_threads(1)
    panel_app._cpa_q.join()
    workers[0].join(timeout=2)
    panel_app._cpa_result_q.put(None)
    panel_app._cpa_result_q.join()
    committer.join(timeout=2)

    assert calls == []
    assert panel_app._cpa_state["pending"] == 0
    assert panel_app._cpa_state["active_workers"] == 0
    assert panel_app._cpa_state["commit_pending"] == 0
    assert not panel_app._cpa_inflight


def test_final_failure_preserves_old_cpa_and_clears_pipeline_state(
    isolated_pipeline, monkeypatch
):
    existing = isolated_pipeline / "xai-bench-0@example.invalid.json"
    canary = b'{"email":"bench-0@example.invalid","access_token":"old-canary"}\n'
    isolated_pipeline.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(canary)
    monkeypatch.setattr(
        panel_app,
        "convert_one",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("SSO 无效或已过期（跳到登录页）")
        ),
    )
    _enqueue_synthetic(1)

    workers, committer = panel_app._start_cpa_pipeline_threads(2)
    panel_app._cpa_q.join()
    panel_app._cpa_result_q.join()

    assert existing.read_bytes() == canary
    assert panel_app._cpa_state["pending"] == 0
    assert panel_app._cpa_state["active_workers"] == 0
    assert panel_app._cpa_state["commit_pending"] == 0
    assert panel_app._cpa_state["commit_active"] == 0
    assert panel_app._cpa_state["running"] is False
    assert panel_app._cpa_state["active"] is False
    assert panel_app._cpa_state["fail"] == 1
    assert not panel_app._cpa_inflight
    failed_lines = (
        panel_app.current_cpa_paths()
        .failed_path.read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(failed_lines) == 1

    _stop_pipeline(workers, committer)


@pytest.mark.parametrize(
    "field",
    ["active_workers", "commit_pending", "commit_active"],
)
def test_workspace_switch_rejects_every_active_pipeline_stage(
    isolated_pipeline, field
):
    panel_app._cpa_state[field] = 1
    if field == "active_workers":
        panel_app._cpa_state["active"] = True

    assert (
        panel_app.credential_change_blocker()
        == "CPA 转换仍在运行，完成后才能迁移凭据目录"
    )
    with pytest.raises(panel_app.CredentialImportBusy):
        panel_app._begin_cpa_workspace_switch()


def test_stale_commit_result_never_writes_into_new_workspace(
    isolated_pipeline,
):
    sso = "stale-workspace-sso"
    fingerprint = panel_app.sso_fingerprint(sso)
    panel_app._cpa_inflight.add(fingerprint)
    result = {
        "item": {
            "email": "stale@example.invalid",
            "sso": sso,
            "password": "",
            "source": "old-workspace",
            "fp": fingerprint,
        },
        "entry": {
            "email": "stale@example.invalid",
            "sso": sso,
            "access_token": "synthetic-access",
            "refresh_token": "synthetic-refresh",
            "auth_kind": "oauth",
        },
        "error": None,
        "workspace_generation": 11,
        "worker_id": 1,
        "attempts": 1,
    }
    panel_app._cpa_workspace_generation = 12

    panel_app._commit_cpa_result(result)

    assert not list(isolated_pipeline.glob("xai-*.json"))
    assert not panel_app.current_cpa_paths().index_path.exists()
    assert fingerprint not in panel_app._cpa_inflight
    assert fingerprint not in panel_app._cpa_done


def test_credentials_config_exposes_saved_and_runtime_cpa_concurrency(
    isolated_pipeline,
):
    response = panel_app.app.test_client().get("/api/config/credentials")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["cpa_oauth_concurrency"] == 2
    assert payload["cpa_runtime_concurrency"] == 2
    assert payload["cpa_concurrency_env_override"] is False


def test_credentials_config_saves_bounded_cpa_concurrency(
    isolated_pipeline,
):
    response = panel_app.app.test_client().post(
        "/api/config/credentials",
        json={
            "credentials_dir": "vault",
            "cpa_oauth_concurrency": 4,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    config = json.loads(panel_app.CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["cpa_oauth_concurrency"] == 4
    assert payload["cpa_oauth_concurrency"] == 4
    assert payload["cpa_runtime_concurrency"] == 2
    assert payload["cpa_restart_required"] is True


def test_credentials_config_saves_oauth_target_instance(
    isolated_pipeline,
):
    response = panel_app.app.test_client().post(
        "/api/config/credentials",
        json={
            "credentials_dir": "vault",
            "cpa_oauth_concurrency": 2,
            "oauth_target_instance": "sub2api-primary",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    config = json.loads(panel_app.CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["oauth_target_instance"] == "sub2api-primary"
    assert payload["oauth_target_instance"] == "sub2api-primary"


def test_credentials_config_reports_environment_override(
    isolated_pipeline, monkeypatch
):
    config = json.loads(panel_app.CONFIG_PATH.read_text(encoding="utf-8"))
    config["cpa_oauth_concurrency"] = 3
    panel_app.CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv(panel_app.CPA_CONCURRENCY_ENV, "4")
    panel_app._cpa_state["concurrency"] = 4

    payload = panel_app.app.test_client().get(
        "/api/config/credentials"
    ).get_json()

    assert payload["cpa_oauth_concurrency"] == 3
    assert payload["cpa_runtime_concurrency"] == 4
    assert payload["cpa_concurrency_env_override"] is True
    assert payload["cpa_effective_concurrency"] == 4


def test_invalid_converter_payload_is_committed_as_failure(
    isolated_pipeline, monkeypatch
):
    monkeypatch.setattr(panel_app, "convert_one", lambda *_args, **_kwargs: None)
    _enqueue_synthetic(1)

    workers, committer = panel_app._start_cpa_pipeline_threads(2)
    panel_app._cpa_q.join()
    panel_app._cpa_result_q.join()

    assert panel_app._cpa_state["ok"] == 0
    assert panel_app._cpa_state["fail"] == 1
    assert "无效结果" in panel_app._cpa_state["last_error"]
    assert not list(isolated_pipeline.glob("xai-*.json"))
    _stop_pipeline(workers, committer)


def test_unexpected_retry_wrapper_error_is_committed_as_failure(
    isolated_pipeline, monkeypatch
):
    monkeypatch.setattr(
        panel_app,
        "_convert_cpa_with_retry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("retry wrapper crashed")
        ),
    )
    _enqueue_synthetic(1)

    workers, committer = panel_app._start_cpa_pipeline_threads(1)
    panel_app._cpa_q.join()
    panel_app._cpa_result_q.join()

    assert panel_app._cpa_state["ok"] == 0
    assert panel_app._cpa_state["fail"] == 1
    assert "retry wrapper crashed" in panel_app._cpa_state["last_error"]
    assert not list(isolated_pipeline.glob("xai-*.json"))
    _stop_pipeline(workers, committer)


def test_running_pipeline_can_save_next_restart_concurrency_only(
    isolated_pipeline,
):
    panel_app._cpa_state["active_workers"] = 1
    panel_app._cpa_state["active"] = True
    panel_app._cpa_state["running"] = True

    response = panel_app.app.test_client().post(
        "/api/config/credentials",
        json={
            "credentials_dir": "vault",
            "cpa_oauth_concurrency": 3,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    config = json.loads(panel_app.CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["cpa_oauth_concurrency"] == 3
    assert payload["cpa_runtime_concurrency"] == 2
    assert payload["cpa_restart_required"] is True


def test_save_cpa_index_uses_atomic_writer(
    isolated_pipeline, monkeypatch
):
    calls = []

    def capture(path, payload):
        calls.append((path, payload))

    monkeypatch.setattr(panel_app, "_write_json_atomic", capture)

    panel_app.save_cpa_index_item(
        "synthetic-fingerprint",
        {"file": "xai-synthetic.json"},
    )

    assert len(calls) == 1
    assert calls[0][0] == panel_app.current_cpa_paths().index_path
    assert calls[0][1]["items"]["synthetic-fingerprint"]["file"] == (
        "xai-synthetic.json"
    )
