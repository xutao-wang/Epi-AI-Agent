"""Exercise catalog-declared relationships in both bundled study packages."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from study_package.installer import install_study_archives
from study_package.registry import discover_studies


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-archive", type=Path, required=True)
    parser.add_argument("--nhanes-archive", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    archives = [
        args.report_archive.expanduser().resolve(),
        args.nhanes_archive.expanduser().resolve(),
    ]
    for archive in archives:
        if not archive.is_file():
            raise FileNotFoundError(f"Study archive not found: {archive}")

    with tempfile.TemporaryDirectory(
        prefix="catalog-relationships-smoke-"
    ) as temporary:
        studies_root = Path(temporary) / "studies"
        install_study_archives(archives, studies_root)
        studies = discover_studies(studies_root)

        report = studies.require("report-india-synthetic")
        report_inventory = report.data_sources[
            "report-india-synthetic"
        ].relationship_inventory()
        report_profile = report_inventory.profile_relationship(
            "Enrollment Cohort A",
            "Baseline Clinical and Demographic Information Cohort A",
            [("SUBJID", "SUBJID")],
        )
        if report_profile.matched_keys < 1:
            raise AssertionError("RePORT SUBJID relationship has no matched keys")

        nhanes = studies.require("nhanes-2017-2018")
        nhanes_inventory = nhanes.data_sources[
            "nhanes-2017-2018"
        ].relationship_inventory()
        paths = nhanes_inventory.find_join_paths("DEMO_J", "DIQ_J")
        if not paths or paths[0].profiles[0].key_pairs != [("SEQN", "SEQN")]:
            raise AssertionError(f"NHANES SEQN path unavailable: {paths}")

    print(
        "catalog-declared relationship smoke passed: "
        f"RePORT matched={report_profile.matched_keys}, "
        f"NHANES paths={len(paths)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
