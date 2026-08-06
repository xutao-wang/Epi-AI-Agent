# Task 7 Implementation Report

## Status

Implemented the FastAPI owner boundary and exact-session provider credential
resolution on branch `aws-test`. No AWS resources were created or changed.

## Authorization contract implemented

- Every protected handler derives `RequestIdentity` from FastAPI dependencies;
  runtime metadata handlers now receive it explicitly as well as through the
  protected router.
- Provider-triggering message submission and interrupt resume first prove
  thread ownership, then resolve the credential for exactly
  `(owner_user_id, session_id)`.
- An owned provider-triggering operation with no credential returns HTTP 428
  with `{"detail":{"code":"PROVIDER_KEY_REQUIRED"}}`.
- A mismatched owner or nonexistent thread returns the same non-disclosing
  HTTP 404 before credential status is considered.
- State reads pass an available credential only to support persisted
  checkpoint recovery. With no credential, state/history/runtime/artifact
  reads and empty-thread operations remain usable and never return 428.
- Request bodies and paths expose no owner, Cognito `sub`, or session selector.

## Files

- `api/server.py`: owner preflight, exact-session credential resolution,
  provider-key forwarding, optional state-recovery key, explicit identities on
  runtime metadata handlers, and authenticated/authorized upload-size checks.
- `api/runtime.py`: provider-free `authorize_thread(identity, thread_id)` owner
  preflight plus raw SQLite checkpoint projection for no-key state/file reads.
- `api/auth.py`: injectable JWKS-client seam used by the real Cognito verifier.
- `api/app.py`: supplies the production checkpoint path to the API runtime.
- `api/schemas.py`: forbids extra identity-like fields on provider-key and
  conversation-rename requests.
- `tests/test_api_server.py`: 428, credential forwarding, keyless read, and
  state-recovery contract tests.
- `tests/test_api_multi_user_isolation.py`: signed RSA multi-user TestClient
  matrix covering conversations, state/options/messages/reset/export,
  attachments, datasets, analysis results, artifacts, interrupts, anonymous
  requests, malformed sessions, nonexistent IDs, and identity-field injection.
- `tests/test_api_runtime.py`: real SQLite/LangGraph interrupt recovery across
  runtime reconstruction, including cross-owner read/resume denial.
- `scripts/smoke_api_owner_authorization.py`: dedicated production-boundary
  FastAPI/Cognito/runtime/history/credential/LangGraph/SQLite smoke.
- `.superpowers/sdd/task-7-report.md`: this report.

## TDD evidence

### Baseline

Command:

```text
.venv/bin/python -m pytest tests/test_api_server.py -q
```

Output:

```text
..............................................                           [100%]
46 passed in 2.61s
```

### RED: provider-key route contract

After adding provider-key-aware fake signatures and new endpoint tests, before
editing production code:

```text
.venv/bin/python -m pytest tests/test_api_server.py -q
```

Output summary:

```text
14 failed, 36 passed in 3.91s
```

The failures were the expected missing `provider_api_key` forwarding and
missing optional state-recovery key. Existing provider-triggering tests also
failed against the stricter test fake, proving the old handlers did not supply
the production runtime's required keyword argument.

### RED: signed multi-user API matrix

Before editing production code:

```text
.venv/bin/python -m pytest tests/test_api_multi_user_isolation.py -q
```

Output:

```text
.F....F..                                                                [100%]
2 failed, 7 passed in 1.98s
```

The two expected failures were cross-owner guessed message submission and
interrupt resume returning HTTP 500 instead of the required non-disclosing
404. Both failures occurred before graph invocation.

### Real SQLite recovery test

Command:

```text
.venv/bin/python -m pytest tests/test_api_runtime.py::test_owner_can_recover_sqlite_interrupt_but_other_user_cannot_read_or_resume -q
```

Output:

```text
.                                                                        [100%]
1 passed in 0.91s
```

This used a real `SqliteSaver`, a real compiled LangGraph interrupt, a closed
first SQLite connection, and a reconstructed runtime over the same database.

### GREEN: required Task 7 suites

Command:

```text
.venv/bin/python -m pytest tests/test_api_multi_user_isolation.py tests/test_api_server.py tests/test_api_runtime.py -q
```

Output before the final data-existence assertion tightening:

```text
........................................................................ [ 43%]
........................................................................ [ 86%]
......................                                                   [100%]
166 passed in 3.21s
```

Final verification after tightening the matrix to create and successfully read
real owner-A attachment, dataset, analysis, figure, and table data:

```text
........................................................................ [ 43%]
........................................................................ [ 86%]
......................                                                   [100%]
166 passed in 3.99s
```

## Dedicated smoke

The required smoke was run exactly once:

```text
.venv/bin/python scripts/smoke_api_owner_authorization.py
```

It failed before application startup because this managed sandbox denied the
loopback JWKS fixture's socket bind:

```text
PermissionError: [Errno 1] Operation not permitted
```

Per `AGENTS.md`, the failed smoke was not rerun automatically. Review then
identified that PyJWT rejects `file://` JWKS URIs. The production
`CognitoTokenVerifier` received an injectable JWKS-client seam, and the smoke
now supplies the generated RSA public key through an in-memory signing-key
client while still exercising the real verifier, FastAPI, runtime, LangGraph,
SQLite, history, and credential storage. No AWS or provider call is made.

After explicit authorization to run the corrected smoke once, command:

```text
.venv/bin/python scripts/smoke_api_owner_authorization.py
```

Output:

```text
API owner authorization smoke: PASS
```

Its source was also syntax-checked:

```text
.venv/bin/python -m py_compile scripts/smoke_api_owner_authorization.py
```

Output: none; exit code 0.

## Review-finding RED/GREEN evidence

The final read-only recovery, upload precedence, schema-injection, and JWKS
seam regressions were written before their fixes.

RED command:

```text
.venv/bin/python -m pytest tests/test_api_runtime.py::test_owner_can_recover_sqlite_interrupt_but_other_user_cannot_read_or_resume tests/test_api_multi_user_isolation.py::test_oversized_attachment_is_rejected_after_identity_and_ownership tests/test_api_multi_user_isolation.py::test_sensitive_identity_fields_are_not_accepted_from_request_bodies tests/test_api_auth.py::test_cognito_verifier_accepts_an_injected_jwks_client -q
```

RED output:

```text
FFFF                                                                     [100%]
4 failed in 1.53s
```

The failures proved: no `checkpoint_path` read interface; pre-auth 413;
provider/rename extras accepted with 200; and no JWKS-client injection.

GREEN output for the same command:

```text
....                                                                     [100%]
4 passed in 0.75s
```

The SQLite case reconstructs the runtime, supplies no provider key, recovers
the same interrupt, reads an approved figure, and builds the archive. The
oversized-upload case proves response precedence 401/400/404/413 for
anonymous/malformed/foreign-owner/owner requests.

## Final focused verification

API/runtime/storage command:

```text
.venv/bin/python -m pytest tests/test_api_multi_user_isolation.py tests/test_api_server.py tests/test_api_runtime.py tests/test_provider_credentials.py tests/test_conversation_history.py tests/test_user_storage.py tests/test_attachment_artifacts.py tests/test_dataset_artifacts.py -q
```

Output:

```text
217 passed in 3.73s
```

Socket-free auth selection:

```text
.venv/bin/python -m pytest tests/test_api_auth.py -q -k 'local_verifier or injected_jwks_client or protected_routes or canonical_session_ids or health_and_public_config'
```

Output: `7 passed, 10 deselected in 0.73s`.

Application/deployment/provider-startup command:

```text
.venv/bin/python -m pytest tests/test_no_study_startup.py tests/test_api_deployment.py tests/test_provider_startup.py -q
```

Output: `29 passed in 1.42s`.

Compilation and diff checks exited 0 with no output:

```text
.venv/bin/python -m py_compile api/auth.py api/app.py api/runtime.py api/schemas.py api/server.py scripts/smoke_api_owner_authorization.py
git diff --check
```

Final combined verification command:

```text
.venv/bin/python -m pytest tests/test_api_multi_user_isolation.py tests/test_api_server.py tests/test_api_runtime.py tests/test_api_auth.py::test_cognito_verifier_accepts_an_injected_jwks_client tests/test_provider_credentials.py tests/test_conversation_history.py tests/test_user_storage.py tests/test_attachment_artifacts.py tests/test_dataset_artifacts.py -q
```

Output:

```text
........................................................................ [ 33%]
........................................................................ [ 66%]
........................................................................ [ 99%]
..                                                                       [100%]
218 passed in 3.78s
```

## Broader regression audit

Command:

```text
.venv/bin/python -m pytest tests -q --ignore-glob='tests/test_e2e*'
```

Output summary:

```text
5 failed, 951 passed, 7 skipped, 10 errors in 28.92s
```

The ten errors are all the existing `tests/test_api_auth.py` loopback JWKS
fixture receiving the same sandbox `PermissionError`. The five failures are in
unchanged areas: one existing model-output label expectation and four existing
working-demo delivery/README expectations. None touches Task 7 production or
test files. The complete required Task 7 suites pass independently.

## Concerns

- The first smoke attempt was blocked by sandbox socket policy. The corrected,
  explicitly authorized socket-free smoke passed once.
- The broad non-E2E suite is not globally green for the unrelated baseline and
  sandbox reasons listed above.
- Newly required `tests/` and `scripts/` files are ignored by broad repository
  patterns; both were intentionally force-staged with `git add -f`.
