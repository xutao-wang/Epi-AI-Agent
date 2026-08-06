# AWS Invitation-Only Research Demo Design

Date: 2026-08-06  
Status: Approved in conversation; awaiting written-spec review

## Objective

Deploy Epi Agent as an invitation-only AWS research demo that is reachable from
anywhere, preserves each user's conversations and artifacts, automatically
installs a pinned synthetic study package, and lets each user supply their own
OpenAI API key.

The first release runs economically on one EC2 instance without Docker. Its
application boundaries must allow later migration to multiple replaceable web
and execution machines without redesigning ownership or public API semantics.

## Approved Product Constraints

- The initial audience is 5–20 invited researchers with low concurrency.
- Only synthetic or fully de-identified research data is allowed.
- Cognito email/password accounts are invitation-only; public self-registration
  is disabled.
- Every conversation, upload, derived dataset, figure, table, and export has
  exactly one Cognito user owner.
- The source synthetic study package is shared, versioned, and treated as
  read-only application input.
- Users supply their own OpenAI API keys. The application never pays OpenAI
  usage from a shared project key.
- OpenAI keys are held server-side in memory only and disappear at logout,
  session expiry, application restart, deployment, or EC2 stop.
- Generated Python continues to use the native local-process runtime. Docker
  execution is not assumed to work and is not a launch dependency.
- The first public address is the AWS-provided CloudFront HTTPS hostname. A
  branded domain can be added or replaced later.
- EC2 can be stopped for private testing. The application is unavailable while
  it is stopped; normal startup must require only starting the instance and
  waiting for health checks.

## Non-Goals For The First Release

- Anonymous access or public self-service signup
- Identifiable or regulated research data
- Hostile-code sandboxing or arbitrary user-submitted Python
- Multiple EC2 application instances or zero-downtime deployment
- RDS, Redis, a durable job queue, ECS, EKS, or Lambda execution
- Guaranteed continuation from the middle of an in-flight Python or LLM call
- Migrating local development conversations into AWS automatically
- A custom branded user-facing domain on the first day
- Amazon Bedrock or a provider migration; direct OpenAI access remains the
  initial provider architecture

## Current Application Baseline

The existing application already has useful persistence and execution
boundaries:

- FastAPI serves the API and compiled React application.
- LangGraph uses `SqliteSaver` in `agent_memory_fastapi.db`.
- `ConversationHistoryStore` stores sidebar metadata in the same database.
- Attachments and generated datasets are stored below the configured runtime
  root and already use thread-scoped paths.
- The native Python runtime starts a subprocess with a sanitized environment,
  time, memory, process-count, and output-size limits.
- Study archives have manifest, checksum, validation, staging, and immutable
  version installation behavior.
- Background graph runs use daemon threads and publish in-memory run status.

The AWS launch requires additional boundaries:

- Cognito authentication and per-request JWT validation
- user ownership on conversations and artifacts
- per-user rather than process-global OpenAI credentials
- reproducible EC2 bootstrap and deployments
- AWS-backed study archive acquisition
- native execution hardening appropriate to an internet-reachable demo
- monitoring, backups, startup recovery, and cost controls

## Target Architecture

```text
Invited user's browser
        |
        | HTTPS, Cognito authorization code + PKCE
        v
CloudFront default HTTPS address
        |
        | HTTPS, distribution-only origin header
        v
origin.<configurable-domain> -> Elastic IP
        |
        v
Nginx on one EC2 instance
        |
        v
FastAPI + one application process
   |        |          |             |
   |        |          |             +-- per-session OpenAI keys in memory
   |        |          +-- hardened native Python subprocesses
   |        +-- encrypted persistent EBS volume
   +-- pinned study archive from private S3

Cognito: identity and invitations
CloudWatch: logs, health, resource, and billing alarms
SSM: administrative access and deployments without inbound SSH
GitHub Actions: tested, versioned application releases through AWS OIDC
```

The application remains a single-machine deployment. CloudFront provides the
stable public entry point, TLS, global edge delivery for static assets, and an
origin access boundary. It does not make the backend highly available or wake
a stopped EC2 instance.

## Domain And TLS Lifecycle

The domain is configuration, not application identity.

The first deployment registers or uses one inexpensive domain and creates a
hidden origin name such as `origin.example.org`. The user-facing address remains
the AWS-provided `https://<distribution>.cloudfront.net` hostname until a
branded address is wanted.

Nginx presents a publicly trusted certificate for the origin name. Certificate
issuance and renewal use a DNS-01 challenge against a narrowly scoped Route 53
hosted zone permission, so no public HTTP or SSH ingress is needed. The EC2
security group permits origin HTTPS only from the AWS-managed CloudFront
origin-facing prefix list. Nginx additionally requires a secret custom header
that only this distribution sends, preventing a different CloudFront
distribution from using the origin.

When the user-facing domain changes:

1. Issue an ACM certificate in `us-east-1` for the new viewer hostname.
2. Add that hostname to the existing CloudFront distribution.
3. Add the new Cognito callback and logout URLs while retaining the old URLs.
4. Update the runtime frontend configuration and allowed-origin policy.
5. Point Route 53 at the same distribution and complete a smoke test.
6. Redirect or retire the old hostname only after active sessions have moved.

The CloudFront distribution, Cognito user pool, EC2 instance, EBS volume,
conversation IDs, and artifacts are unchanged. The origin domain can also be
replaced by issuing a new certificate and changing the origin DNS/configuration;
it does not require a data migration.

## Authentication And Authorization

Cognito User Pools provides managed login. The web client uses OAuth 2.0
authorization code flow with PKCE. The initial callback and logout URLs use the
CloudFront HTTPS hostname. Self-registration is disabled; an administrator
creates each user and Cognito sends the invitation or temporary-password flow.

Every backend API except explicit health and runtime-public-configuration
endpoints requires a valid Cognito access token. Validation checks signature,
issuer, audience/client, token use, and expiration. The stable Cognito `sub`
claim is the application `owner_user_id`; email is display/contact metadata and
never an ownership key.

Authorization is never inferred from a random thread or artifact ID. Every
resource lookup uses the authenticated owner:

```text
authenticated Cognito sub + thread_id -> owned conversation
authenticated Cognito sub + thread_id + artifact_id -> owned artifact
```

A missing resource and a resource owned by someone else return the same
non-disclosing not-found response. Administrative AWS access does not create an
application-level ability to browse other users' conversations through public
APIs.

## OpenAI Bring-Your-Own-Key Contract

After Cognito login, a user enters an OpenAI API key over HTTPS. The backend
validates it without logging the value and stores it only in an in-memory
credential store keyed by authenticated user and application session.

The key must not be placed in:

- browser local storage or IndexedDB;
- a browser URL, analytics event, or error report;
- SQLite, EBS files, S3, Parameter Store, Secrets Manager, or CloudWatch;
- a process-global environment variable; or
- generated Python subprocess environments.

All provider operations use the current user's credential explicitly: primary
LLM calls, title generation, dataset naming, attachment vision, embeddings, and
DB-RAG operations. A process-wide `OPENAI_API_KEY` is forbidden for the hosted
runtime.

Logout, session expiry, deployment, FastAPI restart, EC2 reboot, and EC2 stop
remove the key. Conversations remain visible after a new login, but continuing
provider work requires entering the key again. Invalid or revoked keys fail only
the owning user's request and never affect other sessions.

The first release runs one FastAPI/Uvicorn application process because the
credential store and background-job registry are in memory. Multiple Uvicorn
workers are not enabled until those stores move behind shared interfaces.

## Persistent Storage And Ownership

EC2 has a replaceable root gp3 volume for the operating system and application
cache plus a separately managed encrypted gp3 data volume mounted at
`/srv/epi-agent`. The data volume is not deleted with the instance.

```text
/srv/epi-agent/
├── runtime/
│   ├── agent_memory_fastapi.db
│   ├── users/
│   │   └── <hashed-cognito-sub>/
│   │       └── threads/
│   │           └── <thread-id>/
│   │               ├── attachments/
│   │               ├── datasets/
│   │               ├── figures/
│   │               ├── tables/
│   │               ├── exports/
│   │               └── execution/
│   └── cache/
└── study_data/
    └── studies/
        └── <study-id>/
            └── <package-version>/
```

The raw Cognito `sub` is not exposed in a filesystem path. A deterministic,
collision-resistant encoded or hashed value maps the owner to the directory.
The mapping is controlled by the application; callers never submit paths.

`agent_memory_fastapi.db` remains the first-release checkpoint and conversation
registry. `conversation_history` gains `owner_user_id`, and listing, opening,
renaming, archiving, restoring, deleting, resuming, and exporting all require
the owner match. The LangGraph checkpoint tables can remain keyed by globally
unique thread IDs only after the application verifies ownership before every
checkpointer access.

SQLite uses WAL mode, a busy timeout, explicit schema migrations, and bounded
write transactions. It is suitable only for the approved small, low-concurrency
demo. The storage interface must not leak SQLite-specific behavior into API
contracts so a later PostgreSQL implementation can replace it.

The shared study package is never copied into every user or conversation.
Derived subsets and generated artifacts are private copies under the owning
user/thread directory.

## Conversation And Checkpoint Recovery

The existing local recovery semantics are retained on AWS:

- conversation metadata lists saved threads after application restarts;
- opening a thread loads its latest LangGraph checkpoint;
- a valid human-review interrupt remains paused and renders the same review;
- completed messages and committed artifact references remain available; and
- a non-blocked incomplete snapshot may resume from its last checkpoint when
  the authenticated user reopens it and supplies a current OpenAI key.

In-memory job status, OS threads, LLM network calls, and Python subprocesses do
not survive a process or instance restart. Recovery is checkpoint-based, not
instruction-level continuation. A node interrupted before its result was
committed may run again. Artifact writes used by resumable nodes must therefore
be atomic and idempotent or detect an existing committed identity.

Pending human review must survive logout, deployment, reboot, and EC2 stop.
OpenAI credentials and active processes must not.

Local developer history is not uploaded by default. If a later one-time import
is requested, imported threads must be explicitly assigned to one Cognito owner
and validated before appearing in AWS.

## Synthetic Study Package Installation And Versioning

Each immutable study archive is uploaded to a private S3 path such as:

```text
s3://<study-bucket>/report-india-synthetic/0.3.0/
  report-india-synthetic-0.3.0.tar.gz
```

Deployment configuration pins:

- study ID;
- exact package version;
- archive object key;
- archive SHA-256; and
- installed study root.

The EC2 instance role can read only the required release and application
artifact prefixes. Startup mounts EBS, downloads a missing pinned archive,
verifies its outer checksum and internal manifest, validates DuckDB, catalogs,
indexes, knowledge, and study design, installs through a staging directory, and
atomically activates it. FastAPI does not start if the requested package cannot
be installed or validated.

An already installed exact version is validated and reused without another
download. A new version is installed alongside old versions and becomes the
default for new conversations only after validation.

Every conversation records its study ID and exact package version. Existing
conversations stay pinned to the version with which they were created. The
active version, the immediately previous version, and every version referenced
by a retained conversation remain installed. The first release performs no
automatic package deletion. S3 remains the durable source for reinstall and
rollback.

Application consumers treat package contents as read-only. Any library cache or
coordination files that must be writable belong under `runtime/cache`, not in
the versioned package directory.

## Native Python Execution Safety Boundary

The existing AST policy, sanitized environment, subprocess timeout, memory
limit, process-count limit, output limits, and process-group termination are
useful controls but are not a hostile-code sandbox. Invitation-only access and
synthetic data remain mandatory.

Before internet exposure, native execution also uses a separate unprivileged OS
identity from FastAPI:

- FastAPI cannot pass arbitrary commands to the privilege boundary; it invokes
  one fixed worker wrapper with validated temporary paths.
- The execution user can read only the temporary input directory and write only
  its temporary output directory.
- It cannot read conversation storage, study packages, deployment files,
  application secrets, other users' temporary directories, or FastAPI process
  memory.
- UID-based host firewall rules block all network access, including the EC2
  instance metadata address, from execution workers.
- No AWS credentials or OpenAI keys are inherited.
- Existing resource limits remain enforced, and temporary directories are
  removed after collection.

This is defense in depth for a controlled research demo, not permission to
accept arbitrary hostile programs. A future multi-machine architecture replaces
the native wrapper with isolated, disposable execution workers.

## EC2 Bootstrap And Lifecycle

The instance uses a supported Linux image, Python 3.12, Nginx, SSM Agent,
CloudWatch Agent, and systemd. No inbound SSH port is opened. Administrators use
AWS Systems Manager Session Manager and Run Command.

Bootstrap is idempotent and performs only machine preparation:

1. Install pinned system dependencies.
2. Create separate web and execution OS identities.
3. Attach, format only if blank, and mount the encrypted data EBS volume by
   stable filesystem identity.
4. Create storage directories with restrictive ownership and permissions.
5. Install Nginx, origin certificate renewal, firewall rules, and systemd units.
6. Install the deployment agent/scripts and retrieve the initial application
   release.
7. Validate the pinned study package.
8. Start FastAPI only after all required mounts and validation succeed.

`systemd` orders the application after the data mount and network, restarts it
after ordinary crashes, and reports repeated startup failures to CloudWatch.

Normal lifecycle behavior:

- **Reboot:** EBS remounts and the service starts automatically.
- **Stop:** compute billing stops; the public application is unavailable.
- **Start:** Elastic IP and EBS remain attached; automatic startup takes a few
  minutes and the same CloudFront link becomes healthy.
- **Terminate/replace:** application code and root storage are disposable; the
  separately managed data EBS volume and snapshots remain recoverable.

The operator guide must make `Stop` and `Terminate` visually and operationally
distinct. No running job may be intentionally stopped without accepting
checkpoint-based recovery semantics.

## Application Release And Deployment Flow

AWS configuration is defined as version-controlled CloudFormation and reviewed
through change sets. Deployments use GitHub Actions with AWS OIDC federation;
no long-lived AWS access key is stored in GitHub.

```text
push reviewed commit
  -> Python and frontend tests
  -> build/verify compiled frontend
  -> create immutable application archive
  -> generate checksum and release manifest
  -> upload to private S3 release prefix
  -> manual production-environment approval
  -> SSM deploys exact commit to a new release directory
  -> dependency install and schema migration checks
  -> maintenance/drain gate
  -> switch current symlink and restart systemd service
  -> health, authentication, persistence, and study smoke checks
  -> retain release or roll back symlink
```

EC2 layout:

```text
/opt/epi-agent/releases/<commit-sha>/
/opt/epi-agent/current -> /opt/epi-agent/releases/<commit-sha>/
/etc/epi-agent/app.env
/srv/epi-agent/                       # never replaced by code deployment
```

Pushing GitHub does not directly mutate production. Only a tested immutable
release selected by an approved deployment workflow changes EC2.

Normal deployments enter a drain state: reject new jobs with a maintenance
response, wait for active jobs to finish, and restart only when the active count
is zero. If the wait exceeds the configured workflow deadline, an ordinary
deployment is postponed. An urgent forced deployment requires an explicit
operator override and may cause checkpoint-based re-execution.

The application does not currently provide cancellation of an actively running
job. Existing review-stage Cancel actions end the paused reasoning run only.
True active-run cancellation is a separate feature and is not claimed by the
deployment interface.

Database migrations are forward-checked before activation and must preserve a
rollback-compatible path for the immediately previous application release. A
failed health check returns the symlink to the prior release and restarts it.
Persistent data is never deleted during automated rollback.

## Recovery, Backups, And Observability

The first release is recoverable but not highly available. A single EC2 or
Availability Zone failure causes downtime until restart or recovery.

Recovery controls:

- encrypted gp3 data volume separated from the instance root;
- daily incremental EBS snapshots retained for 14 days;
- application-aware SQLite backup/checkpoint handling before planned snapshots
  or migrations;
- S3 versioning for application and study release buckets;
- retained previous application and study versions;
- CloudFormation source for infrastructure recreation; and
- a documented restore drill into a non-production path.

Monitoring and alarms cover:

- `/health` and study readiness;
- EC2 status checks and systemd failure;
- CPU, T3 credit balance, and surplus-credit charges;
- memory, data-volume space, and inode exhaustion;
- application error rate and repeated provider failures without secret values;
- certificate expiry/renewal failure;
- snapshot failure;
- CloudFront origin errors; and
- estimated AWS spend thresholds.

Logs use bounded retention and redact authorization headers, cookies, OpenAI
keys, prompts designated sensitive, attachment contents, and generated dataset
rows. User-facing errors contain stable codes and remediation, while detailed
server logs retain only safe identifiers such as hashed owner and thread ID.

## Failure Behavior

- Missing or invalid Cognito token: reject before any ownership lookup.
- Cross-owner thread or artifact request: return non-disclosing not found.
- Missing/revoked OpenAI key: affect only that user and prompt for re-entry.
- Study download or validation failure: do not start the application with a
  different or partial package.
- EBS mount failure: do not create a replacement empty database on the root
  volume; fail startup and alarm.
- SQLite lock pressure: return a retryable busy response, log the metric, and do
  not silently drop writes.
- Python timeout/resource breach: terminate the worker process group, preserve
  the conversation, and report a bounded recoverable error.
- Deployment health failure: roll back code only; preserve database and files.
- EC2 stop/restart during work: discard in-memory keys/jobs and recover only
  from committed checkpoints.
- Certificate renewal failure: alarm before expiration; never downgrade the
  origin to HTTP.

## Cost Boundary

The baseline estimate assumes `us-east-1`, one on-demand `t3.large` running 730
hours, approximately 80 GB of gp3 storage across root and data volumes, one
public IPv4 address, incremental snapshots, small S3 release/package storage,
CloudFront, Cognito, and low-volume CloudWatch.

Expected 24/7 testing cost is approximately USD 75–85 per month before tax and
without promotional credits. An inexpensive domain is approximately USD 10–20
per year plus hosted-zone cost. OpenAI model usage is billed to each user's own
OpenAI account.

When EC2 is stopped, EBS, snapshots, S3, hosted zone, and public IPv4 continue
to cost approximately USD 11–15 per month. T3 Unlimited surplus CPU, excessive
logs, data transfer, or unusually large artifacts can raise the bill. Billing
and resource alarms are required before public invitation.

The design intentionally excludes an always-on load balancer, NAT Gateway,
RDS, and duplicate EC2 capacity from the initial cost.

## Scale-Ready Boundaries

The first deployment must use explicit interfaces for:

- conversation/checkpoint persistence;
- artifact storage and retrieval;
- study package acquisition/registry;
- user credential lookup;
- background job submission/status; and
- Python execution.

The migration path is:

1. Replace SQLite with PostgreSQL/RDS.
2. Replace local artifact storage with private S3 object storage.
3. Move ephemeral shared job/session state to Redis or a durable queue.
4. Run multiple stateless FastAPI instances behind a load balancer and Auto
   Scaling Group.
5. Replace local execution with isolated workers consuming queued jobs.

Ownership IDs, thread IDs, artifact IDs, study version pinning, and API
authorization semantics remain unchanged across these steps. The first release
does not pay for or partially operate those future services.

## Infrastructure And Application Verification

Focused automated tests must cover:

- Cognito JWT validation and all failure modes;
- ownership isolation for conversation and artifact operations;
- owner-safe filesystem mapping and path traversal rejection;
- per-session OpenAI key lifecycle, concurrency, logout, and restart behavior;
- provider calls never falling back to a global key;
- SQLite migration, WAL/busy behavior, and checkpoint reopening;
- human-review persistence across a backend restart;
- incomplete checkpoint recovery without duplicate committed artifacts;
- study S3 download, checksum validation, side-by-side version activation, and
  conversation version pinning;
- generated-code environment, permissions, network denial, limits, and cleanup;
- drain, deploy, health failure, and rollback behavior; and
- CloudFormation static validation and least-privilege policy assertions.

Per repository policy, each user-visible feature receives a dedicated real
smoke script. Hosted-deployment smoke coverage includes:

1. Invite and authenticate two Cognito test users.
2. Enter separate OpenAI keys without exposing them in browser/storage/logs.
3. Create separate conversations and artifacts.
4. Prove both API and guessed-ID cross-user access are denied.
5. Pause one conversation at human review.
6. Restart FastAPI and reopen the same review.
7. Deploy a new application release and prove persistence.
8. Install a new study version, prove new-thread activation and old-thread
   version pinning.
9. Stop/start EC2 in an operator smoke and verify automatic recovery and the
   unchanged CloudFront address.

Live provider and AWS smokes run once with bounded time and preserve sanitized
logs and artifacts on failure. They do not expose credentials or rerun paid
operations automatically.

## Implementation Sequence

The work is deliberately split into three reviewable implementation projects.
Each receives its own detailed plan, tests, smoke, review, and commit series:

1. **Multi-user application foundation:** Cognito verification boundary,
   conversation/artifact ownership, owner-safe paths, and per-session OpenAI
   credentials.
2. **Hosted runtime foundation:** study-version pinning and S3 acquisition,
   persistent-storage migrations, native execution isolation, drain/recovery,
   and hosted readiness diagnostics.
3. **AWS delivery:** CloudFormation, EC2 bootstrap, TLS/origin controls,
   backups/monitoring/budgets, GitHub OIDC deployment, rollback, and hosted
   acceptance smokes.

The projects are sequential. Chargeable AWS application resources are not
created until the first two projects pass locally. A minimal read-only AWS
preflight may run earlier to confirm account identity, Region, quotas, and
domain availability.

Across those projects, the end-to-end sequence is:

1. Add user ownership and authorization boundaries locally with test Cognito
   tokens or a test verifier interface.
2. Refactor every OpenAI consumer to explicit per-session credentials and
   remove hosted global-key startup assumptions.
3. Move artifacts to the approved owner/thread layout and migrate storage
   metadata safely.
4. Add study-version pinning to conversations and S3 acquisition around the
   existing validated installer.
5. Harden the native execution worker with OS identity, filesystem, environment,
   metadata, and network isolation.
6. Add drain/readiness, startup recovery, health diagnostics, and redaction.
7. Create CloudFormation, EC2 bootstrap, certificate, systemd, backup, and
   monitoring definitions.
8. Create immutable GitHub Actions/S3/SSM deployment and rollback tooling.
9. Run local focused tests and dedicated feature smokes.
10. Perform read-only AWS account/region/quota checks.
11. Review a CloudFormation change set and monthly budget before resource
    creation.
12. Create the test stack, upload the pinned study and application releases,
    and complete hosted security/persistence/start-stop smokes.
13. Invite the initial research users only after the hosted acceptance checks
    pass.

AWS resource creation is never triggered merely by a Git push or local test.
The first chargeable change set requires explicit user approval after its
resource list and cost boundary are shown.
