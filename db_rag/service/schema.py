from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Any

from db_rag.catalog import load_full_schema_catalog


@lru_cache(maxsize=8)
def _schema_variable_catalog_for_path(
    catalog_path: str,
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[str, list[dict[str, Any]]],
]:
    return _schema_variable_catalog(
        load_full_schema_catalog(Path(catalog_path))
    )


def _schema_variable_catalog(
    catalog: dict[str, Any],
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[str, list[dict[str, Any]]],
]:
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    by_column: dict[str, list[dict[str, Any]]] = {}
    for raw_entry in list(catalog.get("columns") or []):
        if not isinstance(raw_entry, dict):
            continue
        table = str(raw_entry.get("table") or "").strip()
        column = str(raw_entry.get("column") or "").strip()
        if not table or not column:
            continue
        entry: dict[str, Any] = {
            "table": table,
            "column": column,
            "description": str(raw_entry.get("description") or "").strip(),
            "values": raw_entry.get("values"),
            "depends_on": raw_entry.get("depends_on"),
            "condition": raw_entry.get("condition"),
            "section_context": raw_entry.get("section_context"),
        }
        by_pair[(table, column)] = entry
        by_column.setdefault(column, []).append(entry)
    return by_pair, by_column




def _normalize_table_label(table: str) -> str:
    text = str(table or "").strip().lower()
    if not text:
        return ""
    text = text.split(":", 1)[0].strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _catalog_indexes_for_lookup(
    *,
    catalog_path: str | Path | None,
    catalog: dict[str, Any] | None,
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[str, list[dict[str, Any]]],
] | None:
    if catalog is not None:
        return _schema_variable_catalog(catalog)
    if catalog_path is None:
        return None
    return _schema_variable_catalog_for_path(str(catalog_path))


def _lookup_schema_variable_metadata(
    table: str,
    column: str,
    *,
    catalog_path: str | Path | None = None,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    indexes = _catalog_indexes_for_lookup(
        catalog_path=catalog_path,
        catalog=catalog,
    )
    if indexes is None:
        return None
    by_pair, by_column = indexes
    raw_table = str(table or "").strip()
    raw_column = str(column or "").strip()
    if not raw_column:
        return None

    exact = by_pair.get((raw_table, raw_column))
    if exact is not None:
        return dict(exact)

    normalized_table = _normalize_table_label(raw_table)
    if normalized_table:
        matches = [
            entry
            for (entry_table, entry_column), entry in by_pair.items()
            if entry_column == raw_column
            and (
                _normalize_table_label(entry_table).startswith(normalized_table)
                or normalized_table.startswith(_normalize_table_label(entry_table))
            )
        ]
        if len(matches) == 1:
            return dict(matches[0])

    column_matches = list(by_column.get(raw_column) or [])
    if len(column_matches) == 1:
        return dict(column_matches[0])
    return None
