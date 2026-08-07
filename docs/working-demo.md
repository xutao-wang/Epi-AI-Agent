# Working demo operation boundary

## Local native operation

The supported researcher workflow remains:

```bash
source .venv/bin/activate
python run_fastapi.py
```

Python 3.12 is required. Docker is not required. Unless explicitly overridden,
the launcher sets `REPORT_AGENT_AUTH_MODE=local`, verifies the OpenAI key from
`.env` (or prompts for and saves a verified key), prepares the selected runtime
directory, and serves the committed browser build. Local requests use the fixed
`local-user` principal and fixed local session UUID. Legacy unowned local
conversation rows are claimed only for that principal, preserving prior local
work without exposing them in hosted mode.

## Cognito and per-user provider keys

Hosted/native startup uses `REPORT_AGENT_AUTH_MODE=cognito` and requires all of
the following:

- `REPORT_AGENT_AWS_REGION`
- `REPORT_AGENT_COGNITO_USER_POOL_ID`
- `REPORT_AGENT_COGNITO_APP_CLIENT_ID`
- `REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT`
- `REPORT_AGENT_AUTH_REDIRECT_URI`
- `REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI`

Set `REPORT_AGENT_RUNTIME_ROOT`, `REPORT_AGENT_CHECKPOINT_DB_PATH`, and
`REPORT_AGENT_STUDY_ROOT` to private writable locations. Set
`REPORT_AGENT_WEB_CONCURRENCY=1` (and `WEB_CONCURRENCY=1` if the hosting platform
uses it). This release rejects any other worker count because provider
credentials and active job registries remain in process memory.

Do not configure a server-owned `OPENAI_API_KEY` for Cognito mode. After browser
login, each user supplies their own key through the provider-key screen. The
backend validates it against the real OpenAI service and stores it only under
that Cognito subject and canonical browser-session UUID. The key is not written
to SQLite, runtime artifacts, logs, browser storage, or API responses.

Provider-key state is intentionally temporary. A key is removed on logout,
server restart, access-token expiry, or 12 hours of inactivity. Re-entering a
key restores provider-backed work for that browser session. Conversations,
LangGraph checkpoints, attachments, datasets, and exports persist across
restart under owner-specific runtime paths and remain inaccessible to other
subjects even when an identifier is guessed.

## Data and access policy

The hosted working demo is invitation-only. Use synthetic data or data that has
been fully de-identified before upload. Do not upload direct identifiers,
protected health information, confidential source records, or provider keys as
data. The included RePORT India study assets and example CSVs are synthetic.

## Acceptance smoke

Run the bounded real acceptance smoke only with an existing OpenAI credential:

```bash
OPENAI_API_KEY=... .venv/bin/python scripts/smoke_multi_user_isolation_real.py
```

The smoke creates temporary runtime and SQLite paths, an ephemeral RSA key, and
a local protocol-real JWKS endpoint. It starts the production FastAPI app in
Cognito mode with one worker; mints two RS256 access tokens with distinct
subjects and canonical session UUIDs; validates each user's real provider key;
creates a conversation and synthetic CSV; restarts FastAPI; and verifies owner
persistence, provider-key loss, cross-owner denials, and exact key absence from
SQLite, runtime files, captured response bodies, and logs. Every subprocess is
stopped in `finally`, and the run is capped at five minutes.

Missing `OPENAI_API_KEY` is a failed prerequisite and exits nonzero; it is never
reported as a skip or pass. The smoke uses a local issuer because this project
intentionally provisions no AWS resources. It does not replace the future
launch smoke against the real Cognito pool and deployed browser UI.

## Phase 2A AWS delivery

See the [Phase 2A runbook](aws/phase2a-runbook.md). Repository support does not
mean a live stack exists until Task 11; local startup instructions above remain
unchanged.
