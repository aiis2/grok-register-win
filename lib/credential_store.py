from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping


DEFAULT_CREDENTIALS_DIR = Path("data") / "credentials"


@dataclass(frozen=True)
class CredentialLayout:
    app_root: Path
    root: Path
    sso_dir: Path
    mail_dir: Path
    cpa_dir: Path

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
        )


@dataclass(frozen=True)
class WorkerOutputPaths:
    sso_file: Path
    mail_file: Path


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
    for path in (layout.root, layout.sso_dir, layout.mail_dir, layout.cpa_dir):
        if path.exists() and not path.is_dir():
            raise ValueError(f"凭据路径不是目录: {path}")
        path.mkdir(parents=True, exist_ok=True)
    return layout


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
