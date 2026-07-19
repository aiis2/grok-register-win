# Generic Email Receive Testing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a provider-neutral, end-to-end mailbox receive test that sends a Grok-shaped verification code through provider-native email, configured SMTP, or optional direct MX, then verifies receipt with the existing mailbox adapters and reports progress in a secure UI modal.

**Architecture:** Keep receiving and sending separate. Existing `BaseMailbox` implementations remain the receive adapters; new sender strategies expose capability probes and one-recipient test delivery. An orchestration service snapshots the inbox, sends a random `ABC-123` code, calls the existing `wait_for_code()` with `GROK_CODE_PATTERN`, compares exact values, and cleans up. Flask owns a single asynchronous test job and the panel polls it from a modal.

**Tech Stack:** Python 3.10+, Flask, `requests`, stdlib `smtplib`/`email`, `dnspython` for MX lookup, pytest, vanilla HTML/CSS/JavaScript, Playwright CLI, GitHub CLI.

---

### Task 1: Resolve secure mailbox-test configuration

**Files:**
- Modify: `lib/mail_providers.py`
- Modify: `panel/app.py`
- Modify: `config.example.json`
- Test: `tests/test_mail_providers.py`
- Test: `tests/test_panel_email_config.py`

**Step 1: Write failing environment-resolution tests**

Add tests proving that Freemail configuration is resolved in this order: explicit config, Windows/process environment, empty fallback. The desired helper contract is:

```python
def resolved_provider_config(config: dict, environ=None) -> dict:
    """Return a copy with supported environment fallbacks applied."""
```

Test at least:

```python
def test_freemail_environment_fills_missing_config(monkeypatch):
    monkeypatch.setenv("MAIL_WEB_URL", "https://mail.example.com")
    monkeypatch.setenv("ADMIN_NAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    resolved = mail_providers.resolved_provider_config({"email_provider": "freemail"})
    assert resolved["freemail_api_url"] == "https://mail.example.com"
    assert resolved["freemail_username"] == "admin"
    assert resolved["freemail_password"] == "secret"


def test_explicit_freemail_config_wins_over_environment(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "environment-secret")
    resolved = mail_providers.resolved_provider_config(
        {"freemail_password": "configured-secret"}
    )
    assert resolved["freemail_password"] == "configured-secret"
```

**Step 2: Run tests to verify RED**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_mail_providers.py -k "freemail_environment or explicit_freemail" -v
```

Expected: FAIL because `resolved_provider_config` does not exist.

**Step 3: Implement minimal environment resolution**

In `lib/mail_providers.py`, add a pure helper that copies input and applies:

```python
ENV_FALLBACKS = {
    "freemail_api_url": "MAIL_WEB_URL",
    "freemail_username": "ADMIN_NAME",
    "freemail_password": "ADMIN_PASSWORD",
}
```

Normalize a Freemail URL by trimming whitespace/trailing slash and prepending `https://` when no scheme is supplied. Call this helper inside `extra_from_config()` and provider readiness checks so registration and receive testing use the same values.

**Step 4: Write failing panel-secret tests**

Add tests that require:

- `email_config_public()` reports `freemail_password_configured` and SMTP password-configured booleans but never returns either password.
- Blank secret fields in a save request preserve existing saved secrets.
- Explicit non-blank secret fields replace saved secrets.
- `MAIL_WEB_URL`, `ADMIN_NAME`, and `ADMIN_PASSWORD` presence is represented only as booleans/source labels.

**Step 5: Run panel tests to verify RED**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_panel_email_config.py -k "secret or environment" -v
```

Expected: FAIL because the new public fields and save semantics do not exist.

**Step 6: Implement secure panel configuration**

Add these defaults to `config.example.json` and the panel merge logic:

```json
{
  "mail_test_sender_mode": "auto",
  "mail_test_timeout_sec": 90,
  "mail_test_smtp_host": "",
  "mail_test_smtp_port": 587,
  "mail_test_smtp_security": "starttls",
  "mail_test_smtp_username": "",
  "mail_test_smtp_password": "",
  "mail_test_smtp_from": "",
  "mail_test_direct_mx_enabled": false
}
```

Never include `freemail_password` or `mail_test_smtp_password` in `email_config_public()`. Return booleans such as `freemail_password_configured`, `freemail_env_password_available`, and `mail_test_smtp_password_configured`.

**Step 7: Run focused tests and commit**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_mail_providers.py tests/test_panel_email_config.py -q
git diff --check
```

Expected: all focused tests PASS and no whitespace errors.

Commit:

```powershell
git add lib/mail_providers.py panel/app.py config.example.json tests/test_mail_providers.py tests/test_panel_email_config.py
git commit -m "feat: resolve secure mailbox test configuration"
```

### Task 2: Add Freemail native sending capability

**Files:**
- Modify: `lib/base_mailbox.py`
- Create: `tests/test_freemail_mailbox.py`

**Step 1: Write failing Freemail capability tests**

Test a public API on `FreemailMailbox`:

```python
def probe_send_capability(self) -> dict:
    ...

def send_test_message(self, *, sender: str, recipient: str, subject: str, text: str) -> dict:
    ...

def delete_email(self, account: MailboxAccount) -> bool:
    ...
```

Required behavior:

- Login HTTP errors call `raise_for_status()` and become readable errors.
- `can_send: 1` returns available; false returns a reason without attempting `/api/send`.
- `/api/send` receives exactly one `to`, the requested sender/from, subject, and text.
- The response provider/id may be returned but secrets and message body are not logged.
- `DELETE /api/mailboxes` uses only the generated account address.

**Step 2: Run tests to verify RED**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_freemail_mailbox.py -v
```

Expected: FAIL because the methods do not exist and login currently ignores status.

**Step 3: Implement the public Freemail methods**

Reuse one authenticated `requests.Session`. Validate all response statuses before parsing JSON. Keep exact Freemail paths from the official API:

- `POST /api/login`
- `GET /api/session` or the login payload for `can_send`
- `POST /api/send`
- `DELETE /api/mailboxes?address=...`

Do not add generic send behavior to `BaseMailbox`; capability remains optional and is discovered with `hasattr`/a sender registry.

**Step 4: Run tests and commit**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_freemail_mailbox.py tests/test_mail_providers.py -q
```

Commit:

```powershell
git add lib/base_mailbox.py tests/test_freemail_mailbox.py
git commit -m "feat: expose Freemail test sending capability"
```

### Task 3: Implement provider-native, SMTP, and Direct MX senders

**Files:**
- Create: `lib/email_test_senders.py`
- Create: `tests/test_email_test_senders.py`
- Modify: `requirements.txt`

**Step 1: Write failing sender-selection tests**

Define the wished-for API:

```python
@dataclass(frozen=True)
class SenderCapability:
    mode: str
    available: bool
    reason: str = ""


class TestSender(Protocol):
    mode: str
    def capability(self) -> SenderCapability: ...
    def send(self, *, recipient: str, code: str) -> dict: ...


def choose_test_sender(config, provider, mailbox, *, smtp_factory=None, resolver=None):
    ...
```

Test the exact automatic order:

1. available provider-native sender;
2. complete SMTP configuration;
3. Direct MX only when explicitly enabled;
4. otherwise raise `SenderUnavailableError` listing each unavailable reason.

Forced modes must never silently fall back.

**Step 2: Run selection tests to verify RED**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_email_test_senders.py -k "choose or forced" -v
```

Expected: FAIL because the module does not exist.

**Step 3: Implement provider-native registry and selector**

Register Freemail without hard-coding it in orchestration:

```python
NATIVE_SENDER_FACTORIES = {
    "freemail": lambda config, mailbox: FreemailNativeSender(config, mailbox),
}
```

Expose a sanitized capability list for the modal. Unknown providers report native send unsupported but remain testable through SMTP/MX.

**Step 4: Write failing SMTP tests**

Use injected fake SMTP classes. Cover:

- `SMTP_SSL` for `ssl`;
- `SMTP` plus `starttls()` for `starttls`;
- no TLS for `plain`;
- no `login()` when username/password are empty;
- authenticated login when both are configured;
- exactly one recipient, matching the generated mailbox;
- an `EmailMessage` containing `Verification code: ABC-123`;
- readable `SMTPAuthenticationError`, connect, TLS, and recipient errors.

**Step 5: Run SMTP tests to verify RED**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_email_test_senders.py -k smtp -v
```

Expected: FAIL because SMTP sender behavior is missing.

**Step 6: Implement `SmtpRelayTestSender`**

Use stdlib `smtplib` and `email.message.EmailMessage`. Limit recipients to the single address passed by orchestration. Cap socket timeout at 30 seconds and never log credentials or message code.

**Step 7: Write failing Direct MX tests**

Add `dnspython>=2.6.0` to `requirements.txt`, then test with an injected resolver and SMTP factory:

- disabled by default;
- derives the lookup domain only from the recipient after `@`;
- sorts MX records by preference;
- tries each host on port 25 until one accepts;
- uses `EHLO`, optional advertised STARTTLS, `MAIL FROM`, one `RCPT TO`, and `DATA`;
- rejects a recipient mismatch or malformed address;
- returns aggregate diagnostics when DNS, port 25, or all MX hosts fail.

**Step 8: Run Direct MX tests to verify RED**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_email_test_senders.py -k "direct_mx or mx_" -v
```

Expected: FAIL because Direct MX behavior is missing.

**Step 9: Implement `DirectMxTestSender`**

Resolve `dns.resolver.resolve(domain, "MX")`, strip trailing dots, and connect only to returned hosts on port 25. Do not accept a user-supplied destination or MX host. Generate an envelope sender from the configured test sender when valid, otherwise use a safe empty reverse path while keeping a descriptive `From` header.

**Step 10: Run sender tests and commit**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pip install -r requirements.txt --disable-pip-version-check
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_email_test_senders.py -q
```

Commit:

```powershell
git add requirements.txt lib/email_test_senders.py tests/test_email_test_senders.py
git commit -m "feat: add pluggable mailbox test senders"
```

### Task 4: Orchestrate a complete receive test

**Files:**
- Create: `lib/email_receive_test.py`
- Create: `tests/test_email_receive_test.py`

**Step 1: Write failing success-path test**

Define:

```python
def run_email_receive_test(
    config: dict,
    *,
    on_stage: Callable[[str, dict], None],
    cancelled: Callable[[], bool],
    mailbox_factory=mail_providers.make_mailbox,
    sender_selector=choose_test_sender,
) -> dict:
    ...
```

With fake mailbox/sender objects, assert the order:

`checking, creating, snapshotting, sending, waiting, verifying, cleaning, succeeded`.

Require a generated `[A-Z0-9]{3}-[A-Z0-9]{3}` code, pass `before_ids` and `GROK_CODE_PATTERN` to `wait_for_code()`, compare exact values with `hmac.compare_digest`, and return only masked email, provider, sender mode, timings, and cleanup status.

**Step 2: Run success test to verify RED**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_email_receive_test.py -k success -v
```

Expected: FAIL because the module does not exist.

**Step 3: Implement the minimal success path**

Create a local mailbox instance with resolved provider config. Never use or change `_ACTIVE_BOX`. Record monotonic timestamps for total and receive duration.

**Step 4: Write failing error/cancel/cleanup tests**

Cover:

- configuration failure before mailbox creation;
- cancellation at every stage;
- send failure;
- receive timeout;
- no code and wrong code;
- cleanup attempted exactly once after mailbox creation;
- cleanup failure becomes a warning after success and is appended to the primary error after failure;
- error sanitizer removes passwords, bearer tokens, cookies, full test code, and the mailbox local part.

**Step 5: Run error tests to verify RED**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_email_receive_test.py -k "error or cancel or cleanup or redact" -v
```

Expected: FAIL for the missing behaviors.

**Step 6: Implement errors, cancellation, and cleanup**

Use typed `ReceiveTestError(stage, message, details=None)`. Sanitize and truncate all external error strings before returning them. The cancellation callback raises a dedicated exception and still enters `finally` cleanup.

**Step 7: Run tests and commit**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_email_receive_test.py tests/test_email_test_senders.py -q
```

Commit:

```powershell
git add lib/email_receive_test.py tests/test_email_receive_test.py
git commit -m "feat: orchestrate end-to-end mailbox receive tests"
```

### Task 5: Add asynchronous Flask job APIs

**Files:**
- Modify: `panel/app.py`
- Create: `tests/test_panel_email_receive_test.py`

**Step 1: Write failing job-state tests**

Require an in-memory job manager with a lock and states:

`idle, checking, creating, snapshotting, sending, waiting, verifying, cleaning, succeeded, failed, cancelled`.

Test:

- one active test maximum;
- unpredictable `test_id`;
- state snapshots are copies;
- cancellation sets an event;
- completed records expire after a bounded TTL;
- registration blocks receive-test start and receive testing blocks registration start;
- thread-start failure rolls state back atomically, matching the existing registration startup hardening.

**Step 2: Run state tests to verify RED**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_panel_email_receive_test.py -k state -v
```

Expected: FAIL because job state does not exist.

**Step 3: Implement job manager and worker**

Keep secrets only in the worker's private config copy. Public state contains stage, timestamps, masked email, selected sender, safe error, warnings, and cancelability.

**Step 4: Write failing route tests**

Require authenticated routes:

```text
POST /api/config/email/test-capabilities
POST /api/config/email/receive-test
GET  /api/config/email/receive-test/<test_id>
POST /api/config/email/receive-test/<test_id>/cancel
```

Test 400/404/409 handling, secret-free JSON, form overrides without persistence, environment fallback, and precise error payloads.

**Step 5: Run route tests to verify RED**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_panel_email_receive_test.py -k route -v
```

Expected: FAIL because routes do not exist.

**Step 6: Implement the routes**

Replace the Cloudflare-only route implementation with generic capability/start/status/cancel behavior. Preserve backward compatibility: `POST /api/config/email/test` may delegate Cloudflare requests to the existing non-mutating probe or return a deprecation-compatible generic capability result.

**Step 7: Run tests and commit**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_panel_email_receive_test.py tests/test_panel_email_config.py tests/test_panel_registration_settings.py -q
```

Commit:

```powershell
git add panel/app.py tests/test_panel_email_receive_test.py
git commit -m "feat: expose asynchronous mailbox receive test api"
```

### Task 6: Build the receive-test modal and sender settings UI

**Files:**
- Modify: `panel/app.py`
- Modify: `tests/test_panel_email_receive_test.py`

**Step 1: Write failing rendered-contract tests**

Assert `INDEX_HTML` contains:

- always-visible `btn_email_receive_test`;
- modal `email_receive_test_modal` with provider, sender, masked email, progress timeline, timings, warning/error region, cancel and close controls;
- SMTP fields for host/port/security/username/password/from;
- sender-mode selector and disabled-by-default Direct MX checkbox;
- no code that renders a password, Token, or returned test code;
- generic JavaScript functions `openEmailReceiveTest`, `startEmailReceiveTest`, `pollEmailReceiveTest`, and `cancelEmailReceiveTest`.

**Step 2: Run HTML tests to verify RED**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_panel_email_receive_test.py -k html -v
```

Expected: FAIL because modal and controls do not exist.

**Step 3: Implement the modal and settings**

Keep the existing visual language. On open, post current unsaved form values to the capability endpoint. On start, disable conflicting controls and poll every second. Closing hides but does not cancel; reopening restores the active test. Escape and backdrop close only when not forcing an error acknowledgment.

Render the stage sequence from a constant array and update `aria-current`, status text, and progress state. On success show provider, selected sender, masked email, total duration, receive duration, and cleanup. On error lead with stage and safe server message.

**Step 4: Add client-interaction tests**

Test source-level contracts for request URLs, polling stop conditions, cancel route, button disabling, and success/error rendering. Keep network behavior covered by Flask tests and reserve full interaction for Playwright.

**Step 5: Run focused tests and commit**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_panel_email_receive_test.py tests/test_panel_email_config.py -q
```

Commit:

```powershell
git add panel/app.py tests/test_panel_email_receive_test.py
git commit -m "feat: add mailbox receive test modal"
```

### Task 7: Document generic receive testing and release notes

**Files:**
- Modify: `README.md`
- Modify: `config.example.json`
- Create: `docs/releases/v1.5.0.md`
- Test: `tests/test_panel_email_receive_test.py`

**Step 1: Write failing documentation/config contract test**

Require README/config to mention:

- provider-native → SMTP → Direct MX selection;
- Direct MX disabled by default and its delivery limitations;
- Freemail environment variables;
- secret-handling rules;
- modal stages and error behavior.

**Step 2: Run test to verify RED**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_panel_email_receive_test.py -k documentation -v
```

Expected: FAIL until docs and example config are updated.

**Step 3: Update README and create release notes**

Set the README version badge to `v1.5.0`. Create `docs/releases/v1.5.0.md` with an `aiis2`-authored summary covering all major changes since the repository was published:

- Cloudflare Temp Email integration and UI configuration;
- headed Chromium reuse, worker isolation, precise process cleanup;
- 1-10 concurrent registration workers;
- configurable credential warehouse and verified migration;
- generic end-to-end mailbox receive testing with native/SMTP/MX senders;
- rebuilt README, tests, and Windows CI.

Do not claim support that was not verified and do not include real endpoints, accounts, emails, tokens, or passwords.

**Step 4: Run docs test and commit**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_panel_email_receive_test.py -k documentation -v
git diff --check
```

Commit:

```powershell
git add README.md config.example.json docs/releases/v1.5.0.md tests/test_panel_email_receive_test.py
git commit -m "docs: prepare v1.5.0 mailbox testing release"
```

### Task 8: Run automated, browser, and live Freemail verification

**Files:**
- No production changes unless a failing verification produces a TDD regression task.

**Step 1: Run the complete automated suite**

Run:

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest -q
& '..\..\.venv\Scripts\python.exe' -m compileall -q .
git diff --check
```

Expected: all tests PASS, compile exit 0, diff check exit 0.

**Step 2: Start an isolated panel**

Start the feature worktree panel on an unused loopback port with a temporary config outside tracked source. Load Freemail credentials from Windows user environment variables without printing them. Do not mutate the root worktree's `config.json`.

**Step 3: Run Playwright desktop flow**

Browser plugin classification: absent in this session, so use Playwright CLI and record that fallback.

Target flow:

`/ → select/configured Freemail → 测试收件 → capability result → start → stage progression → success/error modal`.

Check page identity, non-blank DOM, no framework overlay, relevant console health, screenshot evidence, and interaction proof. Save screenshots outside the repo.

**Step 4: Run Playwright mobile flow**

Use a 390px-wide viewport. Verify modal controls remain visible, timeline does not overflow horizontally, errors wrap, and cancel/close remain reachable.

**Step 5: Run real Freemail native send/receive test**

With `MAIL_WEB_URL`, `ADMIN_NAME`, and `ADMIN_PASSWORD` loaded from Windows User environment:

- assert capability selects `provider_native` and reports `can_send`;
- create exactly one test mailbox;
- send one random Grok-format code;
- receive and match it through `FreemailMailbox.wait_for_code()`;
- confirm the test mailbox cleanup result;
- confirm no full email, code, password, Cookie, or Token appears in public API responses/logs.

If live delivery fails, report the exact stage and use TDD before any fix.

**Step 6: Verify cleanup and tracked-secret safety**

Run:

```powershell
git status --short
git ls-files
```

Programmatically scan tracked files for the current Freemail URL/password and confirm zero matches without printing the values. Confirm no panel/test Python process, Playwright browser, or temporary test profile remains. Move disposable QA artifacts to Recycle Bin; preserve credential outputs and user-owned files.

### Task 9: Review, integrate, publish, and create the first Release

**Files:**
- No new files expected.

**Step 1: Perform local code review**

Review the complete `master..HEAD` diff for:

- secret exposure;
- arbitrary-recipient or arbitrary-MX abuse;
- SSRF expansion beyond already configured provider endpoints;
- task races and cancellation cleanup;
- registration/test mutual exclusion;
- UI error leakage;
- backward compatibility with all existing providers.

Fix every validated issue with a failing regression test first.

**Step 2: Re-run final verification**

Run the full suite, compileall, `git diff --check`, tracked-secret scan, and exact process cleanup again. Record the final test count.

**Step 3: Fast-forward `master`**

Verify local and `aiis2/master` have not diverged with `git ls-remote`. In the root worktree:

```powershell
git merge --ff-only feature/generic-email-receive-test
```

Re-run the full test suite on merged `master`.

**Step 4: Push as `aiis2`**

Confirm:

```powershell
gh api user --jq .login
```

Expected: `aiis2`.

Push:

```powershell
git push aiis2 master
```

Verify local HEAD equals `git ls-remote aiis2 refs/heads/master`.

**Step 5: Wait for GitHub Actions**

Find the run for the pushed HEAD and wait with `gh run watch --exit-status`. Do not create a release if CI fails.

**Step 6: Create Release `v1.5.0`**

There are currently no tags or Releases in `aiis2/grok-register-win`, so create the first release only after CI succeeds:

```powershell
gh release create v1.5.0 `
  --repo aiis2/grok-register-win `
  --target master `
  --title "v1.5.0 · Generic Email Receive Testing" `
  --notes-file docs/releases/v1.5.0.md
```

Verify `gh release view v1.5.0 --repo aiis2/grok-register-win --json author,isDraft,isPrerelease,tagName,targetCommitish,url` reports author `aiis2`, public non-draft/non-prerelease, tag `v1.5.0`, and target `master`.

**Step 7: Clean the feature worktree**

After confirming commits exist on local/remote `master`, remove only the clean `generic-email-receive-test` worktree and delete the merged feature branch. Do not touch the separate `cloudflare-email-browser-reuse` worktree or the root worktree's `setup.bat` / `启动.bat`.
