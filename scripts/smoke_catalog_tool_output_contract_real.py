"""Exercise complete catalog and inspection output contracts with real NHANES."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db_rag.config import EMBEDDING_MODEL
from db_rag.session_studies import bind_session_studies
from epi_agent.artifacts import StateArtifactStore
from epi_agent.db_rag.tools import build_db_rag_tool_registry
from epi_agent.protocol import ToolContext, ToolResult, serialize_tool_result
from study_package.installer import install_study_archives
from study_package.registry import discover_studies
from utils.env_loader import load_app_environment


QUERIES = [
    "age sex race ethnicity education income survey weights",
    "diabetes diagnosis insulin medication history",
    "blood pressure systolic diastolic examination",
    "body mass index waist height weight",
    "urine albumin creatinine ratio kidney disease",
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the real catalog tool output-contract smoke once."
    )
    parser.add_argument("--nhanes-archive", type=Path, required=True)
    return parser


def _elapsed_ms(start: float) -> float:
    return round((perf_counter() - start) * 1_000, 2)


def _nested_message(result: ToolResult) -> dict[str, Any]:
    outer = json.loads(serialize_tool_result(result))
    inner = json.loads(outer["message"])
    if inner.get("code") == "MODEL_TOOL_MESSAGE_TOO_LARGE":
        raise AssertionError(f"Normal DB-RAG message overflowed: {inner}")
    return inner


def _assert_complete_probe_groups(payload: dict[str, Any]) -> int:
    if "hits" in payload:
        raise AssertionError("Catalog output retained flattened top-level hits.")
    probes = payload.get("probes")
    if not isinstance(probes, list) or len(probes) != len(QUERIES):
        raise AssertionError("Catalog output did not preserve five probe groups.")
    if [probe.get("query") for probe in probes] != QUERIES:
        raise AssertionError("Catalog probe order or identity changed.")
    total_hits = sum(len(probe.get("hits") or []) for probe in probes)
    if total_hits <= 25:
        raise AssertionError(f"Expected more than 25 total hits; got {total_hits}.")
    if not probes[-1].get("hits"):
        raise AssertionError("The fifth probe lost all returned evidence.")
    return total_hits


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    archive = args.nhanes_archive.expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"NHANES archive not found: {archive}")

    load_app_environment(REPO_ROOT)
    api_key = str(os.environ.get("OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required for the real smoke.")

    diagnostics: dict[str, object] = {"embedding_model": EMBEDDING_MODEL}
    with tempfile.TemporaryDirectory(
        prefix="catalog-output-contract-smoke-"
    ) as name:
        studies_root = Path(name) / "studies"
        started = perf_counter()
        install_study_archives([archive], studies_root)
        discovered = discover_studies(studies_root)
        bound = bind_session_studies(
            discovered,
            api_key=api_key,
            expected_embedding_model=EMBEDDING_MODEL,
        )
        diagnostics["install_bind_ms"] = _elapsed_ms(started)

        readiness = bound.readiness["nhanes-2017-2018"]
        if not readiness.available:
            raise AssertionError(
                f"NHANES semantic binding failed: {readiness.message}"
            )
        context = ToolContext(
            studies=bound.studies,
            artifact_store=StateArtifactStore(),
            thread_id="catalog-output-contract-smoke",
            policy=object(),
        )
        registry = build_db_rag_tool_registry()

        started = perf_counter()
        search_result = registry.invoke(
            "dbrag-search_catalog",
            {
                "study_id": "nhanes-2017-2018",
                "queries": QUERIES,
                "limit": 10,
            },
            context=context,
        )
        search_message = _nested_message(search_result)
        search_artifact = context.artifact_store.require(
            search_result.artifacts[0]
        ).content
        diagnostics["search_ms"] = _elapsed_ms(started)
        message_hit_count = _assert_complete_probe_groups(search_message)
        artifact_hit_count = _assert_complete_probe_groups(search_artifact)
        if message_hit_count != artifact_hit_count:
            raise AssertionError(
                "Model-facing and artifact catalog hit counts differ: "
                f"{message_hit_count} != {artifact_hit_count}"
            )

        started = perf_counter()
        inspect_result = registry.invoke(
            "dbrag-inspect_table",
            {
                "table_ref": {
                    "study_id": "nhanes-2017-2018",
                    "source_id": "nhanes-2017-2018",
                    "table": "DEMO_J",
                },
                "offset": 0,
                "limit": 25,
            },
            context=context,
        )
        inspect_message = _nested_message(inspect_result)
        diagnostics["inspect_ms"] = _elapsed_ms(started)
        expected_page = {
            "returned_count": 25,
            "has_more": True,
            "next_offset": 25,
        }
        observed_page = {key: inspect_message.get(key) for key in expected_page}
        if observed_page != expected_page:
            raise AssertionError(
                f"Inspection pagination mismatch: expected {expected_page}, "
                f"observed {observed_page}"
            )
        fields = inspect_message.get("fields")
        if not isinstance(fields, list) or len(fields) != 25:
            raise AssertionError("Inspection did not preserve all 25 fields.")

        diagnostics["probe_count"] = len(search_message["probes"])
        diagnostics["total_hit_count"] = message_hit_count
        diagnostics["inspection"] = expected_page

    print(json.dumps(diagnostics, indent=2, sort_keys=True))
    print("catalog tool output contract smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
