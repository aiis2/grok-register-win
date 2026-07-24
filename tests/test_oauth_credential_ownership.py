from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest


def ownership_module():
    return importlib.import_module("lib.oauth_credential_ownership")


def authorized_entry(
    *,
    sub: str,
    refresh_token: str,
    authorization_id: str,
    generation: int = 1,
    authorized_at: str = "2026-07-23T01:00:00Z",
) -> dict:
    return {
        "sub": sub,
        "refresh_token": refresh_token,
        "_authorization_id": authorization_id,
        "_authorization_generation": generation,
        "_authorized_at": authorized_at,
    }


def test_latest_credential_selection_keeps_one_record_per_stable_identity():
    ownership = ownership_module()
    entries = [
        {
            "email": "one@example.com",
            "sub": "identity-one",
            "refresh_token": "refresh-old",
            "_authorized_at": "2026-07-23T01:00:00Z",
        },
        {
            "email": "renamed@example.com",
            "sub": "identity-one",
            "refresh_token": "refresh-new",
            "_authorized_at": "2026-07-23T02:00:00Z",
        },
        {
            "email": "two@example.com",
            "sub": "identity-two",
            "refresh_token": "refresh-two",
            "_authorized_at": "2026-07-23T01:30:00Z",
        },
    ]

    selected, skipped = ownership.select_latest_credentials(entries)

    assert skipped == 1
    assert len(selected) == 2
    assert {
        item["refresh_token"] for item in selected
    } == {"refresh-new", "refresh-two"}


def test_authorization_metadata_increments_generation_without_exposing_token():
    ownership = ownership_module()
    previous = {
        "email": "one@example.com",
        "sub": "identity-one",
        "refresh_token": "refresh-old",
        "_authorization_generation": 4,
    }
    fresh = {
        "email": "one@example.com",
        "sub": "identity-one",
        "refresh_token": "refresh-new",
    }

    stamped = ownership.stamp_authorization(
        fresh,
        previous=previous,
        authorized_at="2026-07-23T03:00:00Z",
        authorization_id="authorization-test-id",
    )

    assert stamped["_authorization_generation"] == 5
    assert stamped["_authorization_id"] == "authorization-test-id"
    assert stamped["_authorized_at"] == "2026-07-23T03:00:00Z"
    serialized_metadata = json.dumps(
        {
            key: value
            for key, value in stamped.items()
            if key.startswith("_authorization") or key == "_authorized_at"
        }
    )
    assert "refresh-new" not in serialized_metadata


def test_one_refresh_token_cannot_be_claimed_by_two_target_instances(tmp_path):
    ownership = ownership_module()
    registry = ownership.OAuthCredentialOwnershipRegistry(
        tmp_path / "oauth_ownership.json"
    )
    current = [
        {
            "email": "one@example.com",
            **authorized_entry(
                sub="identity-one",
                refresh_token="refresh-current",
                authorization_id="authorization-one",
            ),
        }
    ]

    first = registry.claim(current, "sub2api-primary")
    repeat = registry.claim(current, "sub2api-primary")
    conflict = registry.preflight(current, "sub2api-secondary")

    assert first["claimed"] == 1
    assert repeat["owned_by_target"] == 1
    assert conflict["credential_conflicts"] == 1
    assert conflict["can_export"] is False
    with pytest.raises(ownership.OAuthOwnershipConflict, match="重新生成账号授权"):
        registry.claim(current, "sub2api-secondary")

    raw_registry = (tmp_path / "oauth_ownership.json").read_text(
        encoding="utf-8"
    )
    assert "refresh-current" not in raw_registry
    assert "one@example.com" not in raw_registry


def test_reauthorized_identity_requires_previous_instance_shutdown_ack(tmp_path):
    ownership = ownership_module()
    registry = ownership.OAuthCredentialOwnershipRegistry(
        tmp_path / "oauth_ownership.json"
    )
    previous = [
        authorized_entry(
            sub="identity-one",
            refresh_token="refresh-old",
            authorization_id="authorization-old",
        )
    ]
    fresh = [
        authorized_entry(
            sub="identity-one",
            refresh_token="refresh-new",
            authorization_id="authorization-new",
            generation=2,
            authorized_at="2026-07-23T02:00:00Z",
        )
    ]
    registry.claim(previous, "sub2api-primary")

    preflight = registry.preflight(fresh, "sub2api-secondary")

    assert preflight["transfer_required"] == 1
    assert preflight["credential_conflicts"] == 0
    assert preflight["can_export"] is False
    with pytest.raises(ownership.OAuthOwnershipConflict, match="停用"):
        registry.claim(fresh, "sub2api-secondary")

    transferred = registry.claim(
        fresh,
        "sub2api-secondary",
        acknowledge_previous_instance_disabled=True,
    )
    assert transferred["transferred"] == 1
    assert registry.preflight(fresh, "sub2api-secondary")["can_export"] is True


def test_same_refresh_token_cannot_hide_behind_a_different_identity(tmp_path):
    ownership = ownership_module()
    registry = ownership.OAuthCredentialOwnershipRegistry(
        tmp_path / "oauth_ownership.json"
    )
    registry.claim(
        [
            authorized_entry(
                sub="identity-one",
                refresh_token="shared-refresh-token",
                authorization_id="authorization-one",
            )
        ],
        "sub2api-primary",
    )

    disguised = [
        authorized_entry(
            sub="identity-two",
            refresh_token="shared-refresh-token",
            authorization_id="authorization-two",
        )
    ]

    preflight = registry.preflight(disguised, "sub2api-secondary")
    assert preflight["credential_conflicts"] == 1
    assert preflight["can_export"] is False
    with pytest.raises(ownership.OAuthOwnershipConflict, match="refresh token"):
        registry.claim(disguised, "sub2api-secondary")


def test_corrupt_ownership_registry_fails_closed_without_overwrite(tmp_path):
    ownership = ownership_module()
    path = tmp_path / "oauth_ownership.json"
    path.write_text("{not-valid-json", encoding="utf-8")
    registry = ownership.OAuthCredentialOwnershipRegistry(path)

    with pytest.raises(ownership.OAuthOwnershipConflict, match="损坏"):
        registry.claim(
            [
                authorized_entry(
                    sub="identity-one",
                    refresh_token="refresh-one",
                    authorization_id="authorization-one",
                )
            ],
            "sub2api-primary",
        )

    assert path.read_text(encoding="utf-8") == "{not-valid-json"


def test_legacy_untracked_credentials_must_be_reauthorized_before_export(
    tmp_path,
):
    ownership = ownership_module()
    registry = ownership.OAuthCredentialOwnershipRegistry(
        tmp_path / "oauth_ownership.json"
    )
    legacy = [
        {
            "sub": "identity-one",
            "refresh_token": "possibly-used-refresh-token",
        }
    ]

    preflight = registry.preflight(legacy, "sub2api-primary")

    assert preflight["legacy_untracked"] == 1
    assert preflight["unclaimed"] == 0
    assert preflight["can_export"] is False
    with pytest.raises(
        ownership.OAuthOwnershipConflict,
        match="重新生成账号授权",
    ):
        registry.claim(legacy, "sub2api-primary")
    assert not (tmp_path / "oauth_ownership.json").exists()


def test_interprocess_lock_blocks_second_owner_until_release(tmp_path):
    ownership = ownership_module()
    lock_path = tmp_path / "oauth-operation.lock"
    first = ownership.InterProcessFileLock(lock_path)
    second = ownership.InterProcessFileLock(lock_path)

    assert first.acquire(blocking=False) is True
    assert second.acquire(blocking=False) is False
    first.release()
    assert second.acquire(blocking=False) is True
    second.release()


def test_interprocess_lock_epoch_persists_across_owners(tmp_path):
    ownership = ownership_module()
    lock_path = tmp_path / "oauth-operation.lock"
    first = ownership.InterProcessFileLock(lock_path)
    second = ownership.InterProcessFileLock(lock_path)

    assert first.acquire(blocking=False) is True
    assert first.epoch() == 0
    assert first.bump_epoch() == 1
    first.release()
    assert ownership.interprocess_lock_epoch(lock_path) == 1

    assert second.acquire(blocking=False) is True
    assert second.epoch() == 1
    assert second.bump_epoch() == 2
    second.release()
    assert ownership.interprocess_lock_epoch(lock_path) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows long-path alias")
def test_interprocess_lock_treats_windows_long_path_alias_as_same_file(
    tmp_path,
):
    ownership = ownership_module()
    normal_path = tmp_path / "oauth-operation.lock"
    extended_path = Path("\\\\?\\" + str(normal_path))
    first = ownership.InterProcessFileLock(normal_path)
    alias = ownership.InterProcessFileLock(extended_path)

    assert first.acquire(blocking=False) is True
    try:
        assert alias.acquire(blocking=False) is False
    finally:
        first.release()
