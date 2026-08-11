from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]


def _asset(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _study_installer_harness(
    tmp_path: Path,
    *,
    checksum_fails: bool = False,
    installer_fails: bool = False,
) -> tuple[
    subprocess.CompletedProcess[str],
    Path,
    Path,
    Path,
    Path,
]:
    host_root = tmp_path / "host" / "srv" / "epi-agent"
    study_root = host_root / "study_data"
    package_root = (
        study_root
        / "studies"
        / "packages"
        / "report-india-synthetic"
        / "0.2.0"
    )
    chroma_root = package_root / "database" / "index"
    chroma_root.mkdir(parents=True)
    registry = study_root / "studies" / "registry.json"
    registry.write_text(
        json.dumps(
            {"format_version": 1, "active": {"report-india-synthetic": "0.2.0"}}
        )
        + "\n",
        encoding="utf-8",
    )
    registry.chmod(0o000)
    sentinel = chroma_root / "sentinel.bin"
    sentinel.write_bytes(b"preserve retained study data")
    sentinel.chmod(0o400)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    archive = tmp_path / "study.tar.gz"
    archive.write_bytes(b"verified-study-archive")
    operation_log = tmp_path / "operations.log"
    environment_log = tmp_path / "installer-environment.log"
    dummy_installer = tmp_path / "study_installer.py"
    dummy_installer.write_text("# replaced by the harness\n", encoding="utf-8")

    fake_python = f"""#!/usr/bin/env bash
set -eu
/usr/bin/env > {environment_log}
if [ "${{1:-}}" = "{dummy_installer}" ]; then
  if [ "{1 if installer_fails else 0}" = 1 ]; then
    exit 17
  fi
  test -r "{registry}"
  test -w "{registry}"
  test -r "{sentinel}"
  test -w "{sentinel}"
  printf '%s\n' '{{"format_version":1,"active":{{"report-india-synthetic":"0.2.0"}}}}' > "{registry}"
  mkdir -p "{package_root}"
  printf '%s\n' '{{"format_version":2,"study_id":"report-india-synthetic","label":"RePORT India Synthetic","package_version":"0.2.0","database":{{"source_id":"report-india-synthetic","duckdb":"database/study.duckdb","catalog":"database/schema_catalog.json","index":"database/index","embedding_model":"OpenAI/text-embedding-3-large"}}}}' > "{package_root / 'study-package.json'}"
  printf '%s\n' 'Installed: report-india-synthetic@0.2.0'
  exit 0
fi
exec "{Path(os.sys.executable)}" "$@"
"""

    for name, body in {
        "id": "printf '%s\\n' 0\n",
        "aws": 'printf \'aws %s\\n\' "$*" >> "$TEST_OPERATION_LOG"\ncp "$TEST_ARCHIVE" "$4"\n',
        "sha256sum": """printf 'sha256sum %s\n' "$*" >> "$TEST_OPERATION_LOG"
[ "$TEST_CHECKSUM_FAILS" = 0 ]
""",
        "mktemp": """[ "$1" = -d ] && [ "$#" = 2 ]
staging_dir="${2%XXXXXX}fixture"
mkdir "$staging_dir"
printf '%s\n' "$staging_dir"
""",
        "install": """printf 'install %s\n' "$*" >> "$TEST_OPERATION_LOG"
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
        "chown": """printf 'chown %s\n' "$*" >> "$TEST_OPERATION_LOG"
[ "$1" = -R ] && [ "$2" = -h ] && [ "$3" = epi-agent-web:epi-agent-web ]
shift 3
for target in "$@"; do
  chmod -R u+rwX "$target"
done
""",
        "runuser": """printf 'runuser %s\n' "$*" >> "$TEST_OPERATION_LOG"
[ "$1" = --user ] && [ "$2" = epi-agent-web ] && [ "$3" = -- ]
shift 3
exec "$@"
""",
    }.items():
        _write_executable(fake_bin / name, "#!/usr/bin/env bash\nset -eu\n" + body)
    _write_executable(fake_bin / "bash", "#!/bin/bash\nexec /bin/bash \"$@\"\n")
    _write_executable(fake_bin / "mkdir", "#!/bin/bash\nexec /bin/mkdir \"$@\"\n")
    _write_executable(fake_bin / "study-python", fake_python)

    source = _asset("deploy/aws/bin/install-study.sh")
    staging_parent = host_root / "study-staging"
    source = source.replace("/srv/epi-agent/study_data", str(study_root))
    source = source.replace("/srv/epi-agent/study-staging", str(staging_parent))
    source = source.replace(
        "/opt/epi-agent/current/.venv/bin/python", str(fake_bin / "study-python")
    )
    source = source.replace(
        "/opt/epi-agent/current/study_installer.py", str(dummy_installer)
    )
    source = source.replace(
        "/opt/epi-agent/current/.venv/bin:/usr/bin", f"{fake_bin}:/usr/bin"
    )
    source = source.replace("/usr/sbin/runuser", str(fake_bin / "runuser"))
    source = source.replace(
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        f"PATH={fake_bin}:/usr/bin:/bin",
    )
    harness = tmp_path / "install-study.sh"
    _write_executable(harness, source)

    environment = os.environ | {
        "TEST_ARCHIVE": str(archive),
        "TEST_CHECKSUM_FAILS": "1" if checksum_fails else "0",
        "TEST_OPERATION_LOG": str(operation_log),
    }
    completed = subprocess.run(
        [
            "bash",
            str(harness),
            "example-bucket",
            "studies/report-india-synthetic-0.2.0.tar.gz",
            "0" * 64,
            "report-india-synthetic",
            "0.2.0",
        ],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    return completed, study_root, operation_log, environment_log, staging_parent


def test_study_installer_repairs_retained_data_and_drops_privileges(
    tmp_path: Path,
) -> None:
    completed, study_root, operation_log, environment_log, staging_parent = (
        _study_installer_harness(tmp_path)
    )

    assert completed.returncode == 0, completed.stderr
    operations = operation_log.read_text(encoding="utf-8").splitlines()
    checksum_index = next(
        index for index, value in enumerate(operations) if value.startswith("sha256sum ")
    )
    chown_index = next(
        index for index, value in enumerate(operations) if value.startswith("chown ")
    )
    runuser_indexes = [
        index for index, value in enumerate(operations) if value.startswith("runuser ")
    ]
    staging_dir = staging_parent.parent / "study-staging.fixture"
    assert checksum_index < chown_index < runuser_indexes[0]
    assert len(runuser_indexes) == 2
    assert operations[chown_index] == (
        "chown -R -h epi-agent-web:epi-agent-web "
        f"{study_root} {staging_dir}"
    )
    assert all(
        "--user epi-agent-web -- /usr/bin/env -i" in operations[index]
        for index in runuser_indexes
    )
    registry = study_root / "studies" / "registry.json"
    sentinel = (
        study_root
        / "studies"
        / "packages"
        / "report-india-synthetic"
        / "0.2.0"
        / "database"
        / "index"
        / "sentinel.bin"
    )
    assert json.loads(registry.read_text(encoding="utf-8"))["active"] == {
        "report-india-synthetic": "0.2.0"
    }
    assert sentinel.read_bytes() == b"preserve retained study data"
    assert registry.stat().st_mode & 0o600 == 0o600
    assert sentinel.stat().st_mode & 0o600 == 0o600
    environment = dict(
        line.split("=", 1)
        for line in environment_log.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert environment["PATH"].endswith("/bin:/usr/bin")
    assert environment["LANG"] == "C.UTF-8"
    assert environment["LC_ALL"] == "C.UTF-8"
    assert environment["PYTHONUTF8"] == "1"
    assert environment["REPORT_AGENT_STUDY_ROOT"] == str(study_root)
    assert "AWS_ACCESS_KEY_ID" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert not list(staging_parent.parent.glob("study-staging.*"))


def test_study_installer_checksum_failure_precedes_ownership_and_privilege_drop(
    tmp_path: Path,
) -> None:
    completed, study_root, operation_log, environment_log, staging_parent = (
        _study_installer_harness(tmp_path, checksum_fails=True)
    )

    assert completed.returncode != 0
    operations = operation_log.read_text(encoding="utf-8").splitlines()
    assert any(value.startswith("sha256sum ") for value in operations)
    assert not any(value.startswith("chown ") for value in operations)
    assert not any(value.startswith("runuser ") for value in operations)
    assert not environment_log.exists()
    registry = study_root / "studies" / "registry.json"
    assert registry.stat().st_mode & 0o777 == 0
    assert not list(staging_parent.parent.glob("study-staging.*"))


def test_study_installer_failure_preserves_retained_data_and_cleans_staging(
    tmp_path: Path,
) -> None:
    completed, study_root, operation_log, _, staging_parent = _study_installer_harness(
        tmp_path,
        installer_fails=True,
    )

    assert completed.returncode == 17
    operations = operation_log.read_text(encoding="utf-8").splitlines()
    assert sum(value.startswith("runuser ") for value in operations) == 1
    registry = study_root / "studies" / "registry.json"
    assert json.loads(registry.read_text(encoding="utf-8"))["active"] == {
        "report-india-synthetic": "0.2.0"
    }
    sentinel = (
        study_root
        / "studies"
        / "packages"
        / "report-india-synthetic"
        / "0.2.0"
        / "database"
        / "index"
        / "sentinel.bin"
    )
    assert sentinel.read_bytes() == b"preserve retained study data"
    assert not list(staging_parent.parent.glob("study-staging.*"))
