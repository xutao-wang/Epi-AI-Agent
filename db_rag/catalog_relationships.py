from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict


CATALOG_VERSION = 2
_SAFE_KEY_ID = re.compile(r"^[a-z][a-z0-9_]*$")

Cardinality = Literal[
    "one_to_one",
    "one_to_many",
    "many_to_one",
    "many_to_many",
]
RelationshipSource = Literal["shared_join_key", "declared_relationship"]
Direction = Literal["forward", "reverse"]

_CARDINALITIES = {
    "one_to_one",
    "one_to_many",
    "many_to_one",
    "many_to_many",
}


class JoinEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    table: str
    join_key: str
    column: str


class DeclaredRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relationship_id: str
    from_endpoint: JoinEndpoint
    to_endpoint: JoinEndpoint
    expected_cardinality: Cardinality
    note: str


class RelationshipAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    left_endpoint: JoinEndpoint
    right_endpoint: JoinEndpoint
    source: RelationshipSource
    relationship_id: str | None = None
    expected_cardinality: Cardinality | None = None
    note: str | None = None
    direction: Direction | None = None


class CatalogRelationshipSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    table_keys: dict[str, dict[str, str]]
    relationships: tuple[DeclaredRelationship, ...] = ()

    def authorize_pair(
        self,
        left_table: str,
        left_column: str,
        right_table: str,
        right_column: str,
    ) -> RelationshipAuthorization | None:
        for relationship in self.relationships:
            forward = (
                relationship.from_endpoint.table == left_table
                and relationship.from_endpoint.column == left_column
                and relationship.to_endpoint.table == right_table
                and relationship.to_endpoint.column == right_column
            )
            reverse = (
                relationship.to_endpoint.table == left_table
                and relationship.to_endpoint.column == left_column
                and relationship.from_endpoint.table == right_table
                and relationship.from_endpoint.column == right_column
            )
            if forward or reverse:
                return RelationshipAuthorization(
                    left_endpoint=(
                        relationship.from_endpoint
                        if forward
                        else relationship.to_endpoint
                    ),
                    right_endpoint=(
                        relationship.to_endpoint
                        if forward
                        else relationship.from_endpoint
                    ),
                    source="declared_relationship",
                    relationship_id=relationship.relationship_id,
                    expected_cardinality=(
                        relationship.expected_cardinality
                        if forward
                        else reverse_expected_cardinality(
                            relationship.expected_cardinality
                        )
                    ),
                    note=relationship.note,
                    direction="forward" if forward else "reverse",
                )

        left_keys = self.table_keys.get(left_table, {})
        right_keys = self.table_keys.get(right_table, {})
        for key_id in sorted(set(left_keys) & set(right_keys)):
            if (
                left_keys[key_id] == left_column
                and right_keys[key_id] == right_column
            ):
                return RelationshipAuthorization(
                    left_endpoint=JoinEndpoint(
                        table=left_table,
                        join_key=key_id,
                        column=left_column,
                    ),
                    right_endpoint=JoinEndpoint(
                        table=right_table,
                        join_key=key_id,
                        column=right_column,
                    ),
                    source="shared_join_key",
                )
        return None


def reverse_expected_cardinality(value: Cardinality) -> Cardinality:
    return {
        "one_to_one": "one_to_one",
        "one_to_many": "many_to_one",
        "many_to_one": "one_to_many",
        "many_to_many": "many_to_many",
    }[value]


def _text(value: object) -> str:
    return str(value or "").strip()


def _required_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def parse_catalog_relationships(
    catalog: Mapping[str, Any],
) -> CatalogRelationshipSpec:
    if catalog.get("catalog_version") != CATALOG_VERSION:
        raise ValueError("schema catalog must use catalog_version 2")

    raw_join_keys = _required_mapping(catalog.get("join_keys"), field="join_keys")
    if not raw_join_keys:
        raise ValueError("join_keys must not be empty")
    join_keys: dict[str, str] = {}
    for raw_key_id, raw_column in raw_join_keys.items():
        key_id = _text(raw_key_id)
        column = _text(raw_column)
        if (
            not _SAFE_KEY_ID.fullmatch(key_id)
            or not column
            or key_id in join_keys
        ):
            raise ValueError("join_keys contains an invalid key declaration")
        join_keys[key_id] = column

    raw_tables = catalog.get("tables")
    raw_columns = catalog.get("columns")
    raw_relationships = catalog.get("relationships")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise ValueError("tables must be a nonempty list")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ValueError("columns must be a nonempty list")
    if not isinstance(raw_relationships, list):
        raise ValueError("relationships must be a list")

    catalog_columns = {
        (_text(row.get("table")), _text(row.get("column")))
        for row in raw_columns
        if isinstance(row, Mapping)
        and _text(row.get("table"))
        and _text(row.get("column"))
    }
    table_entries: dict[str, Mapping[str, Any]] = {}
    table_keys: dict[str, dict[str, str]] = {}
    for raw_table in raw_tables:
        table = _required_mapping(raw_table, field="table entry")
        table_name = _text(table.get("table"))
        if not table_name or table_name in table_entries:
            raise ValueError("tables contains a blank or duplicate table")
        authorized: dict[str, str] = {}
        for key_id, column in join_keys.items():
            flag = f"has_{key_id}_join"
            enabled = table.get(flag)
            if not isinstance(enabled, bool):
                raise ValueError(f"{table_name}.{flag} must be a boolean")
            if enabled:
                if (table_name, column) not in catalog_columns:
                    raise ValueError(
                        f"{table_name}.{flag} references a missing catalog column"
                    )
                authorized[key_id] = column
        table_entries[table_name] = table
        if authorized:
            table_keys[table_name] = dict(sorted(authorized.items()))

    relationships: list[DeclaredRelationship] = []
    seen_relationships: dict[
        tuple[tuple[str, str], tuple[str, str]],
        DeclaredRelationship,
    ] = {}
    for raw_relationship in raw_relationships:
        relationship = _required_mapping(
            raw_relationship,
            field="relationship",
        )
        relationship_id = _text(relationship.get("id"))
        if not _SAFE_KEY_ID.fullmatch(relationship_id):
            raise ValueError("relationship id is invalid")
        note = _text(relationship.get("note"))
        expected = _text(relationship.get("expected_cardinality"))
        if not note:
            raise ValueError(f"relationship {relationship_id} is incomplete")
        if expected not in _CARDINALITIES:
            raise ValueError(
                f"relationship {relationship_id} has invalid cardinality"
            )

        endpoints: list[JoinEndpoint] = []
        for direction in ("from", "to"):
            endpoint = _required_mapping(
                relationship.get(direction),
                field=f"relationship {relationship_id}.{direction}",
            )
            table_name = _text(endpoint.get("table"))
            key_id = _text(endpoint.get("join_key"))
            if table_name not in table_entries:
                raise ValueError(
                    f"relationship {relationship_id} references an unknown table"
                )
            if key_id not in join_keys:
                raise ValueError(
                    f"relationship {relationship_id} references an unknown join key"
                )
            if key_id not in table_keys.get(table_name, {}):
                raise ValueError(
                    f"relationship {relationship_id} endpoint is not authorized"
                )
            endpoints.append(
                JoinEndpoint(
                    table=table_name,
                    join_key=key_id,
                    column=join_keys[key_id],
                )
            )

        declared = DeclaredRelationship(
            relationship_id=relationship_id,
            from_endpoint=endpoints[0],
            to_endpoint=endpoints[1],
            expected_cardinality=cast(Cardinality, expected),
            note=note,
        )
        directed = (
            (declared.from_endpoint.table, declared.from_endpoint.join_key),
            (declared.to_endpoint.table, declared.to_endpoint.join_key),
        )
        if directed in seen_relationships:
            raise ValueError("relationship has duplicated endpoints")
        reverse = (directed[1], directed[0])
        prior = seen_relationships.get(reverse)
        if prior is not None:
            if (
                declared.relationship_id != prior.relationship_id
                or declared.note != prior.note
                or declared.expected_cardinality
                != reverse_expected_cardinality(prior.expected_cardinality)
            ):
                raise ValueError(
                    "relationship has contradictory reverse endpoints"
                )
            continue
        seen_relationships[directed] = declared
        relationships.append(declared)

    return CatalogRelationshipSpec(
        table_keys={
            table: dict(sorted(keys.items()))
            for table, keys in sorted(table_keys.items())
        },
        relationships=tuple(relationships),
    )


__all__ = [
    "CATALOG_VERSION",
    "Cardinality",
    "CatalogRelationshipSpec",
    "DeclaredRelationship",
    "Direction",
    "JoinEndpoint",
    "RelationshipAuthorization",
    "RelationshipSource",
    "parse_catalog_relationships",
    "reverse_expected_cardinality",
]
