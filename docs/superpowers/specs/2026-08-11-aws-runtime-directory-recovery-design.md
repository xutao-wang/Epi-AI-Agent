# AWS Runtime Directory Recovery Design

## Goal

Make `epi-agent.service` start reliably after every EC2 stop/start or reboot,
and unblock the one-time study-access recovery command without deploying an
unreviewed release. EC2 remains stopped throughout source changes, local
testing, release preparation, and CloudFormation change-set review.

This correction is limited to the ephemeral service runtime directory
`/run/epi-agent`. It does not change EBS data, conversations, checkpoints,
artifacts, study packages, Cognito ownership, networking, or the application's
non-Docker production runtime.

## Confirmed Failure

The current systemd unit includes `/run/epi-agent` in `ReadWritePaths`, so
systemd requires that path while constructing the service mount namespace.
EC2 UserData created the directory during the machine's first bootstrap, but
`/run` is temporary Linux state and was cleared by the later EC2 stop/start.

The recovery document successfully repaired the retained study ownership and
proved that `epi-agent-web` could read the study registry. Its subsequent
service restart failed with systemd status `226/NAMESPACE` because
`/run/epi-agent` no longer existed. This prevented both recovery completion and
the release installer's current-service health gate. Retrying the same recovery
command or deploying the already uploaded release cannot correct this state.

## Considered Approaches

### 1. Manually create the directory on the live instance

Running `mkdir` or `install -d` through SSM would start the current service
quickly, but the problem would return after the next reboot. It would also be
an untracked host mutation rather than an infrastructure-defined correction.
This approach is rejected.

### 2. Add a systemd-tmpfiles rule

A `tmpfiles.d` rule could recreate the directory during boot. It adds a second
host configuration asset and still would not help the currently installed
older unit until the rule was separately installed and invoked. This is valid
but unnecessarily indirect for a directory owned by one service.

### 3. Use systemd `RuntimeDirectory` plus recovery bootstrap

This is the selected approach. The service unit declares ownership of its own
runtime directory, while the recovery document creates the directory before
restarting the currently installed older unit. It provides both permanent
behavior and a safe bridge from the existing broken deployment.

## Selected Design

### Permanent systemd behavior

Add these directives to the `[Service]` section of
`deploy/aws/systemd/epi-agent.service`:

```ini
RuntimeDirectory=epi-agent
RuntimeDirectoryMode=0750
```

systemd will create `/run/epi-agent` with the service's configured
`User=epi-agent-web` and `Group=epi-agent-web` before applying namespace
restrictions and starting the application. It will recreate the directory on
every service start, including after `/run` has been cleared by a reboot. The
existing `ReadWritePaths` entry remains because the application still needs
write access there.

### Recovery bridge for the currently installed unit

Before `systemctl restart epi-agent.service`, the fixed, parameter-free
recovery SSM document will run exactly:

```sh
install -d -m 0750 -o epi-agent-web -g epi-agent-web /run/epi-agent
```

This command is intentionally placed after study validation and before the
single service restart. It lets the old installed service, which does not yet
contain `RuntimeDirectory`, pass systemd's namespace setup. Failure to create
the directory stops the recovery command before the restart because the
document runs with strict shell error handling.

The release installer already creates `/run/epi-agent`, but it cannot serve as
the recovery bridge: it requires the existing service to be healthy before it
activates a new release.

## CloudFormation Scope

The Phase 2A template changes only the content of
`EpiAgentRecoverStudyAccessDocument`. The review-only CloudFormation change set
should therefore modify that SSM document and should not replace or modify
EC2, EBS, Elastic IP, VPC, Route 53, Cognito, S3, or IAM resources.

The systemd unit is shipped inside the immutable application release rather
than installed directly by CloudFormation. The permanent directive becomes
active only after a later, explicitly authorized release deployment.

The change set will be created for inspection only. It will not be executed as
part of this work authorization.

## Test Contract

Implementation follows RED-GREEN test-driven development. Tests must prove:

1. the shipped systemd unit contains exactly one
   `RuntimeDirectory=epi-agent` and one `RuntimeDirectoryMode=0750` in its
   `[Service]` section while retaining the existing `ReadWritePaths` rule;
2. the recovery command creates `/run/epi-agent` with the fixed service owner,
   group, and mode after study validation and before the single restart;
3. runtime-directory creation failure prevents the restart and health checks;
4. the disposable Linux Docker recovery test starts with `/run/epi-agent`
   absent, executes the real `install` command, and verifies the resulting
   ownership and mode before the fake systemd restart. Docker remains a
   test-only Linux compatibility harness; the AWS application remains a native
   Python/systemd deployment;
5. the existing study contents and sentinel data remain unchanged; and
6. focused infrastructure, host-asset, recovery, CLI, release, study, shell,
   frontend, and CloudFormation smoke gates remain green.

## Release Preparation

After tests pass, commit the source correction and build the release twice from
the clean commit. Require identical SHA-256 values, audit the archive for
tracked-source fidelity and forbidden material, and record the new commit ID,
archive path, size, and digest. Preparing this local immutable archive does not
upload or deploy it.

The previously uploaded release
`03993fe7731931bfe35443d868efb9c6fa6fd844` remains unchanged in S3 but must not
be deployed because it lacks the permanent systemd correction.

## Authorized Boundary and Later Resume Sequence

This work may stop the exact EC2 instance, modify and test `aws-test`, prepare
the local release, and create the CloudFormation change set for review. It must
not execute that change set, start EC2, invoke recovery, upload or deploy the
new release, or create a Cognito user.

After a separate review and explicit authorization, live work resumes in this
order:

1. execute the reviewed SSM-document-only change set;
2. start only EC2 instance `i-0f9ed9c133ea2358b` and require SSM Online;
3. invoke the corrected recovery document once and require local health and
   readiness to pass;
4. upload and deploy the newly verified immutable release once;
5. verify local and public HTTPS application behavior at `epiagent.org` and
   confirm the EC2/EBS identities are unchanged; and
6. create the authorized Cognito user only after the public application passes.

No failed historical SSM command ID is retried.
