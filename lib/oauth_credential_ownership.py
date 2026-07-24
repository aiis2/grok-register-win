from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


OWNERSHIP_VERSION = 1
_TARGET_INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PROCESS_LOCK_GUARD = threading.Lock()
_PROCESS_LOCKED_PATHS: set[str] = set()
_REGISTRY_GUARD = threading.RLock()
_LOCK_EPOCH_OFFSET = 1
_LOCK_EPOCH_SIZE = 8


class OAuthOwnershipConflict(RuntimeError):
    pass


def _normalized_lock_key(path: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(os.fspath(path)))
    if normalized.startswith("\\\\?\\UNC\\"):
        normalized = "\\\\" + normalized[8:]
    elif normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    return os.path.normpath(normalized)


def _text(value) -> str:
    return str(value or "").strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_target_instance(value: str) -> str:
    target = _text(value)
    if not _TARGET_INSTANCE_RE.fullmatch(target):
        raise ValueError(
            "目标实例标识必须为 1–64 位字母、数字、点、下划线或连字符"
        )
    return target


def identity_fingerprint(entry: dict) -> str:
    if not isinstance(entry, dict):
        return ""
    sub = _text(entry.get("sub"))
    email = _text(entry.get("email")).casefold()
    sso = _text(entry.get("sso"))
    if sub:
        material = f"sub:{sub}"
    elif email and email != "unknown":
        material = f"email:{email}"
    elif sso:
        material = "sso:" + hashlib.sha256(sso.encode("utf-8")).hexdigest()
    else:
        access = _text(entry.get("access_token"))
        refresh = _text(entry.get("refresh_token"))
        token = refresh or access
        if not token:
            return ""
        material = "token:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def credential_fingerprint(entry: dict) -> str:
    if not isinstance(entry, dict):
        return ""
    token = _text(entry.get("refresh_token")) or _text(
        entry.get("access_token")
    )
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _authorization_timestamp(entry: dict) -> float:
    for key in ("_authorized_at", "last_refresh"):
        raw = _text(entry.get(key))
        if not raw:
            continue
        try:
            return datetime.fromisoformat(
                raw.replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError):
            continue
    return 0.0


def _has_tracked_authorization(entry: dict) -> bool:
    try:
        generation = int(entry.get("_authorization_generation") or 0)
    except (TypeError, ValueError):
        return False
    authorized_at = _text(entry.get("_authorized_at"))
    if generation < 1 or not _text(entry.get("_authorization_id")):
        return False
    if not authorized_at:
        return False
    try:
        datetime.fromisoformat(authorized_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return True


def select_latest_credentials(
    entries: Iterable[dict],
) -> tuple[list[dict], int]:
    selected: dict[str, tuple[float, dict]] = {}
    order: list[str] = []
    total = 0
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        total += 1
        entry = dict(raw)
        identity = identity_fingerprint(entry)
        if not identity:
            continue
        timestamp = _authorization_timestamp(entry)
        current = selected.get(identity)
        if current is None:
            order.append(identity)
            selected[identity] = (timestamp, entry)
        elif timestamp > current[0]:
            selected[identity] = (timestamp, entry)
    result = [selected[identity][1] for identity in order]
    return result, max(0, total - len(result))


def stamp_authorization(
    entry: dict,
    *,
    previous: Optional[dict] = None,
    authorized_at: str = "",
    authorization_id: str = "",
) -> dict:
    stamped = dict(entry or {})
    generation = 1
    if (
        isinstance(previous, dict)
        and identity_fingerprint(previous)
        and identity_fingerprint(previous) == identity_fingerprint(stamped)
    ):
        try:
            generation = max(
                0, int(previous.get("_authorization_generation") or 0)
            ) + 1
        except (TypeError, ValueError):
            generation = 1
    stamped["_authorization_generation"] = generation
    stamped["_authorization_id"] = _text(authorization_id) or uuid.uuid4().hex
    stamped["_authorized_at"] = _text(authorized_at) or _utc_now()
    return stamped


class InterProcessFileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None
        self._key = _normalized_lock_key(self.path)

    def acquire(self, *, blocking: bool = False) -> bool:
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _PROCESS_LOCK_GUARD:
            if self._key in _PROCESS_LOCKED_PATHS:
                return False
            file_descriptor = os.open(
                os.fspath(self.path),
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                handle = os.fdopen(file_descriptor, "r+b")
            except Exception:
                os.close(file_descriptor)
                raise
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    mode = (
                        msvcrt.LK_LOCK
                        if blocking
                        else msvcrt.LK_NBLCK
                    )
                    msvcrt.locking(handle.fileno(), mode, 1)
                else:
                    import fcntl

                    flags = fcntl.LOCK_EX
                    if not blocking:
                        flags |= fcntl.LOCK_NB
                    fcntl.flock(handle.fileno(), flags)
            except (OSError, IOError):
                handle.close()
                return False
            _PROCESS_LOCKED_PATHS.add(self._key)
            self._handle = handle
            return True

    def epoch(self) -> int:
        handle = self._handle
        if handle is None:
            raise OAuthOwnershipConflict("尚未持有跨进程凭据锁")
        position = handle.tell()
        try:
            handle.seek(_LOCK_EPOCH_OFFSET)
            raw = handle.read(_LOCK_EPOCH_SIZE)
            if len(raw) != _LOCK_EPOCH_SIZE:
                return 0
            return int.from_bytes(raw, byteorder="big", signed=False)
        finally:
            handle.seek(position)

    def bump_epoch(self) -> int:
        current = self.epoch()
        if current >= (1 << (_LOCK_EPOCH_SIZE * 8)) - 1:
            raise OAuthOwnershipConflict("OAuth 凭据工作区 epoch 已耗尽")
        return self.set_epoch(current + 1)

    def set_epoch(self, value: int) -> int:
        handle = self._handle
        if handle is None:
            raise OAuthOwnershipConflict("尚未持有跨进程凭据锁")
        try:
            updated = int(value)
        except (TypeError, ValueError) as exc:
            raise OAuthOwnershipConflict(
                "OAuth 凭据工作区 epoch 无效"
            ) from exc
        maximum = (1 << (_LOCK_EPOCH_SIZE * 8)) - 1
        if updated < 0 or updated > maximum:
            raise OAuthOwnershipConflict(
                "OAuth 凭据工作区 epoch 超出范围"
            )
        handle.seek(_LOCK_EPOCH_OFFSET)
        handle.write(
            updated.to_bytes(
                _LOCK_EPOCH_SIZE,
                byteorder="big",
                signed=False,
            )
        )
        handle.flush()
        os.fsync(handle.fileno())
        handle.seek(0)
        return updated

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None
            with _PROCESS_LOCK_GUARD:
                _PROCESS_LOCKED_PATHS.discard(self._key)

    def __enter__(self):
        if not self.acquire(blocking=True):
            raise OAuthOwnershipConflict("无法获取跨进程凭据锁")
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.release()


def interprocess_lock_version(path: Path) -> int:
    try:
        return max(0, int(Path(path).stat().st_mtime_ns))
    except (OSError, ValueError):
        return 0


def interprocess_lock_epoch(path: Path) -> int:
    try:
        with Path(path).open("rb") as handle:
            handle.seek(_LOCK_EPOCH_OFFSET)
            raw = handle.read(_LOCK_EPOCH_SIZE)
    except OSError:
        return 0
    if len(raw) != _LOCK_EPOCH_SIZE:
        return 0
    return int.from_bytes(raw, byteorder="big", signed=False)


def bump_interprocess_lock_version(path: Path) -> int:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not lock_path.exists():
        lock_path.touch()
    current = interprocess_lock_version(lock_path)
    next_version = max(time.time_ns(), current + 1_000_000)
    os.utime(lock_path, ns=(next_version, next_version))
    return interprocess_lock_version(lock_path)


class OAuthCredentialOwnershipRegistry:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    def _load(self) -> dict:
        if not self.path.is_file():
            return {"version": OWNERSHIP_VERSION, "items": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise OAuthOwnershipConflict(
                "OAuth 凭据所有权登记已损坏；为避免重复使用 refresh token，"
                "已停止导出"
            ) from exc
        if not isinstance(payload, dict):
            raise OAuthOwnershipConflict("OAuth 凭据所有权登记已损坏")
        if payload.get("version") != OWNERSHIP_VERSION:
            raise OAuthOwnershipConflict(
                "OAuth 凭据所有权登记版本不受支持，已停止导出"
            )
        items = payload.get("items")
        if not isinstance(items, dict):
            raise OAuthOwnershipConflict("OAuth 凭据所有权登记已损坏")
        fingerprint_re = re.compile(r"^[0-9a-f]{64}$")
        for identity, item in items.items():
            if (
                not isinstance(identity, str)
                or not fingerprint_re.fullmatch(identity)
                or not isinstance(item, dict)
                or not fingerprint_re.fullmatch(
                    _text(item.get("credential_fingerprint"))
                )
            ):
                raise OAuthOwnershipConflict(
                    "OAuth 凭据所有权登记已损坏，已停止导出"
                )
            try:
                normalize_target_instance(item.get("target_instance"))
            except ValueError as exc:
                raise OAuthOwnershipConflict(
                    "OAuth 凭据所有权登记已损坏，已停止导出"
                ) from exc
        return {"version": OWNERSHIP_VERSION, "items": items}

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
            except Exception:
                pass

    def _evaluate(
        self, entries: Iterable[dict], target_instance: str, payload: dict
    ) -> tuple[dict, list[dict]]:
        target = normalize_target_instance(target_instance)
        selected, skipped = select_latest_credentials(entries)
        items = payload.get("items") or {}
        credential_owners: dict[str, str] = {}
        for stored_identity, stored in items.items():
            credential = _text(stored.get("credential_fingerprint"))
            previous_identity = credential_owners.get(credential)
            if previous_identity and previous_identity != stored_identity:
                raise OAuthOwnershipConflict(
                    "OAuth 凭据所有权登记存在重复 refresh token，已停止导出"
                )
            credential_owners[credential] = stored_identity
        summary = {
            "target_instance": target,
            "total": len(selected),
            "duplicates_skipped": skipped,
            "invalid": 0,
            "legacy_untracked": 0,
            "unclaimed": 0,
            "owned_by_target": 0,
            "refreshed_for_target": 0,
            "credential_conflicts": 0,
            "transfer_required": 0,
        }
        batch_credentials: dict[str, str] = {}
        for entry in selected:
            identity = identity_fingerprint(entry)
            credential = credential_fingerprint(entry)
            if not identity or not credential or not _text(
                entry.get("refresh_token")
            ):
                summary["invalid"] += 1
                continue
            if not _has_tracked_authorization(entry):
                summary["legacy_untracked"] += 1
                continue
            batch_identity = batch_credentials.get(credential)
            stored_identity = credential_owners.get(credential)
            if (
                (batch_identity and batch_identity != identity)
                or (stored_identity and stored_identity != identity)
            ):
                summary["credential_conflicts"] += 1
                continue
            batch_credentials[credential] = identity
            current = items.get(identity)
            if not isinstance(current, dict):
                summary["unclaimed"] += 1
                continue
            same_target = _text(current.get("target_instance")) == target
            same_credential = (
                _text(current.get("credential_fingerprint")) == credential
            )
            if same_target and same_credential:
                summary["owned_by_target"] += 1
            elif same_target:
                summary["refreshed_for_target"] += 1
            elif same_credential:
                summary["credential_conflicts"] += 1
            else:
                summary["transfer_required"] += 1
        summary["can_export"] = (
            summary["invalid"] == 0
            and summary["legacy_untracked"] == 0
            and summary["credential_conflicts"] == 0
            and summary["transfer_required"] == 0
            and summary["total"] > 0
        )
        return summary, selected

    def preflight(
        self, entries: Iterable[dict], target_instance: str
    ) -> dict:
        with _REGISTRY_GUARD:
            payload = self._load()
            summary, _selected = self._evaluate(
                entries, target_instance, payload
            )
            return summary

    def claim(
        self,
        entries: Iterable[dict],
        target_instance: str,
        *,
        acknowledge_previous_instance_disabled: bool = False,
    ) -> dict:
        target = normalize_target_instance(target_instance)
        with _REGISTRY_GUARD:
            lock = InterProcessFileLock(self.lock_path)
            if not lock.acquire(blocking=False):
                raise OAuthOwnershipConflict(
                    "另一个程序实例正在生成或导出 OAuth 凭据，请稍后重试"
                )
            try:
                payload = self._load()
                summary, selected = self._evaluate(entries, target, payload)
                if summary["total"] == 0:
                    raise OAuthOwnershipConflict(
                        "当前没有可导出的 OAuth 凭据"
                    )
                if summary["invalid"]:
                    raise OAuthOwnershipConflict(
                        "存在缺少 refresh token 的账号，请先重新生成账号授权"
                    )
                if summary["legacy_untracked"]:
                    raise OAuthOwnershipConflict(
                        "存在升级前生成且未登记授权代次的 CPA；"
                        "为避免复用可能已在其他实例轮换过的 refresh token，"
                        "请先重新生成账号授权"
                    )
                if summary["credential_conflicts"]:
                    raise OAuthOwnershipConflict(
                        "同一 refresh token 已声明给其他身份或实例；"
                        "请先重新生成账号授权，不能重复或跨实例使用"
                    )
                if (
                    summary["transfer_required"]
                    and not acknowledge_previous_instance_disabled
                ):
                    raise OAuthOwnershipConflict(
                        "目标实例发生变化；请先停用旧实例中的对应账号，"
                        "再确认所有权迁移"
                    )

                items = payload.setdefault("items", {})
                claimed = 0
                transferred = 0
                now = _utc_now()
                for entry in selected:
                    identity = identity_fingerprint(entry)
                    credential = credential_fingerprint(entry)
                    if not identity or not credential:
                        continue
                    previous = items.get(identity)
                    previous_target = (
                        _text(previous.get("target_instance"))
                        if isinstance(previous, dict)
                        else ""
                    )
                    previous_credential = (
                        _text(previous.get("credential_fingerprint"))
                        if isinstance(previous, dict)
                        else ""
                    )
                    if not previous:
                        claimed += 1
                    elif (
                        previous_target != target
                        and previous_credential != credential
                    ):
                        transferred += 1
                    elif previous_credential != credential:
                        claimed += 1
                    items[identity] = {
                        "target_instance": target,
                        "credential_fingerprint": credential,
                        "authorization_id": _text(
                            entry.get("_authorization_id")
                        ),
                        "authorization_generation": int(
                            entry.get("_authorization_generation") or 0
                        ),
                        "claimed_at": now,
                    }
                payload["version"] = OWNERSHIP_VERSION
                payload["updated_at"] = now
                self._write(payload)
                summary["claimed"] = claimed
                summary["transferred"] = transferred
                summary["can_export"] = True
                return summary
            finally:
                lock.release()


def sub2_ownership_extra(entry: dict, target_instance: str) -> dict:
    target = normalize_target_instance(target_instance)
    return {
        "grok_register_owner_instance": target,
        "grok_register_authorization_id": _text(
            entry.get("_authorization_id")
        ),
        "grok_register_authorization_generation": int(
            entry.get("_authorization_generation") or 0
        ),
        "grok_register_authorized_at": _text(entry.get("_authorized_at"))
        or _text(entry.get("last_refresh")),
    }
