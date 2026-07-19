from __future__ import annotations

from pathlib import Path

import pytest

from credential_store import (
    CredentialMigrationError,
    CredentialLayout,
    create_worker_output_paths,
    ensure_layout,
    migrate_credentials,
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


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_migration_moves_current_and_legacy_credentials_after_verified_switch(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    current = ensure_layout(
        CredentialLayout.from_config(app_root, {"credentials_dir": "old-vault"})
    )
    target = CredentialLayout.from_config(
        app_root, {"credentials_dir": "new-vault"}
    )
    sources = [
        _write(current.sso_dir / "accounts_current.txt", "current-sso"),
        _write(current.mail_dir / "mail_credentials_current.txt", "current-mail"),
        _write(current.cpa_dir / "xai-current.json", "current-cpa"),
        _write(app_root / "accounts_legacy.txt", "legacy-sso"),
        _write(app_root / "mail_credentials.txt", "legacy-mail"),
        _write(app_root / "data" / "cpa" / "xai-legacy.json", "legacy-cpa"),
    ]
    switch_observations = []

    def switch(setting):
        switch_observations.append(
            {
                "setting": setting,
                "sources_exist": all(path.exists() for path in sources),
                "target_files": sorted(
                    path.name for path in target.root.rglob("*") if path.is_file()
                ),
            }
        )

    result = migrate_credentials(
        app_root,
        current,
        target,
        switch_config=switch,
        conflict_timestamp="20260719_170000",
    )

    assert result.copied == 6
    assert result.skipped == 0
    assert result.renamed == 0
    assert result.warnings == []
    assert switch_observations == [
        {
            "setting": str(Path("new-vault")),
            "sources_exist": True,
            "target_files": [
                "accounts_current.txt",
                "accounts_legacy.txt",
                "mail_credentials.txt",
                "mail_credentials_current.txt",
                "xai-current.json",
                "xai-legacy.json",
            ],
        }
    ]
    assert all(not path.exists() for path in sources)


def test_migration_skips_identical_target_and_removes_source(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    current = ensure_layout(
        CredentialLayout.from_config(app_root, {"credentials_dir": "old"})
    )
    target = ensure_layout(
        CredentialLayout.from_config(app_root, {"credentials_dir": "new"})
    )
    source = _write(current.sso_dir / "accounts_same.txt", "same")
    destination = _write(target.sso_dir / source.name, "same")

    result = migrate_credentials(
        app_root, current, target, switch_config=lambda setting: None
    )

    assert result.copied == 0
    assert result.skipped == 1
    assert result.renamed == 0
    assert destination.read_text(encoding="utf-8") == "same"
    assert not source.exists()


def test_migration_renames_conflicting_target_without_overwrite(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    current = ensure_layout(
        CredentialLayout.from_config(app_root, {"credentials_dir": "old"})
    )
    target = ensure_layout(
        CredentialLayout.from_config(app_root, {"credentials_dir": "new"})
    )
    source = _write(current.sso_dir / "accounts_same.txt", "new-content")
    existing = _write(target.sso_dir / source.name, "old-content")

    result = migrate_credentials(
        app_root,
        current,
        target,
        switch_config=lambda setting: None,
        conflict_timestamp="20260719_170000",
    )

    migrated = target.sso_dir / "accounts_same-migrated-20260719_170000.txt"
    assert result.copied == 1
    assert result.renamed == 1
    assert existing.read_text(encoding="utf-8") == "old-content"
    assert migrated.read_text(encoding="utf-8") == "new-content"
    assert not source.exists()


def test_verification_failure_rolls_back_new_targets_and_keeps_sources(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    current = ensure_layout(
        CredentialLayout.from_config(app_root, {"credentials_dir": "old"})
    )
    target = CredentialLayout.from_config(
        app_root, {"credentials_dir": "new"}
    )
    first = _write(current.sso_dir / "accounts_first.txt", "first")
    second = _write(current.mail_dir / "mail_credentials_second.txt", "second")
    switches = []
    checks = []

    def fail_second(source, destination):
        checks.append(source.name)
        return source.name != second.name

    with pytest.raises(CredentialMigrationError, match="SHA-256"):
        migrate_credentials(
            app_root,
            current,
            target,
            switch_config=switches.append,
            verify_file=fail_second,
        )

    assert checks == [first.name, second.name]
    assert switches == []
    assert first.exists()
    assert second.exists()
    assert not any(path.is_file() for path in target.root.rglob("*"))


def test_source_delete_failure_becomes_warning_after_config_switch(
    tmp_path, monkeypatch
):
    app_root = tmp_path / "app"
    app_root.mkdir()
    current = ensure_layout(
        CredentialLayout.from_config(app_root, {"credentials_dir": "old"})
    )
    target = CredentialLayout.from_config(
        app_root, {"credentials_dir": "new"}
    )
    source = _write(current.sso_dir / "accounts_locked.txt", "locked")
    original_unlink = Path.unlink

    def fail_source_unlink(path, *args, **kwargs):
        if path == source:
            raise PermissionError("locked by another process")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_source_unlink)
    switches = []

    result = migrate_credentials(
        app_root, current, target, switch_config=switches.append
    )

    assert switches == [str(Path("new"))]
    assert source.exists()
    assert result.removed == 0
    assert len(result.warnings) == 1
    assert "accounts_locked.txt" in result.warnings[0]
    assert "locked by another process" not in result.warnings[0]


def test_migration_rejects_nested_source_and_target_directories(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    current = ensure_layout(
        CredentialLayout.from_config(app_root, {"credentials_dir": "vault"})
    )
    target = CredentialLayout.from_config(
        app_root, {"credentials_dir": "vault/nested"}
    )
    _write(current.sso_dir / "accounts_test.txt", "secret")
    switches = []

    with pytest.raises(CredentialMigrationError, match="不能互相嵌套"):
        migrate_credentials(
            app_root, current, target, switch_config=switches.append
        )

    assert switches == []


def test_cleanup_never_removes_a_target_subdirectory(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    current = ensure_layout(
        CredentialLayout.from_config(app_root, {"credentials_dir": "old"})
    )
    target = CredentialLayout.from_config(
        app_root, {"credentials_dir": "data"}
    )

    migrate_credentials(app_root, current, target, switch_config=lambda value: None)

    assert target.root.is_dir()
    assert target.sso_dir.is_dir()
    assert target.mail_dir.is_dir()
    assert target.cpa_dir.is_dir()
