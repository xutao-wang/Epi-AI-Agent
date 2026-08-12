# Cognito CSP Sign-In Correction Design

## Problem

The production page renders its sign-in gate, but clicking **Sign in** leaves the
browser on `https://epiagent.org/`. A clean headless Chrome reproduction records
the actual failure: the page's Content Security Policy blocks the OIDC discovery
request to the configured Cognito issuer because the policy has
`default-src 'self'` and no `connect-src` directive. The rejected redirect
promise is not rendered by the current gate, so the visible symptom is an
unresponsive button.

This is not Chrome-specific. CSP is a browser security standard enforced by
current Chrome, Safari, Firefox, and Edge implementations. The server header is
the defective boundary.

## Chosen Design

Keep the existing restrictive CSP and add a dedicated `connect-src` directive:

```text
connect-src 'self' https://cognito-idp.us-east-1.amazonaws.com https://*.auth.us-east-1.amazoncognito.com
```

The first external source permits issuer metadata and JWKS access. The second
permits the region-scoped Cognito managed-login token, user-info, revocation, and
logout service hosts. Other external network destinations remain blocked.

The allowlist is region-scoped instead of account-specific so the tracked Nginx
asset remains reusable by the existing Phase 2A deployment template without
embedding an AWS account ID or generated Cognito domain. Removing CSP or allowing
all AWS/Amazon domains is explicitly out of scope.

## Components and Data Flow

1. Nginx serves the frontend with the corrected CSP header.
2. The frontend loads browser-safe authentication configuration from the
   same-origin `/api/public-config` endpoint.
3. `oidc-client-ts` requests Cognito discovery metadata from
   `cognito-idp.us-east-1.amazonaws.com`; the new policy permits it.
4. The browser navigates to Cognito managed login, then returns to
   `https://epiagent.org/auth/callback`.
5. Token and user-info requests use the Cognito managed-login host and are
   permitted by the region-scoped source.

Authentication, Cognito pool/client settings, callback URIs, user ownership,
application APIs, EC2, EBS, and study data do not change.

## Implementation and Deployment

Modify only the tracked Nginx asset and its host-asset regression test. Follow
test-driven development: first add a test that requires the same-origin and both
Cognito `connect-src` entries and observe it fail against the current asset;
then make the minimal header change and observe it pass.

After the focused and broader local gates pass, commit the correction, build the
deterministic archive twice, verify byte identity and checksum, upload one new
commit-specific S3 object, and deploy it once through the existing guarded
Phase 2A release command. No CloudFormation change set is required.

## Verification and Safety

Verification must establish all of the following:

- the focused CSP regression test fails before the asset change and passes after;
- the existing backend/AWS, frontend, smoke, syntax, and deterministic-release
  gates remain green;
- the live response contains the new `connect-src` directive without weakening
  the rest of the policy;
- a clean browser click leaves `epiagent.org` and reaches the expected Cognito
  managed-login page without CSP, console, or failed-request errors;
- public health and readiness remain successful with the exact new release ID;
- the existing Cognito user and encrypted EBS volumes remain unchanged;
- EC2 remains running after successful deployment.

If the single deployment command fails, do not retry automatically. Preserve
its command ID and diagnose before any additional mutation.

## Explicit Non-Goals

- Removing or broadly disabling Content Security Policy.
- Changing authentication flows, Cognito resources, users, or passwords.
- Adding Docker to the production runtime.
- Adding a new sign-in error UI in this correction; the root policy failure is
  fixed at its source.
- Changing CloudFormation, systemd, storage, application endpoints, or schemas.
