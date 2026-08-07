# Task 7 — EC2 bootstrap, SSM deployment, snapshots, and observability

## Scope

Implemented Task 7 in the isolated `aws-phase2a-task0` worktree. No AWS API,
Docker, or other external infrastructure calls were made.

## RED / GREEN evidence

### RED

Added five contract tests for hardened compute, least-privilege instance IAM,
guarded UserData, the SSM command document, and backup/observability.

Command: `.venv/bin/python -m pytest tests/test_aws_infrastructure.py -q`

Before implementation: `5 failed, 15 passed`. The expected failures were
missing-resource `KeyError`s for `ApplicationInstance`,
`ApplicationInstanceRole`, `EpiAgentDeployReleaseDocument`, and the DLM policy.

### GREEN

Commands: `uvx --from cfn-lint==1.53.1 cfn-lint infra/aws/phase2a/template.yaml`,
`.venv/bin/python -m pytest tests/test_aws_infrastructure.py tests/test_aws_host_assets.py -q`,
and `git diff --check`.

- Pinned `cfn-lint==1.53.1`: exit 0, no warnings or errors.
- Infrastructure and host-asset tests: `30 passed in 2.45s`.
- Whitespace check: exit 0, no output.

The first sandboxed linter attempt was blocked only because `uvx` could not
read its existing user cache. The required command was then approved and run
unchanged; it did not use AWS credentials or create resources.

## Changed files

- `infra/aws/phase2a/template.yaml`
  - Adds an optional blank-by-default `AlertEmail` and conditional email
    subscription.
  - Adds an AL2023 public-SSM-AMI `t3.large` host with standard CPU credits,
    30 GiB encrypted gp3 root, IMDSv2, termination protection, no SSH key,
    separate retained-volume attachment, and EIP association.
  - Adds a bounded instance profile: SSM core plus artifact reads, agent
    parameter reads, host-log publishing, CWAgent metrics, and unavoidable
    `DescribeVolumes` only.
  - Adds idempotent guarded UserData. It identifies the exact stack-owned EBS
    volume by its by-id path, formats only that resolved device when `blkid`
    finds no filesystem, mounts by UUID, creates restrictive directories and
    users, writes no secret, configures the agent, and leaves the application
    stopped until SSM deployment.
  - Adds `epi-agent-deploy-release` (SSM schema 2.2, `aws:runShellScript`,
    `ENV_VAR` interpolation, anchored parameter validation), with safe,
    checksummed staging extraction and root-owned host asset installation.
  - Adds 14 retained daily DLM snapshots, 30-day logs, memory/disk/inode
    metrics, status/CPU/credit/memory/data-disk/service-error alarms, metric
    filter, and SNS.
- `deploy/aws/systemd/epi-agent.service`
  - Adds explicit application log destinations for the CloudWatch agent and
    service-error metric filter without widening service privileges.
- `tests/test_aws_infrastructure.py`
  - Adds Task 7 contracts and updates the optional-alert parameter contract.
- `tests/test_aws_host_assets.py`
  - Covers application service log destinations.

## Self-review

### IAM

- S3 is read-only and limited to `releases/*` and `studies/*`; the instance
  role has no S3 write, Route 53, Cognito, or IAM actions.
- `cloudwatch:PutMetricData` must use `*`, so it is constrained to namespace
  `CWAgent`. `ec2:DescribeVolumes` is the sole non-resource-scopable device
  identification exception.
- New roles use the bootstrap-created workload permissions boundary.

### Shell and deployment safety

- SSM inputs are anchored-pattern validated and consumed only as quoted
  `SSM_*` environment variables—never `eval` or executable shell source.
- The only new `rm -rf` removes a generated, quoted SSM staging directory.
- The only new format command is guarded by the expected CloudFormation EBS ID,
  the derived by-id path, block-device verification, and absent `blkid` data.
- UserData emits non-secret Cognito/domain configuration and explicitly keeps
  `epi-agent.service` disabled and stopped before SSM installs a release.

### Replacement, recovery, and cost

- Data-volume retain policies remain in force; the root mapping is the only
  mapping with `DeleteOnTermination`.
- No `CreationPolicy` wait is used. Such a wait would deadlock because UserData
  waits for the retained-volume attachment that CloudFormation creates only
  after the instance reaches CREATE_COMPLETE. UserData still sends success and
  failure `cfn-signal` calls for bootstrap observability.
- Cost controls are one fixed `t3.large` with standard credits, one EIP, no
  NAT/managed compute, 30-day logs, and exactly 14 daily snapshots.

## Remaining concerns

- Static validation cannot confirm AL2023 package availability (notably `uv`),
  region-specific NVMe by-id naming, actual SSM execution, or deployed metric
  dimensions; validate these in a controlled account.
- Email subscriptions require recipient confirmation. Empty `AlertEmail`
  deliberately creates no email subscription.
- The older `InstanceType` and `RootVolumeGiB` parameters remain for existing
  parameter/output compatibility, while deployment is intentionally fixed to
  the required `t3.large` and 30 GiB.
- The prerequisite bootstrap stack must have created
  `epi-agent-workload-boundary`, referenced by both new roles.

## Reviewer remediation

Follow-up review found deployment blockers and this revision addresses them:

- The mandatory workload boundary now permits the constrained SSM managed-node
  connection actions, CWAgent metric publication, and DLM snapshot actions;
  the execution policy now has the exact instance-profile, SSM document,
  metric-filter, and instance-attribute lifecycle actions required by Task 7.
- AL2023 bootstrap installs and verifies Python 3.12 before SSM uses it, and
  installs pinned `uv==0.6.14` via that interpreter.
- First release activation skips the drain endpoint when no current release
  exists. A generated HTTP-only ACME bootstrap server is active before Certbot;
  the SSM document renders the domain into the TLS configuration before the
  installer reloads Nginx after issuance.
- CWAgent uses `drop_device: true` to match the data-disk alarm dimensions.
  The log-stream ARN is explicit, SSM document updates use `NewVersion`, and
  the stack outputs the instance ID and deployment-document name.

Revalidation after remediation: both bootstrap and Phase 2A templates pass
`cfn-lint==1.53.1`; infrastructure and host-asset tests report `31 passed`.

Final TLS/SSM re-review remediation: the SSM document stages the rendered TLS
Nginx file outside `conf.d`; the installer first completes Certbot using the
live HTTP ACME bootstrap, then moves the staged file into the live include,
removes the bootstrap file, validates Nginx, and reloads. A failed Certbot
therefore leaves the known-valid bootstrap configuration in place for retries.
The boundary now also includes the complete managed-instance SSM and
ec2messages action set, and host-log permissions split log-group describe from
log-stream creation/writes. Final validation: both templates lint clean and
the focused suite reports `31 passed`.
