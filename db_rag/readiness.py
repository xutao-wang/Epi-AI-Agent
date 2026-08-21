from __future__ import annotations

import json
from typing import Literal

import chromadb
from chromadb.errors import ChromaError
import duckdb
from pydantic import BaseModel, ConfigDict

from .catalog import CATALOG_VERSION, load_full_schema_catalog
from .catalog_relationships import parse_catalog_relationships
from .config import DbRagRuntimePaths
from .relationships import build_relationship_inventory


class DbRagReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["available", "not_configured"]
    message: str

    @property
    def available(self) -> bool:
        return self.status == "available"


def _not_configured(message: str) -> DbRagReadiness:
    return DbRagReadiness(status="not_configured", message=message)


def resolve_db_rag_readiness(
    *,
    paths: DbRagRuntimePaths,
    expected_embedding_model: str | None = None,
) -> DbRagReadiness:
    """Validate the selected study package's DB-RAG runtime assets."""

    package_model = str(paths.embedding_model or "").strip()
    configured_model = str(expected_embedding_model or package_model).strip()
    if not package_model or configured_model != package_model:
        return _not_configured(
            "DB-RAG dataset is not configured: package embedding model mismatch."
        )
    if not paths.duckdb_path.is_file() or not paths.catalog_path.is_file():
        return _not_configured(
            "DB-RAG dataset is not configured: missing package database or catalog."
        )
    if not paths.chroma_path.is_dir():
        return _not_configured(
            "DB-RAG dataset is not configured: missing package Chroma index."
        )
    try:
        catalog = load_full_schema_catalog(paths.catalog_path)
    except (OSError, json.JSONDecodeError) as error:
        return _not_configured(
            f"DB-RAG dataset is not configured: the schema catalog is unreadable ({error})."
        )
    if not isinstance(catalog, dict) or catalog.get("catalog_version") != CATALOG_VERSION:
        return _not_configured(
            "DB-RAG dataset is not configured: the schema catalog must use "
            f"catalog_version {CATALOG_VERSION}."
        )
    if (
        not isinstance(catalog.get("tables"), list)
        or not catalog["tables"]
        or not isinstance(catalog.get("columns"), list)
        or not catalog["columns"]
    ):
        return _not_configured(
            "DB-RAG dataset is not configured: the schema catalog is incomplete."
        )
    try:
        relationship_spec = parse_catalog_relationships(catalog)
        inventory = build_relationship_inventory(
            paths.duckdb_path,
            relationship_spec=relationship_spec,
        )
        inventory.validate_declared_relationships()
    except (OSError, ValueError, duckdb.Error) as error:
        return _not_configured(
            f"DB-RAG dataset is not configured: the DuckDB database is unreadable ({error})."
        )
    if not inventory.tables:
        return _not_configured(
            "DB-RAG dataset is not configured: the DuckDB database has no tables."
        )
    try:
        client = chromadb.PersistentClient(path=str(paths.chroma_path))
        for collection_name in ("table_summaries", "column_chunks"):
            collection = client.get_collection(collection_name)
            if collection.count() < 1:
                return _not_configured(
                    "DB-RAG dataset is not configured: required package Chroma "
                    f"collection {collection_name} is empty."
                )
    except (OSError, ValueError, ChromaError) as error:
        return _not_configured(
            "DB-RAG dataset is not configured: required package Chroma collection "
            f"is unavailable ({error})."
        )
    return DbRagReadiness(
        status="available",
        message="DB-RAG dataset is available.",
    )


__all__ = ["DbRagReadiness", "resolve_db_rag_readiness"]
