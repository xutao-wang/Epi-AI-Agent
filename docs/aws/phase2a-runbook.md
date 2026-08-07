# Phase 2A AWS operations

> **Warning:** do not execute a change set until an administrator has reviewed every action.

Run `python scripts/aws_phase2a.py identity`, then `validate`, then create and
review change sets. Confirm the certificate email and SNS subscription, invite
the first Cognito user, upload release/study artifacts, deploy by SSM, and
check CloudWatch alarms. Stop preserves EBS cost; terminate can destroy the
root disk. Restore snapshots only to a separate volume/mount. Domain changes,
retained-resource cleanup, and snapshot deletion require separate review.

Repository support does not mean a live stack exists until Task 11. The opt-in
real smoke requires `--allow-live-aws`; it is never run as part of pytest.
