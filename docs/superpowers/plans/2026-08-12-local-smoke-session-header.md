# Local Smoke Session Header Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the cancellation and activity-timeline smoke harnesses authenticate their direct protected API reads with the canonical local session UUID.

**Architecture:** Keep the production identity dependency unchanged. Each smoke module imports `api.auth.LOCAL_SESSION_ID`, exposes one explicit `LOCAL_API_HEADERS` mapping, and passes it only to raw protected conversation and thread-state reads; public health probes remain headerless.

**Tech Stack:** Python 3.12, `requests`, pytest monkeypatching, FastAPI local identity contract, Playwright browser smokes.

## Global Constraints

- Use `.venv/bin/python`; do not use the system Python.
- Do not weaken production authentication or change AWS infrastructure.
- Keep public `/api/health` probes headerless.
- Real feature smokes run once per corrected revision, preserve diagnostics on failure, and are not retried automatically.

---

### Task 1: Authenticate cancellation-smoke state inspection

**Files:**
- Modify: `tests/test_local_cancellation_smoke_runner.py`
- Modify: `scripts/e2e_active_run_cancellation_real.py`

**Interfaces:**
- Consumes: `api.auth.LOCAL_SESSION_ID: str`
- Produces: `scripts.e2e_active_run_cancellation_real.LOCAL_API_HEADERS: dict[str, str]`
- Preserves: `_thread_state(api_url: str) -> dict[str, Any]`

- [ ] **Step 1: Write the failing protected-request test**

Add imports and a focused fake response:

```python
from typing import Any

from api.auth import LOCAL_SESSION_ID
import scripts.e2e_active_run_cancellation_real as smoke


class JsonResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload
```

Replace the direct `_launch_browser` import with the module import, update the existing browser test to call `smoke._launch_browser`, and add:

```python
def test_thread_state_sends_the_canonical_local_session_header(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    responses = iter(
        [
            JsonResponse({"items": [{"thread_id": "thread-1"}]}),
            JsonResponse({"run": {"state": "cancelled"}}),
        ]
    )

    def get(url: str, **kwargs: object) -> JsonResponse:
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr(smoke.requests, "get", get)

    assert smoke._thread_state("http://api.test") == {
        "run": {"state": "cancelled"}
    }
    expected_headers = {"X-Epi-Session-ID": LOCAL_SESSION_ID}
    assert calls == [
        (
            "http://api.test/api/conversations",
            {"headers": expected_headers, "timeout": 5},
        ),
        (
            "http://api.test/api/threads/thread-1/state",
            {"headers": expected_headers, "timeout": 5},
        ),
    ]
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_local_cancellation_smoke_runner.py::test_thread_state_sends_the_canonical_local_session_header -q
```

Expected: FAIL because both raw requests lack the `headers` keyword.

- [ ] **Step 3: Add the canonical header to protected reads**

In `scripts/e2e_active_run_cancellation_real.py`, add:

```python
from api.auth import LOCAL_SESSION_ID


LOCAL_API_HEADERS = {"X-Epi-Session-ID": LOCAL_SESSION_ID}
```

Update only the two `_thread_state` requests:

```python
conversations = requests.get(
    f"{api_url}/api/conversations",
    headers=LOCAL_API_HEADERS,
    timeout=5,
)
```

```python
response = requests.get(
    f"{api_url}/api/threads/{thread_id}/state",
    headers=LOCAL_API_HEADERS,
    timeout=5,
)
```

- [ ] **Step 4: Run the cancellation harness tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_local_cancellation_smoke_runner.py -q
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit the cancellation harness fix**

```bash
git add tests/test_local_cancellation_smoke_runner.py scripts/e2e_active_run_cancellation_real.py
git commit -m "test: authenticate cancellation smoke state reads"
```

---

### Task 2: Authenticate activity-smoke state inspection

**Files:**
- Modify: `tests/test_agent_activity_timeline_smoke_runner.py`
- Modify: `scripts/e2e_agent_activity_timeline_real.py`

**Interfaces:**
- Consumes: `api.auth.LOCAL_SESSION_ID: str`
- Produces: `scripts.e2e_agent_activity_timeline_real.LOCAL_API_HEADERS: dict[str, str]`
- Preserves: `_thread_state(api_url: str) -> dict[str, Any]`

- [ ] **Step 1: Write the failing protected-request test**

Add:

```python
from typing import Any

from api.auth import LOCAL_SESSION_ID


class JsonResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload
```

Add the focused test:

```python
def test_thread_state_sends_the_canonical_local_session_header(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    responses = iter(
        [
            JsonResponse({"items": [{"thread_id": "thread-1"}]}),
            JsonResponse({"run": {"state": "interrupted"}}),
        ]
    )

    def get(url: str, **kwargs: object) -> JsonResponse:
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr(smoke.requests, "get", get)

    assert smoke._thread_state("http://api.test") == {
        "run": {"state": "interrupted"}
    }
    expected_headers = {"X-Epi-Session-ID": LOCAL_SESSION_ID}
    assert calls == [
        (
            "http://api.test/api/conversations",
            {"headers": expected_headers, "timeout": 5},
        ),
        (
            "http://api.test/api/threads/thread-1/state",
            {"headers": expected_headers, "timeout": 5},
        ),
    ]
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_agent_activity_timeline_smoke_runner.py::test_thread_state_sends_the_canonical_local_session_header -q
```

Expected: FAIL because both raw requests lack the `headers` keyword.

- [ ] **Step 3: Add the canonical header to protected reads**

In `scripts/e2e_agent_activity_timeline_real.py`, add:

```python
from api.auth import LOCAL_SESSION_ID


LOCAL_API_HEADERS = {"X-Epi-Session-ID": LOCAL_SESSION_ID}
```

Pass `headers=LOCAL_API_HEADERS` to the conversation-list and thread-state
requests inside `_thread_state`. Do not add headers to `_wait_for_health`.

- [ ] **Step 4: Run the activity harness tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_agent_activity_timeline_smoke_runner.py -q
```

Expected: 7 tests pass.

- [ ] **Step 5: Commit the activity harness fix**

```bash
git add tests/test_agent_activity_timeline_smoke_runner.py scripts/e2e_agent_activity_timeline_real.py
git commit -m "test: authenticate activity smoke state reads"
```

---

### Task 3: Verify the corrected release boundary

**Files:**
- Verify: `scripts/e2e_active_run_cancellation_real.py`
- Verify: `scripts/e2e_agent_activity_timeline_real.py`
- Verify: `frontend/dist/build-manifest.json`

**Interfaces:**
- Consumes: the corrected smoke scripts and the committed production frontend bundle
- Produces: a clean, tested Git revision eligible for `scripts/build_aws_release.py`

- [ ] **Step 1: Run focused harness regression tests**

```bash
.venv/bin/python -m pytest \
  tests/test_local_cancellation_smoke_runner.py \
  tests/test_agent_activity_timeline_smoke_runner.py -q
```

Expected: 10 tests pass.

- [ ] **Step 2: Run the tracked Python release suite**

Run the 46 Git-tracked test modules, excluding the Docker-only retained-study
recovery module when Docker Desktop is unavailable. Run the Cognito JWKS tests
with local-socket permission.

Expected: all executed tests pass; the publication integration may remain one
documented skip.

- [ ] **Step 3: Run frontend verification**

```bash
npm --prefix frontend test
npm --prefix frontend run build
.venv/bin/python scripts/build_aws_release.py --write-frontend-manifest
```

Expected: 24 frontend files and 204 tests pass; Vite completes; the manifest
matches the exact frontend sources.

- [ ] **Step 4: Run each corrected real browser smoke once**

```bash
scripts/run_active_run_cancellation_smoke_local.sh
.venv/bin/python scripts/e2e_agent_activity_timeline_real.py
```

Expected: both scripts print `PASS` and preserve their diagnostic directories.
If either fails, preserve its output and stop without retrying.

- [ ] **Step 5: Commit any regenerated frontend manifest**

If the manifest timestamp changed after the required build:

```bash
git add frontend/dist/build-manifest.json
git commit -m "build: refresh production frontend manifest"
```

Expected: `git status --short --branch` reports a clean `aws-test` branch.

- [ ] **Step 6: Return to the guarded AWS deployment plan**

Build the immutable release from clean `HEAD`, verify account/profile/region,
upload by content checksum, deploy through SSM, and require health, readiness,
release-identity, and CloudWatch checks before declaring success.
