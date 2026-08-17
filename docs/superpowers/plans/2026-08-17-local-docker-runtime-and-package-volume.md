# Local Docker Runtime and Package Volume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a fail-closed local Docker runtime for model-generated Python on macOS and Linux, with user-approved binary-wheel packages accumulated in one persistent Docker volume rather than new custom images.

**Architecture:** Integrate the repository's completed hardened Docker executor foundation from commit `9c30ff5e1545f81a3c09c5124c5f0f49be3d96e0`, then make its broker the only production Python execution backend. The immutable multi-architecture worker image supplies built-in packages; a broker-owned, compatibility-versioned Docker volume supplies approved extra packages read-only to disposable offline analysis containers and read-write only to a constrained installer container.

**Tech Stack:** Python 3.12, FastAPI, LangGraph interrupts/checkpoints, Pydantic 2, SQLite, HTTPX, Docker Engine/Desktop, OCI multi-architecture images, PyPI metadata, binary wheels, React 19, TypeScript, Vitest, pytest, Playwright.

## Global Constraints

- Supported hosts are macOS and Linux only; native Windows and WSL2 are unsupported initially.
- Python 3.12 and a running Docker daemon are prerequisites for complete functionality.
- Local FastAPI binds only to `127.0.0.1`; Compose publishes only `127.0.0.1:8000:8000`.
- Generated Python never runs natively in production; there is no silent `LocalPythonRuntime` fallback.
- The official `linux/amd64` and `linux/arm64` worker image is selected by immutable digest.
- Every analysis uses a fresh container with no network, read-only root/input, UID/GID `65532:65532`, no capabilities, `no-new-privileges`, and fixed CPU, memory, PID, time, scratch, and output limits.
- Containers receive no OpenAI key, Docker socket, conversation storage, study root, user home, application source, or arbitrary host path.
- Existing AST policy remains an early compatibility filter; Docker is the security boundary.
- Built-in-only users create no custom-package volume.
- First approved installation creates one `epi-agent-packages-v1` volume; later approved packages update that same volume.
- Built-ins stay in the image. Only additional/overriding packages live in the volume.
- Installer containers mount the volume read-write; analysis containers mount it read-only.
- Packages must be exact-version binary wheels from official PyPI. Reject sdists, Git/VCS, paths, alternate indexes, extras, editable installs, environment markers, OS packages, and arbitrary installer arguments.
- Metadata resolution may precede approval; payload download, third-party execution, and volume mutation require approval bound to the complete plan.
- Generated/model text never controls Docker arguments, mounts, image identity, package index, pip arguments, or shell commands.
- Installation/recovery is serialized; analysis never observes a partially updated environment.
- Every result records official image digest and package-state hash.
- Cleanup targets only exact labeled resources; never run broad automatic prune.
- Preserve unrelated user changes, including `.superpowers/sdd/task-5-report.md`.

---

## File Map

- Integrate `epi_agent/executor/`, broker runtime modules, worker-image files, and local executor scripts from `9c30ff5`.
- Modify `run_fastapi.py`, `api/deployment.py`, and `api/app.py` for loopback-only broker execution.
- Modify `api/schemas.py`, `api/runtime.py`, `api/server.py`, and `utils/review_interrupts.py` for readiness and package interrupts.
- Modify `compose.yaml` for loopback-only publishing.
- Modify `deploy/docker/analysis-worker/` for the non-root package mount point.
- Create `epi_agent/environments/{models,pypi,store,manager}.py` and `epi_agent/runtimes/python/package_installer.py` for package policy and fixed installation.
- Modify executor protocol/service/runner files for package operations and read-only analysis mounts.
- Modify Python runtime/tool files for requirements, provenance, approval, and retry.
- Create `frontend/src/PackageApproval.tsx` and `PackageInstallProgress.tsx`; modify shared frontend types/client/app/settings.
- Create `scripts/smoke_local_docker_package_volume_real.py`; update README/config and existing Docker smoke.

---

### Task 1: Integrate the completed hardened Docker executor foundation

**Files:**
- Merge source: `9c30ff5e1545f81a3c09c5124c5f0f49be3d96e0`
- Preserve: `docs/superpowers/specs/2026-08-17-local-docker-python-sandbox-design.md`
- Verify: `tests/test_executor_*.py`, `tests/test_broker_python_runtime.py`, `tests/test_analysis_worker_*.py`, `tests/test_python_runtime_*.py`

**Interfaces:**
- Consumes: current `local-multi-study` and exact executor commit above.
- Produces: `BrokerPythonRuntime`, `ExecutionBroker`, `DockerRunner`, UDS protocol, locked worker image, local launcher, and Docker smoke on one integration branch.

- [ ] **Step 1: Create an isolated worktree**

Use `using-git-worktrees`, then run:

```bash
git worktree add .worktrees/local-docker-runtime -b local-docker-runtime local-multi-study
git -C .worktrees/local-docker-runtime status --short
```

Expected: clean new worktree; the unrelated modified report stays untouched in its original worktree.

- [ ] **Step 2: Establish the baseline**

```bash
.venv/bin/python -m pytest tests/test_epi_python_runtime.py tests/test_run_fastapi.py tests/test_api_runtime.py -q
```

Expected: PASS from the integration worktree.

- [ ] **Step 3: Merge the reviewed executor**

```bash
git merge --no-ff 9c30ff5e1545f81a3c09c5124c5f0f49be3d96e0
```

Conflict rules: retain current multi-study/semantic-catalog behavior alongside broker construction; retain current DNS/AMI changes alongside executor AWS resources; preserve all specs/plans; use executor-side `epi_agent/executor/`, broker runtime, worker-image, and executor tests unchanged at this checkpoint; never restore an intentionally deleted current-branch file unless a merged test imports it.

- [ ] **Step 4: Verify the foundation**

```bash
.venv/bin/python -m pytest tests/test_executor_protocol.py tests/test_executor_paths.py tests/test_executor_store.py tests/test_executor_snapshots.py tests/test_executor_docker_runner.py tests/test_executor_results.py tests/test_executor_service.py tests/test_executor_app.py tests/test_broker_python_runtime.py tests/test_analysis_worker_bundle.py tests/test_analysis_worker_image.py tests/test_python_runtime_io_contract.py tests/test_python_runtime_result_transport.py -q
git diff --check
```

Expected: all pass and whitespace validation exits zero.

- [ ] **Step 5: Commit conflict resolutions if needed**

```bash
git add api epi_agent deploy infra scripts tests utils README.md config/app.env
git add -f docs/superpowers
git commit -m "merge: integrate hardened Docker executor"
```

---

### Task 2: Make local execution Docker-only and fail closed

**Files:**
- Modify: `api/deployment.py`, `api/app.py`, `api/schemas.py`, `run_fastapi.py`, `compose.yaml`
- Modify: `frontend/src/types.ts`, `frontend/src/RuntimeSettingsPanel.tsx`
- Test: `tests/test_api_deployment.py`, `tests/test_run_fastapi.py`, `tests/test_no_study_startup.py`, `frontend/src/RuntimeSettingsPanel.test.tsx`

**Interfaces:**
- Produces: `PythonSandboxReadiness(status, message, image_digest, package_state_hash)` and `validate_local_host(host, auth_mode)`.
- Changes: `RuntimeCapabilities.python_analysis: RuntimeCapability` in Python and TypeScript.

- [ ] **Step 1: Write failing loopback/broker tests**

```python
def test_local_mode_rejects_non_loopback_host() -> None:
    with pytest.raises(StartupConfigurationError, match="127.0.0.1"):
        validate_local_host("0.0.0.0", auth_mode="local")


def test_local_mode_rejects_native_execution_backend() -> None:
    with pytest.raises(ValueError, match="broker"):
        execution_backend_config(
            {"REPORT_AGENT_EXECUTION_BACKEND": "local"}, auth_mode="local"
        )
```

Add API/frontend tests that unavailable Docker yields `python_analysis.status == "not_configured"`, disables analysis, and displays remediation. Assert Compose contains `127.0.0.1:8000:8000`.

```bash
.venv/bin/python -m pytest tests/test_api_deployment.py tests/test_run_fastapi.py tests/test_no_study_startup.py -q
npm --prefix frontend test -- --run src/RuntimeSettingsPanel.test.tsx
```

Expected: FAIL.

- [ ] **Step 2: Implement fail-closed configuration**

```python
@dataclass(frozen=True)
class PythonSandboxReadiness:
    status: Literal["available", "not_configured"]
    message: str
    image_digest: str | None = None
    package_state_hash: str | None = None


def validate_local_host(host: str, *, auth_mode: str) -> None:
    if auth_mode == "local" and host not in {"127.0.0.1", "::1"}:
        raise StartupConfigurationError(
            "Local mode must bind to 127.0.0.1 or ::1."
        )
```

Construct only `BrokerPythonRuntime` in `api/app.py`. When Docker/broker self-test fails, inject a runtime that raises `PythonRuntimeFailure("SANDBOX_UNAVAILABLE", "Docker analysis sandbox is unavailable.", category="infrastructure", recoverable=True)`; never call native `exec`. Change Compose to:

```yaml
ports:
  - "127.0.0.1:8000:8000"
```

- [ ] **Step 3: Add Python capability**

```python
class RuntimeCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")
    publication_knowledge: RuntimeCapability
    db_rag_dataset: RuntimeCapability
    study_design: RuntimeCapability
    python_analysis: RuntimeCapability
```

Add matching TypeScript field and a `Python analysis` settings row with remediation when unavailable.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/python -m pytest tests/test_api_deployment.py tests/test_run_fastapi.py tests/test_no_study_startup.py tests/test_api_runtime.py -q
npm --prefix frontend test -- --run src/RuntimeSettingsPanel.test.tsx src/apiClient.test.ts
git diff --check
git add api run_fastapi.py compose.yaml frontend/src
git add -f tests
git commit -m "feat: require the local Docker analysis broker"
```

---

### Task 3: Add the optional read-only package-volume mount

**Files:**
- Modify: `deploy/docker/analysis-worker/Dockerfile`, attestation, and `scripts/build_analysis_image.py`
- Modify: `epi_agent/executor/docker_runner.py`, `epi_agent/runtimes/python/models.py`
- Test: `tests/test_analysis_worker_bundle.py`, `tests/test_analysis_worker_image.py`, `tests/test_executor_docker_runner.py`

**Interfaces:**
- Produces: `PackageVolumeMount(name: str, state_hash: str)`.
- Changes: `DockerRunner.run(job: BrokerJob, *, package_volume: PackageVolumeMount | None, cancel_requested: Callable[[], bool] | None = None) -> DockerRunOutcome` and `PythonExecutionProvenance.package_state_hash`.

- [ ] **Step 1: Write failing mount tests**

No-volume jobs must preserve the existing `docker create` security arguments. Replace the current hard-coded `linux/amd64` with a broker-captured `Literal["linux/amd64", "linux/arm64"]`; reject every other platform. A valid mount adds exactly:

```python
(
    "--mount",
    "type=volume,src=epi-agent-packages-v1,dst=/opt/epi-agent-extra,readonly",
    "--env", "VIRTUAL_ENV=/opt/epi-agent-extra/venv",
    "--env", "PATH=/opt/epi-agent-extra/venv/bin:/usr/local/bin:/usr/bin:/bin",
)
```

Reject names outside `^epi-agent-packages-v[1-9][0-9]*$`, invalid state hashes, caller destinations, and read-write analysis mounts. Assert image mount point ownership `65532:65532`.

- [ ] **Step 2: Implement strict mount identity**

```python
@dataclass(frozen=True)
class PackageVolumeMount:
    name: str
    state_hash: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"epi-agent-packages-v[1-9][0-9]*", self.name) is None:
            raise ValueError("invalid package volume name")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.state_hash) is None:
            raise ValueError("invalid package state hash")
```

Runner derives destination/read-only internally. Before image `USER`, add:

```dockerfile
RUN mkdir -p /opt/epi-agent-extra \
    && chown 65532:65532 /opt/epi-agent-extra
ENV PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1
```

Do not add a Dockerfile `VOLUME`; broker creation remains explicit.

Capture Docker's validated daemon architecture during readiness with fixed mapping `x86_64|amd64 -> linux/amd64` and `aarch64|arm64 -> linux/arm64`. Persist that literal with each admitted job and pass only it to `docker create --platform`.

- [ ] **Step 3: Propagate provenance**

Add nullable `package_state_hash`, validate `sha256:<64 hex>`, and carry it from broker admission through worker result and analysis runtime metadata.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/python -m pytest tests/test_executor_docker_runner.py tests/test_analysis_worker_bundle.py tests/test_analysis_worker_image.py tests/test_epi_python_runtime.py -q
git diff --check
git add deploy/docker/analysis-worker scripts/build_analysis_image.py epi_agent
git add -f tests
git commit -m "feat: mount analysis package volumes read-only"
```

---

### Task 4: Define package plans and resolve binary-wheel metadata

**Files:**
- Modify: `requirements.txt`
- Create: `epi_agent/environments/__init__.py`, `models.py`, `pypi.py`
- Test: `tests/test_environment_models.py`, `tests/test_environment_pypi.py`

**Interfaces:**
- Produces: `PackageRequirement`, `WheelArtifact`, `PackageChange`, `PackagePlan`, `PackageEnvironmentStatus`, and `PyPiWheelResolver.resolve()`.

- [ ] **Step 1: Write failing strict-model tests**

Test PEP 503 normalization, exact stable PEP 440 versions, import roots, deduplication, bounds, fingerprints, unknown fields, and rejection of extras/ranges/URLs/Git/paths/index flags.

```bash
.venv/bin/python -m pytest tests/test_environment_models.py -q
```

Expected: FAIL because the module is absent.

- [ ] **Step 2: Implement immutable models**

Pin `packaging==26.2` and define:

```python
class PackageEnvironmentStatus(StrEnum):
    BUILTIN = "builtin"
    AWAITING_APPROVAL = "awaiting_approval"
    INSTALLING = "installing"
    READY = "ready"
    UNHEALTHY = "unhealthy"
    RECOVERING = "recovering"


class PackageRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    project: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    imports: tuple[str, ...] = Field(min_length=1, max_length=16)


class WheelArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    filename: str = Field(min_length=1, max_length=256)
    url: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PackageChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    project: str = Field(min_length=1, max_length=128)
    from_version: str | None = Field(default=None, max_length=64)
    to_version: str = Field(min_length=1, max_length=64)


class PackagePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    requirement: PackageRequirement
    wheels: tuple[WheelArtifact, ...] = Field(min_length=1, max_length=128)
    changes: tuple[PackageChange, ...] = Field(min_length=1, max_length=128)
    fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class OverlayPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    project: str
    version: str
    imports: tuple[str, ...]
    wheel_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OverlayLock(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    packages: tuple[OverlayPackage, ...] = Field(default=(), max_length=128)


class PackageEnvironmentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    status: PackageEnvironmentStatus
    base_image_digest: str
    compatibility_version: int = Field(ge=1)
    active_lock: OverlayLock
    state_hash: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    pending_fingerprint: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
```

Normalize with `re.sub(r"[-_.]+", "-", value).lower()`; reject pre/dev/post/local versions and invalid import roots.

- [ ] **Step 3: Write and implement resolver tests**

With `httpx.MockTransport`, require only `https://pypi.org/pypi/<project>/json` and `https://pypi.org/pypi/<project>/<version>/json`, redirects off, 10-second request timeout, 1 MB per-response and 5 MB aggregate bounds, JSON type, exact identity, Python 3.12 compatibility, non-yanked compatible wheels, and SHA-256. Reject source-only/incompatible releases and never fetch wheel payload URLs.

Implement `PyPiWheelResolver.resolve(requirement: PackageRequirement, current: OverlayLock, platform: Literal["linux/amd64", "linux/arm64"]) -> PackagePlan`. Resolve `requires_dist` recursively from PyPI JSON metadata without executing package code or downloading wheels. Use deterministic highest-stable-version backtracking, evaluate markers for Python 3.12 and the target Linux architecture, cap resolution at 64 projects, 256 HTTP responses, and 1,024 solver decisions, and reject dependency extras that cannot be represented by the binary-wheel policy. Parse wheel filenames with `packaging.utils.parse_wheel_filename`; accept pure-Python wheels or matching manylinux/musllinux `x86_64`/`aarch64` wheels with `py3`, `cp312`, `abi3`, `none`, or `cp312` compatibility. Fingerprint canonical JSON of requirement, selected wheel filenames/hashes, and complete dependency changes. Return sanitized fixed errors only.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/python -m pytest tests/test_environment_models.py tests/test_environment_pypi.py -q
git diff --check
git add requirements.txt epi_agent/environments
git add -f tests/test_environment_models.py tests/test_environment_pypi.py
git commit -m "feat: resolve exact binary-wheel package plans"
```

---

### Task 5: Persist package state and manage one Docker volume

**Files:**
- Create: `epi_agent/environments/store.py`, `epi_agent/environments/manager.py`
- Create: `epi_agent/runtimes/python/package_installer.py`
- Modify: `deploy/docker/analysis-worker/Dockerfile` and attestation inputs
- Modify: `epi_agent/executor/docker_runner.py`
- Test: `tests/test_environment_store.py`, `tests/test_environment_manager.py`, `tests/test_environment_manager_docker.py`, `tests/test_package_installer.py`

**Interfaces:**
- Produces: `PackageEnvironmentStore` and `PackageEnvironmentManager.status/resolve/install_approved/mount/recover`.
- Consumes: fixed `epi-agent-packages-v1`, official digest, `PackagePlan`, and bounded Docker command executor.

- [ ] **Step 1: Write failing lifecycle tests**

Test initial built-in/no-volume state, append-only events, approval fingerprint binding, one transaction, exact overlay lock, deterministic state hash, ready/unhealthy transitions, failed install never ready, and explicit recovery confirmation.

```python
PACKAGE_TRANSITIONS = {
    "builtin": {"awaiting_approval"},
    "ready": {"awaiting_approval", "recovering"},
    "awaiting_approval": {"builtin", "ready", "installing"},
    "installing": {"ready", "unhealthy"},
    "unhealthy": {"recovering"},
    "recovering": {"ready", "unhealthy"},
}
```

- [ ] **Step 2: Implement transactional state**

Use broker-owned SQLite. Persist only plans, overlay locks, audit events, official digest, compatibility version, hash, state, and bounded diagnostics. Never persist code, data, API keys, tokens, Docker credentials, or PyPI bodies.

Implement `PackageEnvironmentStore.status() -> PackageEnvironmentRecord`, `record_proposal(plan: PackagePlan) -> PackageEnvironmentRecord`, `approve(fingerprint: str) -> PackageEnvironmentRecord`, `begin_install(fingerprint: str) -> PackageEnvironmentRecord`, `activate(lock: OverlayLock, state_hash: str) -> PackageEnvironmentRecord`, and `mark_unhealthy(code: str) -> PackageEnvironmentRecord`. Each mutation uses `BEGIN IMMEDIATE`, validates state/fingerprint, records one event, and rejects changed-content replay.

- [ ] **Step 3: Write failing Docker command tests**

First install creates exactly one versioned volume; later installs create none. Installer argv must include:

```text
docker run --rm --interactive
--label com.epi-agent.managed=true
--label com.epi-agent.role=package-installer
--read-only --cap-drop ALL --security-opt no-new-privileges
--pids-limit 64 --memory 2147483648 --cpus 1
--user 65532:65532
--tmpfs /tmp:rw,noexec,nosuid,nodev,size=268435456
--mount type=volume,src=epi-agent-packages-v1,dst=/opt/epi-agent-extra
```

The installer may use bridge networking but gets no host bind, dataset, runtime root, secrets, inherited environment, or Docker socket. The trusted manager sends a maximum-64-KiB strict JSON `PackageInstallEnvelope` on stdin. The image-bundled installer validates that envelope, writes a hashed requirements file only to `/tmp`, and invokes pip with `--require-hashes --only-binary=:all: --no-input`. No model text reaches pip argv.

- [ ] **Step 4: Implement installation and validation**

Define `PackageEnvironmentManager.VOLUME_NAME = "epi-agent-packages-v1"` and implement `status() -> PackageEnvironmentRecord`, `resolve(requirement: PackageRequirement) -> PackagePlan`, `install_approved(fingerprint: str) -> PackageEnvironmentRecord`, `mount() -> PackageVolumeMount | None`, and `recover(confirmation: str) -> PackageEnvironmentRecord`. Create the volume explicitly before the first installer run with exact labels `com.epi-agent.managed=true`, `com.epi-agent.role=package-environment`, and `com.epi-agent.compatibility=1`; validate all three labels before every mutation, mount, or deletion. Later installs reuse it and never call `docker volume create` again.

First install creates a `--system-site-packages` venv in the volume. Install exact wheels, capture overlay-only `pip list --path /opt/epi-agent-extra/venv/lib/python3.12/site-packages --format=json`, hash canonical JSON, import every approved root in an offline self-test, then activate. Serialize with a broker lock. Failure after mutation marks unhealthy and blocks the mount. Recovery deletes only the exact labeled volume after confirmation, recreates it, and installs the last good hashed lock. Never prune broadly.

Add `package_installer.py` to the reviewed worker-image inputs. It reads exactly one bounded JSON envelope from stdin, accepts only normalized exact requirements and `https://files.pythonhosted.org/` wheel URLs with matching SHA-256, creates the venv when absent, and uses `subprocess.run` with a fixed argument list and scrubbed environment. Extend the broker's command executor with `stdin_bytes: bytes | None = None`, reject more than 65,536 bytes, and keep stdin `DEVNULL` for every command except this fixed installer invocation.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -m pytest tests/test_environment_store.py tests/test_environment_manager.py tests/test_package_installer.py -q
.venv/bin/python -m pytest tests/test_environment_manager_docker.py -q -m docker
git diff --check
git add epi_agent/environments epi_agent/runtimes/python/package_installer.py epi_agent/executor/docker_runner.py deploy/docker/analysis-worker
git add -f tests/test_environment_store.py tests/test_environment_manager.py tests/test_environment_manager_docker.py tests/test_package_installer.py
git commit -m "feat: manage approved packages in one Docker volume"
```

---

### Task 6: Expose package operations through the private broker

**Files:**
- Modify: `epi_agent/executor/protocol.py`, `store.py`, `service.py`, `app.py`
- Modify: `epi_agent/runtimes/python/broker.py`
- Test: `tests/test_executor_protocol.py`, `tests/test_executor_service.py`, `tests/test_executor_app.py`, `tests/test_broker_python_runtime.py`

**Interfaces:**
- Produces: `GET /v1/packages/status`, `POST /v1/packages/resolve`, `POST /v1/packages/install`, `POST /v1/packages/recover`.
- Produces: `BrokerPackageClient.status/resolve/install/recover` using bounded UDS transport.

- [ ] **Step 1: Write strict protocol tests**

`resolve` accepts only `PackageRequirement`; `install` only the exact fingerprint plus `approve`; `recover` only a server-issued opaque confirmation. Reject extra fields, owner IDs, volume/image/mount values, Docker argv, indexes, URLs, and pip options.

- [ ] **Step 2: Implement typed endpoints**

```python
class ResolvePackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    requirement: PackageRequirement


class InstallPackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    action: Literal["approve"]
```

Local mode has one application-wide package state. Endpoints delegate to `PackageEnvironmentManager`. Package work shares the broker's single heavy-work scheduler with analysis; install/recovery blocks new analysis admission.

- [ ] **Step 3: Snapshot package identity at admission**

At admission call `manager.mount()` and persist validated volume name/state hash in the job record. Runner accepts only that stored snapshot. Running/completed jobs retain recorded provenance; new analysis waits during installation.

- [ ] **Step 4: Add graph-facing client**

Reuse bounded UDS transport. Map stable broker codes to `PythonRuntimeFailure` without returning stderr, paths, Docker output, package-index bodies, or secrets.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -m pytest tests/test_executor_protocol.py tests/test_executor_service.py tests/test_executor_app.py tests/test_broker_python_runtime.py -q
git diff --check
git add epi_agent/executor epi_agent/runtimes/python/broker.py
git add -f tests/test_executor_protocol.py tests/test_executor_service.py tests/test_executor_app.py tests/test_broker_python_runtime.py
git commit -m "feat: expose local package volume operations"
```

---

### Task 7: Add durable package approval and one-time retry

**Files:**
- Modify: `epi_agent/runtimes/python/models.py`, `epi_agent/tool_packs/analysis/python/tools.py`
- Modify: `api/schemas.py`, `api/runtime.py`, `api/server.py`, `utils/review_interrupts.py`
- Test: `tests/test_epi_python_tools.py`, `tests/test_review_interrupts.py`, `tests/test_api_runtime.py`, `tests/test_api_server.py`

**Interfaces:**
- Changes: `CustomPythonArguments.required_packages: tuple[PackageRequirement, ...] = ()`.
- Produces interrupts `package_install_approval` and `package_install_progress`.
- Produces decisions `{"action":"approve","fingerprint":"sha256:<64 lowercase hex>"}` and `{"action":"deny"}`.

- [ ] **Step 1: Write failing declaration/interrupt tests**

Require non-built-in imports to map explicitly to exact project/version/import roots; never guess `sklearn -> scikit-learn`. Missing declared packages emit:

```python
{
    "type": "package_install_approval",
    "view": {
        "project": "example-package",
        "version": "1.2.3",
        "imports": ["example_package"],
        "index": "https://pypi.org/",
        "changes": [
            {"project": "dependency", "from_version": None, "to_version": "4.5.6"}
        ],
        "fingerprint": "sha256:" + "a" * 64,
        "warning": "Installing and importing third-party packages executes third-party code.",
    },
}
```

Test approve, deny, fingerprint mismatch, checkpoint replay, install failure, one successful automatic retry, and no repeat approval when satisfied.

- [ ] **Step 2: Extend strict tool arguments**

```python
required_packages: tuple[PackageRequirement, ...] = Field(
    default=(), max_length=16
)
```

Extract absolute AST import roots. Standard library/built-ins need no declaration; each other root must map to one declared project. Reject undeclared and unused requests before submission.

- [ ] **Step 3: Implement the interrupt flow**

Query status before execution. Resolve the first missing requirement and call LangGraph `interrupt()` with the persisted exact plan. Denial makes no mutation and returns recoverably. Approval revalidates fingerprint, calls broker install, exposes durable progress, and retries the immutable request once. A second failure never loops automatically.

- [ ] **Step 4: Validate views/decisions**

Add strict Pydantic adapters. Reject private/unexpected fields, paths, non-PyPI URLs, stderr/stdout, Docker identifiers, wheel URLs, bodies, or tokens. Bind resume to active interrupt ID and fingerprint.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -m pytest tests/test_epi_python_tools.py tests/test_review_interrupts.py tests/test_api_runtime.py tests/test_api_server.py -q
git diff --check
git add epi_agent api utils/review_interrupts.py
git add -f tests
git commit -m "feat: approve missing analysis packages"
```

---

### Task 8: Build approval and progress UI

**Files:**
- Create: `frontend/src/PackageApproval.tsx`, `PackageApproval.test.tsx`
- Create: `frontend/src/PackageInstallProgress.tsx`, `PackageInstallProgress.test.tsx`
- Modify: `frontend/src/types.ts`, `apiClient.ts`, `App.tsx`, `App.test.tsx`, `index.css`

**Interfaces:**
- Consumes Task 7 interrupt variants.
- Sends only approve/fingerprint or deny; never package names, Docker values, URLs, or pip args.

- [ ] **Step 1: Write failing UI tests**

Test exact package/version/import, dependency changes, fixed PyPI origin, warning, disabled double-submit, approve/deny, progress refresh, unhealthy state, and retry. Assert secrets, paths, wheel URLs, Docker IDs, and raw diagnostics never render.

```bash
npm --prefix frontend test -- --run src/PackageApproval.test.tsx src/PackageInstallProgress.test.tsx src/App.test.tsx
```

Expected: FAIL.

- [ ] **Step 2: Add typed variants**

```typescript
export type PackageInstallApprovalInterrupt = {
  id: string;
  type: "package_install_approval";
  view: {
    project: string;
    version: string;
    imports: string[];
    index: "https://pypi.org/";
    changes: PackageChange[];
    fingerprint: string;
    warning: string;
  };
};
```

Add progress state `installing | ready | unhealthy | recovering`, bounded message, and state hash only when ready.

- [ ] **Step 3: Implement cards and App wiring**

Approve posts:

```typescript
{ action: "approve", fingerprint: interrupt.view.fingerprint }
```

Deny posts `{ action: "deny" }`. Progress reuses existing thread-state refresh rather than a separate unbounded poller. Both variants block new messages/settings and resume the original run only after ready.

- [ ] **Step 4: Verify, build, and commit**

```bash
npm --prefix frontend test -- --run src/PackageApproval.test.tsx src/PackageInstallProgress.test.tsx src/App.test.tsx src/apiClient.test.ts
npm --prefix frontend run build
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
git diff --check
git add frontend
git commit -m "feat: review local analysis package installs"
```

---

### Task 9: Complete distribution, smoke, and native-fallback removal

**Files:**
- Modify: `README.md`, `config/app.env`, `.env.example`
- Create: `.github/workflows/publish-analysis-worker.yml`
- Create: `config/analysis-sandbox.json`
- Modify: `deploy/docker/analysis-worker/python-base.lock.json`, `scripts/build_analysis_image.py`
- Modify: `epi_agent/runtimes/python/__init__.py`, `local_process.py`, graph defaults/tests
- Create: `scripts/smoke_local_docker_package_volume_real.py`
- Create: `tests/test_smoke_local_docker_package_volume_real.py`
- Modify: `scripts/smoke_docker_executor_local.py`, `tests/test_smoke_docker_executor_local.py`

**Interfaces:**
- Produces supported terminal setup for macOS/Linux and required real browser smoke.
- Removes production access to native generated-code execution.

- [ ] **Step 1: Add failing distribution/no-fallback assertions**

Assert production cannot select/import `LocalPythonRuntime`, missing Docker disables analysis, Compose is loopback-only, docs list Docker/macOS/Linux, Windows unsupported, wheels-only policy, and no volume before approval.

- [ ] **Step 2: Remove native production execution**

Remove `LocalPythonRuntime` from production exports and all application/graph defaults. If historical tests need it, move it to `tests/support/legacy_local_python_runtime.py`; production modules may not import it. Require injected `PythonRuntime` at graph construction.

- [ ] **Step 3: Add reproducible multi-architecture publication**

Update the base lock to an OCI index digest covering `linux/amd64` and `linux/arm64`. Extend image verification to inspect and test both children. Add a GitHub Actions release workflow using Docker Buildx and `packages: write` to publish `ghcr.io/xutao-wang/epi-ai-agent-analysis-worker`. It builds both supported platforms from reviewed/hashed inputs, emits SBOM/provenance attestations, pushes a commit-SHA tag, captures the OCI index digest, verifies both child platform digests and worker self-tests, and produces a signed `analysis-sandbox-release.json` artifact.

`config/analysis-sandbox.json` stores only the repository and immutable index digest consumed by a release; runtime rejects tag-only identity. Do not create a Git tag or publish externally while implementing this plan unless the user separately authorizes that release action.

- [ ] **Step 4: Document exact setup/recovery**

Document:

```bash
docker version
python scripts/build_analysis_image.py --verify-only
python scripts/run_local_executor.py
python run_fastapi.py --host 127.0.0.1 --port 8000
```

Explain built-in-only mode, first-volume creation, cumulative additions, read-only analysis use, provenance, unhealthy recovery, disk usage, unsupported Windows, and no hosted server requirement.

- [ ] **Step 5: Implement dedicated real browser smoke**

The smoke launches real broker/FastAPI/compiled frontend/OpenAI/Docker; proves built-in analysis creates no volume; requests and approves one absent small binary-wheel package; verifies one volume/install/automatic retry; reuses package without reinstall; inspects offline/non-root/read-only/no-secret controls; verifies cancellation cleanup; and preserves logs/state/screenshots/inspection/artifacts on its one failure or five-minute timeout.

- [ ] **Step 6: Run focused and broad verification**

```bash
.venv/bin/python -m pytest tests/test_environment_models.py tests/test_environment_pypi.py tests/test_environment_store.py tests/test_environment_manager.py tests/test_executor_protocol.py tests/test_executor_docker_runner.py tests/test_executor_service.py tests/test_executor_app.py tests/test_broker_python_runtime.py tests/test_epi_python_tools.py tests/test_review_interrupts.py tests/test_api_runtime.py tests/test_api_server.py tests/test_run_fastapi.py -q
npm --prefix frontend test -- --run
npm --prefix frontend run build
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
.venv/bin/python -m pytest -q
git diff --check
```

Expected: every command passes.

- [ ] **Step 7: Run each real smoke once**

```bash
.venv/bin/python scripts/smoke_docker_executor_local.py
.venv/bin/python scripts/smoke_local_docker_package_volume_real.py
```

Expected: each passes once within five minutes. Never auto-rerun failure; preserve required evidence.

- [ ] **Step 8: Commit the completed release**

```bash
git add README.md config/app.env config/analysis-sandbox.json .env.example .github deploy/docker/analysis-worker epi_agent scripts frontend/dist
git add -f tests docs/superpowers/specs/2026-08-17-local-docker-python-sandbox-design.md docs/superpowers/plans/2026-08-17-local-docker-runtime-and-package-volume.md
git commit -m "feat: ship secure local Docker analysis runtime"
```

---

## Final Review Gates

Invoke `requesting-code-review` for specification compliance, Docker/mount/secret security, package supply-chain controls, approval/concurrency correctness, macOS/Linux installation, and multi-study/DB-RAG/artifact/cancellation regressions. Then invoke `verification-before-completion` and rerun current verification; never claim success from historical output.
