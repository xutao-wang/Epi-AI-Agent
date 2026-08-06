from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Literal

import chromadb

from study_package.manifest import (
    MarkdownStudyDesignManifest,
    StudyPackageManifest,
    resolve_package_path,
)
from utils.env_loader import load_app_environment

from .config import PROJECT_ROOT
from .vectorstore import OpenAIEmbeddingFunction


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
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise ValueError("Study-design search query must not be blank.")
        if not 1 <= limit <= 10:
            raise ValueError("Study-design search limit must be between 1 and 10.")
        collection = self._open_client().get_collection(
            "study_knowledge",
            embedding_function=self._embedding_function(),
        )
        result = collection.query(
            query_texts=[normalized_query],
            n_results=limit,
            where={"source_kind": "study_design"},
            include=["documents", "metadatas", "distances"],
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
        return tuple(hits)
