# Catalog-v2 Runtime Relationships Design

## Status

Approved in conversation on 2026-08-20.

This design supersedes the catalog-v1 relationship-field contract in
`2026-08-19-catalog-declared-relationships-design.md`. The earlier design
removed identifier-name inference, but intentionally retained a fixed mapping
for NHANES and RePORT relationship fields. Catalog version 2 removes that last
study-specific mapping from the application.

## Problem

The application currently converts a fixed set of table fields into runtime
relationship domains:

- `has_seqn_join` and `seqn_col`;
- `has_subjid_join` and `subjid_col`;
- `has_fid_join` and `fid_col`.

That mapping embeds knowledge of particular studies in application code. A
future study cannot add a different join key without another application
change. It also cannot describe a valid cross-key relationship such as a
household contact's `INDEXPID` referring to an index case's `SUBJID`.

The Database repository now produces catalog-version-2 packages with generic
top-level `join_keys`, dynamic per-table authorization flags, and explicit
cross-key `relationships`. Epi-AI-Agent must consume that contract directly,
make the evidence available to DB-RAG, and fail closed rather than guessing a
join.

## Goals

- Remove every study-specific join-key, relationship-domain, and column-name
  mapping from production application code.
- Interpret catalog-version-2 relationship metadata generically.
- Authorize same-key joins from shared logical key declarations.
- Authorize different-key joins only through an explicit catalog relationship.
- Preserve declared relationship direction, expected cardinality, note, and
  semantic ID in relationship-tool output.
- Validate catalog declarations against the packaged DuckDB data before a
  package becomes active.
- Install and activate RePORT India Synthetic 0.4.0 and NHANES 2017-2018 0.3.0
  after the runtime migration.
- Prove the behavior through focused tests, a production-boundary smoke, and
  the complete application test suite.

## Non-Goals

- Inferring joins from column names, descriptions, values, table names, study
  IDs, or language-model output.
- Maintaining a hidden fallback for catalog-version-1 relationship fields.
- Joining tables from different installed study packages.
- Encoding NHANES, RePORT India, disease names, or cohort names in the core or
  DB-RAG prompts.
- Deleting older installed package versions from disk.

## Catalog-v2 Contract

The relationship contract consists of three cooperating structures.

### Logical join keys

The top-level `join_keys` object maps an arbitrary logical key ID to the
physical column used by that key:

```json
{
  "join_keys": {
    "subjid": "SUBJID",
    "indexcasepid": "INDEXPID"
  }
}
```

The application treats the key IDs and physical names as package data. It
does not contain a recognized-key list or compare them with known study names.

### Per-table authorization

Every table entry contains one boolean `has_<key_id>_join` field for every
declared logical key. A true value authorizes the table to participate through
that key; a false value denies that authorization. The flag is authoritative:
an unlisted or similarly named DuckDB column does not authorize a join.

The runtime resolves each true flag to the physical column in `join_keys` and
verifies that the catalog and DuckDB table both contain that column. It does
not require or synthesize legacy `*_col` fields.

### Explicit relationships

Each top-level relationship declares directed endpoints by table and logical
join key, together with an expected cardinality and explanatory note. These
records support relationships whose two sides use different logical keys.

The relationship `id` is a reusable semantic grouping label, not a unique
record identifier. Multiple distinct endpoint declarations may carry the same
ID when they express the same kind of relationship. Sharing an ID does not by
itself authorize an undeclared edge; the endpoint declaration remains the
authorization boundary.

An exact duplicate directed endpoint declaration is invalid. If both
directions of one physical edge are supplied, their cardinalities and metadata
must be mutually consistent; the runtime normalizes them to one traversable
edge.

## Runtime Architecture

The data flow is:

```text
schema_catalog.json
        |
        v
generic catalog-v2 relationship parser
        |
        +--> installer/readiness validation
        |
        v
study-specific relationship specification
        |
        v
DuckDB relationship inventory
        |
        v
DB-RAG relationship tools
        |
        v
agent plan and generated SQL
```

A single parsed relationship specification will hold:

- the authorized logical keys and physical columns for each table; and
- the ordered collection of declared relationship edges.

Catalog parsing and structural validation should live in a small dedicated
module rather than in the DuckDB profiling algorithm. The relationship
inventory will consume the parsed specification and remain responsible for
observed matches, nulls, unmatched keys, row multiplication, and cardinality.

There will be no production constant equivalent to `_RELATIONSHIP_FIELDS` and
no branch for `SEQN`, `SUBJID`, `FID`, `INDEXPID`, NHANES, or RePORT India.

## Join Authorization

The inventory may profile or traverse a requested table-column pair only when
one of these rules applies:

1. Both table endpoints are authorized for the same logical key ID. The
   physical column names may be resolved from that shared declaration.
2. The catalog contains an explicit relationship whose endpoints exactly
   match the requested table and logical keys, in either traversal direction.

An explicit relationship's declared direction is retained. Traversing it in
reverse swaps the endpoints and reverses directional cardinality:
`many_to_one` becomes `one_to_many`, while `one_to_one` and `many_to_many`
remain unchanged.

No other pair is eligible. In particular, matching physical column names,
ID-looking names, semantic similarity, or a repeated relationship ID cannot
create a relationship.

## Tool and Agent Contract

Relationship profiles and join-path edges will include enough catalog evidence
for DB-RAG to use the exact declared join:

- left and right table and column;
- left and right logical key ID;
- relationship source: shared logical key or explicit declaration;
- relationship ID when explicitly declared;
- declared direction relative to the returned edge;
- expected cardinality when declared;
- observed cardinality and existing profile measurements; and
- the catalog linkage note when declared.

The relationship profile remains the evidence object used by dataset planning
and SQL generation. The existing DB-RAG prompt receives a short generic rule:
use the exact relationship evidence returned by the tools and never invent,
rename, or substitute join columns. The central core-agent prompt receives no
study-specific relationship content.

## Validation and Error Handling

Catalog version 2 is the supported runtime contract. Parsing and installation
will validate that:

- `join_keys` is an object of safe, nonblank logical IDs and nonblank physical
  column names;
- `relationships` is a list, including when it is empty;
- every table has a boolean `has_<key_id>_join` field for every logical key;
- every true flag resolves to a matching catalog column and DuckDB column;
- every explicit endpoint references a known table and logical key authorized
  on that table;
- relationship records contain valid IDs, notes, and supported cardinalities;
- repeated relationship IDs are allowed on distinct endpoint declarations;
- duplicate or contradictory endpoint declarations are rejected;
- explicit relationships have matched non-null keys in the packaged data; and
- observed uniqueness does not contradict the declared expected cardinality.

Validation is atomic with installation: a staged package must pass before its
files are promoted or the registry changes. A malformed active package fails
the existing readiness boundary and is reported unavailable; runtime code does
not fall back to inferred joins.

Catalog-version-1 packages may remain installed for history, but the migrated
runtime will not activate or bind them. Attempts to use one will explain that
a catalog-version-2 package is required. Installing a newer version updates
the active version without deleting the older package.

## Producer Contract Correction

The Database repository's current catalog-v2 validator treats relationship IDs
as unique. It must be corrected to allow a repeated ID on distinct endpoint
declarations and to reject duplicates by endpoint identity instead. This is a
contract correction only; the currently built archives each remain valid and
do not require rebuilding because their catalog content does not change.

## Testing

Focused application tests will cover:

- arbitrary logical key and physical column names, demonstrating that the
  interpreter has no recognized-study or recognized-column branches;
- repeated relationship IDs on distinct endpoint pairs;
- rejection of exact duplicate and contradictory endpoint declarations;
- same-logical-key discovery and profiling;
- explicitly declared cross-key discovery and profiling;
- forward and reverse traversal with reversed directional cardinality;
- rejection of existing but unauthorized columns and undeclared cross-key
  pairs;
- missing catalog and DuckDB columns;
- catalog-version-1 rejection with a clear upgrade error;
- relationship metadata propagation through DB-RAG tool results;
- dataset planning using the declared edge; and
- unchanged profile measurements and warnings.

The Database repository will receive a focused validator regression test that
uses two distinct endpoint records with the same relationship ID.

## Production-Boundary Smoke

The dedicated relationship smoke will call the real study installer against
both release archives in an isolated study root, discover and bind the
installed studies, and exercise the production DB-RAG relationship boundary.
It will prove:

- RePORT India `INDEXPID` to `SUBJID` is available with its relationship ID,
  note, direction, and expected `many_to_one` cardinality;
- reverse RePORT traversal returns the swapped columns and `one_to_many`;
- a RePORT same-logical-key relationship remains available where authorized;
- NHANES tables connect through the catalog-declared `SEQN` logical key; and
- an undeclared pair is rejected rather than guessed.

The smoke will run once with a five-minute timeout and will not stub the
installer, study bundle, DuckDB data, or relationship inventory.

## Local Migration and Verification

After focused tests and the isolated smoke pass:

1. Install RePORT India Synthetic 0.4.0 and NHANES 2017-2018 0.3.0 into the
   application's local `study_data` root.
2. Explicitly activate both new versions, even though successful installation
   normally selects the newly installed version.
3. Verify the registry points to those exact versions.
4. Verify study discovery, binding, and readiness for each active study.
5. Run the complete application test suite.

Older versions remain recoverable on disk but are no longer active.

## Acceptance Criteria

- Production application code contains no fixed mapping or conditional for
  the current studies' join keys or relationship domains.
- Catalog-v2 packages are validated and interpreted generically.
- Shared logical keys and explicitly declared cross-key edges work as designed.
- Repeated relationship IDs work on distinct declarations without authorizing
  undeclared edges.
- Relationship evidence reaches DB-RAG and constrains generated joins.
- Both new study versions are installed, active, discoverable, and ready.
- Focused tests, the one required smoke run, and the full test suite pass.
