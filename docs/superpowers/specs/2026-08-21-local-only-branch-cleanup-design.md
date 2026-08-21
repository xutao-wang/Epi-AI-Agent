# Local-Only Branch Cleanup Design

## Objective

Make `local-multi-study` an explicitly local-only application branch before
integrating the multi-provider feature from `master`. AWS infrastructure,
AWS deployment automation, Cognito authentication, browser-submitted provider
credentials, and Docker packaging belong on `aws-test`, where their more
complete implementations are already preserved.

The cleanup must not change the local application's epidemiology workflows,
multi-study routing, conversation behavior, study-package handling, attachment
handling, or native Python execution.

## Branch and sequencing

The cleanup is developed on
`cleanup/local-only-before-master-merge-20260821`, based exactly on
`local-multi-study` commit `9838671`. The incomplete `master` merge was aborted
before starting this work.

The sequence is:

1. Complete and verify the local-only cleanup.
2. Integrate the verified cleanup into `local-multi-study`.
3. Create a fresh temporary integration branch.
4. Merge `master` and adapt its Anthropic/OpenAI-compatible provider feature
   to the local-only architecture.

No cleanup or integration work changes `aws-test`. No branch is pushed as part
of the local cleanup unless separately authorized.

## Approaches considered

### Remove only visible infrastructure directories

Deleting only `infra/aws` and `deploy/aws` would leave AWS scripts, tests,
documentation, Cognito code, Docker packaging, and references to deleted
assets. This produces a misleading and partially broken branch.

### Local-only public architecture with retained internal identity boundaries

This is the selected approach. Remove AWS, Docker, Cognito, and session-key
features from the public application while retaining the fixed local identity
and internal ownership boundaries used by conversations, attachments, and
storage. It provides a clear local product without rewriting stable data
isolation internals.

### Remove identity and ownership abstractions entirely

This would simplify some signatures but would force a broad rewrite of thread,
storage, attachment, and history code. It adds risk without improving the
local user experience, so it is out of scope.

## Removal boundary

### AWS infrastructure and deployment

Remove the AWS-only subsystem, including:

- `infra/aws/**` and `deploy/aws/**`;
- `docs/aws/**`;
- AWS provisioning, release, recovery, installation, and smoke scripts;
- AWS-only unit, smoke, infrastructure, host-asset, and release tests; and
- AWS-specific historical execution reports and design/plan documents whose
  only subject is the removed deployment subsystem.

References in active documentation, command help, test discovery, and release
logic must be removed or rewritten so no retained file points at deleted AWS
assets.

### Docker packaging

Remove `Dockerfile`, `compose.yaml`, `.dockerignore`, Docker-only documentation,
and tests whose sole purpose is exercising a container. The application will
continue to start natively with Python. No retained runtime path may shell out
to Docker.

### Cognito and browser authentication

Remove Cognito configuration, token verification, hosted authentication
middleware, frontend sign-in/sign-out gates, Cognito client code, and their
tests. The HTTP API will always resolve the existing fixed local identity.

The local identity and request-identity types may remain where they provide a
stable interface to conversation, attachment, and storage code. They must not
expose a configurable hosted-authentication mode.

### Provider credentials before the provider merge

Remove the browser `/api/session/provider-key` lifecycle, in-memory
per-user/session provider credential store, and `ProviderKeyGate`. Local OpenAI
credentials continue to come from `.env`/`OPENAI_API_KEY` during the cleanup.

The subsequent `master` integration will generalize environment-based local
credentials to `ANTHROPIC_API_KEY` and custom endpoint variables. That provider
work is deliberately excluded from this cleanup so failures can be attributed
to one change set at a time.

### Hosted worker and deployment hooks

Remove hosted-worker launcher configuration and assumptions, including
`REPORT_AGENT_PYTHON_WORKER_LAUNCHER`. Native `LocalPythonRuntime` execution
remains the only Python execution path. Generic path helpers that are still
used locally may remain, but AWS-specific branches and validation must be
removed.

## Preserved local behavior

The cleanup must preserve:

- native startup through `run_fastapi.py`;
- loading local configuration and secrets from `config/app.env` and `.env`;
- the fixed `local-user`/local-session identity used internally;
- conversation creation, history, rename, archive, restore, and deletion;
- conversation thread isolation;
- uploads, attachment staging, artifact access, and generated datasets;
- study discovery, package installation, multi-study selection, and DB-RAG;
- publication and study-design tools;
- cancellation, review interrupts, activity history, and tool recovery;
- local subprocess-based Python execution; and
- the existing frontend behavior after removing authentication and provider-key
  gates.

Persistent local data formats and paths must not change. No migration of the
checkpoint database, conversation database, runtime directory, or installed
study packages is part of this cleanup.

## Local application flow

At startup, the application loads local environment configuration, validates
the required local OpenAI credential, discovers installed studies, and builds
one FastAPI application. Every request receives the fixed local identity.
Conversation and storage services continue using that identity through their
existing interfaces.

The frontend loads the application directly without an authentication gate.
It does not call provider-key status, submission, or deletion endpoints. Model
requests receive the environment-derived credential through the local runtime
factory rather than a browser credential store.

## Error handling

- A missing local `OPENAI_API_KEY` remains an actionable startup error.
- Invalid local configuration fails before the server begins accepting work.
- Requests do not produce Cognito authorization failures or provider-key gate
  responses because those modes no longer exist on this branch.
- Existing safe public handling of model, workflow, attachment, and study
  errors remains unchanged.

## Testing and acceptance

Verification must not require AWS credentials, an AWS account, Docker, or a
running container daemon.

Acceptance requires:

1. A repository scan confirms that active local code and user documentation do
   not reference AWS infrastructure, Cognito, Docker, deleted deployment
   assets, or the hosted worker launcher.
2. The complete retained Python test suite passes with AWS-, Cognito-, and
   Docker-only tests removed or replaced by local behavior coverage.
3. The complete frontend test suite passes.
4. The frontend production build succeeds and the tracked hashed assets match
   `frontend/dist/index.html`. The AWS-only
   `frontend/dist/build-manifest.json` provenance file is removed with its
   release builder.
5. Targeted local smoke coverage verifies startup, health, conversation
   creation, message execution boundaries, conversation history, attachments,
   installed-study routing, and native Python execution.
6. Git status and diff review confirm that no unrelated study data, generated
   datasets, or user work was changed.

## Out of scope

- Modifying or deleting the `aws-test` branch or `aws-private/aws-test`;
- integrating `master` or implementing multi-provider routing during cleanup;
- changing local study packages or schema catalogs;
- redesigning conversation/storage ownership internals;
- changing persistent local data formats; and
- pushing branches, opening a pull request, or modifying remote `master`.
