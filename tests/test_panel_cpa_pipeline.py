from __future__ import annotations

import json

import pytest

from panel import app as panel_app


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 2),
        ("", 2),
        (1, 1),
        ("2", 2),
        (4, 4),
        (0, 1),
        (8, 4),
        ("bad", 2),
    ],
)
def test_normalize_cpa_concurrency_is_bounded(value, expected):
    assert panel_app.normalize_cpa_concurrency(value) == expected


def test_cpa_concurrency_prefers_environment_over_saved_config(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"cpa_oauth_concurrency": 3}), encoding="utf-8"
    )
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.setenv("CPA_CONCURRENCY", "4")

    assert panel_app.resolve_cpa_concurrency() == 4


def test_cpa_concurrency_uses_saved_config_without_environment(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"cpa_oauth_concurrency": 3}), encoding="utf-8"
    )
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.delenv("CPA_CONCURRENCY", raising=False)

    assert panel_app.resolve_cpa_concurrency() == 3
