from __future__ import annotations

import json
from pathlib import Path

import chromadb
import duckdb

from db_rag.config import DbRagRuntimePaths
from db_rag.readiness import resolve_db_rag_readiness


MODEL = "OpenAI/text-embedding-3-large"


def _write_assets_with_missing_declared_key(root: Path) -> DbRagRuntimePaths:
    duckdb_path = root / "study.duckdb"
    with duckdb.connect(str(duckdb_path)) as connection:
        connection.execute(
            "CREATE TABLE participants (SUBJID VARCHAR, AGE INTEGER)"
        )
        connection.execute("INSERT INTO participants VALUES ('P001', 41)")

    catalog_path = root / "schema_catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "catalog_version": 2,
                "join_keys": {"participant_key": "MISSING_SUBJID"},
                "relationships": [],
                "tables": [
                    {
                        "table": "participants",
                        "text": "Participant records.",
                        "has_participant_key_join": True,
                    }
                ],
                "columns": [
                    {
                        "table": "participants",
                        "column": "SUBJID",
                        "text": "Participant identifier.",
                    },
                    {
                        "table": "participants",
                        "column": "MISSING_SUBJID",
                        "text": "Broken declared key.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    chroma_path = root / "index"
    client = chromadb.PersistentClient(path=str(chroma_path))
    for collection_name in ("table_summaries", "column_chunks"):
        client.create_collection(collection_name).add(
            ids=[collection_name],
            embeddings=[[1.0, 0.0]],
            documents=[collection_name],
        )
    return DbRagRuntimePaths(
        duckdb_path=duckdb_path,
        catalog_path=catalog_path,
        chroma_path=chroma_path,
        embedding_model=MODEL,
    )


def test_declared_relationship_column_missing_from_duckdb_is_not_configured(
    tmp_path: Path,
) -> None:
    paths = _write_assets_with_missing_declared_key(tmp_path)

    readiness = resolve_db_rag_readiness(paths=paths)

    assert readiness.status == "not_configured"
    assert "missing duckdb column" in readiness.message.casefold()


def test_catalog_v1_has_explicit_not_configured_message(tmp_path: Path) -> None:
    paths = _write_assets_with_missing_declared_key(tmp_path)
    catalog = json.loads(paths.catalog_path.read_text(encoding="utf-8"))
    catalog["catalog_version"] = 1
    paths.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    readiness = resolve_db_rag_readiness(paths=paths)

    assert readiness.status == "not_configured"
    assert readiness.message == (
        "DB-RAG dataset is not configured: the schema catalog must use "
        "catalog_version 2."
    )
