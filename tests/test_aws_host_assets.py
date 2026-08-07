from __future__ import annotations

from pathlib import Path


def test_python_worker_launcher_enforces_the_fixed_privilege_boundary() -> None:
    launcher = (
        Path(__file__).parents[1] / "deploy" / "aws" / "bin" / "epi-agent-python-worker"
    )
    source = launcher.read_text(encoding="utf-8")

    assert "realpath -e" in source
    assert "/srv/epi-agent/runtime/users" in source
    assert "/threads/" in source
    assert "/execution/" in source
    assert "find -P" in source
    assert "stat -c %u" in source
    assert "env -i" in source
    assert "runuser --user epi-agent-exec" in source
    assert "/opt/epi-agent/current/epi_agent/runtimes/python/worker.py" in source
    web_acl_command = "runuser --user epi-agent-web -- /usr/bin/setfacl"
    assert source.count(web_acl_command) == 3
    assert "\n/usr/bin/setfacl" not in source
    child_acl_command = f"{web_acl_command} -R"
    assert source.count(child_acl_command) == 2
    run_dir_acl_command = (
        "runuser --user epi-agent-web -- /usr/bin/setfacl "
        '-m "u:$worker_user:rx" "$run_dir"'
    )
    assert source.count(run_dir_acl_command) == 1
    assert source.index(run_dir_acl_command) < source.index(child_acl_command)
    assert source.index(child_acl_command) < source.index(
        "runuser --user epi-agent-exec"
    )
    assert 'eval ' not in source
    assert 'bash -c' not in source


ROOT = Path(__file__).parents[1]


def _asset(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_web_service_runs_the_api_with_least_privilege() -> None:
    source = _asset("deploy/aws/systemd/epi-agent.service")

    assert "User=epi-agent-web" in source
    assert "Group=epi-agent-web" in source
    assert "EnvironmentFile=/etc/epi-agent/app.env" in source
    assert "--host 127.0.0.1 --port 8000" in source
    assert "--workers 1" in source
    assert "UMask=0027" in source
    assert "NoNewPrivileges=true" in source
    assert "ReadWritePaths=/srv/epi-agent/runtime /srv/epi-agent/study_data /run/epi-agent" in source
    assert "ReadOnlyPaths=/opt/epi-agent/current" in source
    assert "ReadWritePaths=/opt/epi-agent/releases" not in source


def test_nginx_only_exposes_safe_operational_routes() -> None:
    source = _asset("deploy/aws/nginx/epi-agent.conf")

    assert "client_max_body_size" in source
    assert "proxy_connect_timeout" in source
    assert "proxy_read_timeout" in source
    assert "add_header X-Content-Type-Options" in source
    assert "proxy_http_version 1.1" in source
    assert 'proxy_set_header Upgrade $http_upgrade' in source
    assert 'proxy_set_header Connection "upgrade"' in source
    assert "location ^~ /.well-known/acme-challenge/" in source
    assert "return 301 https://$host$request_uri" in source
    assert "location = /api/health" in source
    assert "location = /api/readiness" in source
    assert "location = /api/ops/deployment-status { deny all; }" in source


def test_environment_example_has_only_hosted_non_secret_configuration() -> None:
    source = _asset("deploy/aws/env/app.env.example")
    expected = """REPORT_AGENT_AUTH_MODE=cognito
REPORT_AGENT_AWS_REGION=us-east-1
REPORT_AGENT_COGNITO_USER_POOL_ID=us-east-1_example
REPORT_AGENT_COGNITO_APP_CLIENT_ID=example
REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT=https://example.auth.us-east-1.amazoncognito.com/logout
REPORT_AGENT_AUTH_REDIRECT_URI=https://epiagent.org/auth/callback
REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI=https://epiagent.org/
REPORT_AGENT_CORS_ALLOW_ORIGIN_REGEX=^https://epiagent\\.org$
REPORT_AGENT_RUNTIME_ROOT=/srv/epi-agent/runtime
REPORT_AGENT_CHECKPOINT_DB_PATH=/srv/epi-agent/runtime/agent_memory_fastapi.db
REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data
REPORT_AGENT_STATIC_DIR=/opt/epi-agent/current/frontend/dist
REPORT_AGENT_WEB_CONCURRENCY=1
REPORT_AGENT_MAINTENANCE_FILE=/run/epi-agent/maintenance
REPORT_AGENT_PYTHON_WORKER_LAUNCHER=/usr/bin/sudo -n /usr/local/libexec/epi-agent-python-worker
"""
    assert source == expected
    assert "SECRET" not in source
    assert "ACCESS_KEY" not in source
    assert "OPENAI_API_KEY" not in source


def test_release_installer_enforces_a_safe_atomic_activation_contract() -> None:
    source = _asset("deploy/aws/bin/install-release.sh")

    assert '"$(id -u)" -eq 0' in source
    assert '^[0-9a-f]{40}$' in source
    assert '^[0-9a-f]{64}$' in source
    assert "install -m 0640" in source
    assert "/api/ops/deployment-status" in source
    assert "drain_deadline_seconds=600" in source
    assert "rm -f -- \"$maintenance_file\"" in source
    assert "mktemp -d /opt/epi-agent/staging.XXXXXX" in source
    assert "aws s3 cp" in source
    assert "sha256sum -c" in source
    assert "tarfile" in source
    assert "member.issym()" in source
    assert "member.islnk()" in source
    assert "uv venv --python 3.12" in source
    assert "uv pip install --python \"$venv_python\" --requirement requirements.txt" in source
    assert 'ln -s "$release_dir" /opt/epi-agent/current.next' in source
    assert 'mv -Tf /opt/epi-agent/current.next /opt/epi-agent/current' in source
    assert "systemctl restart epi-agent.service" in source
    assert "/api/health" in source
    assert "/api/readiness" in source
    assert "previous_target" in source
    assert "restore_previous_release" in source
    assert "rm -rf /srv/epi-agent" not in source
    assert "rm -rf /opt/epi-agent/releases" not in source
    assert "eval " not in source
    assert "bash -c" not in source


def test_study_installer_verifies_archive_and_preserves_prior_versions() -> None:
    source = _asset("deploy/aws/bin/install-study.sh")

    assert '"$(id -u)" -eq 0' in source
    assert '^[0-9a-f]{64}$' in source
    assert "mktemp -d /srv/epi-agent/study-staging.XXXXXX" in source
    assert "aws s3 cp" in source
    assert "sha256sum -c" in source
    assert "REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data" in source
    assert "/opt/epi-agent/current/.venv/bin/python" in source
    assert "/opt/epi-agent/current/study_installer.py" in source
    assert '"--study" "$archive_path"' in source
    assert "study-package.json" in source
    assert "study_id" in source
    assert "package_version" in source
    assert "rm -rf /srv/epi-agent" not in source


def test_certificate_and_cloudwatch_assets_are_present() -> None:
    certificate_service = _asset(
        "deploy/aws/systemd/epi-agent-certificate-renew.service"
    )
    certificate_timer = _asset("deploy/aws/systemd/epi-agent-certificate-renew.timer")
    cloudwatch = _asset("deploy/aws/cloudwatch/amazon-cloudwatch-agent.json")

    assert "certbot renew" in certificate_service
    assert "nginx" in certificate_service
    assert "OnCalendar=" in certificate_timer
    assert "epi-agent.service" in cloudwatch
    assert "/var/log/nginx/access.log" in cloudwatch
