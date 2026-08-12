#!/usr/bin/env python3.12
"""Exercise guarded AWS study-install contracts without mutating AWS."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tests = [
        "tests/test_aws_phase2a_cli.py",
        "tests/test_aws_infrastructure.py::test_phase2a_install_study_document_is_constrained_and_verified",
        "tests/test_aws_study_installer.py",
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q"],
        cwd=root,
        check=False,
    )
    if completed.returncode:
        return completed.returncode
    regression = subprocess.run(
        [sys.executable, "scripts/smoke_aws_phase2a_template_regressions.py"],
        cwd=root,
        check=False,
    )
    if regression.returncode:
        return regression.returncode
    print("Guarded AWS study installation smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
