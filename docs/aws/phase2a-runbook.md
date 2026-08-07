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
