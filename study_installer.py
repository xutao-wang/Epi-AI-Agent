from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from api.deployment import native_study_root, study_root
from study_package.installer import activate_study_version, install_study_archives
from utils.env_loader import load_app_environment, persist_local_env_values


PROJECT_ROOT = Path(__file__).resolve().parent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install or activate local study packages."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--study", nargs="+", type=Path, metavar="ARCHIVE")
    action.add_argument("--activate", metavar="STUDY_ID@VERSION")
    parser.add_argument(
        "--study-root",
        type=Path,
        help="Use this study-package folder for this command without changing .env.",
    )
    parser.add_argument(
        "--expected-study-id",
        help="Require a single archive to declare this study ID before installation.",
    )
    parser.add_argument(
        "--expected-package-version",
        help="Require a single archive to declare this version before installation.",
    )
    return parser


def _activate_target(value: str) -> tuple[str, str]:
    study_id, separator, package_version = value.partition("@")
    if not separator or not study_id or not package_version or "@" in package_version:
        raise ValueError("--activate must use STUDY_ID@VERSION")
    return study_id, package_version


def configure_study_root(
    *,
    project_root: str | Path = PROJECT_ROOT,
    environ: dict[str, str] = os.environ,
    input_fn=input,
    persist: bool = True,
) -> Path:
    configured = environ.get("REPORT_AGENT_STUDY_ROOT", "").strip()
    if configured:
        selected = Path(configured).expanduser().resolve()
        selected.mkdir(parents=True, exist_ok=True)
        return selected

    root = Path(project_root).resolve()
    default = native_study_root(root)
    selection = input_fn(
        f"Choose study-package folder:\n1. {default} (default)\n"
        "2. Enter another folder\nSelection [1]: "
    ).strip()
    target = default
    if selection == "2":
        entered = input_fn("Enter an absolute study-package folder path: ").strip()
        if not entered:
            raise ValueError("A study-package folder is required.")
        target = Path(entered)
        if not target.is_absolute():
            raise ValueError("Study-package folder must be an absolute path.")
    elif selection not in {"", "1"}:
        raise ValueError("Select 1 for the default folder or 2 for another folder.")

    selected = target.expanduser().resolve()
    selected.mkdir(parents=True, exist_ok=True)
    environ["REPORT_AGENT_STUDY_ROOT"] = str(selected)
    if persist:
        persist_local_env_values(root, {"REPORT_AGENT_STUDY_ROOT": str(selected)})
    return selected


def main(argv: list[str] | None = None) -> int:
    load_app_environment(PROJECT_ROOT)
    args = _parser().parse_args(argv)
    try:
        if args.study is None and (
            args.expected_study_id is not None
            or args.expected_package_version is not None
        ):
            raise ValueError(
                "expected identity options are only valid with --study"
            )
        selected_study_root = args.study_root or configure_study_root()
        studies_root = selected_study_root / "studies"
        if args.study is not None:
            installed = install_study_archives(
                args.study,
                studies_root,
                expected_study_id=args.expected_study_id,
                expected_package_version=args.expected_package_version,
            )
            for package in installed:
                for warning in package.warnings:
                    print(f"Warning: {warning.message}")
                print(f"Installed: {package.study_id}@{package.package_version}")
        else:
            study_id, package_version = _activate_target(args.activate)
            package = activate_study_version(study_id, package_version, studies_root)
            for warning in package.warnings:
                print(f"Warning: {warning.message}")
            print(f"Activated: {package.study_id}@{package.package_version}")
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
