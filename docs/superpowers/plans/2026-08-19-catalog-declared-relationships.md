# Catalog-Declared Relationships Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove regex-inferred join keys and make explicit relationship profiling plus automatic relationship discovery use only join keys declared by the selected study package catalog.

**Architecture:** Parse the current catalog-v1 table metadata into a generic `table -> relationship domain -> physical column` mapping. Pass that mapping with the matching DuckDB path through package validation, readiness, and study-bundle construction; the relationship inventory validates the declarations against DuckDB and uses shared domains for strict profiling and candidate discovery.

**Tech Stack:** Python 3.12, Pydantic, DuckDB, pytest, immutable study-package archives.

## Global Constraints

- Remove the identifier regular expression; do not replace it with another naming heuristic.
- Catalog declarations are the exclusive authority for explicit profiling and automatic discovery.
- RePORT India uses the packaged `SUBJID`/`FID` declarations; NHANES uses the packaged `SEQN` declarations.
- Tables without declared join metadata remain valid and standalone.
- Do not modify or republish the bundled study-package archives.
- Use `.venv/bin/python`; never use the system `python3`.
- The dedicated smoke must exercise real production package installation and study-bundle boundaries without mocks and run once within five minutes.

---

### Task 1: Parse and validate catalog-declared relationship keys

**Files:**
- Modify: `db_rag/relationships.py`
- Test: `tests/test_db_rag_relationships.py`

**Interfaces:**
- Consumes: catalog-v1 dictionaries with `tables` and `columns` arrays.
- Produces: `catalog_relationship_keys(catalog: Mapping[str, Any]) -> dict[str, dict[str, str]]`, where the inner mapping is relationship domain to physical column.

- [ ] **Step 1: Write failing parser tests**

Add imports and focused tests that define the current package contract without relying on the regex:

```python
import pytest

from db_rag.relationships import (
    build_relationship_inventory,
    catalog_relationship_keys,
)


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
```

- [ ] **Step 2: Run the parser tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_db_rag_relationships.py -k catalog_relationship_keys -vv
```

Expected: collection fails because `catalog_relationship_keys` does not exist.

- [ ] **Step 3: Implement the minimal catalog parser and remove regex helpers**

Delete `import re`, `_IDENTIFIER_COLUMN`, and `_is_identifier_column`. Add:

```python
from collections.abc import Mapping
from typing import Any, Literal


_RELATIONSHIP_FIELDS = {
    "nhanes_respondent": ("has_seqn_join", "seqn_col"),
    "report_participant": ("has_subjid_join", "subjid_col"),
    "report_family": ("has_fid_join", "fid_col"),
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def catalog_relationship_keys(
    catalog: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
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
                    f"Declared relationship key is not a catalog column: "
                    f"{table}.{column}"
                )
            table_keys = declared.setdefault(table, {})
            existing = table_keys.get(domain)
            if existing is not None and existing != column:
                raise ValueError(
                    f"Conflicting catalog relationship declaration: "
                    f"{table}.{domain}"
                )
            table_keys[domain] = column
    return {
        table: dict(sorted(keys.items()))
        for table, keys in sorted(declared.items())
    }
```

- [ ] **Step 4: Run the parser tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_db_rag_relationships.py -k catalog_relationship_keys -vv
```

Expected: all parser tests pass.

- [ ] **Step 5: Commit the parser contract**

```bash
git add db_rag/relationships.py tests/test_db_rag_relationships.py
git commit -m "feat: parse catalog relationship keys"
```

### Task 2: Restrict profiling and discovery to compatible declared keys

**Files:**
- Modify: `db_rag/relationships.py`
- Modify: `tests/test_db_rag_relationships.py`

**Interfaces:**
- Consumes: `relationship_keys: Mapping[str, Mapping[str, str]]` from Task 1.
- Produces: strict `RelationshipInventory.profile_relationship(...)`, catalog-driven `candidate_relationships()`, and `find_join_paths(...)` behavior.

- [ ] **Step 1: Convert the relationship database fixture to explicit declarations**

Apply these exact fixture changes to `_build_relationship_db`:

```python
CREATE TABLE "screening" (
    "SUBJID_PSEUDO" VARCHAR,
    "FID_PSEUDO" VARCHAR,
    "FID_PRESENT" INTEGER,
    "AGE" INTEGER,
    "UNDECLARED_ID" VARCHAR
)

INSERT INTO "screening" VALUES
    ('S1', 'F1', 1, 20, 'U1'),
    ('S2', 'F1', 1, 30, 'U2'),
    ('S3', NULL, 0, 40, 'U3')

CREATE TABLE "visits" (
    "SUBJID_PSEUDO" VARCHAR,
    "VISIT_KEY" VARCHAR,
    "UNDECLARED_ID" VARCHAR
)

INSERT INTO "visits" VALUES
    ('S1', 'V1', 'U1'),
    ('S1', 'V2', 'U2'),
    ('S2', 'V3', 'U3'),
    ('S4', 'V4', 'U4')

CREATE TABLE "labs" (
    "VISIT_KEY" VARCHAR,
    "RESULT" INTEGER
)
```

Use the corresponding existing lab values with `VISIT_KEY`, then declare:

```python
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
```

Pass `relationship_keys=RELATIONSHIP_KEYS` to every inventory build in this
test file.

- [ ] **Step 2: Write failing strictness and automatic-discovery tests**

```python
def test_inventory_profiles_only_catalog_declared_columns(tmp_path: Path) -> None:
    duckdb_path = tmp_path / "study.duckdb"
    _build_relationship_db(duckdb_path)

    inventory = build_relationship_inventory(
        duckdb_path,
        relationship_keys=RELATIONSHIP_KEYS,
    )
    screening = inventory.require_table("screening")

    assert screening.columns == [
        "SUBJID_PSEUDO",
        "FID_PSEUDO",
        "FID_PRESENT",
        "AGE",
        "UNDECLARED_ID",
    ]
    assert screening.identifier_columns == ["FID_PSEUDO", "SUBJID_PSEUDO"]
    assert "UNDECLARED_ID" not in screening.identifiers


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
```

Keep the existing path assertion, but require the renamed declared key:

```python
assert [profile.key_pairs for profile in multi_hop[0].profiles] == [
    [("SUBJID_PSEUDO", "SUBJID_PSEUDO")],
    [("VISIT_KEY", "VISIT_KEY")],
]
```

- [ ] **Step 3: Run the relationship test file and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_db_rag_relationships.py -vv
```

Expected: failures show that the inventory has no `columns`/relationship-domain metadata, still builds keys without declarations, or accepts incompatible explicit pairs.

- [ ] **Step 4: Implement strict inventory construction**

Extend the table model and builder signature:

```python
class TableRelationshipInventory(BaseModel):
    table: str
    row_count: int
    columns: list[str]
    relationship_keys: dict[str, str]
    identifier_columns: list[str]
    identifiers: dict[str, IdentifierProfile]


def build_relationship_inventory(
    duckdb_path: Path,
    *,
    relationship_keys: Mapping[str, Mapping[str, str]] | None = None,
) -> RelationshipInventory:
```

Normalize declarations before opening DuckDB. After reading all table names,
reject declared tables absent from DuckDB. For each table, reject every declared
column absent from `information_schema.columns`. Set `identifier_columns` to
the sorted unique declared physical columns, profile only those columns, and
store both `columns` and the domain mapping on the table model.

- [ ] **Step 5: Implement strict explicit profiling and domain-based candidates**

Before SQL execution, validate each explicit pair:

```python
def _domains_for_column(
    table: TableRelationshipInventory,
    column: str,
) -> set[str]:
    return {
        domain
        for domain, declared_column in table.relationship_keys.items()
        if declared_column == column
    }


undeclared_or_incompatible = [
    (left_column, right_column)
    for left_column, right_column in key_pairs
    if not (
        _domains_for_column(left, left_column)
        & _domains_for_column(right, right_column)
    )
]
if undeclared_or_incompatible:
    raise KeyError(
        "Relationship keys must be compatible catalog-declared columns: "
        f"{undeclared_or_incompatible}"
    )
```

Retain the existing profiling SQL and null-warning calculation through the
declared `identifiers`. Replace common physical-column discovery with common
domains:

```python
shared_domains = sorted(
    set(left.relationship_keys) & set(right.relationship_keys)
)
seen_pairs: set[tuple[str, str]] = set()
for domain in shared_domains:
    pair = (
        left.relationship_keys[domain],
        right.relationship_keys[domain],
    )
    if pair in seen_pairs:
        continue
    seen_pairs.add(pair)
    profile = self.profile_relationship(left.table, right.table, [pair])
    if profile.matched_keys:
        candidates.append(profile)
```

- [ ] **Step 6: Run the focused relationship suite and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_db_rag_relationships.py -vv
```

Expected: all relationship tests pass, including existing cardinality and warning assertions.

- [ ] **Step 7: Commit strict inventory behavior**

```bash
git add db_rag/relationships.py tests/test_db_rag_relationships.py
git commit -m "fix: restrict relationships to catalog keys"
```

### Task 3: Wire the matching catalog through installation, readiness, and study bundles

**Files:**
- Modify: `db_rag/study.py`
- Modify: `db_rag/readiness.py`
- Modify: `study_package/installer.py`
- Modify: `tests/test_report_study_bundle.py`
- Modify: `tests/test_installed_study_bundle.py`
- Modify: `tests/test_db_rag_relationship_readiness.py`
- Modify: `tests/test_study_package_installer.py`

**Interfaces:**
- Consumes: `catalog_relationship_keys` and `build_relationship_inventory(..., relationship_keys=...)` from Tasks 1–2.
- Produces: package validation and runtime sources that use relationship metadata from the same catalog as their DuckDB database.

- [ ] **Step 1: Write failing study-source wiring tests**

Update the cache test to capture both arguments:

```python
def test_duckdb_study_source_caches_relationship_inventory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    inventory = object()
    builds: list[tuple[Path, dict[str, dict[str, str]] | None]] = []

    def fake_build(path: Path, *, relationship_keys=None):
        builds.append((path, relationship_keys))
        return inventory

    monkeypatch.setattr("db_rag.study.build_relationship_inventory", fake_build)
    database = tmp_path / "report.duckdb"
    database.touch()
    keys = {"participants": {"report_participant": "SUBJID"}}
    source = DuckDbStudyDataSource(database, relationship_keys=keys)

    assert source.relationship_inventory() is inventory
    assert source.relationship_inventory() is inventory
    assert builds == [(database, keys)]
```

In `tests/test_installed_study_bundle.py`, import `json`,
`create_package_archive_from_root`, and `create_package_root`, then add:

```python
def test_database_package_binds_catalog_relationship_keys(tmp_path) -> None:
    package_root = create_package_root(tmp_path / "source")
    catalog_path = package_root / "database" / "schema_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["tables"][0].update(
        {"has_subjid_join": True, "subjid_col": "SUBJID"}
    )
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "example-study.tar.gz",
    )
    installed = install_study_archives(
        [archive],
        tmp_path / "runtime" / "studies",
    )[0]

    bundle = build_study_bundle(installed)
    source = bundle.data_sources["example-source"]

    assert source.relationship_keys == {
        "participants": {"report_participant": "SUBJID"}
    }
```

- [ ] **Step 2: Write failing readiness and installer-validation tests**

In `_write_complete_assets`, change the participant table catalog entry to:

```python
{
    "table": "participants",
    "text": "Participant-level records.",
    "has_subjid_join": True,
    "subjid_col": "SUBJID_PSEUDO",
}
```

Update `fail_relationships` to accept `*, relationship_keys=None`. Add:

```python
def test_declared_relationship_column_missing_from_duckdb_is_not_configured(
    tmp_path: Path,
) -> None:
    assets = _write_complete_assets(tmp_path)
    catalog = json.loads(assets["catalog_path"].read_text(encoding="utf-8"))
    catalog["tables"][0]["subjid_col"] = "MISSING_SUBJID"
    catalog["columns"].append(
        {
            "table": "participants",
            "column": "MISSING_SUBJID",
            "text": "Broken declared key.",
        }
    )
    assets["catalog_path"].write_text(json.dumps(catalog), encoding="utf-8")
    paths = DbRagRuntimePaths(
        duckdb_path=assets["duckdb_path"],
        catalog_path=assets["catalog_path"],
        chroma_path=assets["chroma_path"],
        embedding_model=MODEL,
    )

    readiness = resolve_db_rag_readiness(paths=paths)

    assert readiness.status == "not_configured"
    assert "missing duckdb column" in readiness.message.casefold()
```

In `tests/test_study_package_installer.py`, add:

```python
def test_stage_rejects_declared_relationship_column_missing_from_duckdb(
    tmp_path: Path,
) -> None:
    package_root = create_package_root(tmp_path / "source")
    catalog_path = package_root / "database" / "schema_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["tables"][0].update(
        {"has_subjid_join": True, "subjid_col": "MISSING_SUBJID"}
    )
    catalog["columns"].append(
        {
            "table": "participants",
            "column": "MISSING_SUBJID",
            "text": "Broken declared key.",
        }
    )
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "study.tar.gz",
    )

    with pytest.raises(ValueError, match="missing DuckDB column"):
        stage_study_archive(archive, tmp_path / "runtime" / "studies")
```

- [ ] **Step 3: Run the three wiring test files and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_report_study_bundle.py \
  tests/test_db_rag_relationship_readiness.py \
  tests/test_study_package_installer.py -vv
```

Expected: tests fail because relationship mappings are not yet passed through and staged packages do not validate declarations against DuckDB.

- [ ] **Step 4: Wire study-bundle construction**

In `db_rag/study.py`, load the catalog once and pass the parsed mapping:

```python
from .relationships import (
    RelationshipInventory,
    build_relationship_inventory,
    catalog_relationship_keys,
)


@dataclass(frozen=True)
class DuckDbStudyDataSource:
    path: Path
    relationship_keys: dict[str, dict[str, str]] | None = None

    @cached_property
    def _relationship_inventory(self) -> RelationshipInventory:
        if not self.path.is_file():
            raise StudySourceUnavailableError(
                "The configured DuckDB study source is unavailable."
            )
        return build_relationship_inventory(
            self.path,
            relationship_keys=self.relationship_keys,
        )
```

Within `build_study_bundle`, assign
`catalog_data = load_full_schema_catalog(paths.catalog_path)`, use it for
`SchemaCatalog`, and construct the source with
`relationship_keys=catalog_relationship_keys(catalog_data)`.

- [ ] **Step 5: Wire readiness and package staging validation**

In `db_rag/readiness.py`, after the existing catalog shape check:

```python
relationship_keys = catalog_relationship_keys(catalog)
inventory = build_relationship_inventory(
    paths.duckdb_path,
    relationship_keys=relationship_keys,
)
```

Keep this inside the existing bounded error handling.

In `study_package/installer.py`, make `_validate_catalog` return the parsed
dictionary after its existing structural validation. In
`validate_staged_package`, retain the existing DuckDB check, capture the
catalog, and validate its declarations against the staged database:

```python
catalog = _validate_catalog(resolved["database.catalog"])
build_relationship_inventory(
    resolved["database.duckdb"],
    relationship_keys=catalog_relationship_keys(catalog),
)
```

- [ ] **Step 6: Run the wiring tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_report_study_bundle.py \
  tests/test_db_rag_relationship_readiness.py \
  tests/test_study_package_installer.py -vv
```

Expected: all selected tests pass.

- [ ] **Step 7: Run DB-RAG tool regressions**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_multi_study_db_rag_tools.py \
  tests/test_db_rag_agent_tools.py \
  tests/test_installed_study_bundle.py -q
```

Expected: all selected tests pass. If an existing fixture directly constructs
`DuckDbStudyDataSource`, pass an explicit empty mapping for standalone data or
the exact declared mapping represented by that fixture.

- [ ] **Step 8: Commit production wiring and validation**

```bash
git add \
  db_rag/study.py \
  db_rag/readiness.py \
  study_package/installer.py \
  tests/test_report_study_bundle.py \
  tests/test_db_rag_relationship_readiness.py \
  tests/test_study_package_installer.py \
  tests/test_installed_study_bundle.py
git commit -m "fix: bind relationships to study catalogs"
```

### Task 4: Add and run the real two-package relationship smoke

**Files:**
- Create: `scripts/smoke_catalog_declared_relationships.py`

**Interfaces:**
- Consumes: `install_study_archives`, `discover_studies`, and the two bundled package archives.
- Produces: one executable internal smoke proving strict RePORT and NHANES relationship behavior through production package boundaries.

- [ ] **Step 1: Write the smoke script**

Create a script following the existing real package smoke structure:

```python
"""Exercise catalog-declared relationships in both bundled study packages."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from study_package.installer import install_study_archives
from study_package.registry import discover_studies


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-archive", type=Path, required=True)
    parser.add_argument("--nhanes-archive", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    archives = [
        args.report_archive.expanduser().resolve(),
        args.nhanes_archive.expanduser().resolve(),
    ]
    for archive in archives:
        if not archive.is_file():
            raise FileNotFoundError(f"Study archive not found: {archive}")

    with tempfile.TemporaryDirectory(
        prefix="catalog-relationships-smoke-"
    ) as temporary:
        studies_root = Path(temporary) / "studies"
        install_study_archives(archives, studies_root)
        studies = discover_studies(studies_root)

        report = studies.require("report-india-synthetic")
        report_inventory = report.data_sources[
            "report-india-synthetic"
        ].relationship_inventory()
        report_profile = report_inventory.profile_relationship(
            "Enrollment Cohort A",
            "Baseline Clinical and Demographic Information Cohort A",
            [("SUBJID", "SUBJID")],
        )
        if report_profile.matched_keys < 1:
            raise AssertionError("RePORT SUBJID relationship has no matched keys")

        nhanes = studies.require("nhanes-2017-2018")
        nhanes_inventory = nhanes.data_sources[
            "nhanes-2017-2018"
        ].relationship_inventory()
        paths = nhanes_inventory.find_join_paths("DEMO_J", "DIQ_J")
        if not paths or paths[0].profiles[0].key_pairs != [("SEQN", "SEQN")]:
            raise AssertionError(f"NHANES SEQN path unavailable: {paths}")

    print(
        "catalog-declared relationship smoke passed: "
        f"RePORT matched={report_profile.matched_keys}, "
        f"NHANES paths={len(paths)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Check script syntax without running the smoke**

Run:

```bash
.venv/bin/python -m py_compile scripts/smoke_catalog_declared_relationships.py
```

Expected: exit code 0 and no output.

- [ ] **Step 3: Run the dedicated smoke exactly once**

Run once, with a five-minute maximum:

```bash
.venv/bin/python scripts/smoke_catalog_declared_relationships.py \
  --report-archive report-india-synthetic-0.3.1.tar.gz \
  --nhanes-archive nhanes-2017-2018-0.2.0.tar.gz
```

Expected output contains `catalog-declared relationship smoke passed`, a
positive RePORT match count, and at least one NHANES path. On failure or
timeout, preserve and report the traceback and do not rerun automatically.

- [ ] **Step 4: Commit the dedicated smoke**

```bash
git add scripts/smoke_catalog_declared_relationships.py
git commit -m "test: smoke catalog-declared relationships"
```

### Task 5: Final regression verification

**Files:**
- Verify only; modify production or test files only if a failure directly exposes a catalog-relationship regression.

**Interfaces:**
- Consumes: all deliverables from Tasks 1–4.
- Produces: current evidence that focused and broad tests pass with no stale regex remaining.

- [ ] **Step 1: Prove the stale heuristic is absent**

Run:

```bash
rg -n "_IDENTIFIER_COLUMN|_is_identifier_column|import re" db_rag/relationships.py
```

Expected: no matches.

- [ ] **Step 2: Run the focused relationship and package suites**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_db_rag_relationships.py \
  tests/test_report_study_bundle.py \
  tests/test_db_rag_relationship_readiness.py \
  tests/test_study_package_installer.py \
  tests/test_installed_study_bundle.py \
  tests/test_multi_study_db_rag_tools.py \
  tests/test_db_rag_agent_tools.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run the full Python suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests pass with no unexpected warnings or collection errors.

- [ ] **Step 4: Check repository integrity**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; status contains only intentional commits or explicitly reported pre-existing user changes.
