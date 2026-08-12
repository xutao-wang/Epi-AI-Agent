# AWS Release Identity Manifest Design

## Goal

Make the readiness and deployment-status endpoints report the exact immutable
release commit running on AWS. Preserve the existing explicit environment
override for tests and unusual operator workflows, while deriving normal AWS
release identity from the verified `release.json` packaged with every release.

This is an observability correction. Release
`2ceb22bab62c60c12cae9dec49de308b2f5353cf` is already active, healthy, and
ready at `epiagent.org`; its endpoints currently display `development` only
because the service process receives no `REPORT_AGENT_RELEASE_ID` value.

## Confirmed Root Cause

The release builder embeds a root-level `release.json` containing the exact
40-character Git commit SHA. The release installer validates that manifest
against the requested release ID before activating the release directory.

The application currently reads release identity only from
`REPORT_AGENT_RELEASE_ID`, defaulting to `development` when it is absent.
Neither EC2 UserData nor the release installer writes that environment value.
Read-only host diagnostics proved all of the following simultaneously:

- `/opt/epi-agent/current` points to the new immutable release directory;
- the service is active and enabled;
- the corrected systemd unit is installed;
- `/run/epi-agent` has the expected owner, group, and mode;
- the active root `release.json` contains the new commit; and
- the configuration file and process environment both omit
  `REPORT_AGENT_RELEASE_ID`.

The activation is therefore correct; only the application's identity source
is incomplete.

## Considered Approaches

### 1. Read the packaged release manifest in the application

This is the selected approach. The application already runs from the active
release directory, so the root `release.json` is the authoritative identity of
that code. Deployments and rollbacks change the `current` symlink and restart
the service, automatically selecting the matching manifest. Reboots retain the
release directory and symlink, so no ephemeral state is involved.

### 2. Rewrite `/etc/epi-agent/app.env` during deployment

This would persist the release ID, but the installer would need atomic updates,
duplicate-key handling, and rollback restoration. It would also mix immutable
release activation with mutable CloudFormation-owned host configuration. This
is rejected as unnecessary operational coupling.

### 3. Add a systemd wrapper or generated environment file

A wrapper could parse the manifest and export the value before starting
Uvicorn. This moves application metadata parsing into host scripts and adds
another asset to install and maintain. It offers no benefit over reading the
same validated JSON inside the application and is rejected.

## Selected Application Behavior

`DeploymentState.from_environ` gains an optional release-manifest path for
deterministic callers and tests. When not provided, it uses `release.json` in
the project/release root containing `api/deployment.py`.

Release identity is resolved in this order:

1. A nonblank `REPORT_AGENT_RELEASE_ID` value wins exactly as it does today.
2. Otherwise, read the selected root `release.json` and accept its
   `commit_sha` only when it is a string matching exactly 40 lowercase
   hexadecimal characters.
3. If the file is absent, unreadable, malformed JSON, structurally invalid, or
   has an invalid commit SHA, use `development`.

An explicitly blank environment value behaves like an absent value and permits
manifest discovery. The manifest path is not configurable through another
environment variable; production has one fixed package layout, and adding
operator-controlled path selection would create unnecessary ambiguity.

The maintenance-file behavior and endpoint schemas remain unchanged.

## Error and Security Behavior

Manifest problems must not prevent local development or crash the service.
`development` is a visible fail-soft value that makes missing identity obvious
without reducing application availability. Deployment safety does not depend
on this application read: the root-owned installer already checksum-verifies
the archive and validates `release.json` before activation.

The parser reads one fixed local file, extracts only `commit_sha`, and never
executes manifest content. It accepts no path from HTTP requests or user
environment configuration.

## Test Contract

Implementation follows RED-GREEN TDD. Tests must prove:

1. a nonblank environment release ID continues to win over any manifest;
2. an absent or blank environment value uses a valid 40-character lowercase
   manifest `commit_sha`;
3. a missing file, unreadable path, malformed JSON, non-object JSON, missing or
   non-string `commit_sha`, uppercase SHA, wrong length, and non-hex SHA all
   fall back to `development` without raising;
4. maintenance-file behavior is unchanged;
5. API readiness and deployment-status continue exposing the resolved
   `DeploymentState.release_id` without schema changes; and
6. the focused deployment tests, API tests, AWS gates, frontend gates, and
   release builder remain green.

## Release and AWS Handoff

After the correction passes review and verification:

1. commit it on `aws-test` and build the clean commit twice;
2. require byte-identical archives and audit the archive as before;
3. upload the new archive to its commit-specific private, versioned S3 key;
4. deploy it exactly once through `epi-agent-deploy-release` and preserve the
   new SSM command ID;
5. verify `/opt/epi-agent/current`, the packaged manifest, the installed
   systemd unit, runtime-directory ownership, and local health/readiness;
6. require public HTTPS readiness to report the exact new commit rather than
   `development`; and
7. only then create or confirm Cognito user `xw488@njms.rutgers.edu`.

No CloudFormation update, recovery rerun, study reinstall, EBS mutation, or
Docker production deployment is required. The current live release remains
available while local implementation and testing occur; a failed new
deployment is not retried automatically.
