# Catalog Tool Output Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve every per-probe catalog result and guarantee that model-facing structured tool messages remain valid JSON without adding deterministic concept-resolution behavior.

**Architecture:** Replace the flattened catalog observation with ordered `probes[].hits` groups and split the current overloaded catalog renderer into dedicated search and table-inspection renderers. Keep rich evidence in artifacts, render complete but compact model observations, and make the generic protocol serializer emit an explicit valid-JSON overflow notice instead of slicing structured messages.

**Tech Stack:** Python 3.12, Pydantic, pytest, LangChain tool adapter, existing DB-RAG study/catalog abstractions, Chroma/OpenAI embeddings for the one-time real smoke.

## Global Constraints

- The LLM remains solely responsible for deciding whether a concept is resolved and whether another discovery call is useful.
- Do not add a concept-resolution ledger, deterministic candidate selection, duplicate-call blocking, or automatic page traversal.
- `CatalogSearchArguments.limit` remains a per-probe limit from 1 through 10, with up to five probes.
- Remove the separate global 25-hit catalog limit entirely; preserve ordered zero-hit probes and every returned hit up to each probe's requested limit.
- Keep mandatory hybrid vector-plus-lexical retrieval and selected-study isolation unchanged.
- Keep the 12,000-character protocol safety boundary.
- Never character-slice a valid JSON tool message.
- Run all Python commands with the repository's real Python 3.12 environment via `.venv/bin/python`.
- The dedicated real smoke runs once, for no more than five minutes, and is not automatically rerun after failure.

---

## File Structure

- Modify `epi_agent/db_rag/tools.py`: group catalog evidence by probe, add exact pagination metadata, and provide separate compact renderers for catalog search and table inspection.
- Modify `epi_agent/protocol.py`: preserve valid structured messages or replace an oversized one with an explicit valid-JSON notice.
- Modify `tests/test_db_rag_agent_tools.py`: regression coverage for fifty grouped hits, zero-hit probes, compact model output, and exact pagination.
- Modify `tests/test_epi_agent_protocol.py`: structured-overflow and existing plain-text-boundary coverage.
- Create `scripts/smoke_catalog_tool_output_contract_real.py`: exercise the production tool registry and serializer against the real installed NHANES package and semantic provider.

---

### Task 1: Preserve Every Catalog Result by Probe

**Files:**
- Modify: `tests/test_db_rag_agent_tools.py`
- Modify: `epi_agent/db_rag/tools.py`

**Interfaces:**
- Consumes: `catalog.search_many(queries: list[str], *, limit: int) -> list[list[SchemaEvidenceHit]]`
- Produces: catalog observation and message dictionaries with `probes: list[{query, returned_count, table_hits, column_hits, unique_table_count, unique_column_count, hits}]`
- Preserves: `retrieval_mode`, `source_ids`, and global retrieval summary counters.

- [ ] **Step 1: Add failing grouped-result regressions**

Add a catalog fake that produces ten unique results for each supplied probe and a second fake response containing an empty middle probe:

```python
class _ManyHitCatalog:
    def search_many(self, queries: list[str], *, limit: int):
        return [
            [
                SchemaEvidenceHit(
                    source="nhanes-2017-2018",
                    table=f"TABLE_{probe_index}_{hit_index}",
                    column=f"FIELD_{probe_index}_{hit_index}",
                    text=f"Evidence {probe_index}-{hit_index}",
                    provenance={
                        "authority": "runtime_schema_catalog",
                        "source_id": "nhanes-2017-2018",
                        "table": f"TABLE_{probe_index}_{hit_index}",
                        "column": f"FIELD_{probe_index}_{hit_index}",
                    },
                    matched_by=("vector", "lexical"),
                )
                for hit_index in range(limit)
            ]
            for probe_index, _query in enumerate(queries)
        ]


class _CatalogWithEmptyProbe(_ManyHitCatalog):
    def search_many(self, queries: list[str], *, limit: int):
        batches = super().search_many(queries, limit=limit)
        batches[1] = []
        return batches
```

Add these tests:

```python
def test_catalog_tool_preserves_ten_hits_for_each_of_five_probes() -> None:
    context = _context(_ManyHitCatalog())
    queries = [f"probe-{index}" for index in range(5)]

    result = build_db_rag_tool_registry().invoke(
        "dbrag-search_catalog",
        {"queries": queries, "limit": 10},
        context=context,
    )

    observation = context.artifact_store.require(result.artifacts[0]).content
    assert "hits" not in observation
    assert [probe["query"] for probe in observation["probes"]] == queries
    assert [probe["returned_count"] for probe in observation["probes"]] == [10] * 5
    assert sum(len(probe["hits"]) for probe in observation["probes"]) == 50
    assert observation["probes"][-1]["hits"][-1]["column"] == "FIELD_4_9"

    model_message = json.loads(result.message)
    assert [probe["query"] for probe in model_message["probes"]] == queries
    assert sum(len(probe["hits"]) for probe in model_message["probes"]) == 50


def test_catalog_tool_preserves_zero_hit_probe_in_original_position() -> None:
    context = _context(_CatalogWithEmptyProbe())

    result = build_db_rag_tool_registry().invoke(
        "dbrag-search_catalog",
        {"queries": ["first", "empty", "third"], "limit": 2},
        context=context,
    )

    observation = context.artifact_store.require(result.artifacts[0]).content
    assert observation["probes"][1] == {
        "query": "empty",
        "returned_count": 0,
        "table_hits": 0,
        "column_hits": 0,
        "unique_table_count": 0,
        "unique_column_count": 0,
        "hits": [],
    }
```

Update the existing hybrid-provenance test to read
`observation["probes"][0]["hits"][0]` rather than top-level `hits`.

- [ ] **Step 2: Run the tests and verify the intended failures**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_db_rag_agent_tools.py::test_catalog_tool_preserves_ten_hits_for_each_of_five_probes \
  tests/test_db_rag_agent_tools.py::test_catalog_tool_preserves_zero_hit_probe_in_original_position \
  -q
```

Expected: FAIL because the observation still has a globally capped top-level `hits` list and no complete `probes[].hits` groups.

- [ ] **Step 3: Implement grouped catalog observations and a dedicated renderer**

In `epi_agent/db_rag/tools.py`:

1. Delete `_MAX_CATALOG_HITS = 25`.
2. Replace the shared `hits` output list with `probe_results` while retaining a local `all_hits` list only for global counts. Normalize only `provider_hits[:limit]` so a provider cannot violate the declared per-probe bound.
3. For each provider batch, append this complete group:

```python
probe_results.append(
    {
        "query": query,
        "returned_count": len(normalized_hits),
        "table_hits": sum(1 for hit in normalized_hits if not hit.get("column")),
        "column_hits": sum(1 for hit in normalized_hits if hit.get("column")),
        "unique_table_count": len(probe_tables),
        "unique_column_count": len(probe_columns),
        "hits": normalized_hits,
    }
)
```

4. Build the observation as:

```python
content = {
    "queries": queries,
    "source_ids": source_ids,
    "retrieval_mode": "hybrid_vector_lexical",
    "retrieval_summary": {
        "probe_count": len(queries),
        "unique_table_count": len(all_tables),
        "unique_column_count": len(all_columns),
        "vector_hits": sum("vector" in hit.get("matched_by", []) for hit in all_hits),
        "lexical_hits": sum("lexical" in hit.get("matched_by", []) for hit in all_hits),
    },
    "probes": probe_results,
}
```

5. Add `_MAX_MODEL_CATALOG_TEXT_CHARS = 80` and `_compact_catalog_hit()` that returns only bounded `source`, exact bounded `table`, optional exact bounded `column`, `text` capped at 80 characters, and `matched_by`. Add `_render_catalog_search()` that preserves all ordered probe groups and at most the existing five-by-ten declared input bounds. Do not include per-hit provenance or `retrieval_probe` in the model view. Serialize this model message with compact JSON separators `(",", ":")`.
6. Change the `catalog_search` artifact renderer and `_search_catalog()` message to `_render_catalog_search`.
7. Compute the artifact summary from `sum(probe["returned_count"] for probe in probe_results)`.

- [ ] **Step 4: Run the focused catalog tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_db_rag_agent_tools.py -q
```

Expected: all catalog tool tests PASS, including fifty preserved hits and the empty probe.

- [ ] **Step 5: Commit the grouped search contract**

```bash
git add epi_agent/db_rag/tools.py tests/test_db_rag_agent_tools.py
git commit -m "fix: preserve catalog results by probe"
```

---

### Task 2: Make Table Pagination Explicit and Compact

**Files:**
- Modify: `tests/test_db_rag_agent_tools.py`
- Modify: `epi_agent/db_rag/tools.py`

**Interfaces:**
- Consumes: `catalog.inspect_table(source, table, *, offset, limit)`
- Produces: `{source, table, offset, returned_count, has_more, next_offset, fields}` with `next_offset` always present.

- [ ] **Step 1: Add failing inspection pagination tests**

Add an exact catalog fake whose table has thirty fields:

```python
class _InspectableCatalog(_HybridCatalog):
    def inspect_table(
        self,
        source: str,
        table: str,
        *,
        offset: int = 0,
        limit: int = 25,
    ):
        fields = [
            SchemaEvidenceHit(
                source=source,
                table=table,
                column=f"FIELD_{index:02d}",
                text=f"Annotated field {index}",
                provenance={
                    "authority": "runtime_schema_catalog",
                    "source_id": source,
                    "table": table,
                    "column": f"FIELD_{index:02d}",
                },
            )
            for index in range(30)
        ]
        return fields[offset : offset + limit]
```

Add tests for both a partial and final page:

```python
def test_inspect_table_returns_explicit_next_page_metadata() -> None:
    context = _context(_InspectableCatalog())

    result = build_db_rag_tool_registry().invoke(
        "dbrag-inspect_table",
        {
            "source": "nhanes-2017-2018",
            "table": "DEMO_J",
            "offset": 0,
            "limit": 25,
        },
        context=context,
    )

    message = json.loads(result.message)
    assert message["returned_count"] == 25
    assert message["has_more"] is True
    assert message["next_offset"] == 25
    assert len(message["fields"]) == 25
    assert set(message["fields"][0]) == {"column", "text", "source_kind"}


def test_inspect_table_final_page_includes_null_next_offset() -> None:
    context = _context(_InspectableCatalog())

    result = build_db_rag_tool_registry().invoke(
        "dbrag-inspect_table",
        {
            "source": "nhanes-2017-2018",
            "table": "DEMO_J",
            "offset": 25,
            "limit": 25,
        },
        context=context,
    )

    message = json.loads(result.message)
    assert message["returned_count"] == 5
    assert message["has_more"] is False
    assert "next_offset" in message
    assert message["next_offset"] is None
    assert [field["column"] for field in message["fields"]] == [
        f"FIELD_{index:02d}" for index in range(25, 30)
    ]
```

- [ ] **Step 2: Run the tests and verify the intended failures**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_db_rag_agent_tools.py::test_inspect_table_returns_explicit_next_page_metadata \
  tests/test_db_rag_agent_tools.py::test_inspect_table_final_page_includes_null_next_offset \
  -q
```

Expected: FAIL because `returned_count` and `has_more` are absent, final `next_offset` is removed by the shared renderer, and fields repeat verbose table-level data.

- [ ] **Step 3: Implement the exact inspection renderer and metadata**

In `epi_agent/db_rag/tools.py`:

1. Add `_compact_inspection_field()` returning exact `column`, bounded `text`, and `source_kind` only.
2. Add `_render_table_profile()` that constructs its dictionary directly so `next_offset: None` is retained:

```python
def _render_table_profile(content: dict[str, Any]) -> dict[str, Any]:
    fields = [
        field
        for field in (
            _compact_inspection_field(item)
            for item in _collection(content, "fields")
        )
        if field
    ][:_MAX_TABLE_FIELDS]
    return {
        "source": _bounded_text(content.get("source"), limit=300),
        "table": _bounded_text(content.get("table"), limit=300),
        "offset": max(0, int(content.get("offset") or 0)),
        "returned_count": len(fields),
        "has_more": bool(content.get("has_more")),
        "next_offset": content.get("next_offset"),
        "fields": fields,
    }
```

3. Add `returned_count` and `has_more` to `_inspect_table()`'s saved content based on the provider's look-ahead result.
4. Render the tool message and `table_profile` artifacts through `_render_table_profile`, not the search renderer.

- [ ] **Step 4: Run all DB-RAG tool tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_db_rag_agent_tools.py -q
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the inspection contract**

```bash
git add epi_agent/db_rag/tools.py tests/test_db_rag_agent_tools.py
git commit -m "fix: expose exact table pagination"
```

---

### Task 3: Never Character-Slice Structured JSON

**Files:**
- Modify: `tests/test_epi_agent_protocol.py`
- Modify: `tests/test_db_rag_agent_tools.py`
- Modify: `epi_agent/protocol.py`

**Interfaces:**
- Consumes: `ToolResult.message: str`
- Produces: outer JSON no longer than `_MAX_MODEL_TOOL_MESSAGE_CHARS`; if the input message is JSON, the returned `message` string is always parseable JSON.

- [ ] **Step 1: Add the failing structured-overflow regression**

Add this test to `tests/test_epi_agent_protocol.py`:

```python
def test_tool_result_serialization_never_slices_structured_json() -> None:
    original_message = json.dumps(
        {
            "next_offset": 25,
            "fields": [
                {"column": f"FIELD_{index}", "text": "x" * 1_000}
                for index in range(25)
            ],
        }
    )

    serialized = protocol.serialize_tool_result(
        ToolResult(
            message=original_message,
            artifacts=(ArtifactRef(id="profile-1", kind="table_profile", version=1),),
        )
    )
    outer = json.loads(serialized)
    inner = json.loads(outer["message"])

    assert len(serialized) <= protocol._MAX_MODEL_TOOL_MESSAGE_CHARS
    assert inner == {
        "artifact_available": True,
        "code": "MODEL_TOOL_MESSAGE_TOO_LARGE",
        "original_char_count": len(original_message),
    }
    assert outer["artifacts"] == [
        {"id": "profile-1", "kind": "table_profile", "version": 1}
    ]
```

Keep the existing escaped-message test as the regression that oversized plain
text is still safely cut and ends with `...`.

- [ ] **Step 2: Add a model-boundary regression for maximum normal DB-RAG output**

In `tests/test_db_rag_agent_tools.py`, import `serialize_tool_result` and add:

```python
def test_maximum_catalog_message_survives_protocol_serialization() -> None:
    context = _context(_ManyHitCatalog())
    result = build_db_rag_tool_registry().invoke(
        "dbrag-search_catalog",
        {"queries": [f"probe-{index}" for index in range(5)], "limit": 10},
        context=context,
    )

    outer = json.loads(serialize_tool_result(result))
    message = json.loads(outer["message"])

    assert "code" not in message
    assert len(message["probes"]) == 5
    assert sum(len(probe["hits"]) for probe in message["probes"]) == 50
```

Add the following inspection assertion using `_InspectableCatalog` and a
25-field first page:

```python
def test_maximum_inspection_message_survives_protocol_serialization() -> None:
    context = _context(_InspectableCatalog())
    result = build_db_rag_tool_registry().invoke(
        "dbrag-inspect_table",
        {
            "source": "nhanes-2017-2018",
            "table": "DEMO_J",
            "offset": 0,
            "limit": 25,
        },
        context=context,
    )

    outer = json.loads(serialize_tool_result(result))
    message = json.loads(outer["message"])

    assert "code" not in message
    assert message["returned_count"] == 25
    assert message["has_more"] is True
    assert message["next_offset"] == 25
    assert len(message["fields"]) == 25
```

- [ ] **Step 3: Run the new tests and verify the intended failures**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_epi_agent_protocol.py::test_tool_result_serialization_never_slices_structured_json \
  tests/test_db_rag_agent_tools.py::test_maximum_catalog_message_survives_protocol_serialization \
  tests/test_db_rag_agent_tools.py::test_maximum_inspection_message_survives_protocol_serialization \
  -q
```

Expected: the protocol test fails because the inner message ends in a raw
ellipsis and cannot be parsed. If either normal DB-RAG response triggers the
new overflow notice after implementation, compact its domain renderer rather
than weakening the 12,000-character boundary or discarding hits.

- [ ] **Step 4: Implement fail-closed structured-message serialization**

In `epi_agent/protocol.py`, add:

```python
def _is_json_message(message: str) -> bool:
    try:
        json.loads(message)
    except (TypeError, ValueError):
        return False
    return True


def _structured_message_overflow_notice(
    message: str,
    *,
    artifact_available: bool,
) -> str:
    return json.dumps(
        {
            "artifact_available": artifact_available,
            "code": "MODEL_TOOL_MESSAGE_TOO_LARGE",
            "original_char_count": len(message),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
```

In `serialize_tool_result()`, after collecting safe artifact references and
before character slicing, try the complete structured message first. If it
fits without some trailing artifact references, remove only those trailing
model references until the complete message fits. If the message itself is
too large, construct the notice and remove trailing model references only if
needed to keep the notice envelope within the boundary:

```python
if _is_json_message(result.message):
    while artifacts:
        serialized = _serialize_model_tool_payload(
            artifacts=artifacts,
            message=result.message,
        )
        if len(serialized) <= _MAX_MODEL_TOOL_MESSAGE_CHARS:
            return serialized
        artifacts.pop()
    serialized = _serialize_model_tool_payload(
        artifacts=[],
        message=result.message,
    )
    if len(serialized) <= _MAX_MODEL_TOOL_MESSAGE_CHARS:
        return serialized
    notice = _structured_message_overflow_notice(
        result.message,
        artifact_available=bool(result.artifacts),
    )
    artifacts = [
        candidate
        for reference in result.artifacts[:_MAX_MODEL_ARTIFACT_REFS]
        if (candidate := _model_artifact_ref(reference)) is not None
    ]
    while len(_serialize_model_tool_payload(
        artifacts=artifacts,
        message=notice,
    )) > _MAX_MODEL_TOOL_MESSAGE_CHARS:
        artifacts.pop()
    return _serialize_model_tool_payload(artifacts=artifacts, message=notice)
```

Leave the existing binary-search truncation path only for non-JSON text.

- [ ] **Step 5: Run focused protocol and DB-RAG contract tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_epi_agent_protocol.py \
  tests/test_db_rag_agent_tools.py \
  -q
```

Expected: all tests PASS; normal maximum DB-RAG responses preserve all
evidence, hostile oversized JSON yields a parseable notice, and oversized
plain text remains bounded.

- [ ] **Step 6: Commit the structured JSON safety fix**

```bash
git add epi_agent/protocol.py tests/test_epi_agent_protocol.py tests/test_db_rag_agent_tools.py
git commit -m "fix: preserve structured tool message validity"
```

---

### Task 4: Add and Run the Real Output-Contract Smoke

**Files:**
- Create: `scripts/smoke_catalog_tool_output_contract_real.py`
- Test: `tests/test_db_rag_agent_tools.py`
- Test: `tests/test_epi_agent_protocol.py`

**Interfaces:**
- Consumes: one installer-ready NHANES archive, `OPENAI_API_KEY`, production study installation/binding, production DB-RAG registry, and production serializer.
- Produces: JSON diagnostics and exit status 0 only when grouped search and exact pagination both survive protocol serialization.

- [ ] **Step 1: Implement the dedicated real smoke CLI**

Create a Python 3.12 script with required `--nhanes-archive` using this
complete structure:

```python
"""Exercise complete catalog and inspection output contracts with real NHANES."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db_rag.config import EMBEDDING_MODEL
from db_rag.session_studies import bind_session_studies
from epi_agent.artifacts import StateArtifactStore
from epi_agent.db_rag.tools import build_db_rag_tool_registry
from epi_agent.protocol import ToolContext, serialize_tool_result
from study_package.installer import install_study_archives
from study_package.registry import discover_studies
from utils.env_loader import load_app_environment


QUERIES = [
    "age sex race ethnicity education income survey weights",
    "diabetes diagnosis insulin medication history",
    "blood pressure systolic diastolic examination",
    "body mass index waist height weight",
    "urine albumin creatinine ratio kidney disease",
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the real catalog tool output-contract smoke once."
    )
    parser.add_argument("--nhanes-archive", type=Path, required=True)
    return parser


def _elapsed_ms(start: float) -> float:
    return round((perf_counter() - start) * 1_000, 2)


def _nested_message(result) -> dict[str, object]:
    outer = json.loads(serialize_tool_result(result))
    inner = json.loads(outer["message"])
    if inner.get("code") == "MODEL_TOOL_MESSAGE_TOO_LARGE":
        raise AssertionError(f"Normal DB-RAG message overflowed: {inner}")
    return inner


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    archive = args.nhanes_archive.expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"NHANES archive not found: {archive}")

    load_app_environment(REPO_ROOT)
    api_key = str(os.environ.get("OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required for the real smoke.")

    diagnostics: dict[str, object] = {"embedding_model": EMBEDDING_MODEL}
    with tempfile.TemporaryDirectory(prefix="catalog-output-contract-smoke-") as name:
        studies_root = Path(name) / "studies"
        started = perf_counter()
        install_study_archives([archive], studies_root)
        discovered = discover_studies(studies_root)
        bound = bind_session_studies(
            discovered,
            api_key=api_key,
            expected_embedding_model=EMBEDDING_MODEL,
        )
        diagnostics["install_bind_ms"] = _elapsed_ms(started)

        readiness = bound.readiness["nhanes-2017-2018"]
        if not readiness.available:
            raise AssertionError(f"NHANES semantic binding failed: {readiness.message}")
        study = bound.studies.require("nhanes-2017-2018")
        context = ToolContext(
            study=study,
            artifact_store=StateArtifactStore(),
            thread_id="catalog-output-contract-smoke",
            policy=object(),
        )
        registry = build_db_rag_tool_registry()

        started = perf_counter()
        search_result = registry.invoke(
            "dbrag-search_catalog",
            {"queries": QUERIES, "limit": 10},
            context=context,
        )
        search_message = _nested_message(search_result)
        search_artifact = context.artifact_store.require(
            search_result.artifacts[0]
        ).content
        diagnostics["search_ms"] = _elapsed_ms(started)

        for payload in (search_message, search_artifact):
            if "hits" in payload:
                raise AssertionError("Catalog output retained flattened top-level hits.")
            probes = payload.get("probes")
            if not isinstance(probes, list) or len(probes) != len(QUERIES):
                raise AssertionError("Catalog output did not preserve five probe groups.")
            if [probe.get("query") for probe in probes] != QUERIES:
                raise AssertionError("Catalog probe order or identity changed.")
            total_hits = sum(len(probe.get("hits") or []) for probe in probes)
            if total_hits <= 25:
                raise AssertionError(f"Expected more than 25 total hits; got {total_hits}.")
            if not probes[-1].get("hits"):
                raise AssertionError("The fifth probe lost all returned evidence.")

        started = perf_counter()
        inspect_result = registry.invoke(
            "dbrag-inspect_table",
            {
                "source": "nhanes-2017-2018",
                "table": "DEMO_J",
                "offset": 0,
                "limit": 25,
            },
            context=context,
        )
        inspect_message = _nested_message(inspect_result)
        diagnostics["inspect_ms"] = _elapsed_ms(started)
        expected_page = {
            "returned_count": 25,
            "has_more": True,
            "next_offset": 25,
        }
        observed_page = {
            key: inspect_message.get(key) for key in expected_page
        }
        if observed_page != expected_page:
            raise AssertionError(
                f"Inspection pagination mismatch: expected {expected_page}, "
                f"observed {observed_page}"
            )
        fields = inspect_message.get("fields")
        if not isinstance(fields, list) or len(fields) != 25:
            raise AssertionError("Inspection did not preserve all 25 fields.")

        diagnostics["probe_count"] = len(search_message["probes"])
        diagnostics["total_hit_count"] = sum(
            len(probe["hits"]) for probe in search_message["probes"]
        )
        diagnostics["inspection"] = expected_page

    print(json.dumps(diagnostics, indent=2, sort_keys=True))
    print("catalog tool output contract smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

The implementation must satisfy these runtime steps:

1. Load the repository environment and require `OPENAI_API_KEY`.
2. Install the supplied archive into a temporary study root.
3. Discover and bind it with `bind_session_studies(..., expected_embedding_model=EMBEDDING_MODEL)`.
4. Require study `nhanes-2017-2018` and create a real `ToolContext` using
   `StateArtifactStore`, that bound `StudyBundle`, thread ID
   `catalog-output-contract-smoke`, and `policy=object()` as accepted by the
   read-only registry.
5. Invoke `dbrag-search_catalog` once with these five probes and `limit=10`:

```python
queries = [
    "age sex race ethnicity education income survey weights",
    "diabetes diagnosis insulin medication history",
    "blood pressure systolic diastolic examination",
    "body mass index waist height weight",
    "urine albumin creatinine ratio kidney disease",
]
```

6. Parse the tool message and saved artifact; assert five ordered probe groups,
   one group per input, total hits greater than 25, no top-level `hits`, and a
   nonempty fifth group.
7. Pass the result through `serialize_tool_result`, parse both outer and nested
   JSON, and assert the nested response still contains every hit and no
   `MODEL_TOOL_MESSAGE_TOO_LARGE` code.
8. Invoke `dbrag-inspect_table` for source `nhanes-2017-2018`, table `DEMO_J`,
   offset 0, limit 25. Assert `returned_count == 25`, `has_more is True`,
   `next_offset == 25`, and 25 fields.
9. Serialize and parse the inspection result through the protocol and assert
   the same pagination metadata survives.
10. Print bounded timing and count diagnostics without printing credentials,
    query vectors, or full evidence.

The code above uses `tempfile.TemporaryDirectory`, `perf_counter`, and a
`main(argv=None) -> int` entry point following
`scripts/smoke_multi_study_semantic_catalog.py`.

- [ ] **Step 2: Run focused tests before the real smoke**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_db_rag_agent_tools.py \
  tests/test_epi_agent_protocol.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run the dedicated real smoke exactly once**

Run with a hard five-minute ceiling controlled by Python's subprocess timeout,
which is available on macOS without GNU coreutils:

```bash
.venv/bin/python -c 'import subprocess; subprocess.run([".venv/bin/python", "scripts/smoke_catalog_tool_output_contract_real.py", "--nhanes-archive", "../Database/nhanes-2017-2018/delivery/nhanes-2017-2018-0.1.0.tar.gz"], check=True, timeout=300)'
```

Do not automatically rerun on failure. Preserve the traceback and printed
diagnostics.

Expected: exit 0 with `catalog tool output contract smoke passed`, five probe
groups, more than 25 total search hits, and exact inspection pagination.

- [ ] **Step 4: Run the applicable regression suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_db_rag_agent_tools.py tests/test_epi_agent_protocol.py tests/test_session_studies.py -q
```

Then run the repository's broader non-working-demo suite used by the current
branch:

```bash
.venv/bin/python -m pytest tests -q \
  --ignore=tests/test_working_demo.py \
  --ignore=tests/test_working_demo_extended.py
```

Expected: no new failures. Record exact pass, skip, deselection, failure, and
duration counts from the command output.

- [ ] **Step 5: Commit the smoke**

```bash
git add scripts/smoke_catalog_tool_output_contract_real.py
git commit -m "test: smoke complete catalog tool outputs"
```

- [ ] **Step 6: Review final scope**

Run:

```bash
git diff local-multi-study...HEAD --check
git diff local-multi-study...HEAD --stat
git log --oneline local-multi-study..HEAD
```

Confirm the diff contains no concept-resolution ledger, automatic candidate
selection, duplicate-call rejection, embedding/ranking change, or unrelated
refactor.
