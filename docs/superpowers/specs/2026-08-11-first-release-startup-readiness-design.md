# First-Release Startup Readiness Design

## Goal

Allow the first AWS application release to remain selected while FastAPI starts,
wait for a real health result instead of checking immediately, and make the
service start automatically after later EC2 stop/start or reboot cycles.

## Observed Failure

SSM command `84aa0bdd-aeaf-4c51-99f9-d1e13da343a1` successfully downloaded
and checksum-verified release `5233a449d76998a2c35c38e7125c8f9566b79a25`,
extracted it, created its Python 3.12 virtual environment, and installed all
requirements. The installer then ran one health request immediately after
`systemctl restart epi-agent.service`. Nothing was listening yet, so `curl`
returned connection error 7.

That error invoked the existing cleanup trap. Because this was the first
release, cleanup removed `/opt/epi-agent/current` and stopped the new service in
the same second it started. The subsequent `No module named uvicorn` log was a
consequence of the removed `current` path, not a missing dependency: read-only
diagnostics proved both root and `epi-agent-web` can import `uvicorn` from the
installed release directory.

The bootstrap also deliberately disables `epi-agent.service` before any release
exists. The installer currently restarts the unit but does not enable it, so a
successful first release would not automatically return after an EC2 reboot.

## Selected Design

Keep deployment atomic and add a bounded, condition-based startup gate in
`deploy/aws/bin/install-release.sh`.

1. After atomically switching `/opt/epi-agent/current`, idempotently enable
   `epi-agent.service` and restart it.
2. Poll `http://127.0.0.1:8000/api/health` every two seconds for at most 120
   seconds.
3. While polling, fail early if systemd reports the unit is no longer active.
4. Keep the maintenance marker and `current` symlink in place throughout the
   startup window. This lets Python resolve the selected virtual environment.
5. When health succeeds, remove the maintenance marker and run the existing
   health/readiness verification.
6. If the service exits or the deadline expires, use the existing cleanup and
   rollback path. Do not add automatic deployment retries.

The wait is intentionally bounded. A fixed sleep would be brittle on a cold EC2
host, while redesigning the application around systemd `Type=notify` would be a
larger architectural change than Phase 2A requires.

## First Release and Rollback Behavior

For a first release, failure removes `current` and stops the service, preserving
the current safe behavior. For an update, failure restores and restarts the
previous release. The installer does not delete release directories or any EBS
runtime data.

The failed inactive release directory
`5233a449d76998a2c35c38e7125c8f9566b79a25` remains untouched. The correction
will have a new commit SHA and therefore a different immutable release path, so
it will not collide with that directory. Cleanup of inactive release directories
is outside this fix and requires a separate retention decision.

## Test Strategy

Extend the executable installer harness before modifying production code.

- Add a first-release case with no existing `current` symlink.
- Make the fake health endpoint fail initially and then succeed.
- Verify the existing installer fails this test before the correction.
- Verify the corrected installer polls, retains the selected release long enough
  to become healthy, exits successfully, clears maintenance, and enables the
  systemd unit.
- Preserve the existing invalid-archive, readiness-failure, rollback, and
  staging-cleanup tests.
- Run the focused host-asset tests, the AWS infrastructure/CLI suites, the
  executable Phase 2A smoke, shell syntax validation, and CloudFormation
  validation.

## Deployment Sequence

After test-first implementation and independent review:

1. Build a clean, commit-identified archive with a new release ID and checksum.
2. Upload it to the existing private, versioned S3 bucket and verify AES-256
   encryption, SHA-256 metadata, and version ID.
3. Invoke the corrected default SSM document exactly once with the new release.
   Never retry either prior failed command unchanged.
4. If deployment succeeds, verify local and public health/readiness, TLS for
   `epiagent.org`, deployment alarms, and unchanged EC2/EBS identities.
5. If deployment fails, preserve and inspect the exact invocation output before
   considering another change.

No CloudFormation, EC2, EBS, VPC, IAM, Cognito, Route 53, database, or user-data
change is required for this correction.
