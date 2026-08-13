# `www` to Apex Domain Redirect Design

## Objective

Make both `https://epiagent.org` and `https://www.epiagent.org` safe to visit,
while keeping `https://epiagent.org` as the single canonical application
origin. Requests to `www.epiagent.org` redirect permanently to the equivalent
apex URL without changing the path or query string.

This is a compatibility and branding change, not a scaling-architecture
change. Future migration from the current Elastic IP to CloudFront or an
Application Load Balancer remains possible through Route 53 alias records.

## Current State

- Route 53 publishes one `A` record from `epiagent.org` to the application
  Elastic IP. `www.epiagent.org` has no DNS record.
- The tracked Nginx configuration names both hosts, but the live Let's Encrypt
  certificate is requested only for `epiagent.org`. Merely adding DNS would
  therefore expose a TLS name mismatch at `https://www.epiagent.org`.
- Cognito callback and logout URLs, the application environment, the real AWS
  smoke, and operational documentation use `https://epiagent.org`.
- Nginx proxies the application directly from the EC2 host to FastAPI.

## Chosen Approach

Keep the apex host canonical and make `www` redirect to it.

The alternatives are rejected for this phase:

- Making `www` canonical would require a coordinated Cognito callback, logout,
  application-origin, and user-session cutover without providing an AWS
  scaling advantage.
- Serving the application independently on both hosts would create two browser
  origins and could split cookies, browser storage, authentication state,
  analytics, and search indexing.

## DNS Design

CloudFormation adds a Route 53 `A` alias named `www.${DomainName}` that targets
the existing apex `A` record in the same hosted zone. The alias follows the
apex target, so a future change from the Elastic IP to an AWS load balancer or
CloudFront does not require a separate `www` target change.

The existing apex record remains unchanged. The stack continues to reuse the
existing hosted zone and does not create, import, replace, or delete the domain
registration or hosted zone.

## TLS and Certificate Lifecycle

The application certificate is one Let's Encrypt certificate named for the
apex domain with both DNS names in its Subject Alternative Names:

- `epiagent.org`
- `www.epiagent.org`

For a new installation, the release installer requests both names. For an
existing installation, it checks whether the current certificate covers both
hosts and expands the certificate only when `www` is missing. It must not issue
a new certificate on every release.

On the existing host, the active HTTP server already accepts both names, so
certificate expansion can use the same webroot challenge after the new DNS
record resolves. On a new host that has no certificate or active application
configuration yet, the release installer creates a narrowly scoped temporary
HTTP bootstrap server for the `www` ACME challenge, validates and reloads
Nginx, obtains the two-name certificate, and removes the temporary server when
the full application configuration is activated. This avoids changing EC2
user data and therefore avoids an instance restart solely for this feature.

Certificate renewal remains owned by the existing Certbot systemd service and
timer. Renewing the expanded certificate renews both names, followed by the
existing Nginx reload.

## Nginx Behavior

Nginx has one application host and one redirect host:

- HTTP ACME challenge paths remain available for both hostnames.
- All other `http://epiagent.org/...` requests redirect to the same apex HTTPS
  URL.
- All other `http://www.epiagent.org/...` requests redirect directly to the
  corresponding apex HTTPS URL.
- `https://www.epiagent.org/...` returns a permanent redirect to
  `https://epiagent.org/...`.
- `https://epiagent.org/...` continues to serve the application and operational
  health routes exactly as it does today.

Redirects preserve `$request_uri`, including the path and query string. The
redirect target is the literal canonical apex host rather than `$host`, which
prevents the alias from remaining visible in the browser. The application is
never proxied under the `www` origin.

The existing security headers, request-size limit, proxy timeouts, WebSocket
headers, and denial of the external deployment-status route remain unchanged
on the application host.

## Authentication and Application Configuration

Cognito remains configured with:

- callback URL `https://epiagent.org/auth/callback`;
- logout URL `https://epiagent.org/`.

The corresponding application environment variables also remain unchanged.
Because `www` redirects before serving the application, no Cognito callback or
logout URL for `www` is added, and users have only one application origin and
one browser session context.

## Deployment and Failure Handling

The change uses the existing reviewed CloudFormation and immutable-release
workflow:

1. Create and inspect a CloudFormation change set that adds only the `www`
   Route 53 record. Any unexpected EC2 replacement, EC2 modification, or
   unrelated infrastructure action stops the rollout for review.
2. Execute the approved change set so `www.epiagent.org` resolves to the current
   application endpoint.
3. Confirm DNS resolution before requesting certificate expansion.
4. Deploy the reviewed release through the existing SSM release document.
5. Create a temporary `www` ACME bootstrap only when there is no active
   certificate configuration, then obtain or expand the certificate before
   activating the redirecting HTTPS configuration.
6. Validate Nginx before each reload, remove the temporary bootstrap during
   activation, and retain the installer's current rollback behavior if
   activation fails.

If DNS is not ready or certificate expansion fails, the installer fails closed
before activating a configuration that claims HTTPS support for `www`, and it
restores or removes any temporary bootstrap it created. The existing apex
application and certificate remain valid. No automatic retry is performed,
avoiding accidental Let's Encrypt rate-limit pressure. Since `www` does not
work before this change and is not the canonical URL, the short period between
DNS creation and release deployment does not regress the apex service.

## Verification

Focused automated coverage will verify:

- CloudFormation contains the existing apex record plus the `www` alias and
  still does not manage the hosted zone or registration;
- a first-time installation creates and cleans up the temporary `www` ACME
  bootstrap without changing the EC2 instance resource;
- first-time certificate issuance requests both names;
- an apex-only existing certificate is expanded, while a certificate already
  covering both names is not reissued;
- Nginx sends `www` requests to the literal apex HTTPS origin and preserves
  `$request_uri`;
- the apex application proxy, security headers, and operational-route behavior
  remain present;
- template and host-asset regression smokes pass.

The single real production smoke will then verify:

- `www.epiagent.org` resolves;
- the presented certificate is valid for both hostnames;
- HTTP and HTTPS `www` requests redirect to the apex while preserving a test
  path and query string;
- the apex health and readiness endpoints return their expected successful
  responses;
- the existing Cognito sign-in and sign-out flow returns to the apex origin.

The real smoke follows the repository's one-run, five-minute limit and captures
the applicable response metadata and logs on failure without an automatic
rerun.

## Non-Goals

- Changing the canonical origin to `www.epiagent.org`.
- Adding CloudFront, an Application Load Balancer, autoscaling, or another
  runtime tier in this change.
- Changing Cognito users, authentication semantics, cookies, or application
  session storage.
- Changing domain registration or replacing the existing Route 53 hosted zone.
