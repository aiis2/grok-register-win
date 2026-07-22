from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence


class AccountQueryError(ValueError):
    """Raised when a public account catalog query is invalid."""


@dataclass(frozen=True)
class _AccountRecord:
    email: str
    source: str
    source_mtime: str
    source_mtime_ns: int
    line_index: int
    sso_fingerprint: str


@dataclass(frozen=True)
class _SourceRecord:
    name: str
    count: int
    mtime: str
    mtime_ns: int


@dataclass(frozen=True)
class _Snapshot:
    accounts: tuple[_AccountRecord, ...]
    sources: tuple[_SourceRecord, ...]


def _default_read_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return text.splitlines()


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


class AccountCatalog:
    """Caches a credential-free projection of local account batch files."""

    _PAGE_SIZES = {25, 50, 100}
    _STATUSES = {"all", "ready", "pending"}
    _SORTS = {"newest", "oldest", "email"}

    def __init__(
        self,
        *,
        fingerprint: Callable[[str], str],
        read_lines: Callable[[Path], list[str]] | None = None,
    ) -> None:
        self._fingerprint = fingerprint
        self._read_lines = read_lines or _default_read_lines
        self._lock = threading.Lock()
        self._signature: tuple[tuple[str, int, int], ...] | None = None
        self._snapshot = _Snapshot(accounts=(), sources=())

    def invalidate(self) -> None:
        with self._lock:
            self._signature = None
            self._snapshot = _Snapshot(accounts=(), sources=())

    @staticmethod
    def _file_signature(files: Sequence[Path]) -> tuple[tuple[str, int, int], ...]:
        signature = []
        for path in files:
            stat = path.stat()
            signature.append((str(path.resolve()), stat.st_size, stat.st_mtime_ns))
        return tuple(sorted(signature))

    def _build_snapshot(self, files: Sequence[Path]) -> _Snapshot:
        source_rows: list[_SourceRecord] = []
        account_rows: list[_AccountRecord] = []
        ordered_files = sorted(
            files,
            key=lambda path: (-path.stat().st_mtime_ns, path.name.casefold()),
        )
        seen_accounts: set[str] = set()

        for path in ordered_files:
            stat = path.stat()
            mtime = _iso_mtime(path)
            lines = [line.strip() for line in self._read_lines(path) if line.strip()]
            source_rows.append(
                _SourceRecord(
                    name=path.name,
                    count=len(lines),
                    mtime=mtime,
                    mtime_ns=stat.st_mtime_ns,
                )
            )
            for line_index, line in enumerate(lines):
                parts = line.split("----")
                email = str(parts[0] if parts else "").strip().casefold()
                sso = "----".join(parts[2:]).strip() if len(parts) >= 3 else ""
                fingerprint = self._fingerprint(sso) if sso else ""
                identity = email or fingerprint
                if not identity or identity in seen_accounts:
                    continue
                seen_accounts.add(identity)
                account_rows.append(
                    _AccountRecord(
                        email=email,
                        source=path.name,
                        source_mtime=mtime,
                        source_mtime_ns=stat.st_mtime_ns,
                        line_index=line_index,
                        sso_fingerprint=fingerprint,
                    )
                )

        return _Snapshot(accounts=tuple(account_rows), sources=tuple(source_rows))

    def _get_snapshot(self, files: Iterable[Path]) -> _Snapshot:
        paths = tuple(Path(path) for path in files)
        signature = self._file_signature(paths)
        with self._lock:
            if signature != self._signature:
                self._snapshot = self._build_snapshot(paths)
                self._signature = signature
            return self._snapshot

    @classmethod
    def _validate_query(
        cls,
        *,
        page: int,
        page_size: int,
        q: str,
        source: str,
        status: str,
        sort: str,
        available_sources: set[str],
    ) -> None:
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise AccountQueryError("page must be an integer greater than or equal to 1")
        if page_size not in cls._PAGE_SIZES:
            raise AccountQueryError("page_size must be one of 25, 50, 100")
        if len(q) > 200:
            raise AccountQueryError("q must not exceed 200 characters")
        if source != "all" and source not in available_sources:
            raise AccountQueryError("source is not available")
        if status not in cls._STATUSES:
            raise AccountQueryError("status must be all, ready, or pending")
        if sort not in cls._SORTS:
            raise AccountQueryError("sort must be newest, oldest, or email")

    def query(
        self,
        files: Iterable[Path],
        completed_fingerprints: set[str],
        *,
        page: int,
        page_size: int,
        q: str,
        source: str,
        status: str,
        sort: str,
    ) -> dict:
        snapshot = self._get_snapshot(files)
        normalized_query = str(q or "").strip().casefold()
        normalized_source = str(source or "all").strip()
        normalized_status = str(status or "all").strip().casefold()
        normalized_sort = str(sort or "newest").strip().casefold()
        source_names = {item.name for item in snapshot.sources}
        self._validate_query(
            page=page,
            page_size=page_size,
            q=normalized_query,
            source=normalized_source,
            status=normalized_status,
            sort=normalized_sort,
            available_sources=source_names,
        )

        projected: list[tuple[_AccountRecord, str]] = []
        for account in snapshot.accounts:
            account_status = (
                "ready"
                if account.sso_fingerprint
                and account.sso_fingerprint in completed_fingerprints
                else "pending"
            )
            if normalized_query and normalized_query not in account.email:
                continue
            if normalized_source != "all" and account.source != normalized_source:
                continue
            if normalized_status != "all" and account_status != normalized_status:
                continue
            projected.append((account, account_status))

        if normalized_sort == "newest":
            projected.sort(
                key=lambda item: (
                    -item[0].source_mtime_ns,
                    item[0].email,
                    item[0].source.casefold(),
                    item[0].line_index,
                )
            )
        elif normalized_sort == "oldest":
            projected.sort(
                key=lambda item: (
                    item[0].source_mtime_ns,
                    item[0].email,
                    item[0].source.casefold(),
                    item[0].line_index,
                )
            )
        else:
            projected.sort(
                key=lambda item: (
                    item[0].email,
                    -item[0].source_mtime_ns,
                    item[0].source.casefold(),
                    item[0].line_index,
                )
            )

        total = len(projected)
        total_pages = math.ceil(total / page_size) if total else 0
        start = (page - 1) * page_size
        page_rows = projected[start : start + page_size]
        return {
            "items": [
                {
                    "email": account.email,
                    "source": account.source,
                    "status": account_status,
                    "source_mtime": account.source_mtime,
                }
                for account, account_status in page_rows
            ],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
            },
            "filters": {
                "sources": [item.name for item in snapshot.sources],
            },
            "files": [
                {
                    "name": item.name,
                    "count": item.count,
                    "mtime": item.mtime,
                }
                for item in snapshot.sources
            ],
        }
