from __future__ import annotations

import queue
import threading

import pytest

from panel import app as panel_app


@pytest.fixture
def isolated_refresh_state(monkeypatch):
    monkeypatch.setattr(panel_app, "_credential_import_lock", threading.RLock())
    monkeypatch.setattr(panel_app, "_cpa_lock", threading.Lock())
    monkeypatch.setattr(panel_app, "_cpa_q", queue.Queue())
    monkeypatch.setattr(panel_app, "_cpa_done", set())
    monkeypatch.setattr(panel_app, "_cpa_inflight", set())
    monkeypatch.setattr(panel_app, "_cpa_workspace_generation", 7)
    monkeypatch.setattr(panel_app, "_CPA_CORE_OK", True)
    monkeypatch.setattr(panel_app, "convert_one", lambda *_args, **_kwargs: {})
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
