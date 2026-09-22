# Guarded Local Python and Optional Docker Design

## Purpose

Epi-AI-Agent is a local, single-user research application. Generated Python
must remain easy to run on macOS and Linux/HPC without requiring an operating
system sandbox. Users who want a stronger host boundary may run the entire
application inside Docker.

This design replaces the Linux-only Bubblewrap direction with one application
execution model named **Guarded Local**. Docker is an optional deployment
boundary around the whole application; it is not a second Python executor.

The design intentionally follows the operational model used by local coding
agents such as OpenCode and Oh My Pi: application-level authorization and
guardrails are always present, while stronger containment is supplied by the
deployment environment when the user chooses one.

## Scope

The first release covered by this design will:

- run generated Python through the existing local worker subprocess on macOS
  and Linux/HPC;
- retain typed static-policy diagnostics and the bounded one-repair recovery
  flow from `design/python-execution-policy`;
- retain clean-environment construction, private temporary run directories,
  ordinary OS resource limits, wall timeout, bounded native and Python output,
  process-group cleanup, typed failures, and staged human review;
- require a persisted acknowledgement that Guarded Local runs with the user's
  host permissions;
- provide an optional Docker image and Compose configuration for whole-app host
  isolation;
- run the container as a non-root user with narrow mounts and without the host
  Docker socket; and
- report the execution mode and absence of an in-process security boundary in
  analysis provenance.

This release will not:

- use Bubblewrap, Seatbelt, Windows job objects, a remote executor, WebAssembly,
  or an application-managed container per Python call;
- claim that native Guarded Local execution is a sandbox;
- silently describe Docker deployment as isolating generated Python from the
  rest of the application container;
- let generated code install packages or request arbitrary network access;
- implement dynamic package installation, environment generations, or a
  package-approval interface; or
- add Windows as a tested native platform.

Dynamic package installation remains a separate follow-up. The current fixed
scientific dependency set stays available through the application's Python
environment, and missing imports continue to return
`DEPENDENCY_NOT_AVAILABLE`.

## Terminology and security claims

**Guarded Local** means generated code executes in a child process under the
same operating-system account and authority as Epi-AI-Agent. Static validation
and runtime controls reduce accidental misuse, resource runaway, and common
unsafe operations. They do not form a security boundary against deliberately
adversarial Python or a compromised dependency.

**Container-isolated deployment** means the entire Epi-AI-Agent process,
including the generated-code worker, runs inside a Docker container. The
container limits access to the host outside explicitly mounted paths. Generated
code still shares the container's filesystem namespace, process environment,
and network namespace with the application, subject to Guarded Local controls.

Product documentation and API responses must not call Guarded Local a
"sandbox". The words "sandbox" and "isolated" may describe the optional Docker
deployment only when the scope is explicit: Docker protects the host; it does
not create a separate per-analysis boundary inside the container.

## Architecture

There is one Python execution path:

```text
analysis-run_custom_python
        |
        +-- validate generated source
        +-- serialize one selected DataFrame
        +-- create private input/output/work directories
        +-- start trusted worker with the application Python interpreter
        +-- enforce timeout, output bounds, and ordinary OS limits
        +-- terminate the worker process group on timeout/cancellation
        +-- validate the result envelope
        +-- stage the analysis result for human review
```

Native and Docker deployments call the same `LocalPythonRuntime`. No executor
selection, backend registry, capability probe, or dedicated analysis-runtime
path is required.

The trusted worker continues to expose the selected table as the in-memory
`dataset` variable. Generated code does not receive filesystem paths, provider
credentials, package-manager commands, runtime configuration, or permission
controls.

## Guarded Local controls

### Static policy

Before process creation, generated source is parsed and checked for prohibited
operations, including:

- host filesystem access;
- network clients and sockets;
- subprocess and process-control APIs;
- environment and secret discovery;
- dynamic code execution;
- package installers; and
- interactive input.

Validation returns typed violations with stable rule IDs, source locations,
bounded correction text, and a bounded number of distinct findings. The shared
agent loop may make one corrected tool call after `POLICY_BLOCKED`. Repeating
the same blocked call or producing a second policy block ends Python recovery
for that agent turn without corrupting the conversation or preventing later
turns.

Static policy is an application guardrail, not containment. The warning and
provenance model must say so directly.

### Worker process

Each analysis attempt starts a fresh worker using the application interpreter.
The parent supplies only an allowlisted environment plus fixed analysis values
such as `MPLBACKEND=Agg`, single-threaded BLAS settings, and a private
Matplotlib directory. Provider keys, proxy variables, Python path overrides,
and unrelated host configuration are omitted.

The parent creates a private temporary run directory containing trusted input,
work, and output subdirectories. It starts the worker in a new process session,
applies supported CPU, address-space, process/thread, and file-size limits,
bounds captured stdout and stderr at the operating-system pipe, enforces a wall
timeout, observes cancellation, and terminates the whole process group before
cleanup.

The worker returns one bounded JSON result envelope and an optional bounded PNG
figure. Trusted parent code validates these outputs before promoting them to the
existing analysis-review artifact flow.

### Consent and status

Guarded Local is the only application execution mode and is never silently
disabled. Before the first startup that enables generated Python, the user must
acknowledge:

> Generated Python runs with your local user-account permissions. Epi-AI-Agent
> applies policy checks and resource limits, but this is not an operating-system
> sandbox.

Native interactive startup records acceptance in the existing local `.env`
mechanism as:

```text
REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED=1
```

If acknowledgement is absent, interactive startup prompts once. Declining or
cancelling stops startup with a concise configuration message rather than
launching a partially functional application. Non-interactive startup, including
Compose, requires the variable explicitly and fails with the same actionable
message when it is absent. Acceptance persists across application restarts.

Runtime capability metadata always reports Python as `available` after startup
and describes it as Guarded Local. Every stored analysis runtime manifest adds:

```json
{
  "execution_mode": "guarded_local",
  "security_boundary": "none"
}
```

Docker deployment may additionally report deployment metadata separately, but
must not change the Python execution mode name.

## Network and package behavior

Generated Python remains offline by policy in both native and Docker
deployments. The application does not provide a prompt that temporarily grants
arbitrary network access to generated code. External literature, APIs, and data
must enter through dedicated trusted tools and then be supplied as approved
analysis inputs.

Generated code cannot invoke `pip`, Conda, `uv`, system package managers, source
builds, or arbitrary installers. The application uses its fixed installed
scientific package set. Package installation requested through the agent returns
a controlled unsupported-operation or missing-dependency result.

Docker does not change this rule. Installing packages manually into a running
container is not a supported persistence mechanism because those changes may be
lost when the container is recreated.

## Optional Docker deployment

### Image

The repository will provide a multi-stage or otherwise minimal Docker build
based on Python 3.12. The image installs `requirements.txt`, copies the
application and committed frontend build, and runs as a dedicated non-root
user. `.env`, runtime data, caches, local worktrees, private study data, and
other ignored content are excluded from the build context.

The container starts the existing FastAPI launcher. Container startup may bind
Uvicorn to `0.0.0.0` only when an explicit container marker is present; native
startup retains the loopback-only host validation. Compose publishes the port
only on host loopback:

```text
127.0.0.1:8080 -> container:8080
```

### Mounts and persistence

The default Compose configuration provides:

- one named read/write volume at `/app/runtime` for the SQLite checkpoint,
  artifacts, and conversation state;
- an optional narrow read-only bind mount at `/app/study_data` for installed
  study packages; and
- no mount of `/`, the user's home directory, SSH material, cloud credentials,
  source checkout, or Docker/Podman sockets.

Provider credentials and the Guarded Local acknowledgement are supplied at
container runtime from environment configuration. They are never copied into
an image layer. Generated workers continue to receive the sanitized child
environment, but Docker deployment does not claim to protect application
secrets from deliberately adversarial code executing in the same container.

### Container hardening

The Compose service uses the non-root image user, an init process, dropped Linux
capabilities, `no-new-privileges`, and bounded process and memory settings. The
application needs outbound network access for model providers and trusted data
tools, so the default container network remains enabled. Generated Python is
still denied network operations by static policy rather than a separate Docker
network namespace.

Users may run the same image with stricter deployment controls, but the shipped
configuration will not expose options that weaken the narrow mount policy.

## Migration from `design/python-execution-policy`

The new branch starts at clean master commit `2496a41`; it does not inherit the
unrelated local edits in master or the Amarel instruction edit in the existing
sandbox worktree.

The following committed concepts are essential and will be migrated with their
tests:

- `8991add`: typed Python policy validation;
- `c7418f1`: structured policy diagnostics through runtime/tool layers;
- `c090447`: bounded policy-aware recovery in the shared agent loop; and
- `647eeb2`: hardened recovery, alias analysis, and bypass regression coverage.

The policy-document portions of `684f973` will be rewritten to match this
design rather than copied verbatim. Bubblewrap-specific commits from `07daba0`
through `16166c5` are not migrated. In particular, the new branch will not add
`SandboxRunner`, `LinuxBubblewrapBackend`, seccomp-filter construction,
`REPORT_AGENT_PYTHON_ANALYSIS_RUNTIME`, Linux capability status, or real
Bubblewrap integration tests.

The existing guarded worker implementation on master remains the execution
base. Any process-output or cancellation hardening that exists only inside the
Bubblewrap backend will be reintroduced through new Guarded Local tests before
implementation, rather than copied as unverified sandbox code.

## Error handling

The existing stable failure taxonomy remains, including invalid syntax,
`POLICY_BLOCKED`, policy repair exhaustion, missing datasets, serialization
failure, timeout, cancellation, resource/output limits, missing dependencies,
worker termination, and malformed output.

Python failure affects only the current tool attempt or current agent turn as
defined by its recoverability. It must not corrupt persisted conversation state,
disable non-Python tools, or prevent a later user message.

Docker startup failures are deployment errors, not Python tool failures. They
must produce actionable messages for missing acknowledgement, unwritable runtime
storage, invalid mounts, missing credentials, and port conflicts.

## Testing

Implementation follows test-driven development. Automated coverage will include:

- typed static-policy diagnostics and alias/bypass cases;
- exactly one policy-repair attempt, unchanged-call rejection, and recovery
  exhaustion;
- environment sanitization and omission of provider credentials;
- one selected dataset and typed worker result handling;
- CPU/memory/process/file/output limits where the host supports them;
- wall timeout, cancellation, descendant cleanup, and bounded native pipes;
- startup acknowledgement persistence and non-interactive failure;
- runtime provenance naming Guarded Local without using "sandbox";
- container-host binding configuration, non-root user, narrow mounts, omitted
  secrets, dropped capabilities, and absence of the Docker socket; and
- an opt-in real Docker smoke test that starts FastAPI, executes generated
  Python, verifies review output, restarts the container, and confirms runtime
  persistence while a host sentinel outside mounted paths remains unavailable.

The normal deterministic suite must not require Docker. The real Docker smoke
test skips with an explicit reason when Docker is unavailable.

## Documentation

The README will provide two equally supported launch paths:

1. Native Guarded Local setup on macOS and Linux/HPC.
2. Optional `docker compose up --build` setup for users who want the whole
   application separated from the host.

Documentation will explain the precise trust boundary, fixed package set,
network policy, persisted runtime data, credential handling, and the fact that
Docker is optional. Bubblewrap setup, dedicated runtime copying, and
`REPORT_AGENT_PYTHON_ANALYSIS_RUNTIME` instructions will be removed.
