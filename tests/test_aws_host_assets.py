from __future__ import annotations

import io
import json
from pathlib import Path
import os
import subprocess
import tarfile


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
    assert (
        "ExecStart=/opt/epi-agent/current/.venv/bin/python -m uvicorn "
        "api.app:app --host 127.0.0.1 --port 8000 --workers 1"
    ) in source
    assert "api.server:app" not in source
    assert "--host 127.0.0.1 --port 8000" in source
    assert "--workers 1" in source
    assert "StandardOutput=append:/var/log/epi-agent/application.log" in source
    assert "StandardError=append:/var/log/epi-agent/application.log" in source
    assert "UMask=0027" in source
    assert "NoNewPrivileges=true" in source
    assert source.count("RuntimeDirectory=epi-agent") == 1
    assert source.count("RuntimeDirectoryMode=0750") == 1
    service_section = source.split("[Service]\n", 1)[1].split("\n[Install]", 1)[0]
    assert "RuntimeDirectory=epi-agent" in service_section
    assert "RuntimeDirectoryMode=0750" in service_section
    assert "ReadWritePaths=/srv/epi-agent/runtime /srv/epi-agent/study_data /run/epi-agent" in source
    assert "ReadOnlyPaths=/opt/epi-agent/current" in source
    assert "ReadWritePaths=/opt/epi-agent/releases" not in source


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

    apex_source = source[apex_position:]
    assert "client_max_body_size 25m;" in apex_source
    assert "proxy_connect_timeout" in apex_source
    assert "proxy_read_timeout" in apex_source
    assert "add_header X-Content-Type-Options" in apex_source
    assert "proxy_http_version 1.1" in apex_source
    assert 'proxy_set_header Upgrade $http_upgrade' in apex_source
    assert 'proxy_set_header Connection "upgrade"' in apex_source
    assert "location = /api/health" in apex_source
    assert "location = /api/readiness" in apex_source
    assert "location = /api/ops/deployment-status { deny all; }" in apex_source


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

    assert "set -Eeuo pipefail" in source
    assert "trap 'exit $?\' ERR" in source
    assert source.index("trap cleanup EXIT") < source.index(
        'install -m 0640 -o root -g epi-agent-web /dev/null "$maintenance_file"'
    )
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
    assert 'if [ -L "$current_link" ]; then\n  wait_for_drain\nfi' in source
    assert "readonly staged_nginx_config=/etc/nginx/staged/epi-agent.conf" in source
    assert 'rm -f -- "$bootstrap_nginx_config"' in source
    assert source.index("/usr/bin/certbot certonly") < source.index('mv -Tf "$staged_nginx_config" "$live_nginx_config"')
    assert '"$(readlink -- "$current_link")" != "$release_dir"' in source
    assert "activate_staged_nginx_config" in source
    assert "restore_nginx_config" in source


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
    assert '"--expected-study-id" "$study_id"' in source
    assert '"--expected-package-version" "$package_version"' in source
    assert "study-package.json" in source
    assert "study_id" in source
    assert "package_version" in source
    assert 'study_root / "studies" / "packages"' in source
    assert "rm -rf /srv/epi-agent" not in source
    ownership_repair = (
        'chown -R -h epi-agent-web:epi-agent-web "$study_root" "$staging_dir"'
    )
    assert ownership_repair in source
    assert "readonly -a study_python=(" in source
    assert "/usr/sbin/runuser" in source
    assert "--user epi-agent-web" in source
    assert "/usr/bin/env" in source
    assert "-i" in source
    assert "PATH=/opt/epi-agent/current/.venv/bin:/usr/bin" in source
    assert "LANG=C.UTF-8" in source
    assert "LC_ALL=C.UTF-8" in source
    assert "PYTHONUTF8=1" in source
    assert "REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data" in source
    assert source.count('"${study_python[@]}"') == 2
    assert source.index("sha256sum -c") < source.index(ownership_repair)
    assert source.index(ownership_repair) < source.index('"${study_python[@]}"')
    assert "OPENAI_API_KEY" not in source
    assert "AWS_ACCESS_KEY_ID" not in source
    assert "eval " not in source
    assert "bash -c" not in source


def test_phase2a_runbook_has_executable_study_rollback_procedure() -> None:
    source = _asset("docs/aws/phase2a-runbook.md")

    assert "aws ssm start-session --target i-0f9ed9c133ea2358b" in source
    assert "cd /opt/epi-agent/current" in source
    assert "/usr/sbin/runuser --user epi-agent-web -- /usr/bin/env -i" in source
    assert "REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data" in source
    assert "/opt/epi-agent/current/.venv/bin/python" in source
    assert "/opt/epi-agent/current/study_installer.py" in source
    assert "--activate report-india-synthetic@0.2.0" in source
    assert "--study-root /srv/epi-agent/study_data" in source
    assert "sudo systemctl restart epi-agent.service" in source
    assert "curl --fail --silent --show-error http://127.0.0.1:8000/api/health" in source
    assert "curl --fail --silent --show-error http://127.0.0.1:8000/api/readiness" in source


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


def _write_release_archive(archive_path: Path) -> None:
    release_id = "a" * 40
    manifest = json.dumps({"commit_sha": release_id, "python_version": "3.12"}).encode()
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, payload in (("release.json", manifest), ("requirements.txt", b"\n")):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _release_harness(
    tmp_path: Path,
    *,
    invalid_archive: bool,
    readiness_fails: bool,
    staging_fails: bool = False,
    health_failures: int = 0,
    service_inactive: bool = False,
    previous_release_exists: bool = True,
    runtime_repair_fails: bool = False,
    certificate_exists: bool = True,
    certificate_covers_www: bool = True,
    certificate_issue_fails: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path, Path]:
    root = tmp_path / "host"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    archive_path = tmp_path / "release.tar.gz"
    if invalid_archive:
        archive_path.write_bytes(b"not a tar archive")
    else:
        _write_release_archive(archive_path)

    for name, body in {
        "id": "printf '%s\\n' 0\n",
        "aws": 'cp "$TEST_ARCHIVE" "$4"\n',
        "sha256sum": "exit 0\n",
        "install": """printf 'install %s\\n' "$*" >> "$TEST_OPERATION_LOG"
if [ "$TEST_RUNTIME_REPAIR_FAILS" = 1 ] && [ "$*" = "-d -m 0750 -o epi-agent-web -g epi-agent-web $TEST_RUNTIME_DIR" ]; then
  exit 1
fi
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|-g)
      [ "$#" -ge 2 ] || exit 64
      shift 2
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done
/usr/bin/install "${args[@]}"
""",
        "systemctl": """printf 'systemctl %s\\n' "$*" >> "$TEST_OPERATION_LOG"
printf "%s\\n" "$*" >> "$TEST_SYSTEMCTL_LOG"
if [ "${1:-}" = is-active ] && [ "$TEST_SERVICE_INACTIVE" = 1 ]; then
  exit 3
fi
""",
        "curl": """case "$*" in
  *deployment-status*) printf '%s\\n' '{"maintenance": true, "active_runs": 0}' ;;
  */api/health*)
    count=0
    if [ -f "$TEST_HEALTH_COUNT_FILE" ]; then
      read -r count < "$TEST_HEALTH_COUNT_FILE"
    fi
    count=$((count + 1))
    printf '%s\\n' "$count" > "$TEST_HEALTH_COUNT_FILE"
    [ "$count" -gt "$TEST_HEALTH_FAILURES" ] || exit 7
    ;;
  *readiness*)
    [ "$TEST_READINESS_FAILS" = 0 ] || exit 22
    printf '%s\\n' '{"status": "ready"}'
    ;;
esac
""",
        "sleep": ":\n",
        "uv": """if [ "$1" = venv ]; then
  mkdir -p "$4/bin"
  : > "$4/bin/python"
fi
""",
        "mv": """replace_destination=false
while [ "$#" -gt 0 ] && [ "${1#-}" != "$1" ]; do
  replace_destination=true
  shift
done
if [ "$replace_destination" = true ]; then
  /bin/rm -f "$2"
fi
/bin/mv "$@"
""",
        "readlink": """[ "$1" = -- ] && shift
/usr/bin/readlink "$@"
""",
        "mktemp": """if [ "$TEST_STAGING_FAILS" = 1 ]; then
  exit 1
fi
/usr/bin/mktemp "$@"
""",
        "nginx": "exit 0\n",
        "openssl": """if [ "$TEST_CERTIFICATE_COVERS_WWW" = 1 ]; then
  printf '%s\\n' 'Hostname www.example.org does match certificate'
else
  printf '%s\\n' 'Hostname www.example.org does NOT match certificate'
fi
""",
        "certbot": """printf 'certbot %s\\n' "$*" >> "$TEST_OPERATION_LOG"
[ "$TEST_CERTBOT_FAILS" = 0 ]
""",
        "python3": 'exec "$TEST_PYTHON" "$@"\n',
    }.items():
        _write_executable(fake_bin / name, "#!/usr/bin/env bash\nset -eu\n" + body)

    source = _asset("deploy/aws/bin/install-release.sh")
    source = source.replace("/opt/epi-agent", str(root / "opt" / "epi-agent"))
    source = source.replace("/run/epi-agent", str(root / "run" / "epi-agent"))
    source = source.replace(
        "/srv/epi-agent/runtime", str(root / "srv" / "epi-agent" / "runtime")
    )
    source = source.replace("/etc/letsencrypt", str(root / "etc" / "letsencrypt"))
    source = source.replace("/etc/nginx", str(root / "etc" / "nginx"))
    source = source.replace("/var/www/certbot", str(root / "var" / "www" / "certbot"))
    source = source.replace("/usr/bin/certbot", "certbot")
    source = source.replace("/usr/bin/openssl", "openssl")
    source = source.replace("/usr/bin/python3.12", "python3")
    source = source.replace(
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        f"PATH={fake_bin}:/usr/bin:/bin",
    )
    harness_path = tmp_path / "install-release.sh"
    _write_executable(harness_path, source)

    current_link = root / "opt" / "epi-agent" / "current"
    current_link.parent.mkdir(parents=True)
    if previous_release_exists:
        current_link.symlink_to("/previous/release")
    certificate = root / "etc" / "letsencrypt" / "live" / "example.org" / "fullchain.pem"
    if certificate_exists:
        certificate.parent.mkdir(parents=True)
        certificate.write_text("fake certificate\n", encoding="utf-8")
    systemctl_log = tmp_path / "systemctl.log"
    environment = os.environ | {
        "TEST_ARCHIVE": str(archive_path),
        "TEST_PYTHON": str(Path(os.sys.executable)),
        "TEST_SYSTEMCTL_LOG": str(systemctl_log),
        "TEST_OPERATION_LOG": str(tmp_path / "operation.log"),
        "TEST_READINESS_FAILS": "1" if readiness_fails else "0",
        "TEST_STAGING_FAILS": "1" if staging_fails else "0",
        "TEST_HEALTH_FAILURES": str(health_failures),
        "TEST_HEALTH_COUNT_FILE": str(tmp_path / "health-count"),
        "TEST_SERVICE_INACTIVE": "1" if service_inactive else "0",
        "TEST_RUNTIME_REPAIR_FAILS": "1" if runtime_repair_fails else "0",
        "TEST_RUNTIME_DIR": str(root / "srv" / "epi-agent" / "runtime"),
        "TEST_CERTIFICATE_COVERS_WWW": "1" if certificate_covers_www else "0",
        "TEST_CERTBOT_FAILS": "1" if certificate_issue_fails else "0",
    }
    completed = subprocess.run(
        [
            "bash",
            str(harness_path),
            "example-bucket",
            "releases/test.tar.gz",
            "0" * 64,
            "a" * 40,
            "example.org",
            "ops@example.org",
        ],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    return (
        completed,
        current_link,
        root / "run" / "epi-agent" / "maintenance",
        root / "opt" / "epi-agent",
        systemctl_log,
    )


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


def test_release_installer_rolls_back_after_readiness_failure(tmp_path: Path) -> None:
    completed, current_link, maintenance_file, staging_parent, systemctl_log = _release_harness(
        tmp_path, invalid_archive=False, readiness_fails=True
    )

    assert completed.returncode != 0, completed.stderr
    assert current_link.readlink() == Path("/previous/release")
    assert not maintenance_file.exists()
    assert not list(staging_parent.glob("staging.*"))
    assert systemctl_log.read_text(encoding="utf-8").splitlines().count(
        "restart epi-agent.service"
    ) == 2


def test_release_installer_waits_for_delayed_first_service_startup(
    tmp_path: Path,
) -> None:
    completed, current_link, maintenance_file, staging_parent, systemctl_log = (
        _release_harness(
            tmp_path,
            invalid_archive=False,
            readiness_fails=False,
            health_failures=1,
            previous_release_exists=False,
        )
    )

    assert completed.returncode == 0, completed.stderr
    assert current_link.readlink() == staging_parent / "releases" / ("a" * 40)
    assert not maintenance_file.exists()
    assert int((tmp_path / "health-count").read_text(encoding="utf-8")) >= 2
    systemctl_calls = systemctl_log.read_text(encoding="utf-8").splitlines()
    assert "enable epi-agent.service" in systemctl_calls
    assert systemctl_calls.count("restart epi-agent.service") == 1


def test_release_installer_repairs_runtime_ownership_before_service_activation(
    tmp_path: Path,
) -> None:
    completed, _, _, _, _ = _release_harness(
        tmp_path,
        invalid_archive=False,
        readiness_fails=False,
        previous_release_exists=False,
    )

    assert completed.returncode == 0, completed.stderr
    source = _asset("deploy/aws/bin/install-release.sh")
    repair = (
        "install -d -m 0750 -o epi-agent-web -g epi-agent-web "
        "/srv/epi-agent/runtime"
    )
    assert repair in source
    assert source.index(repair) < source.index("systemctl enable epi-agent.service")

    operations = (tmp_path / "operation.log").read_text(encoding="utf-8").splitlines()
    harness_repair = (
        "install -d -m 0750 -o epi-agent-web -g epi-agent-web "
        f"{tmp_path / 'host' / 'srv' / 'epi-agent' / 'runtime'}"
    )
    assert harness_repair in operations
    assert operations.index(harness_repair) < operations.index(
        "systemctl enable epi-agent.service"
    )


def test_release_installer_repairs_retained_runtime_directory_without_touching_child(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "host" / "srv" / "epi-agent" / "runtime"
    runtime_dir.mkdir(parents=True)
    runtime_dir.chmod(0o711)
    sentinel = runtime_dir / "sentinel.txt"
    sentinel.write_text("preserve this runtime data", encoding="utf-8")
    sentinel.chmod(0o640)
    sentinel_metadata = sentinel.stat()

    completed, _, _, _, _ = _release_harness(
        tmp_path,
        invalid_archive=False,
        readiness_fails=False,
        previous_release_exists=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert runtime_dir.stat().st_mode & 0o777 == 0o750
    assert sentinel.read_text(encoding="utf-8") == "preserve this runtime data"
    actual_metadata = sentinel.stat()
    assert (
        actual_metadata.st_mode,
        actual_metadata.st_uid,
        actual_metadata.st_gid,
        actual_metadata.st_mtime_ns,
    ) == (
        sentinel_metadata.st_mode,
        sentinel_metadata.st_uid,
        sentinel_metadata.st_gid,
        sentinel_metadata.st_mtime_ns,
    )


def test_release_installer_cleans_up_when_runtime_repair_fails(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "host" / "srv" / "epi-agent" / "runtime"
    runtime_dir.mkdir(parents=True)
    sentinel = runtime_dir / "sentinel.txt"
    sentinel.write_text("preserve this runtime data", encoding="utf-8")
    sentinel.chmod(0o640)
    sentinel_metadata = sentinel.stat()

    completed, current_link, maintenance_file, _, systemctl_log = _release_harness(
        tmp_path,
        invalid_archive=False,
        readiness_fails=False,
        runtime_repair_fails=True,
    )

    assert completed.returncode != 0
    assert current_link.readlink() == Path("/previous/release")
    assert not maintenance_file.exists()
    assert not systemctl_log.exists()
    operations = (tmp_path / "operation.log").read_text(encoding="utf-8").splitlines()
    assert operations[-1] == (
        "install -d -m 0750 -o epi-agent-web -g epi-agent-web "
        f"{runtime_dir}"
    )
    assert sentinel.read_text(encoding="utf-8") == "preserve this runtime data"
    actual_metadata = sentinel.stat()
    assert (
        actual_metadata.st_mode,
        actual_metadata.st_uid,
        actual_metadata.st_gid,
        actual_metadata.st_mtime_ns,
    ) == (
        sentinel_metadata.st_mode,
        sentinel_metadata.st_uid,
        sentinel_metadata.st_gid,
        sentinel_metadata.st_mtime_ns,
    )


def test_release_installer_disables_service_after_failed_first_startup(
    tmp_path: Path,
) -> None:
    completed, current_link, maintenance_file, _, systemctl_log = _release_harness(
        tmp_path,
        invalid_archive=False,
        readiness_fails=False,
        health_failures=1,
        service_inactive=True,
        previous_release_exists=False,
    )

    assert completed.returncode != 0
    assert not current_link.exists()
    assert not current_link.is_symlink()
    assert not maintenance_file.exists()
    systemctl_calls = systemctl_log.read_text(encoding="utf-8").splitlines()
    assert "is-active --quiet epi-agent.service" in systemctl_calls
    assert "disable --now epi-agent.service" in systemctl_calls


def test_release_installer_clears_maintenance_when_archive_is_invalid(tmp_path: Path) -> None:
    completed, current_link, maintenance_file, staging_parent, systemctl_log = _release_harness(
        tmp_path, invalid_archive=True, readiness_fails=False
    )

    assert completed.returncode != 0, completed.stderr
    assert current_link.readlink() == Path("/previous/release")
    assert not maintenance_file.exists()
    assert not list(staging_parent.glob("staging.*"))
    assert not systemctl_log.exists()


def test_release_installer_clears_maintenance_when_staging_creation_fails(
    tmp_path: Path,
) -> None:
    completed, current_link, maintenance_file, staging_parent, systemctl_log = _release_harness(
        tmp_path, invalid_archive=False, readiness_fails=False, staging_fails=True
    )

    assert completed.returncode != 0, completed.stderr
    assert current_link.readlink() == Path("/previous/release")
    assert not maintenance_file.exists()
    assert not list(staging_parent.glob("staging.*"))
    assert not systemctl_log.exists()
