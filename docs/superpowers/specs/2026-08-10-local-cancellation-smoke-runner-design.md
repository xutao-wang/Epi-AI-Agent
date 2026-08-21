# Local Cancellation Smoke Runner Design

## Goal

Provide one executable command that a developer can run locally to exercise the
existing active-run cancellation browser smoke from any Git worktree. The
runner must use feature code and compiled UI from the current worktree while
reusing the main checkout's Python environment, `.env`, and installed study
data.

## Design

Add `scripts/run_active_run_cancellation_smoke_local.sh`. The shell wrapper
resolves its own repository root, asks Git for the shared Git directory, and
derives the main checkout from that directory. An optional first argument can
override the environment root when the repository layout is unusual.

The wrapper validates that the selected environment root contains an executable
`.venv/bin/python`. It then invokes the existing
`scripts/e2e_active_run_cancellation_real.py` from the current worktree with a
240-second timeout and passes the selected environment root through
`--environment-root`. The wrapper does not copy code, launch a different smoke,
or duplicate browser assertions.

## Data and Process Flow

1. The developer runs the wrapper from any directory.
2. The wrapper resolves the worktree containing itself.
3. It resolves or accepts the main checkout used for local-only dependencies.
4. The existing smoke starts the worktree API and compiled UI on localhost.
5. Playwright uploads a temporary CSV, submits a request, cancels the active
   run, and verifies the cancelled state, retained attachment, re-enabled
   composer, and absence of late assistant output.
6. The existing smoke prints its unique diagnostic directory and exit status.

## Error Handling

The wrapper exits before starting the smoke when Git metadata, the environment
root, or its Python executable cannot be found. Error messages identify the
missing path and show how to supply an explicit environment root. Runtime,
browser, API, and assertion failures remain owned by the existing smoke so its
diagnostics are preserved.

## Verification

- Run a shell syntax check against the wrapper.
- Exercise its help or validation path without launching the real smoke.
- Confirm the wrapper resolves the current worktree and expected main checkout.
- Run the real smoke only when the developer explicitly invokes the wrapper and
  grants localhost/browser permissions.

## Non-Goals

- Reimplementing the cancellation smoke.
- Copying `.env`, study data, or virtual environments into the worktree.
- Automatically rerunning a failed real-browser smoke.
- Adding external deployment or CI orchestration.
