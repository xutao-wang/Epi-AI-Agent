from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Literal

import duckdb
from pydantic import BaseModel, ConfigDict

from db_rag.catalog_relationships import (
    Cardinality,
    CatalogRelationshipSpec,
    RelationshipAuthorization,
    reverse_expected_cardinality,
)


class IdentifierProfile(BaseModel):
    column: str
    distinct_count: int
    null_count: int
    null_rate: float


class TableRelationshipInventory(BaseModel):
    table: str
    row_count: int
    columns: list[str]
    relationship_keys: dict[str, str]
    identifier_columns: list[str]
    identifiers: dict[str, IdentifierProfile]


class RelationshipEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    left_column: str
    right_column: str
    left_join_key: str
    right_join_key: str
    source: Literal["shared_join_key", "declared_relationship"]
    relationship_id: str | None = None
    expected_cardinality: Cardinality | None = None
    note: str | None = None
    direction: Literal["forward", "reverse"] | None = None


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
    relationship_evidence: list[RelationshipEvidence]


class JoinPath(BaseModel):
    tables: list[str]
    profiles: list[RelationshipProfile]


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _nonnull_condition(alias: str, columns: list[str]) -> str:
    return " AND ".join(
        f"{alias}.{_quote_identifier(column)} IS NOT NULL" for column in columns
    )


def _relationship_evidence(
    authorization: RelationshipAuthorization,
) -> RelationshipEvidence:
    return RelationshipEvidence(
        left_column=authorization.left_endpoint.column,
        right_column=authorization.right_endpoint.column,
        left_join_key=authorization.left_endpoint.join_key,
        right_join_key=authorization.right_endpoint.join_key,
        source=authorization.source,
        relationship_id=authorization.relationship_id,
        expected_cardinality=authorization.expected_cardinality,
        note=authorization.note,
        direction=authorization.direction,
    )


def _unordered_edge(
    left_table: str,
    left_column: str,
    right_table: str,
    right_column: str,
) -> tuple[tuple[str, str], tuple[str, str]]:
    endpoints = sorted(
        ((left_table, left_column), (right_table, right_column))
    )
    return endpoints[0], endpoints[1]


class RelationshipInventory:
    def __init__(
        self,
        duckdb_path: Path,
        tables: list[TableRelationshipInventory],
        specification: CatalogRelationshipSpec,
    ) -> None:
        self.duckdb_path = Path(duckdb_path)
        self.tables = tables
        self.specification = specification
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
        authorizations = [
            self.specification.authorize_pair(
                left_table,
                left_column,
                right_table,
                right_column,
            )
            for left_column, right_column in key_pairs
        ]
        unauthorized = [
            pair
            for pair, authorization in zip(key_pairs, authorizations, strict=True)
            if authorization is None
        ]
        if unauthorized:
            raise KeyError(
                "Relationship keys are not authorized by the study catalog: "
                f"{unauthorized}"
            )
        evidence = [
            _relationship_evidence(authorization)
            for authorization in authorizations
            if authorization is not None
        ]

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
            relationship_evidence=evidence,
        )

    def candidate_relationships(self) -> list[RelationshipProfile]:
        if self._candidate_profiles is None:
            candidates: list[RelationshipProfile] = []
            seen_edges: set[
                tuple[tuple[str, str], tuple[str, str]]
            ] = set()

            for relationship in self.specification.relationships:
                left = relationship.from_endpoint
                right = relationship.to_endpoint
                edge = _unordered_edge(
                    left.table,
                    left.column,
                    right.table,
                    right.column,
                )
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
                profile = self.profile_relationship(
                    left.table,
                    right.table,
                    [(left.column, right.column)],
                )
                if profile.matched_keys:
                    candidates.append(profile)

            for left_index, left in enumerate(self.tables):
                for right in self.tables[left_index + 1 :]:
                    shared_keys = sorted(
                        set(left.relationship_keys) & set(right.relationship_keys)
                    )
                    for key_id in shared_keys:
                        pair = (
                            left.relationship_keys[key_id],
                            right.relationship_keys[key_id],
                        )
                        edge = _unordered_edge(
                            left.table,
                            pair[0],
                            right.table,
                            pair[1],
                        )
                        if edge in seen_edges:
                            continue
                        seen_edges.add(edge)
                        profile = self.profile_relationship(
                            left.table,
                            right.table,
                            [pair],
                        )
                        if profile.matched_keys:
                            candidates.append(profile)
            self._candidate_profiles = candidates
        return list(self._candidate_profiles)

    def validate_declared_relationships(self) -> None:
        for relationship in self.specification.relationships:
            profile = self.profile_relationship(
                relationship.from_endpoint.table,
                relationship.to_endpoint.table,
                [
                    (
                        relationship.from_endpoint.column,
                        relationship.to_endpoint.column,
                    )
                ],
            )
            if profile.matched_keys < 1:
                raise ValueError(
                    f"relationship {relationship.relationship_id} "
                    "has no matched non-null keys"
                )
            observed = (
                f"{profile.left_cardinality}_to_{profile.right_cardinality}"
            )
            if observed != relationship.expected_cardinality:
                raise ValueError(
                    f"relationship {relationship.relationship_id} expected "
                    f"cardinality {relationship.expected_cardinality} but "
                    f"observed {observed}"
                )

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
        relationship_evidence=[
            RelationshipEvidence(
                left_column=evidence.right_column,
                right_column=evidence.left_column,
                left_join_key=evidence.right_join_key,
                right_join_key=evidence.left_join_key,
                source=evidence.source,
                relationship_id=evidence.relationship_id,
                expected_cardinality=(
                    reverse_expected_cardinality(evidence.expected_cardinality)
                    if evidence.expected_cardinality is not None
                    else None
                ),
                note=evidence.note,
                direction=(
                    "reverse"
                    if evidence.direction == "forward"
                    else "forward"
                    if evidence.direction == "reverse"
                    else None
                ),
            )
            for evidence in profile.relationship_evidence
        ],
    )


def build_relationship_inventory(
    duckdb_path: Path,
    *,
    relationship_spec: CatalogRelationshipSpec,
) -> RelationshipInventory:
    path = Path(duckdb_path)
    tables: list[TableRelationshipInventory] = []
    declared_relationship_keys = relationship_spec.table_keys
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
        missing_tables = sorted(set(declared_relationship_keys) - set(table_names))
        if missing_tables:
            raise ValueError(
                "Catalog relationship declaration references missing DuckDB "
                f"table(s): {missing_tables}"
            )
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
            table_relationship_keys = declared_relationship_keys.get(table_name, {})
            missing_columns = sorted(
                {
                    column
                    for column in table_relationship_keys.values()
                    if column not in columns
                }
            )
            if missing_columns:
                raise ValueError(
                    "Catalog relationship declaration references missing DuckDB "
                    f"column(s) for {table_name}: {missing_columns}"
                )
            identifier_columns = sorted(set(table_relationship_keys.values()))
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
                    columns=columns,
                    relationship_keys=table_relationship_keys,
                    identifier_columns=identifier_columns,
                    identifiers=identifiers,
                )
            )
    return RelationshipInventory(path, tables, relationship_spec)


def profile_relationship(
    duckdb_path: Path,
    left_table: str,
    right_table: str,
    key_pairs: list[tuple[str, str]],
    *,
    relationship_spec: CatalogRelationshipSpec,
) -> RelationshipProfile:
    return build_relationship_inventory(
        duckdb_path,
        relationship_spec=relationship_spec,
    ).profile_relationship(
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
    relationship_spec: CatalogRelationshipSpec,
) -> list[JoinPath]:
    return build_relationship_inventory(
        duckdb_path,
        relationship_spec=relationship_spec,
    ).find_join_paths(
        left_table,
        right_table,
        max_hops=max_hops,
        max_paths=max_paths,
    )
