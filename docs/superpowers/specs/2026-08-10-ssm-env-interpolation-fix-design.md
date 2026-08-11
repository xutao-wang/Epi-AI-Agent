# SSM Environment-Variable Interpolation Fix

## Problem

The first Phase 2A application deployment stopped before downloading the
release. The Systems Manager command reported `SSM_Bucket: unbound variable`.
The S3 bucket and release object exist; the command document did not export its
parameters as environment variables because `interpolationType: ENV_VAR` is
attached to the execution-step inputs instead of the individual parameter
definitions.

## Design

Move `interpolationType: ENV_VAR` to every parameter consumed through an
`SSM_...` environment variable in the `epi-agent-deploy-release` command
document: `Bucket`, `ReleaseKey`, `ReleaseSha256`, `ReleaseId`, `DomainName`,
and `CertificateEmail`. Remove the ineffective step-level setting. Continue to
reference only the generated environment variables from the shell script; do
not interpolate operator values directly into shell source.

The change is limited to the SSM document embedded in the Phase 2A
CloudFormation template. It does not replace EC2, EBS, S3, Cognito, DNS, or the
network. It will be deployed through a reviewed CloudFormation update change
set that should modify only `AWS::SSM::Document`.

## Verification

Add a template regression test that loads the command document and verifies:

- each of the six parameters declares `interpolationType: ENV_VAR`;
- the execution-step inputs do not declare `interpolationType`;
- the shell command continues to use the expected `SSM_...` variables.

Run the focused template, host-asset, and AWS CLI tests, then run CloudFormation
validation. After executing the reviewed one-resource update, build a new
immutable release from the resulting commit, upload it under its commit-based
S3 key, and deploy it once through Systems Manager. Verify the command output,
local health/readiness, public HTTPS endpoint, EC2/SSM state, and CloudWatch
signals.

## Failure Handling

Do not retry the failed command unchanged. If the updated deployment fails,
retrieve the exact SSM invocation output before making another change. The
installer's existing checksum, health-check, and rollback behavior remains
unchanged.

## CloudFormation Rollback Recovery

The first live update created non-default document versions but failed when
the CloudFormation execution role could not read the document or set a new
default version. Complete the document-scoped lifecycle policy in one
administrator bootstrap update by retaining the existing create, update,
delete, and describe actions and adding `ssm:GetDocument`,
`ssm:UpdateDocumentDefaultVersion`, `ssm:AddTagsToResource`,
`ssm:RemoveTagsFromResource`, and `ssm:ListTagsForResource`. These actions
remain restricted to `document/epi-agent-*`; do not grant wildcard SSM access
or unrelated attachment and role-passing permissions.

After the bootstrap stack reaches `UPDATE_COMPLETE`, continue the application
stack rollback without skipping resources. Require
`UPDATE_ROLLBACK_COMPLETE`, then create a fresh application change set and
apply the corrected SSM document through CloudFormation. Do not manually set a
document default version or accept stack drift.

## Embedded Archive Extractor

The corrected SSM parameter flow exposed a separate extractor defect:
`source.extractall(...)` runs after the `tarfile.open(...)` context exits, so
Python raises `OSError: TarFile is closed`. Keep the existing archive-member
safety validation and move extraction inside the open context. Add a functional
regression that extracts the exact Python heredoc from the CloudFormation
document, runs it against a small safe tar archive, and verifies the payload is
created. The executable Phase 2A template smoke must exercise the same real
heredoc, not a reimplemented extractor.

Apply this correction through another reviewed SSM-document-only CloudFormation
change set, then build a new immutable release from the tested commit. Do not
retry failed command `67a8ee05-4db7-431d-be98-443c54613253`.
