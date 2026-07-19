from __future__ import annotations

from pathlib import Path

import pytest

from credential_store import (
    CredentialLayout,
    create_worker_output_paths,
    ensure_layout,
    normalize_credentials_setting,
)


def test_default_directory_resolves_under_app_root(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()

    layout = CredentialLayout.from_config(app_root, {})

    assert layout.root == (app_root / "data" / "credentials").resolve()
    assert layout.sso_dir == layout.root / "sso"
    assert layout.mail_dir == layout.root / "mail"
    assert layout.cpa_dir == layout.root / "cpa"


def test_relative_directory_resolves_under_app_root(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()

    layout = CredentialLayout.from_config(
        app_root, {"credentials_dir": "private/vault"}
    )

    assert layout.root == (app_root / "private" / "vault").resolve()


@pytest.mark.parametrize("configured", [".", ""])
def test_rejects_app_root_as_credentials_directory(tmp_path, configured):
    app_root = tmp_path / "app"
    app_root.mkdir()

    if configured == "":
        configured = str(app_root)

    with pytest.raises(ValueError, match="应用根目录"):
        CredentialLayout.from_config(app_root, {"credentials_dir": configured})


def test_rejects_filesystem_root_as_credentials_directory(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    filesystem_root = Path(app_root.anchor)

    with pytest.raises(ValueError, match="文件系统根目录"):
        CredentialLayout.from_config(
            app_root, {"credentials_dir": str(filesystem_root)}
        )


def test_serializes_internal_path_as_relative_and_external_as_absolute(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    internal = app_root / "private" / "vault"
    external = tmp_path / "external-vault"

    assert normalize_credentials_setting(app_root, str(internal)) == str(
        Path("private") / "vault"
    )
    assert normalize_credentials_setting(app_root, str(external)) == str(
        external.resolve()
    )


def test_ensure_layout_creates_all_subdirectories(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    layout = CredentialLayout.from_config(
        app_root, {"credentials_dir": "data/credentials"}
    )

    result = ensure_layout(layout)

    assert result is layout
    assert layout.root.is_dir()
    assert layout.sso_dir.is_dir()
    assert layout.mail_dir.is_dir()
    assert layout.cpa_dir.is_dir()


def test_ensure_layout_rejects_existing_file(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    target = app_root / "not-a-directory"
    target.write_text("occupied", encoding="utf-8")
    layout = CredentialLayout.from_config(
        app_root, {"credentials_dir": str(target)}
    )

    with pytest.raises(ValueError, match="不是目录"):
        ensure_layout(layout)


def test_worker_output_paths_are_unique_and_stay_in_worker_subdirectories(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    layout = ensure_layout(CredentialLayout.from_config(app_root, {}))

    first = create_worker_output_paths(
        layout,
        worker_id=2,
        pid=3456,
        timestamp="20260719_160000",
        nonce="aaaa1111",
    )
    second = create_worker_output_paths(
        layout,
        worker_id=3,
        pid=3456,
        timestamp="20260719_160000",
        nonce="bbbb2222",
    )

    assert first.sso_file.parent == layout.sso_dir
    assert first.mail_file.parent == layout.mail_dir
    assert "_w2_3456_aaaa1111" in first.sso_file.name
    assert "_w2_3456_aaaa1111" in first.mail_file.name
    assert first.sso_file != second.sso_file
    assert first.mail_file != second.mail_file
