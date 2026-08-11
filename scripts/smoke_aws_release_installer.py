#!/usr/bin/env python3
"""Execute AWS release-installer regressions through the real shell harness."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tests = [
        "tests/test_aws_host_assets.py::test_release_installer_waits_for_delayed_first_service_startup",
        "tests/test_aws_host_assets.py::test_release_installer_disables_service_after_failed_first_startup",
        "tests/test_aws_host_assets.py::test_release_installer_repairs_runtime_ownership_before_service_activation",
        "tests/test_aws_host_assets.py::test_release_installer_repairs_retained_runtime_directory_without_touching_child",
        "tests/test_aws_host_assets.py::test_release_installer_cleans_up_when_runtime_repair_fails",
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q"],
        cwd=root,
        check=False,
    )
    if completed.returncode:
        return completed.returncode
    print("AWS release installer smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
