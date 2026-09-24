# Guarded Local Missing-Dependency Design

> Historical design: retry and terminal behavior here is superseded by
> `tools/python_execution_policy.md`.

## Purpose

When generated Python imports a module that is not installed, Epi-AI-Agent
must stop that analysis attempt without crashing the application or inviting
generated code to install software. The user-facing error should be short,
while operator documentation defines a reproducible way to extend the fixed
analysis environment.

## Scope

This change will:

- return `DEPENDENCY_NOT_AVAILABLE` with the concise message
  `Package “<module>” isn’t installed.`;
- include the unavailable import name as structured `missing_module` detail;
- preserve the conversation, FastAPI process, and later user requests after
  the current analysis attempt stops;
- document separate native and Docker package-update procedures; and
- test the worker, runtime propagation, API projection, and visible failure
  message.

This change will not:

- install packages from generated Python;
- add an in-application approval or installation interface;
- infer a package-distribution name from an import name;
- retry the failed analysis automatically; or
- create per-user, additive, or dynamically versioned environments.

## Failure behavior

The trusted worker catches `ModuleNotFoundError` and emits a typed error:

```json
{
  "code": "DEPENDENCY_NOT_AVAILABLE",
  "message": "Package “xxx” isn’t installed.",
  "category": "unsupported_runtime",
  "recoverable": false,
  "details": {"missing_module": "xxx"}
}
```

The parent runtime validates and propagates the structured detail through the
Python tool error. The failure remains non-recoverable within the current
agent run because the model cannot make the dependency available and must not
loop or attempt installation. The shared runtime ends that run cleanly and the
API projects the concise message into the existing run-failure card.

No analysis result is created for the failed attempt. The server remains
running, saved conversation state remains valid, other conversations remain
usable, and a later user message can start a new run.

## Operator-managed package updates

`requirements.txt` remains the canonical, pinned package definition. An
operator who accepts a new dependency must first identify the correct Python
distribution and choose an exact version. Import names are not treated as pip
distribution names; for example, the `sklearn` import is supplied by the
`scikit-learn` distribution.

For a native deployment, the operator will:

1. stop Epi-AI-Agent;
2. add `distribution==version` to `requirements.txt`;
3. install the complete requirements file into the application virtual
   environment;
4. verify the required import using that environment's Python interpreter;
5. run the relevant automated smoke test; and
6. restart Epi-AI-Agent from the updated environment.

For a Docker deployment, the operator will:

1. stop the Compose service;
2. add `distribution==version` to `requirements.txt`;
3. rebuild the application image;
4. start the rebuilt service; and
5. verify the required import inside the rebuilt container.

Installing directly into a running container is unsupported because the
change is not reproducible and disappears when the container is recreated.
Installing while a native service is running is also unsupported because
concurrent analyses could observe inconsistent environment state.

After a successful update, the user sends a new request to retry the analysis.
The application does not retain or automatically replay failed generated code.

## Security and reproducibility

Package installation is a trusted operator action outside the generated-code
worker. Operators should review the package source and license before adding
it. Exact version pins make native installs and Docker rebuilds follow the same
declared dependency set.

The generated-code policy continues to reject pip, Conda, uv, system package
managers, source builds, subprocess installers, and arbitrary network access.
The missing-dependency response must never include an executable installation
command derived from model-generated input.

## Documentation and interface

The run-failure card shows only the concise missing-package message. It does
not show the installation procedure, administrator language, or a guessed pip
command. The README and Python execution policy contain the complete native and
Docker procedures and explain that the analysis can be retried in a new user
message after the environment is updated.

## Testing

Implementation will follow test-driven development and cover:

- a real worker import of a definitely unavailable module;
- the exact concise public message and stable error code;
- propagation of `details.missing_module` through the worker, runtime, and
  Python tool layers;
- clean termination of the current run without terminating the server;
- successful processing of a subsequent user request; and
- documentation assertions for both native and Docker update paths, exact
  version pinning, import/distribution-name differences, and no runtime
  installation.
