# Mandatory Semantic Publication Retrieval Design

## Goal

Remove the stale lexical-only schema-search path and make packaged publication
evidence retrieval use mandatory semantic retrieval with deterministic lexical
boosting. Exact schema inspection and exact publication-source opening remain
unchanged.

## Scope

This change has two runtime parts:

1. `SchemaCatalog` remains responsible for exact catalog operations such as
   `inspect_table()` and `field_exists()`, but its `search()` and
   `search_many()` operations fail closed unless a semantic catalog is bound.
   `SemanticSchemaCatalog` remains the only working catalog-search provider and
   continues to combine mandatory vector retrieval with lexical boosting.
2. Publication evidence search gains a semantic provider over the packaged
   Chroma `study_knowledge` collection. It filters vector candidates to
   `source_kind="publication"`, combines them with the existing weighted
   lexical ranking, and deterministically fuses the two rankings. Vector
   retrieval is mandatory; failures raise a dedicated unavailable error rather
   than silently returning lexical-only results.

This change does not alter `dbrag-inspect_table`, dataset-plan behavior,
automatic study routing, the study-package format, or publication source
opening. The existing RePORT package already contains publication chunks in
`study_knowledge`. A study such as NHANES with no publication knowledge keeps
the publication search tool unavailable.

## Architecture

### Catalog search

`SchemaCatalog.search_many()` will raise
`SemanticCatalogUnavailableError` for non-empty queries. It will preserve the
existing neutral behavior for an empty query list and non-positive limits.
`UnavailableSemanticSchemaCatalog` may remain as an explicit runtime marker,
but no lexical-only search implementation will remain in the base catalog.

### Publication search

`LocalPublicationKnowledge` continues to load and validate publication JSON,
provide exact `open_source()` lookup, and supply lexical ranking data. A
semantic publication wrapper will receive:

- the validated local publication knowledge;
- the study's `study_knowledge` Chroma collection; and
- the configured embedding function.

For a non-empty query, it will embed the query once, retrieve vector candidates
with a Chroma metadata filter for publication chunks, obtain lexical candidates
from the local provider, and fuse both ranked lists by stable chunk identity.
Exact lexical matches boost ordering but cannot substitute for failed vector
retrieval. Vector or embedding failures become a typed semantic-publication
unavailable error.

The session-level study binding that currently binds per-study semantic schema
collections will also bind the publication wrapper when the selected study has
local publication knowledge. This preserves isolation when RePORT and NHANES
are installed simultaneously.

### Tool errors

`publication-search_study_evidence` will translate the typed semantic retrieval
error to a recoverable `SEMANTIC_STUDY_KNOWLEDGE_UNAVAILABLE` tool error.
Missing publication support still returns the existing
`STUDY_KNOWLEDGE_UNAVAILABLE` error. `publication-open_study_source` continues
to use exact local lookup and therefore remains available even if semantic
retrieval is temporarily unavailable.

## Package and Installer Boundary

No archive, manifest, builder, or installer-format change is required. The
runtime consumes the existing `study_knowledge` Chroma collection. Install-time
validation of publication-vector completeness is intentionally excluded from
this change; runtime search fails visibly if the mandatory vector collection is
missing or unusable.

## Testing

Tests will prove:

- a base `SchemaCatalog` cannot return lexical-only search results;
- exact catalog inspection and field existence still work;
- publication semantic search embeds once, filters to publication chunks, and
  fuses vector and lexical candidates deterministically;
- vector or embedding failures cannot fall back to lexical-only publication
  results;
- exact publication source opening is unaffected;
- the publication tool exposes the typed semantic-unavailable failure;
- separate study sessions bind separate publication collections; and
- focused catalog, publication, session-study, and installer regression suites
  remain green.
