# `www` Redirect and Pinned AMI Delivery Design

## Objective

Deliver `www.epiagent.org` as a secure redirect to the canonical
`https://epiagent.org` URL, and make ordinary application-stack updates
predictable by pinning the EC2 AMI. This uses the existing Phase 2A application
stack; it does not create a DNS-only stack or move the apex DNS record.

## Current State

- The Phase 2A stack owns the apex `A` record for `epiagent.org` and the
  application EC2 instance.
- The implementation branch adds `ApplicationWwwDnsRecord`, which aliases
  `www.epiagent.org` to the apex record, and adds Nginx/TLS support that
  redirects `www` to the apex.
- `ApplicationInstance.ImageId` currently resolves the mutable SSM parameter
  `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64`.
  CloudFormation therefore marked the instance conditionally replaceable in a
  review-only DNS change set even though the currently resolved AMI matches the
  running host.
- The running application instance `i-0f9ed9c133ea2358b` uses
  `ami-07a5b367e8dc8bd92`.

## Chosen Architecture

The existing `epi-agent-phase2a` stack remains the sole owner of both the apex
and `www` application DNS records. The `www` record is an `A` alias in the
existing hosted zone that follows the apex record; the apex record continues to
point at the existing Elastic IP.

The application stack adds an `ApplicationAmiId` string parameter and sets the
EC2 instance's `ImageId` to that parameter. The parameter's initial value is
the currently running AMI, `ami-07a5b367e8dc8bd92`. The CloudFormation
parameter-example file and guarded `plan-stack` operator command include the
same value.

No application, Cognito, session, EIP, EBS, hosted-zone, or domain-registration
resource is moved or redesigned.

## AMI Maintenance Policy

An AMI change is an explicit maintenance release. An operator must deliberately
choose and validate a target AMI, update the `ApplicationAmiId` stack parameter,
create a change set, inspect the required EC2 replacement and dependent-resource
actions, establish a data-recovery and availability window, and obtain explicit
approval before execution.

Routine changes—such as DNS records, application releases, certificate changes,
and alarm/configuration updates—must pass the existing pinned AMI value back to
CloudFormation. They must not automatically consume a newer SSM "latest" AMI.

## `www` Routing and TLS Behavior

- Route 53 creates `www.epiagent.org` as an alias to the apex record.
- The TLS certificate covers both `epiagent.org` and `www.epiagent.org`.
- HTTP requests to either hostname redirect to the literal canonical origin
  `https://epiagent.org$request_uri`.
- HTTPS requests to `www` redirect to the same literal canonical origin.
- Only HTTPS requests to the apex are proxied to FastAPI.
- Cognito callback and logout URLs remain apex-only.

## Delivery Sequence

1. Verify local tests, executable template regression coverage, and both
   CloudFormation templates without executing AWS changes.
2. Build an immutable release and verify its manifest checksum and commit ID.
3. Create an application-stack change set passing every live stack parameter,
   including the pinned current `ApplicationAmiId`.
4. Inspect the change set. For this delivery it must add
   `ApplicationWwwDnsRecord` and must not modify or replace the EC2 instance,
   EIP association, volume attachment, alarms due to an instance reference, or
   any unrelated resource.
5. Execute only the explicitly approved exact change-set ARN.
6. Confirm `www` DNS resolves to the existing endpoint, upload and deploy the
   immutable application release once, then run the dedicated public smoke once.

If the change set contains an EC2 change despite the pinned current AMI, stop
and diagnose before execution. No attempt is made to work around a change-set
warning by accepting the risk blindly.

## Verification

Automated tests verify that:

- the AMI parameter has the expected allowed format and supplies the EC2
  `ImageId`;
- operator planning includes `ApplicationAmiId` and validates it;
- the example parameter file includes the pinned AMI;
- the template no longer contains an Amazon Linux "latest" dynamic reference;
- the apex and `www` DNS resources retain their defined ownership;
- the existing Nginx, certificate lifecycle, Cognito, and redirect contracts
  remain intact.

The production smoke runs once after the approved change and verifies DNS,
two-name TLS, redirect preservation, apex health/readiness, the compiled
browser entry point, and apex Cognito redirect configuration.

## Non-Goals

- Creating a DNS-only stack.
- Moving the apex DNS record between stacks.
- Automatically updating Amazon Linux or replacing EC2 instances.
- Adding autoscaling, a load balancer, CloudFront, or a second application host.
