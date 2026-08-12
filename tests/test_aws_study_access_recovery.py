from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "aws" / "phase2a" / "template.yaml"


class CloudFormationLoader(yaml.SafeLoader):
    """Parse scalar CloudFormation short-form intrinsic functions."""


def _intrinsic(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node):
    return {"Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}": loader.construct_scalar(node)}


CloudFormationLoader.add_multi_constructor("!", _intrinsic)


def _recovery_command() -> str:
    template = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=CloudFormationLoader)
    return "\n".join(
        template["Resources"]["EpiAgentRecoverStudyAccessDocument"]["Properties"][
            "Content"
        ]["mainSteps"][0]["inputs"]["runCommand"]
    )


def _container_recovery(
    tmp_path: Path,
    *,
    remove_chown: bool = False,
    deadline_seconds: int = 120,
    retained_state: str = "valid",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    retained_state_mutation = {
        "valid": ":",
        "no-active-package": (
            "printf '%s\\n' "
            "'{\"format_version\":1,\"active\":{}}' "
            '> "$study_root/studies/registry.json"'
        ),
        "manifest-mismatch": (
            "printf '%s\\n' "
            "'{\"study_id\":\"report-india-synthetic\","
            "\"package_version\":\"9.9.9\","
            "\"database\":{\"index\":\"database/index\"}}' "
            '> "$package_root/study-package.json"'
        ),
        "unsafe-index": (
            "printf '%s\\n' "
            "'{\"study_id\":\"report-india-synthetic\","
            "\"package_version\":\"0.2.0\","
            "\"database\":{\"index\":\"../outside\"}}' "
            '> "$package_root/study-package.json"'
        ),
    }[retained_state]
    command = _recovery_command().replace(
        "readonly recovery_deadline_seconds=120",
        f"readonly recovery_deadline_seconds={deadline_seconds}",
    )
    if remove_chown:
        command = command.replace(
            'chown -R -h epi-agent-web:epi-agent-web "$study_root"', ":"
        )
    recovery = tmp_path / "recovery.sh"
    recovery.write_text(command + "\n", encoding="utf-8")
    driver = tmp_path / "driver.sh"
    driver.write_text(
        """#!/usr/bin/bash
set -u
useradd --uid 1001 --create-home epi-agent-web
study_root=/srv/epi-agent/study_data
package_root=$study_root/studies/packages/report-india-synthetic/0.2.0
index_root=$package_root/database/index
mkdir -p "$index_root" /opt/epi-agent/current/.venv/bin /work/bin
printf '%s\\n' '{"format_version":1,"active":{"report-india-synthetic":"0.2.0"}}' > "$study_root/studies/registry.json"
printf '%s\\n' '{"study_id":"report-india-synthetic","package_version":"0.2.0","database":{"index":"database/index"}}' > "$package_root/study-package.json"
printf '%s' 'preserve retained Chroma data' > "$index_root/sentinel.bin"
chown -R root:root /srv/epi-agent
chmod 0700 "$study_root" "$study_root/studies" "$package_root" "$package_root/database" "$index_root"
chmod 0600 "$study_root/studies/registry.json" "$package_root/study-package.json"
chmod 0400 "$index_root/sentinel.bin"
__RETAINED_STATE_MUTATION__
cat > /opt/epi-agent/current/.venv/bin/python <<'PYTHON'
#!/usr/bin/bash
id -u > /work/validation-uid
/usr/bin/env > /work/environment
exec /usr/local/bin/python3 "$@"
PYTHON
chmod 0755 /opt/epi-agent/current/.venv/bin/python
ln -sf /usr/local/bin/python3 /usr/bin/python3.12
cat > /usr/bin/systemctl <<'SYSTEMCTL'
#!/usr/bin/bash
printf 'systemctl %s\\n' "$*" >> /work/operations
case "$1" in restart) : > /work/service-running ;; is-active) test -f /work/service-running ;; status) exit 0 ;; *) exit 64 ;; esac
SYSTEMCTL
cat > /usr/bin/curl <<'CURL'
#!/usr/bin/bash
printf 'curl %s\\n' "$*" >> /work/operations
test -f /work/service-running
case "$*" in */api/readiness*) printf '%s\\n' '{"status":"ready"}' ;; *) printf '%s\\n' '{"status":"ok"}' ;; esac
CURL
chmod 0755 /usr/bin/systemctl /usr/bin/curl
/bin/sh /work/recovery.sh
result=$?
stat -c '%u:%g %a' "$study_root/studies/registry.json" > /work/registry-stat
stat -c '%u:%g %a' "$index_root/sentinel.bin" > /work/sentinel-stat
cat "$index_root/sentinel.bin" > /work/sentinel-content
exit "$result"
""".replace("__RETAINED_STATE_MUTATION__", retained_state_mutation),
        encoding="utf-8",
    )
    driver.chmod(0o755)
    return (
        subprocess.run(
            [
                "docker", "run", "--rm", "-v", f"{tmp_path}:/work", "python:3.12-slim",
                "/usr/bin/bash", "/work/driver.sh",
            ],
            text=True,
            capture_output=True,
            check=False,
        ),
        tmp_path,
    )


def test_recovery_repairs_root_owned_study_with_real_uid_switch(tmp_path: Path) -> None:
    completed, work = _container_recovery(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert (work / "validation-uid").read_text().strip() == "1001"
    assert (work / "registry-stat").read_text().strip() == "1001:1001 600"
    assert (work / "sentinel-stat").read_text().strip() == "1001:1001 400"
    assert (work / "sentinel-content").read_bytes() == b"preserve retained Chroma data"
    assert (work / "operations").read_text().splitlines() == [
        "systemctl restart epi-agent.service",
        "curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/api/health",
        "curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/api/readiness",
    ]
    assert (work / "environment").read_text().find("OPENAI_API_KEY=") == -1
    assert (work / "environment").read_text().find("AWS_ACCESS_KEY_ID=") == -1
    assert (work / "recovery.sh").read_text().startswith("exec /usr/bin/bash")
    assert (work / "driver.sh").read_text().find("/bin/sh /work/recovery.sh") != -1
    assert (work / "sentinel-stat").is_file()


def test_recovery_without_chown_fails_before_restart(tmp_path: Path) -> None:
    completed, work = _container_recovery(tmp_path, remove_chown=True)

    assert completed.returncode != 0
    assert not (work / "operations").exists()
    assert (work / "registry-stat").read_text().strip() == "0:0 600"


@pytest.mark.parametrize(
    ("retained_state", "expected_error"),
    (
        ("no-active-package", "study registry has no active package"),
        ("manifest-mismatch", "active study manifest does not match registry"),
        ("unsafe-index", "active study index path is unsafe"),
    ),
)
def test_recovery_rejects_inconsistent_retained_state_after_ownership_repair(
    tmp_path: Path,
    retained_state: str,
    expected_error: str,
) -> None:
    completed, work = _container_recovery(tmp_path, retained_state=retained_state)

    assert completed.returncode != 0
    assert expected_error in completed.stderr
    assert (work / "registry-stat").read_text().strip() == "1001:1001 600"
    assert (work / "sentinel-stat").read_text().strip() == "1001:1001 400"
    assert (work / "sentinel-content").read_bytes() == b"preserve retained Chroma data"
    assert not (work / "operations").exists()


def test_recovery_rejects_ready_response_after_deadline_without_waiting(tmp_path: Path) -> None:
    completed, work = _container_recovery(tmp_path, deadline_seconds=0)

    assert completed.returncode != 0
    assert (work / "operations").read_text().splitlines() == [
        "systemctl restart epi-agent.service"
    ]
