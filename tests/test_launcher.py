from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import launcher


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_panel_command_uses_module_entry(monkeypatch):
    monkeypatch.setattr(launcher, "python_bin", lambda: "python-test")

    assert launcher.panel_command() == ["python-test", "-m", "panel.app"]


def test_panel_environment_does_not_force_legacy_cpa_directory(monkeypatch):
    monkeypatch.delenv("CPA_DIR", raising=False)

    env = launcher.panel_environment("http://127.0.0.1:7897")

    assert "CPA_DIR" not in env
    assert env["GROK_REGISTER_DIR"] == str(launcher.ROOT)
    assert env["SSO2CPA_PATH"] == str(launcher.ROOT / "lib")


def test_panel_environment_preserves_explicit_cpa_override(monkeypatch, tmp_path):
    override = tmp_path / "explicit-cpa"
    monkeypatch.setenv("CPA_DIR", str(override))

    env = launcher.panel_environment("http://127.0.0.1:7897")

    assert env["CPA_DIR"] == str(override)


def test_existing_healthy_panel_is_reused_without_starting_another(
    monkeypatch,
):
    opened = []
    monkeypatch.setattr(launcher, "open_port", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(launcher, "wait_health", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)

    assert launcher.reuse_existing_panel() is True
    assert opened == ["http://127.0.0.1:8787/"]


def test_direct_panel_script_can_resolve_project_packages():
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(
        {
            "PANEL_STARTUP_CHECK": "1",
            "AUTO_CPA": "0",
            "ENABLE_CLASH_UI": "0",
        }
    )

    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "panel" / "app.py")],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "PANEL_STARTUP_OK" in result.stdout
