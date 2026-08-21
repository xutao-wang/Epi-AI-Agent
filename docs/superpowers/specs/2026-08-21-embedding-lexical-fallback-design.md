# Embedding-Aware Lexical Retrieval Fallback

## Goal

Keep evidence retrieval available when the configured embedding service cannot
be used. The agent must continue its run with lexical-only evidence, while the
tool result explicitly states that semantic retrieval was unavailable and why.

This behavior applies to these three tools:

- `dbrag-search_catalog`
- `publication-search_study_evidence`
- `study-design-search`

Exact table inspection, source opening, database execution, and other tools are
unchanged because they do not create query embeddings.

## Retrieval Modes

Each search result records one of two modes:

- `hybrid_vector_lexical`: the embedding router is usable. Run vector retrieval
  and lexical ranking, then fuse the ranked candidates using the existing
  hybrid behavior.
- `lexical_fallback`: the embedding router is unavailable. Run local lexical
  ranking only and return the results normally.

The fallback is a successful, degraded tool result, not a `ToolExecutionError`.
It therefore does not interrupt the agent loop or consume a recoverable-error
retry.

## Availability and Reason Contract

Embedding unavailability includes missing credentials, unsupported or
mismatched embedding configuration, unavailable semantic index binding, and a
query-time embedding provider failure. The public explanation must be bounded,
deterministic, and must not expose provider response bodies or credentials.

Every lexical fallback result includes:

```json
{
  "retrieval_mode": "lexical_fallback",
  "embedding": {
    "available": false,
    "model": "OpenAI/text-embedding-3-large",
    "reason_code": "EMBEDDING_CREDENTIALS_MISSING",
    "message": "Embedding model OpenAI/text-embedding-3-large is unavailable because OPENAI_API_KEY is not configured. Results use lexical string search only."
  }
}
```

Reason codes distinguish at least missing credentials, configuration/index
unavailability, and query-time provider failure. Hybrid results may record the
same model with `available: true`, but do not carry a failure message.

## Component Design

### Catalog search

Restore the historical lexical-only path using the current
`_lexical_ranked_rows` scorer. `SchemaCatalog` and
`UnavailableSemanticSchemaCatalog` retain the packaged catalog and return
lexical results rather than failing closed. `SemanticSchemaCatalog` continues
hybrid fusion when embeddings succeed and switches to lexical results when the
embedding request fails.

Schema hits keep `matched_by=("lexical",)` in fallback mode. The catalog tool
derives its top-level retrieval mode and embedding notice from a provider
search-status result instead of assuming every successful call was hybrid.

### Publication evidence search

`LocalPublicationKnowledge.search_lexical` remains the authoritative fallback.
`SemanticPublicationKnowledge` uses the current vector-plus-lexical fusion when
possible and delegates to the local lexical search when semantic retrieval is
unavailable. The unavailable semantic wrapper also delegates to its local
provider. The tool returns the same evidence/artifact shape plus retrieval mode
and embedding notice.

### Study-design search

Add deterministic lexical indexing over Markdown files beneath the declared
`study-design` root, including `overview.md` and nested reference documents.
Split documents into bounded heading-based sections, preserve relative source
path, section heading, and SHA-256 provenance, and rank sections by normalized
query-token overlap with heading and body boosts. When embeddings are usable,
retain the current Chroma result path. When they are unavailable, return the
ranked Markdown sections with the same `StudyDesignHit` shape and the fallback
notice.

## Failure Boundaries

Fallback applies only when the semantic capability is unavailable. Invalid
tool arguments, unreadable/corrupt package source files, malformed provider
responses that violate evidence provenance, and artifact persistence failures
remain explicit errors. This prevents degraded retrieval from hiding package
integrity or storage defects.

## Tests

Regression tests will prove:

1. Each of the three tools returns lexical evidence and a visible missing-key
   explanation when `OPENAI_API_KEY` is absent.
2. Each fallback result uses `retrieval_mode=lexical_fallback` and does not
   produce a terminal or recoverable tool error.
3. Hybrid retrieval remains unchanged when an embedder succeeds.
4. Query-time embedding failures degrade to lexical results with a sanitized
   reason.
5. Study-design lexical hits include bounded excerpts and source provenance.
6. Corrupt local package evidence still fails rather than being mislabeled as
   embedding unavailability.

## Compatibility

Existing hybrid result fields and artifact kinds remain intact. New status
metadata is additive. No new credential or provider is required for lexical
fallback, so Anthropic-only and OpenAI-compatible chat configurations can
complete evidence-driven workflows without an OpenAI embedding key.
