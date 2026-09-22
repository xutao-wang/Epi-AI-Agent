from __future__ import annotations

import argparse

import pytest

import run_fastapi
from api.schemas import RuntimeCapabilities, RuntimeCapability
from epi_agent.analysis_artifacts import AnalysisRun, ArtifactIdentity
from epi_agent.protocol import ArtifactRef
from epi_agent.tool_packs.analysis.review import _review_payload


def test_runtime_capability_names_guarded_local_without_claiming_sandbox():
    capability = RuntimeCapabilities(
        publication_knowledge=RuntimeCapability(
            status="available",
            message="ok",
        ),
        db_rag_dataset=RuntimeCapability(
            status="not_configured",
            message="none",
        ),
    ).python_execution

    assert capability.status == "available"
    assert "Guarded Local" in capability.message
    assert "host permissions" in capability.message
    assert "sandbox" not in capability.message.casefold()


def test_review_payload_retains_guarded_local_provenance():
    run = AnalysisRun(
        method="custom_python",
        dataset=ArtifactIdentity(id="dataset-1", kind="dataset", version=1),
        specification={"analysis_goal": "Count cases"},
        output_text="5",
        runtime={
            "language": "Python",
            "version": "3.12.12",
            "packages": [],
            "execution_mode": "guarded_local",
            "security_boundary": "none",
            "secret_token": "must-not-leak",
        },
    )

    payload = _review_payload(
        ArtifactRef(id="run-1", kind="analysis_run", version=1),
        run,
        feedback_history=(),
    )

    runtime = payload["view"]["runtime"]
    assert runtime["execution_mode"] == "guarded_local"
    assert runtime["security_boundary"] == "none"
    assert "secret_token" not in runtime


def test_existing_acknowledgement_does_not_prompt(tmp_path):
    prompts = []

    run_fastapi.ensure_guarded_python_acknowledgement(
        project_root=tmp_path,
        environ={run_fastapi.GUARDED_PYTHON_ACK_ENV: "1"},
        input_fn=lambda prompt: prompts.append(prompt) or "no",
        interactive=True,
    )

    assert prompts == []


def test_interactive_acceptance_sets_and_persists_acknowledgement(tmp_path):
    environ = {}
    persisted = []

    run_fastapi.ensure_guarded_python_acknowledgement(
        project_root=tmp_path,
        environ=environ,
        input_fn=lambda _prompt: "yes",
        persist_fn=lambda root, values: persisted.append((root, values)),
        interactive=True,
    )

    assert environ[run_fastapi.GUARDED_PYTHON_ACK_ENV] == "1"
    assert persisted == [
        (tmp_path, {run_fastapi.GUARDED_PYTHON_ACK_ENV: "1"})
    ]


@pytest.mark.parametrize("answer", ["", "n", "no"])
def test_declined_acknowledgement_stops_startup(answer, tmp_path):
    with pytest.raises(
        run_fastapi.StartupConfigurationError,
        match=run_fastapi.GUARDED_PYTHON_ACK_ENV,
    ):
        run_fastapi.ensure_guarded_python_acknowledgement(
            project_root=tmp_path,
            environ={},
            input_fn=lambda _prompt: answer,
            interactive=True,
        )


@pytest.mark.parametrize("exception", [EOFError(), KeyboardInterrupt()])
def test_cancelled_acknowledgement_stops_startup(exception, tmp_path):
    def cancel(_prompt):
        raise exception

    with pytest.raises(
        run_fastapi.StartupConfigurationError,
        match=run_fastapi.GUARDED_PYTHON_ACK_ENV,
    ):
        run_fastapi.ensure_guarded_python_acknowledgement(
            project_root=tmp_path,
            environ={},
            input_fn=cancel,
            interactive=True,
        )


def test_noninteractive_startup_requires_explicit_acknowledgement(tmp_path):
    with pytest.raises(
        run_fastapi.StartupConfigurationError,
        match="local user-account permissions",
    ) as caught:
        run_fastapi.ensure_guarded_python_acknowledgement(
            project_root=tmp_path,
            environ={},
            interactive=False,
        )

    assert run_fastapi.GUARDED_PYTHON_ACK_ENV in str(caught.value)


def test_deployment_host_allows_all_interfaces_only_in_container():
    assert run_fastapi.deployment_host("127.0.0.1", {}) == "127.0.0.1"
    with pytest.raises(argparse.ArgumentTypeError):
        run_fastapi.deployment_host("0.0.0.0", {})
    assert (
        run_fastapi.deployment_host(
            "0.0.0.0",
            {"REPORT_AGENT_CONTAINER": "1"},
        )
        == "0.0.0.0"
    )
    with pytest.raises(argparse.ArgumentTypeError):
        run_fastapi.deployment_host(
            "192.168.1.20",
            {"REPORT_AGENT_CONTAINER": "1"},
        )


@pytest.mark.parametrize(
    ("environ", "local_api_keys_only"),
    [({}, True), ({"REPORT_AGENT_CONTAINER": "1"}, False)],
)
def test_container_startup_preserves_runtime_provider_credentials(
    monkeypatch,
    tmp_path,
    environ,
    local_api_keys_only,
):
    calls = []
    monkeypatch.setattr(
        run_fastapi,
        "load_app_environment",
        lambda root, **kwargs: calls.append((root, kwargs)),
    )

    run_fastapi.load_startup_environment(
        project_root=tmp_path,
        environ=environ,
    )

    assert calls == [
        (tmp_path, {"local_api_keys_only": local_api_keys_only})
    ]
