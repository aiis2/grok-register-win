# Cloudflare Temp Email and Browser Reuse Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a first-class `cloudflare_temp_email` provider and configuration UI, then make headed Chromium reuse one owned browser process across normal batch rounds without weakening hard-timeout recovery.

**Architecture:** Keep the generic `CFWorkerMailbox` unchanged and introduce a protocol-specific `CloudflareTempEmailMailbox` using admin address creation plus address-JWT mail APIs. Replace the panel's process-per-account runner with a supervised batch process that consumes structured round markers; the CLI resets browser state between accounts and only restarts when the existing browser is unhealthy.

**Tech Stack:** Python 3.10+, Flask, requests, DrissionPage 4.1, pytest, Windows process supervision, vanilla HTML/JavaScript.

---

## Working conventions

Run all commands from the feature worktree. On this machine use the existing environment:

```powershell
$GROK_PYTHON = (Resolve-Path '..\..\.venv\Scripts\python.exe').Path
```

Keep commits scoped. Never stage `config.json`, `accounts_*.txt`, `mail_credentials.txt`, `data/logs/`, `.venv/`, `_remote_src/`, or `.worktrees/`.

### Task 1: Establish pytest and provider contract tests

**Files:**

- Create: `requirements-dev.txt`
- Create: `tests/conftest.py`
- Create: `tests/test_cloudflare_temp_email_mailbox.py`

**Step 1: Add the test dependency**

```text
-r requirements.txt
pytest>=8.0,<9
```

**Step 2: Write failing creation-contract tests**

Use a small fake `requests.request` response and assert the wished-for API:

```python
from lib.base_mailbox import CloudflareTempEmailMailbox


def test_create_address_uses_admin_contract(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(200, {"address": "alice@example.com", "jwt": "addr-jwt", "address_id": 7})

    monkeypatch.setattr("requests.request", fake_request)
    box = CloudflareTempEmailMailbox(
        api_base="https://mail.example.com/",
        admin_password="admin-secret",
        domain="example.com",
        site_password="site-secret",
    )

    account = box.get_email()

    assert account.email == "alice@example.com"
    assert account.account_id == "addr-jwt"
    assert account.extra["address_id"] == 7
    assert calls[0][0:2] == ("POST", "https://mail.example.com/admin/new_address")
    assert calls[0][2]["headers"]["x-admin-auth"] == "admin-secret"
    assert calls[0][2]["headers"]["x-custom-auth"] == "site-secret"
    assert calls[0][2]["json"]["domain"] == "example.com"
```

Add independent failing tests for missing base/admin/domain, URL normalization, secret-safe error messages, and no secret values in logs.

**Step 3: Run tests and verify RED**

Run:

```powershell
& $GROK_PYTHON -m pytest tests/test_cloudflare_temp_email_mailbox.py -v
```

Expected: collection fails because `CloudflareTempEmailMailbox` does not exist.

**Step 4: Commit only test scaffolding**

```powershell
git add requirements-dev.txt tests/conftest.py tests/test_cloudflare_temp_email_mailbox.py
git commit -m "test: define cloudflare temp email contract"
```

### Task 2: Implement the Cloudflare mailbox create and poll flow

**Files:**

- Modify: `lib/base_mailbox.py:215-350`
- Modify: `lib/base_mailbox.py:2276-2621`
- Test: `tests/test_cloudflare_temp_email_mailbox.py`

**Step 1: Implement the minimal create flow**

Add a distinct factory branch before `cfworker` and a class beside `CFWorkerMailbox`:

```python
elif provider == "cloudflare_temp_email":
    return CloudflareTempEmailMailbox(
        api_base=extra.get("cloudflare_api_base", ""),
        admin_password=extra.get("cloudflare_admin_password", ""),
        domain=extra.get("cloudflare_domain", ""),
        site_password=extra.get("cloudflare_site_password", ""),
        proxy=proxy,
    )
```

The class must retain `jwt` and `address_id` in `MailboxAccount.extra`, while `account_id` remains the address JWT.

**Step 2: Run creation tests and verify GREEN**

```powershell
& $GROK_PYTHON -m pytest tests/test_cloudflare_temp_email_mailbox.py -v
```

Expected: creation tests pass; polling tests are not yet present.

**Step 3: Write failing parsed-mail polling tests**

Cover:

- `GET /api/parsed_mails?limit=10&offset=0`
- `Authorization: Bearer <address-jwt>` and optional `x-custom-auth`
- xAI subject code such as `UTF-6PW xAI confirmation code`
- `before_ids` and `otp_sent_at`
- 429 bounded retry using an injected/monkeypatched sleep
- 404-only fallback to `/api/mails`
- raw MIME fallback extraction
- cancellation checkpoint propagation

**Step 4: Run the new test and verify RED**

```powershell
& $GROK_PYTHON -m pytest tests/test_cloudflare_temp_email_mailbox.py -k "poll or fallback" -v
```

Expected: FAIL because polling still uses no parsed-mail implementation.

**Step 5: Implement minimal polling**

Use address auth headers for all `/api/*` requests. Fall back to `/api/mails` only when `/api/parsed_mails` returns 404/405, not on auth, rate-limit, or server failures. Reuse existing `_safe_extract()`, `_decode_raw_content()`, and `_run_polling_wait()`.

**Step 6: Verify GREEN and commit**

```powershell
& $GROK_PYTHON -m pytest tests/test_cloudflare_temp_email_mailbox.py -v
git add lib/base_mailbox.py tests/test_cloudflare_temp_email_mailbox.py
git commit -m "feat: add cloudflare temp email mailbox"
```

### Task 3: Add address cleanup and provider/config migration

**Files:**

- Modify: `lib/base_mailbox.py`
- Modify: `lib/mail_providers.py:35-180`
- Modify: `grok_register_ttk.py:105-175`
- Modify: `config.example.json`
- Test: `tests/test_cloudflare_temp_email_mailbox.py`
- Create: `tests/test_mail_providers.py`

**Step 1: Write failing cleanup tests**

Assert `DELETE /api/delete_address` uses the address JWT. When it returns a capability failure and `address_id` is known, assert fallback to `DELETE /admin/delete_address/{id}`. A cleanup error must return false/log a redacted warning rather than expose a secret.

**Step 2: Write failing provider migration tests**

```python
def test_legacy_cloudflare_alias_maps_to_dedicated_provider():
    assert normalize_provider("cloudflare") == "cloudflare_temp_email"


def test_legacy_config_populates_canonical_fields():
    extra = extra_from_config({
        "cloudflare_api_base": "https://mail.example.com",
        "cloudflare_api_key": "legacy-admin",
        "defaultDomains": "example.com",
    })
    assert extra["cloudflare_admin_password"] == "legacy-admin"
    assert extra["cloudflare_domain"] == "example.com"
```

Also prove `cfworker` still creates `CFWorkerMailbox`.

**Step 3: Run and verify RED**

```powershell
& $GROK_PYTHON -m pytest tests/test_cloudflare_temp_email_mailbox.py tests/test_mail_providers.py -v
```

**Step 4: Implement cleanup and canonical mapping**

- Canonical id: `cloudflare_temp_email`
- Compatibility aliases: `cloudflare`, `cloudflare-temp-email`
- New config fields take precedence; legacy fields remain fallback-only
- Update default provider chain without duplicating the legacy alias
- Redact address JWT and passwords from all logs

**Step 5: Verify and commit**

```powershell
& $GROK_PYTHON -m pytest tests/test_cloudflare_temp_email_mailbox.py tests/test_mail_providers.py -v
git add lib/base_mailbox.py lib/mail_providers.py grok_register_ttk.py config.example.json tests
git commit -m "feat: route cloudflare email through dedicated provider"
```

### Task 4: Replace the misleading Cloudflare configuration UI

**Files:**

- Modify: `panel/app.py:630-807`
- Modify: `panel/app.py:1620-1700`
- Modify: `panel/app.py:1912-2053`
- Modify: `panel/app.py:2721-2740`
- Create: `tests/test_panel_email_config.py`

**Step 1: Write failing config normalization tests**

Patch the panel's config path to a temporary file. Verify:

- legacy `cloudflare` is returned as `cloudflare_temp_email`
- GET view exposes canonical base/admin/domain/site fields
- POST requires base URL, admin password, and domain
- a POST writes canonical fields and synchronized compatibility fields
- switching other providers does not erase Cloudflare values

**Step 2: Run and verify RED**

```powershell
& $GROK_PYTHON -m pytest tests/test_panel_email_config.py -v
```

Expected: FAIL on provider id and missing canonical fields.

**Step 3: Implement config API changes**

Add pure helpers for normalization/validation so tests do not depend on a live Flask server. Do not expose arbitrary auth modes or unused token paths for the dedicated provider.

**Step 4: Add failing HTML contract test**

Assert `MAIN_HTML` contains inputs:

```text
cloudflare_api_base
cloudflare_admin_password
cloudflare_domain
cloudflare_site_password
```

and does not show `custom_path_token` inside the dedicated Cloudflare panel.

**Step 5: Implement the dedicated panel**

Update the dropdown label, field visibility, load/save payload, help text, and password autocomplete attributes. Keep other provider boxes unchanged.

**Step 6: Add and implement safe connection test**

Create `POST /api/config/email/test` that accepts the form payload, validates it, and probes a non-mutating endpoint (`/open_api/settings`, then `/api/settings` only as a compatibility probe). It must use short timeouts, never create an address, and return a clear capability result.

**Step 7: Verify and commit**

```powershell
& $GROK_PYTHON -m pytest tests/test_panel_email_config.py -v
git add panel/app.py tests/test_panel_email_config.py
git commit -m "feat: add cloudflare email configuration panel"
```

### Task 5: Define headed-browser reuse behavior with failing tests

**Files:**

- Create: `tests/test_browser_lifecycle.py`
- Modify later: `grok_register_ttk.py:851-875`
- Modify later: `grok_register_ttk.py:2340-2480`
- Modify later: `grok_register_ttk.py:4390-4535`

**Step 1: Write fake browser/page objects**

Model `get_tabs()`, `clear_cache()`, page `get()`, page `close()`, `process_id`, `user_data_path`, and `quit()` call capture.

**Step 2: Write failing reset tests**

Prove the wished-for function:

```python
def test_prepare_next_account_reuses_healthy_browser(monkeypatch):
    fake = FakeBrowser(tabs=[FakePage(), FakePage()])
    monkeypatch.setattr(main, "browser", fake)
    monkeypatch.setattr(main, "page", fake.tabs[-1])

    assert main.prepare_browser_for_next_account() is True
    assert fake.quit_calls == []
    assert fake.clear_cache_calls == [(True, True)]
    assert len(fake.open_tabs) == 1
    assert main.page.url == "about:blank"
```

Add failures for disconnected browser returning false, final shutdown using `force=True`, and shutdown waiting for only the recorded PID.

**Step 3: Write failing loop-policy tests**

Extract a small pure decision helper if needed and prove:

- a successful non-final round resets without restart
- a normal failed non-final round resets without restart
- a reset failure restarts once
- the final round does not start another browser
- a mail retry reuses/reset when healthy and restarts only when unhealthy

**Step 4: Run and verify RED**

```powershell
& $GROK_PYTHON -m pytest tests/test_browser_lifecycle.py -v
```

### Task 6: Implement owned-browser reset and shutdown

**Files:**

- Modify: `grok_register_ttk.py:180-205`
- Modify: `grok_register_ttk.py:851-875`
- Modify: `grok_register_ttk.py:2340-2480`
- Modify: `grok_register_ttk.py:4180-4335`
- Modify: `grok_register_ttk.py:4390-4535`
- Test: `tests/test_browser_lifecycle.py`

**Step 1: Capture ownership at startup**

Record `process_id`, `address`, and `user_data_path` immediately after constructing Chromium. Camoufox keeps its existing worker close path and is not forced into Chromium PID handling.

**Step 2: Implement account reset**

Add `prepare_browser_for_next_account(log_callback=None) -> bool` that:

1. checks `browser.get_tabs()`;
2. closes extra tabs;
3. clears cookies/cache using `browser.clear_cache(cache=True, cookies=True)`;
4. clears storage best-effort and navigates the surviving page to `about:blank`;
5. returns false on disconnection without silently starting another process.

**Step 3: Harden owned shutdown**

Call `browser.quit(force=True, del_data=True)` for Chromium. If the captured root PID survives the bounded wait, terminate that exact process tree on Windows; never use an image-name or window-title filter. Always stop the auth proxy bridge.

**Step 4: Remove unconditional restart**

In GUI and CLI registration loops, prepare for the next account only when another account remains. Call `restart_browser()` only when preparation returns false. The outer `finally` is the sole normal final shutdown.

**Step 5: Verify GREEN and commit**

```powershell
& $GROK_PYTHON -m pytest tests/test_browser_lifecycle.py -v
git add grok_register_ttk.py tests/test_browser_lifecycle.py
git commit -m "fix: reuse and precisely close headed browser"
```

### Task 7: Supervise one reusable CLI process per batch

**Files:**

- Modify: `grok_register_ttk.py:4390-4555`
- Modify: `panel/app.py:1009-1470`
- Create: `tests/test_panel_batch_runner.py`

**Step 1: Write failing marker tests**

Capture CLI logs and require one `ROUND_START index=n` before each account plus one `ROUND_RESULT` after it. Markers must not contain email/JWT data.

**Step 2: Write failing parent supervision tests**

Use a fake `Popen` stream to prove:

- one process handles multiple successful rounds;
- each `ROUND_START` refreshes the account deadline;
- success/fail stats track `ROUND_RESULT` exactly once;
- timeout kills the owned process tree and relaunches only the remaining count;
- stop kills the current batch once;
- no global browser cleanup command runs between normal rounds.

**Step 3: Run and verify RED**

```powershell
& $GROK_PYTHON -m pytest tests/test_panel_batch_runner.py -v
```

**Step 4: Emit structured markers from CLI**

Keep human-readable logs. Add stable marker lines around each account attempt and ensure every terminal path emits one result.

**Step 5: Replace `_run_one_round()` with `_run_batch()`**

Pass the requested remaining count through `GROK_REGISTER_COUNT`. Monitor stdout continuously, reset the hard deadline per start marker, and queue new account files after result markers. On a hard timeout, terminate that process tree, count the current round as failed, and start another process for remaining work.

**Step 6: Delete unsafe leftover cleanup**

Remove `_cleanup_browser_leftovers()` and its window-title/`pkill` calls. All termination must flow through the owned CLI process tree or recorded browser PID.

**Step 7: Verify and commit**

```powershell
& $GROK_PYTHON -m pytest tests/test_panel_batch_runner.py tests/test_browser_lifecycle.py -v
git add grok_register_ttk.py panel/app.py tests
git commit -m "fix: reuse browser across panel batch rounds"
```

### Task 8: Add CI, documentation, and repository attribution

**Files:**

- Create: `.github/workflows/tests.yml`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `SECURITY.md`
- Modify: `config.example.json`

**Step 1: Add Windows CI**

Run Python 3.11 on `windows-latest`, install `requirements-dev.txt`, execute pytest and compileall. Unit tests must not launch a real browser or contact external email/Grok services.

**Step 2: Update README**

- Explain the dedicated Cloudflare four-field configuration.
- Document legacy migration.
- Explain headed-browser reuse and restart conditions.
- Update repository badges/links to `aiis2/grok-register-win`.
- Add explicit upstream attribution to `lingxiaoyiyu-hub/grok-register-win`, `huslx/grokzhuce`, and `dreamhunter2333/cloudflare_temp_email` while retaining MIT copyright.

**Step 3: Update contribution/security links**

Replace repository issue/discussion URLs with the new public repository and document that secrets must never appear in issues or logs.

**Step 4: Run documentation checks and commit**

```powershell
git diff --check
git grep -n "lingxiaoyiyu-hub/grok-register-win" -- README.md LICENSE
git add .github/workflows/tests.yml README.md CONTRIBUTING.md SECURITY.md config.example.json
git commit -m "docs: document cloudflare email and browser reuse"
```

### Task 9: Full verification and real UI/process audit

**Files:**

- Modify only if a failing verification exposes a real defect

**Step 1: Run the full automated suite**

```powershell
& $GROK_PYTHON -m pytest -v
& $GROK_PYTHON -m compileall -q grok_register_ttk.py launcher.py panel lib tests
git diff --check HEAD~8..HEAD
```

Expected: zero failures and exit code 0 for all commands.

**Step 2: Start the panel with isolated local config**

Use temporary panel auth/config overrides so the real `config.json` is not changed. Open `http://127.0.0.1:8787`, verify provider switch, field visibility, validation, save/load, and connection-test error rendering. Capture a screenshot for the handoff if browser tooling is available.

**Step 3: Run a non-registering Chromium lifecycle probe**

Start headed Chromium with the project's browser options, perform several reset cycles against `about:blank`, then close. Count processes whose command line contains the captured `user_data_path` before, during, and after. Expected:

- one owned browser root during all reset cycles;
- no increase per cycle;
- zero processes for the owned profile after shutdown;
- ordinary user Chrome/Edge PIDs unchanged.

**Step 4: Audit publish scope**

```powershell
git status --short --branch --untracked-files=all
git ls-files | Select-String -Pattern 'config\.json|accounts_|mail_credentials|data/logs|_remote_src|\.venv|\.worktrees'
```

Expected: clean feature worktree; no sensitive/runtime paths tracked except intended `.gitkeep` and examples.

### Task 10: Integrate, create the public repository, and prove remote parity

**Files:** Git metadata only

**Step 1: Review commits and fast-forward master**

From the root worktree, verify unrelated untracked files remain untouched, then fast-forward `master` to `feature/cloudflare-email-browser-reuse`.

**Step 2: Create the repository**

```powershell
gh repo create aiis2/grok-register-win --public --description "Windows Grok registration panel with Cloudflare Temp Email and reusable browser lifecycle" --source . --remote aiis2
```

If the repository was created externally during implementation, add its HTTPS URL as `aiis2` instead of recreating it.

**Step 3: Push and verify**

```powershell
git push -u aiis2 master
$GROK_LOCAL_HEAD = git rev-parse HEAD
$GROK_REMOTE_HEAD = (git ls-remote aiis2 refs/heads/master).Split()[0]
if ($GROK_LOCAL_HEAD -ne $GROK_REMOTE_HEAD) { throw "remote HEAD mismatch" }
gh repo view aiis2/grok-register-win --json url,visibility,defaultBranchRef
```

Expected: `visibility` is `PUBLIC`, default branch is `master`, and local/remote hashes match.

**Step 4: Final requirement audit**

Re-read the approved design and verify, item by item: protocol parity, UI support, browser reuse, no process accumulation, secret-safe publication, and public remote parity. Do not claim completion from tests alone.
