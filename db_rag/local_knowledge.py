from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from pydantic import ValidationError

from db_rag.knowledge import (
    PublicationEvidenceHit,
    StudyEvidenceChunk,
    parse_study_evidence,
)
from db_rag.config import EMBEDDING_MODEL
from db_rag.retrieval_status import (
    EmbeddingReasonCode,
    RetrievalOutcome,
    hybrid_status,
    lexical_fallback_status,
)
from db_rag.publication_index import (
    PublicationDesignIndex,
    PublicationIndexIngestionManifest,
)


_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_RRF_K = 60


class SemanticPublicationKnowledgeUnavailableError(RuntimeError):
    """The selected study cannot perform mandatory publication retrieval."""


def _tokens(value: str) -> list[str]:
    return _TOKEN_PATTERN.findall(value.casefold())


def _index_paths(root: Path) -> list[Path]:
    return sorted(
        {
            *root.glob("doi_*.json"),
            *root.glob("pmid_*.json"),
            *root.glob("sha256_*.json"),
        }
    )


def _validated_chunks(root: Path) -> list[StudyEvidenceChunk]:
    manifest_path = root / "ingestion-manifest.json"
    try:
        manifest = PublicationIndexIngestionManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as exc:
        raise ValueError("Invalid publication ingestion manifest.") from exc

    paths = _index_paths(root)
    actual_names = {path.name for path in paths}
    manifest_names = {document.index_filename for document in manifest.documents}
    if actual_names != manifest_names:
        raise ValueError(
            "Publication ingestion manifest document set does not match index files."
        )

    document_by_name = {
        document.index_filename: document for document in manifest.documents
    }
    for path in paths:
        document = document_by_name[path.name]
        try:
            publication = PublicationDesignIndex.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError) as exc:
            raise ValueError(f"Invalid publication index: {path.name}.") from exc
        if hashlib.sha256(path.read_bytes()).hexdigest() != document.index_sha256:
            raise ValueError(f"Invalid publication index hash: {path.name}.")
        if (
            publication.publication_id != document.publication_id
            or publication.review_status.status != document.review_status
        ):
            raise ValueError(f"Invalid publication index identity: {path.name}.")

    return parse_study_evidence(root)


def _hit(chunk: StudyEvidenceChunk) -> PublicationEvidenceHit:
    provenance = {
        "authority": "publication_knowledge",
        "source_id": chunk.source_id,
        "path": chunk.path,
        "section": chunk.section,
    }
    for key in (
        "knowledge_type",
        "knowledge_role",
        "source_locator",
        "indexed_path",
        "evidence_ids",
    ):
        value = str(getattr(chunk, key) or "")
        if value:
            provenance[key] = value
    return PublicationEvidenceHit(
        **chunk.model_dump(mode="python"),
        provenance=provenance,
    )


@dataclass(frozen=True)
class LocalPublicationKnowledge:
    _chunks: tuple[StudyEvidenceChunk, ...]
    _document_frequency: dict[str, int]

    @classmethod
    def from_root(cls, root: Path) -> "LocalPublicationKnowledge":
        resolved = Path(root).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError("Publication knowledge root is unavailable.")
        chunks = tuple(_validated_chunks(resolved))
        document_frequency: Counter[str] = Counter()
        for chunk in chunks:
            document_frequency.update(
                set(
                    _tokens(
                        " ".join(
                            [
                                chunk.title,
                                chunk.section,
                                chunk.knowledge_type,
                                chunk.text,
                            ]
                        )
                    )
                )
            )
        return cls(chunks, dict(document_frequency))

    def search_lexical(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[PublicationEvidenceHit]:
        if limit < 1:
            return []
        query_tokens = _tokens(query)
        if not query_tokens or not self._chunks:
            return []
        unique_query_tokens = set(query_tokens)
        total_chunks = len(self._chunks)
        scored: list[tuple[float, StudyEvidenceChunk]] = []
        for chunk in self._chunks:
            term_frequency = Counter(
                _tokens(
                    " ".join(
                        [
                            chunk.title,
                            chunk.section,
                            chunk.knowledge_type,
                            chunk.text,
                        ]
                    )
                )
            )
            score = sum(
                (
                    1.0
                    + math.log(
                        total_chunks
                        / self._document_frequency[token]
                    )
                )
                * (1.0 + math.log(term_frequency[token]))
                for token in query_tokens
                if term_frequency[token]
            )
            title_overlap = len(unique_query_tokens & set(_tokens(chunk.title)))
            section_overlap = len(
                unique_query_tokens & set(_tokens(chunk.section))
            )
            knowledge_type_overlap = len(
                unique_query_tokens & set(_tokens(chunk.knowledge_type))
            )
            score += 2.0 * title_overlap
            score += 1.5 * section_overlap
            score += 1.5 * knowledge_type_overlap
            if score > 0:
                scored.append((score, chunk))
        scored.sort(
            key=lambda item: (
                -item[0],
                item[1].source_id,
                item[1].indexed_path,
                item[1].id,
            )
        )
        return [_hit(chunk) for _score, chunk in scored[:limit]]

    def search_with_status(
        self,
        query: str,
        *,
        limit: int = 5,
        embedding_model: str = EMBEDDING_MODEL,
        embedding_provider: str | None = None,
        embedding_credential_env: str | None = None,
        reason_code: EmbeddingReasonCode = (
            "EMBEDDING_CONFIGURATION_UNAVAILABLE"
        ),
    ) -> RetrievalOutcome[list[PublicationEvidenceHit]]:
        return RetrievalOutcome(
            value=self.search_lexical(query, limit=limit),
            status=lexical_fallback_status(
                embedding_model,
                reason_code,
                provider=embedding_provider,
                credential_env=embedding_credential_env,
            ),
        )

    def open_source(
        self,
        source_id: str,
        *,
        limit: int = 10,
    ) -> list[PublicationEvidenceHit]:
        if limit < 1:
            return []
        chunks = sorted(
            (
                chunk
                for chunk in self._chunks
                if chunk.source_id == source_id
            ),
            key=lambda chunk: (chunk.indexed_path, chunk.id),
        )
        return [_hit(chunk) for chunk in chunks[:limit]]


def _fuse_hits(
    vector_hits: list[PublicationEvidenceHit],
    lexical_hits: list[PublicationEvidenceHit],
    *,
    limit: int,
) -> list[PublicationEvidenceHit]:
    scores: dict[str, float] = {}
    hits_by_id: dict[str, PublicationEvidenceHit] = {}
    matched_by: dict[str, list[str]] = {}
    for mode, hits in (("vector", vector_hits), ("lexical", lexical_hits)):
        seen: set[str] = set()
        for rank, hit in enumerate(hits, start=1):
            if hit.id in seen:
                continue
            seen.add(hit.id)
            hits_by_id.setdefault(hit.id, hit)
            scores[hit.id] = scores.get(hit.id, 0.0) + 1.0 / (_RRF_K + rank)
            matched_by.setdefault(hit.id, []).append(mode)
    ordered_ids = sorted(
        scores,
        key=lambda chunk_id: (-scores[chunk_id], chunk_id),
    )[:limit]
    results: list[PublicationEvidenceHit] = []
    for chunk_id in ordered_ids:
        hit = hits_by_id[chunk_id]
        results.append(
            hit.model_copy(
                update={
                    "provenance": {
                        **hit.provenance,
                        "matched_by": ",".join(matched_by[chunk_id]),
                    }
                }
            )
        )
    return results


@dataclass(frozen=True)
class SemanticPublicationKnowledge:
    _local: LocalPublicationKnowledge
    _collection: Any
    _embedding_function: Any
    embedding_model: str
    embedding_provider: str | None
    embedding_credential_env: str | None

    def __init__(
        self,
        local: LocalPublicationKnowledge,
        *,
        collection: Any,
        embedding_function: Any,
        embedding_model: str = EMBEDDING_MODEL,
        embedding_provider: str | None = None,
        embedding_credential_env: str | None = None,
    ) -> None:
        object.__setattr__(self, "_local", local)
        object.__setattr__(self, "_collection", collection)
        object.__setattr__(self, "_embedding_function", embedding_function)
        object.__setattr__(self, "embedding_model", embedding_model)
        object.__setattr__(self, "embedding_provider", embedding_provider)
        object.__setattr__(
            self,
            "embedding_credential_env",
            embedding_credential_env,
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[PublicationEvidenceHit]:
        return self.search_with_status(query, limit=limit).value

    def search_with_status(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> RetrievalOutcome[list[PublicationEvidenceHit]]:
        if limit < 1 or not query.strip() or not self._local._chunks:
            return RetrievalOutcome(
                value=[],
                status=hybrid_status(
                    self.embedding_model,
                    provider=self.embedding_provider,
                ),
            )
        candidate_limit = limit * 2
        try:
            embeddings = self._embedding_function.embed_query([query])
        except Exception:
            return self._local.search_with_status(
                query,
                limit=limit,
                embedding_model=self.embedding_model,
                embedding_provider=self.embedding_provider,
                embedding_credential_env=self.embedding_credential_env,
                reason_code="EMBEDDING_PROVIDER_UNAVAILABLE",
            )
        if len(embeddings) != 1:
            raise SemanticPublicationKnowledgeUnavailableError(
                "Publication embedding response is malformed."
            )
        try:
            result = self._collection.query(
                query_embeddings=embeddings,
                n_results=candidate_limit,
                where={"source_kind": "publication"},
                include=["metadatas"],
            )
        except Exception:
            return self._local.search_with_status(
                query,
                limit=limit,
                embedding_model=self.embedding_model,
                embedding_provider=self.embedding_provider,
                embedding_credential_env=self.embedding_credential_env,
                reason_code="EMBEDDING_INDEX_UNAVAILABLE",
            )
        try:
            ids = list(result["ids"][0])
            metadatas = list(result["metadatas"][0])
            if len(ids) != len(metadatas):
                raise ValueError("Publication vector result is malformed.")
            verified_chunks = {
                chunk.id: chunk
                for chunk in self._local._chunks
            }
            vector_hits: list[PublicationEvidenceHit] = []
            seen_ids: set[str] = set()
            for chunk_id, metadata in zip(ids, metadatas):
                normalized_id = str(chunk_id)
                chunk = verified_chunks.get(normalized_id)
                if (
                    chunk is None
                    or normalized_id in seen_ids
                    or not isinstance(metadata, dict)
                ):
                    raise ValueError("Publication vector result is unverified.")
                expected_metadata = chunk.chroma_metadata()
                if any(
                    str(metadata.get(key) or "") != str(expected_value)
                    for key, expected_value in expected_metadata.items()
                ):
                    raise ValueError("Publication vector metadata is stale.")
                seen_ids.add(normalized_id)
                vector_hits.append(_hit(chunk))
            if not vector_hits:
                raise ValueError("Publication vector partition is empty.")
        except Exception as error:
            raise SemanticPublicationKnowledgeUnavailableError(
                "Semantic publication result failed provenance validation."
            ) from error
        lexical_hits = self._local.search_lexical(
            query,
            limit=candidate_limit,
        )
        return RetrievalOutcome(
            value=_fuse_hits(vector_hits, lexical_hits, limit=limit),
            status=hybrid_status(
                self.embedding_model,
                provider=self.embedding_provider,
            ),
        )

    def open_source(
        self,
        source_id: str,
        *,
        limit: int = 10,
    ) -> list[PublicationEvidenceHit]:
        return self._local.open_source(source_id, limit=limit)


@dataclass(frozen=True)
class UnavailableSemanticPublicationKnowledge:
    _local: LocalPublicationKnowledge
    embedding_model: str = EMBEDDING_MODEL
    embedding_provider: str | None = None
    embedding_credential_env: str | None = None
    reason_code: EmbeddingReasonCode = "EMBEDDING_CREDENTIALS_MISSING"

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[PublicationEvidenceHit]:
        return self.search_with_status(query, limit=limit).value

    def search_with_status(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> RetrievalOutcome[list[PublicationEvidenceHit]]:
        return self._local.search_with_status(
            query,
            limit=limit,
            embedding_model=self.embedding_model,
            embedding_provider=self.embedding_provider,
            embedding_credential_env=self.embedding_credential_env,
            reason_code=self.reason_code,
        )

    def open_source(
        self,
        source_id: str,
        *,
        limit: int = 10,
    ) -> list[PublicationEvidenceHit]:
        return self._local.open_source(source_id, limit=limit)


__all__ = [
    "LocalPublicationKnowledge",
    "SemanticPublicationKnowledge",
    "SemanticPublicationKnowledgeUnavailableError",
    "UnavailableSemanticPublicationKnowledge",
]
