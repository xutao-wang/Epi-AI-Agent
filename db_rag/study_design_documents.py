from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re
from typing import Literal

import chromadb

from study_package.manifest import (
    MarkdownStudyDesignManifest,
    StudyPackageManifest,
    resolve_package_path,
)
from utils.env_loader import load_app_environment

from .config import PROJECT_ROOT
from .embedding_routes import EmbeddingRoute, resolve_embedding_route
from .retrieval_status import (
    EmbeddingReasonCode,
    RetrievalOutcome,
    hybrid_status,
    lexical_fallback_status,
)


_TOKEN = re.compile(r"[a-z0-9]+")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_STOPWORDS = frozenset(
    {"a", "an", "and", "for", "from", "in", "of", "on", "or", "the", "to"}
)
_RRF_K = 60


class StudyDesignKnowledgeUnavailableError(RuntimeError):
    """Semantic study-design evidence failed integrity validation."""


def _study_design_source_id(relative_path: str) -> str:
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:24]
    return f"study-design-source.{digest}"


def _study_design_hit_id(
    source_id: str,
    section: str,
    chunk_ordinal: int,
    text: str,
) -> str:
    value = f"{source_id}:{section}:{chunk_ordinal}:{text}"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"study-design.{digest}"


@dataclass(frozen=True)
class StudyDesignHit:
    id: str
    source_kind: Literal["study_design"]
    source_id: str
    source_path: str
    source_sha256: str
    section: str
    text: str
    distance: float | None
    matched_by: tuple[Literal["vector", "lexical"], ...] = ()


@dataclass
class MarkdownStudyDesign:
    study_id: str
    label: str
    package_version: str
    overview_path: Path
    design_root: Path
    chroma_path: Path
    embedding_model: str
    embedding_route: EmbeddingRoute | None = None

    @classmethod
    def from_package(
        cls,
        package_root: Path,
        manifest: StudyPackageManifest,
    ) -> "MarkdownStudyDesign":
        declaration = manifest.study_design
        if not isinstance(declaration, MarkdownStudyDesignManifest):
            raise ValueError("Package does not declare Markdown study design")
        design_root = resolve_package_path(
            package_root,
            declaration.root,
            "study_design.root",
        )
        return cls(
            study_id=manifest.study_id,
            label=manifest.label,
            package_version=manifest.package_version,
            overview_path=design_root / declaration.overview,
            design_root=design_root,
            chroma_path=resolve_package_path(
                package_root,
                manifest.database.index,
                "database.index",
            ),
            embedding_model=manifest.database.embedding_model,
        )

    def render_context(self) -> str:
        return self.overview_path.read_text(encoding="utf-8").strip()

    def with_embedding_route(self, route: EmbeddingRoute) -> "MarkdownStudyDesign":
        return replace(self, embedding_route=route)

    def _resolved_embedding_route(self) -> EmbeddingRoute:
        if self.embedding_route is not None:
            return self.embedding_route
        load_app_environment(PROJECT_ROOT)
        return resolve_embedding_route(os.environ, self.embedding_model)

    def _embedding_function(self):
        return self._resolved_embedding_route().create_embedding_function()

    def _open_client(self):
        return chromadb.PersistentClient(path=str(self.chroma_path))

    def _local_sections(self) -> tuple[StudyDesignHit, ...]:
        root = self.design_root.resolve()
        sections: list[StudyDesignHit] = []
        for path in sorted(root.rglob("*.md")):
            if (
                path.is_symlink()
                or not path.is_file()
                or root not in path.resolve().parents
            ):
                continue
            raw = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            relative = path.relative_to(root).as_posix()
            source_id = _study_design_source_id(relative)
            heading = "Document"
            body: list[str] = []
            parsed: list[tuple[str, str]] = []
            for line in raw.splitlines():
                match = _HEADING.match(line)
                if match:
                    text = "\n".join(body).strip()
                    if text:
                        parsed.append((heading, text))
                    heading = match.group(1).strip()
                    body = []
                else:
                    body.append(line)
            text = "\n".join(body).strip()
            if text:
                parsed.append((heading, text))
            for section, text in parsed:
                chunk_ordinal = 0
                sections.append(
                    StudyDesignHit(
                        id=_study_design_hit_id(
                            source_id,
                            section,
                            chunk_ordinal,
                            text,
                        ),
                        source_kind="study_design",
                        source_id=source_id,
                        source_path=relative,
                        source_sha256=digest,
                        section=section,
                        text=text,
                        distance=None,
                    )
                )
        return tuple(sections)

    def _validated_vector_hits(
        self,
        result: dict[str, object],
        local_sections: tuple[StudyDesignHit, ...],
    ) -> list[StudyDesignHit]:
        ids_partitions = result.get("ids")
        metadata_partitions = result.get("metadatas")
        document_partitions = result.get("documents")
        distance_partitions = result.get("distances")
        if not isinstance(ids_partitions, list) or not ids_partitions:
            raise StudyDesignKnowledgeUnavailableError(
                "Study-design vector result is empty or malformed."
            )
        ids = ids_partitions[0]
        if not isinstance(ids, list) or not ids:
            raise StudyDesignKnowledgeUnavailableError(
                "Study-design vector result is empty or malformed."
            )
        if (
            not isinstance(metadata_partitions, list)
            or not metadata_partitions
            or not isinstance(metadata_partitions[0], list)
            or len(ids) != len(metadata_partitions[0])
        ):
            raise StudyDesignKnowledgeUnavailableError(
                "Study-design vector result is empty or malformed."
            )
        metadatas = metadata_partitions[0]
        if len(set(ids)) != len(ids):
            raise StudyDesignKnowledgeUnavailableError(
                "Study-design vector result contains duplicate evidence IDs."
            )
        documents = (
            document_partitions[0]
            if isinstance(document_partitions, list)
            and document_partitions
            and isinstance(document_partitions[0], list)
            else []
        )
        distances = (
            distance_partitions[0]
            if isinstance(distance_partitions, list)
            and distance_partitions
            and isinstance(distance_partitions[0], list)
            else []
        )
        if documents and len(documents) != len(ids):
            raise StudyDesignKnowledgeUnavailableError(
                "Study-design vector result is empty or malformed."
            )
        if distances and len(distances) != len(ids):
            raise StudyDesignKnowledgeUnavailableError(
                "Study-design vector result is empty or malformed."
            )
        local_by_id = {hit.id: hit for hit in local_sections}
        hits: list[StudyDesignHit] = []
        for index, vector_id in enumerate(ids):
            if not isinstance(vector_id, str) or vector_id not in local_by_id:
                raise StudyDesignKnowledgeUnavailableError(
                    "Study-design vector result contains an unknown evidence ID."
                )
            metadata = metadatas[index]
            local = local_by_id[vector_id]
            if not isinstance(metadata, dict) or any(
                metadata.get(key) != expected
                for key, expected in {
                    "source_kind": local.source_kind,
                    "source_id": local.source_id,
                    "source_path": local.source_path,
                    "source_sha256": local.source_sha256,
                    "section": local.section,
                    "chunk_ordinal": 0,
                    "body_text": local.text,
                }.items()
            ):
                raise StudyDesignKnowledgeUnavailableError(
                    "Study-design vector result has inconsistent provenance."
                )
            distance_value = distances[index] if distances else None
            try:
                distance = float(distance_value) if distance_value is not None else None
            except (TypeError, ValueError) as error:
                raise StudyDesignKnowledgeUnavailableError(
                    "Study-design vector result has an invalid distance."
                ) from error
            hits.append(replace(local, distance=distance))
        return hits

    def _rank_lexical_sections(
        self,
        query: str,
        sections: tuple[StudyDesignHit, ...],
        *,
        limit: int,
    ) -> list[StudyDesignHit]:
        query_tokens = {
            token
            for token in _TOKEN.findall(query.casefold())
            if token not in _STOPWORDS
        }
        ranked: list[tuple[tuple[int, int, int, str], StudyDesignHit]] = []
        for hit in sections:
            section_text = hit.section.casefold()
            body_text = hit.text.casefold()
            heading_overlap = sum(token in section_text for token in query_tokens)
            body_overlap = sum(token in body_text for token in query_tokens)
            phrase = int(query.casefold() in f"{section_text} {body_text}")
            if not (phrase or heading_overlap or body_overlap):
                continue
            ranked.append(
                ((phrase, heading_overlap, body_overlap, hit.source_path), hit)
            )
        ranked.sort(
            key=lambda item: (
                -item[0][0],
                -item[0][1],
                -item[0][2],
                item[0][3],
                item[1].id,
            )
        )
        return [hit for _score, hit in ranked[:limit]]

    @staticmethod
    def _fuse_study_design_hits(
        vector_hits: list[StudyDesignHit],
        lexical_hits: list[StudyDesignHit],
        *,
        limit: int,
    ) -> tuple[StudyDesignHit, ...]:
        scores: dict[str, float] = {}
        hits_by_id: dict[str, StudyDesignHit] = {}
        matched_by: dict[str, list[Literal["vector", "lexical"]]] = {}
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
            key=lambda evidence_id: (-scores[evidence_id], evidence_id),
        )[:limit]
        return tuple(
            replace(hits_by_id[evidence_id], matched_by=tuple(matched_by[evidence_id]))
            for evidence_id in ordered_ids
        )

    def search(self, query: str, limit: int = 5) -> tuple[StudyDesignHit, ...]:
        return self.search_with_status(query, limit=limit).value

    def search_with_status(
        self,
        query: str,
        limit: int = 5,
    ) -> RetrievalOutcome[tuple[StudyDesignHit, ...]]:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise ValueError("Study-design search query must not be blank.")
        if not 1 <= limit <= 10:
            raise ValueError("Study-design search limit must be between 1 and 10.")
        local_sections = self._local_sections()
        route = self._resolved_embedding_route()
        if not route.available:
            return self._lexical_outcome(
                normalized_query,
                limit=limit,
                route=route,
                reason_code=(
                    route.unavailable_reason_code
                    or "EMBEDDING_CONFIGURATION_UNAVAILABLE"
                ),
            )
        try:
            collection = self._open_client().get_collection(
                "study_knowledge",
                embedding_function=self._embedding_function(),
            )
        except Exception:
            return self._lexical_outcome(
                normalized_query,
                limit=limit,
                route=route,
                reason_code="EMBEDDING_INDEX_UNAVAILABLE",
            )
        try:
            result = collection.query(
                query_texts=[normalized_query],
                n_results=limit * 2,
                where={"source_kind": "study_design"},
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            return self._lexical_outcome(
                normalized_query,
                limit=limit,
                route=route,
                reason_code="EMBEDDING_PROVIDER_UNAVAILABLE",
            )
        vector_hits = self._validated_vector_hits(result, local_sections)
        lexical_hits = self._rank_lexical_sections(
            normalized_query,
            local_sections,
            limit=limit * 2,
        )
        return RetrievalOutcome(
            value=self._fuse_study_design_hits(
                vector_hits,
                lexical_hits,
                limit=limit,
            ),
            status=hybrid_status(route.model, provider=route.provider),
        )

    def _lexical_outcome(
        self,
        query: str,
        *,
        limit: int,
        route: EmbeddingRoute,
        reason_code: EmbeddingReasonCode,
    ) -> RetrievalOutcome[tuple[StudyDesignHit, ...]]:
        return RetrievalOutcome(
            value=tuple(
                self._rank_lexical_sections(
                    query,
                    self._local_sections(),
                    limit=limit,
                )
            ),
            status=lexical_fallback_status(
                route.model,
                reason_code,
                provider=route.provider,
                credential_env=route.credential_env,
            ),
        )
