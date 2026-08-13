# `www` to Apex Redirect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `www.epiagent.org` resolve securely and permanently redirect every request to the equivalent canonical `https://epiagent.org` URL.

**Architecture:** CloudFormation adds one Route 53 `A` alias from `www.${DomainName}` to the existing apex record. Nginx terminates TLS for a two-name Let's Encrypt certificate, serves the application only on the apex host, and redirects `www` while preserving `$request_uri`; the guarded release installer issues or expands the certificate only when required. A dedicated opt-in Playwright smoke validates public DNS, TLS, redirects, the compiled frontend, raw health/readiness state, and apex Cognito redirect configuration.

**Tech Stack:** AWS CloudFormation, Route 53, EC2 Elastic IP, Nginx, Certbot/Let's Encrypt, Bash, Python 3.12, pytest, Playwright 1.55, Requests, Cognito, Systems Manager, cfn-lint 1.53.1.

## Global Constraints

- Run Python tooling only through `.venv/bin/python`; the project requires Python 3.12.
- Keep `https://epiagent.org` as the only application origin and canonical browser URL.
- Keep Cognito callback `https://epiagent.org/auth/callback` and logout `https://epiagent.org/` unchanged.
- Preserve every redirect path and query string through the literal target `https://epiagent.org$request_uri`.
- Do not modify or replace the existing Route 53 hosted zone, domain registration, EC2 instance, Elastic IP, EBS volumes, Cognito users, or application session semantics.
- A CloudFormation change set must contain only the new `ApplicationWwwDnsRecord`; stop if it includes any unexpected Add, Modify, Remove, Replacement, or EC2 action.
- The live dedicated smoke has a five-minute maximum, runs exactly once, captures failure artifacts, and is never retried automatically.
- No frontend source changes are planned, so do not rebuild the production UI bundle unless a frontend input is unexpectedly modified.
- Target only AWS account `641379499556`, profile `xutao-dev`, and Region `us-east-1` through the guarded operator CLI.
- Do not execute a change set, upload a release, deploy a release, or run the production smoke until the user explicitly approves the reviewed live actions.

## File Structure

- Modify `infra/aws/phase2a/template.yaml`: own the `www` Route 53 alias and no other new infrastructure.
- Modify `deploy/aws/nginx/epi-agent.conf`: separate the canonical application virtual host from the `www` redirect virtual host.
- Modify `deploy/aws/bin/install-release.sh`: own first issuance, SAN inspection, one-time certificate expansion, temporary ACME bootstrap cleanup, and existing Nginx activation rollback.
- Modify `tests/test_aws_infrastructure.py`: assert the exact two-record DNS contract and unchanged Cognito/EC2 resources.
- Modify `scripts/smoke_aws_phase2a_template_regressions.py`: executable offline regression for the `www` alias contract.
- Modify `tests/test_aws_host_assets.py`: exercise Nginx canonicalization and all certificate lifecycle branches through the existing fake-host release harness.
- Create `scripts/smoke_www_apex_redirect_real.py`: one opt-in production browser/raw-state smoke dedicated to this feature.
- Create `tests/test_smoke_www_apex_redirect_real.py`: offline tests for smoke opt-in, URL expectations, configuration assertions, and diagnostic behavior.
- Modify `docs/aws/phase2a-runbook.md`: document the exact reviewed rollout, rollback boundary, and single-run smoke command.

---

### Task 1: Add the Route 53 `www` Alias

**Files:**
- Modify: `tests/test_aws_infrastructure.py:485-498`
- Modify: `scripts/smoke_aws_phase2a_template_regressions.py:100-227`
- Modify: `infra/aws/phase2a/template.yaml:1050-1058`

**Interfaces:**
- Consumes: existing `DomainName`, `HostedZoneId`, and `ApplicationDnsRecord` CloudFormation resources.
- Produces: `ApplicationWwwDnsRecord`, an `AWS::Route53::RecordSet` `A` alias for `www.${DomainName}` targeting the apex record in the same hosted zone.

- [ ] **Step 1: Replace the infrastructure DNS test with the exact two-record contract**

```python
def test_phase2a_dns_uses_existing_zone_and_maps_apex_and_www() -> None:
    template = phase2a_template()
    resources = template["Resources"]
    types = Counter(resource["Type"] for resource in resources.values())

    assert types["AWS::Route53::RecordSet"] == 2
    assert all(
        "HostedZone" not in resource_type and "Domains" not in resource_type
        for resource_type in types
    )
    assert resources["ApplicationDnsRecord"]["Properties"] == {
        "HostedZoneId": {"Ref": "HostedZoneId"},
        "Name": {"Ref": "DomainName"},
        "Type": "A",
        "TTL": "300",
        "ResourceRecords": [{"Ref": "ApplicationElasticIp"}],
    }
    assert resources["ApplicationWwwDnsRecord"] == {
        "Type": "AWS::Route53::RecordSet",
        "DependsOn": "ApplicationDnsRecord",
        "Properties": {
            "HostedZoneId": {"Ref": "HostedZoneId"},
            "Name": {"Fn::Sub": "www.${DomainName}"},
            "Type": "A",
            "AliasTarget": {
                "DNSName": {"Ref": "DomainName"},
                "HostedZoneId": {"Ref": "HostedZoneId"},
                "EvaluateTargetHealth": False,
            },
        },
    }
```

- [ ] **Step 2: Add the same executable contract to the template-regression smoke**

Insert immediately after `phase2a = _load("infra/aws/phase2a/template.yaml")`:

```python
    resources = phase2a["Resources"]
    www_dns = resources.get("ApplicationWwwDnsRecord")
    expected_www_dns = {
        "Type": "AWS::Route53::RecordSet",
        "DependsOn": "ApplicationDnsRecord",
        "Properties": {
            "HostedZoneId": {"Ref": "HostedZoneId"},
            "Name": {"Fn::Sub": "www.${DomainName}"},
            "Type": "A",
            "AliasTarget": {
                "DNSName": {"Ref": "DomainName"},
                "HostedZoneId": {"Ref": "HostedZoneId"},
                "EvaluateTargetHealth": False,
            },
        },
    }
    if www_dns != expected_www_dns:
        raise AssertionError("www DNS must alias the existing apex record")
    if sum(
        resource["Type"] == "AWS::Route53::RecordSet"
        for resource in resources.values()
    ) != 2:
        raise AssertionError("Phase 2A must manage only the apex and www DNS records")
```

- [ ] **Step 3: Run the focused tests and observe the intended failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_infrastructure.py::test_phase2a_dns_uses_existing_zone_and_maps_apex_and_www -q
.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
```

Expected: pytest fails because `ApplicationWwwDnsRecord` is absent, and the executable smoke exits nonzero with `www DNS must alias the existing apex record`.

- [ ] **Step 4: Add the minimal CloudFormation resource**

Insert after `ApplicationDnsRecord`:

```yaml
  ApplicationWwwDnsRecord:
    Type: AWS::Route53::RecordSet
    DependsOn: ApplicationDnsRecord
    Properties:
      HostedZoneId: !Ref HostedZoneId
      Name: !Sub 'www.${DomainName}'
      Type: A
      AliasTarget:
        DNSName: !Ref DomainName
        HostedZoneId: !Ref HostedZoneId
        EvaluateTargetHealth: false
```

- [ ] **Step 5: Run the focused tests and confirm they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_infrastructure.py::test_phase2a_dns_uses_existing_zone_and_maps_apex_and_www -q
.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
```

Expected: `1 passed` and `Phase 2A deployment-template regression smoke passed`.

- [ ] **Step 6: Commit the DNS contract**

```bash
git add infra/aws/phase2a/template.yaml tests/test_aws_infrastructure.py scripts/smoke_aws_phase2a_template_regressions.py
git commit -m "feat: add www DNS alias"
```

---

### Task 2: Make Nginx Canonicalize `www` Without Proxying It

**Files:**
- Modify: `tests/test_aws_host_assets.py:78-93`
- Modify: `deploy/aws/nginx/epi-agent.conf:1-61`

**Interfaces:**
- Consumes: the two-name certificate at `/etc/letsencrypt/live/epiagent.org/{fullchain,privkey}.pem` produced by Task 3.
- Produces: HTTP redirects for both hosts, an HTTPS-only `www` redirect host, and an apex-only HTTPS application proxy.

- [ ] **Step 1: Replace the Nginx host-asset test with canonical-host assertions**

```python
def test_nginx_only_serves_the_application_on_the_canonical_apex() -> None:
    source = _asset("deploy/aws/nginx/epi-agent.conf")

    assert source.count("return 301 https://epiagent.org$request_uri;") == 2
    assert "return 301 https://$host$request_uri;" not in source
    assert source.count("server_name epiagent.org www.epiagent.org;") == 1
    assert source.count("server_name www.epiagent.org;") == 1
    assert source.count("server_name epiagent.org;") == 1
    assert source.count("location ^~ /.well-known/acme-challenge/") == 1
    assert source.count("proxy_pass http://127.0.0.1:8000;") == 3
    www_position = source.index("server_name www.epiagent.org;")
    www_redirect_position = source.index(
        "return 301 https://epiagent.org$request_uri;", www_position
    )
    apex_position = source.index("server_name epiagent.org;", www_redirect_position)
    proxy_position = source.index("proxy_pass http://127.0.0.1:8000;", apex_position)
    assert www_position < www_redirect_position < apex_position < proxy_position
    assert "client_max_body_size 25m;" in source[apex_position:]
    assert "proxy_connect_timeout" in source[apex_position:]
    assert "proxy_read_timeout" in source[apex_position:]
    assert "add_header X-Content-Type-Options" in source[apex_position:]
    assert 'proxy_set_header Upgrade $http_upgrade' in source[apex_position:]
    assert 'proxy_set_header Connection "upgrade"' in source[apex_position:]
    assert "location = /api/health" in source[apex_position:]
    assert "location = /api/readiness" in source[apex_position:]
    assert "location = /api/ops/deployment-status { deny all; }" in source[apex_position:]
```

- [ ] **Step 2: Run the focused test and verify it fails on the current shared host**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_host_assets.py::test_nginx_only_serves_the_application_on_the_canonical_apex -q
```

Expected: FAIL because the current HTTPS server names both hosts and redirects HTTP through `$host`.

- [ ] **Step 3: Replace the Nginx asset with three explicit virtual hosts**

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name epiagent.org www.epiagent.org;
    client_max_body_size 25m;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
        default_type text/plain;
    }

    location / {
        return 301 https://epiagent.org$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name www.epiagent.org;

    ssl_certificate /etc/letsencrypt/live/epiagent.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/epiagent.org/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    return 301 https://epiagent.org$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name epiagent.org;
    client_max_body_size 25m;

    ssl_certificate /etc/letsencrypt/live/epiagent.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/epiagent.org/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Content-Security-Policy "default-src 'self'; connect-src 'self' https://cognito-idp.us-east-1.amazonaws.com https://*.auth.us-east-1.amazoncognito.com; frame-ancestors 'none'; base-uri 'self'" always;

    location = /api/ops/deployment-status { deny all; }

    location = /api/health {
        proxy_pass http://127.0.0.1:8000;
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
    }

    location = /api/readiness {
        proxy_pass http://127.0.0.1:8000;
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_connect_timeout 5s;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
```

- [ ] **Step 4: Run the Nginx and CSP asset tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_aws_host_assets.py::test_nginx_only_serves_the_application_on_the_canonical_apex \
  tests/test_aws_host_assets.py::test_nginx_csp_allows_only_required_cognito_connections -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit the canonical Nginx hosts**

```bash
git add deploy/aws/nginx/epi-agent.conf tests/test_aws_host_assets.py
git commit -m "feat: redirect www to apex in nginx"
```

---

### Task 3: Issue and Expand the Two-Name Certificate Safely

**Files:**
- Modify: `tests/test_aws_host_assets.py:263-416`
- Modify: `tests/test_aws_host_assets.py` after the release-harness tests
- Modify: `deploy/aws/bin/install-release.sh:10-54,158-265`

**Interfaces:**
- Consumes: validated `domain` and `certificate_email`, `/var/www/certbot`, the existing apex certificate when present, the first-boot apex bootstrap, and Task 2's staged Nginx configuration.
- Produces: `ensure_domain_certificate()`, a certificate named `$domain` covering both `$domain` and `www.$domain`; a transient `/etc/nginx/conf.d/epi-agent-www-acme.conf` only for first issuance; no Certbot call when the current certificate already covers both names.

- [ ] **Step 1: Extend the fake-host harness with explicit certificate states**

Change the `_release_harness` signature by adding these keyword parameters:

```python
    certificate_exists: bool = True,
    certificate_covers_www: bool = True,
    certificate_issue_fails: bool = False,
```

Add these fake commands to the dictionary in `_release_harness`:

```python
        "nginx": "exit 0\n",
        "openssl": "[ \"$TEST_CERTIFICATE_COVERS_WWW\" = 1 ]\n",
        "certbot": """printf 'certbot %s\\n' "$*" >> "$TEST_OPERATION_LOG"
[ "$TEST_CERTBOT_FAILS" = 0 ]
""",
```

Replace the unconditional fake certificate creation with:

```python
    certificate = root / "etc" / "letsencrypt" / "live" / "example.org" / "fullchain.pem"
    if certificate_exists:
        certificate.parent.mkdir(parents=True)
        certificate.write_text("fake certificate\n", encoding="utf-8")
```

Add these source rewrites after the existing `/etc/letsencrypt` rewrite:

```python
    source = source.replace("/etc/nginx", str(root / "etc" / "nginx"))
    source = source.replace("/usr/bin/certbot", "certbot")
    source = source.replace("/usr/bin/openssl", "openssl")
```

Add these values to the harness environment:

```python
        "TEST_CERTIFICATE_COVERS_WWW": "1" if certificate_covers_www else "0",
        "TEST_CERTBOT_FAILS": "1" if certificate_issue_fails else "0",
```

- [ ] **Step 2: Add failing certificate lifecycle tests**

```python
def test_release_installer_issues_both_names_on_first_certificate(
    tmp_path: Path,
) -> None:
    completed, _, _, _, _ = _release_harness(
        tmp_path,
        invalid_archive=False,
        readiness_fails=False,
        certificate_exists=False,
    )

    assert completed.returncode == 0, completed.stderr
    operations = (tmp_path / "operation.log").read_text(encoding="utf-8").splitlines()
    certbot_call = next(line for line in operations if line.startswith("certbot "))
    assert "--cert-name example.org" in certbot_call
    assert "-d example.org" in certbot_call
    assert "-d www.example.org" in certbot_call
    assert "--expand" not in certbot_call
    assert not (
        tmp_path / "host" / "etc" / "nginx" / "conf.d" / "epi-agent-www-acme.conf"
    ).exists()


def test_release_installer_expands_an_apex_only_certificate(tmp_path: Path) -> None:
    completed, _, _, _, _ = _release_harness(
        tmp_path,
        invalid_archive=False,
        readiness_fails=False,
        certificate_covers_www=False,
    )

    assert completed.returncode == 0, completed.stderr
    operations = (tmp_path / "operation.log").read_text(encoding="utf-8").splitlines()
    certbot_call = next(line for line in operations if line.startswith("certbot "))
    assert "--cert-name example.org" in certbot_call
    assert "--expand" in certbot_call
    assert "-d example.org" in certbot_call
    assert "-d www.example.org" in certbot_call


def test_release_installer_does_not_reissue_a_complete_certificate(
    tmp_path: Path,
) -> None:
    completed, _, _, _, _ = _release_harness(
        tmp_path,
        invalid_archive=False,
        readiness_fails=False,
    )

    assert completed.returncode == 0, completed.stderr
    operations = (tmp_path / "operation.log").read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("certbot ") for line in operations)


def test_release_installer_removes_temporary_www_bootstrap_on_certbot_failure(
    tmp_path: Path,
) -> None:
    completed, _, maintenance_file, _, _ = _release_harness(
        tmp_path,
        invalid_archive=False,
        readiness_fails=False,
        certificate_exists=False,
        certificate_issue_fails=True,
    )

    assert completed.returncode != 0
    assert not maintenance_file.exists()
    assert not (
        tmp_path / "host" / "etc" / "nginx" / "conf.d" / "epi-agent-www-acme.conf"
    ).exists()
```

- [ ] **Step 3: Run the four lifecycle tests and verify the installer contract is missing**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_aws_host_assets.py::test_release_installer_issues_both_names_on_first_certificate \
  tests/test_aws_host_assets.py::test_release_installer_expands_an_apex_only_certificate \
  tests/test_aws_host_assets.py::test_release_installer_does_not_reissue_a_complete_certificate \
  tests/test_aws_host_assets.py::test_release_installer_removes_temporary_www_bootstrap_on_certbot_failure -q
```

Expected: failures because the installer requests only the apex name and never inspects the certificate SAN.

- [ ] **Step 4: Add the transient bootstrap paths and cleanup at the top of the installer**

Add after the startup deadline constants:

```bash
readonly staged_nginx_config=/etc/nginx/staged/epi-agent.conf
readonly live_nginx_config=/etc/nginx/conf.d/epi-agent.conf
readonly bootstrap_nginx_config=/etc/nginx/conf.d/epi-agent-bootstrap.conf
readonly www_acme_nginx_config=/etc/nginx/conf.d/epi-agent-www-acme.conf
```

Add inside `cleanup()`, after `set +e` and before release rollback:

```bash
  if [ "${www_bootstrap_created:-false}" = true ]; then
    rm -f -- "$www_acme_nginx_config"
    nginx -t && systemctl reload nginx.service || true
  fi
```

- [ ] **Step 5: Replace apex-only issuance with SAN-aware certificate management**

Replace the current lines 235-238 with:

```bash
www_domain="www.$domain"
certificate_path="/etc/letsencrypt/live/$domain/fullchain.pem"
www_bootstrap_created=false

create_www_acme_bootstrap() {
  install -d -m 0755 /etc/nginx/conf.d
  cat >"$www_acme_nginx_config" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $www_domain;
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
        default_type text/plain;
    }
    location / { return 404; }
}
EOF
  www_bootstrap_created=true
  if nginx -t && systemctl reload nginx.service; then
    return 0
  fi
  rm -f -- "$www_acme_nginx_config"
  www_bootstrap_created=false
  nginx -t && systemctl reload nginx.service || true
  return 1
}

ensure_domain_certificate() {
  if [ ! -e "$certificate_path" ]; then
    create_www_acme_bootstrap
    /usr/bin/certbot certonly --webroot -w /var/www/certbot \
      --non-interactive --agree-tos --email "$certificate_email" \
      --cert-name "$domain" -d "$domain" -d "$www_domain"
    return 0
  fi
  if ! /usr/bin/openssl x509 -in "$certificate_path" -noout \
    -checkhost "$www_domain" >/dev/null 2>&1; then
    /usr/bin/certbot certonly --webroot -w /var/www/certbot \
      --non-interactive --agree-tos --email "$certificate_email" \
      --cert-name "$domain" --expand -d "$domain" -d "$www_domain"
  fi
}

ensure_domain_certificate
```

Remove the duplicate declarations of `staged_nginx_config`, `live_nginx_config`, and `bootstrap_nginx_config` from their old position.

- [ ] **Step 6: Integrate the transient file with atomic Nginx activation**

Change the first line of `restore_nginx_config()` and the removal in `activate_staged_nginx_config()` to:

```bash
restore_nginx_config() {
  rm -f -- "$live_nginx_config" "$bootstrap_nginx_config" "$www_acme_nginx_config"
  www_bootstrap_created=false
```

```bash
  rm -f -- "$bootstrap_nginx_config" "$www_acme_nginx_config"
  www_bootstrap_created=false
```

Keep the existing backups of the live and first-boot bootstrap configurations, `nginx -t`, reload, and rollback logic unchanged.

- [ ] **Step 7: Run the lifecycle tests and the full release-installer harness**

Run:

```bash
.venv/bin/python -m pytest tests/test_aws_host_assets.py -q
```

Expected: all host-asset tests pass, including all four new certificate branches and all existing release rollback cases.

- [ ] **Step 8: Commit the certificate lifecycle**

```bash
git add deploy/aws/bin/install-release.sh tests/test_aws_host_assets.py
git commit -m "feat: cover apex and www in TLS certificate"
```

---

### Task 4: Add the Dedicated Production Redirect Smoke and Runbook

**Files:**
- Create: `scripts/smoke_www_apex_redirect_real.py`
- Create: `tests/test_smoke_www_apex_redirect_real.py`
- Modify: `docs/aws/phase2a-runbook.md`

**Interfaces:**
- Consumes: public `http(s)://www.epiagent.org`, canonical `https://epiagent.org`, `/api/health`, `/api/readiness`, `/api/public-config`, Playwright Chromium, and the compiled frontend's `#root` and `Sign in` control.
- Produces: a guarded `main(argv: list[str] | None) -> int`, `run_live(artifact_dir: Path) -> None`, and sanitized artifacts containing redirect/status/config metadata, a browser screenshot, page text, and traceback on failure.

- [ ] **Step 1: Write offline tests for opt-in, redirect expectations, public config, and diagnostics**

Create `tests/test_smoke_www_apex_redirect_real.py`:

```python
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "smoke_www_apex_redirect_real.py"
SPEC = importlib.util.spec_from_file_location("smoke_www_apex_redirect_real", SCRIPT)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_live_smoke_requires_explicit_opt_in(monkeypatch, tmp_path: Path) -> None:
    called = False

    def fake_run_live(artifact_dir: Path) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(smoke, "run_live", fake_run_live)
    assert smoke.main(["--artifact-dir", str(tmp_path)]) == 2
    assert called is False


def test_live_smoke_uses_exact_canonical_redirects() -> None:
    assert smoke.REDIRECT_CASES == (
        (
            "http://www.epiagent.org/?domain_redirect_smoke=www",
            "https://epiagent.org/?domain_redirect_smoke=www",
        ),
        (
            "https://www.epiagent.org/?domain_redirect_smoke=www",
            "https://epiagent.org/?domain_redirect_smoke=www",
        ),
    )


def test_public_config_must_remain_on_the_apex() -> None:
    smoke.assert_public_config(
        {
            "auth_mode": "cognito",
            "cognito": {
                "redirect_uri": "https://epiagent.org/auth/callback",
                "post_logout_redirect_uri": "https://epiagent.org/",
            },
        }
    )
    with pytest.raises(AssertionError):
        smoke.assert_public_config(
            {
                "auth_mode": "cognito",
                "cognito": {
                    "redirect_uri": "https://www.epiagent.org/auth/callback",
                    "post_logout_redirect_uri": "https://epiagent.org/",
                },
            }
        )


def test_failure_diagnostics_are_written_without_secrets(tmp_path: Path) -> None:
    smoke.write_failure(
        tmp_path,
        RuntimeError("public redirect failed"),
        page=None,
        observations={"canonical": "https://epiagent.org/"},
    )
    failure = (tmp_path / "failure.txt").read_text(encoding="utf-8")
    observations = json.loads(
        (tmp_path / "observations.json").read_text(encoding="utf-8")
    )
    assert "public redirect failed" in failure
    assert observations == {"canonical": "https://epiagent.org/"}
```

- [ ] **Step 2: Run the smoke tests and verify the new script is absent**

Run:

```bash
.venv/bin/python -m pytest tests/test_smoke_www_apex_redirect_real.py -q
```

Expected: collection fails because `scripts/smoke_www_apex_redirect_real.py` does not exist.

- [ ] **Step 3: Create the complete guarded browser/raw-state smoke**

Create `scripts/smoke_www_apex_redirect_real.py`:

```python
#!/usr/bin/env python3.12
"""Run the www-to-apex production redirect smoke exactly once."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import traceback
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests


CANONICAL_ORIGIN = "https://epiagent.org"
WWW_ORIGIN = "https://www.epiagent.org"
SMOKE_QUERY = "?domain_redirect_smoke=www"
REDIRECT_CASES = (
    (f"http://www.epiagent.org/{SMOKE_QUERY}", f"{CANONICAL_ORIGIN}/{SMOKE_QUERY}"),
    (f"{WWW_ORIGIN}/{SMOKE_QUERY}", f"{CANONICAL_ORIGIN}/{SMOKE_QUERY}"),
)
MAX_SECONDS = 300


def assert_public_config(payload: dict[str, Any]) -> None:
    cognito = payload.get("cognito") or {}
    assert payload.get("auth_mode") == "cognito", "production auth mode changed"
    assert cognito.get("redirect_uri") == f"{CANONICAL_ORIGIN}/auth/callback"
    assert cognito.get("post_logout_redirect_uri") == f"{CANONICAL_ORIGIN}/"


def assert_redirect(source: str, expected: str) -> dict[str, Any]:
    response = requests.get(source, allow_redirects=False, timeout=15)
    location = response.headers.get("Location", "")
    assert response.status_code == 301, f"{source} returned HTTP {response.status_code}"
    assert location == expected, f"{source} redirected to {location!r}"
    return {"source": source, "status": response.status_code, "location": location}


def _launch_browser(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch()
    except Exception as error:
        if "Executable doesn't exist" not in str(error):
            raise
        return playwright.chromium.launch(channel="chrome")


def write_failure(
    artifact_dir: Path,
    error: BaseException,
    *,
    page: Any | None,
    observations: dict[str, Any],
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "failure.txt").write_text(
        "".join(traceback.format_exception(error)),
        encoding="utf-8",
    )
    (artifact_dir / "observations.json").write_text(
        json.dumps(observations, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if page is not None:
        try:
            (artifact_dir / "failure-page.txt").write_text(
                page.locator("body").inner_text(),
                encoding="utf-8",
            )
            page.screenshot(
                path=str(artifact_dir / "failure-screenshot.png"),
                full_page=True,
            )
        except Exception:
            pass


def run_live(artifact_dir: Path) -> None:
    from playwright.sync_api import sync_playwright

    artifact_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    observations: dict[str, Any] = {"redirects": []}
    page: Any | None = None
    browser: Any | None = None
    try:
        for source, expected in REDIRECT_CASES:
            observations["redirects"].append(assert_redirect(source, expected))

        health = requests.get(f"{CANONICAL_ORIGIN}/api/health", timeout=15)
        readiness = requests.get(f"{CANONICAL_ORIGIN}/api/readiness", timeout=15)
        public_config_response = requests.get(
            f"{CANONICAL_ORIGIN}/api/public-config", timeout=15
        )
        health.raise_for_status()
        readiness.raise_for_status()
        public_config_response.raise_for_status()
        readiness_payload = readiness.json()
        public_config = public_config_response.json()
        assert readiness_payload.get("status") == "ready"
        assert_public_config(public_config)
        observations["health_status"] = health.status_code
        observations["readiness"] = readiness_payload
        observations["public_config"] = {
            "auth_mode": public_config.get("auth_mode"),
            "redirect_uri": (public_config.get("cognito") or {}).get("redirect_uri"),
            "post_logout_redirect_uri": (public_config.get("cognito") or {}).get(
                "post_logout_redirect_uri"
            ),
        }

        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1440, "height": 950})
            page.goto(REDIRECT_CASES[1][0], wait_until="domcontentloaded", timeout=30_000)
            assert page.url == REDIRECT_CASES[1][1]
            page.locator("#root").wait_for(state="attached", timeout=15_000)
            page.get_by_role("button", name="Sign in").wait_for(timeout=15_000)
            page.screenshot(path=str(artifact_dir / "canonical-page.png"), full_page=True)
            observations["browser_final_url"] = page.url
            observations["browser_page_text"] = page.locator("body").inner_text()
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url("**.amazoncognito.com/**", timeout=30_000)
            authorization_url = urlparse(page.url)
            redirect_uri = parse_qs(authorization_url.query).get("redirect_uri", [""])[0]
            assert redirect_uri == f"{CANONICAL_ORIGIN}/auth/callback"
            observations["signin_redirect_uri"] = redirect_uri
            browser.close()
            browser = None

        elapsed = time.monotonic() - started
        assert elapsed <= MAX_SECONDS, f"smoke exceeded {MAX_SECONDS} seconds"
        observations["elapsed_seconds"] = round(elapsed, 3)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "observations.json").write_text(
            json.dumps(observations, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except BaseException as error:
        write_failure(
            artifact_dir,
            error,
            page=page,
            observations=observations,
        )
        raise
    finally:
        if browser is not None:
            browser.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live-aws", action="store_true")
    parser.add_argument("--artifact-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    if not arguments.allow_live_aws:
        print("error: --allow-live-aws is required", file=sys.stderr)
        return 2
    try:
        run_live(arguments.artifact_dir)
    except BaseException as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("PASS www-to-apex production browser smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the offline smoke tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_smoke_www_apex_redirect_real.py -q
```

Expected: `4 passed`; no public network call occurs.

- [ ] **Step 5: Add the exact runbook section**

Add a `## Add the www compatibility redirect` section to `docs/aws/phase2a-runbook.md` containing:

````markdown
## Add the www compatibility redirect

Keep `https://epiagent.org` canonical. The `www` record, expanded certificate,
and Nginx redirect must roll out in that order. First create a reviewed
CloudFormation change set:

```sh
.venv/bin/python scripts/aws_phase2a.py plan-stack \
  --domain-name epiagent.org \
  --hosted-zone-id Z02132461LVJ2PFOYFXFU \
  --certificate-email xw488@njms.rutgers.edu \
  --alert-email xw488@njms.rutgers.edu \
  --data-volume-gib 50
```

Require exactly one infrastructure action: Add
`ApplicationWwwDnsRecord` (`AWS::Route53::RecordSet`). Stop if the change set
modifies or replaces EC2, EIP, EBS, Cognito, the hosted zone, or any unrelated
resource. Execute only after explicit review and account confirmation:

```sh
.venv/bin/python scripts/aws_phase2a.py execute-change-set "$DOMAIN_CHANGE_SET_ARN" \
  --confirm-account 641379499556
```

After the stack reaches `UPDATE_COMPLETE`, confirm `www.epiagent.org` resolves,
then upload and deploy the clean committed release through the existing guarded
commands. The installer expands the certificate once and fails closed before
activating Nginx if ACME validation fails.

Run the dedicated production smoke exactly once; never retry it automatically:

```sh
.venv/bin/python scripts/smoke_www_apex_redirect_real.py \
  --allow-live-aws \
  --artifact-dir artifacts/www-apex-redirect
```

On failure, preserve `artifacts/www-apex-redirect`, the exact SSM command ID,
and the applicable Nginx/Certbot logs. Do not remove the apex record or existing
certificate. Roll back only the release/Nginx configuration after reviewing
whether the two-name certificate has already been issued; removing the `www`
record is a separate reviewed CloudFormation change.
````

Use `$DOMAIN_CHANGE_SET_ARN` only as an operator-provided shell variable containing the exact ARN emitted by the reviewed plan; do not place it in repository files.

- [ ] **Step 6: Add a runbook contract test**

```python
def test_phase2a_runbook_has_guarded_www_redirect_rollout() -> None:
    source = _asset("docs/aws/phase2a-runbook.md")

    assert "ApplicationWwwDnsRecord" in source
    assert "--hosted-zone-id Z02132461LVJ2PFOYFXFU" in source
    assert "--confirm-account 641379499556" in source
    assert "smoke_www_apex_redirect_real.py" in source
    assert "never retry it automatically" in source
    assert "modifies or replaces EC2" in source
```

- [ ] **Step 7: Run the smoke and runbook contract tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_smoke_www_apex_redirect_real.py \
  tests/test_aws_host_assets.py::test_phase2a_runbook_has_guarded_www_redirect_rollout -q
```

Expected: `5 passed`.

- [ ] **Step 8: Commit the dedicated smoke and operator documentation**

```bash
git add scripts/smoke_www_apex_redirect_real.py tests/test_smoke_www_apex_redirect_real.py tests/test_aws_host_assets.py
git add -f docs/aws/phase2a-runbook.md
git commit -m "test: add www redirect production smoke"
```

---

### Task 5: Verify the Complete Repository Change

**Files:**
- Verify only; no file changes expected.

**Interfaces:**
- Consumes: Tasks 1-4 as a clean sequence of commits.
- Produces: evidence that infrastructure, host assets, release building, existing authentication contracts, and the dedicated smoke contract all pass before any AWS mutation.

- [ ] **Step 1: Confirm scope and a clean tracked tree**

Run:

```bash
git status --short --branch
git diff aws-private/aws-test...HEAD --stat
git log --oneline aws-private/aws-test..HEAD
```

Expected: only the approved design/plan, Route 53 alias, Nginx, release installer, tests, dedicated smoke, and runbook appear; no tracked changes are uncommitted.

- [ ] **Step 2: Run focused and adjacent pytest coverage**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_aws_infrastructure.py \
  tests/test_aws_host_assets.py \
  tests/test_aws_phase2a_cli.py \
  tests/test_build_aws_release.py \
  tests/test_smoke_aws_phase2a_real.py \
  tests/test_smoke_www_apex_redirect_real.py \
  tests/test_smoke_multi_user_isolation_real.py -q
```

Expected: all selected tests pass with no failure or error.

- [ ] **Step 3: Run the executable template regression smoke**

Run:

```bash
.venv/bin/python scripts/smoke_aws_phase2a_template_regressions.py
```

Expected: `Phase 2A deployment-template regression smoke passed`.

- [ ] **Step 4: Validate both CloudFormation templates without executing them**

Run:

```bash
.venv/bin/python scripts/aws_phase2a.py validate
```

Expected: cfn-lint and AWS `validate-template` succeed; this command validates only and does not create or execute a change set.

- [ ] **Step 5: Build the immutable release from the clean committed tree**

Run:

```bash
.venv/bin/python scripts/build_aws_release.py
```

Expected: three paths under `dist/aws`; each filename embeds the same 40-character commit ID and uses the `.tar.gz`, `.sha256`, or `.json` suffix. Confirm the manifest `commit_sha` equals `git rev-parse HEAD` and its `archive_sha256` equals the archive's SHA-256.

---

### Task 6: Review and Perform the Guarded Live Rollout

**Files:**
- No repository edits; this task changes reviewed AWS state and writes local artifacts under ignored `dist/aws/` and `artifacts/www-apex-redirect/`.

**Interfaces:**
- Consumes: the clean verified release from Task 5, the existing Phase 2A stack, hosted zone `Z02132461LVJ2PFOYFXFU`, and operator credentials supplied through profile `xutao-dev`.
- Produces: one `www` DNS alias, one expanded two-name certificate, canonical Nginx redirect behavior, a recorded SSM command ID, and one dedicated smoke artifact set.

- [ ] **Step 1: Create—but do not execute—the application change set**

Run:

```bash
.venv/bin/python scripts/aws_phase2a.py plan-stack \
  --domain-name epiagent.org \
  --hosted-zone-id Z02132461LVJ2PFOYFXFU \
  --certificate-email xw488@njms.rutgers.edu \
  --alert-email xw488@njms.rutgers.edu \
  --data-volume-gib 50
```

Expected: a `CREATE_COMPLETE` change set whose only action is Add `ApplicationWwwDnsRecord` of type `AWS::Route53::RecordSet`. Record its exact ARN. Any other action is a hard stop.

- [ ] **Step 2: Stop and obtain explicit user approval for the exact live actions**

Present the change-set ARN and its complete action summary. Ask for explicit approval to execute that ARN, upload the immutable release, deploy it once through SSM, and run the dedicated production smoke once. Do not continue on general approval that does not identify the reviewed change set.

- [ ] **Step 3: Execute only the approved change set and wait for completion**

After approval, start an interactive shell command, paste the exact reviewed ARN
as its one input line, and execute:

```bash
IFS= read -r domain_change_set_arn
case "$domain_change_set_arn" in
  arn:aws:cloudformation:us-east-1:641379499556:changeSet/*/*) ;;
  *) printf '%s\n' 'invalid change-set ARN' >&2; exit 64 ;;
esac
.venv/bin/python scripts/aws_phase2a.py execute-change-set "$domain_change_set_arn" \
  --confirm-account 641379499556
aws cloudformation wait stack-update-complete \
  --stack-name epi-agent-phase2a \
  --profile xutao-dev \
  --region us-east-1
```

The pasted value comes from Step 1 and must match the ARN approved in Step 2
byte-for-byte. Expected: the stack reaches `UPDATE_COMPLETE` without EC2
replacement or modification.

- [ ] **Step 4: Confirm DNS convergence before certificate expansion**

Run:

```bash
dig +short epiagent.org A
dig +short www.epiagent.org A
```

Expected: both commands return the stack's same current Elastic IP. If `www` does not resolve, stop; do not invoke Certbot through deployment.

- [ ] **Step 5: Derive and verify immutable release coordinates locally**

Run:

```bash
domain_release_id="$(git rev-parse HEAD)"
domain_release_archive="dist/aws/epi-agent-${domain_release_id}.tar.gz"
domain_release_sha="$(shasum -a 256 "$domain_release_archive" | awk '{print $1}')"
test "$(.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit_sha"])' "dist/aws/epi-agent-${domain_release_id}.json")" = "$domain_release_id"
test "$(.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["archive_sha256"])' "dist/aws/epi-agent-${domain_release_id}.json")" = "$domain_release_sha"
```

Expected: both `test` commands return zero and print nothing.

- [ ] **Step 6: Upload the release once and deploy it once through SSM**

Run:

```bash
.venv/bin/python scripts/aws_phase2a.py upload-release \
  "$domain_release_archive" "releases/${domain_release_id}.tar.gz"
.venv/bin/python scripts/aws_phase2a.py deploy-release \
  "releases/${domain_release_id}.tar.gz" \
  "$domain_release_sha" \
  "$domain_release_id" \
  epiagent.org \
  xw488@njms.rutgers.edu
```

Expected: one SSM deployment reaches `Success`. Record the command ID from AWS/CloudTrail or the command invocation. On `Failed`, `TimedOut`, or `Cancelled`, retrieve that exact invocation and Nginx/Certbot logs once, preserve them, and stop without resending the command.

- [ ] **Step 7: Run the dedicated public production smoke exactly once**

Run once:

```bash
.venv/bin/python scripts/smoke_www_apex_redirect_real.py \
  --allow-live-aws \
  --artifact-dir artifacts/www-apex-redirect
```

Expected: `PASS www-to-apex production browser smoke`; `observations.json` records both 301 redirects, health 200, readiness `ready`, apex Cognito callback/logout configuration, browser final URL on the apex, and apex `redirect_uri` in the Cognito authorization request.

On failure, preserve the artifact directory and do not rerun. Diagnose from the captured page, response metadata, Nginx logs, Certbot logs, certificate SAN, and the single SSM invocation.

- [ ] **Step 8: Verify the two-name certificate and final canonical response independently**

Run:

```bash
printf '' | openssl s_client -connect www.epiagent.org:443 -servername www.epiagent.org 2>/dev/null \
  | openssl x509 -noout -checkhost www.epiagent.org
curl --silent --show-error --head --max-time 15 \
  'https://www.epiagent.org/?final_www_check=1'
curl --silent --show-error --max-time 15 \
  'https://epiagent.org/api/readiness'
```

Expected: OpenSSL reports `Hostname www.epiagent.org does match certificate`; curl reports `301` with `Location: https://epiagent.org/?final_www_check=1`; readiness returns JSON with `"status":"ready"`.

- [ ] **Step 9: Report the rollout without pushing or merging**

Report the implementation commit IDs, change-set ARN, stack status, SSM command ID/status, DNS answers, certificate hostname result, redirect targets, apex health/readiness, and the single dedicated smoke outcome/artifact path. Leave the branch unpushed and unmerged unless the user separately requests publication.
