from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from db_rag.study_design_documents import (
    MarkdownStudyDesign,
    StudyDesignKnowledgeUnavailableError,
)
from db_rag.embedding_routes import resolve_embedding_route
from study_package.manifest import parse_study_package_manifest
from tests.study_package_fixtures import create_package_root, minimal_manifest


class _RecordingCollection:
    def __init__(self, result: dict[str, object]) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = result

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _RecordingClient:
    def __init__(self, collection: _RecordingCollection) -> None:
        self.collection = collection
        self.calls: list[tuple[str, object]] = []

    def get_collection(self, name: str, embedding_function=None):
        self.calls.append((name, embedding_function))
        return self.collection


def _provider(tmp_path: Path) -> MarkdownStudyDesign:
    manifest_data = minimal_manifest(
        format_version=3,
        study_design_format="markdown",
    )
    root = create_package_root(
        tmp_path,
        manifest=manifest_data,
        study_design_documents={
            "overview.md": "# Overview\n\nAuthoritative.\n",
            "reference/visits.md": "# Visits\n\nRetrieval-only schedule.",
        },
    )
    return MarkdownStudyDesign.from_package(
        root,
        parse_study_package_manifest(manifest_data),
    )


def _vector_result(
    provider: MarkdownStudyDesign,
    *,
    hits: list[object] | None = None,
    ids: list[str] | None = None,
    metadata_updates: dict[str, object] | None = None,
    documents: list[str] | None = None,
) -> dict[str, object]:
    selected = hits or [
        next(hit for hit in provider._local_sections() if hit.section == "Visits")
    ]
    metadata_rows = []
    for hit in selected:
        metadata = {
            "source_kind": hit.source_kind,
            "source_id": hit.source_id,
            "source_path": hit.source_path,
            "source_sha256": hit.source_sha256,
            "section": hit.section,
            "chunk_ordinal": 0,
            "body_text": hit.text,
        }
        metadata.update(metadata_updates or {})
        metadata_rows.append(metadata)
    return {
        "ids": [ids or [hit.id for hit in selected]],
        "documents": [documents or [hit.text for hit in selected]],
        "metadatas": [metadata_rows],
        "distances": [[0.125 for _hit in selected]],
    }


def _semantic_provider(
    provider: MarkdownStudyDesign,
    monkeypatch,
    result: dict[str, object],
) -> _RecordingCollection:
    provider = provider.with_embedding_route(
        resolve_embedding_route(
            {"OPENAI_API_KEY": "test-key"},
            provider.embedding_model,
        )
    )
    collection = _RecordingCollection(result)
    monkeypatch.setattr(provider, "_open_client", lambda: _RecordingClient(collection))
    monkeypatch.setattr(provider, "_embedding_function", lambda: object())
    return provider, collection


def test_markdown_study_design_preserves_overview_markdown(tmp_path: Path) -> None:
    provider = _provider(tmp_path)

    assert provider.render_context() == "# Overview\n\nAuthoritative."
    assert provider.study_id == "example-study"
    assert provider.package_version == "1.0.0"


def test_markdown_study_design_search_filters_and_maps_provenance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    provider, collection = _semantic_provider(
        provider,
        monkeypatch,
        _vector_result(provider),
    )

    hits = provider.search("When are visits?", limit=3)

    assert collection.calls == [{
        "query_texts": ["When are visits?"],
        "n_results": 6,
        "where": {"source_kind": "study_design"},
        "include": ["documents", "metadatas", "distances"],
    }]
    assert len(hits) == 1
    assert hits[0].id.startswith("study-design.")
    assert hits[0].source_kind == "study_design"
    assert hits[0].source_id == (
        "study-design-source."
        + hashlib.sha256(b"reference/visits.md").hexdigest()[:24]
    )
    assert hits[0].source_path == "reference/visits.md"
    assert hits[0].source_sha256 == hashlib.sha256(
        (provider.design_root / "reference/visits.md").read_bytes()
    ).hexdigest()
    assert hits[0].section == "Visits"
    assert hits[0].text == "Retrieval-only schedule."
    assert hits[0].distance == 0.125
    assert hits[0].matched_by == ("vector", "lexical")


def test_markdown_study_design_builds_canonical_local_section_identity(
    tmp_path: Path,
) -> None:
    provider = _provider(tmp_path)

    visits = next(hit for hit in provider._local_sections() if hit.section == "Visits")

    assert visits.source_id == (
        "study-design-source."
        + hashlib.sha256(b"reference/visits.md").hexdigest()[:24]
    )
    assert visits.id.startswith("study-design.")
    assert visits.source_sha256 == hashlib.sha256(
        (provider.design_root / "reference/visits.md").read_bytes()
    ).hexdigest()
    assert visits.matched_by == ()


def test_markdown_study_design_rejects_unknown_vector_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    provider, _collection = _semantic_provider(
        provider,
        monkeypatch,
        _vector_result(provider, ids=["study-design.unknown"]),
    )

    with pytest.raises(StudyDesignKnowledgeUnavailableError, match="unknown"):
        provider.search_with_status("When are visits?", limit=3)


def test_markdown_study_design_rejects_stale_vector_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    provider, _collection = _semantic_provider(
        provider,
        monkeypatch,
        _vector_result(provider, metadata_updates={"source_sha256": "b" * 64}),
    )

    with pytest.raises(StudyDesignKnowledgeUnavailableError, match="provenance"):
        provider.search_with_status("When are visits?", limit=3)


def test_markdown_study_design_rejects_inconsistent_vector_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    provider, _collection = _semantic_provider(
        provider,
        monkeypatch,
        _vector_result(provider, metadata_updates={"body_text": "Stale text."}),
    )

    with pytest.raises(StudyDesignKnowledgeUnavailableError, match="provenance"):
        provider.search_with_status("When are visits?", limit=3)


def test_markdown_study_design_rejects_duplicate_vector_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    result = _vector_result(provider)
    vector_id = result["ids"][0][0]
    metadata = result["metadatas"][0][0]
    result["ids"] = [[vector_id, vector_id]]
    result["documents"] = [[metadata["body_text"], metadata["body_text"]]]
    result["metadatas"] = [[metadata, metadata]]
    result["distances"] = [[0.125, 0.25]]
    provider, _collection = _semantic_provider(provider, monkeypatch, result)

    with pytest.raises(StudyDesignKnowledgeUnavailableError, match="duplicate"):
        provider.search_with_status("When are visits?", limit=3)


def test_markdown_study_design_rejects_empty_vector_partition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    provider, _collection = _semantic_provider(
        provider,
        monkeypatch,
        {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]},
    )

    with pytest.raises(StudyDesignKnowledgeUnavailableError, match="empty"):
        provider.search_with_status("When are visits?", limit=3)


def test_markdown_study_design_fuses_vector_and_lexical_sections(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_data = minimal_manifest(
        format_version=3,
        study_design_format="markdown",
    )
    root = create_package_root(
        tmp_path,
        manifest=manifest_data,
        study_design_documents={
            "overview.md": "# Overview\n\nAuthoritative.\n",
            "reference/dual.md": "# Cohort\n\nCohort enrollment.\n",
            "reference/vector.md": "# Renal outcomes\n\nKidney outcomes.\n",
            "reference/lexical.md": (
                "# Cohort eligibility\n\nCohort eligibility criteria.\n"
            ),
        },
    )
    provider = MarkdownStudyDesign.from_package(
        root,
        parse_study_package_manifest(manifest_data),
    )
    sections = provider._local_sections()
    dual = next(hit for hit in sections if hit.section == "Cohort")
    vector_only = next(hit for hit in sections if hit.section == "Renal outcomes")
    lexical_only = next(
        hit for hit in sections if hit.section == "Cohort eligibility"
    )
    provider, collection = _semantic_provider(
        provider,
        monkeypatch,
        _vector_result(provider, hits=[dual, vector_only]),
    )

    outcome = provider.search_with_status("cohort eligibility", limit=3)

    assert collection.calls[0]["n_results"] == 6
    assert outcome.status.mode == "hybrid_vector_lexical"
    assert [hit.id for hit in outcome.value] == [
        dual.id,
        lexical_only.id,
        vector_only.id,
    ]
    assert [hit.matched_by for hit in outcome.value] == [
        ("vector", "lexical"),
        ("lexical",),
        ("vector",),
    ]


def test_markdown_study_design_falls_back_to_ranked_lexical_sections(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "")

    outcome = provider.search_with_status("visit schedule", limit=3)

    reference_path = provider.design_root / "reference/visits.md"
    assert outcome.status.mode == "lexical_fallback"
    assert outcome.status.reason_code == "EMBEDDING_CREDENTIALS_MISSING"
    assert outcome.value[0].source_path == "reference/visits.md"
    assert outcome.value[0].section == "Visits"
    assert outcome.value[0].source_sha256 == hashlib.sha256(
        reference_path.read_bytes()
    ).hexdigest()
    assert outcome.value[0].distance is None


def test_markdown_study_design_uses_bound_unavailable_route_without_chroma(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path).with_embedding_route(
        resolve_embedding_route(
            {"OPENROUTER_API_KEY": "router-key"},
            "OpenRouter/Qwen/qwen3-embedding-8b",
        )
    )
    monkeypatch.setattr(
        provider,
        "_open_client",
        lambda: (_ for _ in ()).throw(AssertionError("Chroma must not open")),
    )

    outcome = provider.search_with_status("visit schedule", limit=3)

    assert outcome.status.mode == "lexical_fallback"
    assert outcome.status.model == "OpenRouter/Qwen/qwen3-embedding-8b"
    assert outcome.status.provider == "openrouter"
    assert outcome.status.reason_code == "EMBEDDING_ROUTE_UNAVAILABLE"
    assert outcome.value[0].source_path == "reference/visits.md"
