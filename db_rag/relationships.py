from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import duckdb
from pydantic import BaseModel


_RELATIONSHIP_FIELDS = {
    "nhanes_respondent": ("has_seqn_join", "seqn_col"),
    "report_participant": ("has_subjid_join", "subjid_col"),
    "report_family": ("has_fid_join", "fid_col"),
}


class IdentifierProfile(BaseModel):
    column: str
    distinct_count: int
    null_count: int
    null_rate: float


class TableRelationshipInventory(BaseModel):
    table: str
    row_count: int
    identifier_columns: list[str]
    identifiers: dict[str, IdentifierProfile]


class RelationshipProfile(BaseModel):
    left_table: str
    right_table: str
    key_pairs: list[tuple[str, str]]
    left_distinct_keys: int
    right_distinct_keys: int
    matched_keys: int
    joined_rows: int
    left_cardinality: Literal["one", "many"]
    right_cardinality: Literal["one", "many"]
    warnings: list[str]


class JoinPath(BaseModel):
    tables: list[str]
    profiles: list[RelationshipProfile]


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _text(value: Any) -> str:
    return str(value or "").strip()


def catalog_relationship_keys(
    catalog: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    """Return validated join-key declarations from a catalog-v1 payload."""

    catalog_columns = {
        (_text(row.get("table")), _text(row.get("column")))
        for row in catalog.get("columns") or []
        if isinstance(row, Mapping)
        and _text(row.get("table"))
        and _text(row.get("column"))
    }
    declared: dict[str, dict[str, str]] = {}
    for raw_table in catalog.get("tables") or []:
        if not isinstance(raw_table, Mapping):
            continue
        for domain, (enabled_field, column_field) in _RELATIONSHIP_FIELDS.items():
            if raw_table.get(enabled_field) is not True:
                continue
            table = _text(raw_table.get("table"))
            column = _text(raw_table.get(column_field))
            if not table or not column:
                raise ValueError(
                    f"Incomplete catalog relationship declaration: {domain}"
                )
            if (table, column) not in catalog_columns:
                raise ValueError(
                    "Declared relationship key is not a catalog column: "
                    f"{table}.{column}"
                )
            table_keys = declared.setdefault(table, {})
            existing = table_keys.get(domain)
            if existing is not None and existing != column:
                raise ValueError(
                    "Conflicting catalog relationship declaration: "
                    f"{table}.{domain}"
                )
            table_keys[domain] = column
    return {
        table: dict(sorted(keys.items()))
        for table, keys in sorted(declared.items())
    }


def _nonnull_condition(alias: str, columns: list[str]) -> str:
    return " AND ".join(
        f"{alias}.{_quote_identifier(column)} IS NOT NULL" for column in columns
    )


class RelationshipInventory:
    def __init__(
        self,
        duckdb_path: Path,
        tables: list[TableRelationshipInventory],
    ) -> None:
        self.duckdb_path = Path(duckdb_path)
        self.tables = tables
        self._tables_by_name = {table.table: table for table in tables}
        self._candidate_profiles: list[RelationshipProfile] | None = None

    def require_table(self, table: str) -> TableRelationshipInventory:
        try:
            return self._tables_by_name[table]
        except KeyError as error:
            raise KeyError(f"Unknown runtime table: {table}") from error

    def profile_relationship(
        self,
        left_table: str,
        right_table: str,
        key_pairs: list[tuple[str, str]],
    ) -> RelationshipProfile:
        if not key_pairs:
            raise ValueError("key_pairs must contain at least one column pair")
        left = self.require_table(left_table)
        right = self.require_table(right_table)
        left_columns = [pair[0] for pair in key_pairs]
        right_columns = [pair[1] for pair in key_pairs]
        missing_left = [column for column in left_columns if column not in left.identifiers]
        missing_right = [column for column in right_columns if column not in right.identifiers]
        if missing_left or missing_right:
            raise KeyError(
                "Relationship keys must be profiled identifier columns: "
                f"left={missing_left}, right={missing_right}"
            )

        left_table_sql = _quote_identifier(left_table)
        right_table_sql = _quote_identifier(right_table)
        left_projection = ", ".join(
            f'CAST(l.{_quote_identifier(column)} AS VARCHAR) AS "key_{index}"'
            for index, column in enumerate(left_columns)
        )
        right_projection = ", ".join(
            f'CAST(r.{_quote_identifier(column)} AS VARCHAR) AS "key_{index}"'
            for index, column in enumerate(right_columns)
        )
        key_names = ", ".join(f'"key_{index}"' for index in range(len(key_pairs)))
        join_condition = " AND ".join(
            f'CAST(l.{_quote_identifier(left_column)} AS VARCHAR) = '
            f'CAST(r.{_quote_identifier(right_column)} AS VARCHAR)'
            for left_column, right_column in key_pairs
        )
        left_nonnull = _nonnull_condition("l", left_columns)
        right_nonnull = _nonnull_condition("r", right_columns)

        with duckdb.connect(str(self.duckdb_path), read_only=True) as connection:
            left_distinct = connection.execute(
                f"""
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT {left_projection}
                    FROM {left_table_sql} AS l
                    WHERE {left_nonnull}
                )
                """
            ).fetchone()[0]
            right_distinct = connection.execute(
                f"""
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT {right_projection}
                    FROM {right_table_sql} AS r
                    WHERE {right_nonnull}
                )
                """
            ).fetchone()[0]
            matched_keys = connection.execute(
                f"""
                WITH left_keys AS (
                    SELECT DISTINCT {left_projection}
                    FROM {left_table_sql} AS l
                    WHERE {left_nonnull}
                ),
                right_keys AS (
                    SELECT DISTINCT {right_projection}
                    FROM {right_table_sql} AS r
                    WHERE {right_nonnull}
                )
                SELECT COUNT(*)
                FROM left_keys
                INNER JOIN right_keys USING ({key_names})
                """
            ).fetchone()[0]
            joined_rows = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM {left_table_sql} AS l
                INNER JOIN {right_table_sql} AS r ON {join_condition}
                """
            ).fetchone()[0]
            left_max = connection.execute(
                f"""
                SELECT COALESCE(MAX(key_rows), 0)
                FROM (
                    SELECT COUNT(*) AS key_rows
                    FROM {left_table_sql} AS l
                    WHERE {left_nonnull}
                    GROUP BY {", ".join(f"l.{_quote_identifier(column)}" for column in left_columns)}
                )
                """
            ).fetchone()[0]
            right_max = connection.execute(
                f"""
                SELECT COALESCE(MAX(key_rows), 0)
                FROM (
                    SELECT COUNT(*) AS key_rows
                    FROM {right_table_sql} AS r
                    WHERE {right_nonnull}
                    GROUP BY {", ".join(f"r.{_quote_identifier(column)}" for column in right_columns)}
                )
                """
            ).fetchone()[0]

        warnings: list[str] = []
        if joined_rows > matched_keys:
            warnings.append("row_multiplication")
        if matched_keys < left_distinct:
            warnings.append("unmatched_left_keys")
        if matched_keys < right_distinct:
            warnings.append("unmatched_right_keys")
        if any(left.identifiers[column].null_count for column in left_columns):
            warnings.append("null_left_keys")
        if any(right.identifiers[column].null_count for column in right_columns):
            warnings.append("null_right_keys")

        return RelationshipProfile(
            left_table=left_table,
            right_table=right_table,
            key_pairs=key_pairs,
            left_distinct_keys=int(left_distinct),
            right_distinct_keys=int(right_distinct),
            matched_keys=int(matched_keys),
            joined_rows=int(joined_rows),
            left_cardinality="many" if int(left_max) > 1 else "one",
            right_cardinality="many" if int(right_max) > 1 else "one",
            warnings=warnings,
        )

    def candidate_relationships(self) -> list[RelationshipProfile]:
        if self._candidate_profiles is None:
            candidates: list[RelationshipProfile] = []
            for left_index, left in enumerate(self.tables):
                for right in self.tables[left_index + 1 :]:
                    shared_columns = sorted(
                        set(left.identifier_columns) & set(right.identifier_columns)
                    )
                    for column in shared_columns:
                        profile = self.profile_relationship(
                            left.table,
                            right.table,
                            [(column, column)],
                        )
                        if profile.matched_keys:
                            candidates.append(profile)
            self._candidate_profiles = candidates
        return list(self._candidate_profiles)

    def find_join_paths(
        self,
        left_table: str,
        right_table: str,
        *,
        max_hops: int = 3,
        max_paths: int = 20,
    ) -> list[JoinPath]:
        self.require_table(left_table)
        self.require_table(right_table)
        if max_hops < 1 or max_paths < 1:
            return []

        adjacency: dict[str, list[tuple[str, RelationshipProfile]]] = {}
        for profile in self.candidate_relationships():
            adjacency.setdefault(profile.left_table, []).append(
                (profile.right_table, profile)
            )
            adjacency.setdefault(profile.right_table, []).append(
                (profile.left_table, _reverse_profile(profile))
            )
        for edges in adjacency.values():
            edges.sort(key=lambda edge: (edge[0], edge[1].key_pairs))

        paths: list[JoinPath] = []
        queue = deque([(left_table, [left_table], [])])
        while queue and len(paths) < max_paths:
            current, tables, profiles = queue.popleft()
            if len(profiles) >= max_hops:
                continue
            for neighbor, profile in adjacency.get(current, []):
                if neighbor in tables:
                    continue
                next_tables = [*tables, neighbor]
                next_profiles = [*profiles, profile]
                if neighbor == right_table:
                    paths.append(JoinPath(tables=next_tables, profiles=next_profiles))
                    continue
                queue.append((neighbor, next_tables, next_profiles))
        return paths


_REVERSED_WARNING_CODES = {
    "unmatched_left_keys": "unmatched_right_keys",
    "unmatched_right_keys": "unmatched_left_keys",
    "null_left_keys": "null_right_keys",
    "null_right_keys": "null_left_keys",
}


def reverse_relationship_warning_code(warning: str) -> str:
    return _REVERSED_WARNING_CODES.get(warning, warning)


def _reverse_profile(profile: RelationshipProfile) -> RelationshipProfile:
    return RelationshipProfile(
        left_table=profile.right_table,
        right_table=profile.left_table,
        key_pairs=[(right, left) for left, right in profile.key_pairs],
        left_distinct_keys=profile.right_distinct_keys,
        right_distinct_keys=profile.left_distinct_keys,
        matched_keys=profile.matched_keys,
        joined_rows=profile.joined_rows,
        left_cardinality=profile.right_cardinality,
        right_cardinality=profile.left_cardinality,
        warnings=[
            reverse_relationship_warning_code(warning)
            for warning in profile.warnings
        ],
    )


def build_relationship_inventory(duckdb_path: Path) -> RelationshipInventory:
    path = Path(duckdb_path)
    tables: list[TableRelationshipInventory] = []
    with duckdb.connect(str(path), read_only=True) as connection:
        table_names = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main' AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            ).fetchall()
        ]
        for table_name in table_names:
            columns = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'main' AND table_name = ?
                    ORDER BY ordinal_position
                    """,
                    [table_name],
                ).fetchall()
            ]
            identifier_columns = sorted(
                column for column in columns if _is_identifier_column(column)
            )
            table_sql = _quote_identifier(table_name)
            row_count = int(
                connection.execute(f"SELECT COUNT(*) FROM {table_sql}").fetchone()[0]
            )
            identifiers: dict[str, IdentifierProfile] = {}
            for column in identifier_columns:
                column_sql = _quote_identifier(column)
                distinct_count, null_count = connection.execute(
                    f"""
                    SELECT
                        COUNT(DISTINCT {column_sql}),
                        COUNT(*) FILTER (WHERE {column_sql} IS NULL)
                    FROM {table_sql}
                    """
                ).fetchone()
                identifiers[column] = IdentifierProfile(
                    column=column,
                    distinct_count=int(distinct_count),
                    null_count=int(null_count),
                    null_rate=float(null_count) / row_count if row_count else 0.0,
                )
            tables.append(
                TableRelationshipInventory(
                    table=table_name,
                    row_count=row_count,
                    identifier_columns=identifier_columns,
                    identifiers=identifiers,
                )
            )
    return RelationshipInventory(path, tables)


def profile_relationship(
    duckdb_path: Path,
    left_table: str,
    right_table: str,
    key_pairs: list[tuple[str, str]],
) -> RelationshipProfile:
    return build_relationship_inventory(duckdb_path).profile_relationship(
        left_table,
        right_table,
        key_pairs,
    )


def find_join_paths(
    duckdb_path: Path,
    left_table: str,
    right_table: str,
    *,
    max_hops: int = 3,
    max_paths: int = 20,
) -> list[JoinPath]:
    return build_relationship_inventory(duckdb_path).find_join_paths(
        left_table,
        right_table,
        max_hops=max_hops,
        max_paths=max_paths,
    )
