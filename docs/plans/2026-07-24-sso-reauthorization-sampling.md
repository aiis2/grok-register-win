# SSO Reauthorization Sampling Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace single-account SSO reauthorization preflight with deterministic time/order-diverse sampling and bounded end-to-end deadlines.

**Architecture:** Build an immutable, metadata-enriched reauthorization candidate snapshot, generate three primary and up to two reserve samples, and decide batch release from aggregate results rather than the first account. Propagate one monotonic deadline through panel retry logic and every OAuth network/lock stage so a sample and the whole synchronous gate have finite budgets.

**Tech Stack:** Python 3, Flask, curl_cffi, pytest, threading locks, monotonic deadlines.

---

### Task 1: Add globally ordered candidate metadata and deterministic sampling

**Files:**
- Modify: `panel/app.py:1114-1154`
- Modify: `panel/app.py:1286-1298`
- Modify: `panel/app.py:2096-2142`
- Test: `tests/test_panel_sso_refresh.py`

**Step 1: Write failing sampler matrix tests**

Add tests for the pure sample planner:

```python
@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (1, [0]),
        (2, [1, 0]),
        (3, [2, 0, 1]),
        (5, [4, 0, 2]),
    ],
)
def test_reauthorization_primary_sample_indices(size, expected):
    candidates = [
        {"_stable_identity": f"id-{index}", "source": f"source-{index}"}
        for index in range(size)
    ]
    selected = panel_app._plan_reauthorization_preflight_samples(candidates)
    assert [item["_candidate_index"] for item in selected[:3]] == expected
```

Also assert that the source-diversity correction is deterministic, candidate input is
not mutated, and primary/reserve identities never repeat.

**Step 2: Run tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_panel_sso_refresh.py -q -k "sample_indices or source_diversity"
```

Expected: FAIL because the planner and metadata fields do not exist.

**Step 3: Add source metadata without changing unrelated account APIs**

Create a reauthorization-specific snapshot helper. Each candidate must carry:

```python
{
    "_source_mtime_ns": int,
    "_source_line_index": int,
    "_stable_identity": str,
    "_candidate_index": int,
}
```

Resolve current and legacy account files first, de-duplicate identical file names with
the current-directory precedence already used by the application, then globally sort
their records by:

```python
(
    -candidate["_source_mtime_ns"],
    candidate["source"].casefold(),
    candidate["_source_line_index"],
    candidate["_stable_identity"],
)
```

Apply disabled-account filtering, stable-identity de-duplication, and finally `limit`.
Do not return internal metadata from an API.

**Step 4: Implement the pure planner**

Add:

```python
def _plan_reauthorization_preflight_samples(
    candidates: List[dict],
    *,
    max_samples: int = 5,
) -> List[dict]:
    ...
```

The first three unique anchors are `[N - 1, 0, (N - 1) // 2]`. Add reserve anchors
near `1/4` and `3/4`. Prefer an unused source near the middle anchor when possible,
with deterministic tie-breaking.

**Step 5: Run tests and verify GREEN**

Run the Task 1 selector tests, then:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_panel_sso_refresh.py -q
```

Expected: all tests pass after updating the obsolete single-oldest assertion.

**Step 6: Commit only if the overlapping dirty file can be staged by exact hunks**

Do not stage unrelated pre-existing `panel/app.py` changes. If exact hunk staging is
not safely available, leave the implementation unstaged and report that constraint.

### Task 2: Propagate an OAuth conversion deadline through every blocking stage

**Files:**
- Modify: `lib/sso2cpa_core.py:193-249`
- Modify: `lib/sso2cpa_core.py:266`
- Modify: `lib/sso2cpa_core.py:598-784`
- Modify: `lib/sso2cpa_core.py:827-829`
- Test: `tests/test_sso2cpa_consent.py`

**Step 1: Write failing deadline tests**

Cover:

- authorize, action-script GET, prime, consent and token requests receive
  `min(stage_cap, remaining)` timeout values;
- waiting for `_CONSENT_ACTION_ID_LOCK` consumes the same deadline;
- deadline expiry during a swallowed script request raises a typed deadline exception,
  not “protocol changed”;
- the old call signatures still work when `deadline=None`.

Use an injected/fake monotonic clock; never sleep in the test.

**Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_sso2cpa_consent.py -q -k deadline
```

Expected: FAIL because deadline arguments and the typed exception do not exist.

**Step 3: Add the deadline type and helpers**

Implement:

```python
class ConversionDeadlineExceeded(ConvertError):
    def __init__(self, stage: str):
        super().__init__(f"OAuth conversion deadline exceeded: {stage}")
        self.stage = stage


def _deadline_timeout(
    deadline: Optional[float],
    stage_cap: float,
    stage: str,
) -> float:
    ...
```

When the remaining value is too small for a safe libcurl millisecond timeout, raise
immediately rather than passing zero.

**Step 4: Propagate `deadline`**

Update the compatible keyword-only signatures:

```python
def _discover_consent_action_id(..., *, force=False, deadline=None) -> str
def sso_to_token(sso: str, proxy: str = "", *, deadline=None) -> dict
def convert_one(sso: str, email: str = "", proxy: str = "", *, deadline=None) -> dict
```

Use the remaining timeout before every HTTP call and check the deadline again after
each call. Replace the unconditional consent lock context manager with a timed acquire
and guaranteed `finally: release()`.

**Step 5: Run focused and full core tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_sso2cpa_consent.py -q
```

Expected: all tests pass.

### Task 3: Make panel retry, cooldown and preflight honor the shared deadline

**Files:**
- Modify: `panel/app.py:923-990`
- Modify: `panel/app.py:1820-1833`
- Modify: `panel/app.py:2180-2262`
- Test: `tests/test_panel_cpa_pipeline.py`
- Test: `tests/test_panel_sso_refresh.py`

**Step 1: Write failing fake-clock tests**

Add tests proving:

- retry backoff and shared cooldown do not sleep beyond the remaining deadline;
- ordinary early network timeout can retry while budget remains;
- deadline exhaustion stops retries immediately;
- the normalized signature is `oauth_preflight_timeout`;
- deadline failure does not disable/quarantine an account.

**Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_panel_cpa_pipeline.py tests\test_panel_sso_refresh.py -q -k "deadline or preflight_timeout"
```

Expected: FAIL before implementation.

**Step 3: Add optional deadline-aware retry behavior**

Keep ordinary worker behavior unchanged:

```python
def _convert_cpa_with_retry(
    sso: str,
    email: str,
    *,
    deadline: Optional[float] = None,
) -> Tuple[Optional[dict], Optional[Exception], int]:
    ...
```

Pass `deadline` into `convert_one`, bound cooldown/backoff sleep by remaining time, and
classify `ConversionDeadlineExceeded` without another retry.

**Step 4: Pass sample deadlines through the synchronous preflight**

Update:

```python
def _run_cpa_reauthorization_preflight(
    candidate: dict,
    batch_id: str,
    *,
    deadline: Optional[float] = None,
) -> Tuple[bool, str]:
    ...
```

Preserve the current workspace lease, inflight bookkeeping and atomic commit flow.

**Step 5: Run focused tests and verify GREEN**

Run the Task 3 command again, then the two complete test files.

### Task 4: Replace the single-account route gate with aggregate sampling

**Files:**
- Modify: `panel/app.py:8484-8724`
- Test: `tests/test_panel_sso_refresh.py`

**Step 1: Write failing route tests**

Add deterministic tests for:

- all three primary samples run before bulk enqueue;
- the oldest sample times out but newest/middle succeeds;
- one success plus account-specific failures releases the remaining batch;
- three Access denied samples return one consistent `422` circuit result;
- three identical systemic signatures return `422` and the signature;
- mixed zero-success failures consume reserve samples;
- five zero-success samples return `422 preflight_failed`;
- successful/failed sampled identities never enter the bulk queue;
- `queued == preflight_succeeded + bulk_queued`;
- both route aliases return the same aggregate preflight schema;
- responses contain no email, password, SSO, token or source-path data.

**Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_panel_sso_refresh.py -q -k "multisample or reserve or aggregate or aliases"
```

Expected: FAIL because the route still stops after one non-Access-denied result.

**Step 3: Implement the coordinator**

Create a small result accumulator with only non-sensitive fields:

```python
{
    "planned": 3,
    "attempted": 0,
    "succeeded": 0,
    "failed": 0,
    "timed_out": 0,
    "account_denied": 0,
    "distinct_sources": 0,
    "systemic_signature": "",
}
```

Create `batch_deadline = monotonic() + 120`. Give a sample at most 60 seconds and
reserve a fair share of the remaining batch budget for unattempted primary samples.
Execute all primary samples. Execute reserve samples only when there is no success and
the first three failures do not form a three-identity systemic consensus.

Remove every attempted identity from the later bulk list. Quarantine only explicit
Access denied results. Release the bulk queue when `succeeded >= 1`; otherwise return
the appropriate bounded failure response.

**Step 4: Correct run accounting and response messages**

Count successful synchronous samples as processed/queued and sampled failures as
failures. Mark unattempted candidates as skipped on gate failure so completion
invariants hold. Do not expose sample identities or file metadata.

**Step 5: Run the complete refresh tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_panel_sso_refresh.py -q
```

Expected: all tests pass.

### Task 5: Full regression and implementation review

**Files:**
- Verify: `panel/app.py`
- Verify: `lib/sso2cpa_core.py`
- Verify: `tests/test_panel_sso_refresh.py`
- Verify: `tests/test_panel_cpa_pipeline.py`
- Verify: `tests/test_sso2cpa_consent.py`

**Step 1: Run formatting/syntax checks**

```powershell
.\.venv\Scripts\python.exe -m py_compile panel\app.py lib\sso2cpa_core.py
```

Expected: exit code 0.

**Step 2: Run focused regression**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_panel_sso_refresh.py tests\test_panel_cpa_pipeline.py tests\test_sso2cpa_consent.py -q
```

Expected: all tests pass.

**Step 3: Run the complete repository test suite**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all tests pass, or any unrelated pre-existing failure is recorded with exact
evidence and the focused feature suite remains green.

**Step 4: Review the final diff**

Confirm:

- no unrelated dirty-tree change was reverted;
- no sensitive credential value entered tests, logs, docs or responses;
- ordinary background CPA conversion behavior is unchanged when no deadline is passed;
- all timeout and failure branches release locks and workspace leases;
- route totals satisfy `run_ok + run_fail + run_skipped == run_total`.
