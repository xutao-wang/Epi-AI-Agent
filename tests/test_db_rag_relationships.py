from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from db_rag.catalog_relationships import parse_catalog_relationships
from db_rag.relationships import build_relationship_inventory


def _catalog(*, expected_cardinality: str = "many_to_one") -> dict[str, Any]:
    join_keys = {
        "person_token": "PERSON_TOKEN",
        "guardian_token": "GUARDIAN_TOKEN",
        "visit_token": "VISIT_TOKEN",
    }
    table_keys = {
        "screening": {"person_token"},
        "visits": {"person_token", "visit_token"},
        "labs": {"visit_token"},
        "numeric_ids": {"person_token"},
        "children": {"guardian_token"},
        "guardians": {"person_token"},
    }
    tables = []
    for table, enabled_keys in table_keys.items():
        entry: dict[str, Any] = {"table": table}
        for key_id in join_keys:
            entry[f"has_{key_id}_join"] = key_id in enabled_keys
        tables.append(entry)
    return {
        "catalog_version": 2,
        "join_keys": join_keys,
        "relationships": [
            {
                "id": "guardian_link",
                "from": {
                    "table": "children",
                    "join_key": "guardian_token",
                },
                "to": {
                    "table": "guardians",
                    "join_key": "person_token",
                },
                "expected_cardinality": expected_cardinality,
                "note": "Each child references one guardian.",
            }
        ],
        "tables": tables,
        "columns": [
            {"table": "screening", "column": "PERSON_TOKEN"},
            {"table": "visits", "column": "PERSON_TOKEN"},
            {"table": "visits", "column": "VISIT_TOKEN"},
            {"table": "labs", "column": "VISIT_TOKEN"},
            {"table": "numeric_ids", "column": "PERSON_TOKEN"},
            {"table": "children", "column": "GUARDIAN_TOKEN"},
            {"table": "guardians", "column": "PERSON_TOKEN"},
        ],
    }


def _relationship_spec(*, expected_cardinality: str = "many_to_one"):
    return parse_catalog_relationships(
        _catalog(expected_cardinality=expected_cardinality)
    )


def _build_relationship_db(path: Path) -> None:
    connection = duckdb.connect(str(path))
    connection.execute(
        '''
        CREATE TABLE "screening" (
            "PERSON_TOKEN" VARCHAR,
            "FAMILY_TOKEN" VARCHAR,
            "FAMILY_PRESENT" INTEGER,
            "AGE" INTEGER,
            "UNDECLARED_ID" VARCHAR
        )
        '''
    )
    connection.execute(
        '''
        INSERT INTO "screening" VALUES
            ('P1', 'F1', 1, 20, 'U1'),
            ('P2', 'F1', 1, 30, 'U2'),
            ('P3', NULL, 0, 40, 'U3')
        '''
    )
    connection.execute(
        '''
        CREATE TABLE "visits" (
            "PERSON_TOKEN" VARCHAR,
            "VISIT_TOKEN" VARCHAR,
            "UNDECLARED_ID" VARCHAR
        )
        '''
    )
    connection.execute(
        '''
        INSERT INTO "visits" VALUES
            ('P1', 'V1', 'U1'),
            ('P1', 'V2', 'U2'),
            ('P2', 'V3', 'U3'),
            ('P4', 'V4', 'U4')
        '''
    )
    connection.execute(
        '''
        CREATE TABLE "labs" (
            "VISIT_TOKEN" VARCHAR,
            "RESULT" INTEGER
        )
        '''
    )
    connection.execute(
        '''
        INSERT INTO "labs" VALUES
            ('V1', 1),
            ('V2', 2),
            ('V3', 3)
        '''
    )
    connection.execute('CREATE TABLE "numeric_ids" ("PERSON_TOKEN" INTEGER)')
    connection.execute('INSERT INTO "numeric_ids" VALUES (1), (2)')
    connection.execute(
        'CREATE TABLE "children" ("CHILD_TOKEN" VARCHAR, "GUARDIAN_TOKEN" VARCHAR)'
    )
    connection.execute(
        "INSERT INTO \"children\" VALUES ('C1', 'G1'), ('C2', 'G1'), ('C3', 'G2')"
    )
    connection.execute(
        'CREATE TABLE "guardians" ("PERSON_TOKEN" VARCHAR, "NAME" VARCHAR)'
    )
    connection.execute(
        "INSERT INTO \"guardians\" VALUES ('G1', 'A'), ('G2', 'B')"
    )
    connection.close()


def _inventory(path: Path):
    return build_relationship_inventory(
        path,
        relationship_spec=_relationship_spec(),
    )


def test_inventory_profiles_only_catalog_declared_columns(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)

    inventory = _inventory(duckdb_path)
    screening = inventory.require_table("screening")

    assert screening.row_count == 3
    assert screening.columns == [
        "PERSON_TOKEN",
        "FAMILY_TOKEN",
        "FAMILY_PRESENT",
        "AGE",
        "UNDECLARED_ID",
    ]
    assert screening.identifier_columns == ["PERSON_TOKEN"]
    assert screening.identifiers["PERSON_TOKEN"].distinct_count == 3
    assert screening.identifiers["PERSON_TOKEN"].null_rate == 0.0
    assert "UNDECLARED_ID" not in screening.identifiers


def test_profile_shared_key_includes_generic_relationship_evidence(
    tmp_path: Path,
) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    inventory = _inventory(duckdb_path)

    profile = inventory.profile_relationship(
        "screening",
        "visits",
        [("PERSON_TOKEN", "PERSON_TOKEN")],
    )

    assert profile.left_distinct_keys == 3
    assert profile.right_distinct_keys == 3
    assert profile.matched_keys == 2
    assert profile.joined_rows == 3
    assert profile.left_cardinality == "one"
    assert profile.right_cardinality == "many"
    assert "row_multiplication" in profile.warnings
    evidence = profile.relationship_evidence[0]
    assert evidence.source == "shared_join_key"
    assert evidence.left_join_key == "person_token"
    assert evidence.right_join_key == "person_token"


def test_profile_explicit_cross_key_includes_declared_evidence(
    tmp_path: Path,
) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    inventory = _inventory(duckdb_path)

    profile = inventory.profile_relationship(
        "children",
        "guardians",
        [("GUARDIAN_TOKEN", "PERSON_TOKEN")],
    )

    assert profile.matched_keys == 2
    assert profile.left_cardinality == "many"
    assert profile.right_cardinality == "one"
    evidence = profile.relationship_evidence[0]
    assert evidence.relationship_id == "guardian_link"
    assert evidence.expected_cardinality == "many_to_one"
    assert evidence.note == "Each child references one guardian."
    assert evidence.direction == "forward"


def test_reverse_profile_reverses_declared_evidence(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    inventory = _inventory(duckdb_path)

    profile = inventory.profile_relationship(
        "guardians",
        "children",
        [("PERSON_TOKEN", "GUARDIAN_TOKEN")],
    )

    evidence = profile.relationship_evidence[0]
    assert evidence.left_join_key == "person_token"
    assert evidence.right_join_key == "guardian_token"
    assert evidence.direction == "reverse"
    assert evidence.expected_cardinality == "one_to_many"

    path_profile = inventory.find_join_paths("guardians", "children")[0].profiles[0]
    path_evidence = path_profile.relationship_evidence[0]
    assert path_evidence.left_column == "PERSON_TOKEN"
    assert path_evidence.right_column == "GUARDIAN_TOKEN"
    assert path_evidence.direction == "reverse"
    assert path_evidence.expected_cardinality == "one_to_many"


def test_find_join_paths_returns_direct_and_multi_hop_profiles(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    inventory = _inventory(duckdb_path)

    direct = inventory.find_join_paths("screening", "visits")
    multi_hop = inventory.find_join_paths("screening", "labs")

    assert direct[0].tables == ["screening", "visits"]
    assert direct[0].profiles[0].key_pairs == [
        ("PERSON_TOKEN", "PERSON_TOKEN")
    ]
    assert multi_hop[0].tables == ["screening", "visits", "labs"]
    assert [profile.key_pairs for profile in multi_hop[0].profiles] == [
        [("PERSON_TOKEN", "PERSON_TOKEN")],
        [("VISIT_TOKEN", "VISIT_TOKEN")],
    ]


def test_candidates_put_explicit_relationships_before_shared_keys(
    tmp_path: Path,
) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)

    profiles = _inventory(duckdb_path).candidate_relationships()

    assert profiles[0].relationship_evidence[0].source == "declared_relationship"
    assert profiles[0].relationship_evidence[0].relationship_id == "guardian_link"


def test_candidate_inventory_handles_identifier_type_mismatches(
    tmp_path: Path,
) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)

    profiles = _inventory(duckdb_path).candidate_relationships()

    assert all(
        {profile.left_table, profile.right_table}
        != {"screening", "numeric_ids"}
        for profile in profiles
    )


def test_explicit_profile_rejects_existing_undeclared_columns(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    inventory = _inventory(duckdb_path)

    with pytest.raises(KeyError, match="not authorized by the study catalog"):
        inventory.profile_relationship(
            "screening",
            "visits",
            [("UNDECLARED_ID", "UNDECLARED_ID")],
        )


def test_explicit_profile_rejects_undeclared_cross_key(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    inventory = _inventory(duckdb_path)

    with pytest.raises(KeyError, match="not authorized by the study catalog"):
        inventory.profile_relationship(
            "children",
            "visits",
            [("GUARDIAN_TOKEN", "PERSON_TOKEN")],
        )


def test_profile_relationship_rejects_unknown_table(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)

    with pytest.raises(KeyError, match="Unknown runtime table"):
        _inventory(duckdb_path).profile_relationship(
            "missing_table",
            "visits",
            [("PERSON_TOKEN", "PERSON_TOKEN")],
        )


def test_inventory_rejects_declared_columns_missing_from_duckdb(
    tmp_path: Path,
) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    specification = _relationship_spec().model_copy(
        update={
            "table_keys": {
                **_relationship_spec().table_keys,
                "screening": {"person_token": "MISSING_TOKEN"},
            }
        }
    )

    with pytest.raises(ValueError, match="missing DuckDB column"):
        build_relationship_inventory(
            duckdb_path,
            relationship_spec=specification,
        )


def test_inventory_rejects_declared_tables_missing_from_duckdb(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    specification = _relationship_spec().model_copy(
        update={
            "table_keys": {
                **_relationship_spec().table_keys,
                "missing_table": {"person_token": "PERSON_TOKEN"},
            }
        }
    )

    with pytest.raises(ValueError, match="missing DuckDB table"):
        build_relationship_inventory(
            duckdb_path,
            relationship_spec=specification,
        )


def test_validate_declared_relationships_accepts_matching_data(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)

    _inventory(duckdb_path).validate_declared_relationships()


def test_validate_declared_relationships_rejects_no_overlap(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    with duckdb.connect(str(duckdb_path)) as connection:
        connection.execute(
            "UPDATE \"guardians\" SET \"PERSON_TOKEN\" = 'X' || \"PERSON_TOKEN\""
        )

    with pytest.raises(ValueError, match="has no matched non-null keys"):
        _inventory(duckdb_path).validate_declared_relationships()


def test_validate_declared_relationships_rejects_observed_cardinality(
    tmp_path: Path,
) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    inventory = build_relationship_inventory(
        duckdb_path,
        relationship_spec=_relationship_spec(expected_cardinality="one_to_one"),
    )

    with pytest.raises(
        ValueError,
        match="expected cardinality one_to_one but observed many_to_one",
    ):
        inventory.validate_declared_relationships()
