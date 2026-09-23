from __future__ import annotations

import pandas as pd
import pytest

from epi_agent.agent import build_general_system_prompt
from epi_agent.runtimes.python import LocalPythonRuntime, PythonExecutionRequest
from epi_agent.runtimes.python.models import PythonRuntimeFailure
from epi_agent.tool_packs.analysis.python.tools import (
    CustomPythonArguments,
    RunCustomPythonTool,
    _runtime_failure,
)
from tools.execution_policy import validate_generated_code


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

    assert "preloaded dataset" in prompt
    assert "Import every library you use explicitly" in prompt
    assert "single corrected-code attempt" in prompt
    assert "scientifically equivalent" in prompt
    assert "Do not force an unsuitable workaround" in prompt
    assert "do not invent a result" in prompt.casefold()
    assert "No result was produced" in prompt
    assert "Do not read, write, or discover files" in prompt


def test_python_schema_contains_pre_call_restrictions():
    schema = CustomPythonArguments.model_json_schema()
    description = schema["properties"]["code"]["description"]

    assert (
        "Do not use files, network, host environment, or subprocesses"
        in description
    )
    assert "Import every library you use explicitly" in description
    assert "Package installation is unavailable" in RunCustomPythonTool.spec.description
    assert (
        "scientifically equivalent installed alternative"
        in RunCustomPythonTool.spec.description
    )
