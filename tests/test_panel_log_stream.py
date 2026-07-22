from __future__ import annotations

import importlib
import json
import threading
import time
from collections import deque

import pytest

from panel import app as panel_app


def _log_api():
    module = importlib.import_module("panel.log_stream")
    return module.SequencedLogBuffer, module.sanitize_log_message


def test_log_buffer_assigns_monotonic_sequences_and_returns_only_new_events():
    SequencedLogBuffer, _ = _log_api()
    buffer = SequencedLogBuffer(maxlen=4)

    first = buffer.append("first")
    second = buffer.append("second")

    assert first.sequence == 1
    assert second.sequence == 2
    assert [(event.sequence, event.line) for event in buffer.after(1)] == [
        (2, "second")
    ]
    assert buffer.latest_sequence == 2


def test_log_buffer_clear_removes_lines_without_reusing_sequence_numbers():
    SequencedLogBuffer, _ = _log_api()
    buffer = SequencedLogBuffer(maxlen=4)
    buffer.append("before clear")

    buffer.clear()
    after_clear = buffer.append("after clear")

    assert after_clear.sequence == 2
    assert [(event.sequence, event.line) for event in buffer.after(0)] == [
        (2, "after clear")
    ]


def test_log_buffer_rollover_returns_current_window_without_duplicates():
    SequencedLogBuffer, _ = _log_api()
    buffer = SequencedLogBuffer(maxlen=3)
    for value in ("one", "two", "three", "four", "five"):
        buffer.append(value)

    assert [(event.sequence, event.line) for event in buffer.after(1)] == [
        (3, "three"),
        (4, "four"),
        (5, "five"),
    ]
    assert [(event.sequence, event.line) for event in buffer.after(4)] == [
        (5, "five")
    ]


def test_log_buffer_wait_after_wakes_when_a_new_event_arrives():
    SequencedLogBuffer, _ = _log_api()
    buffer = SequencedLogBuffer(maxlen=4)
    started = threading.Event()

    def append_later():
        started.set()
        time.sleep(0.02)
        buffer.append("arrived")

    thread = threading.Thread(target=append_later)
    thread.start()
    assert started.wait(timeout=1)

    events = buffer.wait_after(0, timeout=1)
    thread.join(timeout=1)

    assert [(event.sequence, event.line) for event in events] == [(1, "arrived")]


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("password=plain-password-canary", "plain-password-canary"),
        ('{"access_token":"access-token-canary"}', "access-token-canary"),
        ("refresh_token: refresh-token-canary", "refresh-token-canary"),
        ("sso=sso-token-canary", "sso-token-canary"),
        ("Authorization: Bearer bearer-token-canary", "bearer-token-canary"),
        (
            "https://url-user:url-password-canary@example.test/path",
            "url-password-canary",
        ),
        (
            "user@example.test----account-password-canary----account-sso-canary",
            "account-password-canary",
        ),
        (
            "user@example.test----account-password-canary----account-sso-canary",
            "account-sso-canary",
        ),
    ],
)
def test_log_sanitizer_redacts_common_credential_shapes(raw, secret):
    _, sanitize_log_message = _log_api()

    sanitized = sanitize_log_message(raw)

    assert secret not in sanitized
    assert "[REDACTED]" in sanitized


def test_log_sanitizer_preserves_normal_operational_context():
    _, sanitize_log_message = _log_api()

    assert sanitize_log_message("[W2] 第 3 轮注册成功: user@example.test") == (
        "[W2] 第 3 轮注册成功: user@example.test"
    )


@pytest.fixture
def isolated_log_app(monkeypatch):
    SequencedLogBuffer, _ = _log_api()
    event_buffer = SequencedLogBuffer(maxlen=8)
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(panel_app, "PANEL_AUTH", False)
    monkeypatch.setattr(panel_app, "_logs", deque(maxlen=8))
    monkeypatch.setattr(panel_app, "_log_events", event_buffer, raising=False)
    monkeypatch.setattr(panel_app, "_log_stream_slots", slots, raising=False)
    monkeypatch.setattr(panel_app, "LOG_STREAM_HEARTBEAT_SEC", 0.01, raising=False)
    monkeypatch.setitem(panel_app._job, "log_path", "")
    return event_buffer, slots


def test_log_line_sanitizes_legacy_and_event_consumers(isolated_log_app):
    event_buffer, _slots = isolated_log_app
    secret = "log-line-password-canary"

    panel_app.log_line(f"password={secret}")

    assert secret not in "\n".join(panel_app._logs)
    assert secret not in "\n".join(event.line for event in event_buffer.after(0))
    assert "[REDACTED]" in panel_app._logs[-1]


def test_clear_logs_preserves_the_global_event_sequence(isolated_log_app):
    event_buffer, _slots = isolated_log_app
    panel_app.log_line("before")

    panel_app.clear_logs()
    panel_app.log_line("after")

    assert list(panel_app._logs)[-1].endswith("after")
    assert [(event.sequence, event.line[-5:]) for event in event_buffer.after(0)] == [
        (2, "after")
    ]


def test_log_stream_resumes_after_last_event_id_and_sets_stream_headers(
    isolated_log_app,
):
    panel_app.log_line("first event")
    panel_app.log_line("second event")

    response = panel_app.app.test_client().get(
        "/api/logs/stream?after=0",
        headers={"Last-Event-ID": "1"},
        buffered=False,
    )
    chunk = next(response.response).decode("utf-8")
    response.close()

    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["X-Accel-Buffering"] == "no"
    assert "id: 2\n" in chunk
    assert "event: log\n" in chunk
    data_line = next(line for line in chunk.splitlines() if line.startswith("data: "))
    assert json.loads(data_line.removeprefix("data: ")) == {
        "sequence": 2,
        "line": panel_app._logs[-1],
    }
    assert "first event" not in chunk


def test_log_stream_uses_query_cursor_when_header_is_absent(isolated_log_app):
    panel_app.log_line("first event")
    panel_app.log_line("second event")

    response = panel_app.app.test_client().get(
        "/api/logs/stream?after=1", buffered=False
    )
    chunk = next(response.response).decode("utf-8")
    response.close()

    assert "id: 2\n" in chunk
    assert "first event" not in chunk


def test_log_stream_sends_heartbeat_when_no_event_is_available(isolated_log_app):
    response = panel_app.app.test_client().get(
        "/api/logs/stream?after=0", buffered=False
    )
    chunk = next(response.response).decode("utf-8")
    response.close()

    assert chunk.startswith(": heartbeat")
    assert chunk.endswith("\n\n")


@pytest.mark.parametrize("cursor", ["invalid", "-1", "1.5"])
def test_log_stream_rejects_invalid_cursors(isolated_log_app, cursor):
    response = panel_app.app.test_client().get(f"/api/logs/stream?after={cursor}")

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert "after" in response.get_json()["error"]


def test_log_stream_uses_existing_api_login_guard(isolated_log_app, monkeypatch):
    monkeypatch.setattr(panel_app, "PANEL_AUTH", True)

    response = panel_app.app.test_client().get("/api/logs/stream")

    assert response.status_code == 401
    assert response.get_json() == {"ok": False, "error": "unauthorized"}


def test_log_stream_returns_429_when_all_client_slots_are_busy(
    isolated_log_app, monkeypatch
):
    one_slot = threading.BoundedSemaphore(1)
    assert one_slot.acquire(blocking=False)
    monkeypatch.setattr(panel_app, "_log_stream_slots", one_slot, raising=False)

    response = panel_app.app.test_client().get("/api/logs/stream")
    one_slot.release()

    assert response.status_code == 429
    assert "连接" in response.get_json()["error"]


def test_log_stream_releases_client_slot_when_response_closes(isolated_log_app):
    _event_buffer, slots = isolated_log_app
    panel_app.log_line("stream event")

    response = panel_app.app.test_client().get(
        "/api/logs/stream", buffered=False
    )
    next(response.response)
    response.close()

    assert slots.acquire(blocking=False)
    assert slots.acquire(blocking=False)
    assert not slots.acquire(blocking=False)
    slots.release()
    slots.release()
