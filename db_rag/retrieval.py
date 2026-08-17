from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils.performance import timing_stage

from .concepts import DbRagConcept
from .vectorstore import OpenAIReranker


COLUMNS_PER_CONCEPT = 5
ROW_GRAIN_QUERY_K = 4
ROW_GRAIN_COLUMNS_PER_TABLE = 2
ROW_GRAIN_QUERY_TEMPLATES = (
    "{table} days since first visit visit type timepoint",
    "{table} days since first date event date collection date",
    "{table} days since first form completion timestamp completed date",
    "{table} treatment start screening date evaluation date",
)

RERANKER_FALLBACK_WARNING = "Reranking was unavailable; using similarity retrieval order."


@dataclass
class RerankedColumns:
    columns: list[dict[str, str]]
    warning: str = ""


class RetrievedColumns(list[dict[str, Any]]):
    def __init__(self, columns: list[dict[str, Any]], *, warning: str = "") -> None:
        super().__init__(columns)
        self.warning = warning


def retrieve_single_query(
    table_collection: Any,
    column_collection: Any,
    query: str,
    *,
    table_k: int = 4,
    column_k: int = 12,
    debug: bool = False,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    return retrieve_queries(
        table_collection,
        column_collection,
        [query],
        table_k=table_k,
        column_k=column_k,
        debug=debug,
    )[0]


def retrieve_queries(
    table_collection: Any,
    column_collection: Any,
    queries: list[str],
    *,
    table_k: int = 4,
    column_k: int = 12,
    debug: bool = False,
    query_embeddings: list[list[float]] | None = None,
) -> list[tuple[list[dict[str, str]], list[dict[str, str]]]]:
    if not queries:
        return []
    if query_embeddings is not None and len(query_embeddings) != len(queries):
        raise ValueError("Query embedding count does not match query count.")
    query_arguments = (
        {"query_embeddings": query_embeddings}
        if query_embeddings is not None
        else {"query_texts": queries}
    )
    with timing_stage(
        "db_rag.retrieval.table_query",
        query_count=len(queries),
        n_results=table_k,
    ):
        table_result = table_collection.query(
            **query_arguments,
            n_results=table_k,
            include=["documents", "metadatas"],
        )
    with timing_stage(
        "db_rag.retrieval.column_query",
        query_count=len(queries),
        n_results=column_k,
    ):
        column_result = column_collection.query(
            **query_arguments,
            n_results=column_k,
            include=["documents", "metadatas"],
        )

    batches: list[tuple[list[dict[str, str]], list[dict[str, str]]]] = []
    for index, query in enumerate(queries):
        table_documents = list(table_result["documents"][index])
        table_metadatas = list(table_result["metadatas"][index])
        tables: list[dict[str, str]] = []
        for document, metadata in zip(table_documents, table_metadatas):
            source = str(metadata.get("source") or metadata.get("source_id") or "")
            tables.append(
                {
                    **({"source": source} if source else {}),
                    "table": metadata["table"],
                    "text": document,
                }
            )
        column_documents = list(column_result["documents"][index])
        column_metadatas = list(column_result["metadatas"][index])
        columns: list[dict[str, str]] = []
        for document, metadata in zip(column_documents, column_metadatas):
            source = str(metadata.get("source") or metadata.get("source_id") or "")
            columns.append(
                {
                    **({"source": source} if source else {}),
                    "table": metadata["table"],
                    "column": metadata["column"],
                    "text": document,
                }
            )
        if debug:
            print(f"\nRetrieval for: {query}")
            for entry in tables:
                print(f"  table: {entry['table']}")
            for entry in columns:
                print(f"  column: {entry['table']}.{entry['column']}")
        batches.append((tables, columns))
    return batches


def rerank_columns(
    query: str,
    column_hits: list[dict[str, str]],
    *,
    reranker_model: str | None,
    top_k: int,
    debug: bool = False,
) -> RerankedColumns:
    if not column_hits:
        return RerankedColumns(columns=[])

    if not reranker_model:
        if debug:
            print("\nReranking disabled, using ChromaDB ordering")
        return RerankedColumns(columns=column_hits[:top_k])

    try:
        with timing_stage("db_rag.retrieval.rerank_columns", model=reranker_model, documents=len(column_hits)):
            reranker = OpenAIReranker(model=reranker_model)
            scores = reranker.rerank(query, [hit["text"] for hit in column_hits])
    except Exception:
        if debug:
            print(f"\n{RERANKER_FALLBACK_WARNING}")
        return RerankedColumns(columns=column_hits[:top_k], warning=RERANKER_FALLBACK_WARNING)
    scored_hits = sorted(zip(scores, column_hits), key=lambda item: item[0], reverse=True)

    if debug:
        print("\nReranker scores (after reranking):")
        for score, hit in scored_hits[:top_k]:
            print(f"  score={score:.4f}  {hit['table']}.{hit['column']}")

    return RerankedColumns(columns=[hit for _, hit in scored_hits[:top_k]])


def _lookup_table_by_name(table_collection: Any, table_name: str) -> dict[str, str]:
    normalized = str(table_name or "").strip()
    if not normalized:
        return {"table": "", "text": ""}
    get = getattr(table_collection, "get", None)
    if callable(get):
        try:
            result = get(where={"table": normalized}, include=["documents", "metadatas"])
        except Exception:
            result = {}
        documents = list(dict(result or {}).get("documents") or [])
        metadatas = list(dict(result or {}).get("metadatas") or [])
        for document, metadata in zip(documents, metadatas):
            if str(dict(metadata or {}).get("table") or "").strip() == normalized:
                return {"table": normalized, "text": str(document or normalized)}
    return {"table": normalized, "text": normalized}


def _inject_table_by_name(
    merged_tables: dict[str, dict[str, str]],
    table_collection: Any,
    table_name: str,
) -> None:
    normalized = str(table_name or "").strip()
    if not normalized or normalized in merged_tables:
        return
    merged_tables[normalized] = _lookup_table_by_name(table_collection, normalized)


def _inject_required_tables(
    merged_tables: dict[str, dict[str, str]],
    table_collection: Any,
    required_tables: list[str],
    excluded_tables: set[str] | None = None,
) -> None:
    excluded = set(excluded_tables or set())
    for table_name in required_tables:
        if table_name in excluded:
            continue
        _inject_table_by_name(merged_tables, table_collection, table_name)


def _drop_excluded_tables(
    merged_tables: dict[str, dict[str, str]],
    excluded_tables: list[str],
) -> None:
    for table_name in excluded_tables:
        merged_tables.pop(table_name, None)


def _promote_final_column_tables(
    merged_tables: dict[str, dict[str, str]],
    table_collection: Any,
    final_columns: list[dict[str, str]],
    excluded_tables: set[str],
) -> None:
    for column in final_columns:
        table_name = str(column.get("table") or "").strip()
        if not table_name or table_name in excluded_tables:
            continue
        _inject_table_by_name(merged_tables, table_collection, table_name)


def _retrieve_row_grain_columns_batched(
    column_collection: Any,
    table_names: list[str],
    *,
    row_grain_k: int = ROW_GRAIN_QUERY_K,
    columns_per_table: int = ROW_GRAIN_COLUMNS_PER_TABLE,
    excluded_tables: set[str] | None = None,
    existing_columns: set[tuple[str, str]] | None = None,
    debug: bool = False,
) -> list[dict[str, str]]:
    excluded = set(excluded_tables or set())
    existing = set(existing_columns or set())
    normalized_tables: list[str] = []
    seen_tables: set[str] = set()
    for table_name in table_names:
        normalized = str(table_name or "").strip()
        if not normalized or normalized in excluded or normalized in seen_tables:
            continue
        normalized_tables.append(normalized)
        seen_tables.add(normalized)
    if not normalized_tables:
        return []

    query_specs: list[tuple[str, str]] = []
    for table_name in normalized_tables:
        for template in ROW_GRAIN_QUERY_TEMPLATES:
            query_specs.append((table_name, template.format(table=table_name)))
    query_texts = [query for _table_name, query in query_specs]
    with timing_stage(
        "db_rag.retrieval.row_grain_column_query",
        query_count=len(query_texts),
        n_results=row_grain_k,
    ):
        result = column_collection.query(
            query_texts=query_texts,
            n_results=row_grain_k,
            include=["documents", "metadatas"],
        )

    documents_by_query = list(dict(result or {}).get("documents") or [])
    metadatas_by_query = list(dict(result or {}).get("metadatas") or [])
    candidates_by_table: dict[str, dict[tuple[str, str], dict[str, str]]] = {
        table_name: {} for table_name in normalized_tables
    }
    for index, (target_table, _query) in enumerate(query_specs):
        documents = documents_by_query[index] if index < len(documents_by_query) else []
        metadatas = metadatas_by_query[index] if index < len(metadatas_by_query) else []
        for document, metadata in zip(documents, metadatas):
            metadata_dict = dict(metadata or {})
            table_name = str(metadata_dict.get("table") or "").strip()
            column_name = str(metadata_dict.get("column") or "").strip()
            if table_name != target_table or not column_name:
                continue
            key = (table_name, column_name)
            if key in existing:
                continue
            candidates_by_table.setdefault(table_name, {}).setdefault(
                key,
                {
                    "table": table_name,
                    "column": column_name,
                    "text": f"Retrieval purpose: row_grain\n{document}",
                },
            )

    selected: list[dict[str, str]] = []
    for table_name in normalized_tables:
        candidates = list(candidates_by_table.get(table_name, {}).values())
        selected.extend(candidates[:columns_per_table])

    if debug and selected:
        print(f"\nRow-grain columns ({len(selected)}):")
        for hit in selected:
            print(f"  {hit['table']}.{hit['column']}")

    return selected


def _retrieve_subqueries_batched(
    table_collection: Any,
    column_collection: Any,
    concepts: list[DbRagConcept],
    *,
    table_k: int,
    column_k: int,
) -> list[dict[str, Any]]:
    del table_collection, table_k
    query_texts = [concept.retrieval_probe for concept in concepts]
    with timing_stage("db_rag.retrieval.column_query", query_count=len(query_texts), n_results=column_k):
        column_result = column_collection.query(
            query_texts=query_texts,
            n_results=column_k,
            include=["documents", "metadatas"],
        )

    column_documents = list(column_result.get("documents") or [])
    column_metadatas = list(column_result.get("metadatas") or [])
    results: list[dict[str, Any]] = []
    for index, concept in enumerate(concepts):
        query_column_documents = column_documents[index] if index < len(column_documents) else []
        query_column_metadatas = column_metadatas[index] if index < len(column_metadatas) else []
        columns = [
            {"table": metadata["table"], "column": metadata["column"], "text": document}
            for document, metadata in zip(query_column_documents, query_column_metadatas)
        ]
        results.append({"concept": concept, "tables": [], "columns": columns})
    return results


def _retrieve_context_records_from_subqueries(
    table_collection: Any,
    column_collection: Any,
    question: str,
    concepts: list[DbRagConcept],
    *,
    clinical_concept_ids: set[str] | None = None,
    table_k: int = 4,
    column_k: int = 12,
    columns_per_concept: int = COLUMNS_PER_CONCEPT,
    reranker_model: str | None = None,
    debug: bool = False,
    required_tables: list[str] | None = None,
    excluded_tables: list[str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    subquery_results = _retrieve_subqueries_batched(
        table_collection,
        column_collection,
        concepts,
        table_k=table_k,
        column_k=column_k,
    )
    merged_tables: dict[str, dict[str, str]] = {}
    for result in subquery_results:
        for entry in result["tables"]:
            merged_tables.setdefault(entry["table"], entry)
    excluded_table_set = set(excluded_tables or [])
    _inject_required_tables(
        merged_tables,
        table_collection,
        list(required_tables or []),
        excluded_table_set,
    )
    _drop_excluded_tables(merged_tables, list(excluded_table_set))
    if debug:
        print(f"\nMerged tables: {len(merged_tables)}")
        for entry in merged_tables.values():
            print(f"  {entry['table']}")

    merged_columns: dict[tuple[str, str], dict[str, Any]] = {}
    column_to_concepts: dict[tuple[str, str], set[str]] = {}
    for result in subquery_results:
        concept = result["concept"]
        for entry in result["columns"]:
            if entry["table"] in excluded_table_set:
                continue
            key = (entry["table"], entry["column"])
            merged_columns.setdefault(key, entry)
            column_to_concepts.setdefault(key, set()).add(concept.concept_id)
    if debug:
        print(f"Merged column candidates: {len(merged_columns)}")
        for entry in merged_columns.values():
            print(f"  {entry['table']}.{entry['column']}")

    reranked_columns = rerank_columns(
        question,
        list(merged_columns.values()),
        reranker_model=reranker_model,
        top_k=len(merged_columns),
        debug=debug,
    )
    all_reranked = reranked_columns.columns

    reserved: dict[tuple[str, str], dict[str, str]] = {}
    for concept in concepts:
        concept_hits = [
            hit
            for hit in all_reranked
            if concept.concept_id in column_to_concepts.get((hit["table"], hit["column"]), set())
        ]
        for hit in concept_hits[:columns_per_concept]:
            reserved[(hit["table"], hit["column"])] = hit

    final_columns: list[dict[str, Any]] = []
    clinical_ids = set(clinical_concept_ids or set())
    for key, hit in reserved.items():
        concept_ids = column_to_concepts.get(key, set())
        final_columns.append(
            {
                **hit,
                "clinical_concept_ids": tuple(sorted(concept_ids & clinical_ids)),
                "technical_need_ids": tuple(sorted(concept_ids - clinical_ids)),
            }
        )
    final_columns.extend(
        _retrieve_row_grain_columns_batched(
            column_collection,
            [str(column.get("table") or "") for column in final_columns],
            excluded_tables=excluded_table_set,
            existing_columns={
                (str(column.get("table") or ""), str(column.get("column") or ""))
                for column in final_columns
            },
            debug=debug,
        )
    )

    _promote_final_column_tables(merged_tables, table_collection, final_columns, excluded_table_set)
    _inject_required_tables(
        merged_tables,
        table_collection,
        list(required_tables or []),
        excluded_table_set,
    )
    _drop_excluded_tables(merged_tables, list(excluded_table_set))

    if debug:
        print(f"\nFinal columns ({len(final_columns)}) with guaranteed concept coverage:")
        for hit in final_columns:
            key = (hit["table"], hit["column"])
            source = ", ".join(sorted(column_to_concepts.get(key, set()))) or "?"
            marker = " [reserved]" if key in reserved else ""
            print(f"  {hit['table']}.{hit['column']} (from: {source}){marker}")

    return list(merged_tables.values()), RetrievedColumns(final_columns, warning=reranked_columns.warning)


def retrieve_context_records_for_probes(
    table_collection: Any,
    column_collection: Any,
    question: str,
    probes: list[str],
    *,
    table_k: int = 4,
    column_k: int = 12,
    columns_per_concept: int = COLUMNS_PER_CONCEPT,
    reranker_model: str | None = None,
    debug: bool = False,
    required_tables: list[str] | None = None,
    excluded_tables: list[str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    query_texts: list[str] = []
    seen: set[str] = set()
    for probe in list(probes or []):
        normalized = " ".join(str(probe or "").strip().split())
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        query_texts.append(normalized)
        seen.add(key)

    fallback_query = " ".join(str(question or "").strip().split())
    if not query_texts and fallback_query:
        query_texts = [fallback_query]
    if not query_texts:
        return [], []

    if debug:
        print("\nExplicit retrieval probes:")
        for query_text in query_texts:
            print(f"  -> {query_text}")

    concepts = [
        DbRagConcept(
            concept_id=f"probe-{index + 1}",
            label=query_text,
            retrieval_probe=query_text,
        )
        for index, query_text in enumerate(query_texts)
    ]
    return _retrieve_context_records_from_subqueries(
        table_collection,
        column_collection,
        fallback_query or "; ".join(query_texts),
        concepts,
        clinical_concept_ids=set(),
        table_k=table_k,
        column_k=column_k,
        columns_per_concept=columns_per_concept,
        reranker_model=reranker_model,
        debug=debug,
        required_tables=required_tables,
        excluded_tables=excluded_tables,
    )
