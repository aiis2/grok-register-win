import socket

import grok_register_ttk as main


def _free_local_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def test_turnstile_gate_claim_is_exclusive_and_reusable(monkeypatch):
    port = _free_local_port()
    monkeypatch.setenv("GROK_TURNSTILE_GATE_PORTS", str(port))
    main.release_turnstile_gate()

    try:
        assert main.acquire_turnstile_gate() is True
        assert main._claim_turnstile_gate_port(port) is None
        assert main.release_turnstile_gate() is True

        assert main.acquire_turnstile_gate() is True
    finally:
        main.release_turnstile_gate()


def test_code_submission_acquires_gate_only_after_otp_arrives(monkeypatch):
    events = []

    class FakePage:
        def run_js(self, script, *args):
            events.append("dom")
            if "const aggregate" in script:
                return "filled-aggregate"
            if "const buttons" in script:
                return "clicked"
            return False

    monkeypatch.setattr(main, "page", FakePage())
    monkeypatch.setattr(main, "snapshot_inbox_ids", lambda *args, **kwargs: set())
    monkeypatch.setattr(
        main,
        "get_oai_code",
        lambda *args, **kwargs: events.append("otp") or "ABC-123",
    )
    monkeypatch.setattr(
        main,
        "acquire_turnstile_gate",
        lambda **kwargs: events.append("gate") or True,
    )
    monkeypatch.setattr(main, "sleep_with_cancel", lambda *args, **kwargs: None)

    assert main.fill_code_and_submit("private@example.com", "private-token") == "ABC-123"
    assert events.index("otp") < events.index("gate") < events.index("dom")
