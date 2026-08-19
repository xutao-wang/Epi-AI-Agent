from __future__ import annotations

import pytest

from db_rag.relationships import catalog_relationship_keys


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
