"""Exercise per-call study scoping against the real RePORT India package."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db_rag.config import EMBEDDING_MODEL
from db_rag.session_studies import bind_session_studies
from epi_agent.artifacts import StateArtifactStore
from epi_agent.db_rag.tools import build_db_rag_tool_registry
from epi_agent.protocol import ToolContext
from epi_agent.tool_packs.publication import build_publication_tool_registry
from epi_agent.tool_packs.studies import build_study_discovery_tool_registry
from epi_agent.tool_packs.study_design import build_study_design_tool_registry
from study_package.installer import install_study_archives
from study_package.registry import discover_studies
from utils.env_loader import load_app_environment


STUDY_ID = "report-india-synthetic"
CATALOG_QUERY = "manufactured cigarette smoking intensity per day"
DESIGN_QUERY = "Cohort A pulmonary tuberculosis cases and Cohort B contacts"
PUBLICATION_QUERY = "India tuberculosis cohort eligibility and recruitment"
MAX_SECONDS = 300.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run real RePORT India per-call study-scoping smoke once."
    )
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument(
        "--env-project-root",
        type=Path,
        default=REPO_ROOT,
        help="Project root whose config/app.env and .env should be loaded.",
    )
    return parser


def _json_message(result) -> dict[str, object]:
    return json.loads(result.message)


def main(argv: list[str] | None = None) -> int:
    started = perf_counter()
    args = _parser().parse_args(argv)
    archive = args.archive.expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"RePORT India archive not found: {archive}")

    load_app_environment(args.env_project_root.expanduser().resolve())
    api_key = str(os.environ.get("OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required for the real semantic smoke.")

    with tempfile.TemporaryDirectory(
        prefix="report-study-scoping-smoke-"
    ) as temporary:
        studies_root = Path(temporary) / "studies"
        install_study_archives([archive], studies_root)
        discovered = discover_studies(studies_root)
        bound = bind_session_studies(
            discovered,
            api_key=api_key,
            expected_embedding_model=EMBEDDING_MODEL,
        )
        if bound.studies.ids != (STUDY_ID,):
            raise AssertionError(
                f"Unexpected installed studies: {bound.studies.ids}"
            )
        readiness = bound.readiness[STUDY_ID]
        if not readiness.available:
            raise AssertionError(readiness.message)

        store = StateArtifactStore()
        context = ToolContext(
            studies=bound.studies,
            artifact_store=store,
            thread_id="report-study-scoping-smoke",
            policy=object(),
        )

        directory_result = build_study_discovery_tool_registry().invoke(
            "search_studies",
            {"offset": 0, "limit": 5},
            context=context,
        )
        directory = _json_message(directory_result)
        entries = list(directory.get("studies") or [])
        if len(entries) != 1 or entries[0].get("study_id") != STUDY_ID:
            raise AssertionError(f"Study directory is not RePORT-scoped: {directory}")
        if not entries[0].get("overview_available"):
            raise AssertionError("RePORT study overview is unavailable")

        catalog_result = build_db_rag_tool_registry().invoke(
            "dbrag-search_catalog",
            {
                "study_id": STUDY_ID,
                "queries": [CATALOG_QUERY],
                "limit": 10,
            },
            context=context,
        )
        catalog = _json_message(catalog_result)
        hits = list(list(catalog.get("probes") or [])[0].get("hits") or [])
        scoped_hits = [
            hit
            for hit in hits
            if dict(hit.get("field_ref") or hit.get("table_ref") or {}).get(
                "study_id"
            )
            == STUDY_ID
        ]
        if len(scoped_hits) != len(hits) or not hits:
            raise AssertionError("Catalog hits did not preserve exact study refs")
        smoking_hit = next(
            (
                hit
                for hit in hits
                if dict(hit.get("field_ref") or {}).get("column") == "CIGPAST"
                and "vector" in list(hit.get("matched_by") or [])
            ),
            None,
        )
        if smoking_hit is None:
            raise AssertionError(f"Expected vector-backed CIGPAST hit; got {hits}")
        field_ref = dict(smoking_hit["field_ref"])
        table_ref = {
            key: field_ref[key]
            for key in ("study_id", "source_id", "table")
        }

        inspection_result = build_db_rag_tool_registry().invoke(
            "dbrag-inspect_table",
            {"table_ref": table_ref, "offset": 0, "limit": 25},
            context=context,
        )
        inspection = _json_message(inspection_result)
        fields = list(inspection.get("fields") or [])
        if not fields or any(
            dict(field.get("field_ref") or {}).get("study_id") != STUDY_ID
            for field in fields
        ):
            raise AssertionError("Table inspection lost RePORT study provenance")

        design_result = build_study_design_tool_registry().invoke(
            "study-design-search",
            {"study_id": STUDY_ID, "query": DESIGN_QUERY, "limit": 5},
            context=context,
        )
        design = _json_message(design_result)
        if not design.get("hits") or design.get("study_id") != STUDY_ID:
            raise AssertionError("Study-design retrieval was not RePORT-scoped")

        publication_result = build_publication_tool_registry(
            include_pubmed=False
        ).invoke(
            "publication-search_study_evidence",
            {
                "study_id": STUDY_ID,
                "query": PUBLICATION_QUERY,
                "limit": 5,
            },
            context=context,
        )
        publication = _json_message(publication_result)
        publication_hits = list(publication.get("hits") or [])
        if not publication_hits or any(
            dict(hit.get("source_ref") or {}).get("study_id") != STUDY_ID
            for hit in publication_hits
        ):
            raise AssertionError("Publication retrieval lost RePORT study provenance")

        artifact_studies = {
            artifact.provenance.get("study_id")
            for artifact in store.list_artifacts()
            if artifact.kind != "study_directory"
        }
        if artifact_studies != {STUDY_ID}:
            raise AssertionError(
                f"Saved retrieval provenance is inconsistent: {artifact_studies}"
            )

        diagnostics = {
            "study_id": STUDY_ID,
            "catalog_hits": len(hits),
            "inspected_fields": len(fields),
            "design_hits": len(list(design.get("hits") or [])),
            "publication_hits": len(publication_hits),
            "elapsed_seconds": round(perf_counter() - started, 2),
        }
        if diagnostics["elapsed_seconds"] > MAX_SECONDS:
            raise AssertionError("RePORT study-scoping smoke exceeded five minutes")

    print(json.dumps(diagnostics, indent=2, sort_keys=True))
    print("RePORT India study-scoping smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
