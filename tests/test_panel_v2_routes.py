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


def test_v2_accounts_has_search_filters_pagination_and_batch_regions(
    isolated_v2_panel,
):
    html = panel_app.app.test_client().get("/?ui=modern").get_data(as_text=True)

    for control_id in (
        "accounts-search",
        "accounts-source",
        "accounts-status",
        "accounts-sort",
        "accounts-page-size",
        "accounts-prev",
        "accounts-next",
        "accounts-table-body",
        "accounts-empty",
        "accounts-error",
        "accounts-retry",
        "account-files-body",
        "account-files-select-all",
        "account-files-delete",
        "credential-import-form",
        "credential-import-file",
    ):
        assert f'id="{control_id}"' in html
    for size in (25, 50, 100):
        assert f'value="{size}"' in html
    assert 'id="metric-accounts"' in html


def test_v2_accounts_keeps_all_existing_download_entry_points(isolated_v2_panel):
    html = panel_app.app.test_client().get("/?ui=modern").get_data(as_text=True)

    for path in (
        "/download/sso.txt",
        "/download/accounts.json",
        "/download/all.zip",
        "/download/cpa.zip",
        "/download/sub2.zip",
        "/download/sub2.json",
        "/download/grok2api.json",
    ):
        assert f'href="{path}"' in html


def test_v2_accounts_javascript_is_lazy_cancellable_and_debounced():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")

    assert "/api/v2/accounts" in source
    assert "requestJson('/api/accounts')" not in source
    assert "AbortController" in source
    assert "accounts.requestGeneration" in source
    assert "250" in source
    assert "ensureAccountsLoaded" in source
    assert "next === 'accounts'" in source
    assert "panel-v2-account-page-size" in source


def test_v2_account_and_file_renderers_use_safe_dom_and_encoded_paths():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")
    account_renderer = source.split("function renderAccountRows", 1)[1].split(
        "function renderAccountFiles", 1
    )[0]
    file_renderer = source.split("function renderAccountFiles", 1)[1].split(
        "async function loadAccounts", 1
    )[0]

    assert "document.createElement" in account_renderer
    assert ".textContent" in account_renderer
    assert "innerHTML" not in account_renderer
    assert "document.createElement" in file_renderer
    assert ".textContent" in file_renderer
    assert "encodeURIComponent" in file_renderer
    assert "innerHTML" not in file_renderer


def test_v2_account_batch_actions_reuse_current_delete_and_import_contracts():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")

    assert "/api/accounts/delete" in source
    assert "/api/credentials/import" in source
    assert "new FormData" in source
    assert "confirmAction" in source
    assert "encodeURIComponent" in source


def test_v2_credential_import_captures_file_before_disabling_form_controls():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")
    importer = source.split("async function importCredentials", 1)[1].split(
        "function renderJob", 1
    )[0]

    assert importer.index("new FormData") < importer.index("setBusy('accounts', true)")


def test_v2_mail_contains_every_supported_provider_and_common_actions(
    isolated_v2_panel,
):
    html = panel_app.app.test_client().get("/?ui=modern").get_data(as_text=True)

    for provider in (
        "cfworker",
        "cloudflare_temp_email",
        "moemail",
        "tempmail_lol",
        "duckmail",
        "gptmail",
        "maliapi",
        "luckmail",
        "skymail",
        "cloudmail",
        "freemail",
        "opentrashmail",
        "laoudo",
    ):
        assert f'data-mail-provider="{provider}"' in html
    for control_id in (
        "email-provider",
        "email-failover",
        "email-save",
        "email-connection-test",
        "email-receive-test-open",
        "mail-test-sender-mode",
        "mail-test-timeout-sec",
        "mail-test-smtp-host",
        "mail-test-smtp-port",
        "mail-test-smtp-security",
        "mail-test-smtp-username",
        "mail-test-smtp-password",
        "mail-test-smtp-from",
        "mail-test-direct-mx-enabled",
    ):
        assert f'id="{control_id}"' in html


def test_v2_mail_includes_all_current_provider_configuration_fields(
    isolated_v2_panel,
):
    html = panel_app.app.test_client().get("/?ui=modern").get_data(as_text=True)
    fields = (
        "cfworker_api_url", "cfworker_admin_token", "cfworker_domain",
        "cfworker_custom_auth", "cfworker_subdomain", "cloudflare_api_base",
        "cloudflare_admin_password", "cloudflare_domain", "cloudflare_site_password",
        "moemail_api_url", "moemail_api_key", "duckmail_api_url",
        "duckmail_provider_url", "duckmail_bearer", "duckmail_api_key",
        "duckmail_domain", "gptmail_base_url", "gptmail_api_key", "gptmail_domain",
        "maliapi_base_url", "maliapi_api_key", "maliapi_domain",
        "luckmail_base_url", "luckmail_api_key", "luckmail_project_code",
        "luckmail_domain", "skymail_api_base", "skymail_token", "skymail_domain",
        "cloudmail_api_base", "cloudmail_admin_email", "cloudmail_admin_password",
        "cloudmail_domain", "freemail_api_url", "freemail_admin_token",
        "freemail_username", "freemail_password", "freemail_domain",
        "freemail_use_environment", "opentrashmail_api_url", "opentrashmail_domain",
        "opentrashmail_password", "laoudo_auth", "laoudo_email", "laoudo_account_id",
    )

    for field in fields:
        assert f'id="{field}"' in html
    assert 'value="template-secret-canary"' not in html
    assert 'value="template-freemail-secret-canary"' not in html


def test_v2_mail_uses_redacted_save_and_complete_receive_test_contracts():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")

    for endpoint in (
        "/api/v2/config/email",
        "/api/v2/config/email/test",
        "/api/config/email/test-capabilities",
        "/api/config/email/receive-test",
        "/cancel",
    ):
        assert endpoint in source
    assert "EMAIL_SECRET_FIELDS" in source
    builder = source.split("function buildEmailPayload", 1)[1].split(
        "async function saveEmailConfig", 1
    )[0]
    assert "if (value) payload[field] = value" in builder
    assert "localStorage" not in builder


def test_v2_mail_receive_test_uses_accessible_progress_dialog(isolated_v2_panel):
    html = panel_app.app.test_client().get("/?ui=modern").get_data(as_text=True)

    assert '<dialog id="email-receive-dialog"' in html
    assert 'aria-labelledby="email-receive-title"' in html
    for element_id in (
        "email-receive-provider",
        "email-receive-sender",
        "email-receive-address",
        "email-receive-timeline",
        "email-receive-message",
        "email-receive-start",
        "email-receive-cancel",
        "email-receive-close",
    ):
        assert f'id="{element_id}"' in html


def test_v2_credentials_exposes_storage_migration_and_cpa_controls(
    isolated_v2_panel,
):
    html = panel_app.app.test_client().get("/?ui=modern").get_data(as_text=True)

    for element_id in (
        "credentials-dir",
        "credentials-save",
        "credentials-migrate",
        "credentials-resolved-path",
        "credentials-writable",
        "credentials-sso-files",
        "credentials-mail-files",
        "credentials-cpa-files",
        "credentials-total-files",
        "credentials-total-bytes",
        "credentials-legacy-files",
        "cpa-status",
        "cpa-backfill-limit",
        "cpa-backfill",
    ):
        assert f'id="{element_id}"' in html


def test_v2_credentials_javascript_reuses_existing_safe_contracts():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")

    for endpoint in (
        "/api/config/credentials",
        "/api/config/credentials/migrate",
        "/api/cpa/status",
        "/api/cpa/backfill",
    ):
        assert endpoint in source
    assert "formatBytes" in source
    assert "confirmAction" in source
    assert "setBusy('credentials'" in source
