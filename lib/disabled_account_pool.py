from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from .oauth_credential_ownership import (
        InterProcessFileLock,
        OAuthOwnershipConflict,
    )
except ImportError:  # pragma: no cover - top-level import compatibility
    from oauth_credential_ownership import (  # type: ignore
        InterProcessFileLock,
        OAuthOwnershipConflict,
    )


REGISTRY_VERSION = 1
REGISTRY_FILENAME = "accounts.json"
LOCK_FILENAME = ".accounts.json.lock"


class DisabledAccountPoolError(RuntimeError):
    """Raised when the disabled account registry cannot be used safely."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_email(value: Any) -> str:
    return _text(value).casefold()


def _fingerprint(value: Any) -> str:
    normalized = _text(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def is_access_denied_error(error: Any) -> bool:
    """Return true only for an explicit account-level OAuth denial."""

    decoded = urllib.parse.unquote_plus(_text(error)).casefold()
    return bool(
        re.search(r"\baccess[\s_-]+denied\b", decoded)
        or re.search(
            r"(?:^|[?&\s])(?:error|error_description)=access_denied(?:$|[&\s])",
            decoded,
        )
    )


def _string_set(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {_text(item) for item in value if _text(item)}
    normalized = _text(value)
    return {normalized} if normalized else set()


def _record_aliases(record: Mapping[str, Any]) -> tuple[set[str], set[str], set[str]]:
    emails = {_normalized_email(item) for item in _string_set(record.get("emails"))}
    email = _normalized_email(record.get("email"))
    if email:
        emails.add(email)

    subjects = _string_set(record.get("subjects"))
    subject = _text(record.get("subject"))
    if subject:
        subjects.add(subject)

    fingerprints = _string_set(record.get("sso_fingerprints"))
    fingerprint = _text(record.get("sso_fingerprint"))
    if fingerprint:
        fingerprints.add(fingerprint)
    return emails, subjects, fingerprints


def _account_aliases(account: Mapping[str, Any]) -> tuple[set[str], set[str], set[str]]:
    emails = {_normalized_email(account.get("email"))}
    emails.discard("")
    subjects = {_text(account.get("subject") or account.get("sub"))}
    subjects.discard("")
    fingerprints = {_text(account.get("sso_fingerprint"))}
    fingerprints.discard("")
    sso = _text(account.get("sso"))
    if sso:
        fingerprints.add(_fingerprint(sso))
    return emails, subjects, fingerprints


def _aliases_overlap(
    left: tuple[set[str], set[str], set[str]],
    right: tuple[set[str], set[str], set[str]],
) -> bool:
    return any(a.intersection(b) for a, b in zip(left, right))


class DisabledAccountPool:
    """Persistent, reversible quarantine for account-specific OAuth denials."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / REGISTRY_FILENAME
        self.lock_path = self.directory / LOCK_FILENAME
        self._thread_lock = threading.RLock()

    def _empty_payload(self) -> dict:
        return {
            "version": REGISTRY_VERSION,
            "updated_at": "",
            "accounts": {},
        }

    def _read_payload(self) -> dict:
        if not self.path.exists():
            return self._empty_payload()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DisabledAccountPoolError("禁用账号池文件已损坏，拒绝覆盖") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != REGISTRY_VERSION
            or not isinstance(payload.get("accounts"), dict)
        ):
            raise DisabledAccountPoolError("禁用账号池结构已损坏，拒绝覆盖")
        return payload

    def _write_payload(self, payload: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload["version"] = REGISTRY_VERSION
        payload["updated_at"] = _utc_now()
        temporary = self.path.with_name(
            f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _with_write_lock(self):
        return InterProcessFileLock(self.lock_path)

    def _records(self) -> dict[str, dict]:
        payload = self._read_payload()
        records: dict[str, dict] = {}
        for record_id, value in payload["accounts"].items():
            if isinstance(value, dict):
                record = dict(value)
                record["id"] = _text(record.get("id") or record_id)
                records[str(record_id)] = record
        return records

    def list_internal(self) -> list[dict]:
        with self._thread_lock:
            records = self._records()
        return sorted(
            (dict(record) for record in records.values()),
            key=lambda item: (
                _text(item.get("disabled_at")),
                _normalized_email(item.get("email")),
            ),
            reverse=True,
        )

    def list_public(self) -> list[dict]:
        fields = ("id", "email", "source", "reason", "disabled_at", "last_denied_at")
        return [
            {field: _text(record.get(field)) for field in fields}
            for record in self.list_internal()
        ]

    def identity_sets(self) -> tuple[set[str], set[str], set[str]]:
        emails: set[str] = set()
        subjects: set[str] = set()
        fingerprints: set[str] = set()
        for record in self.list_internal():
            record_emails, record_subjects, record_fingerprints = _record_aliases(
                record
            )
            emails.update(record_emails)
            subjects.update(record_subjects)
            fingerprints.update(record_fingerprints)
        return emails, subjects, fingerprints

    def matches(
        self,
        *,
        email: Any = "",
        subject: Any = "",
        sso: Any = "",
        sso_fingerprint: Any = "",
    ) -> bool:
        aliases = _account_aliases(
            {
                "email": email,
                "subject": subject,
                "sso": sso,
                "sso_fingerprint": sso_fingerprint,
            }
        )
        known = self.identity_sets()
        return _aliases_overlap(aliases, known)

    def disable(self, account: Mapping[str, Any], error: Any) -> dict:
        aliases = _account_aliases(account)
        if not any(aliases):
            raise DisabledAccountPoolError("禁用账号缺少可识别身份")
        now = _utc_now()
        with self._thread_lock:
            try:
                with self._with_write_lock():
                    payload = self._read_payload()
                    records = payload["accounts"]
                    existing_id = ""
                    existing: dict[str, Any] = {}
                    for record_id, value in records.items():
                        if isinstance(value, dict) and _aliases_overlap(
                            aliases, _record_aliases(value)
                        ):
                            existing_id = str(record_id)
                            existing = dict(value)
                            break

                    emails = set(aliases[0])
                    subjects = set(aliases[1])
                    fingerprints = set(aliases[2])
                    if existing:
                        old_aliases = _record_aliases(existing)
                        emails.update(old_aliases[0])
                        subjects.update(old_aliases[1])
                        fingerprints.update(old_aliases[2])

                    seed = next(
                        iter(
                            sorted(subjects)
                            or sorted(emails)
                            or sorted(fingerprints)
                        )
                    )
                    record_id = existing_id or hashlib.sha256(
                        f"disabled-account:{seed}".encode("utf-8")
                    ).hexdigest()[:32]
                    email_value = (
                        _normalized_email(account.get("email"))
                        or _normalized_email(existing.get("email"))
                        or (sorted(emails)[0] if emails else "")
                    )
                    subject_value = (
                        _text(account.get("subject") or account.get("sub"))
                        or _text(existing.get("subject"))
                    )
                    sso_value = _text(account.get("sso"))
                    raw_value = _text(account.get("raw"))
                    if not raw_value and (
                        email_value or account.get("password") or sso_value
                    ):
                        raw_value = (
                            f"{email_value}----{_text(account.get('password'))}"
                            f"----{sso_value}"
                        )
                    record = {
                        **existing,
                        "id": record_id,
                        "email": email_value,
                        "emails": sorted(emails),
                        "subject": subject_value,
                        "subjects": sorted(subjects),
                        "sso_fingerprint": (
                            _fingerprint(sso_value)
                            or _text(account.get("sso_fingerprint"))
                            or _text(existing.get("sso_fingerprint"))
                        ),
                        "sso_fingerprints": sorted(fingerprints),
                        "source": _text(account.get("source"))
                        or _text(existing.get("source")),
                        "raw": raw_value or _text(existing.get("raw")),
                        "reason": "access_denied",
                        "error": _text(error)[:500],
                        "disabled_at": _text(existing.get("disabled_at")) or now,
                        "last_denied_at": now,
                    }
                    records[record_id] = record
                    self._write_payload(payload)
                    return dict(record)
            except OAuthOwnershipConflict as exc:
                raise DisabledAccountPoolError(
                    "禁用账号池正在被其他程序更新"
                ) from exc

    def restore(self, record_id: str) -> dict:
        normalized_id = _text(record_id)
        if not normalized_id:
            raise KeyError(record_id)
        with self._thread_lock:
            try:
                with self._with_write_lock():
                    payload = self._read_payload()
                    record = payload["accounts"].pop(normalized_id, None)
                    if not isinstance(record, dict):
                        raise KeyError(record_id)
                    self._write_payload(payload)
                    restored = dict(record)
                    restored["id"] = normalized_id
                    return restored
            except OAuthOwnershipConflict as exc:
                raise DisabledAccountPoolError(
                    "禁用账号池正在被其他程序更新"
                ) from exc

    def put(self, record: Mapping[str, Any]) -> None:
        record_id = _text(record.get("id"))
        if not record_id:
            raise DisabledAccountPoolError("禁用账号记录缺少 ID")
        with self._thread_lock:
            try:
                with self._with_write_lock():
                    payload = self._read_payload()
                    payload["accounts"][record_id] = dict(record)
                    self._write_payload(payload)
            except OAuthOwnershipConflict as exc:
                raise DisabledAccountPoolError(
                    "禁用账号池正在被其他程序更新"
                ) from exc
