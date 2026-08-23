from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import pytest

from db_rag.config import DbRagRuntimePaths
from db_rag.embedding_startup import assess_study_compatibility, initialize_embedding
from epi_agent.studies import StudyBundle, StudyRegistry


def _registry_path(
    tmp_path: Path,
    *,
    transport: str = "recording",
    dimensions: int = 3,
) -> Path:
    path = tmp_path / "embedding_models.json"
    path.write_text(
        json.dumps(
            {
                "default_profile": "test-profile",
                "profiles": [
                    {
                        "id": "test-profile",
                        "label": "Test embedding model",
                        "provider": "test-provider",
                        "transport": transport,
                        "model": "test-model",
                        "index_compatibility": "Test/test-model",
                        "dimensions": dimensions,
                        "base_url": "https://embedding.test/v1",
                        "api_key_env": "TEST_EMBEDDING_KEY",
                        "timeout_seconds": 2,
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


class RecordingEmbedder:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[object] = []

    def embed_query(self, value: object) -> object:
        self.calls.append(value)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def test_successful_startup_probe_runs_once_and_returns_hybrid_status(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    embedder = RecordingEmbedder([[0.1, 0.2, 0.3]])
    adapter_calls: list[tuple[str, str]] = []

    def adapter(profile, api_key: str):
        adapter_calls.append((profile.id, api_key))
        return embedder

    caplog.set_level(logging.INFO, logger="uvicorn.error")
    result = initialize_embedding(
        {"TEST_EMBEDDING_KEY": "secret-test-key"},
        registry_path=_registry_path(tmp_path),
        adapters={"recording": adapter},
    )

    assert result.route.available is True
    assert result.status.available is True
    assert result.status.retrieval_mode == "hybrid_vector_lexical"
    assert result.status.reason_code is None
    assert result.status.message == ""
    assert adapter_calls == [("test-profile", "secret-test-key")]
    assert embedder.calls == [["Epi Agent embedding startup probe"]]
    assert "secret-test-key" not in repr(result)
    assert "secret-test-key" not in result.status.model_dump_json()
    completion_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("Embedding startup probe completed")
    ]
    assert len(completion_records) == 1
    assert completion_records[0].name == "uvicorn.error"


def test_missing_credentials_skips_adapter_and_returns_lexical_status(
    tmp_path: Path,
) -> None:
    adapter_calls: list[object] = []

    result = initialize_embedding(
        {},
        registry_path=_registry_path(tmp_path),
        adapters={"recording": lambda *args: adapter_calls.append(args)},
    )

    assert adapter_calls == []
    assert result.route.available is False
    assert result.status.reason_code == "EMBEDDING_CREDENTIALS_MISSING"
    assert result.status.message == (
        "Semantic embedding search is unavailable. "
        "(Test embedding model is not configured.) Catalog, publication, and "
        "study-design searches will use lexical matching only."
    )


def test_unregistered_transport_is_a_soft_failure(tmp_path: Path) -> None:
    result = initialize_embedding(
        {"TEST_EMBEDDING_KEY": "secret"},
        registry_path=_registry_path(tmp_path, transport="not_registered"),
        adapters={},
    )

    assert result.status.available is False
    assert result.status.reason_code == "EMBEDDING_TRANSPORT_UNAVAILABLE"
    assert "does not have a supported transport" in result.status.message


@pytest.mark.parametrize(
    ("response", "reason_code"),
    [
        ([], "EMBEDDING_RESPONSE_INVALID"),
        ([[0.1, 0.2], [0.3, 0.4]], "EMBEDDING_RESPONSE_INVALID"),
        (["not-a-vector"], "EMBEDDING_RESPONSE_INVALID"),
        ([[0.1, True, 0.3]], "EMBEDDING_RESPONSE_INVALID"),
        ([[0.1, math.nan, 0.3]], "EMBEDDING_RESPONSE_INVALID"),
        ([[0.1, math.inf, 0.3]], "EMBEDDING_RESPONSE_INVALID"),
        ([[0.1, 0.2]], "EMBEDDING_DIMENSION_MISMATCH"),
    ],
)
def test_invalid_probe_responses_are_soft_failures(
    tmp_path: Path,
    response: object,
    reason_code: str,
) -> None:
    result = initialize_embedding(
        {"TEST_EMBEDDING_KEY": "secret"},
        registry_path=_registry_path(tmp_path),
        adapters={"recording": lambda *_args: RecordingEmbedder(response)},
    )

    assert result.status.available is False
    assert result.status.reason_code == reason_code
    assert "incompatible response" in result.status.message


@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (TimeoutError("private timeout detail"), "EMBEDDING_PROBE_TIMEOUT"),
        (RuntimeError("private provider payload"), "EMBEDDING_PROVIDER_UNAVAILABLE"),
    ],
)
def test_probe_exceptions_are_sanitized_soft_failures(
    tmp_path: Path,
    error: BaseException,
    reason_code: str,
) -> None:
    result = initialize_embedding(
        {"TEST_EMBEDDING_KEY": "secret"},
        registry_path=_registry_path(tmp_path),
        adapters={"recording": lambda *_args: RecordingEmbedder(error)},
    )

    assert result.status.reason_code == reason_code
    assert "cannot be reached" in result.status.message
    assert str(error) not in result.status.message


def test_invalid_registry_never_prevents_startup(tmp_path: Path) -> None:
    path = tmp_path / "embedding_models.json"
    path.write_text("{invalid", encoding="utf-8")

    result = initialize_embedding({}, registry_path=path, adapters={})

    assert result.route.available is False
    assert result.status.profile_label == "Configured embedding profile"
    assert result.status.reason_code == "EMBEDDING_PROFILE_INVALID"


def _study(study_id: str, label: str, embedding_model: str) -> StudyBundle:
    return StudyBundle(
        study_id=study_id,
        label=label,
        knowledge=None,
        catalog=None,
        data_sources={},
        db_rag_paths=DbRagRuntimePaths(
            duckdb_path=Path("database.duckdb"),
            catalog_path=Path("catalog.json"),
            chroma_path=Path("index"),
            embedding_model=embedding_model,
        ),
    )


def test_study_compatibility_is_independent_of_successful_transport_probe(
    tmp_path: Path,
) -> None:
    embedder = RecordingEmbedder([[0.1, 0.2, 0.3]])
    startup = initialize_embedding(
        {"TEST_EMBEDDING_KEY": "secret"},
        registry_path=_registry_path(tmp_path),
        adapters={"recording": lambda *_args: embedder},
    )
    studies = StudyRegistry(
        [
            _study("compatible", "Compatible Study", "Test/test-model"),
            _study("mismatch", "Mismatched Study", "Other/model"),
        ]
    )

    status = assess_study_compatibility(startup.status, startup.route, studies)

    assert status.available is True
    assert status.retrieval_mode == "hybrid_vector_lexical"
    assert status.compatible_study_ids == ("compatible",)
    assert status.incompatible_study_ids == ("mismatch",)
    assert "Mismatched Study" in status.message
    assert "Compatible Study" not in status.message
    assert embedder.calls == [["Epi Agent embedding startup probe"]]


def test_global_probe_failure_keeps_actual_index_compatibility_partition(
    tmp_path: Path,
) -> None:
    startup = initialize_embedding(
        {},
        registry_path=_registry_path(tmp_path),
        adapters={"recording": lambda *_args: pytest.fail("adapter must not run")},
    )
    studies = StudyRegistry(
        [
            _study("compatible", "Compatible Study", "Test/test-model"),
            _study("mismatch", "Mismatched Study", "Other/model"),
        ]
    )

    status = assess_study_compatibility(startup.status, startup.route, studies)

    assert status.available is False
    assert status.compatible_study_ids == ("compatible",)
    assert status.incompatible_study_ids == ("mismatch",)
    assert status.message == startup.status.message
