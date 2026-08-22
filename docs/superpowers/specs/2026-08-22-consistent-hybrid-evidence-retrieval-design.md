# Consistent Hybrid Evidence Retrieval

## Goal

Give catalog, reviewed-publication, and study-design search one truthful hybrid
contract. When the selected embedding route is available, each search runs
vector and lexical retrieval independently, unions the candidates, and fuses
their rankings. When the route is unavailable, each search returns lexical
results successfully under the existing fallback contract.

## Scope

This design changes only these search providers and their tool result metadata:

- `dbrag-search_catalog`;
- `publication-search_study_evidence`; and
- `study-design-search`.

It does not change embedding-profile selection, startup probing, thread status
notices, exact table inspection, exact publication opening, dataset extraction,
or review behavior.

## Shared Retrieval Contract

An embedding-enabled search reports `hybrid_vector_lexical` only after both
retrieval branches have run. Vector and lexical candidates are independently
bounded, deduplicated by their authoritative evidence identity, and combined
with deterministic reciprocal-rank fusion using `K=60` and equal branch
weights of `1.0`. A candidate may enter the final result through vector
retrieval, lexical retrieval, or both.

For publication and study-design search, each branch retrieves at most twice
the requested final limit before fusion. A request for five results therefore
considers at most ten vector and ten lexical candidates, then returns at most
five fused hits. Catalog retains its existing separately bounded table, column,
and lexical candidate partitions.

Every returned hit records `matched_by` in the stable order `vector`,
`lexical`. Exact lexical catalog identifiers retain their existing priority.
Ties use stable evidence identifiers so repeated searches are deterministic.

If embedding routing, credentials, provider access, index binding, or query
embedding is unavailable, the provider returns `lexical_fallback` with the
existing sanitized embedding status. Fallback is a successful degraded result,
not a tool error. Malformed or unverifiable vector evidence remains an error and
must not be hidden as fallback.

## Provider Changes

### Catalog

Catalog search already satisfies the intended union-based behavior and remains
the reference implementation. Its existing exact-match priority, reciprocal
rank fusion, and `matched_by` output stay unchanged.

### Publication evidence

Publication search currently uses vector hits as the candidate gate and lets
lexical ranking boost only overlapping vector hits. It will instead union the
verified vector and verified local lexical candidates before fusion. A
lexical-only publication chunk can therefore appear in the final bounded
result. The existing lexical weight of `1.5` becomes the shared equal weight of
`1.0`. Existing publication identity and metadata verification remain mandatory
for vector hits.

### Study design

Study-design search currently returns only Chroma vector hits while labelling
the result hybrid. It will run the existing deterministic Markdown section
scorer even when vector retrieval succeeds, then union and fuse both branches.
Study-design file identity remains
`study-design-source.<sha256(relative_path)[:24]>`. Fusion uses the distinct
section evidence ID already stored as the Chroma row ID:
`study-design.<sha256(source_id:section:chunk_ordinal:body_text)[:24]>`.
Lexical indexing reproduces both identities from the authoritative Markdown so
the same section can be recognized across branches without merging unrelated
sections from one file. Returned evidence preserves evidence ID, file-level
source ID, path, file hash, section, text, distance when supplied by vector
search, and `matched_by`.

Before fusion, every vector study-design hit must match the authoritative local
Markdown section inventory by Chroma evidence ID, file-level source ID,
relative path, file SHA-256, heading, chunk ordinal, and exact body text.
Unknown, duplicated, inconsistent, or empty vector partitions remain explicit
integrity failures rather than silently becoming lexical fallback.

## Tool Output

The three tools retain their artifact kinds and top-level fields. Hybrid output
continues to include:

```json
{
  "retrieval_mode": "hybrid_vector_lexical",
  "embedding": {
    "available": true,
    "model": "OpenAI/text-embedding-3-large",
    "provider": "openai"
  }
}
```

Catalog keeps its aggregate vector and lexical hit counts. Publication and
study-design hits expose `matched_by` through their bounded provenance so the
reported mode can be audited.

## Verification

Focused tests will prove that each provider:

1. admits vector-only, lexical-only, and dual-matched candidates;
2. ranks dual-matched candidates deterministically;
3. reports `hybrid_vector_lexical` only when both branches execute;
4. retains lexical-only fallback when embeddings are unavailable; and
5. preserves existing integrity failures for malformed vector evidence.

The existing real DB-RAG smoke will be extended or accompanied by a dedicated
production-entry-point smoke that exercises all three providers with a real
embedding route and asserts auditable hybrid output.
