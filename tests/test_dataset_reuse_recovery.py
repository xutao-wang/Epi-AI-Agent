from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from db_rag.catalog import SchemaCatalog
from db_rag.service.models import ValidatedExtractionSql
from epi_agent.artifacts import DatasetPlan, PlanField, StateArtifactStore
from epi_agent.dataset_reuse import dataset_storage_content
from epi_agent.db_rag.tools import _persist_extraction_result
from epi_agent.protocol import ArtifactRef, ToolContext, ToolExecutionError
from epi_agent.studies import StudyBundle, StudyRegistry
from utils.user_storage import UserStorageLayout
from utils.dataset_artifacts import generated_dataset_persistence_journal_path


def _boundary(tmp_path: Path):
    database_path = tmp_path / "source.duckdb"
    with duckdb.connect(str(database_path)) as connection:
        connection.execute("CREATE TABLE cohort (age INTEGER)")
        connection.execute("INSERT INTO cohort VALUES (42), (55)")
    catalog = SchemaCatalog(
        {"columns": [{"table": "cohort", "column": "age", "description": "Age"}]},
        default_source_id="source",
    )
    plan = DatasetPlan(
        study_id="study",
        goal="Extract ages",
        dataset_title="Ages",
        row_definition="One row per record",
        concepts=[],
        required_fields=[PlanField(source="source", table="cohort", column="age", purpose="analysis")],
        operations=[],
    )
    store = StateArtifactStore()
    plan_ref = store.save_dataset_plan(plan)
    store.transition_artifact_status(plan_ref, expected_status="draft", status="approved")
    stored_plan = store.require(plan_ref)
    sql_artifact = SimpleNamespace(
        id="sql-1",
        version=1,
        content={"tables": ["cohort"], "origin": "test"},
        provenance={"study_id": "study"},
    )
    scope = UserStorageLayout(tmp_path / "runtime").thread("owner", "thread")
    context = ToolContext(
        studies=StudyRegistry([StudyBundle(study_id="study", label="Study", knowledge=None, catalog=catalog, data_sources={"source": SimpleNamespace(path=database_path)}, source_id="source")]),
        artifact_store=store,
        thread_id="thread",
        thread_storage=scope,
        policy=object(),
    )
    return context, stored_plan, plan, sql_artifact


def _extract(boundary):
    context, stored_plan, plan, sql_artifact = boundary
    return _persist_extraction_result(
        {"plan_id": stored_plan.id, "plan_version": stored_plan.version},
        context,
        stored_plan=stored_plan,
        plan=plan,
        sql_artifact=sql_artifact,
        validated=ValidatedExtractionSql(sql='SELECT "age" FROM cohort', sha256="unused"),
        execution=None,
    )


def test_cancelled_committed_replay_creates_one_fresh_pending_dataset(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    first = _extract(boundary)
    context = boundary[0]
    old_ref = first.artifacts[0]
    old = context.artifact_store.require(old_ref)
    old_bytes = Path(old.content["path"]).read_bytes()
    context.artifact_store.transition_artifact_status(old_ref, expected_status="pending_review", status="cancelled")

    replay = _extract(boundary)

    assert replay.artifacts[0].id == f"{old_ref.id}-retry-1"
    assert context.artifact_store.require(replay.artifacts[0]).status == "pending_review"
    assert context.artifact_store.require(old_ref).status == "cancelled"
    assert Path(old.content["path"]).read_bytes() == old_bytes


def test_active_committed_replay_reports_active_and_reuses_same_reference(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    first = _extract(boundary)
    context = boundary[0]
    context.artifact_store.activate_dataset(first.artifacts[0])

    replay = _extract(boundary)

    assert replay.artifacts[0] == first.artifacts[0]
    assert "status=active" in replay.message
    assert replay.output_artifacts == (first.artifacts[0],)


def test_dataset_storage_content_ignores_only_live_review_fields() -> None:
    original = {"id": "dataset-1", "status": "pending_review", "provenance": {"sql_id": "sql-1", "status_transitions": []}}
    changed = deepcopy(original)
    changed["status"] = "active"
    changed["provenance"]["status_transitions"] = [{"from_status": "pending_review", "to_status": "active"}]
    assert dataset_storage_content(original) == dataset_storage_content(changed)
    changed["provenance"]["sql_id"] = "sql-2"
    assert dataset_storage_content(original) != dataset_storage_content(changed)


def test_fresh_attempt_identity_is_reserved_before_sql_execution(tmp_path: Path, monkeypatch) -> None:
    boundary = _boundary(tmp_path)
    first = _extract(boundary)
    context = boundary[0]
    context.artifact_store.transition_artifact_status(first.artifacts[0], expected_status="pending_review", status="cancelled")

    def fail_execution(*args, **kwargs):
        raise duckdb.Error("current source failed")

    monkeypatch.setattr("epi_agent.db_rag.tools.execute_validated_extraction_sql", fail_execution)
    with pytest.raises(ToolExecutionError) as raised:
        _extract(boundary)
    assert raised.value.code == "SQL_REPAIR_REQUIRED"

    attempts = context.artifact_store.snapshot()["dataset_persistence_attempts"]
    retry_id = f"{first.artifacts[0].id}-retry-1"
    assert retry_id in attempts
    with pytest.raises(ToolExecutionError):
        _extract(boundary)
    assert f"{first.artifacts[0].id}-retry-2" not in context.artifact_store.snapshot()["dataset_persistence_attempts"]


def test_pending_replay_does_not_execute_sql_again(tmp_path: Path, monkeypatch) -> None:
    boundary = _boundary(tmp_path)
    first = _extract(boundary)
    monkeypatch.setattr("epi_agent.db_rag.tools.execute_validated_extraction_sql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SQL repeated")))
    replay = _extract(boundary)
    assert replay.artifacts[0] == first.artifacts[0]


def test_missing_previous_file_starts_fresh_attempt(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    first = _extract(boundary)
    context = boundary[0]
    old = context.artifact_store.require(first.artifacts[0])
    Path(old.content["path"]).unlink()

    replay = _extract(boundary)

    assert replay.artifacts[0].id == f"{first.artifacts[0].id}-retry-1"


def test_malformed_previous_journal_is_preserved_and_starts_fresh(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    first = _extract(boundary)
    context = boundary[0]
    journal = generated_dataset_persistence_journal_path(runtime_root=context.thread_storage, dataset_id=first.artifacts[0].id)
    journal.write_text("{malformed", encoding="utf-8")

    replay = _extract(boundary)

    assert replay.artifacts[0].id == f"{first.artifacts[0].id}-retry-1"
    assert journal.read_text(encoding="utf-8") == "{malformed"

@pytest.mark.parametrize('damage', ['lineage', 'provenance', 'journal_only_missing', 'staged_missing'])
def test_old_reconciliation_failures_allocate_once(tmp_path, damage):
    import json
    boundary = _boundary(tmp_path)
    first = _extract(boundary)
    context = boundary[0]
    old_id = first.artifacts[0].id
    snapshot = context.artifact_store.snapshot()
    journal = generated_dataset_persistence_journal_path(runtime_root=context.thread_storage, dataset_id=old_id)
    record = json.loads(journal.read_text())
    if damage == 'lineage':
        record['lineage']['sql_version'] = 99
        journal.write_text(json.dumps(record))
    elif damage == 'provenance':
        snapshot['datasets'][old_id]['provenance'] = 5
    elif damage == 'journal_only_missing':
        snapshot['dataset_persistence_attempts'] = {}
        snapshot['datasets'] = {}
        Path(record['expected_final_paths']['path']).unlink()
    else:
        snapshot['dataset_persistence_attempts'][old_id]['state'] = 'staged'
        snapshot['datasets'] = {}
        record['state'] = 'staged'
        journal.write_text(json.dumps(record))
        Path(record['expected_final_paths']['path']).unlink()
    context = replace(context, artifact_store=StateArtifactStore(snapshot))
    boundary = (context, *boundary[1:])
    result = _extract(boundary)
    assert result.artifacts[0].id == f'{old_id}-retry-1'
    assert _extract(boundary).artifacts == result.artifacts

@pytest.mark.parametrize('missing', ['registry', 'attempt', 'both'])
def test_verified_journal_restores_missing_state_without_extraction(tmp_path, monkeypatch, missing):
    boundary = _boundary(tmp_path)
    first = _extract(boundary)
    context = boundary[0]
    snapshot = context.artifact_store.snapshot()
    if missing in {'registry', 'both'}:
        snapshot['datasets'] = {}
    if missing in {'attempt', 'both'}:
        snapshot['dataset_persistence_attempts'] = {}
    context = replace(context, artifact_store=StateArtifactStore(snapshot))
    monkeypatch.setattr('epi_agent.db_rag.tools.execute_validated_extraction_sql', lambda *a, **k: pytest.fail('unexpected extraction'))
    result = _extract((context, *boundary[1:]))
    assert result.artifacts[0] == first.artifacts[0]
    assert context.artifact_store.require(result.artifacts[0]).status == 'pending_review'


def test_two_stale_callers_share_one_fresh_retry(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    from epi_agent.db_rag import tools
    boundary = _boundary(tmp_path)
    first = _extract(boundary)
    context = boundary[0]
    context.artifact_store.transition_artifact_status(first.artifacts[0], expected_status='pending_review', status='cancelled')
    snapshot = context.artifact_store.snapshot()
    contexts = [replace(context, artifact_store=StateArtifactStore(snapshot)) for _ in range(2)]
    barrier, counter_lock = Barrier(2), Lock()
    executions = []
    execute = tools.execute_validated_extraction_sql
    def counted(*args, **kwargs):
        with counter_lock:
            executions.append(1)
        return execute(*args, **kwargs)
    monkeypatch.setattr(tools, 'execute_validated_extraction_sql', counted)
    def call(ctx):
        barrier.wait(timeout=5)
        return _extract((ctx, *boundary[1:]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, contexts))
    assert results[0].artifacts == results[1].artifacts
    assert results[0].artifacts[0].id == first.artifacts[0].id + '-retry-1'
    assert len(executions) == 1


def _replacement_boundary(tmp_path):
    boundary = _boundary(tmp_path)
    predecessor = _extract(boundary).artifacts[0]
    context, stored_plan, plan, sql = boundary
    feedback = context.artifact_store.save_artifact(kind='dataset_review_feedback', content={
        'action': 'revise', 'feedback': 'Create a revised copy',
        'dataset_id': predecessor.id, 'dataset_version': predecessor.version,
        'plan_id': stored_plan.id, 'plan_version': stored_plan.version,
        'sql_id': sql.id, 'sql_version': sql.version,
    })
    def extract(ctx=context):
        return _persist_extraction_result(
            {'plan_id': stored_plan.id, 'plan_version': stored_plan.version,
             'predecessor_dataset_id': predecessor.id, 'predecessor_dataset_version': predecessor.version},
            ctx, stored_plan=stored_plan, plan=plan, sql_artifact=sql,
            validated=ValidatedExtractionSql(sql='SELECT "age" FROM cohort', sha256='unused'),
            execution=None,
        )
    return boundary, predecessor, feedback, extract


@pytest.mark.parametrize('damage', ['missing_predecessor', 'missing_feedback', 'cancelled_replacement'])
def test_stale_replacement_recovers_as_ordinary_creation(tmp_path, damage):
    boundary, predecessor, feedback, extract = _replacement_boundary(tmp_path)
    original = extract().artifacts[0]
    context = boundary[0]
    snapshot = context.artifact_store.snapshot()
    if damage == 'missing_predecessor':
        del snapshot['datasets'][predecessor.id]
    elif damage == 'missing_feedback':
        del snapshot['files'][feedback.id]
    else:
        context.artifact_store.transition_artifact_status(original, expected_status='pending_review', status='cancelled')
        snapshot = context.artifact_store.snapshot()
    ctx = replace(context, artifact_store=StateArtifactStore(snapshot))
    preserved = deepcopy(snapshot['datasets'])
    result = extract(ctx)
    record = ctx.artifact_store.require(result.artifacts[0])
    assert record.id != original.id
    assert record.status == 'pending_review'
    assert 'predecessor_dataset_id' not in record.provenance
    attempt = ctx.artifact_store.get_dataset_persistence_attempt(record.id)
    assert attempt['lineage']['predecessor_dataset_id'] is None
    assert 'replacement' not in attempt
    for key, old in preserved.items():
        assert ctx.artifact_store.snapshot()['datasets'][key] == old
    assert extract(ctx).artifacts == result.artifacts


def test_stale_replacement_discovers_each_exact_family_once(tmp_path, monkeypatch):
    from epi_agent.db_rag import tools

    boundary, _, feedback, extract = _replacement_boundary(tmp_path)
    extract()
    context = boundary[0]
    snapshot = context.artifact_store.snapshot()
    del snapshot["files"][feedback.id]
    ctx = replace(context, artifact_store=StateArtifactStore(snapshot))
    real_family_ids = tools.dataset_attempt_family_ids
    discovered_bases: list[str] = []

    def counted_family_ids(*args, **kwargs):
        discovered_bases.append(str(kwargs["base_dataset_id"]))
        return real_family_ids(*args, **kwargs)

    monkeypatch.setattr(tools, "dataset_attempt_family_ids", counted_family_ids)

    extract(ctx)

    assert len(discovered_bases) == 2
    assert discovered_bases[0] != discovered_bases[1]


@pytest.mark.parametrize('missing', ['registry', 'both'])
def test_committed_replacement_restores_without_repeating_supersession(tmp_path, missing):
    boundary, predecessor, feedback, extract = _replacement_boundary(tmp_path)
    original = extract().artifacts[0]
    context = boundary[0]
    snapshot = context.artifact_store.snapshot()
    old_predecessor = deepcopy(snapshot['datasets'][predecessor.id])
    del snapshot['datasets'][original.id]
    if missing == 'both':
        del snapshot['dataset_persistence_attempts'][original.id]
    ctx = replace(context, artifact_store=StateArtifactStore(snapshot))
    result = extract(ctx)
    assert result.artifacts[0] == original
    assert ctx.artifact_store.snapshot()['datasets'][predecessor.id] == old_predecessor


def test_current_storage_read_failure_does_not_allocate_retry(tmp_path, monkeypatch):
    boundary = _boundary(tmp_path)
    first = _extract(boundary)
    def fail(*args, **kwargs):
        raise PermissionError('storage inaccessible')
    monkeypatch.setattr('epi_agent.db_rag.tools.load_verified_dataset_artifact', fail)
    with pytest.raises(ToolExecutionError) as raised:
        _extract(boundary)
    assert raised.value.code == 'DATASET_PERSISTENCE_PENDING'
    assert set(boundary[0].artifact_store.snapshot()['dataset_persistence_attempts']) == {first.artifacts[0].id}


def test_committed_journal_restores_replacement_after_predecessor_lifecycle_change(tmp_path, monkeypatch):
    boundary, predecessor, feedback, extract = _replacement_boundary(tmp_path)
    original = extract().artifacts[0]
    context = boundary[0]
    snapshot = context.artifact_store.snapshot()
    snapshot['datasets'][predecessor.id]['status'] = 'cancelled'
    del snapshot['datasets'][original.id]
    del snapshot['dataset_persistence_attempts'][original.id]
    ctx = replace(context, artifact_store=StateArtifactStore(snapshot))
    monkeypatch.setattr('epi_agent.db_rag.tools.execute_validated_extraction_sql', lambda *a, **k: pytest.fail('committed extraction repeated'))
    assert extract(ctx).artifacts[0] == original
    assert ctx.artifact_store.require(predecessor).status == 'cancelled'


def test_result_failure_after_old_commit_cannot_start_new_extraction(tmp_path, monkeypatch):
    boundary = _boundary(tmp_path)
    first = _extract(boundary)
    def fail(*args, **kwargs):
        raise ValueError('response unavailable')
    monkeypatch.setattr('epi_agent.db_rag.tools._committed_attempt_result', fail)
    monkeypatch.setattr('epi_agent.db_rag.tools.execute_validated_extraction_sql', lambda *a, **k: pytest.fail('successful commit repeated'))
    with pytest.raises(ToolExecutionError) as raised:
        _extract(boundary)
    assert raised.value.code == 'DATASET_PERSISTENCE_PENDING'
    assert set(boundary[0].artifact_store.snapshot()['dataset_persistence_attempts']) == {first.artifacts[0].id}


def test_replacement_commit_survives_lagging_journal(tmp_path, monkeypatch):
    import json
    boundary, predecessor, feedback, extract = _replacement_boundary(tmp_path)
    original = extract().artifacts[0]
    journal = generated_dataset_persistence_journal_path(runtime_root=boundary[0].thread_storage, dataset_id=original.id)
    record = json.loads(journal.read_text())
    record['state'] = 'promoted'
    journal.write_text(json.dumps(record))
    monkeypatch.setattr('epi_agent.db_rag.tools.execute_validated_extraction_sql', lambda *a, **k: pytest.fail('committed replacement repeated'))
    assert extract().artifacts[0] == original
    assert json.loads(journal.read_text())['state'] == 'committed'


def test_unfinished_replacement_with_superseded_predecessor_becomes_ordinary(tmp_path, monkeypatch):
    from epi_agent.db_rag import tools
    boundary, predecessor, feedback, extract = _replacement_boundary(tmp_path)
    execute = tools.execute_validated_extraction_sql
    def fail(*args, **kwargs):
        raise duckdb.Error('source unavailable')
    monkeypatch.setattr(tools, 'execute_validated_extraction_sql', fail)
    with pytest.raises(ToolExecutionError):
        extract()
    boundary[0].artifact_store.transition_artifact_status(predecessor, expected_status='pending_review', status='superseded')
    monkeypatch.setattr(tools, 'execute_validated_extraction_sql', execute)
    result = extract()
    record = boundary[0].artifact_store.require(result.artifacts[0])
    assert record.status == 'pending_review'
    assert 'predecessor_dataset_id' not in record.provenance
    assert boundary[0].artifact_store.require(predecessor).status == 'superseded'


def test_reserved_ordinary_recovery_remains_current_after_feedback_is_restored(tmp_path):
    boundary, predecessor, feedback, extract = _replacement_boundary(tmp_path)
    original = extract().artifacts[0]
    context = boundary[0]
    snapshot = context.artifact_store.snapshot()
    saved_feedback = snapshot['files'].pop(feedback.id)
    ctx = replace(context, artifact_store=StateArtifactStore(snapshot))
    recovered = extract(ctx).artifacts[0]
    snapshot = ctx.artifact_store.snapshot()
    snapshot['files'][feedback.id] = saved_feedback
    ctx = replace(context, artifact_store=StateArtifactStore(snapshot))
    assert recovered != original
    assert extract(ctx).artifacts[0] == recovered


def test_current_hydration_failure_does_not_select_a_fresh_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    boundary = _boundary(tmp_path)
    first = _extract(boundary)
    context = boundary[0]
    snapshot = context.artifact_store.snapshot()
    snapshot["dataset_persistence_attempts"] = {}

    class HydrationFailingStore(StateArtifactStore):
        def __init__(self, artifacts):
            super().__init__(artifacts)
            self.begin_ids: list[str] = []

        def begin_dataset_persistence_attempt(self, attempt):
            self.begin_ids.append(str(attempt["dataset_id"]))
            raise ValueError("current checkpoint write failed")

    store = HydrationFailingStore(snapshot)
    context = replace(context, artifact_store=store)
    monkeypatch.setattr(
        "epi_agent.db_rag.tools.execute_validated_extraction_sql",
        lambda *args, **kwargs: pytest.fail("hydration failure started SQL"),
    )

    with pytest.raises(ToolExecutionError) as raised:
        _extract((context, *boundary[1:]))

    assert raised.value.code == "DATASET_PERSISTENCE_PENDING"
    assert store.begin_ids == [first.artifacts[0].id]


def test_programming_error_is_not_reported_as_persistence_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    boundary = _boundary(tmp_path)

    def implementation_defect(*args, **kwargs):
        raise TypeError("implementation defect")

    monkeypatch.setattr(
        "epi_agent.db_rag.tools._persist_extraction_result_locked",
        implementation_defect,
    )

    with pytest.raises(TypeError, match="implementation defect"):
        _extract(boundary)


def test_committed_replay_verifies_canonical_files_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from epi_agent.db_rag import tools
    from utils import dataset_artifacts

    boundary = _boundary(tmp_path)
    first = _extract(boundary)
    real_loader = tools.load_verified_dataset_artifact
    real_manifest_verifier = dataset_artifacts.verify_dataset_artifact_manifest
    canonical_loads: list[str] = []
    manifest_verifications = 0

    def counted_loader(*args, **kwargs):
        canonical_loads.append(str(kwargs["dataset_id"]))
        return real_loader(*args, **kwargs)

    def counted_manifest_verifier(*args, **kwargs):
        nonlocal manifest_verifications
        manifest_verifications += 1
        return real_manifest_verifier(*args, **kwargs)

    monkeypatch.setattr(tools, "load_verified_dataset_artifact", counted_loader)
    monkeypatch.setattr(
        dataset_artifacts,
        "verify_dataset_artifact_manifest",
        counted_manifest_verifier,
    )
    monkeypatch.setattr(
        tools,
        "execute_validated_extraction_sql",
        lambda *args, **kwargs: pytest.fail("committed replay repeated SQL"),
    )

    replay = _extract(boundary)

    assert replay.artifacts[0] == first.artifacts[0]
    assert canonical_loads == [first.artifacts[0].id]
    assert manifest_verifications == 1
