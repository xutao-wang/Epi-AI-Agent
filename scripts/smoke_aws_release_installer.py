#!/usr/bin/env python3
"""Execute the first-release startup regression through the real shell harness."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tests = [
        "tests/test_aws_host_assets.py::test_release_installer_waits_for_delayed_first_service_startup",
        "tests/test_aws_host_assets.py::test_release_installer_disables_service_after_failed_first_startup",
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q"],
        cwd=root,
        check=False,
    )
    if completed.returncode:
        return completed.returncode
    print("AWS first-release startup smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
