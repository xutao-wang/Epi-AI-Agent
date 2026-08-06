# Task 10 Implementation Report

## Status

Implemented mode-aware native startup and the bounded real multi-user hosted
acceptance smoke on `aws-test`. Native local startup remains
`python run_fastapi.py`; Docker and AWS resources are not involved.

## Delivered contract

- The native environment defaults explicitly to `REPORT_AGENT_AUTH_MODE=local`.
  Local mode invokes the existing `.env` credential verifier exactly once and
  retains its prompt, normalization, verification, and persistence behavior.
- Cognito mode skips the terminal/server credential verifier, requires the
  complete existing Cognito configuration, starts without a server-owned
  `OPENAI_API_KEY`, and rejects worker settings other than exactly one.
- The real smoke creates temporary runtime/SQLite paths and a subprocess-hosted
  JWKS endpoint backed by an ephemeral RSA key. It mints two RS256 Cognito-like
  access tokens with distinct subjects and canonical session UUIDs, validates
  each user's real BYOK credential, binds a synthetic CSV to user A's thread,
  restarts the production FastAPI app, and checks persistence plus key reset.
- After restart, the smoke re-enters only user A's key and checks user A can
  list, open, read, and download the persisted data while user B cannot list,
  read, mutate, upload, download, resume, delete, reset, or export guessed IDs.
- The smoke scans the runtime tree (including SQLite), both FastAPI logs, the
  JWKS log, and all captured HTTP response bodies for exact provider-key text.
  It stops every subprocess in `finally` and enforces a 300-second maximum.
- README and working-demo documentation cover local native operation, Cognito
  variables and BYOK, invitation-only synthetic/de-identified data, ephemeral
  keys, owner-specific persistence, and deferred AWS/CloudFront delivery.

## TDD evidence

Launcher RED:

```text
.venv/bin/python -m pytest tests/test_run_fastapi.py -q
8 failed, 33 passed
```

Launcher GREEN:

```text
.venv/bin/python -m pytest tests/test_run_fastapi.py -q
41 passed
```

Smoke-helper RED:

```text
.venv/bin/python -m pytest tests/test_smoke_multi_user_isolation_real.py tests/test_run_fastapi.py -q
5 failed, 41 passed
```

The five failures were the expected missing smoke-script boundary. Helper and
launcher GREEN:

```text
.venv/bin/python -m pytest tests/test_smoke_multi_user_isolation_real.py tests/test_run_fastapi.py -q
46 passed
```

Affected backend verification:

```text
.venv/bin/python -m pytest tests/test_run_fastapi.py tests/test_smoke_multi_user_isolation_real.py tests/test_no_study_startup.py tests/test_public_config.py tests/test_provider_credentials.py tests/test_api_multi_user_isolation.py -q
88 passed
```

## Real smoke status

`OPENAI_API_KEY` was present, so an escalated one-shot run was requested. The
execution reviewer rejected it before process creation because the smoke would
send the existing credential and synthetic fixture data to OpenAI and may incur
paid external API calls without explicit trusted-user authorization for that
egress. The run was not retried or circumvented.

Status: **NOT EXECUTED — explicit paid-egress authorization required.** This is
not a smoke pass or application failure, and no AWS/provider resource was
created or called.

## Verification evidence

Frontend:

```text
npm --prefix frontend test
23 files passed; 189 tests passed

npm --prefix frontend run build
52 modules transformed; production bundle built
```

The manifest writer refreshed `frontend/dist/build-manifest.json`, then reported
the same unrelated repository ignore-boundary findings documented by prior task
reports:

```text
FAIL: local-only path is not ignored: local_data/example.csv
FAIL: required working-demo path is ignored: scripts/verify_working_demo_delivery.py
FAIL: required working-demo path is ignored: tests/test_working_demo_delivery.py
```

The full Python suite was rerun with local-socket permission, eliminating the
restricted-sandbox JWKS errors:

```text
4 failed, 1027 passed, 7 skipped
```

All four failures are outside Task 10: one pre-existing model-profile label
expectation and three ignored/untracked working-demo boundary/documentation
expectations. Task 10's focused and affected suites are green.

## Concerns

- The real acceptance smoke remains unexecuted until the user explicitly
  authorizes sending the synthetic fixture and existing credential to OpenAI.
- The project intentionally uses a smoke-only local issuer/JWKS injection into
  the real production app. A real Cognito-pool/browser smoke belongs to the next
  AWS delivery project.
- Existing ignored local development files prevent the broad manifest audit and
  full suite from being completely green; they were not modified because they
  are outside Task 10 and may contain user work.

## Final review fixes

A read-only review found four smoke-hardening gaps. New tests were observed RED
before the fixes (`5 failed, 4 passed`), then GREEN (`9 passed`):

- The smoke is now self-contained instead of importing the ignored local
  `scripts.e2e_process_harness`, so a fresh checkout can execute it.
- The Cognito child receives an explicit empty `OPENAI_API_KEY`, preventing the
  environment loader from restoring the local `.env` key. The Uvicorn factory
  asserts the key is empty both before and after importing the production app.
- Failure diagnostics stop processes, scan logs/runtime/responses/traceback for
  the key, and replace any leaked diagnostic with a redacted finding. Terminal
  failure output never includes the exception message.
- Every restart/final/failure cleanup receives the single global deadline;
  non-finite timeout values are rejected.

Final affected verification after those fixes: `92 passed in 2.92s`; Python
syntax compilation also exited 0.

The follow-up review required cleanup time inside, rather than after, the hard
bound. A deterministic RED test first showed the missing deadline split. The
smoke now reserves the final ten seconds of the default five-minute bound for
termination/reaping, retries warned live processes without clearing them, and
scans only after shutdown is confirmed. Final helper plus launcher verification
is `51 passed in 0.23s`; the final affected suite is `93 passed in 2.61s`.

Task 10's acceptance plan explicitly defines this as a protocol-real backend
HTTP route smoke, while Task 8 owns the compiled-browser authentication smoke.
The multi-user isolation smoke therefore retains the planned HTTP boundary;
the future deployed Cognito/CloudFront project must run the real-pool browser
acceptance smoke before launch.

## Blocker remediation

The hosted-foundation review blockers were reproduced before implementation.
The first focused RED run reported `6 failed, 4 passed`: expiry removed provider
credentials without releasing cached graphs, scoped graph construction gave the
Python runtime the global root, and the browser smoke depended on an ignored
helper plus an undeclared Playwright installation. A separate request-boundary
RED test proved that rejected expired tokens never reached `credential_store.get`
and therefore did not release the cached session.

The production credential store now notifies `ReportAgentApiRuntime.release_session`
when idle or token expiry removes a credential. `create_app` prunes expired
credentials before authentication on every request, so even a request rejected
with HTTP 401 releases expired cached graphs. Callbacks execute after the store
lock is released. Focused verification is `11 passed`.

Scoped graph construction now gives `LocalPythonRuntime` the exact
owner/thread `storage.execution` directory. The regression executes a real
synthetic dataframe request and records that its temporary directory is created
under that exact scoped path.

The Task 8 browser smoke helper is tracked, Playwright is pinned in
`requirements.txt`, and the README documents `python -m playwright install
chromium`. The browser smoke itself was not run because Chromium/Playwright are
declared prerequisites, not silently assumed local tools. The paid OpenAI smoke
also remains unexecuted for the authorization reason above.

Final blocker verification:

```text
.venv/bin/python -m pytest tests/test_provider_credentials.py tests/test_api_server.py tests/test_api_runtime.py tests/test_no_study_startup.py tests/test_graph_studies.py tests/test_epi_python_runtime.py tests/test_smoke_multi_user_isolation_real.py -q
204 passed in 15.67s

npm --prefix frontend test
23 files passed; 189 tests passed

npm --prefix frontend run build
52 modules transformed; production bundle built

.venv/bin/python -m py_compile scripts/smoke_browser_auth_gates_real.py scripts/e2e_process_harness.py api/provider_credentials.py api/server.py graph/builder.py
exit 0
```
