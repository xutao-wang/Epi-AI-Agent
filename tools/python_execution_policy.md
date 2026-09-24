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

Generated Python may run standalone or with one selected table artifact.
Trusted parent code serializes a selected table only when dataset metadata is
explicitly supplied. The worker then exposes it to generated source as the
in-memory pandas DataFrame `dataset`. If all dataset metadata is omitted, the
worker does not define `dataset`, and code may perform calculations, dependency
probes, demonstrations, or explicitly requested in-memory simulations. A
missing or stale supplied dataset is an error and never falls back to
standalone execution.

The choice is structural: it depends on whether validated dataset identity
fields are present, not on matching conversational words such as `attached` or
`uploaded`. Standalone code must never substitute synthetic data for a request
that depends on real participant or study data.

Common installed analysis libraries are available through the fixed
application environment, but they are not preloaded into generated code.
Generated code must import each library it uses explicitly so unused optional
libraries are not loaded.

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

After `DEPENDENCY_NOT_AVAILABLE`, the loop may submit one changed Python call
using installed packages only when the alternative is scientifically
equivalent. It must not force an unsuitable workaround, silently change the
requested method, or invent results. Repeating identical code or encountering
another failure after generated-code execution starts produces
`DEPENDENCY_REPAIR_EXHAUSTED`. Static-policy, syntax, argument, and dataset
selection failures happen before generated-code execution; they preserve their
primary diagnostic and do not falsely report that code ran. Dependency
exhaustion is a nonfatal warning: Python is unavailable only for the rest of
the current user turn, other tools remain available, and a later user message
starts with a fresh budget.

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
or other installers. An unavailable import returns `DEPENDENCY_NOT_AVAILABLE`
with `Package “<module>” isn’t installed.` and structured `missing_module`
and `available_packages` details. `available_packages` is the detected subset
of the runtime's supported analysis libraries, not an inventory of every
application dependency. If the one equivalent-alternative retry does not
succeed, the agent explains why the missing package is needed, gives the pinned
dependency/restart/retry instruction, and states that no result was produced.
This does not put the application run into an error state.

Adding a dependency is a trusted operator action. The operator must identify
the correct distribution, review it, add an exact `distribution==version` pin
to `requirements.txt`, rebuild or reinstall the complete environment, verify
the import, and restart the application. Import and distribution names may
differ, such as `sklearn` and `scikit-learn`. Installing while the native
service is running or directly into a running container is unsupported. The
user sends a new request after the update; the application does not replay the
failed code automatically. Persistent dynamic package installation is not part
of this release.

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
