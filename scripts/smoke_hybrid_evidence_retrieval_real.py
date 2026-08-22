#!/usr/bin/env python3
"""Exercise all production hybrid-evidence tools against one installed study."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.deployment import native_study_root
from db_rag.config import resolve_db_rag_embedding_model
from db_rag.embedding_routes import resolve_embedding_route
from db_rag.session_studies import bind_session_studies
from epi_agent.artifacts import StateArtifactStore
from epi_agent.db_rag.tools import build_db_rag_tool_registry
from epi_agent.protocol import ToolContext
from epi_agent.studies import SearchableStudyDesignProvider
from epi_agent.tool_packs.publication import build_publication_tool_registry
from epi_agent.tool_packs.study_design import build_study_design_tool_registry
from study_package.registry import discover_studies
from utils.env_loader import load_app_environment


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    return parser


def _timeout(_signum: int, _frame: object) -> None:
    raise TimeoutError("Hybrid evidence smoke exceeded its deadline.")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _catalog_hits(content: dict[str, object]) -> list[dict[str, object]]:
    hits: list[dict[str, object]] = []
    for probe in content.get("probes") or []:
        if isinstance(probe, dict):
            hits.extend(hit for hit in probe.get("hits") or [] if isinstance(hit, dict))
    return hits


def _matched_modes(value: object) -> tuple[str, ...]:
    raw_modes = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw_modes, (list, tuple)):
        return ()
    modes: list[str] = []
    for mode in raw_modes:
        normalized = str(mode).strip()
        if normalized in {"vector", "lexical"} and normalized not in modes:
            modes.append(normalized)
    return tuple(modes)


def _verify_artifact(content: dict[str, object], *, catalog: bool) -> int:
    retrieval_mode = str(content.get("retrieval_mode") or "missing")
    embedding = content.get("embedding")
    reason_code = (
        str(embedding.get("reason_code") or "none")
        if isinstance(embedding, dict)
        else "missing"
    )
    _require(
        retrieval_mode == "hybrid_vector_lexical",
        "A retrieval artifact reported mode "
        f"{retrieval_mode!r} with reason {reason_code!r}, "
        "not hybrid_vector_lexical.",
    )
    _require(
        isinstance(embedding, dict) and embedding.get("available") is True,
        "A retrieval artifact did not report an available embedding route.",
    )
    hits = _catalog_hits(content) if catalog else content.get("hits")
    _require(isinstance(hits, list) and hits, "A retrieval artifact had no evidence.")
    for hit in hits:
        _require(isinstance(hit, dict), "A retrieval artifact had malformed evidence.")
        modes = _matched_modes(hit.get("matched_by"))
        _require(
            bool(modes),
            "Evidence is missing auditable matched_by provenance.",
        )
    return len(hits)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.timeout_seconds <= 300:
        raise SystemExit("--timeout-seconds must be between 1 and 300.")
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(args.timeout_seconds)
    started = time.monotonic()
    stage = "startup"
    try:
        load_app_environment(PROJECT_ROOT)
        embedding_model = resolve_db_rag_embedding_model(os.environ)
        route = resolve_embedding_route(os.environ, embedding_model)
        _require(
            route.available and bool(os.environ.get("OPENAI_API_KEY", "").strip()),
            "OPENAI_API_KEY and the configured OpenAI embedding route are required.",
        )
        configured_root = os.environ.get("REPORT_AGENT_STUDY_ROOT", "").strip()
        study_root = Path(configured_root) if configured_root else native_study_root(PROJECT_ROOT)
        discovered = discover_studies(study_root / "studies")
        eligible = [
            study
            for study in discovered.values
            if study.catalog is not None
            and study.knowledge is not None
            and isinstance(study.study_design, SearchableStudyDesignProvider)
        ]
        _require(eligible, "No installed study provides catalog, publication, and Markdown design evidence.")
        study_id = eligible[0].study_id
        bound = bind_session_studies(discovered, embedding_route=route)
        studies = bound.studies
        store = StateArtifactStore()
        context = ToolContext(
            studies=studies,
            artifact_store=store,
            thread_id="real-hybrid-evidence-smoke",
            policy=None,
        )
        stage = "catalog retrieval"
        catalog_result = build_db_rag_tool_registry().invoke(
            "dbrag-search_catalog",
            {"study_id": study_id, "queries": ["participant identifier", "age"], "limit": 5},
            context=context,
        )
        stage = "publication retrieval"
        publication_result = build_publication_tool_registry(include_pubmed=False).invoke(
            "publication-search_study_evidence",
            {"study_id": study_id, "query": "study design", "limit": 5},
            context=context,
        )
        stage = "study-design retrieval"
        design_result = build_study_design_tool_registry().invoke(
            "study-design-search",
            {"study_id": study_id, "query": "participant visits", "limit": 5},
            context=context,
        )
        stage = "catalog artifact validation"
        catalog_count = _verify_artifact(
            store.require(catalog_result.artifacts[0]).content,
            catalog=True,
        )
        stage = "publication artifact validation"
        publication_count = _verify_artifact(
            store.require(publication_result.artifacts[0]).content,
            catalog=False,
        )
        stage = "study-design artifact validation"
        design_count = _verify_artifact(
            store.require(design_result.artifacts[0]).content,
            catalog=False,
        )
        elapsed = time.monotonic() - started
        print(
            "hybrid evidence smoke passed "
            f"study={study_id} model={route.model} "
            f"catalog={catalog_count} publication={publication_count} "
            f"study_design={design_count} elapsed_seconds={elapsed:.2f}"
        )
        return 0
    except (RuntimeError, TimeoutError) as error:
        print(
            f"hybrid evidence smoke failed at {stage}: {error}",
            file=sys.stderr,
        )
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
