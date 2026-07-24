import json

import pytest

from lib.disabled_account_pool import (
    DisabledAccountPool,
    DisabledAccountPoolError,
    is_access_denied_error,
)


def _account(
    email: str = "denied@example.com",
    *,
    sso: str = "web-sso-denied",
    subject: str = "subject-denied",
    password: str = "password-secret",
    source: str = "accounts_batch.txt",
) -> dict:
    return {
        "email": email,
        "password": password,
        "sso": sso,
        "subject": subject,
        "source": source,
        "raw": f"{email}----{password}----{sso}",
    }


def test_empty_pool_has_no_records_or_matches(tmp_path):
    pool = DisabledAccountPool(tmp_path / "disabled")

    assert pool.list_public() == []
    assert pool.identity_sets() == (set(), set(), set())
    assert pool.matches(email="active@example.com", sso="active-sso") is False


def test_disable_persists_identity_and_public_projection_is_secret_free(tmp_path):
    directory = tmp_path / "disabled"
    pool = DisabledAccountPool(directory)

    record = pool.disable(
        _account(),
        RuntimeError("consent failed: Access denied"),
    )

    assert record["reason"] == "access_denied"
    assert record["raw"].endswith("password-secret----web-sso-denied")
    assert pool.matches(email="DENIED@example.com") is True
    assert pool.matches(subject="subject-denied") is True
    assert pool.matches(sso="web-sso-denied") is True

    public = pool.list_public()
    assert len(public) == 1
    assert public[0]["id"] == record["id"]
    serialized = json.dumps(public, ensure_ascii=False)
    assert "password-secret" not in serialized
    assert "web-sso-denied" not in serialized
    assert "raw" not in public[0]

    payload = json.loads((directory / "accounts.json").read_text("utf-8"))
    assert payload["version"] == 1
    assert payload["accounts"][record["id"]]["email"] == "denied@example.com"


def test_repeated_disable_merges_aliases_without_duplicate_record(tmp_path):
    pool = DisabledAccountPool(tmp_path / "disabled")
    original = pool.disable(
        _account(subject="", sso="old-sso"),
        "Access denied",
    )

    updated = pool.disable(
        _account(subject="stable-subject", sso="new-sso"),
        "error=access_denied",
    )

    assert updated["id"] == original["id"]
    assert len(pool.list_public()) == 1
    assert pool.matches(sso="old-sso") is True
    assert pool.matches(sso="new-sso") is True
    assert pool.matches(subject="stable-subject") is True


def test_restore_returns_internal_record_and_removes_match(tmp_path):
    pool = DisabledAccountPool(tmp_path / "disabled")
    disabled = pool.disable(_account(), "Access denied")

    restored = pool.restore(disabled["id"])

    assert restored["raw"].endswith("password-secret----web-sso-denied")
    assert pool.list_public() == []
    assert pool.matches(email="denied@example.com") is False
    with pytest.raises(KeyError):
        pool.restore(disabled["id"])


def test_put_restores_record_after_failed_external_operation(tmp_path):
    pool = DisabledAccountPool(tmp_path / "disabled")
    disabled = pool.disable(_account(), "Access denied")
    restored = pool.restore(disabled["id"])

    pool.put(restored)

    assert pool.matches(email="denied@example.com") is True
    assert pool.list_public()[0]["id"] == disabled["id"]


def test_two_pool_instances_update_without_losing_existing_records(tmp_path):
    directory = tmp_path / "disabled"
    first = DisabledAccountPool(directory)
    second = DisabledAccountPool(directory)

    first.disable(
        _account(email="one@example.com", subject="sub-one", sso="sso-one"),
        "Access denied",
    )
    second.disable(
        _account(email="two@example.com", subject="sub-two", sso="sso-two"),
        "Access denied",
    )

    assert {
        item["email"] for item in DisabledAccountPool(directory).list_public()
    } == {"one@example.com", "two@example.com"}


def test_corrupt_registry_is_never_replaced_with_empty_pool(tmp_path):
    directory = tmp_path / "disabled"
    directory.mkdir(parents=True)
    path = directory / "accounts.json"
    path.write_text("{not-json", encoding="utf-8")
    before = path.read_bytes()
    pool = DisabledAccountPool(directory)

    with pytest.raises(DisabledAccountPoolError, match="损坏"):
        pool.disable(_account(), "Access denied")

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("consent 失败: Access denied", True),
        ("callback?error=access_denied&state=abc", True),
        ("ERROR_DESCRIPTION=Access%20Denied", True),
        (RuntimeError("Access_denied"), True),
        ("401 Client Error: Unauthorized", False),
        ("token http 403", False),
        ("consent 响应缺少 code", False),
        ("Cloudflare challenge", False),
        ("request timeout", False),
    ],
)
def test_access_denied_classifier_is_account_specific(error, expected):
    assert is_access_denied_error(error) is expected
