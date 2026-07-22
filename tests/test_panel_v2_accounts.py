from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path

import pytest

from panel import app as panel_app


def _catalog_api():
    module = importlib.import_module("panel.account_catalog")
    return module.AccountCatalog, module.AccountQueryError


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_accounts(path: Path, lines: list[str], *, mtime_ns: int) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_catalog_reuses_unchanged_snapshot_and_rebuilds_after_file_change(tmp_path):
    AccountCatalog, _ = _catalog_api()
    account_file = tmp_path / "accounts_one.txt"
    _write_accounts(
        account_file,
        ["one@example.com----password-one----sso-one"],
        mtime_ns=1_700_000_000_000_000_000,
    )
    reads: list[Path] = []

    def read_lines(path: Path) -> list[str]:
        reads.append(path)
        return path.read_text(encoding="utf-8").splitlines()

    catalog = AccountCatalog(fingerprint=_fingerprint, read_lines=read_lines)
    query = dict(
        page=1,
        page_size=25,
        q="",
        source="all",
        status="all",
        sort="newest",
    )

    first = catalog.query([account_file], set(), **query)
    second = catalog.query([account_file], set(), **query)

    assert first == second
    assert reads == [account_file]

    _write_accounts(
        account_file,
        [
            "one@example.com----password-one----sso-one",
            "two@example.com----password-two----sso-two",
        ],
        mtime_ns=1_700_000_000_100_000_000,
    )
    changed = catalog.query([account_file], set(), **query)

    assert changed["pagination"]["total"] == 2
    assert reads == [account_file, account_file]


def test_catalog_deduplicates_to_newest_source_and_never_serializes_secrets(tmp_path):
    AccountCatalog, _ = _catalog_api()
    older = tmp_path / "accounts_older.txt"
    newer = tmp_path / "accounts_newer.txt"
    duplicate = "same@example.com----duplicate-password-canary----duplicate-sso-canary"
    _write_accounts(
        older,
        [duplicate, "old-only@example.com----old-password----old-sso"],
        mtime_ns=1_700_000_000_000_000_000,
    )
    _write_accounts(
        newer,
        [duplicate, "new-only@example.com----new-password----new-sso"],
        mtime_ns=1_700_000_100_000_000_000,
    )
    catalog = AccountCatalog(fingerprint=_fingerprint)

    result = catalog.query(
        [older, newer],
        {_fingerprint("duplicate-sso-canary")},
        page=1,
        page_size=25,
        q="same@",
        source="all",
        status="ready",
        sort="newest",
    )

    assert result["items"] == [
        {
            "email": "same@example.com",
            "source": "accounts_newer.txt",
            "status": "ready",
            "source_mtime": result["items"][0]["source_mtime"],
        }
    ]
    assert result["filters"]["sources"] == [
        "accounts_newer.txt",
        "accounts_older.txt",
    ]
    assert result["files"] == [
        {
            "name": "accounts_newer.txt",
            "count": 2,
            "mtime": result["files"][0]["mtime"],
        },
        {
            "name": "accounts_older.txt",
            "count": 2,
            "mtime": result["files"][1]["mtime"],
        },
    ]
    serialized = json.dumps(result, ensure_ascii=False)
    for secret in (
        "duplicate-password-canary",
        "duplicate-sso-canary",
        "old-password",
        "old-sso",
        "new-password",
        "new-sso",
    ):
        assert secret not in serialized
    for forbidden_key in ("password", "sso", "raw", "fingerprint"):
        assert f'"{forbidden_key}"' not in serialized


def test_catalog_filters_sorts_and_paginates_stably(tmp_path):
    AccountCatalog, _ = _catalog_api()
    source_a = tmp_path / "accounts_a.txt"
    source_b = tmp_path / "accounts_b.txt"
    _write_accounts(
        source_a,
        [
            "zeta@example.com----p-zeta----sso-zeta",
            "alpha@example.com----p-alpha----sso-alpha",
        ],
        mtime_ns=1_700_000_000_000_000_000,
    )
    _write_accounts(
        source_b,
        [
            "beta@example.com----p-beta----sso-beta",
            "gamma@example.com----p-gamma----sso-gamma",
        ],
        mtime_ns=1_700_000_100_000_000_000,
    )
    catalog = AccountCatalog(fingerprint=_fingerprint)
    completed = {_fingerprint("sso-alpha"), _fingerprint("sso-beta")}

    ready = catalog.query(
        [source_a, source_b],
        completed,
        page=1,
        page_size=25,
        q="@EXAMPLE.COM",
        source="all",
        status="ready",
        sort="email",
    )
    pending_from_a = catalog.query(
        [source_a, source_b],
        completed,
        page=1,
        page_size=25,
        q="",
        source="accounts_a.txt",
        status="pending",
        sort="oldest",
    )

    assert [item["email"] for item in ready["items"]] == [
        "alpha@example.com",
        "beta@example.com",
    ]
    assert ready["pagination"] == {
        "page": 1,
        "page_size": 25,
        "total": 2,
        "total_pages": 1,
    }
    assert ready["summary"] == {"total_accounts": 4}
    assert [item["email"] for item in pending_from_a["items"]] == [
        "zeta@example.com"
    ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"page": 0}, "page"),
        ({"page_size": 10}, "page_size"),
        ({"source": "missing.txt"}, "source"),
        ({"status": "failed"}, "status"),
        ({"sort": "random"}, "sort"),
    ],
)
def test_catalog_rejects_invalid_query_values(tmp_path, overrides, message):
    AccountCatalog, AccountQueryError = _catalog_api()
    account_file = tmp_path / "accounts_one.txt"
    _write_accounts(
        account_file,
        ["one@example.com----password-one----sso-one"],
        mtime_ns=1_700_000_000_000_000_000,
    )
    query = {
        "page": 1,
        "page_size": 25,
        "q": "",
        "source": "all",
        "status": "all",
        "sort": "newest",
    }
    query.update(overrides)

    with pytest.raises(AccountQueryError, match=message):
        AccountCatalog(fingerprint=_fingerprint).query(
            [account_file], set(), **query
        )


@pytest.fixture
def isolated_panel_accounts(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    config_path = app_root / "config.json"
    config_path.write_text(
        json.dumps({"credentials_dir": "data/credentials"}),
        encoding="utf-8",
    )
    sso_dir = app_root / "data" / "credentials" / "sso"
    sso_dir.mkdir(parents=True)
    monkeypatch.setattr(panel_app, "BASE_DIR", app_root)
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.delenv("CPA_DIR", raising=False)
    if hasattr(panel_app, "_account_catalog"):
        panel_app._account_catalog.invalidate()
    return app_root, sso_dir


def test_v2_accounts_route_returns_paginated_public_projection(
    isolated_panel_accounts, monkeypatch
):
    _app_root, sso_dir = isolated_panel_accounts
    source = sso_dir / "accounts_batch.txt"
    _write_accounts(
        source,
        [
            "ready@example.com----route-password-canary----route-sso-ready-canary",
            "pending@example.com----pending-password-canary----pending-sso-canary",
        ],
        mtime_ns=1_700_000_000_000_000_000,
    )
    monkeypatch.setattr(
        panel_app,
        "_cpa_done",
        {panel_app.sso_fingerprint("route-sso-ready-canary")},
    )

    response = panel_app.app.test_client().get(
        "/api/v2/accounts?page=1&page_size=25&q=ready%40&status=ready&sort=email"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["items"] == [
        {
            "email": "ready@example.com",
            "source": "accounts_batch.txt",
            "status": "ready",
            "source_mtime": payload["items"][0]["source_mtime"],
        }
    ]
    assert payload["pagination"] == {
        "page": 1,
        "page_size": 25,
        "total": 1,
        "total_pages": 1,
    }
    assert payload["filters"]["sources"] == ["accounts_batch.txt"]
    assert payload["files"][0]["count"] == 2
    body = response.get_data(as_text=True)
    for secret in (
        "route-password-canary",
        "route-sso-ready-canary",
        "pending-password-canary",
        "pending-sso-canary",
    ):
        assert secret not in body


@pytest.mark.parametrize(
    "query",
    [
        "page=zero",
        "page=0",
        "page_size=10",
        "status=failed",
        "sort=random",
        "source=missing.txt",
    ],
)
def test_v2_accounts_route_rejects_invalid_queries(isolated_panel_accounts, query):
    _app_root, sso_dir = isolated_panel_accounts
    _write_accounts(
        sso_dir / "accounts_batch.txt",
        ["one@example.com----password-one----sso-one"],
        mtime_ns=1_700_000_000_000_000_000,
    )

    response = panel_app.app.test_client().get(f"/api/v2/accounts?{query}")

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert response.get_json()["error"]


def test_v2_accounts_route_uses_existing_api_login_guard(
    isolated_panel_accounts, monkeypatch
):
    monkeypatch.setattr(panel_app, "PANEL_AUTH", True)

    response = panel_app.app.test_client().get("/api/v2/accounts")

    assert response.status_code == 401
    assert response.get_json() == {"ok": False, "error": "unauthorized"}


def test_account_file_delete_invalidates_v2_catalog(
    isolated_panel_accounts, monkeypatch
):
    _app_root, sso_dir = isolated_panel_accounts
    source = sso_dir / "accounts_delete.txt"
    _write_accounts(
        source,
        ["one@example.com----password-one----sso-one"],
        mtime_ns=1_700_000_000_000_000_000,
    )
    invalidations: list[bool] = []
    monkeypatch.setattr(panel_app, "archive_orphan_cpa", lambda **_kwargs: type("R", (), {"archived": 0})())
    monkeypatch.setattr(
        panel_app,
        "invalidate_account_catalog",
        lambda: invalidations.append(True),
        raising=False,
    )

    response = panel_app.app.test_client().post(
        "/api/accounts/delete", json={"files": [source.name]}
    )

    assert response.status_code == 200
    assert response.get_json()["deleted"] == [source.name]
    assert invalidations == [True]
