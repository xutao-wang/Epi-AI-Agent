#!/usr/bin/env python3
"""Execute AWS study-installer regressions through the real shell harness."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tests = [
        "tests/test_aws_study_installer.py::test_study_installer_repairs_retained_data_and_drops_privileges",
        "tests/test_aws_study_installer.py::test_study_installer_checksum_failure_precedes_ownership_and_privilege_drop",
        "tests/test_aws_study_installer.py::test_study_installer_failure_preserves_retained_data_and_cleans_staging",
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q"],
        cwd=root,
        check=False,
    )
    if completed.returncode:
        return completed.returncode
    print("AWS study installer smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
