# Guarded Local Python and Optional Docker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship one cross-platform Guarded Local Python execution path, with explicit user acknowledgement and an optional hardened Docker deployment around the whole application.

**Architecture:** Keep `LocalPythonRuntime` as the only generated-Python executor on native macOS, Linux/HPC, and inside Docker. Port the typed static-policy and one-repair agent behavior from `design/python-execution-policy`, harden the existing worker subprocess, expose honest Guarded Local provenance, and make Docker an optional whole-application host boundary rather than a second executor.

**Tech Stack:** Python 3.12, Pydantic, LangGraph, FastAPI/Uvicorn, pytest, Docker Engine/Docker Desktop, Docker Compose

**Spec:** `docs/superpowers/specs/2026-09-22-guarded-local-python-docker-design.md`

## Global Constraints

- Native Guarded Local must run on macOS and Linux/HPC with Python 3.12 and must never be called a sandbox.
- `LocalPythonRuntime` is the only Python executor; do not add an executor registry, capability probe, dedicated runtime path, Bubblewrap, seccomp, Seatbelt, Windows job objects, WebAssembly, or remote execution.
- Generated Python runs with the same operating-system authority as the application. Static validation and resource controls are guardrails, not a security boundary.
- Generated Python receives one selected in-memory `dataset`, no provider credentials, no proxy settings, no Python path overrides, no filesystem paths, and no permission controls.
- Generated Python cannot use the host filesystem, network clients, process-control APIs, environment discovery, dynamic execution, interactive input, or package installation.
- Permit exactly one changed corrected Python call after `POLICY_BLOCKED`; an unchanged retry or second policy block exhausts Python only for the current user turn.
- Dynamic package installation, environment generations, package approval, and arbitrary network approval are outside this release. Missing imports remain `DEPENDENCY_NOT_AVAILABLE`.
- Startup requires `REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED=1`; native interactive acceptance persists to `.env`, while non-interactive startup fails when the value is absent.
- Runtime provenance must report `execution_mode: guarded_local` and `security_boundary: none`.
- Docker protects the host from the whole application. It does not isolate generated Python from application files, environment, processes, or networking inside the container.
- Docker must run as a non-root user, publish only `127.0.0.1:8080`, use a named runtime volume, offer only an optional narrow read-only study-data mount, drop all capabilities, set `no-new-privileges`, and never mount the container engine socket.
- The deterministic test suite must not require Docker. Real Docker coverage is opt-in and skips with a precise reason when Docker is unavailable.
- Preserve the user's dirty `master` files and the unrelated `config/vllm_amarel/load_llm_amarel_instruction.md` edit in `design/python-execution-policy`.
- Follow strict red-green-refactor TDD: add one behavior test, run it and observe the expected failure, add the minimum production change, rerun the focused test, then run the affected test module before committing.

## File Map

- `tools/execution_policy.py`: typed AST policy validation, stable rule IDs, bounded findings, alias/bypass protection, and user-facing correction text.
- `epi_agent/runtimes/python/models.py`: typed runtime request/result/failure models and Guarded Local provenance.
- `epi_agent/runtimes/python/local_process.py`: the sole generated-Python executor; child environment, private run directories, limits, bounded pipe collection, cancellation, timeout, and process-group cleanup.
- `epi_agent/runtimes/python/worker.py`: trusted child entry point; loads one selected dataset, executes validated code, and writes bounded typed output.
- `epi_agent/runtime.py`: shared tool loop and current-turn Python policy-repair budget.
- `epi_agent/agent.py` and `epi_agent/tool_packs/analysis/python/tools.py`: pre-call model guidance and structured error propagation.
- `api/schemas.py`, `api/app.py`, and `epi_agent/tool_packs/analysis/review.py`: capability/status text and review-visible runtime provenance.
- `run_fastapi.py`: acknowledgement, interactive persistence, non-interactive failure, and container-only non-loopback binding.
- `Dockerfile`, `.dockerignore`, `compose.yaml`, and `compose.study-data.yaml`: optional whole-application Docker deployment.
- `tests/test_python_execution_policy.py`: typed policy and runtime/tool diagnostic tests migrated from commit `647eeb2`.
- `tests/test_python_policy_recovery.py`: one-repair current-turn recovery tests migrated from commit `647eeb2`.
- `tests/test_guarded_local_runtime.py`: worker, environment, resource, timeout, cancellation, pipe, and descendant-cleanup coverage.
- `tests/test_guarded_local_startup.py`: acknowledgement, provenance, capability, and bind-address behavior.
- `tests/test_docker_deployment.py`: deterministic checks for image and Compose security contracts.
- `tests/docker_smoke_app.py` and `tests/test_docker_smoke.py`: opt-in real-container FastAPI/worker/review/persistence smoke test.
- `tools/python_execution_policy.md` and `README.md`: operator-facing policy and native/Docker instructions.

---

### Task 1: Migrate Typed Static Policy and Structured Diagnostics

**Files:**
- Create: `tests/test_python_execution_policy.py`
- Modify: `tools/execution_policy.py`
- Modify: `epi_agent/agent.py`
- Modify: `epi_agent/runtimes/python/local_process.py`
- Modify: `epi_agent/runtimes/python/models.py`
- Modify: `epi_agent/tool_packs/analysis/python/tools.py`

**Interfaces:**
- Consumes: `validate_generated_code(code: str)` currently returning `dict[str, str] | None`; `PythonRuntimeFailure`; the existing custom-Python tool adapter.
- Produces: `PythonPolicyViolation`, `PythonCodeValidation`, `PYTHON_EXECUTION_POLICY_GUIDANCE`, and `validate_generated_code(code: str) -> PythonCodeValidation`; `PythonRuntimeFailure.details: dict[str, Any] | None`.

- [ ] **Step 1: Add the migrated policy tests before production changes**

Use the final hardened test artifact as the exact source, but add it to this branch with `apply_patch` so no policy-document or Bubblewrap paths are imported:

```bash
git show 647eeb2:tests/test_python_execution_policy.py
```

The resulting `tests/test_python_execution_policy.py` must be byte-for-byte equivalent to that output. It covers typed success, syntax diagnostics, stable rule IDs, preloaded aliases, reader aliases, star imports, reassignment bypasses, bounded/sorted findings, runtime propagation, prompt guidance, and tool-schema guidance. Verify these representative expectations remain present:

```python
def test_allowed_code_returns_typed_success():
    result = validate_generated_code("print(dataset.shape)")
    assert result.failure_code is None
    assert result.violations == ()


def test_distinct_violations_are_sorted_and_capped():
    lines = ["import requests", "open('/tmp/x')", "eval('1')"]
    lines.extend(f"open('/tmp/{index}')" for index in range(12))
    result = validate_generated_code("\n".join(lines))
    assert result.failure_code == "POLICY_BLOCKED"
    assert len(result.violations) == 10
    assert result.truncated is True
```

- [ ] **Step 2: Run the policy tests and observe the intended red state**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_python_execution_policy.py
```

Expected: FAIL during assertions or collection because the current validator returns a dictionary/`None`, `PythonRuntimeFailure` has no `details`, and the current prompt/tool descriptions do not carry the typed policy contract.

- [ ] **Step 3: Apply only the hardened policy and diagnostic code from the approved source commits**

Inspect the exact production diff:

```bash
git diff 2496a41..647eeb2 -- tools/execution_policy.py epi_agent/agent.py epi_agent/runtimes/python/local_process.py epi_agent/runtimes/python/models.py epi_agent/tool_packs/analysis/python/tools.py
```

Apply those hunks with `apply_patch`. Do not apply either policy Markdown file and do not copy any file from commits after `647eeb2`. The final public shapes must be:

```python
@dataclass(frozen=True)
class PythonPolicyViolation:
    rule_id: str
    operation: str
    message: str
    correction: str
    line: int
    column: int


@dataclass(frozen=True)
class PythonCodeValidation:
    failure_code: Literal["INVALID_PYTHON_SYNTAX", "POLICY_BLOCKED"] | None = None
    message: str = ""
    violations: tuple[PythonPolicyViolation, ...] = ()
    truncated: bool = False
    line: int | None = None
    column: int | None = None

    def as_details(self) -> dict[str, object]:
        details: dict[str, object] = {}
        if self.violations:
            details["violations"] = [asdict(item) for item in self.violations]
            details["violations_truncated"] = self.truncated
        if self.line is not None:
            details["line"] = self.line
        if self.column is not None:
            details["column"] = self.column
        return details


def validate_generated_code(code: str) -> PythonCodeValidation:
    """Parse source and return every bounded, source-ordered violation."""
```

`PythonRuntimeFailure` must defensively JSON-copy `details`, `LocalPythonRuntime.execute()` must reject syntax/policy failures before making a temporary directory, and `_runtime_failure()` must preserve details while adding `category`.

- [ ] **Step 4: Run the focused policy suite**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_python_execution_policy.py
```

Expected: PASS, with all policy, adapter, prompt, and schema cases green.

- [ ] **Step 5: Run existing failure-recovery regression coverage**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_tool_failure_recovery.py
```

Expected: PASS; structured details do not double-encode existing tool errors.

- [ ] **Step 6: Commit the policy slice**

```bash
git add -f tests/test_python_execution_policy.py
git add tools/execution_policy.py epi_agent/agent.py epi_agent/runtimes/python/local_process.py epi_agent/runtimes/python/models.py epi_agent/tool_packs/analysis/python/tools.py
git commit -m "feat: add typed guarded Python policy"
```

---

### Task 2: Migrate the One-Repair Current-Turn Recovery Contract

**Files:**
- Create: `tests/test_python_policy_recovery.py`
- Modify: `epi_agent/runtime.py`

**Interfaces:**
- Consumes: tool name `analysis-run_custom_python`; structured `POLICY_BLOCKED` details produced by Task 1; existing `failure_signatures` graph state.
- Produces: a one-attempt policy-repair budget scoped to the current user turn; `POLICY_REPAIR_EXHAUSTED` with `python_available: false`, `repair_attempts_remaining: 0`, and `executed: bool`.

- [ ] **Step 1: Add the hardened recovery tests before touching the shared loop**

Use this immutable source and add the exact output with `apply_patch`:

```bash
git show 647eeb2:tests/test_python_policy_recovery.py
```

Keep all cases, including a changed safe retry, unchanged code with changed narrative, a changed second block, third-call rejection, continued access to non-Python tools, syntax errors not consuming policy budget, reset after a new human message, follow-up patch preservation, canonicalized arguments, and same-batch calls.

- [ ] **Step 2: Run the recovery module and verify red**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_python_policy_recovery.py
```

Expected: FAIL because the shared loop currently treats Python policy blocks as ordinary recoverable tool failures and does not remove only the Python tool after exhaustion.

- [ ] **Step 3: Apply the isolated recovery diff**

Inspect and apply only this exact code diff with `apply_patch`:

```bash
git diff 2496a41..647eeb2 -- epi_agent/runtime.py
```

The implementation must retain these constants and semantics:

```python
_PYTHON_TOOL_NAME = "analysis-run_custom_python"
_PYTHON_POLICY_BLOCKED_CODE = "POLICY_BLOCKED"
_PYTHON_POLICY_EXHAUSTED_CODE = "POLICY_REPAIR_EXHAUSTED"
_PYTHON_POLICY_REPAIR_LIMIT = 1
```

Canonical duplicate detection compares the Python `code` value, not narrative fields. Exhaustion removes only `_PYTHON_TOOL_NAME` from subsequent bound schemas for the current human turn; it must not set a terminal conversation error and must not remove other tools.

- [ ] **Step 4: Run focused and shared recovery suites**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_python_policy_recovery.py tests/test_tool_failure_recovery.py
```

Expected: PASS; both the Python-specific and generic recovery contracts remain green.

- [ ] **Step 5: Commit the recovery slice**

```bash
git add -f tests/test_python_policy_recovery.py
git add epi_agent/runtime.py
git commit -m "feat: bound guarded Python policy recovery"
```

---

### Task 3: Harden the Sole Local Worker Runtime

**Files:**
- Create: `tests/test_guarded_local_runtime.py`
- Modify: `epi_agent/runtimes/python/local_process.py`
- Modify: `epi_agent/runtimes/python/worker.py`

**Interfaces:**
- Consumes: `LocalPythonRuntime.execute(request, datasets)`, `CancellationToken`, `bind_cancellation`, and the typed failures from Task 1.
- Produces: `_child_environment(run_dir: Path) -> dict[str, str]`, `_worker_command() -> list[str]`, `_communicate_bounded(process, *, deadline: float) -> tuple[bytes, bytes]`, `MAX_CHILD_STDOUT_BYTES`, and `MAX_CHILD_STDERR_BYTES`.

- [ ] **Step 1: Add an end-to-end selected-dataset test**

Add this test first:

```python
def test_runtime_executes_against_only_the_selected_dataset(tmp_path):
    runtime = LocalPythonRuntime(runtime_root=tmp_path, timeout_seconds=10)
    result = runtime.execute(
        PythonExecutionRequest(
            code="print(int(dataset['cases'].sum()))",
            selected_dataset_id="selected",
        ),
        {
            "selected": pd.DataFrame({"cases": [2, 3]}),
            "other": pd.DataFrame({"cases": [100]}),
        },
    )
    assert result.output_text.strip() == "5"
    assert result.figure_png == b""
    assert result.runtime.language == "Python"
```

- [ ] **Step 2: Run the selected-dataset test and establish its state**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_guarded_local_runtime.py::test_runtime_executes_against_only_the_selected_dataset
```

Expected: PASS against the existing worker. This is a characterization prerequisite, not the red test for a production change; do not modify production code in this step.

- [ ] **Step 3: Add red tests for environment sanitization and fixed analysis values**

Add tests that call the new pure helper and assert the complete contract:

```python
def test_child_environment_omits_credentials_proxies_and_python_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test")
    monkeypatch.setenv("PYTHONPATH", "/host/code")
    monkeypatch.setenv("PATH", "/usr/bin")
    environment = _child_environment(tmp_path)
    assert environment["PATH"] == "/usr/bin"
    assert environment["MPLBACKEND"] == "Agg"
    assert environment["MPLCONFIGDIR"] == str(tmp_path / "matplotlib")
    assert environment["OPENBLAS_NUM_THREADS"] == "1"
    assert "OPENAI_API_KEY" not in environment
    assert "HTTPS_PROXY" not in environment
    assert "PYTHONPATH" not in environment
```

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_guarded_local_runtime.py::test_child_environment_omits_credentials_proxies_and_python_overrides
```

Expected: FAIL at import because `_child_environment` does not exist.

- [ ] **Step 4: Extract the child-environment and worker-command helpers**

Add the minimal helpers and replace the inline equivalents:

```python
def _child_environment(run_dir: Path) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in _ALLOWED_ENVIRONMENT
        if name in os.environ
    }
    environment.update({
        "PYTHONUTF8": "1",
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(run_dir / "matplotlib"),
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
    })
    return environment


def _worker_command() -> list[str]:
    return [sys.executable, str(Path(__file__).with_name("worker.py"))]
```

Run the focused environment test again; expected: PASS.

- [ ] **Step 5: Add red tests for bounded native pipes**

Create a real helper child through `_worker_command` monkeypatching that writes more than the configured stderr limit without producing `result.json`. Assert that execution terminates promptly with a typed failure:

```python
def test_native_stderr_is_bounded_before_process_exit(monkeypatch, tmp_path):
    script = tmp_path / "flood.py"
    script.write_text(
        "import sys, time\n"
        "sys.stderr.buffer.write(b'x' * 200_000)\n"
        "sys.stderr.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(local_process, "MAX_CHILD_STDERR_BYTES", 16_384)
    monkeypatch.setattr(local_process, "_worker_command", lambda: [sys.executable, str(script)])
    runtime = LocalPythonRuntime(runtime_root=tmp_path, timeout_seconds=5)
    with pytest.raises(PythonRuntimeFailure) as caught:
        runtime.execute(_request("print('unused')"), _datasets())
    assert caught.value.code == "PROCESS_OUTPUT_TOO_LARGE"
    assert caught.value.category == "invalid_output"
```

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_guarded_local_runtime.py::test_native_stderr_is_bounded_before_process_exit
```

Expected: FAIL because the current `communicate()` call accumulates native pipes without enforcing a byte bound.

- [ ] **Step 6: Implement nonblocking bounded pipe collection**

Use `selectors.DefaultSelector`, set both child pipe file descriptors nonblocking with `os.set_blocking`, read chunks no larger than 65,536 bytes, and check `cancellation_point()` plus the monotonic deadline on every loop. Raise this internal exception as soon as either cap is exceeded:

```python
class _NativeOutputLimit(RuntimeError):
    pass


MAX_CHILD_STDOUT_BYTES = 100_000
MAX_CHILD_STDERR_BYTES = 100_000


def _communicate_bounded(process: subprocess.Popen[bytes], *, deadline: float) -> tuple[bytes, bytes]:
    streams = {
        "stdout": (process.stdout, bytearray(), MAX_CHILD_STDOUT_BYTES),
        "stderr": (process.stderr, bytearray(), MAX_CHILD_STDERR_BYTES),
    }
    selector = selectors.DefaultSelector()
    try:
        for name, (stream, _buffer, _limit) in streams.items():
            if stream is None:
                continue
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, data=name)
        while selector.get_map() or process.poll() is None:
            cancellation_point()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, 0)
            for key, _mask in selector.select(min(0.1, remaining)):
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                _stream, buffer, limit = streams[str(key.data)]
                buffer.extend(chunk)
                if len(buffer) > limit:
                    raise _NativeOutputLimit(str(key.data))
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
        return bytes(streams["stdout"][1]), bytes(streams["stderr"][1])
    finally:
        selector.close()
```

Replace the repeated `process.communicate(timeout=min(0.1, remaining))` loop with `_communicate_bounded`. Catch `_NativeOutputLimit`, terminate the process group, and raise:

```python
_failure(
    "PROCESS_OUTPUT_TOO_LARGE",
    "Python worker stdout or stderr exceeded its size limit.",
    category="invalid_output",
    recoverable=True,
)
```

Do not truncate and continue reading; terminate the process group immediately so an infinite writer cannot consume CPU indefinitely. Rerun the focused pipe test; expected: PASS.

- [ ] **Step 7: Add red tests for timeout, cancellation, and descendants**

Add real-process tests using a helper script that starts one sleeping descendant and writes its PID to a test-provided path. The timeout case must raise `EXECUTION_TIMEOUT`; the cancellation case must raise `RunCancelled`; both must poll `os.kill(pid, 0)` until it raises `ProcessLookupError`:

```python
def _sleeping_worker(tmp_path: Path) -> tuple[Path, Path]:
    pid_file = tmp_path / "descendant.pid"
    script = tmp_path / "sleeping_worker.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    return script, pid_file


def test_timeout_terminates_worker_descendants(monkeypatch, tmp_path):
    script, pid_file = _sleeping_worker(tmp_path)
    monkeypatch.setattr(local_process, "_worker_command", lambda: [sys.executable, str(script)])
    runtime = LocalPythonRuntime(runtime_root=tmp_path, timeout_seconds=0.5)
    with pytest.raises(PythonRuntimeFailure) as caught:
        runtime.execute(_request("print('unused')"), _datasets())
    assert caught.value.code == "EXECUTION_TIMEOUT"
    _assert_process_disappears(int(pid_file.read_text(encoding="utf-8")))


def test_cancellation_terminates_worker_descendants(monkeypatch, tmp_path):
    script, pid_file = _sleeping_worker(tmp_path)
    monkeypatch.setattr(local_process, "_worker_command", lambda: [sys.executable, str(script)])
    token = CancellationToken()
    canceller = threading.Thread(target=_cancel_after_file_exists, args=(token, pid_file))
    canceller.start()
    with bind_cancellation(token), pytest.raises(RunCancelled):
        LocalPythonRuntime(runtime_root=tmp_path, timeout_seconds=5).execute(
            _request("print('unused')"), _datasets()
        )
    canceller.join(timeout=2)
    _assert_process_disappears(int(pid_file.read_text(encoding="utf-8")))
```

For cancellation, bind a `CancellationToken` in the test thread and use a short-lived helper thread to call `token.cancel()` after the descendant PID appears. Also add a Popen-start failure test proving no unbound `process` variable is referenced.

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_guarded_local_runtime.py -k 'timeout or cancellation or process_start'
```

Expected: at least the start-failure case FAILS against the current exception structure; after Step 6, descendant assertions expose any incomplete group cleanup.

- [ ] **Step 8: Make process lifecycle cleanup total**

Initialize `process: subprocess.Popen[bytes] | None = None` before `Popen`. Call `_terminate_process_group(process)` only when a process exists. Ensure timeout, cancellation, pipe overflow, and errors after child creation all terminate and reap the process group before the temporary directory exits. Preserve `RunCancelled` without wrapping it in `PythonRuntimeFailure`.

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_guarded_local_runtime.py
```

Expected: PASS for selected dataset, clean environment, resource-spec coverage, pipe bounds, timeout, cancellation, descendant cleanup, malformed results, and output/figure size checks.

- [ ] **Step 9: Commit the runtime hardening slice**

```bash
git add -f tests/test_guarded_local_runtime.py
git add epi_agent/runtimes/python/local_process.py epi_agent/runtimes/python/worker.py
git commit -m "fix: harden guarded Python worker lifecycle"
```

---

### Task 4: Report Guarded Local Capability and Provenance Honestly

**Files:**
- Modify: `tests/test_guarded_local_runtime.py`
- Create: `tests/test_guarded_local_startup.py`
- Modify: `epi_agent/runtimes/python/models.py`
- Modify: `epi_agent/runtimes/python/worker.py`
- Modify: `epi_agent/tool_packs/analysis/review.py`
- Modify: `api/schemas.py`
- Modify: `api/app.py`
- Modify: `api/runtime.py`
- Modify: `frontend/src/types.ts`

**Interfaces:**
- Consumes: `PythonRuntimeManifest`, `RuntimeCapabilities`, `/api/runtime/options`, and review runtime filtering.
- Produces: `PythonRuntimeManifest.execution_mode: Literal["guarded_local"]`, `PythonRuntimeManifest.security_boundary: Literal["none"]`, and `RuntimeCapabilities.python_execution`.

- [ ] **Step 1: Add red runtime-manifest assertions**

Extend the real selected-dataset test:

```python
assert result.runtime.execution_mode == "guarded_local"
assert result.runtime.security_boundary == "none"
```

Run the test. Expected: FAIL because the fields are absent.

- [ ] **Step 2: Add provenance fields to the model and worker envelope**

Implement:

```python
class PythonRuntimeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    language: Literal["Python"] = "Python"
    version: str = Field(min_length=1)
    packages: list[RuntimePackage] = Field(default_factory=list)
    execution_mode: Literal["guarded_local"] = "guarded_local"
    security_boundary: Literal["none"] = "none"
```

Have `worker.py` emit both keys explicitly. Add both names to `_RUNTIME_KEYS` in `analysis/review.py` so human review retains them. Rerun the manifest test; expected: PASS.

- [ ] **Step 3: Add a red capability-schema test**

Add:

```python
def test_runtime_capability_names_guarded_local_without_claiming_sandbox():
    capability = RuntimeCapabilities(
        publication_knowledge=RuntimeCapability(status="available", message="ok"),
        db_rag_dataset=RuntimeCapability(status="not_configured", message="none"),
    ).python_execution
    assert capability.status == "available"
    assert "Guarded Local" in capability.message
    assert "host permissions" in capability.message
    assert "sandbox" not in capability.message.casefold()
```

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_guarded_local_startup.py::test_runtime_capability_names_guarded_local_without_claiming_sandbox
```

Expected: FAIL because `python_execution` is not part of `RuntimeCapabilities`.

- [ ] **Step 4: Add the always-available Python capability**

Add a default field in `api/schemas.py` and use the same exact value in `api/app.py`:

```python
python_execution: RuntimeCapability = Field(
    default_factory=lambda: RuntimeCapability(
        status="available",
        message=(
            "Generated Python is available in Guarded Local mode with host "
            "permissions and no operating-system security boundary."
        ),
    )
)
```

Update `frontend/src/types.ts` so its runtime-capability interface includes `python_execution`. Do not add executor selection or unavailable states. Rerun the focused test; expected: PASS.

- [ ] **Step 5: Test review-visible provenance**

Add a test that constructs an `AnalysisRun` with the two fields, passes it through `_review_payload`, and asserts both survive while private keys remain filtered. Run the full startup/runtime tests:

```bash
../../.venv/bin/python -m pytest -q tests/test_guarded_local_runtime.py tests/test_guarded_local_startup.py
```

Expected: PASS.

- [ ] **Step 6: Commit the provenance slice**

```bash
git add -f tests/test_guarded_local_startup.py tests/test_guarded_local_runtime.py
git add epi_agent/runtimes/python/models.py epi_agent/runtimes/python/worker.py epi_agent/tool_packs/analysis/review.py api/schemas.py api/app.py api/runtime.py frontend/src/types.ts
git commit -m "feat: report guarded local Python provenance"
```

---

### Task 5: Require Persisted Guarded Local Acknowledgement

**Files:**
- Modify: `tests/test_guarded_local_startup.py`
- Modify: `run_fastapi.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `persist_local_env_values(project_root, values)` and `StartupConfigurationError`.
- Produces: `GUARDED_PYTHON_ACK_ENV`, the `ensure_guarded_python_acknowledgement` function, and container-aware host validation.

- [ ] **Step 1: Add acknowledgement tests first**

Add these behaviors with injected `input_fn`, `persist_fn`, and `interactive` so tests never depend on a real terminal:

```python
def test_existing_acknowledgement_does_not_prompt(tmp_path):
    prompts = []
    ensure_guarded_python_acknowledgement(
        project_root=tmp_path,
        environ={GUARDED_PYTHON_ACK_ENV: "1"},
        input_fn=lambda prompt: prompts.append(prompt) or "no",
        interactive=True,
    )
    assert prompts == []


def test_interactive_acceptance_sets_and_persists_acknowledgement(tmp_path):
    environ = {}
    persisted = []
    ensure_guarded_python_acknowledgement(
        project_root=tmp_path,
        environ=environ,
        input_fn=lambda _prompt: "yes",
        persist_fn=lambda root, values: persisted.append((root, values)),
        interactive=True,
    )
    assert environ[GUARDED_PYTHON_ACK_ENV] == "1"
    assert persisted == [(tmp_path, {GUARDED_PYTHON_ACK_ENV: "1"})]


@pytest.mark.parametrize("answer", ["", "n", "no"])
def test_declined_acknowledgement_stops_startup(answer, tmp_path):
    with pytest.raises(StartupConfigurationError, match=GUARDED_PYTHON_ACK_ENV):
        ensure_guarded_python_acknowledgement(
            project_root=tmp_path,
            environ={},
            input_fn=lambda _prompt: answer,
            interactive=True,
        )


def test_cancelled_acknowledgement_stops_startup(tmp_path):
    def cancel(_prompt):
        raise EOFError
    with pytest.raises(StartupConfigurationError, match=GUARDED_PYTHON_ACK_ENV):
        ensure_guarded_python_acknowledgement(
            project_root=tmp_path,
            environ={},
            input_fn=cancel,
            interactive=True,
        )


def test_noninteractive_startup_requires_explicit_acknowledgement(tmp_path):
    with pytest.raises(StartupConfigurationError, match=GUARDED_PYTHON_ACK_ENV):
        ensure_guarded_python_acknowledgement(
            project_root=tmp_path,
            environ={},
            interactive=False,
        )
```

Assert the error includes the exact variable name and says generated Python uses local user-account permissions.

- [ ] **Step 2: Run acknowledgement tests and verify red**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_guarded_local_startup.py -k acknowledgement
```

Expected: FAIL at import because the acknowledgement API does not exist.

- [ ] **Step 3: Implement acknowledgement before provider setup**

Add:

```python
GUARDED_PYTHON_ACK_ENV = "REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED"
_GUARDED_PYTHON_WARNING = (
    "Generated Python runs with your local user-account permissions. "
    "Epi-AI-Agent applies policy checks and resource limits, but this is "
    "not an operating-system sandbox."
)


def ensure_guarded_python_acknowledgement(
    *,
    project_root: str | Path = PROJECT_ROOT,
    environ: MutableMapping[str, str] = os.environ,
    input_fn: Callable[[str], str] = input,
    persist_fn: Callable[[str | Path, dict[str, str]], None] = persist_local_env_values,
    interactive: bool | None = None,
) -> None:
    if environ.get(GUARDED_PYTHON_ACK_ENV, "").strip() == "1":
        return
    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    error_message = (
        f"{_GUARDED_PYTHON_WARNING} Set {GUARDED_PYTHON_ACK_ENV}=1 "
        "after reviewing this warning."
    )
    if not is_interactive:
        raise StartupConfigurationError(error_message)
    try:
        answer = input_fn(f"{_GUARDED_PYTHON_WARNING}\nContinue? [y/N]: ").strip().casefold()
    except (EOFError, KeyboardInterrupt) as error:
        raise StartupConfigurationError(error_message) from error
    if answer not in {"y", "yes"}:
        raise StartupConfigurationError(error_message)
    environ[GUARDED_PYTHON_ACK_ENV] = "1"
    persist_fn(project_root, {GUARDED_PYTHON_ACK_ENV: "1"})
```

Only the exact normalized value `"1"` counts as previously accepted. When `interactive is None`, use `sys.stdin.isatty()`. Accept `y` or `yes`, persist with the existing atomic `.env` writer, and set the current mapping. Convert EOF/interrupt, decline, or missing non-interactive acknowledgement to `StartupConfigurationError`.

Call the helper in `main()` immediately after `load_app_environment()` and before runtime/provider prompts. Add the variable with explanatory comments to `.env.example` but leave it unset so consent is not pre-granted. Run the focused acknowledgement tests; expected: PASS.

- [ ] **Step 4: Add red bind-address tests**

Add tests for these exact cases:

```python
assert deployment_host("127.0.0.1", {}) == "127.0.0.1"
with pytest.raises(argparse.ArgumentTypeError):
    deployment_host("0.0.0.0", {})
assert deployment_host("0.0.0.0", {"REPORT_AGENT_CONTAINER": "1"}) == "0.0.0.0"
with pytest.raises(argparse.ArgumentTypeError):
    deployment_host("192.168.1.20", {"REPORT_AGENT_CONTAINER": "1"})
```

Run the bind tests. Expected: FAIL because the current `_loopback_host` has no container marker behavior.

- [ ] **Step 5: Implement explicit container-only binding**

Replace `_loopback_host` with:

```python
def deployment_host(
    value: str,
    environ: Mapping[str, str] = os.environ,
) -> str:
    host = value.strip()
    if host == "0.0.0.0" and environ.get("REPORT_AGENT_CONTAINER", "").strip() == "1":
        return host
    if host.casefold() == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "--host must be localhost or a loopback IP address"
        ) from error
    if not address.is_loopback:
        raise argparse.ArgumentTypeError(
            "--host must be localhost or a loopback IP address"
        )
    return host
```

Use `type=deployment_host` in `parse_args`. Native behavior remains loopback-only. Rerun all startup tests; expected: PASS.

- [ ] **Step 6: Commit startup consent and binding**

```bash
git add -f tests/test_guarded_local_startup.py
git add run_fastapi.py .env.example
git commit -m "feat: require guarded Python acknowledgement"
```

---

### Task 6: Add the Optional Hardened Docker Deployment

**Files:**
- Create: `tests/test_docker_deployment.py`
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `compose.yaml`
- Create: `compose.study-data.yaml`

**Interfaces:**
- Consumes: `run_fastapi.py --host 0.0.0.0 --port 8080`, committed `frontend/dist`, `requirements.txt`, and acknowledgement from Task 5.
- Produces: production image target `runtime`, test image target `smoke`, Compose service `epi-agent`, named volume `epi-agent-runtime`, and optional read-only study override.

- [ ] **Step 1: Write deterministic deployment-contract tests**

Use `yaml.safe_load` for Compose and plain text for Dockerfile/ignore checks. Cover all of these assertions:

```python
def test_runtime_image_is_python_312_non_root_and_starts_fastapi():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim AS app-base" in dockerfile
    assert "FROM app-base AS runtime" in dockerfile
    runtime = dockerfile.split("FROM app-base AS runtime", 1)[1]
    assert "USER app" in runtime
    assert 'CMD ["python", "run_fastapi.py", "--host", "0.0.0.0", "--port", "8080"]' in runtime
    assert "frontend/dist" not in _dockerignore_patterns()


def test_docker_context_excludes_secrets_runtime_studies_and_worktrees():
    patterns = _dockerignore_patterns()
    assert {".env", ".worktrees", "runtime", "study_data", "config/custom_models.json"} <= patterns
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in dockerfile
    assert "ANTHROPIC_API_KEY" not in dockerfile
    assert "OPENROUTER_API_KEY" not in dockerfile


def test_compose_publishes_only_loopback_and_hardens_the_service():
    service = _compose()["services"]["epi-agent"]
    assert service["ports"] == ["127.0.0.1:8080:8080"]
    assert service["init"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["pids_limit"] == 256
    assert service["mem_limit"] == "4g"
    assert "/var/run/docker.sock" not in json.dumps(service)


def test_default_compose_mounts_only_named_runtime_storage():
    service = _compose()["services"]["epi-agent"]
    assert service["volumes"] == ["epi-agent-runtime:/app/runtime"]
    assert "epi-agent-runtime" in _compose()["volumes"]


def test_study_override_requires_one_read_only_narrow_mount():
    service = _compose("compose.study-data.yaml")["services"]["epi-agent"]
    assert service["volumes"] == [
        "${REPORT_AGENT_STUDY_DATA:?Set REPORT_AGENT_STUDY_DATA to one study-data directory}:/app/study_data:ro"
    ]


def test_compose_requires_acknowledgement_at_interpolation_time():
    service = _compose()["services"]["epi-agent"]
    value = service["environment"]["REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED"]
    assert value.startswith("${REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED:?")
```

The image test must also prove `.env` is never copied, credentials are not `ARG`/`ENV` in the Dockerfile, `USER app` is active in the final target, and `frontend/dist/index.html` is present in the context rather than built with Node.

- [ ] **Step 2: Run deployment tests and verify red**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_docker_deployment.py
```

Expected: FAIL because none of the four deployment files exist.

- [ ] **Step 3: Add `.dockerignore` with an explicit private-data boundary**

Create exactly the necessary exclusions, including:

```dockerignore
.git
.github
.env
.env.*
!.env.example
.venv
.venv-*
.worktrees
runtime
study_data
local_data
synthetic_report_india
__pycache__
*.py[cod]
.pytest_cache
.uv-cache
frontend/node_modules
node_modules
config/custom_models.json
db_rag/benchmark
```

Do not exclude committed `frontend/dist` or `tests/docker_smoke_app.py`, which the smoke target needs.

- [ ] **Step 4: Add a non-root Python 3.12 image with separate smoke and runtime targets**

Use one dependency/application base, then define `smoke` before the final `runtime` target so an unqualified build produces production, not the harness:

```dockerfile
FROM python:3.12-slim AS app-base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN groupadd --system app && useradd --system --gid app --home /app app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt
COPY --chown=app:app . /app
RUN mkdir -p /app/runtime /app/study_data && chown -R app:app /app/runtime /app/study_data

FROM app-base AS smoke
USER app
CMD ["python", "-m", "uvicorn", "tests.docker_smoke_app:app", "--host", "0.0.0.0", "--port", "8080"]

FROM app-base AS runtime
ENV REPORT_AGENT_CONTAINER=1 REPORT_AGENT_RUNTIME_ROOT=/app/runtime REPORT_AGENT_STUDY_ROOT=/app/study_data REPORT_AGENT_STATIC_DIR=/app/frontend/dist REPORT_AGENT_CHECKPOINT_DB_PATH=/app/runtime/agent_memory_fastapi.db
USER app
EXPOSE 8080
CMD ["python", "run_fastapi.py", "--host", "0.0.0.0", "--port", "8080"]
```

Keep API keys and the acknowledgement out of image layers.

- [ ] **Step 5: Add hardened default and study-data Compose files**

Create `compose.yaml` with this contract:

```yaml
services:
  epi-agent:
    build:
      context: .
      target: runtime
    init: true
    environment:
      REPORT_AGENT_CONTAINER: "1"
      REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED: "${REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED:?Set REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED=1 after reading the Guarded Local warning}"
      OPENAI_API_KEY: "${OPENAI_API_KEY:-}"
      ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY:-}"
      OPENROUTER_API_KEY: "${OPENROUTER_API_KEY:-}"
    ports:
      - "127.0.0.1:8080:8080"
    volumes:
      - epi-agent-runtime:/app/runtime
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    pids_limit: 256
    mem_limit: 4g
    restart: unless-stopped

volumes:
  epi-agent-runtime:
```

`compose.study-data.yaml` adds only:

```yaml
services:
  epi-agent:
    volumes:
      - "${REPORT_AGENT_STUDY_DATA:?Set REPORT_AGENT_STUDY_DATA to one study-data directory}:/app/study_data:ro"
```

Do not mount the source checkout, home directory, credentials, root filesystem, or Docker/Podman socket.

- [ ] **Step 6: Run deterministic deployment checks**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_docker_deployment.py
```

Expected: PASS without contacting a registry or requiring Docker.

- [ ] **Step 7: Commit the Docker deployment slice**

```bash
git add -f tests/test_docker_deployment.py
git add Dockerfile .dockerignore compose.yaml compose.study-data.yaml
git commit -m "feat: add optional hardened Docker deployment"
```

---

### Task 7: Add an Opt-In Real Docker Smoke Test

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/docker_smoke_app.py`
- Create: `tests/test_docker_smoke.py`
- Modify: `Dockerfile`

**Interfaces:**
- Consumes: Docker target `smoke`; production `LocalPythonRuntime`; SQLite; FastAPI; named runtime volume behavior.
- Produces: `RUN_DOCKER_SMOKE=1` integration test proving FastAPI transport, generated Python, review-shaped output, restart persistence, non-root execution, narrow mounts, and host-sentinel absence.

- [ ] **Step 1: Add the smoke test with an explicit skip gate**

The module-level gate must be:

```python
RUN_DOCKER_SMOKE = os.getenv("RUN_DOCKER_SMOKE") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_DOCKER_SMOKE,
    reason="set RUN_DOCKER_SMOKE=1 to run the real Docker integration smoke test",
)
```

When enabled, a fixture must also run `docker version --format '{{.Server.Version}}'`; skip with `Docker daemon is unavailable` if the CLI is absent or the daemon call fails. Do not convert build, startup, HTTP, restart, or assertion failures into skips.

- [ ] **Step 2: Run the normal smoke module and verify the safe skip**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_docker_smoke.py
```

Expected: SKIPPED with the exact opt-in reason; no Docker command runs.

- [ ] **Step 3: Add a minimal FastAPI harness that uses the production executor**

`tests/docker_smoke_app.py` must create `app = FastAPI()` and these endpoints:

```python
class SmokeAnalysisRequest(BaseModel):
    code: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/analysis")
def analysis(request: SmokeAnalysisRequest) -> dict[str, object]:
    result = LocalPythonRuntime(runtime_root="/app/runtime/execution").execute(
        PythonExecutionRequest(code=request.code, selected_dataset_id="smoke"),
        {"smoke": pd.DataFrame({"cases": [2, 3, 5]})},
    )
    review = {
        "output_text": result.output_text,
        "runtime": result.runtime.model_dump(mode="json"),
        "review_status": "pending_review",
    }
    _store_review_in_sqlite(Path("/app/runtime/smoke.sqlite"), review)
    return review


@app.get("/analysis/latest")
def latest_analysis() -> dict[str, object]:
    return _load_review_from_sqlite(Path("/app/runtime/smoke.sqlite"))
```

Use parameterized SQLite statements, JSON with `allow_nan=False`, and one table keyed by a constant ID. The harness exists only in the `smoke` image target and is not the production command.

- [ ] **Step 4: Implement the real container lifecycle assertions**

The enabled test must:

1. create unique image, container, and volume names from the test PID;
2. create a host sentinel at a unique absolute path under `tmp_path`;
3. build `docker build --target smoke -t <image> .`;
4. run with `--read-only`, `--tmpfs /tmp`, `--user app`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit 256`, `--memory 4g`, `-v <volume>:/app/runtime`, and a random loopback host port;
5. wait for `/health` using `httpx` with a bounded deadline;
6. POST `print(int(dataset['cases'].sum()))` and assert output `10`, review status `pending_review`, execution mode `guarded_local`, and security boundary `none`;
7. assert `docker inspect` reports a nonzero user, exactly the named runtime mount, no bind mounts, and no engine socket;
8. use `docker exec` to assert the unique host-sentinel absolute path does not exist;
9. stop/remove the first container, start a second container with the same named volume, and assert `/analysis/latest` returns the same review;
10. always remove containers, image, volume, and the test-created sentinel in `finally` blocks.

Use `subprocess.run(command, check=True, text=True, capture_output=True)` where `command` is a list of individual arguments; never use `shell=True`.

- [ ] **Step 5: Run the real smoke when Docker is available**

Run:

```bash
RUN_DOCKER_SMOKE=1 ../../.venv/bin/python -m pytest -q -s tests/test_docker_smoke.py
```

Expected: PASS when the Docker daemon and base-image registry are available. If the environment truly lacks a reachable daemon, the single test is SKIPPED with `Docker daemon is unavailable`; registry/build failures remain FAILURES.

- [ ] **Step 6: Commit the smoke coverage**

```bash
git add -f tests/__init__.py tests/docker_smoke_app.py tests/test_docker_smoke.py
git add Dockerfile
git commit -m "test: add opt-in Docker execution smoke"
```

---

### Task 8: Document the Policy and Both Supported Launch Paths

**Files:**
- Create: `tools/python_execution_policy.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: acknowledgement variable, Guarded Local controls, Docker/Compose files, fixed dependency behavior, and runtime provenance from Tasks 1–7.
- Produces: one normative policy and concise native/Docker operator instructions with no Bubblewrap or dedicated-runtime setup.

- [ ] **Step 1: Add a red documentation contract test**

Add these checks to `tests/test_guarded_local_startup.py`:

```python
def test_documentation_names_guarded_local_and_explains_the_boundary():
    readme = Path("README.md").read_text(encoding="utf-8")
    policy = Path("tools/python_execution_policy.md").read_text(encoding="utf-8")
    combined = readme + "\n" + policy
    assert "Guarded Local" in combined
    assert "REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED=1" in combined
    assert "docker compose up --build" in readme
    assert "host permissions" in combined
    assert "fixed" in combined.casefold() and "package" in combined.casefold()
    assert "REPORT_AGENT_PYTHON_ANALYSIS_RUNTIME" not in combined
    assert "Bubblewrap" not in combined
```

Run the test. Expected: FAIL because the normative policy file is absent and the README has only the old generic safety note.

- [ ] **Step 2: Write the normative policy**

Create `tools/python_execution_policy.md` with these sections and exact claims:

- **Execution model:** one `LocalPythonRuntime` worker path; Guarded Local has no OS security boundary.
- **Allowed input/output:** one preloaded DataFrame, installed analysis libraries, printed JSON-compatible text, and one optional PNG.
- **Blocked operations:** filesystem, network, process control, environment/secrets, dynamic execution, installers, interactive input, and renderer overrides, including stable rule IDs.
- **Recovery:** one changed correction after `POLICY_BLOCKED`; unchanged or second block ends Python for that turn only.
- **Runtime controls:** clean allowlisted environment, private run directories, CPU/address-space/process/file limits when supported, wall timeout, bounded pipes/result/figure, and process-group termination.
- **Trust statement:** these controls reduce accidents and runaway use but cannot safely contain deliberately adversarial Python or a compromised dependency.
- **Dependencies:** fixed application environment; package installation and temporary network grants are unavailable; `DEPENDENCY_NOT_AVAILABLE` is controlled.
- **Docker scope:** optional whole-app host boundary, same Guarded Local worker, application network remains enabled, and no per-analysis container boundary.

- [ ] **Step 3: Rewrite README startup and safety guidance**

Keep the current native install path, then explain the first-run acknowledgement prompt and show a non-interactive example:

```bash
export REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED=1
python run_fastapi.py
```

Add the optional container path:

```bash
export REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED=1
export OPENAI_API_KEY="replace-with-provider-key"
docker compose up --build
```

Document the loopback URL, named-volume persistence, optional study mount:

```bash
export REPORT_AGENT_STUDY_DATA="/absolute/path/to/study_data"
docker compose -f compose.yaml -f compose.study-data.yaml up --build
```

State that provider credentials enter at runtime, generated code is offline by policy, dependencies are fixed, Docker is optional, and Docker separates the whole app from the host rather than separating generated Python from the app. Remove any Bubblewrap, copied-worker, or `REPORT_AGENT_PYTHON_ANALYSIS_RUNTIME` instructions if present.

- [ ] **Step 4: Run documentation and deterministic suites**

Run:

```bash
../../.venv/bin/python -m pytest -q tests/test_guarded_local_startup.py tests/test_docker_deployment.py
```

Expected: PASS.

- [ ] **Step 5: Commit documentation**

```bash
git add -f tools/python_execution_policy.md tests/test_guarded_local_startup.py
git add README.md
git commit -m "docs: explain guarded local and Docker execution"
```

---

### Task 9: Final Verification and Scope Audit

**Files:**
- Verify only; modify a file only if a failing test exposes a defect, and use a new red-green cycle before that change.

**Interfaces:**
- Consumes: all prior task outputs.
- Produces: evidence that the approved design is complete without Bubblewrap code, silent downgrade logic, or changes outside this worktree.

- [ ] **Step 1: Run the full deterministic Python suite**

Run:

```bash
../../.venv/bin/python -m pytest -q
```

Expected: PASS; Docker smoke is reported as one explicit skip unless opted in.

- [ ] **Step 2: Run frontend type/tests after capability-schema change**

Run:

```bash
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

Expected: both commands PASS and the committed frontend build remains valid. If the build rewrites hashed assets, inspect and commit only the generated `frontend/dist` changes required by the source type change.

- [ ] **Step 3: Validate Compose interpolation and rendered security settings**

When Docker Compose is installed, run:

```bash
REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED=1 docker compose config
```

Expected: exit 0; rendered port is loopback-only, the runtime mount is a named volume, and hardening keys remain present. If Compose is not installed, record the deterministic `tests/test_docker_deployment.py` result and the explicit reason this optional CLI check was skipped.

- [ ] **Step 4: Run the real smoke or record its precise environmental skip**

Run:

```bash
RUN_DOCKER_SMOKE=1 ../../.venv/bin/python -m pytest -q -s tests/test_docker_smoke.py
```

Expected: PASS with a working daemon; otherwise SKIPPED only for an unavailable CLI/daemon. Do not describe a skipped smoke as a passing real deployment test.

- [ ] **Step 5: Audit forbidden architecture and terminology**

Run:

```bash
rg -n "Bubblewrap|bwrap|SandboxRunner|LinuxBubblewrapBackend|seccomp|REPORT_AGENT_PYTHON_ANALYSIS_RUNTIME" README.md tools epi_agent api run_fastapi.py Dockerfile compose.yaml compose.study-data.yaml
rg -n "sandbox" README.md tools/python_execution_policy.md api epi_agent run_fastapi.py
```

Expected: the first command returns no matches. Every match from the second command is an explicit negative statement such as “not an operating-system sandbox”; no API/status field calls Guarded Local a sandbox.

- [ ] **Step 6: Confirm worktree isolation and review the final diff**

Run:

```bash
git status --short --branch
git diff --check
git log --oneline 2496a41..HEAD
git diff --stat 2496a41..HEAD
```

Expected: no whitespace errors; only the planned branch files changed. Then inspect the other worktrees read-only and confirm the dirty Amarel/model-profile edits are unchanged.

- [ ] **Step 7: Commit any test-driven verification correction, then leave the tree clean**

If Step 1–6 required a correction, commit only after its new focused test has failed then passed:

```bash
git add <exact corrected paths>
git commit -m "fix: complete guarded local deployment verification"
```

Finally run `git status --short --branch`; expected: only `## design/python-guarded-local` with no modified or untracked files.
