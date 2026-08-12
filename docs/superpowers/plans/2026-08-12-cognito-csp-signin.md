# Cognito CSP Sign-In Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permit the production frontend to reach the required AWS Cognito OIDC endpoints so the sign-in button redirects users to managed login in all modern CSP-enforcing browsers.

**Architecture:** Preserve the existing restrictive Nginx Content Security Policy and add a dedicated region-scoped `connect-src` allowlist for the Cognito issuer and managed-login service. Ship the corrected tracked Nginx asset through the existing immutable release path; no CloudFormation or authentication-flow change is required.

**Tech Stack:** Nginx, CSP Level 3, AWS Cognito/OIDC, Python 3.12, pytest, Playwright/Chrome, deterministic tar/gzip, S3, SSM, systemd.

## Global Constraints

- Work only in the existing isolated `aws-execution` worktree on branch `aws-test`.
- Keep `default-src 'self'`, `frame-ancestors 'none'`, and `base-uri 'self'` intact.
- The exact new directive is `connect-src 'self' https://cognito-idp.us-east-1.amazonaws.com https://*.auth.us-east-1.amazoncognito.com`.
- Do not remove CSP or allow all AWS, Amazon, `https:`, or wildcard top-level destinations.
- Modify only `deploy/aws/nginx/epi-agent.conf` and `tests/test_aws_host_assets.py` for the implementation commit.
- Do not change CloudFormation, Cognito resources/users/passwords, authentication logic, callback URIs, APIs, schemas, systemd, EC2/EBS configuration, study data, or user data.
- Production remains native Python/systemd; Docker Desktop is only a temporary local Linux test harness and must be stopped after testing.
- Build twice from a clean commit and require byte-identical archives before producing the handoff artifact.
- Upload one new commit-specific S3 object and deploy it exactly once. Do not retry automatically if the deployment fails.
- Preserve EC2 in the running state and preserve both encrypted EBS volumes.
- Verify the live CSP header and reproduce the sign-in click in a clean browser before reporting completion.

## File Map

- Modify `tests/test_aws_host_assets.py`: encode the exact CSP contract that permits Cognito without weakening other directives.
- Modify `deploy/aws/nginx/epi-agent.conf`: add the required `connect-src` directive to the existing response header.
- Use unchanged `scripts/build_aws_release.py`: create deterministic commit-specific release archives.
- Use unchanged `scripts/aws_phase2a.py`: perform account-guarded upload and single deployment.
- Create ignored local artifacts only under `dist/aws/`: reproducibility builds, handoff archive, checksums, manifests, and the browser probe.

---

### Task 1: Add the strict Cognito CSP connection allowlist

**Files:**
- Modify: `tests/test_aws_host_assets.py`
- Modify: `deploy/aws/nginx/epi-agent.conf`

**Interfaces:**
- Consumes: the existing Nginx `Content-Security-Policy` header in `deploy/aws/nginx/epi-agent.conf`.
- Produces: the exact browser policy `default-src 'self'; connect-src 'self' https://cognito-idp.us-east-1.amazonaws.com https://*.auth.us-east-1.amazoncognito.com; frame-ancestors 'none'; base-uri 'self'`.

- [ ] **Step 1: Add the failing CSP regression test**

Add this focused test immediately after `test_nginx_only_exposes_safe_operational_routes`:

```python
def test_nginx_csp_allows_only_required_cognito_connections() -> None:
    source = _asset("deploy/aws/nginx/epi-agent.conf")
    expected = (
        'add_header Content-Security-Policy "default-src \'self\'; '
        "connect-src 'self' https://cognito-idp.us-east-1.amazonaws.com "
        "https://*.auth.us-east-1.amazoncognito.com; "
        "frame-ancestors 'none'; base-uri 'self'\" always;"
    )

    assert expected in source
    assert "connect-src *" not in source
    assert "connect-src https:" not in source
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_host_assets.py::test_nginx_csp_allows_only_required_cognito_connections -q
```

Expected: one assertion failure because the tracked Nginx header has no
`connect-src` directive. A collection, import, or syntax error is not an
acceptable RED result.

- [ ] **Step 3: Make the minimal Nginx asset change**

Replace the existing CSP header with this exact line:

```nginx
    add_header Content-Security-Policy "default-src 'self'; connect-src 'self' https://cognito-idp.us-east-1.amazonaws.com https://*.auth.us-east-1.amazoncognito.com; frame-ancestors 'none'; base-uri 'self'" always;
```

Do not edit any other Nginx directive.

- [ ] **Step 4: Verify GREEN and focused host behavior**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_aws_host_assets.py -q
git diff --check
```

Expected: every host-asset test passes and `git diff --check` exits `0`.

- [ ] **Step 5: Verify the exact two-file diff and commit**

Run:

```bash
git status --short
git diff -- deploy/aws/nginx/epi-agent.conf tests/test_aws_host_assets.py
git diff --name-only
```

Expected: the implementation diff contains exactly the two permitted files and
the CSP still contains the original default, frame-ancestor, and base rules.

Commit:

```bash
git add deploy/aws/nginx/epi-agent.conf
git add -f tests/test_aws_host_assets.py
git commit -m "fix: allow Cognito connections in browser CSP"
```

Expected: one focused commit and a clean tracked worktree.

---

### Task 2: Run the full gate and prepare a reproducible release

**Files:**
- Verify: application, AWS, frontend, and release tooling.
- Create ignored: `dist/aws/csp-repro-one/`, `dist/aws/csp-repro-two/`, and commit-specific handoff artifacts in `dist/aws/`.

**Interfaces:**
- Consumes: the clean Task 1 commit.
- Produces: a verified release ID, archive SHA-256, size, manifest, and one byte-identical handoff archive ready for S3.

- [ ] **Step 1: Start Docker Desktop only if the local harness is unavailable**

Run `docker info`. If the Docker API is unavailable, start Docker Desktop and
poll the same command until it succeeds. Do not create or change the production
deployment architecture.

- [ ] **Step 2: Run the complete backend and AWS regression gate**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python -m pytest tests/test_api_deployment.py tests/test_api_server.py tests/test_aws_phase2a_cli.py tests/test_aws_infrastructure.py tests/test_aws_study_access_recovery.py tests/test_aws_study_installer.py tests/test_aws_host_assets.py tests/test_build_aws_release.py tests/test_smoke_aws_phase2a_real.py tests/test_smoke_multi_user_isolation_real.py -q
```

Expected: all selected tests pass, including the Docker-backed local Linux
harness and the new CSP test.

- [ ] **Step 3: Run executable deployment smokes and syntax checks**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_release_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_study_installer.py
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/smoke_aws_service_entrypoint.py
bash -n deploy/aws/bin/install-release.sh deploy/aws/bin/install-study.sh deploy/aws/bin/epi-agent-python-worker
git diff --check
```

Expected: every smoke reports success; shell syntax and diff checks exit `0`.

- [ ] **Step 4: Run frontend tests and production build**

From `frontend/`, run:

```bash
npm test -- --run
npm run build -- --emptyOutDir false
```

Expected: all frontend tests pass and Vite completes a production build.

- [ ] **Step 5: Stop Docker Desktop after the Linux-harness tests**

If this task started Docker Desktop, run `docker desktop stop` and confirm
`docker info` can no longer connect. This has no effect on EC2.

- [ ] **Step 6: Build twice and prove deterministic identity**

From the repository root, require a clean tracked tree, then run:

```bash
git status --short --branch
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/build_aws_release.py --output-dir dist/aws/csp-repro-one
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/build_aws_release.py --output-dir dist/aws/csp-repro-two
release_id=$(git rev-parse HEAD)
cmp "dist/aws/csp-repro-one/epi-agent-${release_id}.tar.gz" "dist/aws/csp-repro-two/epi-agent-${release_id}.tar.gz"
shasum -a 256 "dist/aws/csp-repro-one/epi-agent-${release_id}.tar.gz" "dist/aws/csp-repro-two/epi-agent-${release_id}.tar.gz"
```

Expected: tracked status is clean, both builder runs succeed, `cmp` exits `0`,
and both SHA-256 values are identical.

- [ ] **Step 7: Build and verify the handoff artifact**

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/build_aws_release.py --output-dir dist/aws
release_id=$(git rev-parse HEAD)
cd dist/aws
shasum -a 256 -c "epi-agent-${release_id}.sha256"
cd ../..
cmp "dist/aws/csp-repro-one/epi-agent-${release_id}.tar.gz" "dist/aws/epi-agent-${release_id}.tar.gz"
release_sha=$(awk '{print $1}' "dist/aws/epi-agent-${release_id}.sha256")
test "${#release_sha}" -eq 64
stat -f 'archive-size=%z' "dist/aws/epi-agent-${release_id}.tar.gz"
git status --short --branch
```

Expected: the sidecar reports `OK`, the handoff archive is byte-identical to
the reproduction archive, its root `release.json.commit_sha` equals
`release_id`, and tracked Git status remains clean. Record release ID, SHA-256,
size, archive path, and manifest before any AWS mutation.

---

### Task 3: Upload once, deploy once, and verify the browser flow

**Files:**
- Use unchanged: `scripts/aws_phase2a.py`
- Create ignored: `dist/aws/csp-signin-probe.py`
- No tracked repository modifications.

**Interfaces:**
- Consumes: the exact Task 2 `release_id`, archive, `release_sha`, size, and S3 key `releases/${release_id}.tar.gz`.
- Produces: one S3 version, one successful SSM deployment command, a live CSP header, and a clean-browser transition to Cognito managed login.

- [ ] **Step 1: Perform read-only AWS and production preflight**

Require all of the following before mutation:

- `scripts/aws_phase2a.py identity` returns account `641379499556` and
  principal `arn:aws:iam::641379499556:user/xutao-dev`;
- EC2 `i-0f9ed9c133ea2358b` is `running` and SSM is `Online`;
- encrypted volumes `vol-0cb3e1bb3af5e4245` and
  `vol-043235ec767877900` are attached;
- public `/api/health` is `ok`, `/api/readiness` reports the previous exact
  release, and `/` is HTTP `200`;
- Cognito user `94d82418-0061-7036-b1c1-f476f85b4036` remains enabled with
  email `xw488@njms.rutgers.edu`;
- `head-object` returns 404 for the new exact S3 key.

Stop before upload if any identity, resource, or immutability check differs.

- [ ] **Step 2: Upload the verified object exactly once**

With the recorded runtime values, run exactly once:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py upload-release "dist/aws/epi-agent-${release_id}.tar.gz" "releases/${release_id}.tar.gz"
```

Then use `head-object` and `list-object-versions` to require exact archive size,
exact `sha256` metadata, `AES256`, one nonempty version ID, exactly one object
version for the key, and no delete marker.

- [ ] **Step 3: Deploy the verified release exactly once**

Run this guarded command once, substituting the recorded 64-character digest
for `release_sha` through the shell variable:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python scripts/aws_phase2a.py deploy-release "releases/${release_id}.tar.gz" "${release_sha}" "${release_id}" epiagent.org xw488@njms.rutgers.edu
```

Do not resend if it exits nonzero. Retrieve SSM history afterward, require
exactly one `epi-agent-deploy-release` command whose `ReleaseId` is the new
commit, record its command ID, and require `Status=Success` and `ResponseCode=0`.

- [ ] **Step 4: Verify the exact live release and security header**

Require:

```bash
curl --fail --silent --show-error --max-time 20 https://epiagent.org/api/health
curl --fail --silent --show-error --max-time 20 https://epiagent.org/api/readiness
curl --fail --silent --show-error --head --max-time 20 https://epiagent.org/ | tr -d '\r' | rg -i '^content-security-policy:'
```

Expected: health is `ok`; readiness is `ready` with `release_id` equal to the
new commit; the CSP header exactly retains the three prior directives and adds
the strict two-origin Cognito `connect-src` allowlist.

- [ ] **Step 5: Reproduce the sign-in click in a clean browser**

Create ignored `dist/aws/csp-signin-probe.py` with this complete probe:

```python
from playwright.sync_api import sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, channel="chrome")
    page = browser.new_page()
    browser_errors: list[str] = []
    page.on("pageerror", lambda error: browser_errors.append(f"pageerror: {error}"))
    page.on(
        "console",
        lambda message: (
            browser_errors.append(f"console: {message.text}")
            if message.type == "error"
            else None
        ),
    )
    page.goto("https://epiagent.org", wait_until="networkidle")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("https://*.auth.us-east-1.amazoncognito.com/**", timeout=20_000)
    assert page.url.startswith(
        "https://epi-agent-phase2a-641379499556.auth.us-east-1.amazoncognito.com/"
    )
    assert not any("Content Security Policy" in item for item in browser_errors)
    print(f"managed-login-url={page.url.split('?', 1)[0]}")
    print("csp-errors=0")
    browser.close()
```

Run:

```bash
/Users/xutaowang/Desktop/RA\ work/Epi-Agent/Epi-AI-Agent/.venv/bin/python dist/aws/csp-signin-probe.py
```

Expected: the browser leaves `epiagent.org`, reaches the exact Cognito managed
login host, and reports zero CSP errors. Do not enter, request, or expose the
temporary Cognito credential.

- [ ] **Step 6: Verify preserved infrastructure and user state**

Re-read EC2 and EBS state and require the same running instance and two encrypted
attached volumes. Re-read the Cognito user and require the same username, exact
email, enabled state, and existing status without modifying credentials. Require
`git status --short --branch` to remain clean.

- [ ] **Step 7: Record the operational handoff**

Report the new release ID, archive SHA, S3 version ID, SSM command ID, public
health/readiness results, exact CSP header, browser destination host, instance
state, EBS identities, Cognito state, tests, and review results. Tell the user to
refresh `https://epiagent.org`, click **Sign in**, use the emailed temporary
credential, and set a permanent password of at least 14 characters. Never ask
the user to disclose that password.
