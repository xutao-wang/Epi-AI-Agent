# Semantic Catalog Runtime Wiring Design

## Goal

Make every installed study package use its packaged Chroma table and column
indexes for runtime schema retrieval. Catalog search will combine mandatory
semantic-vector retrieval with deterministic exact/lexical boosting. It must
never silently return lexical-only results when semantic retrieval is
unavailable.

The fix must work for one installed database and for multiple concurrently
installed databases. RePORT India and NHANES will each retain an isolated
Chroma client, table collection, column collection, schema catalog, and DuckDB
source.

## Current defect

Package installation and readiness validation confirm that
`table_summaries` and `column_chunks` exist. Study discovery then constructs a
`SchemaCatalog` from `schema_catalog.json` without attaching those Chroma
collections. `SchemaCatalog.search_many()` consequently takes its lexical-only
fallback branch. The catalog tool still runs, but no query embedding or Chroma
similarity search occurs.

This defect predates multi-study installation. It affects the existing
single-study RePORT runtime as well as NHANES.

## Scope

This change includes:

- session-bound semantic catalog construction for every active installed
  study;
- one isolated Chroma client per study index path;
- one query-embedding batch reused for table and column searches;
- mandatory vector retrieval plus exact/lexical result boosting;
- per-study readiness and failure reporting;
- DB-RAG tool availability when multiple valid studies are installed;
- retrieval-mode diagnostics and provenance;
- focused tests and a real RePORT-plus-NHANES semantic catalog smoke.

This change does not include:

- agent-driven automatic study routing and clarification, which is the next
  feature built on top of this runtime foundation;
- federated SQL or joins across study packages;
- package-format or database-builder changes;
- rebuilding either database index;
- frontend study-selection controls.

The existing `active_study_id` scoping remains the selection boundary during
this fix. Only the selected study may be searched or queried.

## Runtime ownership

Study package discovery remains startup-safe and provider-key-free. It loads
immutable package metadata, paths, catalog JSON, optional knowledge, and
DuckDB source descriptors.

The API graph factory receives the current session's provider key. It creates
a session-bound `StudyRegistry` by binding every structurally valid installed
study to its own semantic catalog:

```text
Session-bound StudyRegistry
├── report-india-synthetic
│   ├── PersistentClient(report/index)
│   ├── table_summaries
│   ├── column_chunks
│   ├── SchemaCatalog(report)
│   └── DuckDB(report)
└── nhanes-2017-2018
    ├── PersistentClient(nhanes/index)
    ├── table_summaries
    ├── column_chunks
    ├── SchemaCatalog(nhanes)
    └── DuckDB(nhanes)
```

Collection names may be identical because the persistent index directories
provide separate namespaces. The binding must never reuse one user's OpenAI
key in another user's graph or a global process-level catalog.

Opening the collections does not embed a query. Only a catalog search against
the selected study invokes the embedding function.

## Catalog retrieval contract

The agent continues to call one tool, `dbrag-search_catalog`, with a batch of
schema probes. The agent does not choose between lexical and vector modes.

For each batch, the selected study catalog will:

1. Embed all probes once with the package-declared and application-supported
   OpenAI embedding model.
2. Reuse those query vectors for both `table_summaries` and `column_chunks`.
3. Run deterministic exact/lexical matching over the selected study's catalog
   entries.
4. Merge vector and lexical rankings, deduplicate physical fields, and preserve
   source identity.
5. Return bounded evidence with retrieval provenance.

Lexical matching is a complementary exactness signal, not an availability
fallback. It prioritizes exact column codes, exact table names, exact phrases,
and bounded terminology overlap. Common generic words must not dominate the
ranking. Vector and lexical rankings will be combined with deterministic rank
fusion rather than by comparing incompatible raw scores.

Each returned hit records whether it was found by `vector`, `lexical`, or both.
The catalog-search observation records
`retrieval_mode: hybrid_vector_lexical` and bounded vector/lexical hit counts.

## Failure behavior

Production catalog search must fail closed when semantic retrieval cannot run.
Missing collections, embedding-model mismatch, query-embedding failure, or
Chroma query failure produce a recoverable
`SEMANTIC_CATALOG_UNAVAILABLE` tool error. No lexical-only catalog evidence is
returned after such a failure.

The current lexical-only branch will not remain an implicit production path.
Tests that do not need semantic retrieval must inject a purpose-built fake
catalog provider rather than relying on fallback behavior.

Readiness is evaluated per study. Multiple installed studies no longer make
DB-RAG globally unavailable merely because there is no sole default study.
DB-RAG tools are compiled when at least one installed study is structurally
ready. At invocation, the selected study must exist and be semantically ready;
otherwise the tool returns the corresponding typed error.

## Multiple-database isolation

The selected `StudyBundle` remains the only source available through
`ToolContext.study`. Catalog hits must have the selected package's `study_id`
and `source_id`. Table inspection, relationship discovery, SQL validation, and
DuckDB execution continue to resolve through that same bundle.

Tests must prove that a RePORT selection cannot return NHANES tables or columns
and that an NHANES selection cannot return RePORT evidence. The number of
installed databases must not multiply embedding requests because only the
selected catalog is searched.

## Performance

One catalog-tool call performs exactly one batched query-embedding operation.
The resulting vectors are reused for table and column Chroma searches. Local
lexical ranking and rank fusion operate over the small selected catalog and
must not trigger provider calls.

Timing diagnostics will separately report query embedding, table retrieval,
column retrieval, lexical ranking, and merge stages. This makes any latency
change observable without exposing provider credentials or query vectors.

## Verification

Focused tests will cover:

- session binding attaches the correct collections to each study;
- two databases with identical collection names remain path-isolated;
- one probe batch is embedded once and reused twice;
- semantic and exact matches are both represented in merged results;
- exact variable codes receive deterministic lexical preference;
- missing or failing Chroma produces `SEMANTIC_CATALOG_UNAVAILABLE`;
- no lexical-only result is returned after semantic failure;
- multiple installed studies keep DB-RAG tools available;
- selected-study retrieval and SQL sources cannot cross package boundaries;
- provider keys are session-bound and not retained in global study discovery.

A dedicated executable smoke under `scripts/` will use the real RePORT India
and NHANES archives, install both into a temporary studies root, bind both with
the configured provider key, and run real semantic catalog probes against each
selected study. It will assert correct study provenance, expected table or
column evidence, absence of cross-study hits, and one successful read-only
DuckDB inspection per package. In accordance with repository policy, this real
smoke runs once with a five-minute maximum and preserves its diagnostics if it
fails.

## Follow-on routing feature

After this wiring fix, a dedicated agent routing gate will choose a study from
bounded package descriptions or request clarification before any catalog
search. The routing feature will reuse the session-bound registry and strict
selected-study isolation defined here; it does not change the hybrid retrieval
contract.
