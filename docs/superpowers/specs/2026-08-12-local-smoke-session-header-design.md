# Local smoke session-header design

## Context

The local API now requires a canonical `X-Epi-Session-ID` header on every
protected route. The browser client already supplies the fixed local session
UUID, but the active-run cancellation and agent-activity smoke scripts inspect
API state with raw `requests` calls that omit the header. The browser workflow
therefore succeeds while the smoke's postcondition check receives HTTP 400.

## Scope

Correct only the two local smoke harnesses:

- `scripts/e2e_active_run_cancellation_real.py`
- `scripts/e2e_agent_activity_timeline_real.py`

Production authentication, API routing, browser requests, and AWS
infrastructure remain unchanged.

## Design

Each smoke will define one local API header mapping using the canonical fixed
session UUID from `api.auth.LOCAL_SESSION_ID`. Every raw request to a protected
conversation or thread-state route will pass that mapping. Public health
requests will remain headerless so the smoke continues proving that health is
public.

The mapping will be passed explicitly at each protected request boundary. This
keeps authentication requirements visible and avoids a process-wide requests
session whose implicit behavior could hide future contract changes.

## Error handling and security

The server will continue rejecting missing, malformed, or noncanonical session
IDs. The smoke will not bypass identity checks or weaken local/Cognito
authentication. Hosted browser requests will continue sending both the Cognito
bearer token and browser-session UUID through the existing authenticated fetch
wrapper.

## Verification

Focused tests will patch the smoke modules' HTTP client and assert that both
conversation-list and thread-state requests carry the canonical local session
header. The tests must fail against the current harness before implementation
and pass afterward.

After focused and broader unit verification, each applicable real browser smoke
will run once. A failed real smoke will preserve its diagnostics and will not be
retried automatically. Deployment remains blocked until the fixed harness
validates the exact release boundary.
