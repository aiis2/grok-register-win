from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass


_ACCOUNT_LINE_RE = re.compile(
    r"(?P<email>[^\s]+@[^\s]+?)----[^\r\n]*?----[^\s]+"
)
_URL_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>https?://)[^/\s:@]+:[^@\s/]+@",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_KEY_VALUE_RE = re.compile(
    r"(?P<prefix>[\"']?(?:password|passwd|pwd|sso|access_token|refresh_token|"
    r"id_token|admin_token|api_key|authorization)[\"']?\s*[:=]\s*)"
    r"(?P<open>[\"']?)(?P<value>[^\"'\s,;}]+)(?P<close>[\"']?)",
    re.IGNORECASE,
)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)


def sanitize_log_message(message: object) -> str:
    """Redact credential shapes before a message reaches any log consumer."""
    text = str(message)
    text = _ACCOUNT_LINE_RE.sub(
        lambda match: f"{match.group('email')}----[REDACTED]----[REDACTED]",
        text,
    )
    text = _URL_CREDENTIAL_RE.sub(
        lambda match: f"{match.group('scheme')}[REDACTED]@",
        text,
    )
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)

    def redact_value(match: re.Match[str]) -> str:
        return (
            f"{match.group('prefix')}{match.group('open')}"
            f"[REDACTED]{match.group('close')}"
        )

    text = _KEY_VALUE_RE.sub(redact_value, text)
    return _JWT_RE.sub("[REDACTED]", text)


@dataclass(frozen=True)
class LogEvent:
    sequence: int
    line: str


class SequencedLogBuffer:
    """Thread-safe, bounded log events with a process-lifetime sequence."""

    def __init__(self, *, maxlen: int = 2000) -> None:
        if maxlen < 1:
            raise ValueError("maxlen must be greater than zero")
        self._events: deque[LogEvent] = deque(maxlen=maxlen)
        self._condition = threading.Condition()
        self._sequence = 0

    @property
    def latest_sequence(self) -> int:
        with self._condition:
            return self._sequence

    def append(self, line: object) -> LogEvent:
        sanitized = sanitize_log_message(line)
        with self._condition:
            self._sequence += 1
            event = LogEvent(sequence=self._sequence, line=sanitized)
            self._events.append(event)
            self._condition.notify_all()
            return event

    def clear(self) -> None:
        with self._condition:
            self._events.clear()
            self._condition.notify_all()

    def _after_locked(self, sequence: int) -> list[LogEvent]:
        return [event for event in self._events if event.sequence > sequence]

    def after(self, sequence: int) -> list[LogEvent]:
        with self._condition:
            return self._after_locked(sequence)

    def wait_after(self, sequence: int, *, timeout: float) -> list[LogEvent]:
        with self._condition:
            available = self._after_locked(sequence)
            if available:
                return available
            self._condition.wait(timeout=max(0.0, float(timeout)))
            return self._after_locked(sequence)
