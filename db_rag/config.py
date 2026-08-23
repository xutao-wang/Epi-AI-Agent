from __future__ import annotations

from dataclasses import dataclass
import os
from collections.abc import Mapping
from pathlib import Path

from study_package.manifest import StudyPackageManifest, resolve_package_path
from utils.env_loader import load_app_environment


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EMBEDDING_MODEL = "OpenAI/text-embedding-3-large"
SUPPORTED_DB_RAG_EMBEDDING_MODELS = (
    EMBEDDING_MODEL,
)
SUPPORTED_DB_RAG_RERANKER_MODELS: tuple[str, ...] = ()
DEFAULT_DB_RAG_REQUEST_TIMEOUT_SECONDS = 75.0


@dataclass(frozen=True)
class DbRagRuntimePaths:
    duckdb_path: Path
    catalog_path: Path
    chroma_path: Path
    embedding_model: str


def resolve_db_rag_runtime_paths(
    package_root: Path,
    manifest: StudyPackageManifest,
) -> DbRagRuntimePaths:
    return DbRagRuntimePaths(
        duckdb_path=resolve_package_path(
            package_root,
            manifest.database.duckdb,
            "database.duckdb",
        ),
        catalog_path=resolve_package_path(
            package_root,
            manifest.database.catalog,
            "database.catalog",
        ),
        chroma_path=resolve_package_path(
            package_root,
            manifest.database.index,
            "database.index",
        ),
        embedding_model=manifest.database.embedding_model,
    )


def resolve_db_rag_embedding_model(
    environ: Mapping[str, str] | None = None,
) -> str:
    if environ is None:
        load_app_environment(PROJECT_ROOT)
        environ = os.environ
    model = str(environ.get("DB_RAG_EMBEDDING_MODEL", "") or "").strip()
    if not model:
        return EMBEDDING_MODEL
    return model


def resolve_db_rag_request_timeout_seconds() -> float:
    load_app_environment(PROJECT_ROOT)
    raw_value = str(
        os.getenv(
            "DB_RAG_REQUEST_TIMEOUT_SECONDS",
            DEFAULT_DB_RAG_REQUEST_TIMEOUT_SECONDS,
        )
    ).strip()
    try:
        timeout = float(raw_value)
    except ValueError as error:
        raise ValueError(
            "DB_RAG_REQUEST_TIMEOUT_SECONDS must be numeric."
        ) from error
    if timeout <= 0:
        raise ValueError(
            "DB_RAG_REQUEST_TIMEOUT_SECONDS must be positive."
        )
    return timeout


def resolve_db_rag_reranker_model() -> str | None:
    load_app_environment(PROJECT_ROOT)
    model = str(os.getenv("DB_RAG_RERANKER_MODEL", "") or "").strip()
    if not model:
        return None
    if model not in SUPPORTED_DB_RAG_RERANKER_MODELS:
        supported = ", ".join(SUPPORTED_DB_RAG_RERANKER_MODELS)
        raise ValueError(f"Unsupported DB_RAG_RERANKER_MODEL '{model}'. Supported values: {supported}.")
    return model


def resolve_db_rag_dataset_naming_model() -> str | None:
    load_app_environment(PROJECT_ROOT)
    model = str(os.getenv("DB_RAG_DATASET_NAMING_MODEL", "") or "").strip()
    return model or "gpt-5.6-luna"
