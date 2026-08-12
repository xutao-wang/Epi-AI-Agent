# Phase 2A AWS operations

> **Warning:** do not execute a change set until an administrator has reviewed every action.

After the administrator bootstrap, run `python scripts/aws_phase2a.py identity`
and `validate`, then create the application plan with the executable variables below.
Review every Add/Modify/Replace action before executing the exact change-set
ARN. Confirm the certificate email and SNS subscription, invite the first
Cognito user in the Cognito Console, upload release/study artifacts, deploy by
SSM, and check EC2/CPU/credits/memory/disk/service-error CloudWatch alarms.

> **Warning:** stopping preserves EBS and EIP cost; termination can destroy the
> root disk. Do not terminate retained data without a recovery plan.

Rollback by executing a reviewed change set or activating a prior release. Restore
an EBS snapshot only to a new, separate volume and mount before migration. Domain
changes require DNS/TLS review. Cost-bearing resources are the EC2 host, EIP,
EBS data/root disks and snapshots, S3 storage, CloudWatch logs/alarms, and SNS.
Retained-resource cleanup and snapshot deletion require separate explicit review.

Repository support does not mean a live stack exists until Task 11. The opt-in
real smoke requires `--allow-live-aws`; it is never run as part of pytest.
Its BYOK check reads only `REPORT_AGENT_SMOKE_PROVIDER_KEY` from the environment;
never place provider keys on the command line.

## Beginner operator sequence

For the one-time bootstrap, an MFA-protected administrator uses **CloudFormation
→ Change sets → Create change set → For new stack**, uploads
`infra/aws/bootstrap/template.yaml`, sets stack name `epi-agent-bootstrap`,
type `CREATE`, `DeveloperGroupName=Developer`, and `CAPABILITY_NAMED_IAM`.
Create the change set, inspect every Add/Modify/Replace IAM role, policy, and
permissions-boundary action, then **stop for explicit approval**. Execute that
reviewed change set in the Console only after approval. The pinned developer
CLI must not perform this administrator action.

```sh
HOSTED_ZONE_ID="Z123456789EXAMPLE"
CERT_EMAIL="ops@example.org"
python scripts/aws_phase2a.py identity
python scripts/aws_phase2a.py validate
python scripts/aws_phase2a.py plan-stack --domain-name epiagent.org --hosted-zone-id "$HOSTED_ZONE_ID" --certificate-email "$CERT_EMAIL"
```

> **Warning:** inspect every CloudFormation **Add**, **Modify**, and especially
> **Replace** action before executing the exact reviewed change-set ARN. Bootstrap
> execution is an administrator action; it creates IAM authority.

Review the application plan and execute only with the exact account confirmation.
In **SNS → Subscriptions**, confirm the email.
In **Cognito → User pools → epi-agent-phase2a → Users**, choose **Create user**
to send the first invitation.

Upload immutable packages with `upload-release RELEASE releases/ID.tar.gz` and
`upload-study STUDY studies/ID.tar.gz`. Install a study only through the
stack-provided `epi-agent-install-study` document after reviewing and executing
the CloudFormation change set that creates it. The guarded operation verifies
the S3 checksum metadata, targets only the stack instance, runs the production
installer as `epi-agent-web`, restarts the service once, and requires local
health, readiness, active-version identity, and installed archive checksum.

When retained study permissions prevent the current application from starting,
first create, inspect, approve, and execute the change set that adds
`epi-agent-recover-study-access`. Start the exact stack instance, invoke
`recover-study-access` with its exact instance confirmation, and require SSM
`Success` plus healthy local endpoints before invoking `deploy-release`. Never
deploy first and plan to repair the retained study afterward: the release
installer's current-service gate will fail closed. In **CloudWatch →
Alarms/Logs**, check status, CPU, credits, memory, disk, and service errors.

> **Warning:** terminate, retained bucket/volume cleanup, and snapshot deletion
> are separate destructive actions. Stopping the instance retains EBS/EIP cost;
> termination removes the root disk. Snapshot restore means creating a separate
> volume, attaching it to a separate mount, validating it, then migrating.

For rollback deploy a previously verified release. For a domain change, update
DNS and certificate configuration through a reviewed change set. Cost inventory:
EC2 runtime, EIP, root/data EBS, 14-day snapshots, S3 objects/requests,
CloudWatch logs/alarms, SNS, Cognito, Route 53 hosted-zone and DNS-query charges.

## Exact reviewed actions

```sh
# Administrator-only Console action: the pinned xutao-dev CLI cannot bootstrap.
python scripts/aws_phase2a.py execute-change-set CHANGE_SET_ARN --confirm-account 641379499556
python scripts/aws_phase2a.py upload-release dist/aws/epi-agent-SHA.tar.gz releases/SHA.tar.gz
python scripts/aws_phase2a.py upload-study report-india-synthetic-0.3.0.tar.gz studies/report-india-synthetic-0.3.0.tar.gz
python scripts/aws_phase2a.py recover-study-access --confirm-instance i-0f9ed9c133ea2358b
python scripts/aws_phase2a.py deploy-release releases/SHA.tar.gz SHA256 RELEASE_SHA epiagent.org ops@example.org
python scripts/aws_phase2a.py install-study studies/report-india-synthetic-0.3.0.tar.gz c1cd71222657502d214a745a236018f984e1092cab0ddbf7479b5d14b15493d4 report-india-synthetic 0.3.0 --confirm-instance i-0f9ed9c133ea2358b
```

`recover-study-access` is a separately authorized, parameter-free recovery
operation. Run it only after its CloudFormation change set is complete and the
instance is SSM Online. Record the emitted
`study access recovery command ID: COMMAND_ID` line; on any non-success terminal
state, inspect that exact invocation once, do not retry the command ID, stop the
instance, and return to source diagnosis.

`install-study` is also a separately authorized operation. Run it only after
the named SSM document exists, the exact archive has been uploaded, its S3
`sha256` metadata matches the command, and the instance is SSM Online. Record
the emitted `study installation command ID: COMMAND_ID` line. A non-success
terminal state is inspected once and never resent automatically. Confirm that
`registry.json` activates `report-india-synthetic@0.3.0`, that the versioned
`0.2.0` directory remains installed, and that public and local health/readiness
remain successful.

Study rollback is explicit rather than automatic. After separate approval, run
the current release's `study_installer.py --activate
report-india-synthetic@0.2.0` as `epi-agent-web`, restart
`epi-agent.service` once, and require health and readiness before accepting the
rollback. Preserve the failed installation command and logs so rollback does
not hide the original compatibility failure.

Replace only the uppercase placeholders after reviewing the matching change set
or immutable release metadata; never paste a provider key into a command.
