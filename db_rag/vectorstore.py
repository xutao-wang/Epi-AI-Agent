from __future__ import annotations

import json
import threading
from collections import OrderedDict
from hashlib import sha256
from pathlib import Path
from typing import Any

from utils.env_loader import load_app_environment
from utils.performance import timing_stage

from .config import EMBEDDING_MODEL, resolve_db_rag_request_timeout_seconds


class RerankerUnavailable(RuntimeError):
    """A requested reranker is unavailable in the OpenAI-only application."""


class OpenAIEmbeddingFunction:
    _MAX_EMPTY_DATA_RETRIES = 1
    _QUERY_CACHE_SIZE = 64

    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        provider_model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ):
        resolved_model = str(model or "").strip()
        if not resolved_model:
            raise ValueError("DB-RAG embedding model must not be blank.")
        resolved_provider_model = str(provider_model or "").strip()
        if not resolved_provider_model:
            resolved_provider_model = resolved_model.split("/", 1)[-1]

        from openai import OpenAI

        self._query_cache: OrderedDict[bytes, tuple[float, ...]] = OrderedDict()
        self._query_cache_lock = threading.Lock()
        self.config_model = resolved_model
        self.model = resolved_provider_model
        client_options: dict[str, Any] = dict(
            api_key=api_key,
            max_retries=0,
            timeout=(
                timeout_seconds
                if timeout_seconds is not None
                else resolve_db_rag_request_timeout_seconds()
            ),
        )
        if base_url is not None:
            client_options["base_url"] = base_url
        self.client = OpenAI(**client_options)

    @staticmethod
    def name() -> str:
        return "openai"

    @staticmethod
    def _normalize_input(input: str | list[str]) -> list[str]:
        if isinstance(input, str):
            return [input]
        return list(input)

    def embed_query(self, input: str | list[str]) -> list[list[float]]:
        return self.__call__(input)

    def _create_embedding_batch(self, batch: list[str]) -> list[list[float]]:
        for _attempt in range(self._MAX_EMPTY_DATA_RETRIES):
            with timing_stage(
                "db_rag.embedding",
                provider="openai",
                model=self.config_model,
                batch_size=len(batch),
            ):
                response = self.client.embeddings.create(
                    model=self.model,
                    input=batch,
                )
            data = getattr(response, "data", None)
            if data:
                return [item.embedding for item in data]
        preview = batch[0][:120] if batch else ""
        raise ValueError(
            f"No embedding data received for model {self.config_model} "
            f"after {self._MAX_EMPTY_DATA_RETRIES} attempts. "
            f"Query preview: {preview!r}"
        )

    def __call__(self, input: str | list[str]) -> list[list[float]]:
        normalized_input = self._normalize_input(input)
        cache_keys = [
            sha256(text.encode("utf-8")).digest()
            for text in normalized_input
        ]
        vectors: dict[bytes, tuple[float, ...]] = {}
        missing: dict[bytes, str] = {}
        with self._query_cache_lock:
            for cache_key, text in zip(cache_keys, normalized_input):
                cached = self._query_cache.get(cache_key)
                if cached is None:
                    missing.setdefault(cache_key, text)
                    continue
                vectors[cache_key] = cached
                self._query_cache.move_to_end(cache_key)

        missing_keys = list(missing)
        missing_texts = [missing[key] for key in missing_keys]
        missing_embeddings: list[list[float]] = []
        for start in range(0, len(missing_texts), 100):
            batch = missing_texts[start : start + 100]
            missing_embeddings.extend(self._create_embedding_batch(batch))
        if len(missing_embeddings) != len(missing_keys):
            raise ValueError("Embedding response count does not match input count.")
        vectors.update(
            {
                key: tuple(embedding)
                for key, embedding in zip(missing_keys, missing_embeddings)
            }
        )
        if missing_keys:
            with self._query_cache_lock:
                for cache_key in missing_keys:
                    self._query_cache[cache_key] = vectors[cache_key]
                    self._query_cache.move_to_end(cache_key)
                while len(self._query_cache) > self._QUERY_CACHE_SIZE:
                    self._query_cache.popitem(last=False)
        return [list(vectors[key]) for key in cache_keys]


class OpenAIReranker:
    def __init__(self, model: str | None):
        if str(model or "").strip():
            raise ValueError(
                "DB-RAG reranking is disabled in the OpenAI-only application."
            )

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        del query
        return [0.0] * len(documents)


def build_chroma(
    table_chunks: list[dict[str, object]],
    column_chunks: list[dict[str, object]],
    *,
    model: str,
    api_key: str,
    chroma_dir: Path,
    knowledge_chunks: list[Any] | None = None,
) -> None:
    import chromadb

    if chroma_dir.exists():
        for child in chroma_dir.iterdir():
            if child.is_file():
                child.unlink()
    chroma_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(chroma_dir))
    for collection_name in (
        "table_summaries",
        "column_chunks",
        "study_knowledge",
    ):
        try:
            client.delete_collection(collection_name)
        except Exception:
            pass
    embedding_function = OpenAIEmbeddingFunction(model=model, api_key=api_key)
    table_collection = client.create_collection(
        "table_summaries",
        embedding_function=embedding_function,
    )
    column_collection = client.create_collection(
        "column_chunks",
        embedding_function=embedding_function,
    )
    knowledge_collection = client.create_collection(
        "study_knowledge",
        embedding_function=embedding_function,
    )
    table_collection.add(
        ids=[chunk["id"] for chunk in table_chunks],
        documents=[chunk["text"] for chunk in table_chunks],
        metadatas=[chunk["metadata"] for chunk in table_chunks],
    )
    column_collection.add(
        ids=[chunk["id"] for chunk in column_chunks],
        documents=[chunk["text"] for chunk in column_chunks],
        metadatas=[chunk["metadata"] for chunk in column_chunks],
    )
    knowledge_rows = list(knowledge_chunks or [])
    if knowledge_rows:
        knowledge_collection.add(
            ids=[str(getattr(chunk, "id")) for chunk in knowledge_rows],
            documents=[
                str(getattr(chunk, "embedding_text")())
                for chunk in knowledge_rows
            ],
            metadatas=[
                getattr(chunk, "chroma_metadata")()
                for chunk in knowledge_rows
            ],
        )


def replace_study_knowledge(
    *,
    model: str,
    api_key: str,
    chroma_dir: Path,
    knowledge_chunks: list[Any],
) -> None:
    import chromadb

    chroma_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(chroma_dir))
    try:
        client.delete_collection("study_knowledge")
    except Exception:
        pass
    embedding_function = OpenAIEmbeddingFunction(model=model, api_key=api_key)
    collection = client.create_collection(
        "study_knowledge",
        embedding_function=embedding_function,
    )
    if knowledge_chunks:
        collection.add(
            ids=[str(getattr(chunk, "id")) for chunk in knowledge_chunks],
            documents=[
                str(getattr(chunk, "embedding_text")())
                for chunk in knowledge_chunks
            ],
            metadatas=[
                getattr(chunk, "chroma_metadata")()
                for chunk in knowledge_chunks
            ],
        )


def write_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = manifest_path.with_suffix(f"{manifest_path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary_path.replace(manifest_path)
