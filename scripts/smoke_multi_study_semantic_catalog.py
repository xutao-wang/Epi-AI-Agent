"""Exercise real multi-study semantic catalog retrieval through runtime wiring."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter

import duckdb


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db_rag.config import EMBEDDING_MODEL
from db_rag.session_studies import bind_session_studies
from study_package.installer import install_study_archives
from study_package.registry import discover_studies
from utils.env_loader import load_app_environment


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run real RePORT-plus-NHANES semantic catalog retrieval once."
    )
    parser.add_argument(
        "--report-archive",
        type=Path,
        required=True,
        help="Installer-ready RePORT India .tar.gz package.",
    )
    parser.add_argument(
        "--nhanes-archive",
        type=Path,
        required=True,
        help="Installer-ready NHANES 2017-2018 .tar.gz package.",
    )
    return parser


def _elapsed_ms(start: float) -> float:
    return round((perf_counter() - start) * 1000, 2)


def _require_vector_hit(
    hits,
    *,
    table: str,
    column: str | None = None,
):
    matches = [
        hit
        for hit in hits
        if hit.table == table
        and (column is None or hit.column == column)
        and "vector" in hit.matched_by
    ]
    if not matches:
        observed = [
            {
                "source": hit.source,
                "table": hit.table,
                "column": hit.column,
                "matched_by": list(hit.matched_by),
            }
            for hit in hits
        ]
        raise AssertionError(
            "Expected vector-backed catalog hit "
            f"{table}{f'.{column}' if column else ''}; observed {observed}"
        )
    return matches[0]


def _table_count(database_path: Path) -> int:
    with duckdb.connect(str(database_path), read_only=True) as connection:
        return len(connection.execute("SHOW TABLES").fetchall())


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    archives = [
        args.report_archive.expanduser().resolve(),
        args.nhanes_archive.expanduser().resolve(),
    ]
    for archive in archives:
        if not archive.is_file():
            raise FileNotFoundError(f"Study archive not found: {archive}")

    load_app_environment(REPO_ROOT)
    api_key = str(os.environ.get("OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required for the real semantic smoke.")

    diagnostics: dict[str, object] = {"embedding_model": EMBEDDING_MODEL}
    with tempfile.TemporaryDirectory(
        prefix="multi-study-semantic-catalog-smoke-"
    ) as temporary:
        studies_root = Path(temporary) / "studies"

        started = perf_counter()
        install_study_archives(archives, studies_root)
        diagnostics["install_ms"] = _elapsed_ms(started)

        started = perf_counter()
        discovered = discover_studies(studies_root)
        bound = bind_session_studies(
            discovered,
            api_key=api_key,
            expected_embedding_model=EMBEDDING_MODEL,
        )
        diagnostics["discover_and_bind_ms"] = _elapsed_ms(started)
        expected_studies = {
            "report-india-synthetic",
            "nhanes-2017-2018",
        }
        if {study.study_id for study in bound.studies.values} != expected_studies:
            raise AssertionError(
                "Installed study IDs differ from the expected RePORT and NHANES IDs."
            )
        unavailable = {
            study_id: readiness.message
            for study_id, readiness in bound.readiness.items()
            if not readiness.available
        }
        if unavailable:
            raise AssertionError(f"Semantic study binding failed: {unavailable}")

        probes = {
            "report-india-synthetic": {
                "query": "manufactured cigarette smoking intensity per day",
                "vector_table": (
                    "Baseline Clinical and Demographic Information Cohort A"
                ),
                "vector_column": "CIGPAST",
                "inspect_column": None,
            },
            "nhanes-2017-2018": {
                "query": "long-term blood sugar control glycohemoglobin",
                "vector_table": "GHB_J",
                "vector_column": None,
                "inspect_column": "LBXGH",
            },
        }
        study_diagnostics: dict[str, object] = {}
        for study_id, expectation in probes.items():
            study = bound.studies.require(study_id)
            started = perf_counter()
            hits = study.catalog.search(expectation["query"], limit=10)
            search_ms = _elapsed_ms(started)
            if any(hit.source != study.source_id for hit in hits):
                raise AssertionError(
                    f"{study_id} returned cross-study catalog evidence."
                )
            expected_hit = _require_vector_hit(
                hits,
                table=expectation["vector_table"],
                column=expectation["vector_column"],
            )
            inspected_column = expectation["inspect_column"]
            if inspected_column is not None:
                inspected_fields = study.catalog.inspect_table(
                    study.source_id,
                    expectation["vector_table"],
                    limit=100,
                )
                if not any(
                    field.column == inspected_column for field in inspected_fields
                ):
                    raise AssertionError(
                        f"Exact inspection of {expectation['vector_table']} did not "
                        f"return {inspected_column}."
                    )

            started = perf_counter()
            table_count = _table_count(study.db_rag_paths.duckdb_path)
            duckdb_ms = _elapsed_ms(started)
            if table_count < 1:
                raise AssertionError(f"{study_id} DuckDB contains no tables.")
            study_diagnostics[study_id] = {
                "source_id": study.source_id,
                "matched_table": expected_hit.table,
                "matched_column": expected_hit.column,
                "matched_by": list(expected_hit.matched_by),
                "inspected_column": inspected_column,
                "returned_hit_count": len(hits),
                "table_count": table_count,
                "search_ms": search_ms,
                "duckdb_inspection_ms": duckdb_ms,
            }
        diagnostics["studies"] = study_diagnostics

    print(json.dumps(diagnostics, indent=2, sort_keys=True))
    print("multi-study semantic catalog smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
