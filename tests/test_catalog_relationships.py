from __future__ import annotations

import copy
from typing import Any

import pytest

from db_rag.catalog_relationships import parse_catalog_relationships


def _catalog() -> dict[str, Any]:
    return {
        "catalog_version": 2,
        "join_keys": {
            "person_token": "PERSON_TOKEN",
            "guardian_token": "GUARDIAN_TOKEN",
        },
        "relationships": [
            {
                "id": "guardian_link",
                "from": {
                    "table": "children",
                    "join_key": "guardian_token",
                },
                "to": {
                    "table": "adults",
                    "join_key": "person_token",
                },
                "expected_cardinality": "many_to_one",
                "note": "Each child references one guardian.",
            }
        ],
        "tables": [
            {
                "table": "adults",
                "has_person_token_join": True,
                "has_guardian_token_join": False,
            },
            {
                "table": "children",
                "has_person_token_join": True,
                "has_guardian_token_join": True,
            },
        ],
        "columns": [
            {"table": "adults", "column": "PERSON_TOKEN"},
            {"table": "children", "column": "PERSON_TOKEN"},
            {"table": "children", "column": "GUARDIAN_TOKEN"},
        ],
    }


def test_parses_dynamic_keys_and_explicit_relationship() -> None:
    specification = parse_catalog_relationships(_catalog())

    assert specification.table_keys == {
        "adults": {"person_token": "PERSON_TOKEN"},
        "children": {
            "guardian_token": "GUARDIAN_TOKEN",
            "person_token": "PERSON_TOKEN",
        },
    }
    relationship = specification.relationships[0]
    assert relationship.relationship_id == "guardian_link"
    assert relationship.from_endpoint.column == "GUARDIAN_TOKEN"
    assert relationship.to_endpoint.column == "PERSON_TOKEN"


def test_authorizes_shared_key_and_explicit_cross_key() -> None:
    specification = parse_catalog_relationships(_catalog())

    shared = specification.authorize_pair(
        "adults",
        "PERSON_TOKEN",
        "children",
        "PERSON_TOKEN",
    )
    explicit = specification.authorize_pair(
        "children",
        "GUARDIAN_TOKEN",
        "adults",
        "PERSON_TOKEN",
    )

    assert shared is not None
    assert shared.source == "shared_join_key"
    assert shared.left_endpoint.join_key == "person_token"
    assert explicit is not None
    assert explicit.source == "declared_relationship"
    assert explicit.relationship_id == "guardian_link"
    assert explicit.expected_cardinality == "many_to_one"
    assert explicit.direction == "forward"


def test_reverse_authorization_reverses_cardinality() -> None:
    authorization = parse_catalog_relationships(_catalog()).authorize_pair(
        "adults",
        "PERSON_TOKEN",
        "children",
        "GUARDIAN_TOKEN",
    )

    assert authorization is not None
    assert authorization.direction == "reverse"
    assert authorization.expected_cardinality == "one_to_many"
    assert authorization.left_endpoint.table == "adults"
    assert authorization.right_endpoint.table == "children"


def test_reused_relationship_id_is_allowed_for_distinct_edges() -> None:
    catalog = _catalog()
    catalog["tables"].append(
        {
            "table": "children_follow_up",
            "has_person_token_join": True,
            "has_guardian_token_join": True,
        }
    )
    catalog["columns"].extend(
        [
            {"table": "children_follow_up", "column": "PERSON_TOKEN"},
            {"table": "children_follow_up", "column": "GUARDIAN_TOKEN"},
        ]
    )
    second = copy.deepcopy(catalog["relationships"][0])
    second["from"]["table"] = "children_follow_up"
    catalog["relationships"].append(second)

    assert len(parse_catalog_relationships(catalog).relationships) == 2


def test_consistent_reverse_relationship_is_normalized() -> None:
    catalog = _catalog()
    reverse = copy.deepcopy(catalog["relationships"][0])
    reverse["from"], reverse["to"] = reverse["to"], reverse["from"]
    reverse["expected_cardinality"] = "one_to_many"
    catalog["relationships"].append(reverse)

    assert len(parse_catalog_relationships(catalog).relationships) == 1


def test_undeclared_cross_key_is_not_authorized() -> None:
    specification = parse_catalog_relationships(_catalog())

    assert specification.authorize_pair(
        "adults",
        "PERSON_TOKEN",
        "children",
        "GUARDIAN_TOKEN",
    ) is not None
    assert specification.authorize_pair(
        "adults",
        "PERSON_TOKEN",
        "children",
        "PERSON_TOKEN_MISSING",
    ) is None


def test_rejects_catalog_v1() -> None:
    catalog = _catalog()
    catalog["catalog_version"] = 1

    with pytest.raises(ValueError, match="catalog_version 2"):
        parse_catalog_relationships(catalog)


def test_rejects_unsafe_join_key_id() -> None:
    catalog = _catalog()
    catalog["join_keys"] = {"Person Token": "PERSON_TOKEN"}

    with pytest.raises(ValueError, match="invalid key"):
        parse_catalog_relationships(catalog)


def test_rejects_missing_relationship_flag() -> None:
    catalog = _catalog()
    del catalog["tables"][0]["has_guardian_token_join"]

    with pytest.raises(ValueError, match="has_guardian_token_join"):
        parse_catalog_relationships(catalog)


def test_rejects_non_boolean_relationship_flag() -> None:
    catalog = _catalog()
    catalog["tables"][0]["has_guardian_token_join"] = "false"

    with pytest.raises(ValueError, match="boolean"):
        parse_catalog_relationships(catalog)


def test_rejects_true_flag_without_catalog_column() -> None:
    catalog = _catalog()
    catalog["tables"][0]["has_guardian_token_join"] = True

    with pytest.raises(ValueError, match="catalog column"):
        parse_catalog_relationships(catalog)


@pytest.mark.parametrize("field", ["from", "to"])
def test_rejects_unknown_relationship_table(field: str) -> None:
    catalog = _catalog()
    catalog["relationships"][0][field]["table"] = "missing"

    with pytest.raises(ValueError, match="unknown table"):
        parse_catalog_relationships(catalog)


def test_rejects_unknown_relationship_key() -> None:
    catalog = _catalog()
    catalog["relationships"][0]["from"]["join_key"] = "missing"

    with pytest.raises(ValueError, match="unknown join key"):
        parse_catalog_relationships(catalog)


def test_rejects_unauthorized_relationship_endpoint() -> None:
    catalog = _catalog()
    catalog["tables"][1]["has_guardian_token_join"] = False

    with pytest.raises(ValueError, match="not authorized"):
        parse_catalog_relationships(catalog)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", "", "relationship id"),
        ("note", "", "incomplete"),
        ("expected_cardinality", "several", "cardinality"),
    ],
)
def test_rejects_incomplete_relationship(
    field: str,
    value: str,
    message: str,
) -> None:
    catalog = _catalog()
    catalog["relationships"][0][field] = value

    with pytest.raises(ValueError, match=message):
        parse_catalog_relationships(catalog)


def test_rejects_duplicate_relationship_endpoints() -> None:
    catalog = _catalog()
    catalog["relationships"].append(copy.deepcopy(catalog["relationships"][0]))

    with pytest.raises(ValueError, match="duplicated endpoints"):
        parse_catalog_relationships(catalog)


def test_rejects_contradictory_reverse_relationship() -> None:
    catalog = _catalog()
    reverse = copy.deepcopy(catalog["relationships"][0])
    reverse["id"] = "different_relationship"
    reverse["from"], reverse["to"] = reverse["to"], reverse["from"]
    reverse["expected_cardinality"] = "one_to_many"
    catalog["relationships"].append(reverse)

    with pytest.raises(ValueError, match="contradictory reverse endpoints"):
        parse_catalog_relationships(catalog)
