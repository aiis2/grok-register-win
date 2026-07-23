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


def test_default_and_modern_render_v2_while_legacy_stays_classic(
    isolated_v2_panel,
):
    client = panel_app.app.test_client()

    default_html = client.get("/").get_data(as_text=True)
    modern_response = client.get("/?ui=modern")
    modern_html = modern_response.get_data(as_text=True)
    legacy_html = client.get("/?ui=legacy").get_data(as_text=True)

    assert modern_response.status_code == 200
    assert 'data-panel-version="2"' in default_html
    assert 'data-panel-version="2"' in modern_html
    assert 'data-panel-version="2"' not in legacy_html
    assert 'id="register-concurrency"' in default_html
    assert 'id="register_concurrency"' in legacy_html


def test_v1_9_release_documents_hidden_window_and_sso_refresh(isolated_v2_panel):
    root = Path(panel_app.__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    release = (root / "docs" / "releases" / "v1.9.0.md").read_text(
        encoding="utf-8"
    )

    assert "# v1.9.0" in release
    for phrase in (
        "aiis2",
        "Chrome_WidgetWin_1",
        "任务栏",
        "刷新全部 SSO",
        "10000",
        "失败保留旧 CPA",
        "?ui=legacy",
        "374 passed",
    ):
        assert phrase in release
    release_lower = release.casefold()
    assert "asz798838958" not in release_lower
    assert "lingxiaoyiyu-hub" not in release_lower
    combined = f"{readme}\n{release}".casefold()
    assert "38.147.173.173" not in combined
    assert "mail.aiis2.shop" not in combined


def test_v1_10_release_documents_combined_bounded_log_console(
    isolated_v2_panel,
):
    root = Path(panel_app.__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    release = (root / "docs" / "releases" / "v1.10.0.md").read_text(
        encoding="utf-8"
    )

    assert "version-v1.10.1" in readme
    for phrase in (
        "aiis2",
        "注册与日志",
        "2000",
        "300",
        "批量",
        "#logs",
        "Playwright",
        "726ms",
        "74ms",
    ):
        assert phrase in release
    release_lower = release.casefold()
    for forbidden in ("asz798838958", "lingxiaoyiyu-hub"):
        assert forbidden not in release_lower
    combined = f"{readme}\n{release}".casefold()
    for forbidden in ("38.147.173.173", "mail.aiis2.shop"):
        assert forbidden not in combined


def test_v1_10_1_release_documents_turnstile_recovery_and_soak_validation(
    isolated_v2_panel,
):
    root = Path(panel_app.__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    release = (root / "docs" / "releases" / "v1.10.1.md").read_text(
        encoding="utf-8"
    )

    assert "version-v1.10.1" in readme
    for phrase in (
        "aiis2",
        "Chromium",
        "Shadow DOM",
        "Turnstile",
        "1000",
        "10 个并发槽",
        "Playwright",
        "SSO",
        "Sub2API",
        "381 passed",
    ):
        assert phrase in release
    release_lower = release.casefold()
    for forbidden in (
        "asz798838958",
        "lingxiaoyiyu-hub",
    ):
        assert forbidden not in release_lower


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
    for section in ("overview", "register", "accounts", "mail", "credentials"):
        assert f'id="section-{section}"' in html
        assert f'href="#{section}"' in html
    assert 'id="registration-log-console"' in html


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


def test_v2_screen_reader_only_labels_do_not_expand_mobile_document_width():
    root = Path(panel_app.__file__).resolve().parent
    css = (root / "static" / "panel-v2.css").read_text(encoding="utf-8")
    rule = css.split(".sr-only {", 1)[1].split("}", 1)[0]

    assert "position: absolute" in rule
    assert "left: 0" in rule
    assert "top: 0" in rule


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
    assert 'id="registration-verification-lanes"' in html


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
    assert "job.verification_concurrency" in source
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
        "cpa-refresh-all",
    ):
        assert f'id="{element_id}"' in html
    assert 'max="10000"' in html
    assert "不会生成新的 Web SSO" in html
    assert "失败保留旧 CPA" in html


def test_v2_credentials_javascript_reuses_existing_safe_contracts():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")

    for endpoint in (
        "/api/config/credentials",
        "/api/config/credentials/migrate",
        "/api/cpa/status",
        "/api/cpa/backfill",
        "/api/cpa/refresh-all",
    ):
        assert endpoint in source
    assert "formatBytes" in source
    assert "confirmAction" in source
    assert "setBusy('credentials'" in source
    assert "async function refreshAllSso()" in source
    assert "失败时保留旧 CPA" in source


def test_v2_logs_exposes_live_controls_and_accessible_output(isolated_v2_panel):
    html = panel_app.app.test_client().get("/?ui=modern").get_data(as_text=True)

    for element_id in (
        "logs-connection-status",
        "logs-pause",
        "logs-autoscroll",
        "logs-level",
        "logs-search",
        "logs-reconnect",
        "logs-clear",
        "logs-output",
        "logs-count",
    ):
        assert f'id="{element_id}"' in html
    assert 'role="log"' in html
    assert 'aria-live="polite"' in html


def test_v2_combines_registration_and_logs_in_one_navigation_section(
    isolated_v2_panel,
):
    html = panel_app.app.test_client().get("/?ui=modern").get_data(as_text=True)

    assert 'data-section-link="register">注册与日志</a>' in html
    assert 'data-section-link="logs"' not in html
    assert 'id="section-logs"' not in html
    register_section = html.split('id="section-register"', 1)[1].split(
        "</section>", 1
    )[0]
    assert register_section.index('id="registration-form"') < register_section.index(
        'id="logs-output"'
    )
    assert 'id="registration-log-console"' in register_section
    assert 'id="logs-load-older"' in register_section
    assert 'id="logs-show-latest"' in register_section


def test_v2_log_rendering_is_bounded_scheduled_and_backwards_compatible():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")

    for marker in (
        "const LOG_VISIBLE_STEP = 300",
        "const LOG_RENDER_INTERVAL_MS = 100",
        "const LOG_SEARCH_DEBOUNCE_MS = 180",
        "function scheduleLogRender",
        "function loadOlderLogs",
        "function showLatestLogs",
        "requested === 'logs'",
    ):
        assert marker in source

    append_logic = source.split("function appendLogEvent", 1)[1].split(
        "function handleLogEvent", 1
    )[0]
    assert "scheduleLogRender" in append_logic
    assert "renderLogs()" not in append_logic


def test_v2_logs_uses_resumable_sse_deduplication_and_polling_fallback():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")

    assert "new EventSource" in source
    assert "/api/logs/stream?after=" in source
    assert "lastSequence" in source
    assert "seenSequences" in source
    assert "event.lastEventId" in source
    assert "addEventListener('log'" in source
    assert "startLogFallback" in source
    assert "/api/job/status" in source
    assert "fallbackTimer" in source


def test_v2_logs_pause_filter_autoscroll_and_clear_are_display_only():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")

    for marker in (
        "logs.paused",
        "logs.autoScroll",
        "logs.level",
        "logs.query",
        "renderLogs",
        "clearLocalLogs",
    ):
        assert marker in source
    clear_logic = source.split("function clearLocalLogs", 1)[1].split(
        "function", 1
    )[0]
    assert "requestJson" not in clear_logic
    assert "fetch" not in clear_logic


def test_v2_local_storage_is_restricted_to_display_preferences():
    root = Path(panel_app.__file__).resolve().parent
    source = (root / "static" / "panel-v2.js").read_text(encoding="utf-8")

    assert "DISPLAY_PREFERENCE_KEYS" in source
    saver = source.split("function savePreference", 1)[1].split(
        "function", 1
    )[0]
    assert "DISPLAY_PREFERENCE_KEYS.has(key)" in saver
    preference_block = source.split("const DISPLAY_PREFERENCE_KEYS", 1)[1].split(
        ");", 1
    )[0].casefold()
    for forbidden in (
        "password",
        "token",
        "secret",
        "credential",
        "smtp",
        "freemail",
    ):
        assert forbidden not in preference_block
