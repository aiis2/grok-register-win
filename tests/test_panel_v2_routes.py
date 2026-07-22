from __future__ import annotations

import json
from pathlib import Path

import pytest

from panel import app as panel_app


@pytest.fixture
def isolated_v2_panel(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    config_path = app_root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "credentials_dir": "data/credentials",
                "register_concurrency": 3,
                "gptmail_api_key": "template-secret-canary",
                "freemail_password": "template-freemail-secret-canary",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(panel_app, "BASE_DIR", app_root)
    monkeypatch.setattr(panel_app, "CONFIG_PATH", config_path)
    monkeypatch.setattr(panel_app, "PANEL_AUTH", False)
    monkeypatch.delenv("CPA_DIR", raising=False)
    return app_root


def test_modern_query_renders_v2_while_default_and_legacy_stay_classic(
    isolated_v2_panel,
):
    client = panel_app.app.test_client()

    default_html = client.get("/").get_data(as_text=True)
    modern_response = client.get("/?ui=modern")
    modern_html = modern_response.get_data(as_text=True)
    legacy_html = client.get("/?ui=legacy").get_data(as_text=True)

    assert modern_response.status_code == 200
    assert 'data-panel-version="2"' in modern_html
    assert 'data-panel-version="2"' not in default_html
    assert 'data-panel-version="2"' not in legacy_html
    assert 'id="register_concurrency"' in default_html
    assert 'id="register_concurrency"' in legacy_html


def test_v2_uses_only_local_assets_and_has_server_rendered_legacy_fallback(
    isolated_v2_panel,
):
    html = panel_app.app.test_client().get("/?ui=modern").get_data(as_text=True)

    assert 'href="/static/panel-v2.css"' in html
    assert 'src="/static/panel-v2.js"' in html
    assert 'href="/?ui=legacy"' in html
    assert "cdn." not in html.casefold()
    assert "unpkg.com" not in html.casefold()
    assert "fonts.googleapis.com" not in html.casefold()
    assert "template-secret-canary" not in html
    assert "template-freemail-secret-canary" not in html


def test_v2_static_assets_are_served_by_flask(isolated_v2_panel):
    client = panel_app.app.test_client()

    css = client.get("/static/panel-v2.css")
    javascript = client.get("/static/panel-v2.js")

    assert css.status_code == 200
    assert css.mimetype == "text/css"
    assert javascript.status_code == 200
    assert "javascript" in javascript.mimetype


def test_v2_shell_has_accessible_landmarks_sections_and_theme_controls(
    isolated_v2_panel,
):
    html = panel_app.app.test_client().get("/?ui=modern").get_data(as_text=True)

    assert "<header" in html
    assert "<main" in html
    assert "<nav" in html
    assert 'aria-label="主导航"' in html
    assert 'id="theme-toggle"' in html
    assert 'aria-label="界面主题"' in html
    for section in ("overview", "register", "accounts", "mail", "credentials", "logs"):
        assert f'id="section-{section}"' in html
        assert f'href="#{section}"' in html


def test_v2_theme_prepaint_runs_before_stylesheet_and_uses_strict_preference(
    isolated_v2_panel,
):
    html = panel_app.app.test_client().get("/?ui=modern").get_data(as_text=True)

    prepaint = html.index("panel-v2-theme")
    stylesheet = html.index("panel-v2.css")
    assert prepaint < stylesheet
    assert "system" in html[prepaint:stylesheet]
    assert "light" in html[prepaint:stylesheet]
    assert "dark" in html[prepaint:stylesheet]


def test_v2_css_uses_semantic_tokens_responsive_layout_and_reduced_motion():
    root = Path(panel_app.__file__).resolve().parent
    css = (root / "static" / "panel-v2.css").read_text(encoding="utf-8")

    for token in (
        "--surface-canvas",
        "--surface-panel",
        "--text-primary",
        "--text-muted",
        "--border-subtle",
        "--accent",
    ):
        assert token in css
    assert "Segoe UI Variable" in css
    assert "Microsoft YaHei UI" in css
    assert "Cascadia Mono" in css
    assert "max-width: 1440px" in css
    assert "@media" in css and "768px" in css
    assert "prefers-reduced-motion" in css


def test_v2_javascript_defines_hash_navigation_and_system_theme_behavior():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")

    assert "panel-v2-theme" in source
    assert "panel-v2-section" in source
    assert "matchMedia" in source
    assert "hashchange" in source
    assert "#overview" in source
    assert "textContent" in source


def test_v2_registration_has_existing_limits_browser_modes_and_worker_region(
    isolated_v2_panel,
):
    html = panel_app.app.test_client().get("/?ui=modern").get_data(as_text=True)

    assert 'id="registration-form"' in html
    assert 'id="register-count"' in html
    assert 'min="1"' in html and 'max="10000"' in html
    assert 'id="register-concurrency"' in html
    assert 'max="10"' in html
    assert 'id="browser-engine"' in html
    assert 'value="chromium"' in html
    assert 'value="camoufox"' in html
    assert 'id="browser-window-mode"' in html
    for mode in ("hidden", "minimized", "visible"):
        assert f'value="{mode}"' in html
    assert 'id="start-registration"' in html
    assert 'id="stop-registration"' in html
    assert 'id="worker-grid"' in html


def test_v2_uses_accessible_dialog_instead_of_native_confirm(isolated_v2_panel):
    root = Path(panel_app.__file__).resolve().parent
    html = panel_app.app.test_client().get("/?ui=modern").get_data(as_text=True)
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")

    assert '<dialog id="confirm-dialog"' in html
    assert 'aria-labelledby="confirm-title"' in html
    assert 'id="confirm-cancel"' in html
    assert 'id="confirm-accept"' in html
    assert ".showModal()" in source
    assert "window.confirm" not in source


def test_v2_registration_javascript_reuses_current_backend_contracts():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")

    for endpoint in (
        "/api/job/status",
        "/api/job/start",
        "/api/job/stop",
        "/api/config/browser",
        "/api/job/workers/",
        "/browser/",
    ):
        assert endpoint in source
    assert "async function requestJson" in source
    assert "Promise.allSettled" in source
    assert "browser.generation" in source
    assert "workerControlPending" in source
    assert "setInterval" in source


def test_v2_worker_renderer_builds_safe_dom_without_inner_html():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")
    renderer = source.split("function renderWorkers", 1)[1].split(
        "async function controlWorkerBrowser", 1
    )[0]

    assert "document.createElement" in renderer
    assert ".textContent" in renderer
    assert "innerHTML" not in renderer
    assert "browser.generation" in renderer


def test_v2_registration_busy_state_is_scoped_to_related_controls():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")

    assert "setBusy('registration'" in source
    assert "data-busy-group" in source
    assert "document.body" not in source.split("function setBusy", 1)[1].split("}", 1)[0]
