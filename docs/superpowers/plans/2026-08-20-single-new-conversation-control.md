# Single New Conversation Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the saved-conversations duplicate and allow the sole upper-right New conversation control to open a blank conversation while the prior thread keeps running.

**Architecture:** Keep `App.newConversation` as the single state-reset operation, but split short-lived UI transitions from a stable background run so only the former temporarily block it. Remove the sidebar callback and markup completely so both the visual duplication and its unused interface disappear. Existing selection-generation ownership checks continue to isolate late responses from the previous thread.

**Tech Stack:** React 19, TypeScript, Vitest, Testing Library, Playwright, FastAPI, Python 3.12

## Global Constraints

- Do not cancel the previous thread's backend run when starting a new conversation.
- Do not add background-run badges, confirmation dialogs, or backend APIs.
- Preserve existing restrictions on message submission, uploads, and review controls.
- Add a dedicated real browser smoke under `scripts/` with a maximum five-minute runtime and no stubbed production dependencies.
- Rebuild the tracked frontend bundle with `npm --prefix frontend run build`, then refresh the build manifest with `.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest`.

---

### Task 1: Make the header the sole always-available control

**Files:**
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/ConversationHistory.test.tsx`
- Modify: `frontend/src/App.tsx:904-907,1096-1128`
- Modify: `frontend/src/ConversationHistory.tsx:4-24,64-76`
- Modify: `frontend/src/styles.css:225-251`

**Interfaces:**
- Consumes: the existing `newConversation(): void` state-reset operation and `ConversationHistory` callbacks.
- Produces: one upper-right `button` named `New conversation`; `ConversationHistory` no longer accepts `onNewConversation`.

- [ ] **Step 1: Write failing component and app assertions**

In `ConversationHistory.test.tsx`, remove `onNewConversation` setup, prop usage, and click assertions from the rename test. In `App.test.tsx`, change the saved-conversation test to require the single header control and absence of the sidebar control:

```tsx
expect(
  screen.queryByRole("button", {
    name: "Start new conversation from saved conversations",
  }),
).not.toBeInTheDocument();
const newConversation = screen.getByRole("button", {
  name: "New conversation",
});
fireEvent.click(newConversation);
```

Add this focused test, which keeps the request contract limited to runtime
options, thread creation, and message submission:

```tsx
it("starts a new conversation while the selected thread is running", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(runtimeOptionsResponse())
    .mockResolvedValueOnce(createThreadResponse())
    .mockResolvedValueOnce(
      jsonResponse(
        threadState({
          run: {
            state: "running",
            steps: 1,
            error: null,
            error_code: null,
            user_message: null,
            started_at: 1,
            updated_at: 1,
          },
          conversation: [
            { id: "running-user", role: "user", text: "Long analysis" },
          ],
        }),
      ),
    );

  render(
    <App
      apiBase="http://api.test"
      fetchImpl={fetchMock}
      loadConversationHistory={false}
    />,
  );
  fireEvent.change(
    await screen.findByLabelText("Ask a question about your dataset!"),
    { target: { value: "Long analysis" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByRole("button", { name: "Cancel run" });

  const newConversation = screen.getByRole("button", {
    name: "New conversation",
  });
  expect(newConversation).toBeEnabled();
  fireEvent.click(newConversation);

  expect(screen.queryByText("Long analysis")).not.toBeInTheDocument();
  expect(
    screen.getByLabelText("Ask a question about your dataset!"),
  ).toBeEnabled();
});
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
npm --prefix frontend test -- ConversationHistory.test.tsx App.test.tsx
```

Expected: FAIL because the App still renders the sidebar control and the header button is disabled for a running thread.

- [ ] **Step 3: Implement the minimal production change**

In `App.tsx`, split the transient operations from the background run and use
the narrower condition in the new-conversation guard:

```tsx
const isConversationTransitionBusy =
  isSubmitting ||
  isResuming ||
  isCancelling ||
  isUploadingAttachments ||
  isLoadingConversation;
const isBusy = isConversationTransitionBusy || isRunInFlight;

function newConversation() {
  if (isConversationTransitionBusy) {
    return;
  }

  createThreadPromiseRef.current = null;
```

Use the narrower disabled condition on the header button, and stop passing `onNewConversation` to `ConversationHistory`:

```tsx
<button
  className="new-conversation-button"
  disabled={isConversationTransitionBusy}
  onClick={newConversation}
  type="button"
>
  New conversation
</button>
```

In `ConversationHistory.tsx`, remove `onNewConversation` from the destructured props and type, then reduce the header to:

```tsx
<div className="conversation-history-header">
  <h2>Saved conversations</h2>
</div>
```

Delete the now-unused `.conversation-history-new-button`, hover, and focus-visible CSS rules. Update the two existing `App.test.tsx` callers of the sidebar-specific accessible name to use the header's exact `New conversation` name.

- [ ] **Step 4: Run focused and complete frontend tests**

Run:

```bash
npm --prefix frontend test -- ConversationHistory.test.tsx App.test.tsx
npm --prefix frontend test
```

Expected: both commands PASS with no failed Vitest cases.

- [ ] **Step 5: Commit the behavior change**

```bash
git add frontend/src/App.tsx frontend/src/App.test.tsx frontend/src/ConversationHistory.tsx frontend/src/ConversationHistory.test.tsx frontend/src/styles.css
git commit -m "fix: keep new conversation available during runs"
```

### Task 2: Add real-browser regression coverage and rebuild delivery assets

**Files:**
- Create: `scripts/e2e_new_conversation_during_active_run_real.py`
- Modify: `frontend/dist/index.html`
- Modify: generated files under `frontend/dist/assets/`
- Modify: `frontend/dist/build-manifest.json`

**Interfaces:**
- Consumes: the real FastAPI entry point, compiled `frontend/dist`, local authentication session header, installed study data, Playwright, and a real `OPENAI_API_KEY`.
- Produces: an executable smoke that proves the sole control detaches from—not cancels—an active thread.

- [ ] **Step 1: Create the dedicated real smoke**

Create `scripts/e2e_new_conversation_during_active_run_real.py` with this complete implementation, reusing only the process helpers from the existing real cancellation smoke:

```python
#!/usr/bin/env python3
"""Start a new conversation while a real background run remains active."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any

import requests

from e2e_active_run_cancellation_real import (
    LOCAL_API_HEADERS,
    MESSAGE_LABEL,
    REPO_ROOT,
    _find_port,
    _launch_browser,
    _remaining_ms,
    _wait_for_health,
)


def _conversations(api_url: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"{api_url}/api/conversations",
        headers=LOCAL_API_HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    return response.json().get("items") or []


def _thread_state(api_url: str, thread_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{api_url}/api/threads/{thread_id}/state",
        headers=LOCAL_API_HEADERS,
        timeout=5,
    )
    response.raise_for_status()
    return response.json()


def _write_failure_diagnostics(
    artifact_dir: Path,
    api_url: str,
    page: Any | None,
    error: BaseException,
) -> None:
    (artifact_dir / "failure.txt").write_text(
        "".join(traceback.format_exception(error)),
        encoding="utf-8",
    )
    if page is not None:
        try:
            (artifact_dir / "failure-page.txt").write_text(
                page.locator("body").inner_text(),
                encoding="utf-8",
            )
            page.screenshot(
                path=str(artifact_dir / "failure-screenshot.png"),
                full_page=True,
            )
        except Exception:
            pass
    try:
        conversations = _conversations(api_url)
        (artifact_dir / "failure-conversations.json").write_text(
            json.dumps(conversations, indent=2),
            encoding="utf-8",
        )
        if conversations:
            state = _thread_state(api_url, conversations[0]["thread_id"])
            (artifact_dir / "failure-state.json").write_text(
                json.dumps(state, indent=2),
                encoding="utf-8",
            )
    except Exception:
        pass


def run(args: argparse.Namespace) -> int:
    if args.timeout_seconds > 300:
        raise ValueError("The feature smoke is limited to five minutes.")
    deadline = time.monotonic() + args.timeout_seconds
    artifact_dir = (
        args.artifact_dir.expanduser().resolve()
        if args.artifact_dir
        else Path(tempfile.mkdtemp(prefix="new-conversation-active-run-smoke-"))
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = artifact_dir / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)

    from utils.env_loader import load_app_environment

    environment_root = args.environment_root.expanduser().resolve()
    load_app_environment(environment_root)
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError("A real OPENAI_API_KEY is required for this smoke.")
    static_dir = REPO_ROOT / "frontend" / "dist"
    if not (static_dir / "index.html").is_file():
        raise RuntimeError("Build frontend/dist before running this smoke.")

    host = "127.0.0.1"
    port = _find_port(host, args.api_port)
    api_url = f"http://{host}:{port}"
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "REPORT_AGENT_RUNTIME_ROOT": str(runtime_root),
            "REPORT_AGENT_CHECKPOINT_DB_PATH": str(
                runtime_root / "agent_memory_fastapi.db"
            ),
            "REPORT_AGENT_STATIC_DIR": str(static_dir),
            "REPORT_AGENT_STUDY_ROOT": str(environment_root / "study_data"),
        }
    )
    api_log = (artifact_dir / "api.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "api.app:app",
            "--host",
            host,
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=environment,
        stdout=api_log,
        stderr=subprocess.STDOUT,
        text=True,
    )

    from playwright.sync_api import sync_playwright

    page: Any | None = None
    try:
        _wait_for_health(api_url, deadline, process)
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            try:
                page = browser.new_page()
                page.goto(
                    api_url,
                    wait_until="networkidle",
                    timeout=_remaining_ms(deadline),
                )
                composer = page.get_by_label(MESSAGE_LABEL)
                composer.wait_for(timeout=_remaining_ms(deadline))
                composer.fill(
                    "Inspect the installed epidemiology data and perform a "
                    "careful multi-step analysis of an appropriate outcome."
                )
                page.get_by_role("button", name="Send", exact=True).click()
                page.get_by_role(
                    "button",
                    name="Cancel run",
                    exact=True,
                ).wait_for(timeout=_remaining_ms(deadline))

                new_button = page.get_by_role(
                    "button",
                    name="New conversation",
                    exact=True,
                )
                if not new_button.is_enabled():
                    raise AssertionError(
                        "New conversation was disabled during an active run."
                    )
                duplicate = page.get_by_role(
                    "button",
                    name="Start new conversation from saved conversations",
                )
                if duplicate.count():
                    raise AssertionError(
                        "The saved-conversations duplicate is still rendered."
                    )

                conversations = _conversations(api_url)
                if len(conversations) != 1:
                    raise AssertionError(
                        f"Expected one active conversation: {conversations!r}"
                    )
                thread_id = conversations[0]["thread_id"]
                running_state = _thread_state(api_url, thread_id)
                if running_state["run"]["state"] != "running":
                    raise AssertionError(
                        "The test did not observe an active run: "
                        f"{running_state['run']!r}"
                    )

                new_button.click()
                composer.wait_for(timeout=_remaining_ms(deadline))
                if not composer.is_enabled() or composer.input_value():
                    raise AssertionError(
                        "The blank conversation composer was not ready."
                    )
                detached_state = _thread_state(api_url, thread_id)
                (artifact_dir / "detached-state.json").write_text(
                    json.dumps(detached_state, indent=2),
                    encoding="utf-8",
                )
                if detached_state["run"]["state"] == "cancelled":
                    raise AssertionError(
                        "Starting a new conversation cancelled the previous run."
                    )
            finally:
                browser.close()
    except BaseException as error:
        _write_failure_diagnostics(
            artifact_dir,
            api_url,
            page,
            error,
        )
        print(f"FAIL new-conversation active-run smoke: {artifact_dir}")
        raise
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        api_log.close()

    print(f"PASS new-conversation active-run smoke: {artifact_dir}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start a new conversation during a real active run."
    )
    parser.add_argument("--api-port", type=int, default=8861)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument(
        "--environment-root",
        type=Path,
        default=REPO_ROOT,
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(run(_parser().parse_args()))
```

Make the file executable with `chmod +x scripts/e2e_new_conversation_during_active_run_real.py`.

- [ ] **Step 2: Rebuild the tracked production frontend and manifest**

Run:

```bash
npm --prefix frontend run build
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
```

Expected: TypeScript and Vite build successfully, and the manifest verifier reports the refreshed build manifest without errors.

- [ ] **Step 3: Run the dedicated real smoke exactly once**

Run:

```bash
.venv/bin/python scripts/e2e_new_conversation_during_active_run_real.py --timeout-seconds 300
```

Expected: PASS within five minutes. If it fails or times out, do not rerun it automatically; report the artifact directory and preserved diagnostics.

- [ ] **Step 4: Perform final verification**

Run:

```bash
npm --prefix frontend test
git diff --check
git status --short
```

Expected: all frontend tests PASS, `git diff --check` emits no output, and status lists only the intended smoke and generated delivery changes.

- [ ] **Step 5: Commit smoke and delivery assets**

```bash
git add scripts/e2e_new_conversation_during_active_run_real.py frontend/dist
git commit -m "test: cover new conversation during active run"
```
