from __future__ import annotations

import json

import pandas as pd
import pytest

from api.runtime import ReportAgentApiRuntime
from api.schemas import CompletedAnalysisResult
from epi_agent.agent import build_general_system_prompt
from epi_agent.analysis_artifacts import AnalysisRun, save_analysis_run
from epi_agent.artifacts import StateArtifactStore
from epi_agent.protocol import ToolContext, ToolExecutionError
from epi_agent.runtimes.python import LocalPythonRuntime, PythonExecutionRequest
from epi_agent.runtimes.python.models import PythonRuntimeFailure
from epi_agent.studies import StudyRegistry
from epi_agent.tool_packs.analysis.python.tools import (
    CustomPythonArguments,
    RunCustomPythonTool,
    _runtime_failure,
    build_custom_python_tool_registry,
)
from epi_agent.tool_packs.analysis.review import _analysis_run, _review_payload
from tools.execution_policy import validate_generated_code


def test_custom_python_arguments_accept_standalone_execution():
    arguments = CustomPythonArguments.model_validate(
        {
            "analysis_goal": "Demonstrate a calculation",
            "code": "print(2 + 2)",
            "code_summary": "Add two integers.",
            "code_assumptions": "This example does not use participant data.",
        }
    )

    assert arguments.dataset_id is None
    assert arguments.dataset_kind is None
    assert arguments.dataset_version is None
    assert arguments.dataset_source is None
    assert arguments.dataset_source_reason is None


def test_custom_python_tool_explains_partial_dataset_metadata(tmp_path):
    registry = build_custom_python_tool_registry(
        LocalPythonRuntime(runtime_root=tmp_path),
        runtime_root=tmp_path,
    )

    with pytest.raises(ToolExecutionError) as caught:
        registry.invoke(
            "analysis-run_custom_python",
            {
                "dataset_id": "dataset-1",
                "analysis_goal": "Analyze cases",
                "code": "print(dataset.shape)",
                "code_summary": "Report the dataset shape.",
                "code_assumptions": "The selected dataset is tabular.",
            },
            context=ToolContext(
                studies=StudyRegistry(),
                artifact_store=StateArtifactStore(),
                thread_id="thread-1",
                policy=None,
            ),
        )

    assert caught.value.code == "DATASET_METADATA_INCOMPLETE"
    assert str(caught.value) == (
        "Provide all dataset metadata fields together, or omit all of them."
    )
    assert caught.value.details == {
        "required_together": [
            "dataset_id",
            "dataset_kind",
            "dataset_version",
            "dataset_source",
            "dataset_source_reason",
        ]
    }


def test_completed_standalone_result_has_no_dataset_source():
    result = CompletedAnalysisResult.model_validate(
        {
            "analysis_run_id": "run-1",
            "analysis_run_version": 1,
            "method": "custom_python",
            "python_code": "print(2 + 2)",
            "output_text": "4\n",
        }
    )

    assert result.dataset is None
    assert result.dataset_source is None
    assert result.dataset_source_reason is None


def test_api_ignores_dataset_source_without_dataset(monkeypatch):
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda *_args, **_kwargs: None,
        default_runtime_settings={"model_name": "test-model"},
        models=["test-model"],
    )
    run = AnalysisRun(
        method="custom_python",
        specification={
            "code": "print(2 + 2)",
            "dataset_source": "prior_artifact",
            "dataset_source_reason": "Inconsistent stale metadata.",
        },
        output_text="4\n",
        runtime={"language": "Python", "version": "3.12"},
    )
    monkeypatch.setattr(
        runtime,
        "_file_artifact",
        lambda *_args, **_kwargs: {
            "kind": "analysis_run",
            "content": {
                "kind": "analysis_run",
                "status": "active",
                "version": 1,
                "content": run.model_dump(mode="json"),
            },
        },
    )

    result = runtime.analysis_result("thread-1", "run-1")

    assert result.dataset is None
    assert result.dataset_source is None
    assert result.dataset_source_reason is None


def test_custom_python_tool_runs_standalone_and_records_no_dataset(tmp_path):
    store = StateArtifactStore()
    registry = build_custom_python_tool_registry(
        LocalPythonRuntime(
            runtime_root=tmp_path,
            timeout_seconds=180,
            memory_limit_bytes=None,
        ),
        runtime_root=tmp_path,
    )

    result = registry.invoke(
        "analysis-run_custom_python",
        {
            "analysis_goal": "Demonstrate a calculation",
            "code": "print(2 + 2)",
            "code_summary": "Add two integers.",
            "code_assumptions": "This example does not use participant data.",
        },
        context=ToolContext(
            studies=StudyRegistry(),
            artifact_store=store,
            thread_id="thread-1",
            policy=None,
        ),
    )

    message = json.loads(result.message)
    assert message["analysis_run_status"] == "pending_review"
    assert len(result.artifacts) == 1
    stored = store.require(result.artifacts[0])
    run = AnalysisRun.model_validate(stored.content)
    assert run.dataset is None
    assert run.output_text.strip() == "4"
    assert "dataset" not in stored.provenance


def test_custom_python_tool_preserves_exact_dataset_execution(tmp_path):
    dataset_path = tmp_path / "selected.parquet"
    pd.DataFrame({"cases": [2, 3]}).to_parquet(dataset_path, index=False)
    store = StateArtifactStore()
    store.save_dataset(
        {
            "id": "dataset-1",
            "kind": "analysis_dataset",
            "version": 1,
            "status": "active",
            "path": str(dataset_path),
            "provenance": {},
        }
    )
    registry = build_custom_python_tool_registry(
        LocalPythonRuntime(
            runtime_root=tmp_path,
            timeout_seconds=180,
            memory_limit_bytes=None,
        ),
        runtime_root=tmp_path,
    )

    result = registry.invoke(
        "analysis-run_custom_python",
        {
            "dataset_id": "dataset-1",
            "dataset_kind": "analysis_dataset",
            "dataset_version": 1,
            "dataset_source": "prior_artifact",
            "dataset_source_reason": "The selected artifact contains cases.",
            "analysis_goal": "Sum cases",
            "code": "print(int(dataset['cases'].sum()))",
            "code_summary": "Sum the cases column.",
            "code_assumptions": "Cases are additive counts.",
        },
        context=ToolContext(
            studies=StudyRegistry(),
            artifact_store=store,
            thread_id="thread-1",
            policy=None,
        ),
    )

    stored = store.require(result.artifacts[-1])
    run = AnalysisRun.model_validate(stored.content)
    assert run.dataset is not None
    assert run.dataset.id == "dataset-1"
    assert run.output_text.strip() == "5"
    assert stored.provenance["dataset"]["id"] == "dataset-1"


def test_missing_supplied_dataset_never_falls_back_to_standalone(tmp_path):
    store = StateArtifactStore()
    registry = build_custom_python_tool_registry(
        LocalPythonRuntime(runtime_root=tmp_path),
        runtime_root=tmp_path,
    )

    with pytest.raises(ToolExecutionError) as caught:
        registry.invoke(
            "analysis-run_custom_python",
            {
                "dataset_id": "missing-dataset",
                "dataset_kind": "analysis_dataset",
                "dataset_version": 1,
                "dataset_source": "prior_artifact",
                "dataset_source_reason": "Analyze the selected artifact.",
                "analysis_goal": "Count rows",
                "code": "print(len(dataset))",
                "code_summary": "Count dataset rows.",
                "code_assumptions": "Each row is one observation.",
            },
            context=ToolContext(
                studies=StudyRegistry(),
                artifact_store=store,
                thread_id="thread-1",
                policy=None,
            ),
        )

    assert caught.value.code == "DATASET_NOT_AVAILABLE"
    assert store.list_artifacts() == []


def test_standalone_analysis_run_is_reviewable():
    store = StateArtifactStore()
    context = ToolContext(
        studies=StudyRegistry(),
        artifact_store=store,
        thread_id="thread-1",
        policy=None,
    )
    reference = save_analysis_run(
        store,
        AnalysisRun(
            method="custom_python",
            specification={
                "analysis_goal": "Demonstrate a calculation",
                "code": "print(2 + 2)",
            },
            output_text="4\n",
            runtime={"language": "Python", "version": "3.12"},
        ),
        thread_id="thread-1",
        status="pending_review",
    )

    run_reference, run, linked = _analysis_run(
        {
            "analysis_run_id": reference.id,
            "analysis_run_version": reference.version,
        },
        context,
    )
    payload = _review_payload(run_reference, run, feedback_history=())

    assert linked == ()
    assert payload["view"]["dataset"] is None
    assert payload["view"]["output_text"] == "4\n"


def test_allowed_code_returns_typed_success():
    result = validate_generated_code("print(dataset.shape)")

    assert result.failure_code is None
    assert result.violations == ()


def test_invalid_syntax_is_not_a_policy_block():
    result = validate_generated_code("if True print('x')")

    assert result.failure_code == "INVALID_PYTHON_SYNTAX"
    assert result.violations == ()
    assert result.line == 1
    assert result.column is not None


@pytest.mark.parametrize(
    ("code", "rule_id"),
    [
        ("eval('1 + 1')", "PY_DYNAMIC_EXECUTION"),
        ("import pip", "PY_PACKAGE_INSTALLATION"),
        ("open('/tmp/input.csv')", "PY_FILESYSTEM_ACCESS"),
        ("import pathlib", "PY_FILESYSTEM_ACCESS"),
        ("import requests", "PY_NETWORK_ACCESS"),
        ("from urllib import request", "PY_NETWORK_ACCESS"),
        ("import subprocess", "PY_PROCESS_CONTROL"),
        ("import os", "PY_HOST_ENVIRONMENT_ACCESS"),
        ("import sys\nsys.exit(0)", "PY_PROCESS_CONTROL"),
        ("import matplotlib\nmatplotlib.use('svg')", "PY_RENDERER_OVERRIDE"),
        ("dataset.to_csv('out.csv')", "PY_FILESYSTEM_ACCESS"),
    ],
)
def test_prohibited_operations_have_stable_rule_ids(code, rule_id):
    result = validate_generated_code(code)

    assert result.failure_code == "POLICY_BLOCKED"
    assert rule_id in {item.rule_id for item in result.violations}


@pytest.mark.parametrize(
    "code",
    [
        "print(pd.read_csv('/etc/passwd'))",
        "import pandas as frame\nprint(frame.read_json('https://example.test/data'))",
        "from pandas import read_parquet as load_table\nprint(load_table('/tmp/data'))",
        "print(np.load('/tmp/data.npy'))",
        "import numpy as numbers\nprint(numbers.loadtxt('/tmp/data.txt'))",
        "from matplotlib.pyplot import imread as load_image\nprint(load_image('/tmp/image.png'))",
        "print(pd.read_csv('/etc/passwd'))\nimport statistics as pd",
        (
            "import pandas as frame\nprint(frame.read_csv('/etc/passwd'))"
            "\nimport statistics as frame"
        ),
        "from pandas import *\nprint(read_csv('/etc/passwd'))",
        "from numpy import *\nprint(load('/tmp/data.npy'))",
        "from matplotlib.pyplot import *\nprint(imread('/tmp/image.png'))",
        (
            "from pandas import read_csv as load\nprint(load('/etc/passwd'))"
            "\nimport numpy as load"
        ),
        (
            "from numpy import load as reader\nprint(reader('/tmp/data.npy'))"
            "\nimport pandas as reader"
        ),
    ],
)
def test_library_readers_are_blocked_for_known_and_imported_aliases(code):
    result = validate_generated_code(code)

    assert result.failure_code == "POLICY_BLOCKED"
    assert "PY_FILESYSTEM_ACCESS" in {
        item.rule_id for item in result.violations
    }


@pytest.mark.parametrize(
    "code",
    [
        "print(dataset.rename(columns={'old': 'new'}).head())",
        "print(dataset['value'].replace({1: 2}).head())",
        "import pandas as pd\nprint(pd.crosstab(dataset['a'], dataset['b']))",
    ],
)
def test_in_memory_operations_are_allowed(code):
    assert validate_generated_code(code).failure_code is None


def test_distinct_violations_are_sorted_and_capped():
    lines = ["import requests", "open('/tmp/x')", "eval('1')"]
    lines.extend(f"open('/tmp/{index}')" for index in range(12))

    result = validate_generated_code("\n".join(lines))

    assert result.failure_code == "POLICY_BLOCKED"
    assert len(result.violations) == 10
    assert result.truncated is True
    assert [item.line for item in result.violations] == sorted(
        item.line for item in result.violations
    )
    assert all(item.message and item.correction for item in result.violations)


@pytest.mark.parametrize(
    ("code", "expected_code"),
    [
        ("if True print('x')", "INVALID_PYTHON_SYNTAX"),
        ("open('/tmp/x')", "POLICY_BLOCKED"),
    ],
)
def test_runtime_rejects_before_worker_setup(
    monkeypatch,
    code,
    expected_code,
):
    def fail_if_created(*args, **kwargs):
        raise AssertionError("worker setup started")

    monkeypatch.setattr(
        "epi_agent.runtimes.python.local_process.tempfile.TemporaryDirectory",
        fail_if_created,
    )
    runtime = LocalPythonRuntime()
    request = PythonExecutionRequest(
        code=code,
        selected_dataset_id="dataset-1",
    )

    with pytest.raises(PythonRuntimeFailure) as caught:
        runtime.execute(
            request,
            {"dataset-1": pd.DataFrame({"x": [1]})},
        )

    assert caught.value.code == expected_code
    assert caught.value.details
    assert caught.value.execution_started is False


def test_runtime_adapter_preserves_policy_details():
    runtime_error = PythonRuntimeFailure(
        "POLICY_BLOCKED",
        "Generated Python violates the execution policy.",
        category="policy_blocked",
        recoverable=True,
        execution_started=False,
        details={
            "violations": [
                {
                    "rule_id": "PY_FILESYSTEM_ACCESS",
                    "operation": "open",
                    "message": "Generated code cannot access or modify files.",
                    "correction": "Analyze the preloaded dataset.",
                    "line": 1,
                    "column": 0,
                }
            ],
            "violations_truncated": False,
        },
    )

    tool_error = _runtime_failure(runtime_error)

    assert tool_error.details == {
        "category": "policy_blocked",
        "execution_started": False,
        "violations": [
            {
                "rule_id": "PY_FILESYSTEM_ACCESS",
                "operation": "open",
                "message": "Generated code cannot access or modify files.",
                "correction": "Analyze the preloaded dataset.",
                "line": 1,
                "column": 0,
            }
        ],
        "violations_truncated": False,
    }


def test_runtime_adapter_preserves_missing_dependency_details():
    runtime_error = PythonRuntimeFailure(
        "DEPENDENCY_NOT_AVAILABLE",
        "Package “sklearn” isn’t installed.",
        category="unsupported_runtime",
        recoverable=True,
        execution_started=True,
        details={
            "available_packages": ["numpy", "pandas", "statsmodels"],
            "missing_module": "sklearn",
        },
    )

    tool_error = _runtime_failure(runtime_error)

    assert tool_error.code == "DEPENDENCY_NOT_AVAILABLE"
    assert str(tool_error) == "Package “sklearn” isn’t installed."
    assert tool_error.recoverable is True
    assert tool_error.details == {
        "available_packages": ["numpy", "pandas", "statsmodels"],
        "category": "unsupported_runtime",
        "execution_started": True,
        "missing_module": "sklearn",
    }


def test_system_prompt_contains_python_policy():
    prompt = build_general_system_prompt(include_db_rag=False)

    assert "with or without a selected dataset" in prompt
    assert "never by matching words" in prompt
    assert "Never substitute synthetic data" in prompt
    assert "Import every library you use explicitly" in prompt
    assert "single corrected-code attempt" in prompt
    assert "scientifically equivalent" in prompt
    assert "Do not force an unsuitable workaround" in prompt
    assert "do not invent a result" in prompt.casefold()
    assert "No result was produced" in prompt
    assert "Do not read, write, or discover files" in prompt


def test_python_schema_contains_pre_call_restrictions(tmp_path):
    schema = CustomPythonArguments.model_json_schema()
    description = schema["properties"]["code"]["description"]
    registry = build_custom_python_tool_registry(
        LocalPythonRuntime(runtime_root=tmp_path),
        runtime_root=tmp_path,
    )
    model_description = registry.model_schemas()[0]["function"]["description"]

    assert "Standalone code may create data in memory" in description
    assert "dataset_id" not in schema["required"]
    assert (
        "Do not use files, network, host environment, or subprocesses"
        in description
    )
    assert "Import every library you use explicitly" in description
    assert (
        "Supply all five dataset metadata fields together or omit all five"
        in model_description
    )
    assert "Package installation is unavailable" in RunCustomPythonTool.spec.description
    assert (
        "scientifically equivalent installed alternative"
        in RunCustomPythonTool.spec.description
    )
