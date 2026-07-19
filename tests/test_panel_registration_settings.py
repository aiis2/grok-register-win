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
        json={"count": 7, "concurrency": 3, "browser_engine": "chromium"},
    )

    assert response.status_code == 200
    assert calls == [(7, 3)]
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["register_concurrency"] == 3
    assert response.get_json()["concurrency"] == 3


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
