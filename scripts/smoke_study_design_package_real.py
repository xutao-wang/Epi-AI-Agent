"""Exercise a real format-v3 study-design package through production boundaries."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db_rag.study import build_study_bundle
from study_installer import main as installer_main
from study_package.installer import load_installed_study
from study_package.registry import load_registry, package_root


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the real Markdown study-design package smoke once."
    )
    parser.add_argument(
        "--archive",
        type=Path,
        required=True,
        help="Installer-ready format-v3 .tar.gz study package.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    archive = args.archive.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="study-design-package-smoke-") as temporary:
        study_root = Path(temporary) / "study-data"
        exit_code = installer_main(
            ["--study", str(archive), "--study-root", str(study_root)]
        )
        if exit_code != 0:
            raise AssertionError(f"Study installer returned {exit_code}")

        studies_root = study_root / "studies"
        registry = load_registry(studies_root)
        if len(registry.active) != 1:
            raise AssertionError(
                f"Expected one active study, found {sorted(registry.active)}"
            )
        study_id, package_version = next(iter(registry.active.items()))
        installed = load_installed_study(
            package_root(studies_root, study_id, package_version)
        )
        bundle = build_study_bundle(installed)
        if bundle.study_design is None:
            raise AssertionError("Installed package has no study-design provider")
        overview = bundle.study_design.render_context()
        if "# RePORT India Study Design" not in overview:
            raise AssertionError("Authoritative overview title is missing")
        if "\n\n## Study populations\n" not in overview:
            raise AssertionError("Overview Markdown section boundaries were lost")
        if bundle.knowledge is None:
            raise AssertionError("Publication knowledge is not independently available")

        hits = bundle.study_design.search(
            "Which aliases describe Cohort A active pulmonary TB index cases and "
            "Cohort B household contacts?",
            limit=10,
        )
        if not hits:
            raise AssertionError("Study-design search returned no hits")
        if not any(
            hit.source_path == "reference/population-aliases.md" for hit in hits
        ):
            raise AssertionError(
                "Study-design search did not retrieve the nested aliases document: "
                f"{[hit.source_path for hit in hits]}"
            )
        for hit in hits:
            if (
                hit.source_kind != "study_design"
                or not hit.source_id
                or len(hit.source_sha256) != 64
                or not hit.source_path
            ):
                raise AssertionError(f"Invalid study-design hit provenance: {hit}")

    print(
        "study design package smoke passed: "
        f"{study_id}@{package_version}, {len(hits)} design hits"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
