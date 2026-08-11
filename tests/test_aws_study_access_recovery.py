from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "aws" / "phase2a" / "template.yaml"


class CloudFormationLoader(yaml.SafeLoader):
    """Parse scalar CloudFormation short-form intrinsic functions."""


def _intrinsic(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node):
    value = loader.construct_scalar(node)
    return {"Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}": value}


CloudFormationLoader.add_multi_constructor("!", _intrinsic)


def _recovery_command() -> str:
    template = yaml.load(
        TEMPLATE.read_text(encoding="utf-8"),
        Loader=CloudFormationLoader,
    )
    run_command = template["Resources"]["EpiAgentRecoverStudyAccessDocument"][
        "Properties"
    ]["Content"]["mainSteps"][0]["inputs"]["runCommand"]
    return "\n".join(run_command)


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _recovery_harness(
    tmp_path: Path,
    *,
    invalid_registry: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path, Path]:
    study_root = tmp_path / "host" / "srv" / "epi-agent" / "study_data"
    package_root = (
        study_root
        / "studies"
        / "packages"
        / "report-india-synthetic"
        / "0.2.0"
    )
    index_root = package_root / "database" / "index"
    index_root.mkdir(parents=True)
    sentinel = index_root / "sentinel.bin"
    sentinel.write_bytes(b"preserve retained Chroma data")
    sentinel.chmod(0o400)
    index_root.chmod(0o500)
    registry = study_root / "studies" / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "format_version": 1,
                "active": {}
                if invalid_registry
                else {"report-india-synthetic": "0.2.0"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry.chmod(0o000)
    (package_root / "study-package.json").write_text(
        json.dumps(
            {
                "format_version": 2,
                "study_id": "report-india-synthetic",
                "package_version": "0.2.0",
                "database": {"index": "database/index"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    operation_log = tmp_path / "operations.log"
    environment_log = tmp_path / "environment.log"
    service_marker = tmp_path / "service-running"
    fake_python = fake_bin / "current-python"
    _write_executable(
        fake_python,
        "#!/bin/bash\n"
        "set -eu\n"
        f"/usr/bin/env > {environment_log}\n"
        f'exec "{sys.executable}" "$@"\n',
    )

    fake_commands = {
        "id": "printf '%s\\n' 0\n",
        "chown": f"""printf 'chown %s\\n' "$*" >> "{operation_log}"
[ "$1" = -R ] && [ "$2" = -h ] && [ "$3" = epi-agent-web:epi-agent-web ]
shift 3
for target in "$@"; do
  chmod -R u+rwX "$target"
done
""",
        "runuser": f"""printf 'runuser %s\\n' "$*" >> "{operation_log}"
[ "$1" = --user ] && [ "$2" = epi-agent-web ] && [ "$3" = -- ]
shift 3
exec "$@"
""",
        "systemctl": f"""printf 'systemctl %s\\n' "$*" >> "{operation_log}"
case "$1" in
  restart)
    [ "$2" = epi-agent.service ]
    : > "{service_marker}"
    ;;
  is-active)
    [ -f "{service_marker}" ]
    ;;
  status)
    exit 0
    ;;
  *)
    exit 64
    ;;
esac
""",
        "curl": f"""printf 'curl %s\\n' "$*" >> "{operation_log}"
[ -f "{service_marker}" ]
case "$*" in
  *'/api/readiness'*) printf '%s\\n' '{{"status":"ready"}}' ;;
  *) printf '%s\\n' '{{"status":"ok"}}' ;;
esac
""",
        "sleep": ":\n",
    }
    for name, body in fake_commands.items():
        _write_executable(
            fake_bin / name,
            "#!/bin/bash\nset -eu\n" + body,
        )

    source = _recovery_command()
    source = source.replace("/srv/epi-agent/study_data", str(study_root))
    source = source.replace(
        "PATH=/opt/epi-agent/current/.venv/bin:/usr/bin",
        f"PATH={fake_bin}:/usr/bin",
    )
    source = source.replace(
        "/opt/epi-agent/current/.venv/bin/python",
        shlex.quote(str(fake_python)),
    )
    source = source.replace("/usr/sbin/runuser", str(fake_bin / "runuser"))
    source = source.replace("/usr/bin/python3.12", shlex.quote(sys.executable))
    completed = subprocess.run(
        ["bash", "-c", source],
        text=True,
        capture_output=True,
        check=False,
        env=os.environ | {"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )
    return completed, registry, sentinel, operation_log, environment_log


def test_recovery_repairs_access_before_restarting_current_service(
    tmp_path: Path,
) -> None:
    completed, registry, sentinel, operation_log, environment_log = (
        _recovery_harness(tmp_path)
    )

    assert completed.returncode == 0, completed.stderr
    operations = operation_log.read_text(encoding="utf-8").splitlines()
    chown_index = next(
        index for index, value in enumerate(operations) if value.startswith("chown ")
    )
    runuser_index = next(
        index for index, value in enumerate(operations) if value.startswith("runuser ")
    )
    restart_index = operations.index("systemctl restart epi-agent.service")
    health_index = next(
        index
        for index, value in enumerate(operations)
        if value.startswith("curl ") and "/api/health" in value
    )
    assert chown_index < runuser_index < restart_index < health_index
    assert json.loads(registry.read_text(encoding="utf-8"))["active"] == {
        "report-india-synthetic": "0.2.0"
    }
    assert sentinel.read_bytes() == b"preserve retained Chroma data"
    assert registry.stat().st_mode & 0o600 == 0o600
    assert sentinel.stat().st_mode & 0o600 == 0o600
    environment = dict(
        line.split("=", 1)
        for line in environment_log.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert environment["LANG"] == "C.UTF-8"
    assert environment["LC_ALL"] == "C.UTF-8"
    assert environment["PYTHONUTF8"] == "1"
    assert environment["REPORT_AGENT_STUDY_ROOT"] == str(
        registry.parents[1]
    )
    assert "AWS_ACCESS_KEY_ID" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment


def test_recovery_validation_failure_prevents_service_restart(tmp_path: Path) -> None:
    completed, registry, sentinel, operation_log, _ = _recovery_harness(
        tmp_path,
        invalid_registry=True,
    )

    assert completed.returncode != 0
    operations = operation_log.read_text(encoding="utf-8").splitlines()
    assert any(value.startswith("chown ") for value in operations)
    assert any(value.startswith("runuser ") for value in operations)
    assert not any(value.startswith("systemctl restart") for value in operations)
    assert json.loads(registry.read_text(encoding="utf-8"))["active"] == {}
    assert sentinel.read_bytes() == b"preserve retained Chroma data"
