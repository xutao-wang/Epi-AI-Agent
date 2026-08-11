# AWS Study Access Recovery Design

## Goal

Restore the existing EBS-backed study to the `epi-agent-web` service identity
before attempting another application release deployment. This removes the
deployment deadlock in which the release installer requires a healthy current
application, while the current application cannot start because its retained
study registry is owned by `root:root` with mode `0600`.

This design supersedes only the deployment order in the approved AWS study
installer ownership design. The reviewed `install-study.sh` privilege-drop
implementation remains unchanged.

## Confirmed Failure Sequence

The existing release SSM document first copies host scripts from the verified
release archive and then invokes `/usr/local/sbin/install-release.sh`.
`install-release.sh` sees an existing `/opt/epi-agent/current` symlink and waits
for the current application to drain. The current service cannot become healthy
because `epi-agent-web` cannot read the retained registry. Consequently, a
normal release command can fail before activating the new release and before a
separate corrected study-install command is allowed to run.

Deploying first and repairing second is therefore not a valid recovery order.

## Selected Approach

Add one dedicated, parameter-free SSM Command document named
`epi-agent-recover-study-access`. It performs a fixed recovery operation and
does not download, install, or activate an application release.

The document runs as the SSM root execution identity and:

1. requires the fixed retained root `/srv/epi-agent/study_data` and current
   release Python interpreter to exist;
2. applies exactly
   `chown -R -h epi-agent-web:epi-agent-web /srv/epi-agent/study_data`;
3. runs a fixed validation program as `epi-agent-web` through
   `/usr/sbin/runuser --user epi-agent-web -- /usr/bin/env -i`;
4. validates that the registry is readable and writable, has at least one
   active study, each active package manifest matches its study ID and version,
   and each declared database index directory is readable, writable, and
   searchable by the service identity;
5. restarts the existing `epi-agent.service` once; and
6. waits for local health and readiness to return HTTP 200, failing if the
   service exits or the fixed deadline expires.

The document has no caller-controlled parameters, shell fragments, paths,
users, commands, or environment variables. It does not delete, replace, or
reinstall study data.

## Infrastructure and Operator Interfaces

`infra/aws/phase2a/template.yaml` adds:

- `EpiAgentRecoverStudyAccessDocument`, an `AWS::SSM::Document` with
  `UpdateMethod: NewVersion`; and
- `RecoverStudyAccessDocumentName`, a stack output consumed by the operator
  CLI.

The document name remains under the existing `epi-agent-*` IAM scope. No IAM
policy, EC2, EBS, networking, Route 53, Cognito, systemd unit, application
schema, or data-volume configuration changes are required.

`scripts/aws_phase2a.py` adds a `recover-study-access` command. It:

- verifies the fixed `xutao-dev` account identity;
- reads the instance ID and recovery-document name from the Phase 2A stack;
- requires `--confirm-instance i-0f9ed9c133ea2358b` to equal the stack output;
- sends the parameter-free recovery document to that one instance;
- polls only the newly returned command ID; and
- succeeds only on SSM `Success`, otherwise stopping without retrying the
  command ID.

The recovery command is not combined with `deploy-release`; each live mutation
remains separately reviewable and separately authorized.

`docs/aws/phase2a-runbook.md` documents the recovery command and makes the
recovery-before-deployment order explicit.

## Safety and Data Preservation

- EC2 remains stopped during source implementation, testing, and review.
- The recovery command changes ownership only below
  `/srv/epi-agent/study_data` and does not follow command-line symlink targets
  because `chown` uses `-h`.
- It does not remove or replace the active registry, package versions, Chroma
  data, conversations, checkpoints, artifacts, releases, or other retained EBS
  contents.
- Python receives only fixed `PATH`, `LANG`, `LC_ALL`, `PYTHONUTF8`, and
  `REPORT_AGENT_STUDY_ROOT` values. AWS and provider credentials are not passed.
- The service restart occurs only after ownership and service-user validation
  succeed.
- A new failure is inspected once; the failed command ID is never retried and
  EC2 is stopped again before code changes resume.

## Regression Coverage

Implementation follows RED-GREEN TDD. Tests must prove:

1. the CloudFormation document is parameter-free and uses the fixed paths,
   user, group, environment, and command sequence;
2. ownership repair precedes service-user validation, which precedes the
   service restart and health/readiness checks;
3. the validation rejects unreadable or inconsistent registry/package/index
   state before restarting the service;
4. a real-shell harness beginning with a `root:root`-equivalent mode-`0600`
   registry and non-writable retained index makes both accessible to the
   simulated service identity, preserves sentinel content, restarts the
   service, and reaches the simulated health/readiness gates;
5. recovery failure prevents the service restart and propagates a nonzero
   result;
6. the CLI requires the exact stack instance confirmation, selects only the
   stack-provided recovery document, sends no parameters, polls only the new
   command ID, and fails closed on non-success terminal states; and
7. existing infrastructure, operator CLI, release installer, study installer,
   shell syntax, and executable smoke gates remain green.

## Corrected Live Recovery Order

After the new recovery source, tests, and reviews are complete:

1. create a CloudFormation change set that adds only the recovery SSM document
   and its output;
2. inspect the exact change set and obtain explicit authorization before
   execution;
3. execute the reviewed change set while EC2 remains stopped;
4. build and verify a new immutable application release, upload it under its
   commit-specific key, and verify S3 checksum metadata;
5. obtain explicit authorization naming the release object, recovery document,
   instance, and subsequent deployment actions;
6. start only EC2 instance `i-0f9ed9c133ea2358b` and require SSM Online;
7. invoke `recover-study-access` exactly once and require the existing current
   application to become healthy and ready;
8. deploy the new application release exactly once and require the new release
   to remain current, healthy, and ready;
9. invoke the corrected study installer exactly once against the existing
   verified `report-india-synthetic@0.2.0` S3 object, then restart the service
   once; and
10. verify the active registry, installed study manifest, service-user
    ownership, Chroma access and collection counts, runtime capabilities,
    local/public health and readiness, TLS, alarms, and unchanged EC2/EBS
    identities.

No failed historical SSM command ID is retried, and the study object is not
uploaded again.
