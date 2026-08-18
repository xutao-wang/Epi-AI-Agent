from db_rag.readiness import DbRagReadiness
import pandas as pd
import pytest

from epi_agent.runtimes.python import PythonExecutionRequest
from epi_agent.studies import StudyBundle, StudyRegistry
from graph.builder import build_graph
from utils.model_runtime_profiles import model_runtime_profile
from utils.user_storage import UserStorageLayout


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("not_configured", "available"), True),
        (("not_configured", "not_configured"), False),
    ],
)
def test_build_graph_uses_readiness_across_all_installed_studies(
    tmp_path,
    monkeypatch,
    statuses,
    expected,
) -> None:
    captured = {}
    studies = StudyRegistry(
        [
            StudyBundle(
                study_id="report-india-synthetic",
                label="RePORT",
                knowledge=None,
                catalog=None,
                data_sources={},
            ),
            StudyBundle(
                study_id="nhanes-2017-2018",
                label="NHANES",
                knowledge=None,
                catalog=None,
                data_sources={},
            ),
        ]
    )
    monkeypatch.setattr(
        "graph.builder._build_attachment_reader_service",
        lambda _llm, _root: "attachments",
    )
    monkeypatch.setattr(
        "graph.builder.sqlite3.connect",
        lambda *_args, **_kwargs: "connection",
    )
    monkeypatch.setattr(
        "graph.builder.SqliteSaver",
        lambda connection: f"checkpointer:{connection}",
    )
    monkeypatch.setattr(
        "graph.builder.LocalPythonRuntime",
        lambda **_kwargs: "python-runtime",
    )
    monkeypatch.setattr(
        "graph.builder.build_general_epi_agent_graph",
        lambda **kwargs: captured.update(kwargs) or "compiled-graph",
    )

    build_graph(
        llm="llm",
        model_profile=model_runtime_profile("gpt-5.6-terra"),
        db_path=tmp_path / "checkpoint.sqlite",
        runtime_root=tmp_path,
        studies=studies,
        db_rag_readiness_by_study={
            study_id: DbRagReadiness(
                status=status,
                message="readiness",
            )
            for study_id, status in zip(
                ("report-india-synthetic", "nhanes-2017-2018"),
                statuses,
            )
        },
    )

    assert captured["include_db_rag"] is expected


def test_build_graph_preserves_generic_registry_when_db_rag_is_not_configured(
    tmp_path,
    monkeypatch,
) -> None:
    agent_calls = []
    bundle = StudyBundle(
        study_id="cohort-alpha",
        label="Cohort Alpha",
        knowledge=None,
        catalog=None,
        data_sources={},
    )
    studies = StudyRegistry([bundle])

    monkeypatch.setattr(
        "graph.builder._build_attachment_reader_service",
        lambda _llm, _root: "attachments",
    )
    monkeypatch.setattr(
        "graph.builder.sqlite3.connect",
        lambda *_args, **_kwargs: "connection",
    )
    monkeypatch.setattr(
        "graph.builder.SqliteSaver",
        lambda connection: f"checkpointer:{connection}",
    )
    monkeypatch.setattr(
        "graph.builder.LocalPythonRuntime",
        lambda **_kwargs: "python-runtime",
    )
    monkeypatch.setattr(
        "graph.builder.build_general_epi_agent_graph",
        lambda **kwargs: agent_calls.append(kwargs) or "compiled-graph",
    )

    profile = model_runtime_profile("gpt-5.6-terra")
    result = build_graph(
        llm="llm",
        model_profile=profile,
        db_path=tmp_path / "checkpoint.sqlite",
        runtime_root=tmp_path,
        studies=studies,
        db_rag_readiness=DbRagReadiness(
            status="not_configured",
            message="DB-RAG dataset is not configured.",
        ),
    )

    assert result == "compiled-graph"
    assert agent_calls[0]["studies"] is studies
    assert "default_study_id" not in agent_calls[0]
    assert agent_calls[0]["include_db_rag"] is False
    assert agent_calls[0]["model_profile"] is profile


def test_build_graph_scopes_python_temporary_files_to_owner_thread_execution(
    tmp_path,
    monkeypatch,
) -> None:
    captured = {}
    storage = UserStorageLayout(tmp_path).thread("user-a", "thread-a")
    bundle = StudyBundle(
        study_id="cohort-alpha",
        label="Cohort Alpha",
        knowledge=None,
        catalog=None,
        data_sources={},
    )
    monkeypatch.setattr(
        "graph.builder._build_attachment_reader_service",
        lambda _llm, _root: "attachments",
    )
    monkeypatch.setattr(
        "graph.builder.sqlite3.connect",
        lambda *_args, **_kwargs: "connection",
    )
    monkeypatch.setattr(
        "graph.builder.SqliteSaver",
        lambda connection: f"checkpointer:{connection}",
    )
    monkeypatch.setattr(
        "graph.builder.build_general_epi_agent_graph",
        lambda **kwargs: captured.update(kwargs) or "compiled-graph",
    )

    build_graph(
        llm="llm",
        model_profile=model_runtime_profile("gpt-5.6-terra"),
        db_path=tmp_path / "checkpoint.sqlite",
        runtime_root=tmp_path,
        storage=storage,
        studies=StudyRegistry([bundle]),
        db_rag_readiness=DbRagReadiness(
            status="not_configured",
            message="DB-RAG dataset is not configured.",
        ),
    )

    runtime = captured["python_runtime"]
    assert runtime._runtime_root == storage.execution.resolve()
    original_temporary_directory = __import__("tempfile").TemporaryDirectory
    temporary_roots = []

    def recording_temporary_directory(*args, **kwargs):
        temporary_roots.append(kwargs.get("dir"))
        return original_temporary_directory(*args, **kwargs)

    monkeypatch.setattr(
        "epi_agent.runtimes.python.local_process.tempfile.TemporaryDirectory",
        recording_temporary_directory,
    )
    result = runtime.execute(
        PythonExecutionRequest(
            code="print(int(len(dataset)))",
            selected_dataset_id="synthetic",
        ),
        {"synthetic": pd.DataFrame({"value": [1, 2]})},
    )

    assert result.output_text == "2\n"
    assert temporary_roots == [str(storage.execution.resolve())]
