# AWS Phase 2A Infrastructure and Manual Delivery Design

Date: 2026-08-07
Status: Approved in conversation; awaiting written-spec review

## Objective

Provision and validate the first live AWS environment for Epi Agent in AWS
account `641379499556`, Region `us-east-1`. The application will be available
at `https://epiagent.org` to invited researchers worldwide while its single EC2
instance is running.

This phase deploys the existing Cognito/BYOK, owner-isolated application without
Docker. It deliberately establishes a transparent manual deployment and
recovery path through AWS Systems Manager before Phase 2B automates the same
release process with GitHub Actions.

This document supersedes the CloudFront entry-point assumptions in
`2026-08-06-aws-invitation-demo-design.md`. The application ownership, BYOK,
checkpoint, study-package, and native-execution boundaries in that document
remain applicable.

## Approved Constraints

- The AWS CLI must explicitly use profile `xutao-dev` for deployment commands.
- The initial audience is 5–20 invited researchers with low concurrency.
- Only synthetic or fully de-identified research data is permitted.
- The first release uses one EC2 instance and one FastAPI/Uvicorn worker.
- Native Python execution remains the launch runtime; Docker is not required.
- Users provide their own OpenAI API keys, which remain server-side in memory
  only.
- Cognito self-registration is disabled; administrators invite users.
- `epiagent.org` is already registered in Route 53 and its existing public
  hosted zone is reused.
- CloudFront, an Application Load Balancer, NAT Gateway, RDS, Redis, ECS, EKS,
  and multi-instance high availability are excluded.
- Stopping EC2 is allowed during private testing. The site is unavailable while
  stopped, but persistent storage and the public address remain available for
  a normal restart.

## Delivery Approach

Three approaches were considered:

1. Provision the single-instance foundation, deploy manually through Systems
   Manager, validate it, and then automate the verified process.
2. Build CloudFormation and GitHub Actions together before the first live test.
3. Move directly to a managed container platform such as ECS or App Runner.

Approach 1 is selected. It has the fewest interacting failure modes for a first
AWS launch and leaves a known-good administrative path if later automation
fails. Approach 2 is Phase 2B. Approach 3 waits until the Docker execution
boundary is integrated and tested.

## Architecture

```text
Invited users worldwide
          |
          | HTTPS
          v
    epiagent.org
    Route 53 A record
          |
          v
      Elastic IP
          |
          v
Nginx on one EC2 t3.large
          |
          v
FastAPI + compiled React, one worker
   |          |              |
   |          |              +-- per-session OpenAI keys in memory
   |          +-- restricted native Python subprocesses
   +-- encrypted persistent gp3 EBS data volume

Private S3 bucket: immutable application releases and study archives
Cognito user pool: invitation-only identity and managed login
CloudWatch: logs, instance/application metrics, and alarms
Systems Manager: administration and deployment without inbound SSH
CloudFormation: version-controlled AWS resource definitions and updates
```

The single EC2 instance is a deliberate cost boundary, not a high-availability
claim. Route 53 and the Elastic IP keep the name and address stable across an
EC2 stop/start. They do not wake the instance or serve the application while it
is stopped.

## CloudFormation Boundary

CloudFormation is the infrastructure blueprint, not a runtime service. It
creates and updates the VPC, subnet, internet gateway, routes, security group,
EC2 instance, Elastic IP, EBS volume, S3 bucket, Cognito resources, IAM roles,
DNS record, snapshot policy, logs, metrics, and alarms.

The registered domain and existing hosted zone are not created, imported, or
deleted by the stack. The hosted-zone ID and domain name enter the template as
validated parameters, and the stack manages only the application DNS record.

Stateful resources use both `DeletionPolicy: Retain` and
`UpdateReplacePolicy: Retain` where CloudFormation supports them:

- the data EBS volume;
- the versioned release/study S3 bucket; and
- the Cognito user pool whose stable subjects own application data.

The replaceable root volume, EC2 instance, DNS record, and Elastic IP are not
treated as durable application data. A stack deletion requires an explicit
operator confirmation and leaves retained resources that must be inventoried
and managed rather than silently forgotten.

Before every create or update, the operator validates the template and reviews
a CloudFormation change set. Infrastructure changes are never applied from an
unreviewed local file.

### One-Time IAM Bootstrap

The current `xutao-dev` identity receives `PowerUserAccess` and
`IAMReadOnlyAccess` through the `Developer` group. That is sufficient for most
application resources but intentionally does not allow it to create or pass the
IAM roles required by the stack.

Before the first stack deployment, an administrator performs one narrowly
scoped bootstrap:

1. Create an `EpiAgentCloudFormationExecutionRole` trusted only by
   CloudFormation, with permissions limited to the resource types and naming
   prefixes defined by this design.
2. Grant the `Developer` group `iam:PassRole` for exactly that execution-role
   ARN and no other role.
3. Record the execution-role ARN as deployment configuration.
4. Require every stack create, update, and change set to use that service role.

The bootstrap may be delivered as a separately reviewed administrator template
or exact Console procedure. The root account is not used for routine stack or
application deployment after this step. The implementation plan must include a
read-only permissions simulation or dry run before creating billable resources.

## Network and Public HTTPS

The stack creates one small VPC and one public subnet in one Availability Zone.
The EC2 instance has an Elastic IP and a default route through an internet
gateway. No NAT Gateway is required.

The instance security group permits:

- TCP 443 from the internet for the application;
- TCP 80 from the internet only for ACME certificate validation and HTTP-to-
  HTTPS redirection; and
- required outbound HTTPS and DNS for package installation, OpenAI, S3,
  Cognito/JWKS, Systems Manager, CloudWatch, and certificate renewal.

Port 22 is not opened. Administrative access uses Systems Manager Session
Manager and Run Command. EC2 Instance Metadata Service v2 is required with a
hop limit that does not expose credentials to nested execution processes.

Route 53 creates a standard A record from `epiagent.org` to the Elastic IP.
Nginx obtains a publicly trusted Let's Encrypt certificate with HTTP-01
validation, redirects ordinary port-80 requests to HTTPS, and renews the
certificate automatically. This avoids both an always-on load balancer and EC2
permission to alter the hosted zone. A renewal dry run is part of launch
acceptance.

The Cognito user pool initially uses an AWS-provided managed-login domain. Its
callback and post-logout URLs are HTTPS URLs under `epiagent.org`. A later
custom Cognito domain or replacement application domain changes DNS,
certificate, callback, logout, and allowed-origin configuration; it does not
require moving conversations or artifacts.

## Compute and Machine Bootstrap

The instance type is a CloudFormation parameter with `t3.large` as the initial
default: 2 vCPUs and 8 GiB of memory. T3 credit mode is `standard` to bound
costs. This size is intended for controlled testing and one or two concurrent
analysis jobs, not unbounded simultaneous execution.

CloudWatch alarms and launch observations determine whether to resize. A
resize updates the instance-type parameter and performs an expected stop/start;
the Elastic IP and retained data volume remain unchanged. A `t3.xlarge` is the
first simple vertical upgrade if CPU, memory, or concurrency requires it.

The operating system is Amazon Linux 2023 on x86-64, resolved from the current
AWS-managed AMI parameter rather than a hard-coded AMI ID. Bootstrap is
idempotent and installs or configures:

- `uv` and a pinned Python 3.12 runtime;
- Nginx and Certbot;
- Systems Manager and CloudWatch agents;
- dedicated `epi-agent-web` and `epi-agent-exec` OS identities;
- the data-volume mount and required directory permissions;
- systemd mount, application, certificate-renewal, and monitoring units; and
- version-controlled deployment and health-check scripts.

User data prepares the machine but contains no provider key, session token,
private release credential, or user data. Bootstrap may safely run again. It
formats the data volume only when it is demonstrably blank, records its
filesystem UUID, mounts by UUID, and refuses to start FastAPI when the required
mount is absent.

Systemd starts the application only after networking and the persistent mount
are ready. It restarts ordinary application crashes and exposes repeated
failure through CloudWatch.

## Storage Layout and Ownership

The initial EBS allocation is:

- 30 GiB encrypted gp3 root volume for the operating system, dependencies, and
  replaceable application releases; and
- 50 GiB encrypted gp3 data volume for durable application state.

The 50 GiB volume is shared capacity, not 50 GiB per user. It can be expanded
without changing application paths; reducing it is not an ordinary operation.
CloudWatch alarms fire before free space or inodes become critical.

```text
/srv/epi-agent/
├── runtime/
│   ├── agent_memory_fastapi.db
│   ├── users/
│   │   ├── <hashed-cognito-sub-A>/threads/<thread-id>/...
│   │   └── <hashed-cognito-sub-B>/threads/<thread-id>/...
│   └── cache/
└── study_data/studies/<study-id>/<version>/
```

All users physically share the volume and SQLite database, but every
conversation row and artifact path is selected through the authenticated
Cognito owner. Users never receive filesystem access or supply storage paths.
The shared synthetic study package is installed once as read-only input;
derived datasets and artifacts live under the owning user and thread.

The private, encrypted, versioned S3 bucket uses separate prefixes:

```text
s3://<generated-bucket>/releases/<git-commit>/...
s3://<generated-bucket>/studies/<study-id>/<version>/...
```

The EC2 role can read only the required prefixes and publish operational logs.
It cannot make objects public. The initial operator can upload immutable
releases and study archives. Phase 2B gives GitHub OIDC a separate role limited
to release publication and deployment initiation.

## Authentication and Request Data Flow

1. A browser requests `https://epiagent.org` through Nginx.
2. FastAPI serves the compiled React application and handles `/api` requests.
3. The browser redirects to Cognito managed login using authorization code flow
   with PKCE.
4. Cognito returns the browser to `https://epiagent.org/auth/callback`.
5. API requests carry a Cognito access token that FastAPI validates.
6. The stable Cognito `sub` becomes the server-derived owner identity.
7. The authenticated user enters an OpenAI key over HTTPS.
8. The server validates and holds the key in memory for that user/session only.
9. Owner-scoped checkpoints and artifacts are written to the data EBS volume.

Cognito allows administrator-created users only. Public self-registration is
disabled. The first launch uses the Cognito Essentials tier and basic managed
login features; SMS and machine-to-machine flows are not configured.

OpenAI keys are never stored in browser persistent storage, SQLite, EBS, S3,
environment files, Parameter Store, Secrets Manager, logs, metrics, or generated
Python environments. Logout, token/session expiry, application restart,
deployment, reboot, and EC2 stop clear them.

## Native Python Security Boundary

Generated Python remains a controlled-demo feature rather than a hostile-code
sandbox. Invitation-only access and synthetic or fully de-identified data are
mandatory.

FastAPI invokes a fixed wrapper under the unprivileged `epi-agent-exec` user.
That user receives a single temporary input/output scope, inherits no OpenAI or
AWS credential, cannot read application configuration or another user's
storage, and is blocked from outbound networking, including EC2 metadata. The
existing AST policy, sanitized environment, time, memory, process-count,
output-size, and process-group termination controls remain enforced.

These controls prepare a later migration to disposable Docker or remote worker
execution. They do not claim that arbitrary adversarial code is safe to run on
the web instance.

## Manual Application Release Flow

Phase 2A uses immutable release archives and Systems Manager:

```text
tested aws-test commit
  -> build compiled frontend
  -> create application archive, checksum, and manifest
  -> upload to private S3 releases/<commit>/
  -> invoke a version-controlled SSM deployment document
  -> download and verify exact release
  -> install dependencies into a new release directory
  -> run configuration and migration prechecks
  -> switch current symlink
  -> restart systemd service
  -> run health/authentication/persistence smoke checks
  -> retain new release or roll back symlink
```

EC2 uses:

```text
/opt/epi-agent/releases/<git-commit>/
/opt/epi-agent/current -> /opt/epi-agent/releases/<git-commit>/
/etc/epi-agent/app.env
/srv/epi-agent/                         # never replaced by code deployment
```

The environment file contains non-secret deployment configuration and is
readable only by the application administrator and service identity. It never
contains a shared OpenAI key.

An ordinary deployment stops accepting new jobs and waits for active work to
finish. If the bounded drain deadline expires, the deployment is postponed.
An explicitly forced deployment may interrupt active work, which can recover
only from its last committed checkpoint. A failed health check restores the
previous symlink and restarts the previous release. Automated rollback never
deletes persistent data or runs a reverse data migration.

## Stop, Start, and Failure Semantics

- **Application restart:** systemd restarts FastAPI; stored conversations and
  reviews remain; in-memory keys and active jobs disappear.
- **EC2 reboot:** the data volume remounts by UUID and systemd starts the same
  selected release after storage and network readiness.
- **EC2 stop:** compute charges stop; the public site is unavailable; EBS,
  snapshots, S3, Route 53, and the Elastic IP continue to exist and incur their
  respective charges.
- **EC2 start:** the Elastic IP remains stable, services start automatically,
  and health checks become ready after bootstrap/startup completes.
- **EC2 replacement:** the root disk and cached release installation are
  replaceable; the retained data volume is reattached only through an explicit,
  verified recovery operation.
- **Availability Zone failure:** the service remains unavailable until the
  single-machine environment is recovered; Phase 2A does not claim automatic
  failover.

Checkpoint recovery is not instruction-level continuation. An interrupted LLM
call or Python process may rerun from its last committed graph checkpoint.
Pending human review and committed artifact references survive; operating
system threads and subprocesses do not.

## Backups, Monitoring, and Cost Controls

The data volume is tagged for a daily Data Lifecycle Manager snapshot policy
that retains 14 recovery points. Planned database-sensitive operations run a
SQLite-aware checkpoint/backup step first. S3 versioning protects published
archives. A restore drill attaches a snapshot-created volume to a separate test
path and never overwrites production storage.

CloudWatch collects or alarms on:

- EC2 status-check failures;
- CPU utilization and T3 credit balance;
- memory utilization;
- root/data disk capacity and inode availability;
- systemd application failure and repeated restart;
- Nginx and application error rates without secret-bearing request data;
- HTTPS certificate age and renewal failure; and
- snapshot-policy failure.

Log groups use explicit retention, initially 30 days. Authorization headers,
cookies, Cognito tokens, OpenAI keys, and request bodies that could contain keys
are never logged. The already configured AWS Budget remains the billing alarm;
the stack does not silently create a duplicate budget.

At current `us-east-1` rates, the approved planning estimate is:

| Resource group | Running 730 hours/month | EC2 stopped |
| --- | ---: | ---: |
| `t3.large` compute | about USD 61 | USD 0 |
| 80 GiB gp3 EBS | about USD 6.40 | about USD 6.40 |
| one public IPv4 address | about USD 3.65 | about USD 3.65 |
| Route 53 hosted zone | USD 0.50 | USD 0.50 |
| snapshots, S3, and monitoring | about USD 3–13 | about USD 2–6 |
| **Estimated AWS total** | **about USD 75–85** | **about USD 12–17** |

The registered domain is approximately USD 16/year separately. Cognito is
expected to remain within the applicable free tier at 5–20 directly signing-in
users. OpenAI usage is billed to each user's provider account. Data transfer,
large logs or artifacts, snapshot growth, and sustained CPU pressure can raise
the estimate.

## Verification and Launch Acceptance

Infrastructure validation must prove:

- CloudFormation syntax/linting and change-set review pass;
- all deployment commands explicitly target profile `xutao-dev`, account
  `641379499556`, and Region `us-east-1`;
- S3 public access is blocked and encryption/versioning are enabled;
- root and data EBS encryption are enabled;
- durable resources have the intended retention policies;
- no security group exposes SSH;
- IMDSv2 is required;
- the instance appears online in Systems Manager; and
- CloudWatch metrics, logs, alarms, and snapshot policy operate.

Application acceptance must prove:

- `https://epiagent.org` serves a valid certificate and redirects HTTP;
- certificate renewal dry-run succeeds;
- Cognito public self-registration is unavailable;
- an invited user completes login and logout;
- a user can enter a valid OpenAI key and perform an authenticated operation;
- two real Cognito users cannot read, modify, enumerate, or delete one another's
  conversations or artifacts;
- OpenAI key material does not appear in SQLite, the runtime tree, release
  files, captured HTTP responses, browser persistent storage, or logs;
- conversations, artifacts, and a pending human-review interrupt survive an
  application restart and EC2 reboot;
- EC2 stop/start preserves data and returns the same public URL;
- a new immutable release can deploy and roll back without changing persistent
  data;
- a snapshot restores successfully at a separate test mount; and
- local native startup and its focused regression suite still work without
  Docker or AWS configuration.

The environment is not opened to additional invited users until all launch
acceptance checks pass.

## Phase 2B and Scale-Out Compatibility

Phase 2B adds GitHub Actions with AWS OIDC to build, publish, approve, and invoke
the same immutable S3/SSM deployment flow. It stores no long-lived AWS access
key in GitHub and retains the manual Systems Manager recovery path.

The later scale-out path remains:

1. replace SQLite with PostgreSQL/RDS;
2. replace user artifact files with private S3 object storage;
3. move in-memory credential/job coordination behind shared interfaces;
4. add a queue and isolated execution workers, including the completed Docker
   sandbox when it is ready; and
5. run replaceable web nodes behind a load balancer and Auto Scaling Group.

The current Cognito owner, thread and artifact identifiers, provider-key
contract, study-version pinning, application release format, and authorization
semantics remain stable across that migration.

## Explicit Non-Goals

- Anonymous access or public signup
- Identifiable, protected, or regulated research data
- Highly available or zero-downtime service
- Multiple application instances or workers
- Automatic wake-on-request behavior
- Hostile-code sandbox guarantees
- Server-funded OpenAI usage or Amazon Bedrock migration
- Automatic GitHub deployment in Phase 2A
- Automatic migration of local conversations into AWS
- Destructive stack teardown or production restore automation
