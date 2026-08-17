from __future__ import annotations

import hashlib
import json
from pathlib import Path
from copy import deepcopy

import pytest

from db_rag.local_knowledge import LocalPublicationKnowledge
from tests.test_publication_index import minimal_publication_index


def _fixture_root(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "publication_indexes"
    root.mkdir()
    documents: list[dict[str, str]] = []
    for name, status in (
        ("approved", "manually_verified"),
        ("unverified", "needs_manual_review"),
    ):
        publication_id = f"doi:10.1000/{name}"
        payload = deepcopy(minimal_publication_index())
        payload["publication_id"] = publication_id
        payload["citation"]["doi"] = f"10.1000/{name}"
        payload["review_status"] = (
            {
                "status": "manually_verified",
                "reviewer": "reviewer@example.org",
                "reviewed_at": "2026-07-26T12:00:00Z",
                "review_notes": [],
            }
            if status == "manually_verified"
            else {
                "status": "needs_manual_review",
                "reviewer": None,
                "reviewed_at": None,
                "review_notes": [],
            }
        )
        filename = f"doi_10.1000_{name}.json"
        index_path = root / filename
        index_path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        documents.append(
            {
                "source_filename": f"{name}.pdf",
                "source_sha256": "a" * 64,
                "extracted_text_sha256": "b" * 64,
                "publication_id": publication_id,
                "index_filename": filename,
                "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
                "review_status": status,
            }
        )

    (root / "ingestion-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "model": None,
                "prompt_sha256": "c" * 64,
                "documents": documents,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return root, "doi:10.1000/unverified"


def test_local_publication_knowledge_is_verified_and_deterministic(
    tmp_path: Path,
) -> None:
    root, unverified_source_id = _fixture_root(tmp_path)

    knowledge = LocalPublicationKnowledge.from_root(root)
    hits = knowledge.search_lexical("tuberculosis cohort eligibility", limit=3)

    assert hits
    assert all(hit.source_id != unverified_source_id for hit in hits)
    assert hits == knowledge.search_lexical(
        "tuberculosis cohort eligibility",
        limit=3,
    )
    assert knowledge.open_source(hits[0].source_id)


def test_local_publication_knowledge_rejects_malformed_index(
    tmp_path: Path,
) -> None:
    root, _unverified_source_id = _fixture_root(tmp_path)
    index_path = next(root.glob("doi_*.json"))
    index_path.write_text("{malformed", encoding="utf-8")

    with pytest.raises(ValueError, match="publication index"):
        LocalPublicationKnowledge.from_root(root)


def test_local_publication_knowledge_rejects_manifest_document_mismatch(
    tmp_path: Path,
) -> None:
    root, _unverified_source_id = _fixture_root(tmp_path)
    manifest_path = root / "ingestion-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["documents"] = manifest["documents"][:-1]
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="document set"):
        LocalPublicationKnowledge.from_root(root)
