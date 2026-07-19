from __future__ import annotations

import inspect
import threading
from unittest.mock import mock_open

import pytest

import grok_register_ttk as main
from panel import app as panel_app


class FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, value):
        self.writes.append(value)

    def flush(self):
        return None


class FakeProcess:
    def __init__(self, lines=(), *, stays_alive=False, pid=7654):
        self.pid = pid
        self.stdin = FakeStdin()
        self._lines = list(lines)
        self._stays_alive = stays_alive
        self._reader_done = False
        self.stdout = self
        self.wait_calls = []

    def __iter__(self):
        for line in self._lines:
            yield line + "\n"
        self._reader_done = True

    def poll(self):
        if self._stays_alive:
            return None
        return 0 if self._reader_done else None

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return 0


def test_cli_markers_are_stable_and_never_contain_account_secrets():
    start = main.format_round_marker("start", index=2, total=5, attempt=1)
    result = main.format_round_marker(
        "result", index=2, total=5, attempt=1, status="success"
    )

    assert "ROUND_START index=2 total=5 attempt=1" in start
    assert "ROUND_RESULT index=2 total=5 attempt=1 status=success" in result
    combined = (start + result).lower()
    assert "email" not in combined
    assert "jwt" not in combined
    assert "token" not in combined


def test_cli_batch_emits_one_start_and_result_pair_per_terminal_round(monkeypatch):
    logs = []
    cleanups = []
    events = []
    browser_starts = []

    def capture_log(message):
        logs.append(message)
        events.append(("log", message))

    monkeypatch.setattr(main, "cli_log", capture_log)
    monkeypatch.setattr(main, "get_round_timeout_sec", lambda: 60)
    monkeypatch.setattr(
        main, "start_browser", lambda **kwargs: browser_starts.append(kwargs)
    )
    monkeypatch.setattr(main, "cleanup_runtime_memory", lambda **kwargs: None)
    monkeypatch.setattr(
        main,
        "cleanup_active_mailbox",
        lambda **kwargs: cleanups.append(kwargs) or events.append(("cleanup", "")) or True,
        raising=False,
    )
    monkeypatch.setattr(main, "transition_browser_for_next_attempt", lambda *args, **kwargs: "reused")
    monkeypatch.setattr(main, "sleep_with_cancel", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "open_signup_page", lambda **kwargs: None)
    monkeypatch.setattr(
        main,
        "fill_email_and_submit",
        lambda **kwargs: ("private@example.com", "private-jwt"),
    )
    monkeypatch.setattr(main, "fill_code_and_submit", lambda *args, **kwargs: "ABC-123")
    monkeypatch.setattr(
        main,
        "fill_profile_and_submit",
        lambda **kwargs: {"given_name": "A", "family_name": "B", "password": "secret"},
    )
    monkeypatch.setattr(main, "wait_for_sso_cookie", lambda **kwargs: "private-sso")
    monkeypatch.setattr(main, "add_token_to_grok2api_pools", lambda *args, **kwargs: None)
    monkeypatch.setattr("builtins.open", mock_open())
    monkeypatch.setitem(main.config, "enable_nsfw", False)

    main.run_registration_cli(2, round_offset=3, total_count=5)

    starts = [line for line in logs if "@@GROK_ROUND_START" in line]
    results = [line for line in logs if "@@GROK_ROUND_RESULT" in line]
    assert ["index=4" in line for line in starts] == [True, False]
    assert "index=5" in starts[1]
    assert len(results) == 2
    assert all("status=success" in line for line in results)
    assert len(browser_starts) == 1
    assert len(cleanups) == 2
    result_positions = [
        index
        for index, event in enumerate(events)
        if event[0] == "log" and "@@GROK_ROUND_RESULT" in event[1]
    ]
    cleanup_positions = [
        index for index, event in enumerate(events) if event[0] == "cleanup"
    ]
    assert all(result < cleanup for result, cleanup in zip(result_positions, cleanup_positions))
    markers = " ".join(starts + results).lower()
    assert "private@example.com" not in markers
    assert "private-jwt" not in markers
    assert "private-sso" not in markers


def test_cli_turnstile_stall_restarts_slot_and_retries_same_round(
    tmp_path, monkeypatch
):
    logs = []
    transitions = []
    profile_calls = []
    monkeypatch.setitem(main.config, "credentials_dir", str(tmp_path / "credentials"))
    monkeypatch.setitem(main.config, "enable_nsfw", False)
    monkeypatch.setattr(main, "cli_log", logs.append)
    monkeypatch.setattr(main, "get_round_timeout_sec", lambda: 60)
    monkeypatch.setattr(main, "start_browser", lambda **kwargs: None)
    monkeypatch.setattr(main, "cleanup_runtime_memory", lambda **kwargs: None)
    monkeypatch.setattr(main, "cleanup_active_mailbox", lambda **kwargs: True)
    monkeypatch.setattr(main, "sleep_with_cancel", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "open_signup_page", lambda **kwargs: None)
    monkeypatch.setattr(
        main,
        "fill_email_and_submit",
        lambda **kwargs: ("private@example.com", "private-jwt"),
    )
    monkeypatch.setattr(
        main, "fill_code_and_submit", lambda *args, **kwargs: "ABC-123"
    )

    def fill_profile(**kwargs):
        profile_calls.append(True)
        if len(profile_calls) == 1:
            raise main.TurnstileRetryNeeded("灰色占位无 iframe")
        return {"given_name": "A", "family_name": "B", "password": "secret"}

    def transition(has_more, log_callback=None, force_restart=False):
        transitions.append((has_more, force_restart))
        return "restarted" if force_restart else "final"

    monkeypatch.setattr(main, "fill_profile_and_submit", fill_profile)
    monkeypatch.setattr(main, "wait_for_sso_cookie", lambda **kwargs: "private-sso")
    monkeypatch.setattr(
        main, "add_token_to_grok2api_pools", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(main, "transition_browser_for_next_attempt", transition)

    main.run_registration_cli(1, total_count=1)

    assert len(profile_calls) == 2
    assert transitions[0] == (True, True)
    assert transitions[-1] == (False, False)
    results = [line for line in logs if "@@GROK_ROUND_RESULT" in line]
    assert "status=retry" in results[0]
    assert "status=success" in results[-1]


def test_cli_writes_worker_scoped_credentials_and_redacted_markers(
    tmp_path, monkeypatch
):
    logs = []
    credentials_root = tmp_path / "credential-vault"
    monkeypatch.setenv("GROK_WORKER_ID", "7")
    monkeypatch.setitem(main.config, "credentials_dir", str(credentials_root))
    monkeypatch.setitem(main.config, "enable_nsfw", False)
    monkeypatch.setattr(main, "cli_log", logs.append)
    monkeypatch.setattr(main, "get_round_timeout_sec", lambda: 60)
    monkeypatch.setattr(main, "start_browser", lambda **kwargs: None)
    monkeypatch.setattr(main, "cleanup_runtime_memory", lambda **kwargs: None)
    monkeypatch.setattr(main, "cleanup_active_mailbox", lambda **kwargs: True)
    monkeypatch.setattr(
        main, "transition_browser_for_next_attempt", lambda *args, **kwargs: "reused"
    )
    monkeypatch.setattr(main, "sleep_with_cancel", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "open_signup_page", lambda **kwargs: None)
    monkeypatch.setattr(
        main,
        "fill_email_and_submit",
        lambda **kwargs: ("private@example.com", "private-jwt"),
    )
    monkeypatch.setattr(
        main, "fill_code_and_submit", lambda *args, **kwargs: "ABC-123"
    )
    monkeypatch.setattr(
        main,
        "fill_profile_and_submit",
        lambda **kwargs: {
            "given_name": "A",
            "family_name": "B",
            "password": "private-password",
        },
    )
    monkeypatch.setattr(main, "wait_for_sso_cookie", lambda **kwargs: "private-sso")
    monkeypatch.setattr(
        main, "add_token_to_grok2api_pools", lambda *args, **kwargs: None
    )

    main.run_registration_cli(1, total_count=1)

    sso_files = list((credentials_root / "sso").glob("accounts_*_w7_*.txt"))
    mail_files = list(
        (credentials_root / "mail").glob("mail_credentials_*_w7_*.txt")
    )
    assert len(sso_files) == 1
    assert len(mail_files) == 1
    assert sso_files[0].read_text(encoding="utf-8") == (
        "private@example.com----private-password----private-sso\n"
    )
    assert mail_files[0].read_text(encoding="utf-8") == (
        "private@example.com\tprivate-jwt\n"
    )
    markers = " ".join(line for line in logs if "@@GROK_ROUND_" in line)
    assert "worker=7" in markers
    assert "private@example.com" not in markers
    assert "private-jwt" not in markers
    assert "private-sso" not in markers


def test_marker_state_refreshes_deadline_and_deduplicates_results():
    state = panel_app.new_batch_marker_state(
        start_index=1, batch_count=2, total=2, round_timeout=30, now=10
    )

    event = panel_app.consume_batch_marker(
        state, "[12:00:00] @@GROK_ROUND_START index=1 total=2 attempt=1", now=12
    )
    assert event["kind"] == "start"
    assert state["deadline"] == 42

    first = panel_app.consume_batch_marker(
        state,
        "[12:00:01] @@GROK_ROUND_RESULT index=1 total=2 attempt=1 status=success",
        now=13,
    )
    duplicate = panel_app.consume_batch_marker(
        state,
        "[12:00:02] @@GROK_ROUND_RESULT index=1 total=2 attempt=1 status=success",
        now=14,
    )
    assert first["terminal"] is True
    assert duplicate["duplicate"] is True
    assert state["outcomes"] == [(1, "success")]

    panel_app.consume_batch_marker(
        state, "@@GROK_ROUND_START index=2 total=2 attempt=1", now=20
    )
    assert state["deadline"] == 50


def test_retry_result_is_not_counted_as_terminal_round():
    state = panel_app.new_batch_marker_state(1, 1, 1, 30, now=0)
    panel_app.consume_batch_marker(
        state, "@@GROK_ROUND_START index=1 total=1 attempt=1", now=1
    )
    event = panel_app.consume_batch_marker(
        state,
        "@@GROK_ROUND_RESULT index=1 total=1 attempt=1 status=retry",
        now=2,
    )

    assert event["terminal"] is False
    assert state["outcomes"] == []


def test_one_process_supervises_multiple_rounds_and_counts_each_result_once():
    fake = FakeProcess(
        [
            "@@GROK_ROUND_START index=1 total=2 attempt=1",
            "@@GROK_ROUND_RESULT index=1 total=2 attempt=1 status=success",
            "@@GROK_ROUND_START index=2 total=2 attempt=1",
            "@@GROK_ROUND_RESULT index=2 total=2 attempt=1 status=failed",
        ]
    )
    state = panel_app.new_batch_marker_state(1, 2, 2, 30, now=0)
    results = []
    terminated = []

    summary = panel_app.supervise_batch_process(
        fake,
        state,
        stop_requested=lambda: False,
        on_result=lambda index, status: results.append((index, status)),
        terminate_proc=lambda proc: terminated.append(proc.pid),
        now=lambda: 1,
    )

    assert summary["outcomes"] == [(1, "success"), (2, "failed")]
    assert results == [(1, "success"), (2, "failed")]
    assert terminated == []


def test_timeout_kills_owned_batch_once_and_counts_current_round_failed():
    fake = FakeProcess(stays_alive=True)
    state = panel_app.new_batch_marker_state(2, 2, 3, 5, now=0)
    panel_app.consume_batch_marker(
        state, "@@GROK_ROUND_START index=2 total=3 attempt=1", now=0
    )
    results = []
    terminated = []

    summary = panel_app.supervise_batch_process(
        fake,
        state,
        stop_requested=lambda: False,
        on_result=lambda index, status: results.append((index, status)),
        terminate_proc=lambda proc: terminated.append(proc.pid),
        now=lambda: 10,
    )

    assert summary["timed_out"] is True
    assert results == [(2, "failed")]
    assert terminated == [7654]
    assert panel_app.remaining_batch_count(total=3, outcomes=[(1, "success"), *results]) == 1


def test_cleanup_timeout_after_all_results_does_not_invent_extra_round():
    fake = FakeProcess(stays_alive=True)
    state = panel_app.new_batch_marker_state(1, 1, 1, 5, now=0)
    panel_app.consume_batch_marker(
        state,
        "@@GROK_ROUND_RESULT index=1 total=1 attempt=1 status=success",
        now=0,
    )
    state["deadline"] = 1
    results = []
    terminated = []

    summary = panel_app.supervise_batch_process(
        fake,
        state,
        stop_requested=lambda: False,
        on_result=lambda index, status: results.append((index, status)),
        terminate_proc=lambda proc: terminated.append(proc.pid),
        now=lambda: 10,
    )

    assert summary["cleanup_timed_out"] is True
    assert summary["outcomes"] == [(1, "success")]
    assert results == []
    assert terminated == [7654]


def test_stop_kills_current_batch_once_without_fabricating_result():
    fake = FakeProcess(stays_alive=True)
    state = panel_app.new_batch_marker_state(1, 3, 3, 30, now=0)
    terminated = []
    results = []

    summary = panel_app.supervise_batch_process(
        fake,
        state,
        stop_requested=lambda: True,
        on_result=lambda index, status: results.append((index, status)),
        terminate_proc=lambda proc: terminated.append(proc.pid),
        now=lambda: 1,
    )

    assert summary["stopped"] is True
    assert terminated == [7654]
    assert results == []


def test_relaunch_environment_contains_only_remaining_count():
    env = panel_app.build_cli_batch_env(
        {}, batch_count=2, round_offset=3, total=5, engine="chromium", timeout=90
    )

    assert env["GROK_REGISTER_COUNT"] == "2"
    assert env["GROK_ROUND_OFFSET"] == "3"
    assert env["GROK_REGISTER_TOTAL"] == "5"
    assert env["ROUND_TIMEOUT_SEC"] == "90"


def test_worker_batch_uses_config_snapshot_without_rewriting_shared_file(
    monkeypatch,
):
    config_snapshot = {
        "email_provider": "freemail",
        "freemail_api_url": "https://mail.example.com",
        "browser_engine": "chromium",
        "round_timeout_sec": 300,
    }
    saves = []
    monkeypatch.setattr(panel_app, "load_config", lambda: dict(config_snapshot))
    monkeypatch.setattr(panel_app, "save_config", lambda cfg: saves.append(dict(cfg)))
    monkeypatch.setattr(
        panel_app, "resolve_proxy_url", lambda: "http://127.0.0.1:7897"
    )
    monkeypatch.setattr(panel_app, "log_line", lambda message: None)

    def fail_after_preflight(*args, **kwargs):
        raise RuntimeError("stop after preflight")

    monkeypatch.setattr(panel_app.subprocess, "Popen", fail_after_preflight)

    summary = panel_app._run_batch(1, 1, 1, worker_id=1)

    assert summary["fatal"] is True
    assert saves == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 1), ("2", 2), (10, 10), (" 7 ", 7)],
)
def test_registration_concurrency_accepts_only_one_through_ten(value, expected):
    assert panel_app.normalize_registration_concurrency(value) == expected


@pytest.mark.parametrize("value", [0, 11, -1, "many", 1.5, True, None])
def test_registration_concurrency_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="1-10"):
        panel_app.normalize_registration_concurrency(value)


def test_registration_work_is_balanced_into_nonoverlapping_contiguous_ranges():
    assignments = panel_app.partition_registration_work(total=10, concurrency=3)

    assert [
        (item.worker_id, item.start_index, item.batch_count, item.round_offset)
        for item in assignments
    ] == [
        (1, 1, 4, 0),
        (2, 5, 3, 4),
        (3, 8, 3, 7),
    ]
    covered = [
        index
        for item in assignments
        for index in range(item.start_index, item.start_index + item.batch_count)
    ]
    assert covered == list(range(1, 11))


def test_registration_work_never_creates_empty_workers():
    assignments = panel_app.partition_registration_work(total=2, concurrency=10)

    assert [(item.worker_id, item.batch_count) for item in assignments] == [
        (1, 1),
        (2, 1),
    ]


def test_worker_environment_contains_unique_worker_identity():
    env = panel_app.build_cli_batch_env(
        {},
        batch_count=3,
        round_offset=4,
        total=10,
        engine="chromium",
        timeout=120,
        worker_id=2,
    )

    assert env["GROK_WORKER_ID"] == "2"
    assert env["GROK_REGISTER_COUNT"] == "3"
    assert env["GROK_ROUND_OFFSET"] == "4"


def test_worker_assignments_run_at_the_same_time_and_keep_all_results():
    assignments = panel_app.partition_registration_work(total=6, concurrency=3)
    barrier = threading.Barrier(3)
    active_lock = threading.Lock()
    active = 0
    peak_active = 0

    def run_assignment(assignment):
        nonlocal active, peak_active
        with active_lock:
            active += 1
            peak_active = max(peak_active, active)
        barrier.wait(timeout=2)
        with active_lock:
            active -= 1
        return {
            "worker_id": assignment.worker_id,
            "outcomes": [
                (index, "success")
                for index in range(
                    assignment.start_index,
                    assignment.start_index + assignment.batch_count,
                )
            ],
        }

    summaries = panel_app.run_worker_assignments(
        assignments, run_assignment=run_assignment
    )

    assert peak_active == 3
    assert sorted(summaries) == [1, 2, 3]
    assert sorted(
        index
        for summary in summaries.values()
        for index, _status in summary["outcomes"]
    ) == list(range(1, 7))


def test_worker_process_registry_tracks_unique_pids_and_stops_each_once(monkeypatch):
    monkeypatch.setattr(panel_app, "_procs", {})
    monkeypatch.setitem(panel_app._job, "workers", {})
    terminated = []
    monkeypatch.setattr(
        panel_app,
        "_terminate_register_proc",
        lambda proc: terminated.append(proc.pid),
    )
    processes = [FakeProcess(pid=8000 + worker_id) for worker_id in range(1, 4)]

    for worker_id, proc in enumerate(processes, start=1):
        panel_app.register_worker_process(worker_id, proc)

    assert {worker_id: proc.pid for worker_id, proc in panel_app._procs.items()} == {
        1: 8001,
        2: 8002,
        3: 8003,
    }
    assert {
        int(worker_id): worker["pid"]
        for worker_id, worker in panel_app._job["workers"].items()
    } == {1: 8001, 2: 8002, 3: 8003}

    panel_app.terminate_all_worker_processes()
    panel_app.terminate_all_worker_processes()

    assert sorted(terminated) == [8001, 8002, 8003]
    assert panel_app._procs == {}


def test_unregister_worker_process_cannot_remove_replacement_process(monkeypatch):
    monkeypatch.setattr(panel_app, "_procs", {})
    monkeypatch.setitem(panel_app._job, "workers", {})
    original = FakeProcess(pid=8101)
    replacement = FakeProcess(pid=8102)
    panel_app.register_worker_process(1, original)
    panel_app.register_worker_process(1, replacement)

    panel_app.unregister_worker_process(1, original)

    assert panel_app._procs[1] is replacement
    assert panel_app._job["workers"]["1"]["pid"] == 8102


def test_global_result_aggregation_ignores_duplicate_index(monkeypatch):
    monkeypatch.setitem(panel_app._job, "success", 0)
    monkeypatch.setitem(panel_app._job, "fail", 0)
    monkeypatch.setitem(panel_app._job, "outcomes", {})

    first = panel_app.record_job_result(4, "success", worker_id=1)
    duplicate = panel_app.record_job_result(4, "failed", worker_id=2)
    second = panel_app.record_job_result(5, "failed", worker_id=2)

    assert first is True
    assert duplicate is False
    assert second is True
    assert panel_app._job["success"] == 1
    assert panel_app._job["fail"] == 1
    assert panel_app._job["outcomes"] == {"4": "success", "5": "failed"}


def test_worker_slot_relaunches_only_its_unfinished_slice(monkeypatch):
    calls = []
    summaries = iter(
        [
            {
                "outcomes": [(5, "success")],
                "stopped": False,
                "fatal": False,
                "timed_out": True,
            },
            {
                "outcomes": [(6, "success"), (7, "failed")],
                "stopped": False,
                "fatal": False,
                "timed_out": False,
            },
        ]
    )

    def fake_run_batch(*, start_index, total, batch_count, worker_id):
        calls.append((start_index, total, batch_count, worker_id))
        return next(summaries)

    monkeypatch.setattr(panel_app, "_run_batch", fake_run_batch)
    monkeypatch.setitem(panel_app._job, "stop", False)

    summary = panel_app.run_worker_assignment(
        panel_app.WorkerAssignment(worker_id=2, start_index=5, batch_count=3),
        total=9,
    )

    assert calls == [(5, 9, 3, 2), (6, 9, 2, 2)]
    assert summary["outcomes"] == [
        (5, "success"),
        (6, "success"),
        (7, "failed"),
    ]


def test_one_worker_failure_does_not_cancel_other_workers():
    assignments = panel_app.partition_registration_work(total=4, concurrency=2)

    def run_assignment(assignment):
        if assignment.worker_id == 1:
            raise RuntimeError("worker one crashed")
        return {"outcomes": [(3, "success"), (4, "success")]}

    summaries = panel_app.run_worker_assignments(
        assignments, run_assignment=run_assignment
    )

    assert summaries[1]["fatal"] is True
    assert "worker one crashed" in summaries[1]["error"]
    assert summaries[2]["outcomes"] == [(3, "success"), (4, "success")]


def test_dead_proxy_probe_remains_boolean(monkeypatch):
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("closed")))
    monkeypatch.setattr(panel_app, "load_config", lambda: {"proxy": "http://127.0.0.1:65500"})

    resolved = panel_app.resolve_proxy_url()

    assert isinstance(resolved, str)


def test_panel_has_no_global_browser_leftover_cleanup():
    assert not hasattr(panel_app, "_cleanup_browser_leftovers")
    assert "_run_one_round" not in inspect.getsource(panel_app.job_worker)


def test_legacy_tk_gui_routes_all_credentials_through_configured_store():
    start_source = inspect.getsource(main.GrokRegisterGUI.start_registration)
    run_source = inspect.getsource(main.GrokRegisterGUI.run_registration)

    assert "create_worker_output_paths" in start_source
    assert "os.path.dirname(__file__)" not in start_source
    assert '"mail_credentials.txt"' not in run_source
    assert "self.mail_credentials_file" in run_source
