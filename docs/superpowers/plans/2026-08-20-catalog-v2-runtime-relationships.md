# Catalog-v2 Runtime Relationships Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Epi-AI-Agent's fixed study join mappings with a generic catalog-v2 relationship interpreter, expose declared relationship evidence to DB-RAG, and activate the new RePORT India and NHANES packages.

**Architecture:** A new `db_rag.catalog_relationships` module parses and structurally validates package relationship declarations into an immutable runtime specification. `db_rag.relationships` consumes that specification to authorize and profile shared-key or explicit cross-key joins, while installer, readiness, study binding, and DB-RAG tools all reuse the same contract. The Database producer validator receives the small prerequisite correction that makes relationship IDs reusable labels rather than unique record keys.

**Tech Stack:** Python 3.12, Pydantic v2, DuckDB, ChromaDB, pytest, JSON study-package catalogs, existing Epi-AI-Agent tool/artifact framework.

## Global Constraints

- Run Python only through the repository's Python 3.12 virtual environment.
- Production code must not contain fixed mappings or conditionals for `SEQN`, `SUBJID`, `FID`, `INDEXPID`, NHANES, RePORT India, disease names, or cohort names.
- Catalog version 2 is the only relationship contract accepted by the migrated runtime; there is no catalog-v1 fallback.
- A join is authorized only by a shared logical key or an exact explicit relationship endpoint declaration.
- Repeated relationship IDs are valid on distinct endpoint declarations and never authorize undeclared edges.
- Existing physical column names, descriptions, values, table names, and language-model output are never join inference sources.
- Invalid packages fail before promotion or registry mutation.
- No frontend file changes are required, so no frontend bundle rebuild is performed.
- The dedicated internal smoke must use production installation and relationship boundaries, must not stub applicable dependencies, must finish within five minutes, and must run exactly once after focused and full tests pass.
- Implement in isolated worktrees at execution time: Database from local `master`, and Epi-AI-Agent from `local-multi-study` containing design commit `c94c745`.
- In the Epi-AI-Agent worktree, execute each `.venv/bin/python` command below
  with `/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/.venv/bin/python`;
  this uses the required existing Python 3.12 environment without creating or
  tracking a second environment.

---

### Task 1: Correct reusable relationship IDs in the Database producer

**Working repository:** `Database`

**Files:**
- Modify: `scripts/catalog_relationships.py:71-166`
- Modify: `report-india-synthetic/tests/test_catalog_relationships.py:138-154`

**Interfaces:**
- Consumes: catalog-v2 relationship records with `id`, `from`, `to`, `expected_cardinality`, and `note`.
- Produces: `validate_catalog_relationships(catalog: Mapping[str, Any], duckdb_path: Path) -> None`, accepting repeated semantic IDs on distinct edges while rejecting duplicated or contradictory endpoint declarations.

- [ ] **Step 1: Write failing producer-contract tests**

Replace the duplicate-ID test with one test that adds a third table and a distinct edge under the existing ID, plus one test retaining the exact duplicated edge:

```python
def test_accepts_reused_relationship_id_for_distinct_endpoints(
    tmp_path: Path,
) -> None:
    database = _duckdb(tmp_path / "study.duckdb")
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            'CREATE TABLE "Contact Follow Up" '
            '(CONTACTID VARCHAR, INDEXPID VARCHAR)'
        )
        connection.execute(
            "INSERT INTO \"Contact Follow Up\" VALUES "
            "('C1', 'A001'), ('C2', 'A001'), ('C3', 'A002')"
        )
    catalog = _catalog()
    catalog["tables"].append(
        {
            "table": "Contact Follow Up",
            "has_subjid_join": False,
            "has_indexcasepid_join": True,
        }
    )
    catalog["columns"].extend(
        [
            {"table": "Contact Follow Up", "column": "CONTACTID"},
            {"table": "Contact Follow Up", "column": "INDEXPID"},
        ]
    )
    second = copy.deepcopy(catalog["relationships"][0])
    second["from"]["table"] = "Contact Follow Up"
    catalog["relationships"].append(second)

    validate_catalog_relationships(catalog, database)


def test_rejects_duplicate_relationship_endpoints(tmp_path: Path) -> None:
    catalog = _catalog()
    catalog["relationships"].append(copy.deepcopy(catalog["relationships"][0]))

    with pytest.raises(ValueError, match="duplicated endpoints"):
        validate_catalog_relationships(catalog, _duckdb(tmp_path / "study.duckdb"))
```

- [ ] **Step 2: Run the focused producer tests and confirm the new acceptance test fails**

Run from the Database worktree:

```bash
python3.12 -m pytest report-india-synthetic/tests/test_catalog_relationships.py -q
```

Expected: `test_accepts_reused_relationship_id_for_distinct_endpoints` fails with `relationship id is invalid or duplicated`; all unrelated cases retain their existing results.

- [ ] **Step 3: Change duplicate detection from IDs to endpoint identity**

Keep `_SAFE_KEY_ID` validation for each ID, but replace `seen_relationships: set[str]` with endpoint tracking. Resolve endpoints as `(table_name, key_id, physical_column)`, reject a repeated directed endpoint tuple, and allow the same ID on a different tuple:

```python
seen_relationships: dict[
    tuple[tuple[str, str], tuple[str, str]],
    tuple[str, str, str],
] = {}

# Inside the relationship loop, before resolving endpoints:
if not _SAFE_KEY_ID.fullmatch(relationship_id):
    raise ValueError("relationship id is invalid")

resolved_endpoints: list[tuple[str, str, str]] = []

# At the end of each existing endpoint-validation iteration:
resolved_endpoints.append((table_name, key_id, join_keys[key_id]))

directed = (
    (resolved_endpoints[0][0], resolved_endpoints[0][1]),
    (resolved_endpoints[1][0], resolved_endpoints[1][1]),
)
if directed in seen_relationships:
    raise ValueError("relationship has duplicated endpoints")
reverse = (directed[1], directed[0])
if reverse in seen_relationships:
    prior_id, prior_expected, prior_note = seen_relationships[reverse]
    reversed_expected = {
        "one_to_one": "one_to_one",
        "one_to_many": "many_to_one",
        "many_to_one": "one_to_many",
        "many_to_many": "many_to_many",
    }[prior_expected]
    if (
        relationship_id != prior_id
        or expected != reversed_expected
        or note != prior_note
    ):
        raise ValueError("relationship has contradictory reverse endpoints")
else:
    seen_relationships[directed] = (relationship_id, expected, note)
```

Use the physical columns from `resolved_endpoints` for `_matched_keys` and `_observed_side`. A consistent reverse declaration may be validated but does not create a second semantic edge in the consumer.

- [ ] **Step 4: Run the producer relationship and full Database test suites**

```bash
python3.12 -m pytest report-india-synthetic/tests/test_catalog_relationships.py -q
python3.12 -m pytest -q
```

Expected: focused tests pass; the full Database suite reports all tests passed.

- [ ] **Step 5: Commit the producer correction**

```bash
git add scripts/catalog_relationships.py report-india-synthetic/tests/test_catalog_relationships.py
git commit -m "fix: allow reusable catalog relationship ids"
```

---

### Task 2: Add the generic catalog-v2 relationship parser

**Working repository:** `Epi-AI-Agent`

**Files:**
- Create: `db_rag/catalog_relationships.py`
- Create: `tests/test_catalog_relationships.py`

**Interfaces:**
- Produces: `JoinEndpoint`, `DeclaredRelationship`, `RelationshipAuthorization`, and `CatalogRelationshipSpec` frozen Pydantic models.
- Produces: `parse_catalog_relationships(catalog: Mapping[str, Any]) -> CatalogRelationshipSpec`.
- Produces: `reverse_expected_cardinality(value: Cardinality) -> Cardinality`.
- Produces: `CatalogRelationshipSpec.authorize_pair(left_table, left_column, right_table, right_column) -> RelationshipAuthorization | None` for Task 3.

- [ ] **Step 1: Write parser contract tests using arbitrary names**

Create a catalog fixture whose names do not resemble either installed study:

```python
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
                "from": {"table": "children", "join_key": "guardian_token"},
                "to": {"table": "adults", "join_key": "person_token"},
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
```

Test all of these exact outcomes:

```python
def test_parses_dynamic_keys_and_explicit_relationship() -> None:
    specification = parse_catalog_relationships(_catalog())
    assert specification.table_keys == {
        "adults": {"person_token": "PERSON_TOKEN"},
        "children": {
            "guardian_token": "GUARDIAN_TOKEN",
            "person_token": "PERSON_TOKEN",
        },
    }
    assert specification.relationships[0].relationship_id == "guardian_link"


def test_authorizes_shared_key_and_explicit_cross_key() -> None:
    specification = parse_catalog_relationships(_catalog())
    shared = specification.authorize_pair(
        "adults", "PERSON_TOKEN", "children", "PERSON_TOKEN"
    )
    explicit = specification.authorize_pair(
        "children", "GUARDIAN_TOKEN", "adults", "PERSON_TOKEN"
    )
    assert shared is not None and shared.source == "shared_join_key"
    assert explicit is not None and explicit.relationship_id == "guardian_link"
    assert explicit.expected_cardinality == "many_to_one"


def test_reverse_authorization_reverses_cardinality() -> None:
    authorization = parse_catalog_relationships(_catalog()).authorize_pair(
        "adults", "PERSON_TOKEN", "children", "GUARDIAN_TOKEN"
    )
    assert authorization is not None
    assert authorization.direction == "reverse"
    assert authorization.expected_cardinality == "one_to_many"


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


def test_undeclared_cross_key_is_not_authorized() -> None:
    assert parse_catalog_relationships(_catalog()).authorize_pair(
        "adults", "PERSON_TOKEN", "children", "GUARDIAN_TOKEN"
    ) is not None
    assert parse_catalog_relationships(_catalog()).authorize_pair(
        "adults", "PERSON_TOKEN", "children", "PERSON_TOKEN_MISSING"
    ) is None
```

Add individual failure tests using these exact fresh-catalog mutations and
expected message fragments:

```python
catalog["catalog_version"] = 1
# "catalog_version 2"
catalog["join_keys"] = {"Person Token": "PERSON_TOKEN"}
# "invalid key"
del catalog["tables"][0]["has_guardian_token_join"]
# "has_guardian_token_join"
catalog["tables"][0]["has_guardian_token_join"] = "false"
# "boolean"
catalog["tables"][0]["has_guardian_token_join"] = True
# "catalog column"
catalog["relationships"][0]["from"]["table"] = "missing"
# "unknown table"
catalog["relationships"][0]["from"]["join_key"] = "missing"
# "unknown join key"
catalog["tables"][1]["has_guardian_token_join"] = False
# "not authorized"
catalog["relationships"][0]["id"] = ""
# "relationship id"
catalog["relationships"][0]["note"] = ""
# "incomplete"
catalog["relationships"][0]["expected_cardinality"] = "several"
# "cardinality"
catalog["relationships"].append(copy.deepcopy(catalog["relationships"][0]))
# "duplicated endpoints"
```

For the reverse contradiction case, append a copy with `from` and `to`
swapped but leave `expected_cardinality` as `many_to_one`; expect
`contradictory reverse endpoints`. Each mutation operates on a fresh
`_catalog()` result.

- [ ] **Step 2: Run the parser tests and confirm the module is missing**

```bash
.venv/bin/python -m pytest tests/test_catalog_relationships.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'db_rag.catalog_relationships'`.

- [ ] **Step 3: Implement the frozen catalog contract models**

Create these public types in `db_rag/catalog_relationships.py`:

```python
Cardinality = Literal[
    "one_to_one",
    "one_to_many",
    "many_to_one",
    "many_to_many",
]
RelationshipSource = Literal["shared_join_key", "declared_relationship"]
Direction = Literal["forward", "reverse"]


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
```

Implement `authorize_pair` by comparing all four table/column values against each declared endpoint first, then iterating the sorted intersection of the two tables' logical key IDs. It must return `None` for every unlisted pair.

- [ ] **Step 4: Implement structural parsing and reverse-edge normalization**

Use `CATALOG_VERSION = 2`, `_SAFE_KEY_ID = re.compile(r"^[a-z][a-z0-9_]*$")`, and explicit cardinality reversal:

```python
def reverse_expected_cardinality(value: Cardinality) -> Cardinality:
    return {
        "one_to_one": "one_to_one",
        "one_to_many": "many_to_one",
        "many_to_one": "one_to_many",
        "many_to_many": "many_to_many",
    }[value]
```

`parse_catalog_relationships` must construct `table_keys` exclusively from `join_keys` plus each table's `has_<key_id>_join` boolean. It must validate against the catalog `columns` array, allow a repeated relationship ID on different endpoint pairs, reject a repeated directed pair, and collapse a consistent reverse declaration onto the first declaration. Use the canonical endpoint pair only for duplicate/reverse detection; never use it to infer new edges.

- [ ] **Step 5: Run parser tests and commit**

```bash
.venv/bin/python -m pytest tests/test_catalog_relationships.py -q
git add db_rag/catalog_relationships.py tests/test_catalog_relationships.py
git commit -m "feat: parse generic catalog v2 relationships"
```

Expected: all parser tests pass and the commit contains no installed study names or fixed physical join-column names in production code.

---

### Task 3: Make the DuckDB relationship inventory consume the generic specification

**Working repository:** `Epi-AI-Agent`

**Files:**
- Modify: `db_rag/relationships.py:1-506`
- Modify: `tests/test_db_rag_relationships.py:1-300`

**Interfaces:**
- Consumes: `CatalogRelationshipSpec` and `RelationshipAuthorization` from Task 2.
- Produces: `RelationshipEvidence` embedded in every `RelationshipProfile`.
- Produces: `build_relationship_inventory(duckdb_path: Path, *, relationship_spec: CatalogRelationshipSpec) -> RelationshipInventory`.
- Produces: `RelationshipInventory.validate_declared_relationships() -> None`.

- [ ] **Step 1: Replace legacy test fixtures with an arbitrary catalog-v2 specification**

Remove imports and expectations for `catalog_relationship_keys`. Parse a generic catalog with `person_token`, `visit_token`, and an explicit `guardian_token -> person_token` edge. Extend the DuckDB fixture with `children` and `guardians` tables so the explicit edge has matched data and observed `many_to_one` cardinality.

Add assertions that:

```python
profile = inventory.profile_relationship(
    "screening", "visits", [("SUBJID_PSEUDO", "SUBJID_PSEUDO")]
)
assert profile.relationship_evidence[0].source == "shared_join_key"
assert profile.relationship_evidence[0].left_join_key == "person_token"

explicit = inventory.profile_relationship(
    "children", "guardians", [("GUARDIAN_TOKEN", "PERSON_TOKEN")]
)
assert explicit.relationship_evidence[0].relationship_id == "guardian_link"
assert explicit.relationship_evidence[0].expected_cardinality == "many_to_one"
assert explicit.relationship_evidence[0].note == "Each child references one guardian."

reverse = inventory.profile_relationship(
    "guardians", "children", [("PERSON_TOKEN", "GUARDIAN_TOKEN")]
)
assert reverse.relationship_evidence[0].direction == "reverse"
assert reverse.relationship_evidence[0].expected_cardinality == "one_to_many"
```

Retain the existing assertions for distinct counts, null rates, matched keys, joined rows, row multiplication, warnings, multi-hop paths, type mismatches, unknown tables, and missing DuckDB columns. Change incompatible-domain assertions to expect an unauthorized catalog relationship.

- [ ] **Step 2: Run the relationship tests and verify legacy behavior fails against the new contract**

```bash
.venv/bin/python -m pytest tests/test_db_rag_relationships.py -q
```

Expected: failures reference the missing `relationship_spec` interface and missing `relationship_evidence` output.

- [ ] **Step 3: Remove the hardcoded mapping and add evidence models**

Delete `_RELATIONSHIP_FIELDS`, `catalog_relationship_keys`, and `_domains_for_column`. Add:

```python
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
```

`profile_relationship` must call `self.specification.authorize_pair` for every requested physical pair. If any returns `None`, raise `KeyError("Relationship keys are not authorized by the study catalog: ...")`. Convert each authorization into one `RelationshipEvidence` entry without altering the existing DuckDB measurement queries.

- [ ] **Step 4: Pass the specification through inventory construction**

Change the constructors and convenience wrappers to require the parsed specification:

```python
class RelationshipInventory:
    def __init__(
        self,
        duckdb_path: Path,
        tables: list[TableRelationshipInventory],
        specification: CatalogRelationshipSpec,
    ) -> None:
        self.specification = specification


def build_relationship_inventory(
    duckdb_path: Path,
    *,
    relationship_spec: CatalogRelationshipSpec,
) -> RelationshipInventory:
```

Build each table's `relationship_keys` from `relationship_spec.table_keys`. Preserve existing physical-table/column checks and identifier profiling. Update module-level `profile_relationship` and `find_join_paths` wrappers to accept `relationship_spec` under the same keyword.

- [ ] **Step 5: Add explicit edges to candidate discovery and reverse all evidence**

Generate candidates in this order:

1. Each normalized explicit relationship in its declared direction.
2. Each shared logical key across distinct tables.

Profile only catalog-authorized pairs, retain only profiles with `matched_keys > 0`, and deduplicate by the unordered table/physical-column edge so an explicit declaration wins over a derived shared-key edge. Update `_reverse_profile` to swap both columns and logical keys, reverse expected cardinality, toggle forward/reverse direction, and preserve ID and note.

- [ ] **Step 6: Implement declared-data validation**

Add:

```python
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
                f"relationship {relationship.relationship_id} has no matched non-null keys"
            )
        observed = f"{profile.left_cardinality}_to_{profile.right_cardinality}"
        if observed != relationship.expected_cardinality:
            raise ValueError(
                f"relationship {relationship.relationship_id} expected cardinality "
                f"{relationship.expected_cardinality} but observed {observed}"
            )
```

Test the no-overlap and contradictory-cardinality cases using arbitrary key names.

- [ ] **Step 7: Run relationship and parser tests, scan for hardcoding, and commit**

```bash
.venv/bin/python -m pytest tests/test_catalog_relationships.py tests/test_db_rag_relationships.py -q
rg -n '_RELATIONSHIP_FIELDS|nhanes_respondent|report_participant|report_family|has_seqn_join|has_subjid_join|has_fid_join|seqn_col|subjid_col|fid_col' db_rag epi_agent study_package
git add db_rag/relationships.py tests/test_db_rag_relationships.py
git commit -m "feat: profile catalog v2 relationship edges"
```

Expected: tests pass; `rg` returns no production-code matches. Test fixture matches are allowed only when intentionally checking removed legacy inputs.

---

### Task 4: Wire catalog v2 through catalog building, installation, readiness, and study binding

**Working repository:** `Epi-AI-Agent`

**Files:**
- Modify: `db_rag/catalog.py:14,388-429`
- Modify: `db_rag/study.py:20-92`
- Modify: `db_rag/readiness.py:1-91`
- Modify: `study_package/installer.py:14-20,109-123,290-315`
- Modify: `tests/study_package_fixtures.py:150-200`
- Modify: `tests/test_study_package_installer.py:89-121`
- Modify: `tests/test_installed_study_bundle.py:55-90`
- Modify: `tests/test_db_rag_relationship_readiness.py:1-61`
- Modify: `tests/test_report_study_bundle.py:280-340`
- Modify: `tests/test_session_studies.py:27-52`

**Interfaces:**
- Consumes: `parse_catalog_relationships` and `build_relationship_inventory(..., relationship_spec=...)`.
- Produces: all staged and active study bundles bound to the exact catalog-v2 specification loaded from their own package.
- Produces: `DuckDbStudyDataSource.relationship_spec: CatalogRelationshipSpec`.

- [ ] **Step 1: Upgrade the shared package fixture and consumer tests to catalog v2**

Change `create_package_root` to emit:

```python
{
    "catalog_version": 2,
    "join_keys": {"participant_key": "SUBJID"},
    "relationships": [],
    "tables": [
        {
            "table": "participants",
            "text": "Participant records",
            "has_participant_key_join": True,
        }
    ],
    "columns": [
        {
            "table": "participants",
            "column": "SUBJID",
            "description": "Participant identifier",
            "text": "Participant identifier",
        }
    ],
}
```

Update the missing-key installer and readiness tests to use `join_keys` plus
`has_participant_key_join`. Add an installer test that changes the fixture to
catalog version 1 and expects `database.catalog must use catalog_version 2`.
Update the tracked session fixture to version 2 with its dynamic key
declarations.

Add an activation regression that installs a valid fixture, changes only its
installed catalog version to 1, and verifies activation fails clearly before
the registry write:

```python
def test_activate_rejects_installed_catalog_v1(tmp_path: Path) -> None:
    studies_root = tmp_path / "runtime" / "studies"
    installed = install_study_archives(
        [create_package_archive(tmp_path / "source")],
        studies_root,
    )[0]
    catalog_path = installed.package_root / "database" / "schema_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["catalog_version"] = 1
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(ValueError, match="catalog_version 2"):
        activate_study_version("example-study", "1.0.0", studies_root)
```

- [ ] **Step 2: Update catalog-builder tests before production code**

Change `build_full_schema_catalog` tests to pass:

```python
join_keys={"person_token": "PERSON_TOKEN"},
relationships=[],
```

and use table metadata `{"has_person_token_join": True}`. Assert the output contains the two top-level fields, retains `row_count`, retains the dynamic boolean, and contains none of `seqn_col`, `subjid_col`, `fid_col`, or the three fixed `has_*` fields. Keep the parametrized nonboolean test with `has_person_token_join`.

- [ ] **Step 3: Run the affected tests and confirm version/interface failures**

```bash
.venv/bin/python -m pytest \
  tests/test_study_package_installer.py \
  tests/test_installed_study_bundle.py \
  tests/test_db_rag_relationship_readiness.py \
  tests/test_report_study_bundle.py \
  tests/test_session_studies.py -q
```

Expected: failures show the installer still requires version 1, the builder lacks `join_keys`/`relationships`, and the study source still accepts `relationship_keys`.

- [ ] **Step 4: Make catalog building dynamic**

Import the single `CATALOG_VERSION = 2` constant from
`db_rag.catalog_relationships` into `db_rag.catalog`; do not define a second
version constant. Change `_table_entry` to accept
`join_key_ids: tuple[str, ...]`, retain only `row_count` from fixed profile
metadata, and require one boolean for every `has_<key_id>_join` field. Change
the builder signature to:

```python
def build_full_schema_catalog(
    *,
    table_chunks: list[Any],
    column_chunks: list[Any],
    source_fingerprint: str,
    join_keys: Mapping[str, str],
    relationships: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
```

Return JSON-safe copies of `join_keys` and `relationships` beside `catalog_version`; do not preserve any legacy `*_col` field.

- [ ] **Step 5: Reuse one parsed specification at every package boundary**

In the installer, require catalog version 2, parse once after `_validate_catalog`, build the inventory with `relationship_spec`, and call `inventory.validate_declared_relationships()` before index validation.

In readiness, parse the loaded catalog and perform the same
inventory/declaration validation inside the existing bounded error handling.
Return the explicit message `DB-RAG dataset is not configured: the schema
catalog must use catalog_version 2.` for a v1 catalog, and assert that text in
`tests/test_db_rag_relationship_readiness.py`.

In study binding, replace the data-source field and build call:

```python
@dataclass(frozen=True)
class DuckDbStudyDataSource:
    path: Path
    relationship_spec: CatalogRelationshipSpec

# build_study_bundle
relationship_spec = parse_catalog_relationships(catalog_data)
DuckDbStudyDataSource(
    paths.duckdb_path,
    relationship_spec=relationship_spec,
)
```

The data source passes that specification into `build_relationship_inventory`. Catch `ValueError` as well as `duckdb.Error` and expose it through `StudySourceUnavailableError` without fallback.

- [ ] **Step 6: Assert package binding retains generic declarations**

Update `test_database_package_binds_catalog_relationship_keys` to assert:

```python
assert source.relationship_spec.table_keys == {
    "participants": {"participant_key": "SUBJID"}
}
assert source.relationship_spec.relationships == ()
```

The test must not mention a study-specific logical relationship domain.

Update `test_duckdb_study_source_caches_relationship_inventory` in
`tests/test_report_study_bundle.py` to construct
`CatalogRelationshipSpec(table_keys={"participants": {"participant_key":
"PERSON_TOKEN"}}, relationships=())`, pass it as `relationship_spec`, and
assert the monkeypatched builder receives that exact object under the same
keyword. This removes the final tracked test caller of the old
`relationship_keys` data-source interface.

- [ ] **Step 7: Run the affected suite and commit**

```bash
.venv/bin/python -m pytest \
  tests/test_study_package_installer.py \
  tests/test_installed_study_bundle.py \
  tests/test_db_rag_relationship_readiness.py \
  tests/test_report_study_bundle.py \
  tests/test_session_studies.py \
  tests/test_db_rag_service_schema.py -q
git add db_rag/catalog.py db_rag/study.py db_rag/readiness.py \
  study_package/installer.py tests/study_package_fixtures.py \
  tests/test_study_package_installer.py tests/test_installed_study_bundle.py \
  tests/test_db_rag_relationship_readiness.py tests/test_report_study_bundle.py \
  tests/test_session_studies.py
git commit -m "feat: require catalog v2 study relationships"
```

Expected: all listed tests pass.

---

### Task 5: Expose relationship evidence to DB-RAG and constrain its prompt

**Working repository:** `Epi-AI-Agent`

**Files:**
- Modify: `epi_agent/db_rag/tools.py:420-466,1299-1452,2058-2118`
- Modify: `epi_agent/db_rag/prompt.py:1-75`
- Modify: `tests/test_multi_study_db_rag_tools.py:79-112,360-445`

**Interfaces:**
- Consumes: serialized `RelationshipProfile.relationship_evidence` from Task 3.
- Produces: bounded relationship evidence in both `dbrag-profile_relationship` and `dbrag-find_join_paths` messages/artifacts.
- Preserves: existing `key_pairs` dataset-plan and SQL-compiler contract.

- [ ] **Step 1: Extend the fake provider and write a failing tool-output test**

Make `_RelationshipInventory.profile_relationship` return:

```python
"relationship_evidence": [
    {
        "left_column": key_pairs[0][0],
        "right_column": key_pairs[0][1],
        "left_join_key": "child_key",
        "right_join_key": "adult_key",
        "source": "declared_relationship",
        "relationship_id": "guardian_link",
        "expected_cardinality": "many_to_one",
        "note": "Each child references one guardian.",
        "direction": "forward",
    }
],
```

Then assert the parsed tool message preserves all nine fields and the saved artifact contains the same evidence. Add a prompt assertion for the exact generic rule described in Step 3.

- [ ] **Step 2: Run the tool tests and confirm evidence is currently stripped**

```bash
.venv/bin/python -m pytest tests/test_multi_study_db_rag_tools.py -q
```

Expected: the new assertion fails because `_safe_relationship_profile` does not yet return `relationship_evidence`.

- [ ] **Step 3: Add bounded evidence rendering and the generic prompt rule**

Add this bounded evidence renderer:

```python
def _safe_relationship_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    required = {
        key: _bounded_text(value.get(key), limit=300)
        for key in (
            "left_column",
            "right_column",
            "left_join_key",
            "right_join_key",
        )
    }
    if not all(required.values()):
        return {}
    source = _bounded_text(value.get("source"), limit=100)
    if source not in {"shared_join_key", "declared_relationship"}:
        return {}
    result: dict[str, Any] = {**required, "source": source}
    for key in ("relationship_id", "note"):
        text = _bounded_text(value.get(key), limit=500)
        if text:
            result[key] = text
    expected = _bounded_text(value.get("expected_cardinality"), limit=100)
    if expected in {
        "one_to_one",
        "one_to_many",
        "many_to_one",
        "many_to_many",
    }:
        result["expected_cardinality"] = expected
    direction = _bounded_text(value.get("direction"), limit=100)
    if direction in {"forward", "reverse"}:
        result["direction"] = direction
    return result
```

Have `_safe_relationship_profile` include at most 20 evidence entries. Do not change `_operation_key_pairs`, `_profile_matches_operation`, SQL compilation, or the plan schema; they continue using exact physical `key_pairs`.

Add this generic instruction to `DB_RAG_SYSTEM_PROMPT` immediately after the existing relationship-inspection rule:

```text
Use the exact join columns, direction, and declared relationship evidence
returned by relationship tools. Never rename, substitute, or infer a join key.
```

- [ ] **Step 4: Verify dataset validation still consumes the profiled physical edge**

Import `_profile_matches_operation` and `_safe_relationship_profile` into the
test module and add this focused metadata-preservation test:

```python
def test_relationship_evidence_preserves_exact_physical_edge_matching() -> None:
    profile = _safe_relationship_profile(
        {
            "left_table": "CHILDREN",
            "right_table": "ADULTS",
            "key_pairs": [["CHILD_TOKEN", "ADULT_TOKEN"]],
            "left_distinct_keys": 2,
            "right_distinct_keys": 1,
            "matched_keys": 1,
            "joined_rows": 2,
            "left_cardinality": "many",
            "right_cardinality": "one",
            "warnings": [],
            "relationship_evidence": [
                {
                    "left_column": "CHILD_TOKEN",
                    "right_column": "ADULT_TOKEN",
                    "left_join_key": "child_key",
                    "right_join_key": "adult_key",
                    "source": "declared_relationship",
                    "relationship_id": "guardian_link",
                    "expected_cardinality": "many_to_one",
                    "note": "Each child references one guardian.",
                    "direction": "forward",
                }
            ],
        }
    )
    assert profile["relationship_evidence"][0]["relationship_id"] == "guardian_link"
    assert _profile_matches_operation(
        profile,
        left_table="CHILDREN",
        right_table="ADULTS",
        key_pairs=[("CHILD_TOKEN", "ADULT_TOKEN")],
    )
```

This proves metadata enrichment does not weaken or replace exact physical-edge
validation.

- [ ] **Step 5: Run tool tests and commit**

```bash
.venv/bin/python -m pytest tests/test_multi_study_db_rag_tools.py -q
git add epi_agent/db_rag/tools.py epi_agent/db_rag/prompt.py \
  tests/test_multi_study_db_rag_tools.py
git commit -m "feat: expose declared join evidence to db rag"
```

Expected: all multi-study DB-RAG tool tests pass.

---

### Task 6: Upgrade the production-boundary relationship smoke

**Working repository:** `Epi-AI-Agent`

**Files:**
- Modify: `scripts/smoke_catalog_declared_relationships.py:1-79`

**Interfaces:**
- Consumes: the real Database release archives, production installer, registry discovery, study bundles, DB-RAG tool registry, and artifact store.
- Produces: one executable smoke covering install, activation, explicit and shared-key paths, reverse metadata, and fail-closed behavior.

- [ ] **Step 1: Replace direct-only inventory assertions with production tool calls**

Retain required archive arguments and isolated `TemporaryDirectory`. After `install_study_archives` and `discover_studies`, create:

```python
context = ToolContext(
    studies=studies,
    artifact_store=StateArtifactStore(),
)
tools = build_db_rag_tool_registry()
```

Invoke `dbrag-profile_relationship` for RePORT's declared `Enrollment Cohort B.INDEXPID -> Enrollment Cohort A.SUBJID`, parse its JSON message, and assert:

```python
evidence = message["profile"]["relationship_evidence"][0]
assert evidence["relationship_id"] == "cohort_b_index_case"
assert evidence["expected_cardinality"] == "many_to_one"
assert evidence["direction"] == "forward"
assert evidence["note"]
assert message["profile"]["matched_keys"] > 0
```

- [ ] **Step 2: Add reverse, shared-key, NHANES, and rejection assertions**

Profile the same RePORT edge in reverse and assert swapped physical columns, `direction == "reverse"`, and `expected_cardinality == "one_to_many"`.

Use `dbrag-find_join_paths` for:

- RePORT `Enrollment Cohort A` and `Baseline Clinical and Demographic Information Cohort A`, expecting shared `SUBJID` evidence;
- NHANES `DEMO_J` and `DIQ_J`, expecting shared `SEQN` evidence.

Invoke `dbrag-profile_relationship` for existing non-key NHANES columns such as `DEMO_J.RIAGENDR` and `DIQ_J.DIQ010`; assert it raises `ToolExecutionError` with code `RELATIONSHIP_UNAVAILABLE`.

- [ ] **Step 3: Add an internal five-minute deadline**

Move the smoke body into
`_run(args: argparse.Namespace, artifact_root: Path) -> None`. Create its
isolated root with `tempfile.mkdtemp` in `main`; delete that exact root on
success and print its path to stderr without deleting it on failure. Then use
`signal.SIGALRM` around that helper:

```python
def _timeout(_signum: int, _frame: object) -> None:
    raise TimeoutError("catalog relationship smoke exceeded 300 seconds")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    artifact_root = Path(tempfile.mkdtemp(prefix="catalog-relationships-smoke-"))
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(300)
    try:
        _run(args, artifact_root)
    except BaseException:
        print(f"Smoke artifacts preserved at: {artifact_root}", file=sys.stderr)
        raise
    else:
        shutil.rmtree(artifact_root)
    finally:
        signal.alarm(0)
    return 0
```

Create `artifact_root` before any archive validation in `main` so every
post-start failure can report it. Install into `artifact_root / "studies"`,
which preserves the real registry automatically. Accumulate each successfully
parsed tool message in `tool_messages`, then refresh the diagnostic file after
each parse and before asserting that message:

```python
(artifact_root / "tool-messages.json").write_text(
    json.dumps(tool_messages, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
```

Do not run this smoke yet. The repository rule permits one execution only, after all automated non-smoke tests pass.

- [ ] **Step 4: Make the smoke executable and syntax-check without executing it**

```bash
chmod +x scripts/smoke_catalog_declared_relationships.py
.venv/bin/python -m py_compile scripts/smoke_catalog_declared_relationships.py
```

Expected: syntax compilation succeeds; no package installation or smoke assertions run.

- [ ] **Step 5: Commit the smoke update**

```bash
git add scripts/smoke_catalog_declared_relationships.py
git commit -m "test: smoke catalog v2 relationship routing"
```

---

### Task 7: Verify once, install and activate the releases, and audit hardcoding

**Working repository:** `Epi-AI-Agent`

**Files:**
- Runtime-only changes: `study_data/studies/registry.json` and ignored installed package directories.
- Verify only: all production Python under `db_rag/`, `epi_agent/`, and `study_package/`.

**Interfaces:**
- Consumes: Database archives `report-india-synthetic-0.4.0.tar.gz` and `nhanes-2017-2018-0.3.0.tar.gz`.
- Produces: active local studies `report-india-synthetic@0.4.0` and `nhanes-2017-2018@0.3.0`.

- [ ] **Step 1: Run all focused catalog, installer, relationship, study, and tool tests**

```bash
.venv/bin/python -m pytest \
  tests/test_catalog_relationships.py \
  tests/test_db_rag_relationships.py \
  tests/test_study_package_installer.py \
  tests/test_installed_study_bundle.py \
  tests/test_db_rag_relationship_readiness.py \
  tests/test_report_study_bundle.py \
  tests/test_session_studies.py \
  tests/test_multi_study_db_rag_tools.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run the complete application suite before consuming the one smoke attempt**

```bash
.venv/bin/python -m pytest -q
```

Expected: the complete suite passes. If it fails, preserve the output, fix through the same TDD cycle, and rerun the affected tests and full suite; do not run the dedicated smoke until this step passes.

- [ ] **Step 3: Run the dedicated production-boundary smoke exactly once**

```bash
.venv/bin/python scripts/smoke_catalog_declared_relationships.py \
  --report-archive "/Users/xutaowang/Desktop/RA work/Epi-Agent/Database/report-india-synthetic/delivery/report-india-synthetic-0.4.0.tar.gz" \
  --nhanes-archive "/Users/xutaowang/Desktop/RA work/Epi-Agent/Database/nhanes-2017-2018/delivery/nhanes-2017-2018-0.3.0.tar.gz"
```

Expected: one success line reports the RePORT explicit and shared paths, NHANES path, and rejected undeclared pair. If it fails or times out, preserve the complete traceback and temporary artifact diagnostics and do not rerun it automatically.

- [ ] **Step 4: Install and explicitly activate both releases in the real local study root**

Run with the new code from the implementation worktree while targeting the
primary checkout's normal local study root explicitly:

```bash
.venv/bin/python study_installer.py --study \
  "/Users/xutaowang/Desktop/RA work/Epi-Agent/Database/report-india-synthetic/delivery/report-india-synthetic-0.4.0.tar.gz" \
  "/Users/xutaowang/Desktop/RA work/Epi-Agent/Database/nhanes-2017-2018/delivery/nhanes-2017-2018-0.3.0.tar.gz" \
  --study-root "/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/study_data"
.venv/bin/python study_installer.py \
  --activate report-india-synthetic@0.4.0 \
  --study-root "/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/study_data"
.venv/bin/python study_installer.py \
  --activate nhanes-2017-2018@0.3.0 \
  --study-root "/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/study_data"
```

Expected: the commands print both installed identities and both activated identities. Older versions remain on disk.

- [ ] **Step 5: Verify registry, discovery, readiness, and installed catalog versions**

```bash
jq -e '
  .active["report-india-synthetic"] == "0.4.0" and
  .active["nhanes-2017-2018"] == "0.3.0"
' "/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/study_data/studies/registry.json"
.venv/bin/python -c 'from pathlib import Path; from study_package.registry import discover_studies; studies = discover_studies(Path("/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/study_data/studies")); assert studies.ids() == ("nhanes-2017-2018", "report-india-synthetic"); [study.data_sources[study.source_id].relationship_inventory().validate_declared_relationships() for study in studies.values()]; print(studies.ids())'
jq -e '.catalog_version == 2' \
  "/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/study_data/studies/packages/report-india-synthetic/0.4.0/database/schema_catalog.json"
jq -e '.catalog_version == 2' \
  "/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/study_data/studies/packages/nhanes-2017-2018/0.3.0/database/schema_catalog.json"
```

Expected: both `jq` checks succeed; discovery prints the two active study IDs; declared relationship validation raises no error.

- [ ] **Step 6: Perform the final hardcoding and worktree audit**

```bash
rg -n '_RELATIONSHIP_FIELDS|nhanes_respondent|report_participant|report_family|seqn_col|subjid_col|fid_col|\bSEQN\b|\bSUBJID\b|\bFID\b|\bINDEXPID\b' \
  db_rag epi_agent study_package
git status --short --branch
git log --oneline -6
```

Expected: no production hardcoding matches; only intended ignored local study installation changes are absent from Git status; the implementation commits are present. Use the `verification-before-completion` skill before reporting success and the `finishing-a-development-branch` skill to choose local merge/cleanup behavior.
