from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from epi_agent.protocol import ArtifactRef
from graph.conversation_events import store_thread_artifact


ArtifactStatus = Literal[
    "draft",
    "pending_review",
    "approved",
    "active",
    "superseded",
    "cancelled",
    "rejected",
]

FieldRole = Literal[
    "requested",
    "identifier",
    "grain",
    "filter_support",
    "linkage",
]


class StoredArtifact(BaseModel):
    id: str
    kind: str
    version: int
    status: ArtifactStatus
    content: dict[str, Any]
    provenance: dict[str, Any]


class PlanField(BaseModel):
    source: str
    table: str
    column: str
    output_column: str | None = Field(
        default=None,
        description=(
            "Unique extracted-dataset column name. Set an explicit "
            "provenance-preserving alias whenever source columns share a name."
        ),
    )
    purpose: str = ""
    roles: set[FieldRole] = Field(
        default_factory=set,
        description=(
            "Explicit semantic roles assigned by the EpiAgent from the query "
            "and schema evidence. A field can be requested, identifier, grain, "
            "filter_support, linkage, or a combination of those roles."
        ),
    )
    aggregation: Literal["avg", "min", "max", "sum", "count"] | None = Field(
        default=None,
        description=(
            "Reviewed SQL aggregate for a requested field. Required identity "
            "and grain fields cannot be aggregated."
        ),
    )

    @field_validator("output_column", mode="before")
    @classmethod
    def normalize_output_column(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value


class DatasetPlanConcept(BaseModel):
    """A requested analysis concept and its runtime field assignments."""

    model_config = ConfigDict(extra="allow")

    concept_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    retrieval_probe: str | None = None
    fields: list[PlanField] = Field(
        description=(
            "Runtime fields that implement this requested scientific concept."
        ),
        json_schema_extra={"minItems": 1},
    )


class PlanFieldReference(BaseModel):
    source: str = Field(min_length=1)
    table: str = Field(min_length=1)
    column: str = Field(min_length=1)


class PlanAggregate(BaseModel):
    field: PlanFieldReference
    function: Literal["avg", "min", "max", "sum", "count"]


class PlanReduction(BaseModel):
    """An optional SQL-generation hint for repeated records.

    Reduction hints remain typed for compatibility with the deterministic
    compiler, but their cross-field completeness is not a dataset-plan gate.
    """

    source: str = ""
    table: str = ""
    group_by: list[PlanFieldReference] = Field(default_factory=list)
    strategy: str | None = None
    order_by: PlanFieldReference | None = None
    tie_breakers: list[PlanFieldReference] = Field(default_factory=list)
    aggregates: list[PlanAggregate] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)


class PlanRelationshipKeyPair(BaseModel):
    left_column: str = Field(min_length=1)
    right_column: str = Field(min_length=1)


class PlanOperation(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(
        min_length=1,
        description="Operation type, such as select, filter, or join.",
    )
    description: str = ""
    join_type: str | None = None
    source: str | None = None
    left_table: str | None = None
    right_table: str | None = None
    key_pairs: list[PlanRelationshipKeyPair] = Field(default_factory=list)
    field_refs: list[PlanFieldReference] = Field(default_factory=list)
    relationship_artifact_id: str | None = None
    relationship_artifact_version: int | None = Field(default=None, ge=1)

    @field_validator("join_type", mode="before")
    @classmethod
    def normalize_join_type(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = " ".join(value.strip().casefold().split())
        return {
            "inner join": "inner",
            "left join": "left",
            "left outer": "left",
            "left outer join": "left",
        }.get(normalized, normalized or None)


class DatasetPlan(BaseModel):
    study_id: str = Field(min_length=1)
    goal: str
    row_definition: str = ""
    concepts: list[DatasetPlanConcept] = Field(
        description=(
            "Requested analysis variables grouped by scientific concept; "
            "outcomes, exposures, and covariates belong here."
        )
    )
    required_fields: list[PlanField] = Field(
        default_factory=list,
        description=(
            "Canonical output identifiers for the requested observational unit."
        ),
    )
    operations: list[PlanOperation] = Field(
        default_factory=list,
        description=(
            "Structured runtime operations. Plans selecting multiple tables "
            "must provide a connected set of explicit, evidence-backed join "
            "operations, with both endpoint keys included in plan fields."
        ),
    )
    filters: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Reviewed row filters. Every filter requires a description and "
            "either a canonical predicate with referenced_columns or "
            "value_constraints. Each value constraint identifies table, column, "
            "operator, and exactly one of value or values."
        ),
    )
    reductions: list[PlanReduction] = Field(default_factory=list)
    unresolved: list[dict[str, str]] = Field(default_factory=list)

    @field_validator("study_id")
    @classmethod
    def normalize_study_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("study_id must not be blank")
        return normalized


def dataset_plan_from_artifact(stored: StoredArtifact) -> DatasetPlan:
    if stored.kind != "dataset_plan":
        raise ValueError("ARTIFACT_KIND_MISMATCH: artifact is not a dataset plan")
    content = deepcopy(dict(stored.content))
    content_study_id = str(content.get("study_id") or "").strip()
    provenance_study_id = str(
        stored.provenance.get("study_id") or ""
    ).strip()
    if not provenance_study_id:
        raise ValueError(
            "ARTIFACT_STUDY_PROVENANCE_MISSING: dataset plan provenance "
            "does not identify one study"
        )
    if content_study_id and content_study_id != provenance_study_id:
        raise ValueError(
            "STUDY_REFERENCE_MISMATCH: dataset plan content and provenance "
            "identify different studies"
        )
    if not content_study_id:
        content["study_id"] = provenance_study_id
    return DatasetPlan.model_validate(content)

class StateArtifactStore:
    """Versioned artifact adapter backed by the parent state's artifact envelope."""

    def __init__(self, artifacts: dict[str, Any] | None = None) -> None:
        self._artifacts = deepcopy(dict(artifacts or {}))

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "StateArtifactStore":
        return cls(dict(state.get("artifacts") or {}))

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self._artifacts)

    def require(self, reference: ArtifactRef | str) -> StoredArtifact:
        artifact_id = reference.id if isinstance(reference, ArtifactRef) else reference
        record = dict(self._artifacts.get("files") or {}).get(artifact_id)
        if isinstance(record, dict) and isinstance(record.get("content"), dict):
            artifact = StoredArtifact.model_validate(record["content"])
        else:
            dataset = dict(self._artifacts.get("datasets") or {}).get(artifact_id)
            if not isinstance(dataset, dict):
                raise KeyError(f"Unknown artifact: {artifact_id}")
            artifact = StoredArtifact(
                id=artifact_id,
                kind=str(dataset.get("kind") or "dataset"),
                version=int(dataset.get("version") or 1),
                status=str(dataset.get("status") or "active"),
                content=deepcopy(dataset),
                provenance=dict(dataset.get("provenance") or {}),
            )

        if isinstance(reference, ArtifactRef) and (
            artifact.kind != reference.kind or artifact.version != reference.version
        ):
            raise KeyError(f"Artifact reference is no longer current: {artifact_id}")
        return artifact

    def list_artifacts(self, *, kind: str | None = None) -> list[StoredArtifact]:
        artifact_ids = [
            *dict(self._artifacts.get("files") or {}).keys(),
            *dict(self._artifacts.get("datasets") or {}).keys(),
        ]
        artifacts = [self.require(artifact_id) for artifact_id in artifact_ids]
        if kind is None:
            return artifacts
        return [artifact for artifact in artifacts if artifact.kind == kind]

    def save_dataset_plan(
        self,
        plan: DatasetPlan,
        *,
        status: ArtifactStatus = "draft",
        prior_id: str | None = None,
        prior_version: int | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        plan = DatasetPlan.model_validate(plan.model_dump(mode="json"))
        if (prior_id is None) != (prior_version is None):
            raise ValueError("prior_id and prior_version must be provided together")

        version = 1
        supplied_provenance = dict(provenance or {})
        supplied_study_id = str(
            supplied_provenance.get("study_id") or plan.study_id
        ).strip()
        if supplied_study_id != plan.study_id:
            raise ValueError(
                "STUDY_REFERENCE_MISMATCH: dataset plan content and provenance "
                "identify different studies"
            )
        artifact_provenance = {
            "producer": "db_rag",
            **supplied_provenance,
            "study_id": plan.study_id,
        }
        if prior_id is not None and prior_version is not None:
            prior = self.require(
                ArtifactRef(id=prior_id, kind="dataset_plan", version=prior_version)
            )
            prior_plan = dataset_plan_from_artifact(prior)
            if prior_plan.study_id != plan.study_id:
                raise ValueError(
                    "STUDY_REFERENCE_MISMATCH: a dataset plan revision cannot "
                    "switch studies"
                )
            version = prior.version + 1
            artifact_provenance["supersedes"] = prior.id
            self._replace_stored_artifact(prior.model_copy(update={"status": "superseded"}))

        stored = StoredArtifact(
            id="",
            kind="dataset_plan",
            version=version,
            status=status,
            content=plan.model_dump(mode="json"),
            provenance=artifact_provenance,
        )
        return self.save_artifact(
            kind=stored.kind,
            content=stored.content,
            status=stored.status,
            version=stored.version,
            provenance=stored.provenance,
            summary=f"Dataset plan version {stored.version}",
        )

    def save_artifact(
        self,
        *,
        kind: str,
        content: dict[str, Any],
        mime: str = "application/json",
        status: ArtifactStatus = "active",
        version: int = 1,
        provenance: dict[str, Any] | None = None,
        summary: str | None = None,
    ) -> ArtifactRef:
        if not kind.strip():
            raise ValueError("Artifact kind is required")
        if version < 1:
            raise ValueError("Artifact version must be positive")

        stored = StoredArtifact(
            id="",
            kind=kind,
            version=version,
            status=status,
            content=deepcopy(content),
            provenance=deepcopy(dict(provenance or {})),
        )
        updated = store_thread_artifact(
            {"artifacts": self._artifacts, "meta": {}},
            {
                "kind": stored.kind,
                "producer": str(stored.provenance.get("producer") or "db_rag"),
                "mime": mime,
                "summary": summary or f"{stored.kind} version {stored.version}",
                "status": stored.status,
                "content": stored.model_dump(mode="json"),
            },
        )
        self._artifacts = deepcopy(updated["artifacts"])
        artifact_id = next(reversed(self._artifacts["files"]))
        stored = stored.model_copy(update={"id": artifact_id})
        self._artifacts["files"][artifact_id]["content"] = stored.model_dump(mode="json")
        return ArtifactRef(id=artifact_id, kind=stored.kind, version=stored.version)

    def save_dataset(self, artifact: dict[str, Any], *, make_active: bool = False) -> ArtifactRef:
        """Keep dataset records in the existing datasets collection."""

        record = deepcopy(dict(artifact))
        artifact_id = str(record.get("id") or "").strip()
        if not artifact_id:
            raise ValueError("Dataset artifacts require an id")
        datasets = dict(self._artifacts.get("datasets") or {})
        datasets[artifact_id] = record
        self._artifacts["datasets"] = datasets
        if make_active:
            self._artifacts["active_dataset_id"] = artifact_id
        return ArtifactRef(
            id=artifact_id,
            kind=str(record.get("kind") or "dataset"),
            version=int(record.get("version") or 1),
        )

    def save_replacement_dataset(
        self,
        artifact: dict[str, Any],
        *,
        predecessor_ref: ArtifactRef,
        plan_ref: ArtifactRef,
        feedback_ref: ArtifactRef,
        provenance: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        plan = self.require(plan_ref)
        if plan.kind != "dataset_plan" or plan.status != "approved":
            raise ValueError("Replacement requires the exact approved dataset plan")
        plan_model = dataset_plan_from_artifact(plan)

        predecessor = self.require(predecessor_ref)
        if predecessor.status != "pending_review":
            raise ValueError(
                "Replacement predecessor must have status=pending_review"
            )
        predecessor_provenance = dict(
            predecessor.content.get("provenance") or {}
        )
        if (
            predecessor_provenance.get("plan_id") != plan.id
            or predecessor_provenance.get("plan_version") != plan.version
            or predecessor_provenance.get("study_id")
            != plan_model.study_id
        ):
            raise ValueError(
                "STUDY_REFERENCE_MISMATCH: replacement predecessor does not "
                "match the exact plan lineage"
            )

        feedback = self.require(feedback_ref)
        feedback_content = dict(feedback.content)
        if (
            feedback.kind != "dataset_review_feedback"
            or feedback.status != "active"
            or feedback_content.get("action") != "revise"
            or not str(feedback_content.get("feedback") or "").strip()
            or feedback_content.get("dataset_id") != predecessor.id
            or feedback_content.get("dataset_version") != predecessor.version
            or feedback_content.get("plan_id") != plan.id
            or feedback_content.get("plan_version") != plan.version
            or feedback_content.get("sql_id")
            != predecessor_provenance.get("sql_id")
            or feedback_content.get("sql_version")
            != predecessor_provenance.get("sql_version")
        ):
            raise ValueError(
                "Replacement requires an exact matching feedback audit event"
            )

        record = deepcopy(dict(artifact))
        replacement_id = str(record.get("id") or "").strip()
        replacement_provenance = dict(record.get("provenance") or {})
        if (
            not replacement_id
            or str(record.get("status") or "") != "pending_review"
            or replacement_provenance.get("plan_id") != plan.id
            or replacement_provenance.get("plan_version") != plan.version
            or replacement_provenance.get("study_id")
            != plan_model.study_id
        ):
            raise ValueError(
                "STUDY_REFERENCE_MISMATCH: replacement dataset must be "
                "pending review with exact plan lineage"
            )
        if replacement_id in dict(self._artifacts.get("datasets") or {}):
            raise ValueError(f"Replacement dataset already exists: {replacement_id}")

        transaction = StateArtifactStore(self._artifacts)
        replacement_ref = transaction.save_dataset(record, make_active=False)
        transaction.transition_artifact_status(
            predecessor_ref,
            expected_status="pending_review",
            status="superseded",
            provenance={
                **deepcopy(dict(provenance or {})),
                "feedback_event_id": feedback.id,
                "feedback_event_version": feedback.version,
                "replacement_dataset_id": replacement_ref.id,
                "replacement_dataset_version": replacement_ref.version,
            },
        )
        self._artifacts = transaction.snapshot()
        return replacement_ref

    def get_dataset_persistence_attempt(
        self,
        dataset_id: str,
    ) -> dict[str, Any] | None:
        attempt = dict(
            self._artifacts.get("dataset_persistence_attempts") or {}
        ).get(dataset_id)
        return deepcopy(attempt) if isinstance(attempt, dict) else None

    @staticmethod
    def _validate_persistence_attempt_payload(
        attempt: dict[str, Any],
    ) -> dict[str, Any]:
        record = deepcopy(dict(attempt))
        dataset_id = str(record.get("dataset_id") or "").strip()
        lineage = dict(record.get("lineage") or {})
        final_paths = dict(record.get("expected_final_paths") or {})
        staging_paths = dict(record.get("expected_staging_paths") or {})
        replacement = record.get("replacement")
        required_lineage = {
            "study_id",
            "approved_selected_columns",
            "approved_selected_tables",
            "expected_output_aliases",
            "plan_content_sha256",
            "thread_id",
            "plan_id",
            "plan_version",
            "sql_content_sha256",
            "sql_id",
            "sql_version",
            "predecessor_dataset_id",
            "predecessor_dataset_version",
        }
        path_keys = {"path", "schema_path", "metadata_path"}
        allowed_keys = {
            "dataset_id",
            "state",
            "lineage",
            "expected_final_paths",
            "expected_staging_paths",
            "replacement",
        }
        replacement_keys = {
            "predecessor_id",
            "predecessor_kind",
            "predecessor_version",
            "feedback_id",
            "feedback_kind",
            "feedback_version",
        }
        if (
            not dataset_id
            or record.get("state") != "begun"
            or not set(record).issubset(allowed_keys)
            or set(lineage) != required_lineage
            or not str(lineage.get("study_id") or "").strip()
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(lineage.get("plan_content_sha256") or ""),
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(lineage.get("sql_content_sha256") or ""),
            )
            or not isinstance(lineage.get("expected_output_aliases"), list)
            or not all(
                isinstance(value, str) and value
                for value in lineage.get("expected_output_aliases") or []
            )
            or not isinstance(lineage.get("approved_selected_tables"), list)
            or not all(
                isinstance(value, str) and value
                for value in lineage.get("approved_selected_tables") or []
            )
            or not isinstance(lineage.get("approved_selected_columns"), list)
            or not all(
                isinstance(value, dict)
                for value in lineage.get("approved_selected_columns") or []
            )
            or set(final_paths) != path_keys
            or set(staging_paths) != path_keys
            or not all(
                isinstance(value, str) and value
                for value in [*final_paths.values(), *staging_paths.values()]
            )
        ):
            raise ValueError("Invalid dataset persistence attempt")
        if replacement is not None and (
            not isinstance(replacement, dict)
            or set(replacement) != replacement_keys
        ):
            raise ValueError("Invalid replacement persistence control")
        json.dumps(record, allow_nan=False)
        return record

    def begin_dataset_persistence_attempt(
        self,
        attempt: dict[str, Any],
    ) -> dict[str, Any]:
        record = self._validate_persistence_attempt_payload(attempt)
        dataset_id = record["dataset_id"]
        existing = self.get_dataset_persistence_attempt(dataset_id)
        if existing is not None:
            if existing != record:
                raise ValueError(
                    "Dataset persistence attempt identity collision"
                )
            return existing
        attempts = dict(
            self._artifacts.get("dataset_persistence_attempts") or {}
        )
        attempts[dataset_id] = record
        self._artifacts["dataset_persistence_attempts"] = attempts
        return deepcopy(record)

    @staticmethod
    def _validate_manifest(manifest: dict[str, Any]) -> None:
        if set(manifest) != {"path", "schema_path", "metadata_path"}:
            raise ValueError("Dataset persistence manifest is incomplete")
        for value in manifest.values():
            item = dict(value or {})
            if (
                set(item) != {"sha256", "size"}
                or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""))
                or not isinstance(item.get("size"), int)
                or item["size"] < 0
            ):
                raise ValueError("Dataset persistence manifest is invalid")

    def advance_dataset_persistence_attempt(
        self,
        dataset_id: str,
        *,
        lineage: dict[str, Any],
        expected_state: str,
        state: str,
        manifest: dict[str, Any] | None = None,
        dataset: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempt = self.get_dataset_persistence_attempt(dataset_id)
        if attempt is None:
            raise KeyError(f"Unknown dataset persistence attempt: {dataset_id}")
        if dict(attempt.get("lineage") or {}) != dict(lineage):
            raise ValueError("Dataset persistence attempt lineage collision")
        transitions = {
            ("begun", "staged"),
            ("staged", "promoted"),
        }
        if attempt.get("state") == state:
            if (
                manifest is not None
                and dict(attempt.get("manifest") or {}) != dict(manifest)
            ) or (
                dataset is not None
                and dict(attempt.get("dataset") or {}) != dict(dataset)
            ):
                raise ValueError("Dataset persistence attempt state collision")
            return attempt
        if (
            attempt.get("state") != expected_state
            or (expected_state, state) not in transitions
        ):
            raise ValueError(
                "Dataset persistence attempt has an invalid state transition"
            )
        updated = deepcopy(attempt)
        updated["state"] = state
        if state == "staged":
            if manifest is None or dataset is None:
                raise ValueError(
                    "Staged persistence requires manifest and dataset metadata"
                )
            allowed_dataset_keys = {
                "id",
                "kind",
                "path",
                "schema_path",
                "metadata_path",
                "row_count",
                "column_count",
                "columns",
                "created_at",
                "provenance",
                "version",
                "status",
            }
            if not set(dataset).issubset(allowed_dataset_keys):
                raise ValueError(
                    "Persistence attempt dataset metadata contains unsupported fields"
                )
            self._validate_manifest(dict(manifest))
            updated["manifest"] = deepcopy(dict(manifest))
            updated["dataset"] = deepcopy(dict(dataset))
        json.dumps(updated, allow_nan=False)
        attempts = dict(
            self._artifacts.get("dataset_persistence_attempts") or {}
        )
        attempts[dataset_id] = updated
        self._artifacts["dataset_persistence_attempts"] = attempts
        return deepcopy(updated)

    def commit_dataset_persistence_attempt(
        self,
        dataset_id: str,
        *,
        lineage: dict[str, Any],
        artifact: dict[str, Any],
        plan_ref: ArtifactRef,
        predecessor_ref: ArtifactRef | None = None,
        feedback_ref: ArtifactRef | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        attempt = self.get_dataset_persistence_attempt(dataset_id)
        if attempt is None:
            raise KeyError(f"Unknown dataset persistence attempt: {dataset_id}")
        record = deepcopy(dict(artifact))
        if (
            dict(attempt.get("lineage") or {}) != dict(lineage)
            or dict(attempt.get("dataset") or {}) != record
            or record.get("id") != dataset_id
        ):
            raise ValueError("Dataset persistence commit lineage collision")
        if (predecessor_ref is None) != (feedback_ref is None):
            raise ValueError(
                "Replacement persistence requires predecessor and feedback together"
            )
        replacement_control = attempt.get("replacement")
        if predecessor_ref is None:
            if replacement_control is not None:
                raise ValueError(
                    "Ordinary persistence cannot commit replacement control"
                )
        elif dict(replacement_control or {}) != {
            "predecessor_id": predecessor_ref.id,
            "predecessor_kind": predecessor_ref.kind,
            "predecessor_version": predecessor_ref.version,
            "feedback_id": feedback_ref.id,
            "feedback_kind": feedback_ref.kind,
            "feedback_version": feedback_ref.version,
        }:
            raise ValueError("Replacement persistence control collision")
        plan = self.require(plan_ref)
        plan_model = dataset_plan_from_artifact(plan)
        record_provenance = dict(record.get("provenance") or {})
        if (
            plan.kind != "dataset_plan"
            or plan.status != "approved"
            or record_provenance.get("plan_id") != plan.id
            or record_provenance.get("plan_version") != plan.version
            or record_provenance.get("study_id") != plan_model.study_id
            or lineage.get("study_id") != plan_model.study_id
        ):
            raise ValueError(
                "Dataset persistence commit requires exact approved plan lineage"
            )
        if attempt.get("state") == "committed":
            existing = self.require(dataset_id)
            if existing.content != record:
                raise ValueError("Committed dataset registry collision")
            if (
                predecessor_ref is not None
                and self.require(predecessor_ref).status != "superseded"
            ):
                raise ValueError(
                    "Committed replacement predecessor is not superseded"
                )
            return ArtifactRef(
                id=existing.id,
                kind=existing.kind,
                version=existing.version,
            )
        if attempt.get("state") != "promoted":
            raise ValueError(
                "Dataset persistence commit requires state=promoted"
            )

        transaction = StateArtifactStore(self._artifacts)
        if predecessor_ref is None:
            if dataset_id in dict(
                transaction._artifacts.get("datasets") or {}
            ):
                raise ValueError("Dataset registry identity collision")
            reference = transaction.save_dataset(record, make_active=False)
        else:
            reference = transaction.save_replacement_dataset(
                record,
                predecessor_ref=predecessor_ref,
                plan_ref=plan_ref,
                feedback_ref=feedback_ref,
                provenance=provenance,
            )
        attempts = dict(
            transaction._artifacts.get("dataset_persistence_attempts") or {}
        )
        committed = deepcopy(attempt)
        committed["state"] = "committed"
        attempts[dataset_id] = committed
        transaction._artifacts["dataset_persistence_attempts"] = attempts
        self._artifacts = transaction.snapshot()
        return reference

    def activate_dataset(
        self,
        reference: ArtifactRef,
        *,
        expected_status: ArtifactStatus = "pending_review",
        provenance: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        self.transition_artifact_status(
            reference,
            expected_status=expected_status,
            status="active",
            provenance=provenance,
        )
        self._artifacts["active_dataset_id"] = reference.id
        return reference

    def transition_artifact_status(
        self,
        reference: ArtifactRef,
        *,
        expected_status: ArtifactStatus,
        status: ArtifactStatus,
        provenance: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        artifact = self.require(reference)
        if artifact.status != expected_status:
            raise ValueError(
                f"Artifact {reference.id} expected status={expected_status}, "
                f"found status={artifact.status}."
            )
        transition = {
            **deepcopy(dict(provenance or {})),
            "from_status": expected_status,
            "to_status": status,
        }
        artifact_provenance = deepcopy(artifact.provenance)
        history = list(artifact_provenance.get("status_transitions") or [])
        history.append(transition)
        artifact_provenance["status_transitions"] = history
        self._replace_stored_artifact(
            artifact.model_copy(
                update={
                    "status": status,
                    "provenance": artifact_provenance,
                }
            )
        )
        return reference

    def transition_artifact_statuses(
        self,
        references: tuple[ArtifactRef, ...],
        *,
        expected_status: ArtifactStatus,
        status: ArtifactStatus,
        provenance: dict[str, Any] | None = None,
    ) -> tuple[ArtifactRef, ...]:
        transaction = StateArtifactStore(self._artifacts)
        for reference in references:
            transaction.transition_artifact_status(
                reference,
                expected_status=expected_status,
                status=status,
                provenance=provenance,
            )
        self._artifacts = transaction.snapshot()
        return references

    def _replace_stored_artifact(self, artifact: StoredArtifact) -> None:
        files = dict(self._artifacts.get("files") or {})
        record = dict(files.get(artifact.id) or {})
        if record:
            record["status"] = artifact.status
            record["content"] = artifact.model_dump(mode="json")
            files[artifact.id] = record
            self._artifacts["files"] = files
            return

        datasets = dict(self._artifacts.get("datasets") or {})
        dataset = dict(datasets.get(artifact.id) or {})
        if dataset:
            dataset.update(
                {
                    "id": artifact.id,
                    "kind": artifact.kind,
                    "version": artifact.version,
                    "status": artifact.status,
                    "provenance": deepcopy(artifact.provenance),
                }
            )
            datasets[artifact.id] = dataset
            self._artifacts["datasets"] = datasets
            return
        raise KeyError(f"Unknown artifact: {artifact.id}")
