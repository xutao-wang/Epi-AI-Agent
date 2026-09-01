from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from utils.performance import timing_stage

from .catalog_relationships import CATALOG_VERSION
from .config import EMBEDDING_MODEL
from .retrieval import retrieve_queries
from .retrieval_status import (
    EmbeddingReasonCode,
    RetrievalOutcome,
    hybrid_status,
    lexical_fallback_status,
)


_TOKEN = re.compile(r"[a-z0-9_]+")
_RRF_K = 60
_LEXICAL_STOPWORDS = frozenset(
    {"a", "an", "and", "for", "from", "in", "of", "on", "or", "the", "to"}
)


class SemanticCatalogUnavailableError(RuntimeError):
    """The selected study cannot perform mandatory semantic catalog search."""


class SchemaEvidenceHit(BaseModel):
    source: str | None = None
    table: str
    column: str | None = None
    text: str
    source_kind: Literal["schema"] = "schema"
    provenance: dict[str, str]
    matched_by: tuple[Literal["vector", "lexical"], ...] = ()


class SchemaCatalog:
    def __init__(
        self,
        catalog: dict[str, Any],
        *,
        default_source_id: str | None = None,
        embedding_model: str = EMBEDDING_MODEL,
        embedding_provider: str | None = None,
        embedding_credential_env: str | None = None,
        unavailable_reason_code: EmbeddingReasonCode = (
            "EMBEDDING_CONFIGURATION_UNAVAILABLE"
        ),
    ) -> None:
        self._catalog = dict(catalog)
        self._default_source_id = _as_text(default_source_id)
        self.embedding_model = embedding_model
        self.embedding_provider = embedding_provider
        self.embedding_credential_env = embedding_credential_env
        self._unavailable_reason_code = unavailable_reason_code

    def field_exists(self, table: str, column: str) -> bool:
        return any(
            _as_text(entry.get("table")) == table
            and _as_text(entry.get("column")) == column
            and entry.get("runtime_available", True) is not False
            for entry in self._catalog.get("columns", [])
            if isinstance(entry, dict)
        )

    def field_metadata(
        self,
        source: str,
        table: str,
        column: str,
    ) -> dict[str, Any] | None:
        expected = (_as_text(source), _as_text(table), _as_text(column))
        if not all(expected):
            return None
        for raw_entry in self._catalog.get("columns", []):
            if not isinstance(raw_entry, dict):
                continue
            entry_source = (
                _as_text(raw_entry.get("source") or raw_entry.get("source_id"))
                or self._default_source_id
            )
            identity = (
                entry_source,
                _as_text(raw_entry.get("table")),
                _as_text(raw_entry.get("column")),
            )
            if (
                identity == expected
                and raw_entry.get("runtime_available", True) is not False
            ):
                return deepcopy(raw_entry)
        return None

    def inspect_table(
        self,
        source: str,
        table: str,
        *,
        offset: int = 0,
        limit: int = 25,
    ) -> list[SchemaEvidenceHit]:
        if offset < 0 or limit < 1:
            return []
        rows = [
            dict(entry)
            for entry in self._catalog.get("columns", [])
            if isinstance(entry, dict)
            and _as_text(entry.get("table")) == table
            and entry.get("runtime_available", True) is not False
            and (
                _as_text(entry.get("source") or entry.get("source_id"))
                or self._default_source_id
            )
            == source
        ][offset : offset + limit]
        return [
            _schema_evidence_hit(
                row,
                default_source_id=self._default_source_id,
            )
            for row in rows
        ]

    def search(self, query: str, *, limit: int = 5) -> list[SchemaEvidenceHit]:
        return self.search_many([query], limit=limit)[0]

    def search_many(
        self,
        queries: list[str],
        *,
        limit: int = 5,
    ) -> list[list[SchemaEvidenceHit]]:
        return self.search_many_with_status(queries, limit=limit).value

    def search_many_with_status(
        self,
        queries: list[str],
        *,
        limit: int = 5,
    ) -> RetrievalOutcome[list[list[SchemaEvidenceHit]]]:
        return self._lexical_outcome(
            queries,
            limit=limit,
            reason_code=self._unavailable_reason_code,
        )

    def _lexical_outcome(
        self,
        queries: list[str],
        *,
        limit: int,
        reason_code: EmbeddingReasonCode,
    ) -> RetrievalOutcome[list[list[SchemaEvidenceHit]]]:
        results: list[list[SchemaEvidenceHit]] = []
        for query in queries:
            lexical_rows, exact_keys = _lexical_ranked_rows(
                self._catalog,
                query,
                limit=limit,
            )
            results.append(
                _fuse_ranked_rows(
                    [],
                    lexical_rows,
                    exact_keys,
                    limit=limit,
                    default_source_id=self._default_source_id,
                )
            )
        return RetrievalOutcome(
            value=results,
            status=lexical_fallback_status(
                self.embedding_model,
                reason_code,
                provider=self.embedding_provider,
                credential_env=self.embedding_credential_env,
            ),
        )


class UnavailableSemanticSchemaCatalog(SchemaCatalog):
    """Allow exact inspection and lexical search without semantic retrieval."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault(
            "unavailable_reason_code",
            "EMBEDDING_CREDENTIALS_MISSING",
        )
        super().__init__(*args, **kwargs)


class SemanticSchemaCatalog(SchemaCatalog):
    """Search one study with mandatory vector retrieval and lexical boosting."""

    def __init__(
        self,
        catalog: dict[str, Any],
        *,
        table_collection: Any,
        column_collection: Any,
        embedding_function: Any,
        default_source_id: str | None = None,
        embedding_model: str = EMBEDDING_MODEL,
        embedding_provider: str | None = None,
        embedding_credential_env: str | None = None,
    ) -> None:
        super().__init__(
            catalog,
            default_source_id=default_source_id,
            embedding_model=embedding_model,
            embedding_provider=embedding_provider,
            embedding_credential_env=embedding_credential_env,
        )
        self._table_collection = table_collection
        self._column_collection = column_collection
        self._embedding_function = embedding_function

    def search_many(
        self,
        queries: list[str],
        *,
        limit: int = 5,
    ) -> list[list[SchemaEvidenceHit]]:
        return self.search_many_with_status(queries, limit=limit).value

    def search_many_with_status(
        self,
        queries: list[str],
        *,
        limit: int = 5,
    ) -> RetrievalOutcome[list[list[SchemaEvidenceHit]]]:
        if not queries:
            return RetrievalOutcome(
                value=[],
                status=hybrid_status(
                    self.embedding_model,
                    provider=self.embedding_provider,
                ),
            )
        if limit < 1:
            return RetrievalOutcome(
                value=[[] for _query in queries],
                status=hybrid_status(
                    self.embedding_model,
                    provider=self.embedding_provider,
                ),
            )
        try:
            with timing_stage(
                "db_rag.catalog.query_embedding",
                query_count=len(queries),
            ):
                embeddings = self._embedding_function.embed_query(queries)
        except Exception:
            return self._lexical_outcome(
                queries,
                limit=limit,
                reason_code="EMBEDDING_PROVIDER_UNAVAILABLE",
            )
        if len(embeddings) != len(queries):
            raise SemanticCatalogUnavailableError(
                "Semantic catalog returned an invalid embedding batch."
            )
        try:
            vector_batches = retrieve_queries(
                self._table_collection,
                self._column_collection,
                queries,
                table_k=limit,
                column_k=limit,
                query_embeddings=embeddings,
            )
        except Exception:
            return self._lexical_outcome(
                queries,
                limit=limit,
                reason_code="EMBEDDING_INDEX_UNAVAILABLE",
            )
        if len(vector_batches) != len(queries):
            raise SemanticCatalogUnavailableError(
                "Semantic catalog returned an invalid result batch."
            )

        results: list[list[SchemaEvidenceHit]] = []
        for query, (table_rows, column_rows) in zip(queries, vector_batches):
            with timing_stage("db_rag.catalog.lexical_rank"):
                lexical_rows, exact_keys = _lexical_ranked_rows(
                    self._catalog,
                    query,
                    limit=limit,
                )
            with timing_stage("db_rag.catalog.rank_fusion"):
                results.append(
                    _fuse_ranked_rows(
                        [*table_rows, *column_rows],
                        lexical_rows,
                        exact_keys,
                        limit=limit,
                        default_source_id=self._default_source_id,
                    )
                )
        return RetrievalOutcome(
            value=results,
            status=hybrid_status(
                self.embedding_model,
                provider=self.embedding_provider,
            ),
        )


def _evidence_key(row: dict[str, Any]) -> tuple[str, str]:
    return (_as_text(row.get("table")), _as_text(row.get("column")))


def _lexical_ranked_rows(
    catalog: dict[str, Any],
    query: str,
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    normalized_query = " ".join(_TOKEN.findall(query.casefold()))
    terms = {
        term
        for term in _TOKEN.findall(query.casefold())
        if term not in _LEXICAL_STOPWORDS and (len(term) > 2 or "_" in term)
    }
    entries = [
        *(
            dict(row)
            for row in catalog.get("tables", [])
            if isinstance(row, dict)
            and row.get("runtime_available", True) is not False
        ),
        *(
            dict(row)
            for row in catalog.get("columns", [])
            if isinstance(row, dict)
            and row.get("runtime_available", True) is not False
        ),
    ]
    ranked: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
    exact_keys: set[tuple[str, str]] = set()
    for ordinal, row in enumerate(entries):
        table = _as_text(row.get("table")).casefold()
        column = _as_text(row.get("column")).casefold()
        text = _as_text(row.get("text")).casefold()
        identifiers = {
            value
            for value in (table, column, f"{table}.{column}" if column else "")
            if value
        }
        exact = normalized_query in identifiers
        phrase = bool(normalized_query and normalized_query in text)
        overlap = sum(term in text for term in terms)
        if not (exact or phrase or overlap):
            continue
        key = _evidence_key(row)
        if exact:
            exact_keys.add(key)
        ranked.append(((int(exact), int(phrase), overlap, -ordinal), row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [row for _score, row in ranked[:limit]], exact_keys


def _fuse_ranked_rows(
    vector_rows: list[dict[str, Any]],
    lexical_rows: list[dict[str, Any]],
    exact_keys: set[tuple[str, str]],
    *,
    limit: int,
    default_source_id: str,
) -> list[SchemaEvidenceHit]:
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    scores: dict[tuple[str, str], float] = {}
    modes: dict[tuple[str, str], set[Literal["vector", "lexical"]]] = {}

    for mode, rows in (("vector", vector_rows), ("lexical", lexical_rows)):
        seen: set[tuple[str, str]] = set()
        for rank, raw_row in enumerate(rows, start=1):
            row = dict(raw_row)
            explicit_source = _as_text(row.get("source") or row.get("source_id"))
            if (
                explicit_source
                and default_source_id
                and explicit_source != default_source_id
            ):
                continue
            key = _evidence_key(row)
            if not key[0] or key in seen:
                continue
            seen.add(key)
            row["source"] = explicit_source or default_source_id
            existing = rows_by_key.setdefault(key, row)
            for field, value in row.items():
                if value not in (None, "") and existing.get(field) in (None, ""):
                    existing[field] = value
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            modes.setdefault(key, set()).add(mode)

    ordered_keys = sorted(
        rows_by_key,
        key=lambda key: (
            0 if key in exact_keys else 1,
            -scores[key],
            key[0].casefold(),
            key[1].casefold(),
        ),
    )[:limit]
    mode_order = ("vector", "lexical")
    return [
        _schema_evidence_hit(
            rows_by_key[key],
            default_source_id=default_source_id,
            matched_by=tuple(mode for mode in mode_order if mode in modes[key]),
        )
        for key in ordered_keys
    ]


def _schema_evidence_hit(
    row: dict[str, Any],
    *,
    default_source_id: str = "",
    matched_by: tuple[Literal["vector", "lexical"], ...] = (),
) -> SchemaEvidenceHit:
    source = _as_text(row.get("source") or row.get("source_id"))
    source = source or default_source_id
    table = _as_text(row.get("table"))
    column = _as_text(row.get("column"))
    return SchemaEvidenceHit(
        source=source or None,
        table=table,
        column=column or None,
        text=_as_text(row.get("text")),
        provenance={
            "authority": "runtime_schema_catalog",
            **({"source_id": source} if source else {}),
            "table": table,
            **({"column": column} if column else {}),
        },
        matched_by=matched_by,
    )


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _chunk_metadata(chunk: Any) -> dict[str, Any]:
    if isinstance(chunk, dict):
        metadata = chunk.get("metadata") or {}
        return dict(metadata) if isinstance(metadata, dict) else {}
    metadata = getattr(chunk, "metadata", None)
    return dict(metadata) if isinstance(metadata, dict) else {}


def _chunk_value(chunk: Any, key: str) -> Any:
    if isinstance(chunk, dict) and key in chunk:
        return chunk.get(key)
    metadata = _chunk_metadata(chunk)
    if key in metadata:
        return metadata.get(key)
    return getattr(chunk, key, None)


def _column_entry(chunk: Any) -> dict[str, Any]:
    metadata = _chunk_metadata(chunk)
    entry: dict[str, Any] = {
        "table": _as_text(_chunk_value(chunk, "table")),
        "column": _as_text(_chunk_value(chunk, "column")),
        "text": _as_text(_chunk_value(chunk, "text")),
    }
    for key in ("schema_column", "description"):
        value = _as_text(metadata.get(key))
        if value:
            entry[key] = value
    if "runtime_available" in metadata:
        entry["runtime_available"] = bool(metadata.get("runtime_available"))
    values_json = _as_text(metadata.get("values_json"))
    if values_json:
        try:
            values = json.loads(values_json)
        except json.JSONDecodeError:
            values = None
        if isinstance(values, dict):
            entry["values"] = values
    for key in ("depends_on", "condition", "section_context"):
        value = _as_text(metadata.get(key))
        if value:
            entry[key] = value
    return entry


def _table_entry(
    chunk: Any,
    join_key_ids: tuple[str, ...],
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "table": _as_text(_chunk_value(chunk, "table")),
        "text": _as_text(_chunk_value(chunk, "text")),
    }
    metadata = _chunk_metadata(chunk)
    if "row_count" in metadata:
        entry["row_count"] = metadata["row_count"]
    for key_id in join_key_ids:
        key = f"has_{key_id}_join"
        value = metadata.get(key)
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        entry[key] = value
    return entry


def build_full_schema_catalog(
    *,
    table_chunks: list[Any],
    column_chunks: list[Any],
    source_fingerprint: str,
    join_keys: Mapping[str, str],
    relationships: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    join_key_copy = json.loads(json.dumps(dict(join_keys)))
    relationship_copy = json.loads(
        json.dumps([dict(relationship) for relationship in relationships])
    )
    join_key_ids = tuple(join_key_copy)
    tables = [
        _table_entry(chunk, join_key_ids)
        for chunk in list(table_chunks or [])
        if _as_text(_chunk_value(chunk, "table"))
    ]
    columns = [
        _column_entry(chunk)
        for chunk in list(column_chunks or [])
        if _as_text(_chunk_value(chunk, "table")) and _as_text(_chunk_value(chunk, "column"))
    ]
    return {
        "catalog_version": CATALOG_VERSION,
        "join_keys": join_key_copy,
        "relationships": relationship_copy,
        "source_fingerprint": _as_text(source_fingerprint),
        "include_excel_profiles": True,
        "contains_raw_excel_rows": False,
        "tables": tables,
        "columns": columns,
        "stats": {
            "tables": len(tables),
            "columns": len(columns),
            "table_chars": sum(len(entry["text"]) for entry in tables),
            "column_chars": sum(len(entry["text"]) for entry in columns),
        },
    }


def write_full_schema_catalog(path: Path, catalog: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_full_schema_catalog(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
