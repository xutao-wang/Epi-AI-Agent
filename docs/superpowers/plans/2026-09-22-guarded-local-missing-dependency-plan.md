# Guarded Local Missing-Dependency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make unavailable Python imports fail with a concise, structured, non-crashing error and document a reproducible operator-managed package update procedure for native and Docker deployments.

**Architecture:** The trusted worker will attach `missing_module` to its existing typed error envelope, and the parent runtime will validate and propagate that detail through the existing Python tool path. The shared agent runtime and React run-failure card already provide the correct terminal-attempt behavior; regression tests will lock that behavior down. Package changes remain outside the agent and are made by updating the pinned `requirements.txt`, reinstalling or rebuilding, verifying the import, and restarting.

**Tech Stack:** Python 3.12, Pydantic, pytest, LangGraph, FastAPI state projection, React 19, TypeScript, Vitest, Docker Compose, Markdown.

**Spec:** `docs/superpowers/specs/2026-09-22-guarded-local-missing-dependency-design.md`

## Global Constraints

- The public message is exactly `Package “<module>” isn’t installed.`.
- The stable failure code remains `DEPENDENCY_NOT_AVAILABLE`.
- The structured detail key is `missing_module` and its value is the unavailable import name.
- The failure remains non-recoverable within the current agent run, but it must not stop FastAPI or block a later user request.
- Generated Python never installs packages and never receives an installation command.
- `requirements.txt` remains the canonical dependency definition and every added distribution uses an exact `==` version pin.
- Import names must never be assumed to equal package-distribution names.
- Native and Docker deployments use the same declared dependency set.
- Installing into a running native service or running container is unsupported.

---

### Task 1: Structured missing-dependency failure

**Files:**
- Modify: `epi_agent/runtimes/python/worker.py:59-83,196-202`
- Modify: `epi_agent/runtimes/python/local_process.py:260-305`
- Test: `tests/test_guarded_local_runtime.py`
- Test: `tests/test_python_execution_policy.py`

**Interfaces:**
- Consumes: `_error_payload(code: str, message: str, category: str, recoverable: bool, details: dict[str, object] | None = None) -> dict[str, object]` in the worker.
- Produces: `PythonRuntimeFailure(code="DEPENDENCY_NOT_AVAILABLE", category="unsupported_runtime", recoverable=False, details={"missing_module": module})` from `LocalPythonRuntime.execute`.
- Produces: the same detail plus `category` from `_runtime_failure(error: PythonRuntimeFailure) -> ToolExecutionError`.

- [ ] **Step 1: Add an end-to-end failing runtime test**

Add this test to `tests/test_guarded_local_runtime.py`:

```python
def test_missing_dependency_returns_concise_structured_failure(tmp_path):
    missing_module = "epi_agent_definitely_missing_dependency"
    runtime = LocalPythonRuntime(
        runtime_root=tmp_path,
        timeout_seconds=180,
        memory_limit_bytes=None,
    )

    with pytest.raises(PythonRuntimeFailure) as caught:
        runtime.execute(
            _request(f"import {missing_module}"),
            _datasets(),
        )

    assert caught.value.code == "DEPENDENCY_NOT_AVAILABLE"
    assert str(caught.value) == f"Package “{missing_module}” isn’t installed."
    assert caught.value.category == "unsupported_runtime"
    assert caught.value.recoverable is False
    assert caught.value.details == {"missing_module": missing_module}
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Run the test and confirm the old message/detail fail**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_guarded_local_runtime.py::test_missing_dependency_returns_concise_structured_failure
```

Expected: FAIL because the current message is `Python runtime does not include package: ...` and `details` is `None`.

- [ ] **Step 3: Extend the worker error envelope**

Change `_error_payload` in `epi_agent/runtimes/python/worker.py` to construct the inner error separately and include details only when supplied:

```python
def _error_payload(
    *,
    code: str,
    message: str,
    category: str,
    recoverable: bool,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {
        "code": code,
        "type": "PythonRuntimeError",
        "message": message[:2_000],
        "category": category,
        "recoverable": recoverable,
    }
    if details is not None:
        error["details"] = details
    return {"status": "error", "error": error}
```

Update the `ModuleNotFoundError` handler:

```python
    except ModuleNotFoundError as exc:
        sys.stdout = previous_stdout
        missing_module = str(exc.name or "unknown")[:512]
        payload = _error_payload(
            code="DEPENDENCY_NOT_AVAILABLE",
            message=f"Package “{missing_module}” isn’t installed.",
            category="unsupported_runtime",
            recoverable=False,
            details={"missing_module": missing_module},
        )
```

- [ ] **Step 4: Validate and propagate worker details in the parent**

In `_parse_result` in `epi_agent/runtimes/python/local_process.py`, validate `error.details` before raising the typed failure:

```python
        details = error.get("details")
        if details is not None and not isinstance(details, dict):
            raise _failure(
                "OUTPUT_MALFORMED",
                "Python runtime returned an invalid error envelope.",
                category="invalid_output",
                recoverable=True,
            )
        raise _failure(
            str(error.get("code") or "EXECUTION_FAILED"),
            str(error.get("message") or "Python execution failed.")[:2_000],
            category=str(error.get("category") or "infrastructure"),
            recoverable=bool(error.get("recoverable")),
            details=details,
        )
```

- [ ] **Step 5: Test malformed detail rejection**

Add to `tests/test_guarded_local_runtime.py`:

```python
def test_parent_rejects_non_object_worker_error_details(tmp_path):
    (tmp_path / "result.json").write_text(
        json.dumps(
            {
                "status": "error",
                "error": {
                    "code": "DEPENDENCY_NOT_AVAILABLE",
                    "message": "Package is unavailable.",
                    "category": "unsupported_runtime",
                    "recoverable": False,
                    "details": ["not", "an", "object"],
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PythonRuntimeFailure) as caught:
        _parse_result(tmp_path, duration_seconds=0.1)

    assert caught.value.code == "OUTPUT_MALFORMED"
```

Add `import json` at the top of that test file.

- [ ] **Step 6: Test Python-tool detail propagation**

Add to `tests/test_python_execution_policy.py`:

```python
def test_runtime_adapter_preserves_missing_dependency_details():
    runtime_error = PythonRuntimeFailure(
        "DEPENDENCY_NOT_AVAILABLE",
        "Package “sklearn” isn’t installed.",
        category="unsupported_runtime",
        recoverable=False,
        details={"missing_module": "sklearn"},
    )

    tool_error = _runtime_failure(runtime_error)

    assert tool_error.code == "DEPENDENCY_NOT_AVAILABLE"
    assert str(tool_error) == "Package “sklearn” isn’t installed."
    assert tool_error.recoverable is False
    assert tool_error.details == {
        "category": "unsupported_runtime",
        "missing_module": "sklearn",
    }
```

- [ ] **Step 7: Run the focused backend tests**

Run:

```bash
../../.venv/bin/python -m pytest -q \
  tests/test_guarded_local_runtime.py \
  tests/test_python_execution_policy.py
```

Expected: PASS.

- [ ] **Step 8: Commit the structured failure**

```bash
git add \
  epi_agent/runtimes/python/worker.py \
  epi_agent/runtimes/python/local_process.py \
  tests/test_guarded_local_runtime.py \
  tests/test_python_execution_policy.py
git commit -m "feat: report missing Python dependencies clearly"
```

---

### Task 2: Preserve the application and concise interface

**Files:**
- Modify: `tests/test_tool_failure_recovery.py`
- Modify: `tests/test_tool_tracking.py`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: non-recoverable `ToolExecutionError` with code `DEPENDENCY_NOT_AVAILABLE`.
- Produces: graph `terminal_error` containing the stable code and concise message.
- Produces: API `RunStatus(state="error", error_code="DEPENDENCY_NOT_AVAILABLE", user_message="Package “xxx” isn’t installed.")`.
- Produces: an enabled conversation composer after the failure card is displayed.

- [ ] **Step 1: Add a graph regression test for clean termination**

Add this tool beside the existing test tools in `tests/test_tool_failure_recovery.py`:

```python
class MissingDependencyTool(FailingTool):
    def invoke(self, arguments, context):
        self.executions.append(arguments)
        raise ToolExecutionError(
            "DEPENDENCY_NOT_AVAILABLE",
            "Package “sklearn” isn’t installed.",
            recoverable=False,
            details={
                "category": "unsupported_runtime",
                "missing_module": "sklearn",
            },
        )
```

Add the regression test:

```python
def test_missing_dependency_ends_only_the_current_agent_run():
    tool = MissingDependencyTool()
    model = ScriptedModel(
        [AIMessage(content="", tool_calls=[tool_call("missing")])]
    )

    result = run_graph(tool, model)

    assert tool.executions == [{"code": "bad"}]
    assert result["terminal_error"] == {
        "code": "DEPENDENCY_NOT_AVAILABLE",
        "message": "Package “sklearn” isn’t installed.",
        "recoverable": False,
    }
```

- [ ] **Step 2: Run the graph regression test**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_tool_failure_recovery.py::test_missing_dependency_ends_only_the_current_agent_run
```

Expected: PASS, confirming the existing shared terminal-error path handles this typed failure without an uncaught exception.

- [ ] **Step 3: Add an API projection regression test**

Add `from types import SimpleNamespace` to `tests/test_tool_tracking.py`, then add:

```python
def test_missing_dependency_projects_a_concise_public_run_failure():
    snapshot = SimpleNamespace(
        values={
            "messages": [],
            "artifacts": {},
            "terminal_error": {
                "code": "DEPENDENCY_NOT_AVAILABLE",
                "message": "Package “sklearn” isn’t installed.",
                "recoverable": False,
            },
        },
        next=(),
        interrupts=(),
    )

    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={"state": "done", "steps": 1},
    )

    assert projected.run.state == "error"
    assert projected.run.error_code == "DEPENDENCY_NOT_AVAILABLE"
    assert projected.run.user_message == "Package “sklearn” isn’t installed."
```

- [ ] **Step 4: Run the API projection regression test**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_tool_tracking.py::test_missing_dependency_projects_a_concise_public_run_failure
```

Expected: PASS.

- [ ] **Step 5: Add a dependency-specific interface regression test**

Add to `frontend/src/App.test.tsx`, beside `shows a safe terminal failure and keeps the composer usable`:

```tsx
  it("shows a concise missing-package failure and keeps the composer usable", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "error",
              steps: 1,
              error: "Package “sklearn” isn’t installed.",
              error_code: "DEPENDENCY_NOT_AVAILABLE",
              user_message: "Package “sklearn” isn’t installed.",
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Fit the model" },
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

    const composer = await screen.findByLabelText(
      "Ask a question about your dataset!",
    );
    fireEvent.change(composer, { target: { value: "Fit the model" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Package “sklearn” isn’t installed.",
    );
    expect(composer).not.toBeDisabled();
    fireEvent.change(composer, { target: { value: "Retry with statsmodels" } });
    expect(screen.getByRole("button", { name: "Send" })).not.toBeDisabled();
  });
```

- [ ] **Step 6: Run the interface test**

Run from `frontend/`:

```bash
npm test -- --run App.test.tsx
```

Expected: PASS. If Node/npm is unavailable on the implementation host, record that limitation and run this command on the first Node-capable machine before merging.

- [ ] **Step 7: Commit the non-crashing interface coverage**

```bash
git add \
  tests/test_tool_failure_recovery.py \
  tests/test_tool_tracking.py \
  frontend/src/App.test.tsx
git commit -m "test: keep missing dependency failures nonfatal"
```

---

### Task 3: Define the native and Docker installation procedure

**Files:**
- Modify: `README.md:132-135`
- Modify: `tools/python_execution_policy.md:76-86`
- Modify: `tests/test_guarded_local_startup.py:182-197`

**Interfaces:**
- Consumes: fixed, pinned dependencies declared in `requirements.txt`.
- Produces: one native operator procedure and one Docker operator procedure, both ending with import verification and application restart.
- Produces: a documented retry contract requiring a new user message after the environment update.

- [ ] **Step 1: Strengthen the documentation contract test**

Extend `test_documentation_names_guarded_local_and_explains_the_boundary` in `tests/test_guarded_local_startup.py` with:

```python
    assert "Package “<module>” isn’t installed." in combined
    assert "distribution==version" in combined
    assert "uv pip install -r requirements.txt" in readme
    assert "docker compose build" in readme
    assert "docker compose exec epi-agent python -c" in readme
    assert "sklearn" in combined
    assert "scikit-learn" in combined
    assert "running container" in combined
    assert "new request" in combined
```

- [ ] **Step 2: Run the documentation test and confirm it fails**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_guarded_local_startup.py::test_documentation_names_guarded_local_and_explains_the_boundary
```

Expected: FAIL because the operator procedures are not documented yet.

- [ ] **Step 3: Add the operator workflow to the README**

After the fixed-package paragraph in `README.md`, add a section named `### Adding an approved Python package` that states:

````markdown
If an analysis imports an unavailable module, the run stops with
`Package “<module>” isn’t installed.` The application remains available. Retry
with an installed library, or update the environment before sending a new
request.

Package changes are operator-managed. Do not copy the import name directly
into an installer command: imports and distributions can differ, such as
`sklearn` and `scikit-learn`. Review the package, select an exact version, and
add `distribution==version` to `requirements.txt`.

For a native installation, stop Epi-AI-Agent and run:

```bash
source .venv/bin/activate
uv pip install -r requirements.txt
python -c "import xxx"
python run_fastapi.py
```

Replace `xxx` with the verified import name. Installing while the service is
running is unsupported.

For Docker, stop the service and rebuild from the updated requirements:

```bash
docker compose down
docker compose build
docker compose up -d
docker compose exec epi-agent python -c "import xxx"
```

Do not install into a running container; that change disappears when the
container is replaced. After verification, send a new request to retry the
analysis. Failed generated code is never replayed automatically.
````

- [ ] **Step 4: Make the policy normative**

Expand `## Network and packages` in `tools/python_execution_policy.md` with the same requirements, without duplicating all shell commands:

```markdown
An unavailable import returns `DEPENDENCY_NOT_AVAILABLE` with
`Package “<module>” isn’t installed.` and structured `missing_module` detail.
The current analysis run ends; the application and later user requests remain
available.

Adding a dependency is a trusted operator action. The operator must identify
the correct distribution, review it, add an exact `distribution==version` pin
to `requirements.txt`, rebuild or reinstall the complete environment, verify
the import, and restart the application. Import and distribution names may
differ, such as `sklearn` and `scikit-learn`. Installing while the native
service is running or directly into a running container is unsupported. The
user sends a new request after the update; the application does not replay the
failed code automatically.
```

- [ ] **Step 5: Run the documentation and policy tests**

Run:

```bash
../../.venv/bin/python -m pytest -q \
  tests/test_guarded_local_startup.py \
  tests/test_python_execution_policy.py
```

Expected: PASS.

- [ ] **Step 6: Commit the operator workflow**

```bash
git add README.md tools/python_execution_policy.md tests/test_guarded_local_startup.py
git commit -m "docs: define approved Python package updates"
```

---

### Task 4: Full verification

**Files:**
- Verify only; no planned modifications.

**Interfaces:**
- Consumes: all implementation and documentation changes from Tasks 1-3.
- Produces: evidence that the deterministic Python suite and frontend suite remain green and the worktree contains only intended commits.

- [ ] **Step 1: Run the deterministic Python suite**

Run:

```bash
../../.venv/bin/python -m pytest -q
```

Expected: all deterministic tests pass; only the opt-in real Docker smoke may skip when `RUN_DOCKER_SMOKE` is unset.

- [ ] **Step 2: Run frontend tests and build**

Run from `frontend/`:

```bash
npm test
npm run build
```

Expected: both commands pass. If Node/npm is unavailable, report both commands as unverified rather than claiming success.

- [ ] **Step 3: Run the optional Docker smoke where Docker is available**

Run from the repository root:

```bash
RUN_DOCKER_SMOKE=1 ../../.venv/bin/python -m pytest -q -s tests/test_docker_smoke.py
```

Expected: PASS on a Docker-capable host. If the daemon is unavailable, retain and report the test's explicit skip reason.

- [ ] **Step 4: Audit terminology and package-installation boundaries**

Run:

```bash
rg -n "DEPENDENCY_NOT_AVAILABLE|missing_module|isn’t installed|pip install|conda|uv pip" \
  epi_agent tools README.md tests frontend/src
git diff --check
git status --short --branch
```

Expected: generated-code policy still blocks installers; operator commands appear only in trusted setup documentation; no whitespace errors or unrelated changes are present.

- [ ] **Step 5: Commit any verification-only fixture update if required**

No commit is expected. If the frontend build deterministically updates the committed `frontend/dist` assets, inspect the manifest and asset diff, add only those generated files, and commit:

```bash
git add frontend/dist
git commit -m "build: refresh guarded local frontend assets"
```
