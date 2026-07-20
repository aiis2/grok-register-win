from __future__ import annotations

import json

import pytest

from panel import app as panel_app


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    config_path = app_root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "credentials_dir": "data/credentials",
                "register_concurrency": 4,
                "browser_engine": "chromium",
                "browser_window_mode": "hidden",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(panel_app, "BASE_DIR", app_root)
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.delenv("CPA_DIR", raising=False)
    return app_root, config_path


def test_panel_contains_concurrency_worker_and_credential_controls():
    html = panel_app.INDEX_HTML

    assert 'id="count"' in html
    assert 'max="10000"' in html
    assert 'id="register_concurrency"' in html
    assert 'min="1"' in html and 'max="10"' in html
    assert 'id="worker_grid"' in html
    assert 'id="credentials_dir"' in html
    assert 'id="credential_save"' in html
    assert 'id="credential_migrate"' in html
    assert "/api/config/credentials" in html
    assert "/api/config/credentials/migrate" in html
    assert "SHA-256" in html
    assert "不会覆盖" in html


def test_index_renders_saved_registration_concurrency(isolated_config):
    response = panel_app.app.test_client().get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="register_concurrency"' in html
    assert 'value="4"' in html


@pytest.mark.parametrize("value", [0, 11, -1, "many", 1.5, True, None])
def test_job_start_rejects_invalid_concurrency(
    isolated_config, monkeypatch, value
):
    calls = []
    monkeypatch.setattr(
        panel_app,
        "start_job",
        lambda count, concurrency: calls.append((count, concurrency)) or (True, "ok"),
    )

    response = panel_app.app.test_client().post(
        "/api/job/start", json={"count": 6, "concurrency": value}
    )

    assert response.status_code == 400
    assert "1-10" in response.get_json()["error"]
    assert calls == []


def test_job_start_persists_and_passes_valid_concurrency(
    isolated_config, monkeypatch
):
    _app_root, config_path = isolated_config
    calls = []
    monkeypatch.setattr(
        panel_app,
        "start_job",
        lambda count, concurrency: calls.append((count, concurrency)) or (True, "ok"),
    )

    response = panel_app.app.test_client().post(
        "/api/job/start",
        json={
            "count": 7,
            "concurrency": 3,
            "browser_engine": "chromium",
            "browser_window_mode": "minimized",
        },
    )

    assert response.status_code == 200
    assert calls == [(7, 3)]
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["register_concurrency"] == 3
    assert saved["browser_window_mode"] == "minimized"
    assert response.get_json()["concurrency"] == 3


def test_browser_config_get_and_post_round_trip_window_mode(isolated_config):
    client = panel_app.app.test_client()

    before = client.get("/api/config/browser")
    updated = client.post(
        "/api/config/browser",
        json={"browser_engine": "chromium", "browser_window_mode": "visible"},
    )
    after = client.get("/api/config/browser")

    assert before.status_code == 200
    assert before.get_json()["browser_window_mode"] == "hidden"
    assert updated.status_code == 200
    assert updated.get_json()["browser_window_mode"] == "visible"
    assert after.get_json()["browser_window_mode"] == "visible"


def test_job_start_uses_saved_concurrency_when_request_omits_it(
    isolated_config, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        panel_app,
        "start_job",
        lambda count, concurrency: calls.append((count, concurrency)) or (True, "ok"),
    )

    response = panel_app.app.test_client().post(
        "/api/job/start", json={"count": 8}
    )

    assert response.status_code == 200
    assert calls == [(8, 4)]


def test_job_status_exposes_sorted_worker_array_and_effective_concurrency(
    isolated_config, monkeypatch
):
    monkeypatch.setitem(panel_app._job, "concurrency", 4)
    monkeypatch.setitem(
        panel_app._job,
        "workers",
        {
            "2": {
                "worker_id": 2,
                "pid": 9002,
                "status": "running",
                "start_index": 4,
                "batch_count": 3,
            },
            "1": {
                "worker_id": 1,
                "pid": 9001,
                "status": "completed",
                "start_index": 1,
                "batch_count": 3,
            },
        },
    )
    monkeypatch.setitem(panel_app._job, "outcomes", {"1": "success"})

    response = panel_app.app.test_client().get("/api/job/status")

    assert response.status_code == 200
    job = response.get_json()["job"]
    assert [worker["worker_id"] for worker in job["workers"]] == [1, 2]
    assert job["effective_concurrency"] == 2
    assert job["active_workers"] == 1
    assert job["outcomes"] == {"1": "success"}


def test_poll_disables_storage_migration_while_registration_runs():
    html = panel_app.INDEX_HTML

    assert "setCredentialActionsDisabled" in html
    assert "st.running" in html
    assert "cpa.pending" in html


def test_start_button_stays_locked_while_start_request_is_in_flight():
    html = panel_app.INDEX_HTML
    start_source = html.split("async function startJob(){", 1)[1].split(
        "async function stopJob(){", 1
    )[0]

    assert "let registrationStartPending=false;" in html
    assert "if(registrationStartPending) return;" in start_source
    assert (
        start_source.index("registrationStartPending=true;")
        < start_source.index("await ")
    )
    assert "finally{" in start_source
    assert "registrationStartPending=false;" in start_source
    assert (
        "registerButton.disabled=registrationStartPending||"
        "emailReceiveRegistrationRunning||running;"
    ) in html
    assert (
        "document.getElementById('btn_start').disabled="
        "registrationStartPending||!!st.running||emailReceiveRunning;"
    ) in html


def test_start_job_accepts_ten_thousand_rounds(tmp_path, monkeypatch):
    created = []

    class DeferredThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon
            created.append(self)

        def start(self):
            return None

    monkeypatch.setattr(panel_app.threading, "Thread", DeferredThread)
    monkeypatch.setattr(panel_app, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setitem(panel_app._job, "running", False)
    monkeypatch.setitem(panel_app._job, "stop", False)

    result = panel_app.start_job(10_000, concurrency=10)

    assert result == (True, "已启动")
    assert len(created) == 1
    assert panel_app._job["count"] == 10_000


def test_start_job_rejects_more_than_ten_thousand_rounds():
    assert panel_app.start_job(10_001) == (False, "轮数范围 1-10000")


def test_start_job_reserves_running_state_before_worker_thread_runs(
    isolated_config, tmp_path, monkeypatch
):
    created = []

    class DeferredThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(panel_app.threading, "Thread", DeferredThread)
    monkeypatch.setattr(panel_app, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setitem(panel_app._job, "running", False)
    monkeypatch.setitem(panel_app._job, "stop", False)

    first = panel_app.start_job(5, concurrency=2)
    second = panel_app.start_job(5, concurrency=2)

    assert first == (True, "已启动")
    assert second == (False, "已有任务在运行")
    assert len(created) == 1 and created[0].started is True
    assert panel_app._job["running"] is True
    assert panel_app._job["status"] == "starting"
    assert panel_app._job["count"] == 5
    assert panel_app._job["concurrency"] == 2


def test_stop_requested_during_startup_is_not_lost(
    isolated_config, tmp_path, monkeypatch
):
    created = []

    class DeferredThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            created.append(self)

        def start(self):
            return None

    monkeypatch.setattr(panel_app.threading, "Thread", DeferredThread)
    monkeypatch.setattr(panel_app, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setitem(panel_app._job, "running", False)
    monkeypatch.setitem(panel_app._job, "stop", False)
    monkeypatch.setattr(
        panel_app,
        "resolve_proxy_url",
        lambda: (_ for _ in ()).throw(AssertionError("proxy probe should not run")),
    )

    assert panel_app.start_job(3, concurrency=2)[0] is True
    assert panel_app.stop_job()[0] is True
    created[0].target(*created[0].args)

    assert panel_app._job["running"] is False
    assert panel_app._job["stop"] is True
    assert panel_app._procs == {}
