from __future__ import annotations

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
