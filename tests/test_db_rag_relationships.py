from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from db_rag.relationships import (
    build_relationship_inventory,
    catalog_relationship_keys,
)


RELATIONSHIP_KEYS = {
    "screening": {
        "report_family": "FID_PSEUDO",
        "report_participant": "SUBJID_PSEUDO",
    },
    "visits": {
        "report_participant": "SUBJID_PSEUDO",
        "visit": "VISIT_KEY",
    },
    "labs": {"visit": "VISIT_KEY"},
    "numeric_ids": {"report_participant": "SUBJID_PSEUDO"},
}


def _build_relationship_db(path: Path) -> None:
    connection = duckdb.connect(str(path))
    connection.execute(
        '''
        CREATE TABLE "screening" (
            "SUBJID_PSEUDO" VARCHAR,
            "FID_PSEUDO" VARCHAR,
            "FID_PRESENT" INTEGER,
            "AGE" INTEGER,
            "UNDECLARED_ID" VARCHAR
        )
        '''
    )
    connection.execute(
        '''
        INSERT INTO "screening" VALUES
            ('S1', 'F1', 1, 20, 'U1'),
            ('S2', 'F1', 1, 30, 'U2'),
            ('S3', NULL, 0, 40, 'U3')
        '''
    )
    connection.execute(
        '''
        CREATE TABLE "visits" (
            "SUBJID_PSEUDO" VARCHAR,
            "VISIT_KEY" VARCHAR,
            "UNDECLARED_ID" VARCHAR
        )
        '''
    )
    connection.execute(
        '''
        INSERT INTO "visits" VALUES
            ('S1', 'V1', 'U1'),
            ('S1', 'V2', 'U2'),
            ('S2', 'V3', 'U3'),
            ('S4', 'V4', 'U4')
        '''
    )
    connection.execute(
        '''
        CREATE TABLE "labs" (
            "VISIT_KEY" VARCHAR,
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
    connection.execute(
        '''
        CREATE TABLE "numeric_ids" (
            "SUBJID_PSEUDO" INTEGER
        )
        '''
    )
    connection.execute('INSERT INTO "numeric_ids" VALUES (1), (2)')
    connection.close()


def test_catalog_relationship_keys_extracts_current_package_declarations() -> None:
    catalog = {
        "tables": [
            {
                "table": "DEMO_J",
                "has_seqn_join": True,
                "seqn_col": "SEQN",
            },
            {
                "table": "Enrollment Cohort A",
                "has_subjid_join": True,
                "subjid_col": "SUBJID",
                "has_fid_join": True,
                "fid_col": "FID",
            },
            {
                "table": "standalone",
                "has_subjid_join": False,
                "subjid_col": "SUBJID",
            },
        ],
        "columns": [
            {"table": "DEMO_J", "column": "SEQN"},
            {"table": "Enrollment Cohort A", "column": "SUBJID"},
            {"table": "Enrollment Cohort A", "column": "FID"},
            {"table": "standalone", "column": "SUBJID"},
        ],
    }

    assert catalog_relationship_keys(catalog) == {
        "DEMO_J": {"nhanes_respondent": "SEQN"},
        "Enrollment Cohort A": {
            "report_family": "FID",
            "report_participant": "SUBJID",
        },
    }


@pytest.mark.parametrize(
    "table_entry",
    [
        {"table": "DEMO_J", "has_seqn_join": True, "seqn_col": ""},
        {"table": "", "has_seqn_join": True, "seqn_col": "SEQN"},
    ],
)
def test_catalog_relationship_keys_rejects_incomplete_enabled_declarations(
    table_entry: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="relationship declaration"):
        catalog_relationship_keys(
            {
                "tables": [table_entry],
                "columns": [{"table": "DEMO_J", "column": "SEQN"}],
            }
        )


def test_catalog_relationship_keys_rejects_columns_missing_from_catalog() -> None:
    with pytest.raises(ValueError, match="catalog column"):
        catalog_relationship_keys(
            {
                "tables": [
                    {
                        "table": "DEMO_J",
                        "has_seqn_join": True,
                        "seqn_col": "SEQN",
                    }
                ],
                "columns": [{"table": "DEMO_J", "column": "RIDAGEYR"}],
            }
        )


def test_inventory_profiles_only_catalog_declared_columns(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)

    inventory = build_relationship_inventory(
        duckdb_path,
        relationship_keys=RELATIONSHIP_KEYS,
    )
    screening = inventory.require_table("screening")

    assert screening.row_count == 3
    assert screening.columns == [
        "SUBJID_PSEUDO",
        "FID_PSEUDO",
        "FID_PRESENT",
        "AGE",
        "UNDECLARED_ID",
    ]
    assert screening.identifier_columns == ["FID_PSEUDO", "SUBJID_PSEUDO"]
    assert screening.identifiers["SUBJID_PSEUDO"].distinct_count == 3
    assert screening.identifiers["SUBJID_PSEUDO"].null_rate == 0.0
    assert screening.identifiers["FID_PSEUDO"].distinct_count == 1
    assert screening.identifiers["FID_PSEUDO"].null_rate == 1 / 3
    assert "UNDECLARED_ID" not in screening.identifiers


def test_profile_relationship_measures_cardinality_and_row_multiplication(
    tmp_path: Path,
) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    inventory = build_relationship_inventory(
        duckdb_path,
        relationship_keys=RELATIONSHIP_KEYS,
    )

    profile = inventory.profile_relationship(
        "screening",
        "visits",
        [("SUBJID_PSEUDO", "SUBJID_PSEUDO")],
    )

    assert profile.left_distinct_keys == 3
    assert profile.right_distinct_keys == 3
    assert profile.matched_keys == 2
    assert profile.joined_rows == 3
    assert profile.left_cardinality == "one"
    assert profile.right_cardinality == "many"
    assert "row_multiplication" in profile.warnings


def test_find_join_paths_returns_direct_and_multi_hop_profiles(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    inventory = build_relationship_inventory(
        duckdb_path,
        relationship_keys=RELATIONSHIP_KEYS,
    )

    direct = inventory.find_join_paths("screening", "visits")
    multi_hop = inventory.find_join_paths("screening", "labs")

    assert direct[0].tables == ["screening", "visits"]
    assert direct[0].profiles[0].key_pairs == [
        ("SUBJID_PSEUDO", "SUBJID_PSEUDO")
    ]
    assert multi_hop[0].tables == ["screening", "visits", "labs"]
    assert [profile.key_pairs for profile in multi_hop[0].profiles] == [
        [("SUBJID_PSEUDO", "SUBJID_PSEUDO")],
        [("VISIT_KEY", "VISIT_KEY")],
    ]


def test_candidate_inventory_handles_identifier_type_mismatches(
    tmp_path: Path,
) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    inventory = build_relationship_inventory(
        duckdb_path,
        relationship_keys=RELATIONSHIP_KEYS,
    )

    profiles = inventory.candidate_relationships()

    assert all(
        {
            profile.left_table,
            profile.right_table,
        }
        != {"screening", "numeric_ids"}
        for profile in profiles
    )


def test_explicit_profile_rejects_existing_undeclared_columns(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    inventory = build_relationship_inventory(
        duckdb_path,
        relationship_keys=RELATIONSHIP_KEYS,
    )

    with pytest.raises(KeyError, match="catalog-declared"):
        inventory.profile_relationship(
            "screening",
            "visits",
            [("UNDECLARED_ID", "UNDECLARED_ID")],
        )


def test_explicit_profile_rejects_incompatible_relationship_domains(
    tmp_path: Path,
) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)
    inventory = build_relationship_inventory(
        duckdb_path,
        relationship_keys=RELATIONSHIP_KEYS,
    )

    with pytest.raises(KeyError, match="compatible catalog-declared"):
        inventory.profile_relationship(
            "screening",
            "visits",
            [("FID_PSEUDO", "SUBJID_PSEUDO")],
        )


def test_inventory_rejects_declared_columns_missing_from_duckdb(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)

    with pytest.raises(ValueError, match="missing DuckDB column"):
        build_relationship_inventory(
            duckdb_path,
            relationship_keys={"screening": {"participant": "SEQN"}},
        )
