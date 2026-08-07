# AWS Phase 2A Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing invitation-only Epi Agent application deployable at `https://epiagent.org` on one recoverable AWS EC2 instance, correct model-specific sampling parameters, then validate the live environment without Docker.

**Architecture:** Model runtime profiles first govern which request parameters each OpenAI model may receive, so GPT-5.6 requests omit unsupported sampling controls in local and AWS execution alike. Version-controlled CloudFormation provisions the AWS foundation in account `641379499556`, Region `us-east-1`; immutable release archives are uploaded to private S3 and installed through an SSM Command document. Nginx terminates HTTPS and proxies to one FastAPI process, while a retained encrypted EBS data volume holds SQLite, conversations, and user artifacts. Repository implementation and offline validation finish before a separately approved change set creates billable resources.

**Tech Stack:** Python 3.12, FastAPI, React/Vite, pytest, Amazon Linux 2023, CloudFormation, EC2, gp3 EBS, S3, Cognito, Route 53, Systems Manager, CloudWatch, Nginx, Certbot, systemd, AWS CLI 2.36+, cfn-lint 1.53.1.

## Global Constraints

- Work only on branch `aws-test`; verify it before every task commit.
- Preserve the untracked `report-india-synthetic-0.3.0.tar.gz` and `.sha256` files; never commit their binary contents.
- Every AWS CLI command must include `--profile xutao-dev --region us-east-1` unless the command is global and rejects a Region option.
- Every mutating AWS command must first prove `Account == 641379499556` with STS in the same operator invocation.
- No task before Task 10 may create, update, or delete an AWS resource.
- Task 10 may create only the reviewed IAM bootstrap stack after explicit user approval.
- Task 11 may execute the billable Phase 2A stack only after the user reviews the generated CloudFormation change set.
- The first release uses exactly one `t3.large`, T3 credit mode `standard`, one Uvicorn worker, a 30 GiB root gp3 volume, and a retained 50 GiB data gp3 volume.
- `epiagent.org` and hosted zone `Z02132461LVJ2PFOYFXFU` already exist and must never be imported, replaced, or deleted by CloudFormation.
- Public ingress is TCP 80 and 443 only; no SSH, load balancer, CloudFront, NAT Gateway, RDS, Redis, ECS, or EKS.
- Cognito public sign-up is disabled. Users bring their own OpenAI keys, and no server-owned provider key may be persisted.
- Model request parameters are capability-driven: every GPT-5.6 public or internal profile must omit `temperature` and `top_p`; neither a UI default nor a legacy saved value may cause those keys to reach `ChatOpenAI`.
- Local native startup remains supported and does not require Docker or AWS configuration.
- Generated Python remains a controlled-demo boundary, runs through one fixed privileged launcher on AWS, receives no AWS/OpenAI credentials, and has outbound traffic blocked.
- Durable EBS, S3, and Cognito resources use both deletion and update-replacement retention policies.
- All shell scripts use `set -euo pipefail`, quote expansions, reject unknown arguments, and avoid printing environment contents.
- Never log authorization headers, cookies, Cognito tokens, provider keys, request bodies containing keys, or AWS credential material.

## File and Responsibility Map

### Model capability correction

- Modify `utils/model_runtime_profiles.py`: declare sampling-control support per model and expose it through model descriptors.
- Modify `llm_vllm.py`: omit `temperature` and `top_p` for profiles that do not support sampling controls.
- Modify `api/runtime.py` and `api/schemas.py`: normalize unsupported sampling values to `None` and return the capability to clients.
- Modify `frontend/src/types.ts` and `frontend/src/RuntimeSettingsPanel.tsx`: clear and hide unsupported controls when a GPT-5.6 model is selected.
- Modify `tests/test_llm_vllm.py`, `tests/test_api_runtime.py`, `frontend/src/RuntimeSettingsPanel.test.tsx`, and related fixtures: prove GPT-5.6 omission while preserving GPT-5.4 behavior.

### Application deployment interfaces

- Modify `api/deployment.py`: environment-derived maintenance/release state.
- Modify `api/runtime.py`: process-local active-run count used by the drain check.
- Modify `api/schemas.py`: typed readiness and deployment-status responses.
- Modify `api/server.py`: liveness, readiness, maintenance middleware, and loopback operations status.
- Modify `tests/test_api_deployment.py`, `tests/test_api_runtime.py`, and `tests/test_api_server.py`: TDD coverage for those contracts.

### Native worker boundary

- Modify `epi_agent/runtimes/python/local_process.py`: optional fixed hosted launcher while retaining the existing local command.
- Create `deploy/aws/bin/epi-agent-python-worker`: root-owned path validator and privilege drop to `epi-agent-exec`.
- Modify `tests/test_epi_python_runtime.py`: command selection, environment, path, and local compatibility tests.

### Release and host assets

- Create `scripts/build_aws_release.py`: build one immutable tracked-source archive plus manifest/checksum.
- Create `tests/test_build_aws_release.py`: exclusions, commit identity, checksum, and determinism checks.
- Create `deploy/aws/bin/install-release.sh`: bounded drain, install, activate, health-check, and rollback entry point.
- Create `deploy/aws/bin/install-study.sh`: verify outer SHA-256 and invoke the existing study installer.
- Create `deploy/aws/nginx/epi-agent.conf`: HTTP ACME/redirect, HTTPS proxy, and external operations-route denial.
- Create `deploy/aws/systemd/epi-agent.service`: one-worker FastAPI service ordered after the data mount.
- Create `deploy/aws/systemd/epi-agent-certificate-renew.service` and `.timer`: renewal and Nginx reload.
- Create `deploy/aws/cloudwatch/amazon-cloudwatch-agent.json`: application/system logs and disk/memory metrics.
- Create `deploy/aws/env/app.env.example`: non-secret production configuration contract.
- Create `tests/test_aws_host_assets.py`: static security and syntax assertions for all host assets.

### Infrastructure and operator tooling

- Create `infra/aws/bootstrap/template.yaml`: one-time CloudFormation execution role and exact Developer-group PassRole grant.
- Create `infra/aws/phase2a/template.yaml`: network, storage, Cognito, EC2, DNS, SSM, snapshots, logs, and alarms.
- Create `infra/aws/phase2a/parameters.example.json`: non-secret example parameters.
- Create `infra/aws/phase2a/cfn-lint-requirements.txt`: `cfn-lint==1.53.1`.
- Create `scripts/aws_phase2a.py`: account-guarded validate, change-set, execute, outputs, start, stop, release-upload, and SSM-deploy commands.
- Create `tests/test_aws_phase2a_cli.py`: command construction and mutation-guard tests.
- Create `tests/test_aws_infrastructure.py`: required-resource and forbidden-resource assertions.

### Operations and acceptance

- Create `docs/aws/phase2a-runbook.md`: beginner-safe bootstrap, deployment, stop/start, rollback, invite, backup, and restore procedures.
- Create `scripts/smoke_aws_phase2a_real.py`: opt-in live Cognito/HTTPS/persistence acceptance smoke with secret redaction.
- Create `tests/test_smoke_aws_phase2a_real.py`: offline contract tests for the opt-in smoke.
- Modify `README.md` and `docs/working-demo.md`: link the runbook and replace the now-obsolete “AWS delivery deferred” wording.

---

### Task 0: Enforce model-specific sampling parameter support

**Files:**
- Modify: `utils/model_runtime_profiles.py`
- Modify: `llm_vllm.py`
- Modify: `api/runtime.py:791-832`
- Modify: `api/schemas.py:87-105`
- Modify: `frontend/src/types.ts:59-73`
- Modify: `frontend/src/RuntimeSettingsPanel.tsx`
- Modify: `tests/test_llm_vllm.py`
- Modify: `tests/test_api_runtime.py:750-780`
- Modify: `tests/test_api_server.py`
- Modify: `frontend/src/RuntimeSettingsPanel.test.tsx`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Produces: `ModelRuntimeProfile.supports_sampling_controls: bool`.
- Produces: `ModelRuntimeProfile.descriptor()["supports_sampling_controls"]` and matching API/frontend `ModelOption` fields.
- Produces: model-aware `ReportAgentApiRuntime._normalize_settings(...)` that returns `temperature=None` and `top_p=None` for every GPT-5.6 profile.
- Produces: defense-in-depth in `build_openai_llm(...)` so unsupported sampling keys are absent even if a stale caller supplies values.
- Consumed by: the runtime-options endpoint, model settings UI, local native runs, and the later AWS release.

- [ ] **Step 1: Write failing model-profile capability tests**

Add a focused test in `tests/test_api_runtime.py` with these exact assertions:

```python
def test_model_profiles_declare_sampling_control_support() -> None:
    assert model_runtime_profile("gpt-5.4").supports_sampling_controls is True
    for model_id in (
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "gpt5.6-Luna-Light",
    ):
        assert (
            model_runtime_profile(model_id).supports_sampling_controls
            is False
        )
```

Also assert that the public descriptor for `gpt-5.6-sol` contains
`"supports_sampling_controls": False`.

- [ ] **Step 2: Run the profile test and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_api_runtime.py -k sampling_control_support -q`

Expected: FAIL because `ModelRuntimeProfile` has no
`supports_sampling_controls` field.

- [ ] **Step 3: Add the explicit model capability**

Add this required dataclass field next to `reasoning_effort`:

```python
supports_sampling_controls: bool
```

Set it to `True` only for `gpt-5.4`, and set it to `False` for
`gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol`, and
`gpt5.6-Luna-Light`. Add the field to `descriptor()`:

```python
"supports_sampling_controls": self.supports_sampling_controls,
```

Do not infer support from a model-name prefix at request time; the registry is
the single source of truth.

- [ ] **Step 4: Write failing transport tests for omission and preservation**

Extend `tests/test_llm_vllm.py` with one reusable capture helper, then prove both
branches:

```python
def _capture_chat_openai(monkeypatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(llm_vllm, "ChatOpenAI", FakeChatOpenAI)
    return captured


def test_gpt56_omits_unsupported_sampling_kwargs(monkeypatch) -> None:
    captured = _capture_chat_openai(monkeypatch)
    llm_vllm.build_openai_llm(
        model_name="gpt-5.6-sol",
        api_key="session-key",
        temperature=0.2,
        top_p=0.8,
    )
    assert "temperature" not in captured
    assert "top_p" not in captured


def test_gpt54_preserves_supported_sampling_kwargs(monkeypatch) -> None:
    captured = _capture_chat_openai(monkeypatch)
    llm_vllm.build_openai_llm(
        model_name="gpt-5.4",
        api_key="session-key",
        temperature=0.2,
        top_p=0.8,
    )
    assert captured["temperature"] == 0.2
    assert captured["top_p"] == 0.8
```

Refactor the existing key-forwarding test to use `_capture_chat_openai` so the
test module has only one fake implementation.

- [ ] **Step 5: Run the transport tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_llm_vllm.py -q`

Expected: the GPT-5.6 test FAILS because explicit values are currently passed
to `ChatOpenAI`; the GPT-5.4 preservation test passes.

- [ ] **Step 6: Gate transport kwargs by the model profile**

Replace the unconditional sampling assignments in `build_openai_llm` with:

```python
supports_sampling = (
    profile is None or profile.supports_sampling_controls
)
if supports_sampling and temperature is not None:
    kwargs["temperature"] = temperature
elif supports_sampling and profile is not None and profile.model_id == "gpt-5.4":
    kwargs["temperature"] = 0.0
if supports_sampling and top_p is not None:
    kwargs["top_p"] = top_p
elif supports_sampling and profile is not None and profile.model_id == "gpt-5.4":
    kwargs["top_p"] = 1.0
```

The important contract is key absence for unsupported profiles. Do not send
`temperature=None`, `top_p=None`, `temperature=0`, or `top_p=1` as a substitute
for omission.

- [ ] **Step 7: Write failing runtime normalization and descriptor tests**

Replace the existing GPT-5.6 custom-settings expectation in
`tests/test_api_runtime.py` so stale values are accepted but normalized away:

```python
assert state.runtime_settings.model_name == "gpt-5.6-luna"
assert state.runtime_settings.temperature is None
assert state.runtime_settings.top_p is None
assert state.runtime_settings.max_steps == 6
assert state.runtime_settings.timeout_seconds == 120
```

Add an assertion on `runtime.runtime_options()`:

```python
options = runtime.runtime_options()
gpt56 = next(model for model in options.models if model.id == "gpt-5.6-luna")
assert gpt56.supports_sampling_controls is False
```

Keep a GPT-5.4 test proving valid non-null sampling settings still survive
normalization.

- [ ] **Step 8: Run runtime/API tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_api_runtime.py tests/test_api_server.py -k 'runtime_settings or runtime_options or custom_openai_model' -q`

Expected: FAIL because GPT-5.6 values remain non-null and `ModelOption` lacks
the capability field.

- [ ] **Step 9: Normalize unsupported values and extend the API schema**

Add this required field to `api.schemas.ModelOption`:

```python
supports_sampling_controls: bool
```

In `_normalize_settings`, resolve the selected profile after validating the
model name, then clear sampling fields before range validation:

```python
profile = model_runtime_profile(normalized.model_name)
if not profile.supports_sampling_controls:
    normalized.temperature = None
    normalized.top_p = None
```

Use the same `profile` object when assigning the model-specific workflow
timeout. This deliberately tolerates old saved/default values while ensuring
they cannot reach a GPT-5.6 request.

- [ ] **Step 10: Write failing frontend capability tests**

Add a required `supportsSamplingControls: boolean` parameter to the
`modelOption` fixture factory and return it as `supports_sampling_controls`.
Pass `false` for every GPT-5.6 option and `true` for GPT-5.4. Create a
`standardSettings` fixture with `model_name: "gpt-5.4"`, render with that
fixture, and assert:

```typescript
fireEvent.change(screen.getByLabelText("Model"), {
  target: { value: "gpt-5.6-luna" },
});
expect(onChange).toHaveBeenLastCalledWith({
  ...standardSettings,
  model_name: "gpt-5.6-luna",
  temperature: null,
  top_p: null,
});
expect(screen.queryByLabelText("Temperature")).not.toBeInTheDocument();
expect(screen.queryByLabelText("Top probability")).not.toBeInTheDocument();
expect(
  screen.getByText("Sampling controls are unavailable for this model."),
).toBeInTheDocument();
```

Also retain the existing GPT-5.4 edit test to prove both controls remain
visible and editable for a supporting profile. Add the required boolean to the
`ModelOption` fixture in `frontend/src/App.test.tsx` so the full TypeScript test
suite remains type-correct.

- [ ] **Step 11: Run the frontend test and confirm RED**

Run: `npm --prefix frontend test -- --run src/RuntimeSettingsPanel.test.tsx`

Expected: FAIL because the selected model does not yet control the sampling
fields or clear stale values.

- [ ] **Step 12: Implement model-aware frontend controls**

Add this field to `frontend/src/types.ts`:

```typescript
supports_sampling_controls: boolean;
```

In `RuntimeSettingsPanel`, derive the selected profile from `modelOptions`.
Use a dedicated model-change handler that clears both fields when the target
profile does not support them:

```typescript
function handleModelChange(modelName: string) {
  const selected = modelOptions.find((model) => model.id === modelName);
  updateSettings({
    model_name: modelName,
    ...(selected && !selected.supports_sampling_controls
      ? { temperature: null, top_p: null }
      : {}),
  });
}
```

Render the Temperature and Top probability inputs only when the selected
profile supports sampling controls. Otherwise render exactly:

```tsx
<p>Sampling controls are unavailable for this model.</p>
```

Leave max steps and workflow timeout visible for every model.

- [ ] **Step 13: Run the complete correction gate**

Run:

```bash
.venv/bin/python -m pytest tests/test_llm_vllm.py tests/test_api_runtime.py tests/test_api_server.py -q
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

Expected: all commands PASS. Inspect the fake `ChatOpenAI` kwargs once more and
confirm no GPT-5.6 request contains either sampling key.

- [ ] **Step 14: Commit the correction before infrastructure work**

```bash
git add utils/model_runtime_profiles.py llm_vllm.py api/runtime.py api/schemas.py \
  frontend/src/types.ts frontend/src/RuntimeSettingsPanel.tsx \
  tests/test_llm_vllm.py tests/test_api_runtime.py tests/test_api_server.py \
  frontend/src/RuntimeSettingsPanel.test.tsx frontend/src/App.test.tsx
git commit -m "fix: omit unsupported GPT-5.6 sampling controls"
```

Do not begin Task 1 until this commit passes the complete correction gate.

---

### Task 1: Add maintenance, readiness, release, and drain status

**Files:**
- Modify: `api/deployment.py`
- Modify: `api/runtime.py:650-790`
- Modify: `api/schemas.py`
- Modify: `api/server.py:137-224`
- Modify: `tests/test_api_deployment.py`
- Modify: `tests/test_api_runtime.py`
- Modify: `tests/test_api_server.py`

**Interfaces:**
- Produces: `DeploymentState.from_environ(environ)`, `DeploymentState.maintenance_enabled()`, and `DeploymentState.release_id`.
- Produces: `ReportAgentApiRuntime.active_run_count() -> int`.
- Produces: unauthenticated `GET /api/health`, `GET /api/readiness`, and `GET /api/ops/deployment-status`.
- Produces: HTTP 503 with code `DEPLOYMENT_MAINTENANCE` for unsafe requests while the sentinel exists.
- Consumed by: `install-release.sh`, Nginx, systemd, and CloudWatch health checks.

- [ ] **Step 1: Write failing deployment-state tests**

Add tests proving an absent sentinel is ready, a present sentinel is maintenance, release IDs default to `development`, and explicit paths/IDs are honored:

```python
def test_deployment_state_reads_release_and_maintenance(monkeypatch, tmp_path):
    sentinel = tmp_path / "maintenance"
    monkeypatch.setenv("REPORT_AGENT_MAINTENANCE_FILE", str(sentinel))
    monkeypatch.setenv("REPORT_AGENT_RELEASE_ID", "abc123")
    state = DeploymentState.from_environ(os.environ)
    assert state.release_id == "abc123"
    assert state.maintenance_enabled() is False
    sentinel.touch()
    assert state.maintenance_enabled() is True
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_api_deployment.py -q`

Expected: FAIL because `DeploymentState` does not exist.

- [ ] **Step 3: Implement the deployment state**

Add this public shape to `api/deployment.py`:

```python
@dataclass(frozen=True)
class DeploymentState:
    maintenance_file: Path | None
    release_id: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "DeploymentState":
        configured = str(environ.get("REPORT_AGENT_MAINTENANCE_FILE", "")).strip()
        release_id = str(environ.get("REPORT_AGENT_RELEASE_ID", "development")).strip()
        return cls(
            maintenance_file=Path(configured) if configured else None,
            release_id=release_id or "development",
        )

    def maintenance_enabled(self) -> bool:
        return self.maintenance_file is not None and self.maintenance_file.is_file()
```

Use `collections.abc.Mapping`; do not cache the sentinel result.

- [ ] **Step 4: Write failing active-run count tests**

Construct two `ThreadRuntime` fakes whose runner statuses are `running` and `idle`, then assert the count is one. Also assert exceptions from a stale runner are counted as non-running, matching `_thread_is_running`.

- [ ] **Step 5: Run the runtime test and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_api_runtime.py -k active_run_count -q`

Expected: FAIL because `active_run_count` does not exist.

- [ ] **Step 6: Implement the minimal runtime count**

```python
def active_run_count(self) -> int:
    with self._lock:
        threads = tuple(self._threads.items())
    return sum(
        1
        for (_owner, thread_id), thread in threads
        if self._thread_is_running(thread_id, thread)
    )
```

Do not expose thread IDs or owners.

- [ ] **Step 7: Write failing HTTP contract tests**

Cover these exact cases:

```python
assert client.get("/api/health").json() == {"status": "ok"}
assert client.get("/api/readiness").json() == {
    "status": "ready", "release_id": "release-1"
}
assert client.get("/api/ops/deployment-status").json() == {
    "status": "ready",
    "release_id": "release-1",
    "maintenance": False,
    "active_runs": 0,
}
```

After touching the sentinel, readiness must return 503, operations status must remain 200 with `maintenance: true`, GET history must remain readable, and POST `/api/threads` must return 503 with `Retry-After: 30`.

- [ ] **Step 8: Implement schemas, middleware, and endpoints**

Add `ReadinessStatus` and `DeploymentStatus` Pydantic models. Add a `deployment_state` optional argument to `create_app`, default it from `os.environ`, and add middleware before the credential-pruning middleware:

```python
if (
    deployment_state.maintenance_enabled()
    and request.method not in {"GET", "HEAD", "OPTIONS"}
):
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "30"},
        content={"detail": {"code": "DEPLOYMENT_MAINTENANCE"}},
    )
```

`/api/health` is liveness and always remains 200 while the process runs.
`/api/readiness` returns 503 during maintenance. `/api/ops/deployment-status`
returns only release ID, maintenance boolean, and active count; Nginx blocks it
from external clients in Task 4.

- [ ] **Step 9: Run focused and adjacent tests**

Run: `.venv/bin/python -m pytest tests/test_api_deployment.py tests/test_api_runtime.py tests/test_api_server.py tests/test_api_auth.py -q`

Expected: PASS.

- [ ] **Step 10: Commit Task 1**

```bash
git add api/deployment.py api/runtime.py api/schemas.py api/server.py \
  tests/test_api_deployment.py tests/test_api_runtime.py tests/test_api_server.py
git commit -m "feat: add hosted deployment readiness and drain state"
```

### Task 2: Route hosted Python through a fixed OS privilege boundary

**Files:**
- Modify: `epi_agent/runtimes/python/local_process.py:220-340`
- Modify: `api/app.py`
- Create: `deploy/aws/bin/epi-agent-python-worker`
- Modify: `tests/test_epi_python_runtime.py`
- Modify: `tests/test_no_study_startup.py`
- Create: `tests/test_aws_host_assets.py`

**Interfaces:**
- Produces: `LocalPythonRuntime(..., worker_launcher: Sequence[str] | None = None)`.
- Produces: environment setting `REPORT_AGENT_PYTHON_WORKER_LAUNCHER=/usr/local/libexec/epi-agent-python-worker`.
- Produces: root-owned launcher accepting only `--input-dir` and `--output-dir` below the configured execution root, then dropping to `epi-agent-exec`.
- Preserves: the current `sys.executable worker.py` command in local mode.

- [ ] **Step 1: Write failing launcher-selection tests**

Monkeypatch `subprocess.Popen` and assert:

```python
runtime = LocalPythonRuntime(
    runtime_root=tmp_path,
    worker_launcher=("/usr/local/libexec/epi-agent-python-worker",),
    memory_limit_bytes=None,
)
```

starts the fixed launcher with only `--input-dir` and `--output-dir`, while the
default runtime still starts `sys.executable` plus `worker.py`. Assert that
`OPENAI_API_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`, and `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` are absent.

- [ ] **Step 2: Run and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_epi_python_runtime.py -k 'launcher or environment' -q`

Expected: FAIL because `worker_launcher` is unsupported.

- [ ] **Step 3: Implement explicit command construction**

Store an immutable tuple in `LocalPythonRuntime.__init__`. Reject an empty
launcher and any non-absolute first element. Construct exactly one of:

```python
if self._worker_launcher is None:
    command = [sys.executable, str(Path(__file__).with_name("worker.py"))]
else:
    command = list(self._worker_launcher)
command.extend(["--input-dir", str(input_dir), "--output-dir", str(output_dir)])
```

Add `python_worker_launcher(environ) -> tuple[str, ...] | None` to
`api/deployment.py` using `shlex.split`, and pass it from `api/app.py` when
constructing the local-process runtime. Hosted startup fails if the configured
launcher is relative or contains NUL/newline characters.

- [ ] **Step 4: Write the launcher contract test before the launcher**

`tests/test_aws_host_assets.py` must assert the launcher contains all of:

- canonicalization with `realpath -e`;
- exact execution root `/srv/epi-agent/runtime/users`;
- rejection unless both paths contain `/threads/` and `/execution/`;
- rejection of symlinks and paths owned by another UID;
- explicit `env -i` allowlist;
- privilege drop with `runuser --user epi-agent-exec`;
- fixed worker `/opt/epi-agent/current/epi_agent/runtimes/python/worker.py`;
- no evaluation of caller-supplied shell text.

- [ ] **Step 5: Implement the root-owned fixed launcher**

The script accepts exactly four argument values. Its command tail is fixed:

```bash
exec /usr/sbin/runuser --user epi-agent-exec -- \
  /usr/bin/env -i \
  PATH=/opt/epi-agent/current/.venv/bin:/usr/bin \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUTF8=1 MPLBACKEND=Agg \
  /opt/epi-agent/current/.venv/bin/python \
  /opt/epi-agent/current/epi_agent/runtimes/python/worker.py \
  --input-dir "$input_dir" --output-dir "$output_dir"
```

Before `exec`, validate canonical paths and use narrow ACLs for
`epi-agent-exec`: read/execute on the run/input tree and read/write/execute on
the output/work tree. Task 4 installs this file as root mode `0755` and a
sudoers rule allowing `epi-agent-web` to invoke this exact path only.

- [ ] **Step 6: Run the runtime and static boundary tests**

Run: `.venv/bin/python -m pytest tests/test_epi_python_runtime.py tests/test_no_study_startup.py tests/test_aws_host_assets.py -q`

Expected: PASS on macOS without invoking sudo; the hosted launcher test is
contract/static and the local runtime tests execute normally.

- [ ] **Step 7: Commit Task 2**

```bash
git add api/app.py api/deployment.py epi_agent/runtimes/python/local_process.py \
  deploy/aws/bin/epi-agent-python-worker tests/test_epi_python_runtime.py \
  tests/test_no_study_startup.py
git add -f tests/test_aws_host_assets.py
git commit -m "feat: add hosted Python execution launcher boundary"
```

### Task 3: Build immutable application releases

**Files:**
- Create: `scripts/build_aws_release.py`
- Create: `tests/test_build_aws_release.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `ReleaseManifest(commit_sha, archive_sha256, created_at, python_version, frontend_manifest_sha256)`.
- Produces: `dist/aws/epi-agent-<40-char-sha>.tar.gz`, `.sha256`, and `.json`.
- Consumed by: `scripts/aws_phase2a.py upload-release` and `install-release.sh`.

- [ ] **Step 1: Write failing release-builder tests**

Create a temporary Git repository fixture and prove:

- tracked files are archived;
- `.env`, `.git`, `runtime`, `study_data`, untracked files, and raw study
  archives are absent;
- dirty tracked files cause exit 2;
- archive/member paths never escape the root;
- manifest commit is exactly 40 lowercase hex characters;
- the sidecar checksum equals the archive bytes; and
- two builds of the same commit produce identical bytes by fixing tar metadata,
  ordering, gzip mtime, UID, GID, uname, and gname.

- [ ] **Step 2: Run and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_build_aws_release.py -q`

Expected: FAIL because the builder does not exist.

- [ ] **Step 3: Implement the manifest and safe archive builder**

Use only the Python standard library and Git subprocesses. The public entry
point is:

```python
def build_release(project_root: Path, output_dir: Path) -> tuple[Path, Path, Path]:
    commit_sha = git_output(project_root, "rev-parse", "HEAD")
    require_clean_tracked_tree(project_root)
    tracked = git_output(project_root, "ls-files", "-z").split("\0")
    # Validate each PurePosixPath, add files in sorted order, then add release.json.
```

The embedded `release.json` contains the commit, Python requirement `3.12`,
frontend build-manifest SHA-256, and build format version `1`. Compute the
archive SHA after closing gzip, then write sidecars atomically.

- [ ] **Step 4: Ignore only generated release output**

Add `/dist/aws/` to `.gitignore`. Do not broaden existing ignore patterns and
do not ignore `deploy/aws` or `infra/aws`.

- [ ] **Step 5: Run focused tests and build a real local artifact**

Run: `.venv/bin/python -m pytest tests/test_build_aws_release.py -q`

Run: `.venv/bin/python scripts/build_aws_release.py --output-dir dist/aws`

Expected: PASS and three files named with commit `HEAD`; `tar -tzf` contains no
`.env`, `runtime/`, `study_data/`, or `report-india-*.tar.gz` entry.

- [ ] **Step 6: Commit Task 3**

```bash
git add .gitignore
git add -f scripts/build_aws_release.py tests/test_build_aws_release.py
git commit -m "feat: build immutable AWS application releases"
```

### Task 4: Add idempotent Amazon Linux host and release assets

**Files:**
- Create: `deploy/aws/bin/install-release.sh`
- Create: `deploy/aws/bin/install-study.sh`
- Create: `deploy/aws/nginx/epi-agent.conf`
- Create: `deploy/aws/systemd/epi-agent.service`
- Create: `deploy/aws/systemd/epi-agent-certificate-renew.service`
- Create: `deploy/aws/systemd/epi-agent-certificate-renew.timer`
- Create: `deploy/aws/cloudwatch/amazon-cloudwatch-agent.json`
- Create: `deploy/aws/env/app.env.example`
- Modify: `tests/test_aws_host_assets.py`

**Interfaces:**
- `install-release.sh <bucket> <release-key> <sha256> <release-id> <domain> <certificate-email> [--force]`.
- `install-study.sh <bucket> <study-key> <sha256> <study-id> <version>`.
- systemd service binds FastAPI only to `127.0.0.1:8000` with one worker.
- Nginx exposes `/api/health` and `/api/readiness`, but denies external
  `/api/ops/deployment-status`.

- [ ] **Step 1: Extend static tests before writing assets**

Test exact invariants rather than snapshots: service `User=epi-agent-web`,
`EnvironmentFile=/etc/epi-agent/app.env`, `ExecStart` contains
`--host 127.0.0.1 --port 8000`, `UMask=0027`, `NoNewPrivileges=true`, and
read/write paths exclude `/opt/epi-agent/releases`. Nginx must set request-size
limits, proxy timeouts, security headers, WebSocket-safe HTTP/1.1 headers, ACME
webroot, HTTP redirect, and `location = /api/ops/deployment-status { deny all; }`.

- [ ] **Step 2: Write the systemd, Nginx, CloudWatch, and environment assets**

The environment example contains exactly these hosted values with safe example
identifiers and no provider key:

```dotenv
REPORT_AGENT_AUTH_MODE=cognito
REPORT_AGENT_AWS_REGION=us-east-1
REPORT_AGENT_COGNITO_USER_POOL_ID=us-east-1_example
REPORT_AGENT_COGNITO_APP_CLIENT_ID=example
REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT=https://example.auth.us-east-1.amazoncognito.com/logout
REPORT_AGENT_AUTH_REDIRECT_URI=https://epiagent.org/auth/callback
REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI=https://epiagent.org/
REPORT_AGENT_CORS_ALLOW_ORIGIN_REGEX=^https://epiagent\.org$
REPORT_AGENT_RUNTIME_ROOT=/srv/epi-agent/runtime
REPORT_AGENT_CHECKPOINT_DB_PATH=/srv/epi-agent/runtime/agent_memory_fastapi.db
REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data
REPORT_AGENT_STATIC_DIR=/opt/epi-agent/current/frontend/dist
REPORT_AGENT_WEB_CONCURRENCY=1
REPORT_AGENT_MAINTENANCE_FILE=/run/epi-agent/maintenance
REPORT_AGENT_PYTHON_WORKER_LAUNCHER=/usr/bin/sudo -n /usr/local/libexec/epi-agent-python-worker
```

The actual deployment fills Cognito IDs/endpoints from CloudFormation outputs;
no secret is stored in this file.

- [ ] **Step 3: Write failing release-script contract tests**

Static tests must prove the script:

- verifies caller is root;
- checks release ID and SHA against `^[0-9a-f]{40}$` and
  `^[0-9a-f]{64}$`;
- creates maintenance with `install -m 0640`;
- polls local `/api/ops/deployment-status` with a 600-second default deadline;
- removes maintenance and exits without switching when draining times out;
- downloads from S3 to a staging directory;
- verifies SHA-256 before extraction;
- rejects tar absolute paths, `..`, symlinks, and hard links;
- creates a new `.venv` with Python 3.12 and installs `requirements.txt`;
- atomically replaces `/opt/epi-agent/current` only after prechecks;
- restarts systemd and checks liveness/readiness;
- restores the prior symlink and service when activation fails; and
- never removes `/srv/epi-agent` or a prior release.

- [ ] **Step 4: Implement `install-release.sh` minimally**

Use `mktemp -d /opt/epi-agent/staging.XXXXXX`, a cleanup trap, `aws s3 cp`,
`sha256sum -c`, a Python tar member validator, `uv venv --python 3.12`, and
`uv pip install --python <venv-python> --requirement requirements.txt`.
Activation uses a temporary symlink plus `mv -T`:

```bash
ln -s "$release_dir" /opt/epi-agent/current.next
mv -Tf /opt/epi-agent/current.next /opt/epi-agent/current
systemctl restart epi-agent.service
```

Never interpolate an SSM parameter into shell source; values arrive as
positional arguments after strict regex validation.

- [ ] **Step 5: Implement `install-study.sh` around the existing installer**

Download to a staging file, verify the exact outer checksum, then invoke:

```bash
/opt/epi-agent/current/.venv/bin/python /opt/epi-agent/current/study_installer.py \
  --study "$archive_path"
```

Set `REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data`, validate the installed
manifest ID/version, and leave prior referenced versions installed.

- [ ] **Step 6: Run syntax and static tests**

Run: `bash -n deploy/aws/bin/install-release.sh deploy/aws/bin/install-study.sh deploy/aws/bin/epi-agent-python-worker`

Run: `.venv/bin/python -m pytest tests/test_aws_host_assets.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add deploy/aws tests/test_aws_host_assets.py
git commit -m "feat: add Amazon Linux deployment assets"
```

### Task 5: Define the one-time IAM bootstrap stack

**Files:**
- Create: `infra/aws/bootstrap/template.yaml`
- Create: `tests/test_aws_infrastructure.py`

**Interfaces:**
- Produces stack `epi-agent-bootstrap`.
- Produces role `EpiAgentCloudFormationExecutionRole` trusted only by
  `cloudformation.amazonaws.com`.
- Produces a Developer-group inline policy allowing `iam:PassRole` only for the
  execution-role ARN.
- Produces output `CloudFormationExecutionRoleArn`.

- [ ] **Step 1: Write failing bootstrap-template assertions**

Assert the template contains no access key, IAM user, or wildcard PassRole;
trust principal is exactly CloudFormation; role name is stable; Developer group
is a parameter defaulting to `Developer`; and the inline group policy resource
references only `!GetAtt CloudFormationExecutionRole.Arn` for PassRole.

- [ ] **Step 2: Run and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_aws_infrastructure.py -k bootstrap -q`

Expected: FAIL because the template does not exist.

- [ ] **Step 3: Implement the bootstrap template**

The execution-role policy permits CloudFormation to manage the approved
EC2/VPC/EBS, S3, Cognito, Route 53 records, CloudWatch/Logs/SNS, SSM, DLM, and
IAM role resources. IAM resource ARNs are limited to names beginning
`epi-agent-`; `iam:PassRole` limits `iam:PassedToService` to EC2 and DLM. It may
not manage IAM users, access keys, organizations, account settings, domains, or
hosted zones.

Add explicit output and tag every resource `Project=epi-agent` and
`Environment=phase2a` where supported.

- [ ] **Step 4: Validate with pinned cfn-lint**

Run: `uvx --from cfn-lint==1.53.1 cfn-lint infra/aws/bootstrap/template.yaml`

Expected: exit 0 with no errors.

- [ ] **Step 5: Commit Task 5**

```bash
git add infra/aws/bootstrap/template.yaml
git add -f tests/test_aws_infrastructure.py
git commit -m "feat: define AWS IAM bootstrap stack"
```

### Task 6: Define network, retained storage, identity, and DNS

**Files:**
- Create: `infra/aws/phase2a/template.yaml`
- Create: `infra/aws/phase2a/parameters.example.json`
- Create: `infra/aws/phase2a/cfn-lint-requirements.txt`
- Modify: `tests/test_aws_infrastructure.py`

**Interfaces:**
- Produces stack `epi-agent-phase2a` with parameters `DomainName`,
  `HostedZoneId`, `InstanceType`, `RootVolumeGiB`, `DataVolumeGiB`,
  `CertificateEmail`, and optional `DataSnapshotId`.
- Produces outputs for bucket, Cognito IDs/domain, EIP, instance ID, SSM
  document, and application URL.

- [ ] **Step 1: Write failing network/storage/identity assertions**

Tests must require exactly one VPC, one public subnet, one internet gateway,
one route table, one S3 gateway endpoint, one security group, one retained data
volume, one versioned private S3 bucket, one invitation-only Cognito user pool,
one public app client without a secret, one Cognito domain, and one Route 53 A
record. Assert no resource type contains CloudFront, ELB, RDS, ElastiCache, NAT,
ECS, or EKS.

- [ ] **Step 2: Run and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_aws_infrastructure.py -k phase2a -q`

Expected: FAIL because the template does not exist.

- [ ] **Step 3: Implement parameters, network, and security group**

Use VPC `10.42.0.0/24`, subnet `10.42.0.0/26`, DNS support/hostnames enabled,
and one `0.0.0.0/0` route through the IGW. Ingress contains only TCP 80 and 443
from `0.0.0.0/0`; egress is documented and limited to required TCP 443 plus DNS
TCP/UDP 53. Do not add port 22.

- [ ] **Step 4: Implement retained storage and S3**

The 50 GiB gp3 data volume is encrypted, tagged for DLM, placed in the subnet's
AZ, and optionally restored from `DataSnapshotId`. Apply both retention
policies. The generated-name S3 bucket has AES-256 default encryption,
versioning, ownership enforcement, all four public-access blocks, TLS-only
bucket policy, and both retention policies.

- [ ] **Step 5: Implement Cognito and Route 53**

The user pool uses email username/verification, `AdminCreateUserConfig:
AllowAdminCreateUserOnly: true`, Essentials tier, deletion/update retention,
and a strong password policy. The client has `GenerateSecret: false`, code+PKCE
flow, scopes `openid` and `email`, callback
`https://epiagent.org/auth/callback`, and logout `https://epiagent.org/`.
Create a standard apex A record to the EIP; never create a hosted zone or domain
registration resource.

- [ ] **Step 6: Add exact non-secret example parameters**

The example file contains:

```json
[
  {"ParameterKey":"DomainName","ParameterValue":"epiagent.org"},
  {"ParameterKey":"HostedZoneId","ParameterValue":"Z02132461LVJ2PFOYFXFU"},
  {"ParameterKey":"InstanceType","ParameterValue":"t3.large"},
  {"ParameterKey":"RootVolumeGiB","ParameterValue":"30"},
  {"ParameterKey":"DataVolumeGiB","ParameterValue":"50"}
]
```

`CertificateEmail` is deliberately excluded because it is operator-specific and
must be passed at change-set time, not committed.

Create `infra/aws/phase2a/cfn-lint-requirements.txt` with exactly:

```text
cfn-lint==1.53.1
```

- [ ] **Step 7: Validate and commit Task 6**

Run: `uvx --from cfn-lint==1.53.1 cfn-lint infra/aws/phase2a/template.yaml`

Run: `.venv/bin/python -m pytest tests/test_aws_infrastructure.py -q`

Expected: PASS.

```bash
git add infra/aws/phase2a tests/test_aws_infrastructure.py
git commit -m "feat: define AWS network storage and identity"
```

### Task 7: Add EC2 bootstrap, SSM deployment, snapshots, and observability

**Files:**
- Modify: `infra/aws/phase2a/template.yaml`
- Modify: `tests/test_aws_infrastructure.py`

**Interfaces:**
- Produces EC2 instance, instance role/profile, EIP association, DLM lifecycle,
  CloudWatch agent parameter/log group/alarms, SNS topic, and SSM Command
  document `epi-agent-deploy-release` schema 2.2.
- Consumes: release/study S3 prefixes and host assets from Tasks 3–4.

- [ ] **Step 1: Write failing compute and operations assertions**

Require `t3.large`, `CpuCredits: standard`, AL2023 public SSM AMI parameter,
30 GiB encrypted gp3 root, IMDSv2 required/hop-limit 1, termination protection,
no SSH key, `DeleteOnTermination: true` only for root, separate retained volume
attachment, Elastic IP association, one-worker environment, 30-day log
retention, daily DLM schedule with 14 snapshots, and SSM schema 2.2.

- [ ] **Step 2: Implement least-privilege instance IAM**

Attach `AmazonSSMManagedInstanceCore` and an inline policy that can read only
`${ArtifactBucket.Arn}/releases/*` and `/studies/*`, read the CloudWatch agent
parameter, publish only to the stack log group/metrics, and describe its own EBS
attachment where unavoidable. It may not write S3, Route 53, Cognito, or IAM.

- [ ] **Step 3: Implement idempotent UserData**

UserData installs `awscli-2`, `amazon-cloudwatch-agent`, `nginx`, `certbot`,
`python3-certbot-nginx`, `acl`, `jq`, `sudo`, and `uv`; creates
`epi-agent-web`/`epi-agent-exec`; waits for the data-volume by EBS by-id path;
formats only when `blkid` reports no filesystem; mounts by UUID at
`/srv/epi-agent`; creates restrictive runtime/study/release directories; and
leaves the application stopped until the first SSM deployment.

Every destructive filesystem command is guarded by the exact expected EBS
volume ID from CloudFormation. UserData contains no secret and signals success
or failure with `cfn-signal`.

- [ ] **Step 4: Implement SSM Command document**

Use schema `2.2`, `aws:runShellScript`, `interpolationType: ENV_VAR`, and strict
allowed patterns for bucket, key, release SHA, release ID, domain, and email.
The document downloads/extracts the release to staging only to access the
root-owned installer, installs host assets, then invokes `install-release.sh`.
No parameter is concatenated as executable shell source.

- [ ] **Step 5: Implement DLM, logs, metrics, and alarms**

Add daily snapshots retaining 14, log group retention 30, CloudWatch Agent
config for memory/disk/inodes and Nginx/application logs, alarms for EC2 status,
CPU, CPU credits, memory, data disk, and service-error metric filter. Create an
SNS topic; create an email subscription only when optional `AlertEmail` is
non-empty.

- [ ] **Step 6: Validate and commit Task 7**

Run: `uvx --from cfn-lint==1.53.1 cfn-lint infra/aws/phase2a/template.yaml`

Run: `.venv/bin/python -m pytest tests/test_aws_infrastructure.py tests/test_aws_host_assets.py -q`

Expected: PASS.

```bash
git add infra/aws/phase2a/template.yaml tests/test_aws_infrastructure.py
git commit -m "feat: add AWS compute deployment and recovery resources"
```

### Task 8: Add an account-guarded Phase 2A operator CLI

**Files:**
- Create: `scripts/aws_phase2a.py`
- Create: `tests/test_aws_phase2a_cli.py`

**Interfaces:**
- Produces subcommands: `identity`, `validate`, `plan-bootstrap`,
  `execute-bootstrap`, `plan-stack`, `execute-change-set`, `outputs`,
  `upload-release`, `upload-study`, `deploy-release`, `stop`, and `start`.
- Every mutating subcommand calls `require_expected_identity()` first.
- Change-set planning never executes the change set.

- [ ] **Step 1: Write failing identity and command-construction tests**

Inject a `Runner` protocol:

```python
class Runner(Protocol):
    def run(self, argv: Sequence[str], *, capture_output: bool = True) -> CompletedProcess[str]: ...
```

Prove wrong account, wrong ARN profile, expired login, missing explicit profile,
and Region mismatch stop before any second AWS call. Prove every generated AWS
command includes `--profile xutao-dev --region us-east-1` and every stack
operation includes `--role-arn` after bootstrap.

- [ ] **Step 2: Run and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_aws_phase2a_cli.py -q`

Expected: FAIL because the CLI does not exist.

- [ ] **Step 3: Implement the immutable target and identity guard**

```python
EXPECTED_ACCOUNT = "641379499556"
EXPECTED_PROFILE = "xutao-dev"
EXPECTED_REGION = "us-east-1"
BOOTSTRAP_STACK = "epi-agent-bootstrap"
APPLICATION_STACK = "epi-agent-phase2a"

def require_expected_identity(runner: Runner) -> dict[str, str]:
    identity = run_json(runner, aws("sts", "get-caller-identity"))
    if identity.get("Account") != EXPECTED_ACCOUNT:
        raise OperatorError("refusing AWS mutation: unexpected account")
    return identity
```

Do not accept command-line overrides for account/profile/Region.

- [ ] **Step 4: Implement validate and two-step change sets**

`validate` runs pinned cfn-lint locally and AWS `validate-template` read-only.
`plan-*` creates a named change set, waits, and prints its JSON description but
does not execute. `execute-change-set` requires the exact returned change-set
ARN plus `--confirm-account 641379499556`; it describes the set again and
refuses if status or stack name differs.

- [ ] **Step 5: Implement release, study, deployment, stop/start commands**

Uploads use exact SHA metadata and `--expected-size`; existing keys with a
different checksum are rejected rather than overwritten. `deploy-release`
invokes the stack output SSM document and waits for command success. `stop` and
`start` resolve only the stack output instance ID, print cost/availability
effects, and require `--confirm-instance <exact-id>`.

- [ ] **Step 6: Run tests and safe read-only CLI commands**

Run: `.venv/bin/python -m pytest tests/test_aws_phase2a_cli.py -q`

Run: `.venv/bin/python scripts/aws_phase2a.py identity`

Run: `.venv/bin/python scripts/aws_phase2a.py validate`

Expected: tests PASS; identity prints account `641379499556`; validate makes no
resource changes.

- [ ] **Step 7: Commit Task 8**

```bash
git add -f scripts/aws_phase2a.py tests/test_aws_phase2a_cli.py
git commit -m "feat: add guarded AWS phase 2a operator CLI"
```

### Task 9: Add the runbook, opt-in real smoke, and full local gate

**Files:**
- Create: `docs/aws/phase2a-runbook.md`
- Create: `scripts/smoke_aws_phase2a_real.py`
- Create: `tests/test_smoke_aws_phase2a_real.py`
- Modify: `README.md`
- Modify: `docs/working-demo.md`

**Interfaces:**
- The real smoke requires `--base-url https://epiagent.org`, two test-user
  credential environment variable names, and explicit `--allow-live-aws`.
- The smoke never accepts provider keys as command-line arguments and redacts
  all secret-bearing HTTP data.

- [ ] **Step 1: Write failing offline smoke-contract tests**

Prove the smoke refuses without `--allow-live-aws`, refuses non-HTTPS or a host
other than `epiagent.org`, requires two different Cognito users, obtains secrets
only from named environment variables, and never includes their values in
exceptions or captured output.

- [ ] **Step 2: Implement the opt-in smoke skeleton**

Implement phases `tls`, `http_redirect`, `cognito_login`, `provider_key`,
`owner_isolation`, `restart_persistence`, `stop_start`, and `snapshot_restore`.
Potentially disruptive phases are individually opt-in flags and print the exact
resource target before action. The default real run performs only HTTPS,
authentication, BYOK, and owner-isolation checks.

- [ ] **Step 3: Write the beginner-safe runbook**

Include exact Console navigation for the one-time admin bootstrap, exact CLI
commands for identity/validate/change set, how to read every CloudFormation
change action, certificate-email handling, SNS confirmation, first invitation,
release/study upload, SSM deployment, CloudWatch checks, stop versus terminate,
rollback, snapshot restore to a separate mount, domain changes, and complete
cost-bearing resource inventory. Put red warning boxes before stack execution,
EC2 termination, retained-resource cleanup, and snapshot deletion.

- [ ] **Step 4: Update existing docs**

Keep local startup instructions unchanged. Replace only the statement that AWS
delivery is entirely deferred with a link to the Phase 2A runbook and a clear
status: repository support does not mean a live stack exists until Task 11.

- [ ] **Step 5: Run the complete repository-side gate**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_api_deployment.py tests/test_api_runtime.py tests/test_api_server.py \
  tests/test_api_auth.py tests/test_epi_python_runtime.py \
  tests/test_build_aws_release.py tests/test_aws_host_assets.py \
  tests/test_aws_infrastructure.py tests/test_aws_phase2a_cli.py \
  tests/test_smoke_aws_phase2a_real.py -q
```

Run: `uvx --from cfn-lint==1.53.1 cfn-lint infra/aws/bootstrap/template.yaml infra/aws/phase2a/template.yaml`

Run: `bash -n deploy/aws/bin/*.sh deploy/aws/bin/epi-agent-python-worker`

Run: `cd frontend && npm test -- --run && npm run build`

Run: `.venv/bin/python scripts/build_aws_release.py --output-dir dist/aws`

Expected: every test/lint/build passes; the live smoke is not executed by
pytest; no AWS resource mutation has occurred.

- [ ] **Step 6: Request two-stage code review**

Use `requesting-code-review`: first review spec compliance and AWS security,
then review code quality/test evidence. Fix all Critical and Important findings
before live change-set work.

- [ ] **Step 7: Commit Task 9**

```bash
git add README.md docs/working-demo.md
git add -f docs/aws/phase2a-runbook.md scripts/smoke_aws_phase2a_real.py \
  tests/test_smoke_aws_phase2a_real.py
git commit -m "docs: add AWS phase 2a operations and acceptance guide"
```

### Task 10: Bootstrap IAM after explicit administrator approval

**Files:**
- No repository changes expected.
- Evidence: save sanitized command/change-set outputs under a temporary
  operator directory outside Git; never save credentials.

**Interfaces:**
- Consumes: reviewed `infra/aws/bootstrap/template.yaml`.
- Produces: `CloudFormationExecutionRoleArn` and exact Developer-group PassRole.

- [ ] **Step 1: Re-run all non-mutating preflight checks**

Run:

```bash
.venv/bin/python scripts/aws_phase2a.py identity
.venv/bin/python scripts/aws_phase2a.py validate
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::641379499556:user/xutao-dev \
  --action-names cloudformation:CreateChangeSet iam:CreateRole iam:PassRole \
  --profile xutao-dev --region us-east-1
```

Expected: identity is correct; current developer cannot create IAM roles; no
resource changes.

- [ ] **Step 2: Generate and review the bootstrap change set**

An MFA-protected administrator uses the runbook to create the change set for
`epi-agent-bootstrap` without executing it. Verify it contains only the
execution role, its policy, and one Developer-group PassRole policy.

- [ ] **Step 3: Stop for explicit user approval**

Show the full sanitized resource/action list. Do not execute merely because the
plan says to continue.

- [ ] **Step 4: Execute bootstrap and verify least privilege**

After approval, execute the exact change-set ARN, wait for
`CREATE_COMPLETE`, retrieve the execution-role ARN, and rerun IAM simulation.
Expected: `xutao-dev` can pass only that role and still cannot create arbitrary
IAM roles directly.

- [ ] **Step 5: Record the non-secret role ARN in the local operator config**

Store it in a gitignored `infra/aws/phase2a/operator.local.json`; never commit
credentials or session tokens. No billable compute exists after this task.

### Task 11: Create and execute the billable Phase 2A change set

**Files:**
- No repository changes unless a validated template defect is found; any defect
  returns to TDD and review before another change set.

**Interfaces:**
- Consumes: execution role, main template, certificate email supplied privately
  by the user.
- Produces: the `epi-agent-phase2a` stack and its outputs.

- [ ] **Step 1: Create a main-stack change set without executing**

Run the operator CLI `plan-stack` with the private certificate email. Confirm
account, Region, hosted-zone ID, `t3.large`, 30/50 GiB volumes, and estimated
USD 75–85/month running cost.

- [ ] **Step 2: Review every change and forbidden-resource absence**

Export the change-set description and verify no CloudFront, ELB, NAT Gateway,
RDS, Redis, ECS, EKS, hosted zone, domain registration, SSH ingress, access key,
or server-owned OpenAI secret appears.

- [ ] **Step 3: Stop for explicit billable-resource approval**

Show the user the exact resource counts, recurring-cost estimate, retained
resources, and teardown behavior. Do not execute without a new approval.

- [ ] **Step 4: Execute the exact change-set ARN and monitor**

After approval, execute through `scripts/aws_phase2a.py
execute-change-set`. Poll stack events. On failure, preserve events, identify
the first failed resource, and do not improvise deletions of retained data.

- [ ] **Step 5: Verify infrastructure outputs and security**

Use read-only CLI calls to prove: SSM online, EBS encrypted/attached/retained,
S3 private/encrypted/versioned, Cognito admin-only, SG ports 80/443 only,
IMDSv2 required, DLM enabled, logs/alarms created, EIP assigned, and Route 53 A
record matches the EIP.

### Task 12: Publish, deploy, and complete live launch acceptance

**Files:**
- Repository changes only for defects found through a red test and reviewed
  fix; otherwise this task produces operational evidence, not source changes.

**Interfaces:**
- Consumes: local `report-india-synthetic-0.3.0.tar.gz` plus its SHA sidecar,
  immutable application release, stack outputs, and two invited test users.
- Produces: healthy `https://epiagent.org`, validated persistence/recovery, and
  Phase 2A acceptance record.

- [ ] **Step 1: Upload immutable application and study artifacts**

Use the operator CLI to upload the exact HEAD release and study version `0.3.0`.
Read back S3 metadata/checksums. Reject overwrite if an existing immutable key
differs.

- [ ] **Step 2: Deploy through SSM and obtain HTTPS**

Invoke the stack SSM document, wait for success, verify the data mount before
FastAPI, install/validate the study, obtain the `epiagent.org` certificate, and
confirm HTTP redirects to HTTPS. Run `certbot renew --dry-run` through SSM.

- [ ] **Step 3: Create exactly two Cognito test invitations**

Use admin-create-user, never public signup. Complete initial password flows
privately. Confirm the browser callback and logout URLs are exactly under
`https://epiagent.org`.

- [ ] **Step 4: Run nondisruptive real acceptance**

Run the opt-in smoke for TLS, login, BYOK, and two-user ownership isolation.
Scan SQLite, runtime files, systemd/Nginx/application logs, and captured
responses for test provider-key markers; expect zero occurrences.

- [ ] **Step 5: Run persistence and rollback acceptance with approval**

Create a conversation, artifact, and pending human review. Restart the service,
then reboot EC2; verify all durable state remains and provider keys do not.
Deploy the immediately previous release and roll forward again, proving EBS
data is unchanged.

- [ ] **Step 6: Run stop/start and snapshot-restore acceptance with approval**

Stop EC2, confirm the site is unavailable and compute state is stopped; start
it and confirm the same EIP/domain/data. Create or select a DLM snapshot, restore
to a separate temporary volume/test mount, verify SQLite and artifacts, then
detach it. Deleting the temporary restore volume requires separate explicit
approval.

- [ ] **Step 7: Produce final launch report**

Record exact stack/release/study IDs, test results, alarms, snapshot ID,
remaining costs, limitations, and whether EC2 is left running or stopped. Do
not invite additional users until every mandatory acceptance item passes.

---

## Plan Completion Gate

Phase 2A is complete only when:

- repository tasks 0–9 are committed on `aws-test` with fresh passing evidence;
- two-stage code review has no unresolved Critical or Important finding;
- the user separately approved both IAM bootstrap and billable stack execution;
- the live stack targets account `641379499556`, Region `us-east-1`;
- all infrastructure and application acceptance checks pass;
- provider-key scans find no persistence or logging;
- a real snapshot restore and release rollback are demonstrated; and
- the final report states whether EC2 is running and the continuing monthly
  cost.

Failure of a live task does not authorize deletion, replacement, security
relaxation, or broader IAM. Preserve evidence, diagnose the first failure, add a
failing test where possible, fix through the normal review path, and create a
new change set.
