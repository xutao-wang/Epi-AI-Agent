# Phase 2A AWS operations

> **Warning:** do not execute a change set until an administrator has reviewed every action.

Run `python scripts/aws_phase2a.py identity`, then `validate`. An administrator
creates and reviews the bootstrap change set before its explicit execution;
then use `plan-stack --domain-name epiagent.org --hosted-zone-id Z... --certificate-email ops@example.org`.
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
→ Create stack → With new resources → Upload a template file**, selects
`infra/aws/bootstrap/template.yaml`, names it `epi-agent-bootstrap`, enters
`DeveloperGroupName=Developer`, acknowledges `CAPABILITY_NAMED_IAM`, reviews the
IAM role, policy, and permissions-boundary resources, and only then creates the
stack. The pinned developer CLI must not perform this administrator action.

```sh
HOSTED_ZONE_ID="Z123456789EXAMPLE"
CERT_EMAIL="ops@example.org"
python scripts/aws_phase2a.py identity
python scripts/aws_phase2a.py validate
python scripts/aws_phase2a.py plan-stack --domain-name epiagent.org --hosted-zone-id "$HOSTED_ZONE_ID" --certificate-email "$CERT_EMAIL"
```

In the AWS Console select **IAM → User groups → Developer** and verify the
operator uses `xutao-dev`. Run:

```sh
python scripts/aws_phase2a.py identity
python scripts/aws_phase2a.py validate
python scripts/aws_phase2a.py plan-bootstrap
```

> **Warning:** inspect every CloudFormation **Add**, **Modify**, and especially
> **Replace** action before executing the exact reviewed change-set ARN. Bootstrap
> execution is an administrator action; it creates IAM authority.

After bootstrap, use `plan-stack --domain-name epiagent.org --hosted-zone-id Z...`
`--certificate-email ops@example.org`, review its actions, and execute only with
the exact account confirmation. In **SNS → Subscriptions**, confirm the email.
In **Cognito → User pools → epi-agent-phase2a → Users**, choose **Create user**
to send the first invitation.

Upload immutable packages with `upload-release RELEASE releases/ID.tar.gz` and
`upload-study STUDY studies/ID.tar.gz`; invoke `deploy-release` with the exact
key, SHA-256, release ID, domain, and certificate email. In **CloudWatch →
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
python scripts/aws_phase2a.py execute-bootstrap CHANGE_SET_ARN --confirm-account 641379499556
python scripts/aws_phase2a.py execute-change-set CHANGE_SET_ARN --confirm-account 641379499556
python scripts/aws_phase2a.py upload-release dist/aws/epi-agent-SHA.tar.gz releases/SHA.tar.gz
python scripts/aws_phase2a.py upload-study study.tar.gz studies/STUDY.tar.gz
python scripts/aws_phase2a.py deploy-release releases/SHA.tar.gz SHA256 RELEASE_SHA epiagent.org ops@example.org
```

Replace only the uppercase placeholders after reviewing the matching change set
or immutable release metadata; never paste a provider key into a command.
