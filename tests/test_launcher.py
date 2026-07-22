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
