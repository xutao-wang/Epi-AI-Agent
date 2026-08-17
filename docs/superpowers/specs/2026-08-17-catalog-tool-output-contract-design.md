# Catalog Tool Output Contract Design

## Goal

Correct two DB-RAG discovery output defects without constraining the agent's
scientific judgment:

1. `dbrag-search_catalog` must return every result produced for every probe,
   up to the requested per-probe limit.
2. Model-facing JSON must remain valid JSON and must never be cut in the middle
   of a document.

The agent will continue to decide whether a concept is resolved, whether a
candidate table needs inspection, and whether another search is warranted.
This change will not add a deterministic concept-resolution ledger or a
runtime rule that chooses candidates for the agent.

## Confirmed defects

`CatalogSearchArguments.limit` is documented and passed to the catalog as a
per-probe limit. The tool accepts up to five probes and ten hits per probe.
After receiving those batches, however, `_search_catalog()` flattens all hits
into one list and applies `_MAX_CATALOG_HITS = 25`. For a five-probe request,
the first probes can consume the entire global allowance and erase later
probes even though retrieval succeeded for them.

DB-RAG tools encode their model-facing message as JSON text. The generic
`serialize_tool_result()` function currently treats that JSON as arbitrary
text when enforcing its 12,000-character safety boundary. It slices the text
and appends `...`, leaving the inner message syntactically invalid. In table
inspection responses this can also hide `next_offset`, causing the agent to
guess pages and repeat or overlap calls.

## Chosen design

### Catalog search

Search results will remain grouped by probe throughout the observation and
model-facing response:

```json
{
  "retrieval_mode": "hybrid_vector_lexical",
  "source_ids": ["nhanes-2017-2018"],
  "retrieval_summary": {
    "probe_count": 2,
    "unique_table_count": 3,
    "unique_column_count": 4,
    "vector_hits": 8,
    "lexical_hits": 5
  },
  "probes": [
    {
      "query": "long-term blood sugar control",
      "returned_count": 5,
      "table_hits": 1,
      "column_hits": 4,
      "hits": []
    },
    {
      "query": "fasting glucose",
      "returned_count": 5,
      "table_hits": 2,
      "column_hits": 3,
      "hits": []
    }
  ]
}
```

There will be no separate global result-count limit. Each probe will contain
zero through `limit` hits, and probes with zero hits will still appear. With
the existing input constraints, the maximum complete response is five probe
groups containing ten hits each.

The saved artifact will retain the complete bounded evidence for each hit,
including provenance and the existing bounded description. The model-facing
response will contain all probe groups and all hits but use a compact hit shape:

- `source`
- `table`
- optional `column`
- short `text`
- `matched_by`

Redundant per-hit provenance and `retrieval_probe` will not be repeated in the
model view because the enclosing probe and saved artifact already preserve
them. Hybrid vector-plus-lexical retrieval remains mandatory; this change does
not alter ranking or embedding behavior.

### Exact table inspection

`dbrag-inspect_table` will keep its exact, paginated behavior. Its model-facing
response will always expose pagination before the field list and will include
explicit completion metadata:

```json
{
  "source": "nhanes-2017-2018",
  "table": "DEMO_J",
  "offset": 0,
  "returned_count": 25,
  "has_more": true,
  "next_offset": 25,
  "fields": []
}
```

`next_offset` will be present even when its value is `null`. Each model-facing
field will omit the top-level source and table repetition while retaining its
exact column identifier and bounded annotation text. The saved artifact will
continue to hold the richer field evidence.

This design makes the correct next page observable but does not force the
agent to request it.

### JSON safety boundary

The 12,000-character outer model-observation limit remains in place. The
generic serializer will distinguish structured JSON messages from plain text:

- JSON that fits is returned unchanged.
- Oversized plain text retains the existing bounded text behavior.
- Oversized JSON is never character-sliced. It is replaced by a small, valid,
  explicit JSON notice with a stable code such as
  `MODEL_TOOL_MESSAGE_TOO_LARGE`, the original character count, and guidance
  that the referenced artifact contains the complete result.

The compact catalog and inspection renderers are responsible for keeping all
normal maximum-size responses below the boundary. The explicit notice is a
fail-closed guard for an unexpected or hostile structured payload, not a
normal DB-RAG pagination mechanism. This prevents silent corruption if a
future renderer violates its size contract.

## Agent autonomy

There will be no concept-resolution JSON ledger and no deterministic rule that
marks a concept resolved. The runtime will not reject a new search merely
because a similar query was made, and it will not block an inspection because
a table was already viewed. Prompt guidance may continue to encourage efficient
search and inspection, but the LLM remains responsible for the scientific
choice of evidence and the decision to continue discovery.

## Alternatives considered

1. Keep the global 25-hit cap and distribute it evenly across probes. This is
   rejected because it still contradicts the declared per-probe contract and
   silently lowers the requested limit.
2. Raise the generic message-size limit and retain verbose flattened output.
   This is rejected because it leaves the malformed-JSON failure mode and
   spends context on repeated provenance.
3. Merge search and inspection into one tool. This is rejected because
   semantic candidate discovery and exact table enumeration have different
   purposes, costs, and evidence contracts.

## Compatibility and error handling

The catalog-search artifact schema will change from top-level `hits` plus
summary-only probe entries to complete `probes[].hits`. All in-repository
consumers and tests must move to the grouped schema in the same change. No
compatibility alias for the flattened `hits` list will remain, because keeping
it would preserve the ambiguous contract.

Malformed provider batches, unavailable semantic retrieval, invalid sources,
and unavailable tables will retain their existing typed errors. If a compact
DB-RAG model response unexpectedly triggers `MODEL_TOOL_MESSAGE_TOO_LARGE`, the
result remains syntactically valid and its artifact reference remains present,
making the contract failure visible rather than silently falling back or
returning partial evidence.

## Verification

Test-driven implementation will add focused regressions for:

- five probes with ten distinct hits each produce five ordered groups and all
  fifty hits in the saved observation;
- a zero-hit probe is retained in its original position;
- the model-facing catalog response contains every per-probe hit and remains
  below the protocol boundary for representative maximum-size results;
- inspection includes `returned_count`, `has_more`, and `next_offset`, with
  `next_offset: null` on the final page;
- maximum-page inspection remains valid nested JSON after protocol
  serialization;
- an unexpected oversized JSON message produces the explicit valid JSON
  notice and never a message ending in a raw `...` fragment;
- oversized plain text remains safely bounded;
- existing semantic-unavailability and selected-study isolation behavior is
  unchanged.

The dedicated internal feature smoke will invoke the production DB-RAG tool
registry against a real installed study catalog, issue a multi-probe semantic
search, inspect one returned table, pass both results through the production
protocol serializer, and assert grouped probe coverage plus parseable
pagination metadata. It will use the real configured embedding and study
dependencies, run once with a five-minute maximum, and preserve diagnostics on
failure as required by the repository instructions.
