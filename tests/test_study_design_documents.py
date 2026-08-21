from __future__ import annotations

import hashlib
from pathlib import Path

from db_rag.study_design_documents import MarkdownStudyDesign
from study_package.manifest import parse_study_package_manifest
from tests.study_package_fixtures import create_package_root, minimal_manifest


class _RecordingCollection:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "documents": [["stored embedding document"]],
            "metadatas": [[{
                "source_kind": "study_design",
                "source_id": "study-design-source.fixture",
                "source_path": "reference/visits.md",
                "source_sha256": "a" * 64,
                "section": "Visits",
                "body_text": "Retrieval-only schedule.",
            }]],
            "distances": [[0.125]],
        }


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
    collection = _RecordingCollection()
    client = _RecordingClient(collection)
    monkeypatch.setattr(provider, "_open_client", lambda: client)
    monkeypatch.setattr(provider, "_embedding_function", lambda: object())

    hits = provider.search("When are visits?", limit=3)

    assert collection.calls == [{
        "query_texts": ["When are visits?"],
        "n_results": 3,
        "where": {"source_kind": "study_design"},
        "include": ["documents", "metadatas", "distances"],
    }]
    assert len(hits) == 1
    assert hits[0].source_kind == "study_design"
    assert hits[0].source_id == "study-design-source.fixture"
    assert hits[0].source_path == "reference/visits.md"
    assert hits[0].source_sha256 == "a" * 64
    assert hits[0].section == "Visits"
    assert hits[0].text == "Retrieval-only schedule."
    assert hits[0].distance == 0.125


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
