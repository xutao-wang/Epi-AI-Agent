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
