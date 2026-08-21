from __future__ import annotations

from dataclasses import dataclass
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
from .retrieval_status import (
    EmbeddingReasonCode,
    RetrievalOutcome,
    hybrid_status,
    lexical_fallback_status,
)
from .vectorstore import OpenAIEmbeddingFunction


_TOKEN = re.compile(r"[a-z0-9]+")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_STOPWORDS = frozenset(
    {"a", "an", "and", "for", "from", "in", "of", "on", "or", "the", "to"}
)


@dataclass(frozen=True)
class StudyDesignHit:
    source_kind: Literal["study_design"]
    source_id: str
    source_path: str
    source_sha256: str
    section: str
    text: str
    distance: float | None


@dataclass
class MarkdownStudyDesign:
    study_id: str
    label: str
    package_version: str
    overview_path: Path
    design_root: Path
    chroma_path: Path
    embedding_model: str

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

    def _embedding_function(self) -> OpenAIEmbeddingFunction:
        load_app_environment(PROJECT_ROOT)
        api_key = str(os.getenv("OPENAI_API_KEY", "") or "").strip()
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for study-design search.")
        return OpenAIEmbeddingFunction(self.embedding_model, api_key=api_key)

    def _open_client(self):
        return chromadb.PersistentClient(path=str(self.chroma_path))

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
        load_app_environment(PROJECT_ROOT)
        if not str(os.getenv("OPENAI_API_KEY", "") or "").strip():
            return self._lexical_outcome(
                normalized_query,
                limit=limit,
                reason_code="EMBEDDING_CREDENTIALS_MISSING",
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
                reason_code="EMBEDDING_INDEX_UNAVAILABLE",
            )
        try:
            result = collection.query(
                query_texts=[normalized_query],
                n_results=limit,
                where={"source_kind": "study_design"},
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            return self._lexical_outcome(
                normalized_query,
                limit=limit,
                reason_code="EMBEDDING_PROVIDER_UNAVAILABLE",
            )
        metadatas = list((result.get("metadatas") or [[]])[0] or [])
        documents = list((result.get("documents") or [[]])[0] or [])
        distances = list((result.get("distances") or [[]])[0] or [])
        hits: list[StudyDesignHit] = []
        for index, metadata in enumerate(metadatas):
            if str(metadata.get("source_kind") or "") != "study_design":
                continue
            distance_value = distances[index] if index < len(distances) else None
            text = str(metadata.get("body_text") or "").strip()
            if not text and index < len(documents):
                text = str(documents[index] or "").strip()
            hits.append(
                StudyDesignHit(
                    source_kind="study_design",
                    source_id=str(metadata.get("source_id") or ""),
                    source_path=str(metadata.get("source_path") or ""),
                    source_sha256=str(metadata.get("source_sha256") or ""),
                    section=str(metadata.get("section") or "Document"),
                    text=text,
                    distance=(
                        float(distance_value)
                        if distance_value is not None
                        else None
                    ),
                )
            )
        return RetrievalOutcome(
            value=tuple(hits),
            status=hybrid_status(self.embedding_model),
        )

    def _lexical_outcome(
        self,
        query: str,
        *,
        limit: int,
        reason_code: EmbeddingReasonCode,
    ) -> RetrievalOutcome[tuple[StudyDesignHit, ...]]:
        query_tokens = {
            token
            for token in _TOKEN.findall(query.casefold())
            if token not in _STOPWORDS
        }
        ranked: list[tuple[tuple[int, int, int, str, int], StudyDesignHit]] = []
        root = self.design_root.resolve()
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
            sections: list[tuple[str, str]] = []
            heading = "Document"
            body: list[str] = []
            for line in raw.splitlines():
                match = _HEADING.match(line)
                if match:
                    if "\n".join(body).strip():
                        sections.append((heading, "\n".join(body).strip()))
                    heading = match.group(1).strip()
                    body = []
                else:
                    body.append(line)
            if "\n".join(body).strip():
                sections.append((heading, "\n".join(body).strip()))
            for ordinal, (section, text) in enumerate(sections):
                section_text = section.casefold()
                body_text = text.casefold()
                heading_overlap = sum(token in section_text for token in query_tokens)
                body_overlap = sum(token in body_text for token in query_tokens)
                phrase = int(query.casefold() in f"{section_text} {body_text}")
                if not (phrase or heading_overlap or body_overlap):
                    continue
                hit = StudyDesignHit(
                    source_kind="study_design",
                    source_id=hashlib.sha256(
                        f"{relative}#{ordinal}".encode("utf-8")
                    ).hexdigest(),
                    source_path=relative,
                    source_sha256=digest,
                    section=section,
                    text=text,
                    distance=None,
                )
                ranked.append(
                    ((phrase, heading_overlap, body_overlap, relative, -ordinal), hit)
                )
        ranked.sort(
            key=lambda item: (
                -item[0][0],
                -item[0][1],
                -item[0][2],
                item[0][3],
                -item[0][4],
            )
        )
        return RetrievalOutcome(
            value=tuple(hit for _score, hit in ranked[:limit]),
            status=lexical_fallback_status(self.embedding_model, reason_code),
        )
