# Generated Python Execution Policy

## Execution model

Epi-AI-Agent uses one generated-Python execution mode named **Guarded Local**.
Each analysis runs in a fresh child process through `LocalPythonRuntime`, using
the same Python 3.12 environment and operating-system account as the
application. This behavior is the same in native and Docker deployments.

Guarded Local runs with the application's host permissions. Its validation,
resource limits, and process controls reduce accidental misuse and runaway
execution, but they are not an operating-system security boundary. They cannot
contain deliberately adversarial Python or a compromised installed dependency.

Before startup, the user must acknowledge this boundary interactively or set:

```text
REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED=1
```

## Inputs and outputs

Trusted parent code serializes one selected approved table. The worker loads
that table and exposes it to generated source only as the in-memory pandas
DataFrame `dataset`. Common installed analysis libraries are available through
the fixed application environment.

Generated source may calculate in memory, print bounded reviewable output, and
create a Matplotlib figure. The worker returns one bounded JSON result envelope
and, when present, one bounded PNG. Parent code validates both before creating
an analysis result for human review.

## Static guardrails

Source is parsed before a child process is created. Violations include a stable
rule ID, operation, source location, bounded explanation, and correction text.
At most ten source-ordered findings are returned.

The policy blocks:

- filesystem access or mutation (`PY_FILESYSTEM_ACCESS`);
- network clients, URLs, and sockets (`PY_NETWORK_ACCESS`);
- subprocess and process-control APIs (`PY_PROCESS_CONTROL`);
- host environment and secret discovery (`PY_HOST_ENVIRONMENT_ACCESS`);
- dynamic compilation, imports, or execution (`PY_DYNAMIC_EXECUTION`);
- package installers (`PY_PACKAGE_INSTALLATION`);
- interactive input (`PY_INTERACTIVE_INPUT`); and
- plotting-backend replacement (`PY_RENDERER_OVERRIDE`).

Invalid syntax returns `INVALID_PYTHON_SYNTAX`, not a policy violation.

Static checking is an application guardrail. Python is dynamic, so static
checking must not be represented as containment.

## Recovery

After `POLICY_BLOCKED`, the shared agent loop may submit one changed corrected
Python call. It must remove every reported violation. Repeating identical code
or receiving a second policy block produces `POLICY_REPAIR_EXHAUSTED` and makes
the Python tool unavailable only for the remainder of that user turn. Other
tools remain available, and a later user message starts with a fresh budget.

## Runtime controls

The parent provides an allowlisted environment plus fixed analysis values.
Provider credentials, proxy settings, Python path overrides, and unrelated host
configuration are omitted. Every attempt receives private input, work, output,
and Matplotlib directories.

On supported systems, the child receives CPU, address-space, process/thread,
and file-size limits. The parent also enforces a wall timeout, bounded native
stdout and stderr pipes, bounded result and figure files, cancellation checks,
and process-group termination so descendants are cleaned up before temporary
files are removed.

## Network and packages

Generated Python is offline by policy. External publications, APIs, and data
must enter through dedicated trusted tools and approved analysis inputs. There
is no temporary prompt that grants arbitrary network access.

The scientific package set is fixed by `requirements.txt`. Generated code
cannot invoke pip, Conda, uv, operating-system package managers, source builds,
or other installers. An unavailable import returns the controlled
`DEPENDENCY_NOT_AVAILABLE` failure. Persistent dynamic package installation is
not part of this release.

## Docker deployment boundary

Docker is optional and does not select another Python executor. It places the
entire application and its Guarded Local worker inside one container, limiting
host access to explicitly configured mounts. Generated Python still shares the
application container's filesystem, process environment, and network namespace
subject to the application guardrails above.

The shipped Compose configuration runs as a non-root user, publishes only on
host loopback, drops Linux capabilities, sets `no-new-privileges`, bounds memory
and process count, persists `/app/runtime` in a named volume, and provides a
separate optional read-only `/app/study_data` mount. It does not mount the host
home, source checkout, credentials, root filesystem, or container-engine
socket. Application networking remains enabled for model providers and trusted
tools.

Every analysis runtime manifest reports:

```json
{
  "execution_mode": "guarded_local",
  "security_boundary": "none"
}
```

These values describe the Python process itself. Docker deployment is a
separate whole-application host boundary.

