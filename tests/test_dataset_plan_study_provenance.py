from __future__ import annotations

import pytest
from pydantic import ValidationError

import epi_agent.artifacts as artifacts_module
from epi_agent.artifacts import (
    DatasetPlan,
    StateArtifactStore,
    StoredArtifact,
)
from epi_agent.db_rag.tools import build_db_rag_tool_registry
from epi_agent.protocol import ToolContext, ToolExecutionError
from epi_agent.studies import StudyBundle, StudyRegistry


def _load_plan(stored: StoredArtifact) -> DatasetPlan:
    loader = getattr(artifacts_module, "dataset_plan_from_artifact", None)
    assert callable(loader), "dataset plan provenance loader is missing"
    return loader(stored)


def _plan(study_id: str) -> DatasetPlan:
    return DatasetPlan(
        study_id=study_id,
        goal="Build an analysis dataset",
        concepts=[
            {
                "concept_id": "age",
                "label": "Age",
                "fields": [
                    {
                        "source": study_id,
                        "table": "DEMO",
                        "column": "AGE",
                    }
                ],
            }
        ],
    )


def test_new_dataset_plans_require_study_id() -> None:
    content = _plan("study-one").model_dump(mode="json")
    content.pop("study_id")

    with pytest.raises(ValidationError):
        DatasetPlan.model_validate(content)


def test_plan_content_and_artifact_provenance_must_match() -> None:
    store = StateArtifactStore()

    with pytest.raises(ValueError, match="STUDY_REFERENCE_MISMATCH"):
        store.save_dataset_plan(
            _plan("study-one"),
            provenance={"study_id": "study-two"},
        )


def test_plan_revision_cannot_switch_studies() -> None:
    store = StateArtifactStore()
    first = store.save_dataset_plan(
        _plan("study-one"),
        provenance={"study_id": "study-one"},
    )

    with pytest.raises(ValueError, match="STUDY_REFERENCE_MISMATCH"):
        store.save_dataset_plan(
            _plan("study-two"),
            prior_id=first.id,
            prior_version=first.version,
            provenance={"study_id": "study-two"},
        )


def test_legacy_plan_normalizes_from_unambiguous_artifact_provenance() -> None:
    content = _plan("study-one").model_dump(mode="json")
    content.pop("study_id")
    stored = StoredArtifact(
        id="plan-1",
        kind="dataset_plan",
        version=1,
        status="approved",
        content=content,
        provenance={"study_id": "study-one"},
    )

    plan = _load_plan(stored)

    assert plan.study_id == "study-one"


def test_legacy_plan_without_study_provenance_is_explicitly_stale() -> None:
    content = _plan("study-one").model_dump(mode="json")
    content.pop("study_id")
    stored = StoredArtifact(
        id="plan-1",
        kind="dataset_plan",
        version=1,
        status="approved",
        content=content,
        provenance={},
    )

    with pytest.raises(
        ValueError,
        match="ARTIFACT_STUDY_PROVENANCE_MISSING",
    ):
        _load_plan(stored)


def test_saved_plan_content_cannot_disagree_with_provenance() -> None:
    stored = StoredArtifact(
        id="plan-1",
        kind="dataset_plan",
        version=1,
        status="approved",
        content=_plan("study-one").model_dump(mode="json"),
        provenance={"study_id": "study-two"},
    )

    with pytest.raises(ValueError, match="STUDY_REFERENCE_MISMATCH"):
        _load_plan(stored)


class _Catalog:
    def field_exists(self, table: str, column: str) -> bool:
        return table == "DEMO" and column == "AGE"


def _tool_context(*study_ids: str) -> ToolContext:
    return ToolContext(
        studies=StudyRegistry(
            [
                StudyBundle(
                    study_id=study_id,
                    label=study_id,
                    knowledge=None,
                    catalog=_Catalog(),
                    data_sources={study_id: object()},
                    source_id=study_id,
                )
                for study_id in study_ids
            ]
        ),
        artifact_store=StateArtifactStore(),
        thread_id="thread-1",
        policy=object(),
    )


def test_save_plan_uses_declared_study_for_artifact_provenance() -> None:
    context = _tool_context("study-one", "study-two")

    result = build_db_rag_tool_registry().invoke(
        "dbrag-save_dataset_plan",
        {"plan": _plan("study-two").model_dump(mode="json")},
        context=context,
    )

    stored = context.artifact_store.require(result.artifacts[0])
    assert stored.content["study_id"] == "study-two"
    assert stored.provenance["study_id"] == "study-two"


def test_save_plan_rejects_a_source_from_another_study() -> None:
    context = _tool_context("study-one", "study-two")
    plan = _plan("study-one").model_dump(mode="json")
    plan["concepts"][0]["fields"][0]["source"] = "study-two"

    with pytest.raises(ToolExecutionError) as raised:
        build_db_rag_tool_registry().invoke(
            "dbrag-save_dataset_plan",
            {"plan": plan},
            context=context,
        )

    assert raised.value.code == "STUDY_REFERENCE_MISMATCH"


def test_save_plan_reports_uninstalled_declared_study() -> None:
    context = _tool_context("study-one")

    with pytest.raises(ToolExecutionError) as raised:
        build_db_rag_tool_registry().invoke(
            "dbrag-save_dataset_plan",
            {"plan": _plan("missing-study").model_dump(mode="json")},
            context=context,
        )

    assert raised.value.code == "PLAN_STUDY_UNAVAILABLE"


def test_validate_plan_derives_study_from_saved_plan_provenance() -> None:
    context = _tool_context("study-one", "study-two")
    plan_ref = context.artifact_store.save_dataset_plan(_plan("study-two"))

    result = build_db_rag_tool_registry().invoke(
        "dbrag-validate_dataset_plan",
        {"plan_id": plan_ref.id, "plan_version": plan_ref.version},
        context=context,
    )

    assert "passed runtime fact validation" in result.message.casefold()


def test_validate_plan_reports_when_its_study_is_no_longer_installed() -> None:
    store = StateArtifactStore()
    plan_ref = store.save_dataset_plan(_plan("study-one"))
    context = ToolContext(
        studies=StudyRegistry(),
        artifact_store=store,
        thread_id="thread-1",
        policy=object(),
    )

    with pytest.raises(ToolExecutionError) as raised:
        build_db_rag_tool_registry().invoke(
            "dbrag-validate_dataset_plan",
            {"plan_id": plan_ref.id, "plan_version": plan_ref.version},
            context=context,
        )

    assert raised.value.code == "PLAN_STUDY_UNAVAILABLE"
