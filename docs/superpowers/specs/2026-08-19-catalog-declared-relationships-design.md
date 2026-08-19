# Catalog-Declared Relationship Discovery Design

## Problem

The relationship inventory currently classifies possible join columns with a
regular expression that recognizes names containing `ID`. That heuristic is
not study metadata. It excludes valid keys such as NHANES `SEQN`, can include
unrelated ID-looking columns, and prevents the runtime from safely validating
joins that the installed study package already declares.

The bundled catalogs contain study-specific relationship declarations:

- RePORT India table entries declare participant and family linkage through
  `has_subjid_join`/`subjid_col` and `has_fid_join`/`fid_col`.
- NHANES table entries declare respondent linkage through
  `has_seqn_join`/`seqn_col`.

Relationship profiling and automatic path discovery must use those catalog
declarations as their exclusive source of join keys.

## Goals

- Remove the identifier-name regular expression and all regex-derived join
  candidates.
- Bind each DuckDB relationship inventory to the schema catalog from the same
  installed study package.
- Permit explicit profiling only when both requested table columns are
  declared join keys in that package catalog.
- Build automatic relationship candidates only from compatible declared keys
  shared by two tables.
- Preserve the existing cardinality, match, row-multiplication, null, and
  unmatched-key measurements.
- Fail closed when relationship metadata is absent or invalid.

## Package-Creation Contract

The schema catalog's `tables` and `columns` arrays describe what data exists,
but ordinary column entries do not authorize joins. Every table intended to
participate in cross-table analysis must also declare its join key in its table
catalog entry. Tables that are intentionally standalone may omit relationship
metadata, and a single-table package may have no relationship declarations.

For the current catalog version, the recognized declarations are the fields
already shipped by the study packages:

| Relationship domain | Enable field | Column field |
| --- | --- | --- |
| NHANES respondent | `has_seqn_join` | `seqn_col` |
| RePORT participant | `has_subjid_join` | `subjid_col` |
| RePORT family | `has_fid_join` | `fid_col` |

When an enable field is `true`, its column field must be nonblank and must name
an actual column in both the catalog and DuckDB table. A false or absent enable
field does not declare a relationship, even if the associated column field or
an ID-looking column exists.

This regression fix will not change the package format or mutate the existing
immutable archives. A later catalog-version migration may replace the current
study-specific fields with a generic named `join_keys` structure that supports
new relationship domains and composite keys.

## Runtime Design

The study-bundle builder will load `database/schema_catalog.json` once and use
that same object for schema search and relationship configuration. A small
catalog parser will produce declared keys per table, retaining the relationship
domain so that only compatible declarations can connect two tables.

The DuckDB data source will receive this parsed declaration set and pass it to
the relationship-inventory builder. Each table inventory will retain its full
column list for validation while profiling statistics only for declared join
keys.

Explicit profiling will verify all of the following before querying data:

1. Both tables exist in the selected package's DuckDB database.
2. Each requested column exists in its respective table.
3. Each requested column is declared as a join key for that table.
4. The two declarations represent the same relationship domain.

Automatic discovery will compare compatible declarations rather than raw
column names. It will profile an edge only when the declared keys have at least
one matched non-null value. Join-path traversal will continue to use the
resulting observed edges and preserve its current hop and path limits.

This means `DEMO_J.SEQN` can connect to `DIQ_J.SEQN`, RePORT participant tables
can connect through their declared `SUBJID` columns, and an unrelated column
such as `VISIT_ID` cannot become a candidate merely because its name matches a
pattern.

## Validation and Errors

Catalog parsing will ignore malformed non-object table entries but will reject
an enabled relationship declaration with a missing table name or blank key
column. Inventory construction will reject declarations that refer to a table
or column absent from DuckDB. These failures will flow through the existing
study-readiness boundary so a broken package is reported as unavailable rather
than silently falling back to inferred joins.

An explicit request for an undeclared key will raise a bounded relationship
validation error through the existing DB-RAG tool boundary. A catalog with no
declared relationships remains valid for standalone-table analysis; it simply
produces no automatic relationship paths and rejects cross-table profiling.

## Testing and Verification

Focused tests will establish that:

- catalog declarations are parsed for `SEQN`, `SUBJID`, and `FID`;
- ID-looking but undeclared columns are not profiled or discovered;
- `SEQN` is profiled and automatically connects NHANES-style tables;
- explicit profiling rejects an existing but undeclared column;
- missing declared DuckDB columns fail inventory construction;
- study bundles pass the matching catalog declarations into their DuckDB data
  sources;
- existing cardinality and warning behavior remains unchanged.

The dedicated internal feature smoke will exercise the production package
installation and study-bundle boundary against both bundled archives. It will
assert a catalog-declared RePORT relationship and the `DEMO_J`-to-`DIQ_J`
NHANES `SEQN` relationship without mocking DB-RAG, DuckDB, or the packages. The
smoke will run once with a five-minute limit, as required by the repository
instructions.

## Out of Scope

- Inferring joins from column names, values, descriptions, or language-model
  output.
- Joining across different installed study packages.
- Changing or republishing the bundled study-package archives.
- Introducing the future generic `join_keys` catalog version.
