from __future__ import annotations

import os
import secrets
import shutil
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping


DEFAULT_CREDENTIALS_DIR = Path("data") / "credentials"


@dataclass(frozen=True)
class CredentialLayout:
    app_root: Path
    root: Path
    sso_dir: Path
    mail_dir: Path
    cpa_dir: Path
    archive_dir: Path

    @classmethod
    def from_config(
        cls, app_root: Path, config: Mapping[str, object]
    ) -> "CredentialLayout":
        resolved_app_root = Path(app_root).resolve()
        raw_value = str(config.get("credentials_dir") or "").strip()
        configured = Path(raw_value) if raw_value else DEFAULT_CREDENTIALS_DIR
        root = (
            configured.resolve()
            if configured.is_absolute()
            else (resolved_app_root / configured).resolve()
        )
        if root.parent == root:
            raise ValueError("凭据目录不能是文件系统根目录")
        if root == resolved_app_root:
            raise ValueError("凭据目录不能是应用根目录")
        return cls(
            app_root=resolved_app_root,
            root=root,
            sso_dir=root / "sso",
            mail_dir=root / "mail",
            cpa_dir=root / "cpa",
            archive_dir=root / "archive",
        )


@dataclass(frozen=True)
class WorkerOutputPaths:
    sso_file: Path
    mail_file: Path


@dataclass(frozen=True)
class MigrationResult:
    copied: int
    skipped: int
    renamed: int
    removed: int
    warnings: list[str]
    target_setting: str


class CredentialMigrationError(RuntimeError):
    pass


class CredentialImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class CredentialImportResult:
    archived: int
    archive_dir: Path
    account_file: Path


def normalize_credentials_setting(app_root: Path, value: str) -> str:
    layout = CredentialLayout.from_config(
        Path(app_root), {"credentials_dir": str(value or "").strip()}
    )
    try:
        relative = layout.root.relative_to(layout.app_root)
    except ValueError:
        return str(layout.root)
    return str(relative)


def ensure_layout(layout: CredentialLayout) -> CredentialLayout:
    for path in (
        layout.root,
        layout.sso_dir,
        layout.mail_dir,
        layout.cpa_dir,
        layout.archive_dir,
    ):
        if path.exists() and not path.is_dir():
            raise ValueError(f"凭据路径不是目录: {path}")
        path.mkdir(parents=True, exist_ok=True)
    return layout


def _import_archive_destination(directory: Path, name: str) -> Path:
    destination = directory / Path(name).name
    if not destination.exists():
        return destination
    for sequence in range(2, 10000):
        candidate = directory / f"{destination.stem}-{sequence}{destination.suffix}"
        if not candidate.exists():
            return candidate
    raise CredentialImportError("无法为归档文件生成目标名称")


def activate_credential_import(
    layout: CredentialLayout,
    staged_account_file: Path,
    live_account_file: Path,
    archive_sources: Iterable[tuple[str, Path]],
    *,
    batch_id: str,
    timestamp: str | None = None,
    move_file: Callable[[str, str], object] = shutil.move,
    replace_file: Callable[[Path, Path], object] = os.replace,
) -> CredentialImportResult:
    """Archive the current SSO/CPA workspace and atomically activate one batch.

    The caller must hold the application activity, migration, and CPA workspace
    locks.  Only exact files supplied by the caller are moved; no directory is
    recursively removed.
    """

    ensured = ensure_layout(layout)
    root = ensured.root.resolve()
    staged = Path(staged_account_file).resolve()
    live = Path(live_account_file).resolve()
    safe_batch = "".join(ch for ch in str(batch_id or "") if ch.isalnum())[:32]
    if not safe_batch:
        raise CredentialImportError("导入批次标识无效")

    try:
        staged.relative_to(root)
        live.relative_to(ensured.sso_dir.resolve())
    except ValueError as exc:
        raise CredentialImportError("导入路径超出凭据目录") from exc
    if staged.parent.parent != root or not staged.parent.name.startswith(".staging-"):
        raise CredentialImportError("导入暂存路径无效")
    if live.parent != ensured.sso_dir.resolve():
        raise CredentialImportError("账号激活路径无效")
    if not staged.is_file():
        raise CredentialImportError("导入暂存文件不存在")

    batch_name = (
        f"{timestamp or datetime.now().strftime('%Y%m%d_%H%M%S')}_import_{safe_batch[:8]}"
    )
    archive_dir = (ensured.archive_dir / batch_name).resolve()
    try:
        archive_dir.relative_to(ensured.archive_dir.resolve())
    except ValueError as exc:  # pragma: no cover - defensive path invariant
        raise CredentialImportError("导入归档路径无效") from exc

    journal: list[tuple[Path, Path]] = []
    activated = False
    try:
        seen: set[Path] = set()
        for raw_category, raw_source in archive_sources:
            category = str(raw_category or "").strip().lower()
            if category not in {"sso", "cpa"}:
                raise CredentialImportError("导入归档分类无效")
            source = Path(raw_source).resolve()
            if source in seen or not source.is_file():
                continue
            if source == staged or source == live:
                raise CredentialImportError("导入文件不能同时作为归档来源")
            seen.add(source)
            destination_dir = archive_dir / category
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = _import_archive_destination(
                destination_dir, source.name
            )
            move_file(str(source), str(destination))
            journal.append((source, destination))

        live.parent.mkdir(parents=True, exist_ok=True)
        replace_file(staged, live)
        activated = True
        if not live.is_file():
            raise CredentialImportError("导入账号文件激活失败")
    except Exception as exc:
        if activated and live.exists():
            try:
                staged.parent.mkdir(parents=True, exist_ok=True)
                move_file(str(live), str(staged))
            except Exception:
                pass
        for source, destination in reversed(journal):
            try:
                if destination.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    move_file(str(destination), str(source))
            except Exception:
                pass
        if isinstance(exc, CredentialImportError):
            raise
        raise CredentialImportError("导入批次激活失败") from exc

    return CredentialImportResult(
        archived=len(journal),
        archive_dir=archive_dir,
        account_file=live,
    )


def create_worker_output_paths(
    layout: CredentialLayout,
    worker_id: int,
    pid: int | None = None,
    *,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> WorkerOutputPaths:
    ensured = ensure_layout(layout)
    safe_worker_id = max(1, int(worker_id))
    safe_pid = max(1, int(pid or os.getpid()))
    safe_timestamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_nonce = str(nonce or secrets.token_hex(4)).strip()
    suffix = f"{safe_timestamp}_w{safe_worker_id}_{safe_pid}_{safe_nonce}"
    return WorkerOutputPaths(
        sso_file=ensured.sso_dir / f"accounts_{suffix}.txt",
        mail_file=ensured.mail_dir / f"mail_credentials_{suffix}.txt",
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(source: Path, destination: Path) -> bool:
    return file_sha256(source) == file_sha256(destination)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _migration_sources(
    app_root: Path,
    current: CredentialLayout,
    target: CredentialLayout,
) -> list[tuple[Path, Path]]:
    groups: list[tuple[Path, tuple[str, ...], Path]] = []
    if current.root != target.root:
        groups.extend(
            [
                (current.sso_dir, ("accounts_*.txt",), target.sso_dir),
                (
                    current.mail_dir,
                    ("mail_credentials*.txt",),
                    target.mail_dir,
                ),
                (
                    current.cpa_dir,
                    ("xai-*.json", "index.json", "failed.jsonl"),
                    target.cpa_dir,
                ),
            ]
        )
    groups.extend(
        [
            (app_root, ("accounts_*.txt",), target.sso_dir),
            (app_root, ("mail_credentials*.txt",), target.mail_dir),
            (
                app_root / "data" / "cpa",
                ("xai-*.json", "index.json", "failed.jsonl"),
                target.cpa_dir,
            ),
        ]
    )

    sources: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for directory, patterns, destination_dir in groups:
        if not directory.is_dir():
            continue
        for pattern in patterns:
            for source in sorted(directory.glob(pattern), key=lambda path: path.name):
                resolved = source.resolve()
                if not source.is_file() or resolved in seen:
                    continue
                if _is_relative_to(resolved, target.root):
                    continue
                seen.add(resolved)
                sources.append((source, destination_dir))
    if current.root != target.root and current.archive_dir.is_dir():
        for source in sorted(
            current.archive_dir.rglob("*"), key=lambda path: str(path)
        ):
            resolved = source.resolve()
            if not source.is_file() or resolved in seen:
                continue
            relative = source.relative_to(current.archive_dir)
            seen.add(resolved)
            sources.append((source, target.archive_dir / relative.parent))
    return sources


def _conflict_destination(
    source: Path,
    destination_dir: Path,
    conflict_timestamp: str,
) -> tuple[Path, bool, bool]:
    direct = destination_dir / source.name
    if not direct.exists():
        return direct, False, False
    if direct.is_file() and verify_sha256(source, direct):
        return direct, True, False

    base = f"{source.stem}-migrated-{conflict_timestamp}"
    for sequence in range(1, 10000):
        suffix = "" if sequence == 1 else f"-{sequence}"
        candidate = destination_dir / f"{base}{suffix}{source.suffix}"
        if not candidate.exists():
            return candidate, False, True
        if candidate.is_file() and verify_sha256(source, candidate):
            return candidate, True, True
    raise CredentialMigrationError(f"无法为冲突文件生成目标名称: {source.name}")


def migrate_credentials(
    app_root: Path,
    current: CredentialLayout,
    target: CredentialLayout,
    *,
    switch_config: Callable[[str], None],
    verify_file: Callable[[Path, Path], bool] = verify_sha256,
    conflict_timestamp: str | None = None,
) -> MigrationResult:
    resolved_app_root = Path(app_root).resolve()
    if current.root != target.root and (
        _is_relative_to(target.root, current.root)
        or _is_relative_to(current.root, target.root)
    ):
        raise CredentialMigrationError("新旧凭据目录不能互相嵌套")

    ensured_target = ensure_layout(target)
    timestamp = conflict_timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    sources = _migration_sources(
        resolved_app_root, current=current, target=ensured_target
    )
    created: list[Path] = []
    temporary: list[Path] = []
    completed_sources: list[Path] = []
    copied = 0
    skipped = 0
    renamed = 0

    try:
        for source, destination_dir in sources:
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination, identical, conflict = _conflict_destination(
                source, destination_dir, timestamp
            )
            if conflict:
                renamed += 1
            if identical:
                skipped += 1
                completed_sources.append(source)
                continue

            temp_path = destination.with_name(
                f".{destination.name}.migrate-{secrets.token_hex(4)}.tmp"
            )
            temporary.append(temp_path)
            shutil.copy2(source, temp_path)
            if not verify_file(source, temp_path):
                raise CredentialMigrationError(
                    f"SHA-256 校验失败，迁移已取消: {source.name}"
                )
            os.replace(temp_path, destination)
            temporary.remove(temp_path)
            created.append(destination)
            completed_sources.append(source)
            copied += 1

        target_setting = normalize_credentials_setting(
            resolved_app_root, str(ensured_target.root)
        )
        switch_config(target_setting)
    except Exception as exc:
        for temp_path in temporary:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
        for destination in reversed(created):
            try:
                destination.unlink(missing_ok=True)
            except Exception:
                pass
        if isinstance(exc, CredentialMigrationError):
            raise
        raise CredentialMigrationError(f"凭据迁移失败: {type(exc).__name__}") from exc

    warnings: list[str] = []
    removed = 0
    for source in completed_sources:
        try:
            source.unlink()
            removed += 1
        except Exception:
            warnings.append(f"未能删除源文件: {source.name}")

    cleanup_candidates = [
        current.sso_dir,
        current.mail_dir,
        current.cpa_dir,
        *sorted(
            (path for path in current.archive_dir.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ),
        current.archive_dir,
        current.root,
        resolved_app_root / "data" / "cpa",
    ]
    for directory in cleanup_candidates:
        resolved_directory = directory.resolve()
        if (
            resolved_directory == ensured_target.root
            or _is_relative_to(resolved_directory, ensured_target.root)
            or _is_relative_to(ensured_target.root, resolved_directory)
        ):
            continue
        try:
            directory.rmdir()
        except Exception:
            pass

    return MigrationResult(
        copied=copied,
        skipped=skipped,
        renamed=renamed,
        removed=removed,
        warnings=warnings,
        target_setting=target_setting,
    )
