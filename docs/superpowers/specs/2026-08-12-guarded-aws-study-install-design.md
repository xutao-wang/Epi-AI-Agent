# Guarded AWS Study Installation Design

## Goal

Provide one repeatable, account-guarded operator command that installs and
activates a checksummed study archive on the existing Phase 2A host. The new
operation wraps the existing `install-study.sh` and `study_installer.py`
validation pipeline; it does not duplicate or weaken package validation.

The first intended use is installing
`report-india-synthetic@0.3.0` while retaining the installed `0.2.0` package
for rollback.

## Existing Verified Boundary

The current application release already installs
`/usr/local/sbin/install-study.sh` with the ownership correction from commit
`7dbc21c`. That script:

1. accepts a bucket, `studies/` object key, SHA-256, study ID, and version;
2. validates each value before constructing the S3 request;
3. downloads the archive to a private staging directory and verifies its
   SHA-256;
4. changes the retained study root and staging ownership to
   `epi-agent-web:epi-agent-web`;
5. invokes the current release's `study_installer.py` as `epi-agent-web` in a
   fixed environment; and
6. verifies that the installed manifest matches the requested study ID and
   version.

`study_installer.py` stages and validates the complete archive before changing
the active registry. It rejects unsafe archive entries, invalid or mismatched
manifests, unreadable DuckDB/catalog/Chroma assets, invalid knowledge, invalid
Markdown study design, and reused versions with different archive hashes. A
successful install preserves existing versions, promotes the new version into
its versioned directory, and atomically updates `registry.json` to make it
active.

## Selected Approach

Add a dedicated SSM Command document named `epi-agent-install-study` and a
matching `aws_phase2a.py install-study` subcommand.

The operator flow remains three separately reviewable actions:

1. `upload-study` places an immutable checksummed archive under `studies/`;
2. `install-study` installs and activates that exact object on the stack's one
   application instance; and
3. `deploy-release` independently changes application code.

The install operation is not folded into application deployment. Study data
and application releases keep separate identities, checksums, rollback paths,
and authorization points.

## SSM Document

`infra/aws/phase2a/template.yaml` adds
`EpiAgentInstallStudyDocument`, with `UpdateMethod: NewVersion`, and the stack
output `InstallStudyDocumentName`.

The document accepts five typed string parameters:

- `Bucket`: the stack application bucket;
- `StudyKey`: a key beginning with `studies/`;
- `StudySha256`: exactly 64 lowercase hexadecimal characters;
- `StudyId`: one safe lowercase identifier segment; and
- `PackageVersion`: one safe lowercase version segment.

Each parameter uses `interpolationType: ENV_VAR` and a restrictive
`allowedPattern`. The document executes a fixed Bash program and never
evaluates caller input as shell source.

The fixed program:

1. invokes `/usr/local/sbin/install-study.sh` with the five SSM environment
   values;
2. restarts `epi-agent.service` exactly once after installation succeeds;
3. waits up to 120 seconds for local `/api/health` and `/api/readiness`;
4. fails immediately if the service becomes inactive;
5. requires readiness JSON with `status == "ready"` before the deadline; and
6. runs a fixed Python verification as `epi-agent-web` that loads
   `/srv/epi-agent/study_data/studies/registry.json`, confirms the requested
   study/version is active, loads the installed package through the production
   registry/installer code, confirms the installed record and manifest
   identity, and confirms its recorded archive SHA-256 equals the requested
   checksum.

The document succeeds only if package installation, activation, service
restart, health, readiness, and post-install identity verification all pass.
It never deletes older package versions.

## Operator CLI

`scripts/aws_phase2a.py` adds:

```text
install-study <key> <sha> <study_id> <version> --confirm-instance <instance-id>
```

The command:

1. validates the key, checksum, study ID, and version locally before AWS calls;
2. verifies the pinned account `641379499556` and principal
   `arn:aws:iam::641379499556:user/xutao-dev`;
3. reads `ApplicationBucketName`, `ApplicationInstanceId`, and
   `InstallStudyDocumentName` from the `epi-agent-phase2a` stack;
4. requires `--confirm-instance` to equal the exact stack instance ID;
5. verifies the S3 object's `sha256` metadata equals the requested checksum;
6. sends the stack-provided SSM document only to the stack-provided instance;
7. prints the returned command ID before polling;
8. waits only for that command ID; and
9. succeeds only on SSM `Success`, without automatically retrying a failed
   invocation.

The command cannot accept a bucket name, document name, instance ID target, or
shell fragment from the operator beyond the exact instance confirmation.

## Failure and Rollback Behavior

- Failure before registry promotion leaves the active `0.2.0` package
  unchanged.
- A successful `0.3.0` installation retains `0.2.0` on disk and changes only
  the active registry entry.
- If service health or readiness fails after registry activation, the SSM
  command reports failure and preserves its command ID and output for
  diagnosis. It does not automatically retry or silently reactivate `0.2.0`.
- Rollback is an explicit, separately authorized operation using the existing
  production CLI on the host:
  `study_installer.py --activate report-india-synthetic@0.2.0`, followed by one
  service restart and health/readiness verification. Automatic rollback is
  excluded because it could hide a package/application compatibility failure.
- No application release, CloudFormation change set, S3 object, package
  version, conversation, runtime artifact, EBS volume, or EC2 instance is
  deleted by this operation.

## Regression Coverage

Implementation follows RED-GREEN TDD and must prove:

1. the CloudFormation document has exactly the five constrained parameters,
   uses environment interpolation, invokes only the fixed installer path, and
   orders install before restart before health/readiness before post-install
   verification;
2. the document verifies the active registry, installed identity, and archive
   checksum as `epi-agent-web` and preserves older versions;
3. the CLI rejects unsafe keys, malformed checksums, IDs, versions, and an
   incorrect instance confirmation before sending a command;
4. the CLI uses only stack outputs for bucket, document, and instance, verifies
   S3 checksum metadata, prints the new command ID, and polls that exact ID;
5. terminal SSM failure and timeout return failure without resending;
6. existing AWS infrastructure, operator CLI, release installer, study
   installer, and template-regression suites remain green; and
7. the real tracked `report-india-synthetic-0.3.0.tar.gz` still installs into a
   clean temporary root as format version 3 with its Markdown design and
   activates `report-india-synthetic@0.3.0`.

## Deployment Sequence

After source implementation and local verification:

1. create a CloudFormation change set containing the new SSM document and
   output;
2. inspect every change and obtain explicit approval before executing the exact
   change-set ARN;
3. execute the reviewed change set and confirm the new stack output;
4. upload the immutable application release and `0.3.0` study archive under
   version-specific keys, without overwriting different content;
5. deploy the new application release and require its exact release ID to be
   healthy and ready;
6. obtain explicit approval for the exact study key, checksum, study ID,
   version, document, and instance;
7. invoke `install-study` once and preserve its command ID;
8. verify the active registry is `0.3.0`, `0.2.0` remains installed, the app is
   healthy and ready, and the service identity can read and write the active
   Chroma index; and
9. perform hosted acceptance for cancellation and the plain-language activity
   timeline before publication.

No AWS mutation occurs during source implementation or local verification.
