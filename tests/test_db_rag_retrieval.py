from __future__ import annotations

from db_rag import retrieval


class _EmbeddingRecordingCollection:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def query(
        self,
        *,
        n_results: int,
        include: list[str],
        query_embeddings=None,
        query_texts=None,
    ) -> dict[str, list[list[object]]]:
        self.calls.append(
            {
                "n_results": n_results,
                "include": include,
                "query_embeddings": query_embeddings,
                "query_texts": query_texts,
            }
        )
        count = len(query_embeddings or query_texts or [])
        return {
            "documents": [[] for _ in range(count)],
            "metadatas": [[] for _ in range(count)],
        }


def test_retrieve_queries_reuses_one_explicit_embedding_batch() -> None:
    tables = _EmbeddingRecordingCollection()
    columns = _EmbeddingRecordingCollection()
    embeddings = [[0.1, 0.2], [0.3, 0.4]]

    retrieval.retrieve_queries(
        tables,
        columns,
        ["diabetes diagnosis", "glycohemoglobin"],
        query_embeddings=embeddings,
    )

    assert tables.calls[0]["query_embeddings"] is embeddings
    assert columns.calls[0]["query_embeddings"] is embeddings
    assert tables.calls[0]["query_texts"] is None
    assert columns.calls[0]["query_texts"] is None
