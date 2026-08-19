from __future__ import annotations

import io
from pathlib import Path
import zipfile

from fastapi.testclient import TestClient
from jwt.exceptions import InvalidTokenError

from api.auth import AuthenticatedUser, LOCAL_SESSION_ID, RequestIdentity
from api.deployment import DeploymentState
from api.provider_credentials import ProviderCredentialStore
from api.runtime import ThreadAlreadyRunningError, ThreadAwaitingReviewError
from api.schemas import (
    ApiThreadState,
    AttachmentUploadResult,
    DatasetPreview,
    DatasetSchemaResponse,
    ModelOption,
    RunStatus,
    RuntimeInfo,
    RuntimeOptions,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeSettings,
)
from api.conversation_history import ConversationSummary
from api.server import create_app
from utils.attachment_artifacts import AttachmentLimits
from utils.model_runtime_profiles import model_runtime_profile
from utils.provider_startup import ProviderCredentialError


class _FakeRuntime:
    def __init__(self) -> None:
        self.created_threads = 0
        self.created_thread_settings: list[dict | None] = []
        self.submitted_messages: list[tuple[str, str, list[str]]] = []
        self.submitted_models: list[str | None] = []
        self.submitted_studies: list[str | None] = []
        self.resumed_interrupts: list[tuple[str, str, dict]] = []
        self.reset_threads: list[str] = []
        self.discarded: list[tuple[str, str]] = []
        self.attachment_limits = AttachmentLimits(
            max_bytes=64,
            max_files_per_message=2,
            max_message_bytes=96,
        )
        self.released_sessions: list[tuple[str, str]] = []
        self.authorized_threads: list[str] = []
        self.state_provider_keys: list[str | None] = []
        self.submitted_provider_keys: list[str] = []
        self.resumed_provider_keys: list[str] = []

    def active_run_count(self) -> int:
        return 0

    def release_session(self, owner_user_id: str, session_id: str) -> None:
        self.released_sessions.append((owner_user_id, session_id))

    def create_thread(self, _identity, runtime_settings: dict | None = None) -> str:
        self.created_threads += 1
        self.created_thread_settings.append(runtime_settings)
        return "thread-created"

    def authorize_thread(self, _identity, thread_id: str) -> None:
        self.authorized_threads.append(thread_id)

    def list_conversations(self, _identity):
        return [
            ConversationSummary(
                thread_id="thread-1",
                title="TB analysis",
                title_source="automatic",
                model_name="gpt-5.6-terra",
                created_at="2026-07-30T00:00:00+00:00",
                updated_at="2026-07-30T00:00:00+00:00",
            )
        ]

    def rename_conversation(self, _identity, thread_id: str, title: str):
        if thread_id != "thread-1":
            return None
        return ConversationSummary(
            thread_id=thread_id,
            title=title,
            title_source="manual",
            model_name="gpt-5.6-terra",
            created_at="2026-07-30T00:00:00+00:00",
            updated_at="2026-07-30T00:01:00+00:00",
        )

    def open_conversation(self, _identity, thread_id: str):
        if thread_id != "thread-1":
            return None
        return ConversationSummary(
            thread_id=thread_id,
            title="TB analysis",
            title_source="automatic",
            model_name="gpt-5.6-terra",
            created_at="2026-07-30T00:00:00+00:00",
            updated_at="2026-07-30T00:00:00+00:00",
            last_opened_at="2026-07-30T18:24:00+00:00",
        )

    def archive_conversation(self, _identity, thread_id: str):
        if thread_id != "thread-1":
            return None
        return ConversationSummary(
            thread_id=thread_id,
            title="TB analysis",
            title_source="automatic",
            model_name="gpt-5.6-terra",
            created_at="2026-07-30T00:00:00+00:00",
            updated_at="2026-07-30T00:00:00+00:00",
            archived_at="2026-07-30T01:00:00+00:00",
        )

    def restore_conversation(self, _identity, thread_id: str):
        if thread_id != "thread-1":
            return None
        return ConversationSummary(
            thread_id=thread_id,
            title="TB analysis",
            title_source="automatic",
            model_name="gpt-5.6-terra",
            created_at="2026-07-30T00:00:00+00:00",
            updated_at="2026-07-30T01:00:00+00:00",
            archived_at=None,
        )

    def delete_conversation(self, _identity, thread_id: str) -> bool:
        return thread_id == "thread-1"

    def runtime_info(self) -> RuntimeInfo:
        return RuntimeInfo(
            model_name="gpt-5.4",
            temperature=0.1,
            top_p=0.9,
            max_steps=4,
            timeout_seconds=300,
            db_rag_embedding_model="OpenAI/text-embedding-3-large",
            db_rag_reranker_model="disabled",
        )

    def runtime_options(self) -> RuntimeOptions:
        return RuntimeOptions(
            defaults=RuntimeSettings(
                model_name="gpt-5.4",
                temperature=0.1,
                top_p=0.9,
                max_steps=4,
                timeout_seconds=300,
                db_rag_embedding_model="OpenAI/text-embedding-3-large",
                db_rag_reranker_model="disabled",
            ),
            models=[
                ModelOption(**model_runtime_profile(model_id).descriptor())
                for model_id in ("gpt-5.4", "gpt-5.6-luna")
            ],
            capabilities=RuntimeCapabilities(
                publication_knowledge=RuntimeCapability(
                    status="available",
                    message="Publication knowledge is available.",
                ),
                db_rag_dataset=RuntimeCapability(
                    status="not_configured",
                    message="DB-RAG dataset is not configured.",
                ),
            ),
        )

    def state(
        self,
        _identity,
        thread_id: str,
        *,
        provider_api_key: str | None = None,
    ) -> ApiThreadState:
        self.state_provider_keys.append(provider_api_key)
        return ApiThreadState(
            thread_id=thread_id,
            run=RunStatus(state="idle"),
            conversation=[],
            active_interrupt=None,
            datasets=[],
            output={"message_count": len(self.submitted_messages)},
            diagnostics={"resumed_count": len(self.resumed_interrupts)},
        )

    def submit_message(
        self,
        _identity,
        thread_id: str,
        text: str,
        attachment_ids: list[str],
        model_name: str | None = None,
        *,
        provider_api_key: str,
    ) -> None:
        if text == "duplicate":
            raise ThreadAlreadyRunningError(thread_id)
        if text == "awaiting-review":
            raise ThreadAwaitingReviewError(thread_id)
        self.submitted_messages.append((thread_id, text, attachment_ids))
        self.submitted_models.append(model_name)
        self.submitted_provider_keys.append(provider_api_key)

    def resume_interrupt(
        self,
        _identity,
        thread_id: str,
        interrupt_id: str,
        payload: dict,
        *,
        provider_api_key: str,
    ) -> None:
        if payload.get("feedback") == "duplicate":
            raise ThreadAlreadyRunningError(thread_id)
        self.resumed_interrupts.append((thread_id, interrupt_id, payload))
        self.resumed_provider_keys.append(provider_api_key)

    def reset(self, _identity, thread_id: str) -> str:
        self.reset_threads.append(thread_id)
        return f"{thread_id}-reset"

    def export_thread(self, _identity, thread_id: str) -> dict:
        return {
            "thread_id": thread_id,
            "conversation": [],
            "output": {"qa_response": "The database contains 17 rows."},
            "diagnostics": {"resumed_count": len(self.resumed_interrupts)},
            "datasets": [],
            "file_artifacts": [],
            "active_interrupt": None,
            "run": {"state": "idle", "steps": 0, "error": None},
        }

    def export_thread_archive(self, _identity, thread_id: str) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w") as archive:
            archive.writestr("thread.json", f'{{"thread_id": "{thread_id}"}}')
        return buffer.getvalue()

    def stage_attachments(
        self,
        _identity,
        thread_id: str,
        uploads: list[tuple[str, str, bytes]],
    ) -> AttachmentUploadResult:
        self.uploaded = (thread_id, uploads)
        return AttachmentUploadResult(
            attachments=[
                {
                    "id": f"attachment-{index}",
                    "filename": filename,
                    "kind": "tabular" if filename.endswith(".csv") else "structured",
                    "mime": mime,
                    "byte_size": len(content),
                    "status": "staged",
                }
                for index, (filename, mime, content) in enumerate(uploads, start=1)
            ]
        )

    def discard_staged_attachment(
        self,
        _identity,
        thread_id: str,
        attachment_id: str,
    ) -> None:
        self.discarded.append((thread_id, attachment_id))

    def conversation_attachment_bytes(self, _identity, thread_id: str, attachment_id: str):
        return type(
            "ArtifactBytes",
            (),
            {
                "content": b"id,age\n1,42\n",
                "mime": "text/csv",
                "filename": "cohort.csv",
            },
        )()

    def dataset_preview(
        self,
        _identity,
        thread_id: str,
        dataset_id: str,
        *,
        limit: int = 100,
    ) -> DatasetPreview:
        return DatasetPreview(
            dataset_id=dataset_id,
            columns=["person_id", "condition"],
            rows=[{"person_id": 1, "condition": "diabetes"}][:limit],
            row_count=2,
        )

    def dataset_schema(self, _identity, thread_id: str, dataset_id: str) -> DatasetSchemaResponse:
        return DatasetSchemaResponse(
            dataset_id=dataset_id,
            schema_={"person_id": {"dataType": "integer"}},
        )

    def dataset_csv_bytes(self, _identity, thread_id: str, dataset_id: str) -> bytes:
        return b"person_id,condition\n1,diabetes\n"

    def file_artifact_bytes(self, _identity, thread_id: str, artifact_id: str):
        return type(
            "ArtifactBytes",
            (),
            {
                "content": b"plot-bytes",
                "mime": "image/png",
                "filename": f"{artifact_id}.png",
            },
        )()

    def table_preview(
        self,
        _identity,
        thread_id: str,
        artifact_id: str,
        *,
        limit: int = 100,
    ) -> dict:
        return {
            "columns": ["group", "n"],
            "rows": [{"group": "Good", "n": "10"}][:limit],
            "row_count": 1,
        }


def _client(
    runtime: _FakeRuntime,
    *,
    with_provider_key: bool = True,
) -> TestClient:
    credential_store = ProviderCredentialStore()
    if with_provider_key:
        credential_store.put(
            RequestIdentity(
                user=AuthenticatedUser(owner_user_id="local-user"),
                session_id=LOCAL_SESSION_ID,
            ),
            "session-provider-key",
        )
    return TestClient(
        create_app(runtime=runtime, credential_store=credential_store),
        headers={"X-Epi-Session-ID": LOCAL_SESSION_ID},
    )


class _RecordingProviderKeyValidator:
    def __init__(self, error: ProviderCredentialError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def validate(self, provider: str, api_key: str) -> None:
        self.calls.append((provider, api_key))
        if self.error is not None:
            raise self.error


class _RejectingTokenVerifier:
    def verify(self, _authorization: str | None) -> AuthenticatedUser:
        raise InvalidTokenError("expired access token")


def test_request_prunes_expired_credentials_before_authentication() -> None:
    runtime = _FakeRuntime()
    clocks = {"monotonic": 10.0, "wall": 100.0}
    identity = RequestIdentity(
        user=AuthenticatedUser(
            owner_user_id="user-a",
            token_expires_at_epoch=101,
        ),
        session_id="11111111-1111-4111-8111-111111111111",
    )
    store = ProviderCredentialStore(
        monotonic_clock=lambda: clocks["monotonic"],
        wall_clock=lambda: clocks["wall"],
        on_expire=runtime.release_session,
    )
    store.put(identity, "expired-session-key")
    clocks["wall"] = 101.0
    client = TestClient(
        create_app(
            runtime=runtime,
            credential_store=store,
            token_verifier=_RejectingTokenVerifier(),
        ),
        headers={
            "Authorization": "Bearer expired-token",
            "X-Epi-Session-ID": identity.session_id,
        },
    )

    response = client.get("/api/session/provider-key")

    assert response.status_code == 401
    assert runtime.released_sessions == [("user-a", identity.session_id)]
    assert store.prune_expired() == 0


def test_provider_key_routes_report_status_store_after_validation_and_clear_session() -> None:
    runtime = _FakeRuntime()
    store = ProviderCredentialStore()
    validator = _RecordingProviderKeyValidator()
    client = TestClient(
        create_app(
            runtime=runtime,
            credential_store=store,
            provider_key_validator=validator,
        ),
        headers={"X-Epi-Session-ID": LOCAL_SESSION_ID},
    )

    before = client.get("/api/session/provider-key")
    configured = client.put(
        "/api/session/provider-key",
        json={"api_key": "  example-user-key  "},
    )
    after = client.get("/api/session/provider-key")

    assert before.status_code == 200
    assert before.json() == {"configured": False}
    assert configured.status_code == 200
    assert configured.json() == {"configured": True}
    assert "example-user-key" not in configured.text
    assert validator.calls == [("openai", "example-user-key")]
    assert runtime.released_sessions == [("local-user", LOCAL_SESSION_ID)]
    assert after.json() == {"configured": True}

    cleared = client.delete("/api/session/provider-key")

    assert cleared.status_code == 204
    assert client.get("/api/session/provider-key").json() == {"configured": False}
    assert runtime.released_sessions == [
        ("local-user", LOCAL_SESSION_ID),
        ("local-user", LOCAL_SESSION_ID),
    ]


def test_replacing_provider_key_releases_session_after_each_success() -> None:
    runtime = _FakeRuntime()
    store = ProviderCredentialStore()
    validator = _RecordingProviderKeyValidator()
    client = TestClient(
        create_app(
            runtime=runtime,
            credential_store=store,
            provider_key_validator=validator,
        ),
        headers={"X-Epi-Session-ID": LOCAL_SESSION_ID},
    )

    first = client.put(
        "/api/session/provider-key",
        json={"api_key": "first-user-key"},
    )
    replacement = client.put(
        "/api/session/provider-key",
        json={"api_key": "replacement-user-key"},
    )

    assert first.status_code == 200
    assert replacement.status_code == 200
    assert runtime.released_sessions == [
        ("local-user", LOCAL_SESSION_ID),
        ("local-user", LOCAL_SESSION_ID),
    ]
    assert validator.calls == [
        ("openai", "first-user-key"),
        ("openai", "replacement-user-key"),
    ]


def test_provider_key_validation_failure_does_not_store_or_echo_submitted_key() -> None:
    runtime = _FakeRuntime()
    rejected_key = "submitted-secret"
    raw_kind = f"validator-kind-{rejected_key}"
    store = ProviderCredentialStore()
    validator = _RecordingProviderKeyValidator(
        ProviderCredentialError(raw_kind, "The provider rejected this key.")
    )
    client = TestClient(
        create_app(
            runtime=runtime,
            credential_store=store,
            provider_key_validator=validator,
        ),
        headers={"X-Epi-Session-ID": LOCAL_SESSION_ID},
    )

    response = client.put(
        "/api/session/provider-key",
        json={"api_key": rejected_key},
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "kind": "PROVIDER_KEY_INVALID",
            "message": "The provider rejected this key.",
        }
    }
    assert rejected_key not in response.text
    assert raw_kind not in response.text
    assert store.has(
        type("Identity", (), {"owner_user_id": "local-user", "session_id": LOCAL_SESSION_ID})()
    ) is False


def test_provider_key_schema_rejection_never_echoes_submitted_key() -> None:
    submitted_key = "very-long-submitted-secret-" * 200
    client = TestClient(
        create_app(runtime=_FakeRuntime()),
        headers={"X-Epi-Session-ID": LOCAL_SESSION_ID},
    )

    response = client.put(
        "/api/session/provider-key",
        json={"api_key": submitted_key},
    )

    assert response.status_code == 422
    assert submitted_key not in response.text


def test_provider_key_routes_require_a_valid_request_identity() -> None:
    client = TestClient(create_app(runtime=_FakeRuntime()))

    assert client.get("/api/session/provider-key").status_code == 400
    assert client.put(
        "/api/session/provider-key",
        json={"api_key": "example-user-key"},
    ).status_code == 400
    assert client.delete("/api/session/provider-key").status_code == 400


def test_health_route_returns_ok_without_touching_runtime() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert runtime.created_threads == 0


def test_deployment_status_routes_and_maintenance_drain(tmp_path: Path) -> None:
    sentinel = tmp_path / "maintenance"
    deployment_state = DeploymentState(
        maintenance_file=sentinel,
        release_id="release-1",
    )
    runtime = _FakeRuntime()
    anonymous_client = TestClient(
        create_app(runtime=runtime, deployment_state=deployment_state),
    )
    client = TestClient(
        create_app(runtime=runtime, deployment_state=deployment_state),
        headers={"X-Epi-Session-ID": LOCAL_SESSION_ID},
    )

    assert anonymous_client.get("/api/health").json() == {"status": "ok"}
    assert anonymous_client.get("/api/readiness").json() == {
        "status": "ready",
        "release_id": "release-1",
    }
    assert anonymous_client.get("/api/ops/deployment-status").json() == {
        "status": "ready",
        "release_id": "release-1",
        "maintenance": False,
        "active_runs": 0,
    }

    sentinel.touch()

    readiness = anonymous_client.get("/api/readiness")
    operations = anonymous_client.get("/api/ops/deployment-status")
    history = client.get("/api/conversations")
    created = client.post("/api/threads")

    assert readiness.status_code == 503
    assert readiness.json() == {"status": "maintenance", "release_id": "release-1"}
    assert operations.status_code == 200
    assert operations.json() == {
        "status": "maintenance",
        "release_id": "release-1",
        "maintenance": True,
        "active_runs": 0,
    }
    assert history.status_code == 200
    assert created.status_code == 503
    assert created.json() == {"detail": {"code": "DEPLOYMENT_MAINTENANCE"}}
    assert created.headers["Retry-After"] == "30"


def test_maintenance_rejects_unsafe_requests_before_credential_pruning(
    tmp_path: Path,
) -> None:
    class _FailingPruneCredentialStore(ProviderCredentialStore):
        def prune_expired(self) -> int:
            raise AssertionError("credential pruning should not run")

    sentinel = tmp_path / "maintenance"
    sentinel.touch()
    client = TestClient(
        create_app(
            runtime=_FakeRuntime(),
            credential_store=_FailingPruneCredentialStore(),
            deployment_state=DeploymentState(
                maintenance_file=sentinel,
                release_id="release-1",
            ),
        ),
        headers={"X-Epi-Session-ID": LOCAL_SESSION_ID},
        raise_server_exceptions=False,
    )

    response = client.post("/api/threads")

    assert response.status_code == 503


def test_static_frontend_serves_index_and_assets(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text(
        '<div id="root"></div><script src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets_dir / "app.js").write_text("console.log('report-agent')", encoding="utf-8")
    client = TestClient(
        create_app(runtime=_FakeRuntime(), static_dir=static_dir),
        headers={"X-Epi-Session-ID": LOCAL_SESSION_ID},
    )

    index_response = client.get("/")
    asset_response = client.get("/assets/app.js")
    route_response = client.get("/analysis/thread-1")

    assert index_response.status_code == 200
    assert '<div id="root"></div>' in index_response.text
    assert asset_response.status_code == 200
    assert "report-agent" in asset_response.text
    assert route_response.status_code == 200
    assert '<script src="/assets/app.js"></script>' in route_response.text


def test_runtime_options_route_returns_backend_supported_choices() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.get("/api/runtime/options")

    assert response.status_code == 200
    assert response.json() == {
        "defaults": {
            "model_name": "gpt-5.4",
            "temperature": 0.1,
            "top_p": 0.9,
            "max_steps": 4,
            "timeout_seconds": 300.0,
            "db_rag_embedding_model": "OpenAI/text-embedding-3-large",
            "db_rag_reranker_model": "disabled",
        },
        "models": [
            model_runtime_profile("gpt-5.4").descriptor(),
            model_runtime_profile("gpt-5.6-luna").descriptor(),
        ],
        "capabilities": {
            "publication_knowledge": {
                "status": "available",
                "message": "Publication knowledge is available.",
            },
            "db_rag_dataset": {
                "status": "not_configured",
                "message": "DB-RAG dataset is not configured.",
            },
            "study_design": {
                "status": "available",
                "message": "Study design knowledge is available.",
            },
        },
    }


def test_create_thread_forwards_model_name_payload() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.post(
        "/api/threads",
        json={"model_name": "gpt-5.6-luna"},
    )

    assert response.status_code == 200
    assert response.json() == {"thread_id": "thread-created"}
    assert runtime.created_thread_settings == [
        {"model_name": "gpt-5.6-luna"}
    ]


def test_create_thread_without_model_uses_configured_default() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.post(
        "/api/threads",
        json={},
    )

    assert response.status_code == 200
    assert runtime.created_thread_settings == [None]


def test_create_thread_rejects_unknown_settings_field() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.post(
        "/api/threads",
        json={"model_name": "gpt-5.4", "temperature": 0.2},
    )

    assert response.status_code == 422
    assert runtime.created_thread_settings == []


def test_create_thread_and_submit_message_forwards_text_and_returns_state() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    create_response = client.post("/api/threads")
    submit_response = client.post(
        "/api/threads/thread-created/messages",
        json={"text": "Create a diabetes cohort"},
    )

    assert create_response.status_code == 200
    assert create_response.json() == {"thread_id": "thread-created"}
    assert runtime.created_threads == 1
    assert submit_response.status_code == 200
    assert runtime.submitted_messages == [
        ("thread-created", "Create a diabetes cohort", [])
    ]
    assert submit_response.json()["thread_id"] == "thread-created"
    assert submit_response.json()["output"] == {"message_count": 1}


def test_submit_message_forwards_a_changed_model_before_the_first_message() -> None:
    runtime = _FakeRuntime()
    response = _client(runtime).post(
        "/api/threads/thread-created/messages",
        json={"text": "Create a diabetes cohort", "model_name": "gpt-5.6-luna"},
    )

    assert response.status_code == 200
    assert runtime.submitted_models == ["gpt-5.6-luna"]


def test_submit_message_rejects_removed_active_study_field() -> None:
    runtime = _FakeRuntime()
    response = _client(runtime).post(
        "/api/threads/thread-created/messages",
        json={"text": "Use study two", "active_study_id": "study-two"},
    )

    assert response.status_code == 422
    assert runtime.submitted_messages == []


def test_resume_interrupt_forwards_exact_payload() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)
    payload = {
        "action": "revise",
        "selected_column_keys": ["age", "a1c"],
        "feedback": "Use adult patients only",
    }

    response = client.post(
        "/api/threads/thread-1/interrupts/interrupt-1/resume",
        json=payload,
    )

    assert response.status_code == 200
    assert runtime.resumed_interrupts == [("thread-1", "interrupt-1", payload)]
    assert response.json()["thread_id"] == "thread-1"
    assert response.json()["diagnostics"] == {"resumed_count": 1}


def test_resume_interrupt_accepts_minimal_plan_selection_payload() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)
    payload = {
        "action": "approve",
        "selected_column_keys": ["concept-diabetes::person::condition"],
    }

    response = client.post(
        "/api/threads/thread-1/interrupts/interrupt-1/resume",
        json=payload,
    )

    assert response.status_code == 200
    assert runtime.resumed_interrupts == [("thread-1", "interrupt-1", payload)]


def test_resume_interrupt_accepts_typed_clarification_answer() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.post(
        "/api/threads/thread-1/interrupts/interrupt-clarification/resume",
        json={
            "action": "answer",
            "answer": "Use the 12-month follow-up visit.",
        },
    )

    assert response.status_code == 200
    assert runtime.resumed_interrupts == [
        (
            "thread-1",
            "interrupt-clarification",
            {
                "action": "answer",
                "answer": "Use the 12-month follow-up visit.",
            },
        )
    ]


def test_resume_interrupt_accepts_model_output_continue() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.post(
        "/api/threads/thread-1/interrupts/interrupt-output/resume",
        json={"action": "continue"},
    )

    assert response.status_code == 200
    assert runtime.resumed_interrupts == [
        (
            "thread-1",
            "interrupt-output",
            {"action": "continue"},
        )
    ]


def test_resume_interrupt_rejects_blank_clarification_answer() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.post(
        "/api/threads/thread-1/interrupts/interrupt-clarification/resume",
        json={"action": "answer", "answer": "   "},
    )

    assert response.status_code == 422
    assert runtime.resumed_interrupts == []


def test_duplicate_running_submit_returns_409_conflict() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.post(
        "/api/threads/thread-1/messages",
        json={"text": "duplicate"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Thread thread-1 is already running"}
    assert runtime.submitted_messages == []


def test_submit_message_rejects_unresolved_review() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.post(
        "/api/threads/thread-1/messages",
        json={"text": "awaiting-review", "attachment_ids": []},
    )

    assert response.status_code == 409
    assert "awaiting human review" in response.json()["detail"].casefold()
    assert runtime.submitted_messages == []


def test_duplicate_running_resume_returns_409_conflict() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.post(
        "/api/threads/thread-1/interrupts/interrupt-1/resume",
        json={"action": "revise", "feedback": "duplicate"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Thread thread-1 is already running"}
    assert runtime.resumed_interrupts == []


def test_get_state_route_returns_state() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.get("/api/threads/thread-1/state")

    assert response.status_code == 200
    assert response.json()["thread_id"] == "thread-1"
    assert response.json()["run"]["state"] == "idle"


def test_runtime_route_returns_safe_runtime_settings() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.get("/api/runtime")

    assert response.status_code == 200
    assert response.json() == {
        "model_name": "gpt-5.4",
        "temperature": 0.1,
        "top_p": 0.9,
        "max_steps": 4,
        "timeout_seconds": 300.0,
        "db_rag_embedding_model": "OpenAI/text-embedding-3-large",
        "db_rag_reranker_model": "disabled",
    }


def test_reset_route_returns_new_thread_id() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.post("/api/threads/thread-1/reset")

    assert response.status_code == 200
    assert runtime.reset_threads == ["thread-1"]
    assert response.json() == {"thread_id": "thread-1-reset"}


def test_export_route_returns_thread_json() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.get("/api/threads/thread-1/export")

    assert response.status_code == 200
    assert response.json()["thread_id"] == "thread-1"
    assert response.json()["output"] == {
        "qa_response": "The database contains 17 rows."
    }
    assert response.headers["content-disposition"] == (
        'attachment; filename="thread-1-thread.json"'
    )


def test_export_zip_route_returns_thread_archive() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.get("/api/threads/thread-1/export.zip")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == (
        'attachment; filename="thread-1-thread.zip"'
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "thread.json" in archive.namelist()


def test_attachment_upload_accepts_multiple_files() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.post(
        "/api/threads/thread-1/attachments",
        files=[
            ("files", ("cohort.csv", b"id,age\n1,42\n", "text/csv")),
            ("files", ("annotations.xml", b"<variables/>", "application/xml")),
        ],
    )

    assert response.status_code == 200
    assert [item["filename"] for item in response.json()["attachments"]] == [
        "cohort.csv",
        "annotations.xml",
    ]
    assert runtime.uploaded[0] == "thread-1"


def test_attachment_upload_rejects_oversized_file_before_runtime_staging() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.post(
        "/api/threads/thread-1/attachments",
        files=[
            (
                "files",
                ("large.txt", b"x" * 65, "text/plain"),
            )
        ],
    )

    assert response.status_code == 400
    assert "configured attachment size limit" in response.json()["detail"]
    assert not hasattr(runtime, "uploaded")


def test_provider_triggering_routes_require_the_exact_session_key() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime, with_provider_key=False)

    submit = client.post(
        "/api/threads/thread-1/messages",
        json={"text": "Create a cohort"},
    )
    resume = client.post(
        "/api/threads/thread-1/interrupts/interrupt-1/resume",
        json={"action": "approve", "selected_column_keys": ["age"]},
    )

    expected = {"detail": {"code": "PROVIDER_KEY_REQUIRED"}}
    assert submit.status_code == 428
    assert submit.json() == expected
    assert resume.status_code == 428
    assert resume.json() == expected
    assert runtime.authorized_threads == ["thread-1", "thread-1"]
    assert runtime.submitted_messages == []
    assert runtime.resumed_interrupts == []


def test_provider_triggering_routes_forward_the_session_key_only_to_work() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    assert client.post(
        "/api/threads/thread-1/messages",
        json={"text": "Create a cohort"},
    ).status_code == 200
    assert client.post(
        "/api/threads/thread-1/interrupts/interrupt-1/resume",
        json={"action": "approve", "selected_column_keys": ["age"]},
    ).status_code == 200

    assert runtime.submitted_provider_keys == ["session-provider-key"]
    assert runtime.resumed_provider_keys == ["session-provider-key"]


def test_read_only_and_empty_thread_routes_remain_usable_without_a_key() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime, with_provider_key=False)

    responses = [
        client.get("/api/conversations"),
        client.post("/api/conversations/thread-1/open"),
        client.post("/api/threads"),
        client.get("/api/runtime"),
        client.get("/api/runtime/options"),
        client.get("/api/threads/thread-1/state"),
        client.get("/api/threads/thread-1/attachments/attachment-1"),
        client.get("/api/threads/thread-1/datasets/dataset-1/preview"),
        client.get("/api/threads/thread-1/datasets/dataset-1/schema"),
        client.get("/api/threads/thread-1/datasets/dataset-1/download"),
        client.get("/api/threads/thread-1/artifacts/figure-1"),
        client.get("/api/threads/thread-1/artifacts/table-1/table-preview"),
        client.get("/api/threads/thread-1/export"),
        client.get("/api/threads/thread-1/export.zip"),
    ]

    assert [response.status_code for response in responses] == [200] * len(responses)
    assert runtime.state_provider_keys == [None]


def test_state_uses_an_available_key_for_owner_aware_checkpoint_recovery() -> None:
    runtime = _FakeRuntime()

    response = _client(runtime).get("/api/threads/thread-1/state")

    assert response.status_code == 200
    assert runtime.state_provider_keys == ["session-provider-key"]


def test_attachment_upload_rejects_aggregate_and_count_limits() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    aggregate = client.post(
        "/api/threads/thread-1/attachments",
        files=[
            ("files", ("a.txt", b"a" * 50, "text/plain")),
            ("files", ("b.txt", b"b" * 50, "text/plain")),
        ],
    )
    count = client.post(
        "/api/threads/thread-1/attachments",
        files=[
            ("files", ("a.txt", b"a", "text/plain")),
            ("files", ("b.txt", b"b", "text/plain")),
            ("files", ("c.txt", b"c", "text/plain")),
        ],
    )

    assert aggregate.status_code == 400
    assert "total attachment size limit" in aggregate.json()["detail"]
    assert count.status_code == 400
    assert "attachment count limit" in count.json()["detail"]
    assert not hasattr(runtime, "uploaded")


def test_submit_message_passes_exact_attachment_ids() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.post(
        "/api/threads/thread-1/messages",
        json={
            "text": "Analyze these files",
            "attachment_ids": ["attachment-a", "attachment-b"],
        },
    )

    assert response.status_code == 200
    assert runtime.submitted_messages[-1] == (
        "thread-1",
        "Analyze these files",
        ["attachment-a", "attachment-b"],
    )


def test_submit_rejects_empty_text_and_no_attachments() -> None:
    response = _client(_FakeRuntime()).post(
        "/api/threads/thread-1/messages",
        json={"text": "", "attachment_ids": []},
    )

    assert response.status_code == 422


def test_delete_discards_only_a_staged_attachment() -> None:
    runtime = _FakeRuntime()
    response = _client(runtime).delete(
        "/api/threads/thread-1/attachments/attachment-staged"
    )

    assert response.status_code == 204
    assert runtime.discarded == [("thread-1", "attachment-staged")]


def test_attachment_download_is_thread_scoped() -> None:
    response = _client(_FakeRuntime()).get(
        "/api/threads/thread-1/attachments/attachment-ready"
    )

    assert response.status_code == 200
    assert response.content == b"id,age\n1,42\n"
    assert response.headers["content-disposition"] == (
        'inline; filename="cohort.csv"; filename*=UTF-8\'\'cohort.csv'
    )


def test_dataset_preview_route_returns_rows() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.get("/api/threads/thread-1/datasets/subset-1/preview?limit=1")

    assert response.status_code == 200
    assert response.json() == {
        "dataset_id": "subset-1",
        "columns": ["person_id", "condition"],
        "rows": [{"person_id": 1, "condition": "diabetes"}],
        "row_count": 2,
    }


def test_dataset_schema_route_returns_schema() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.get("/api/threads/thread-1/datasets/subset-1/schema")

    assert response.status_code == 200
    assert response.json() == {
        "dataset_id": "subset-1",
        "schema": {"person_id": {"dataType": "integer"}},
    }


def test_dataset_schema_route_maps_missing_file_to_404() -> None:
    class MissingSchemaRuntime(_FakeRuntime):
        def dataset_schema(
            self,
            _identity,
            thread_id: str,
            dataset_id: str,
        ) -> DatasetSchemaResponse:
            raise FileNotFoundError(dataset_id)

    client = _client(MissingSchemaRuntime())

    response = client.get("/api/threads/thread-1/datasets/subset-1/schema")

    assert response.status_code == 404
    assert response.json() == {"detail": "Dataset file not found"}


def test_dataset_download_route_returns_csv() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.get("/api/threads/thread-1/datasets/subset-1/download")

    assert response.status_code == 200
    assert response.content == b"person_id,condition\n1,diabetes\n"
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        'attachment; filename="subset-1.csv"'
    )


def test_file_artifact_route_returns_bytes() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.get("/api/threads/thread-1/artifacts/figure-1")

    assert response.status_code == 200
    assert response.content == b"plot-bytes"
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["content-disposition"] == (
        'inline; filename="figure-1.png"; filename*=UTF-8\'\'figure-1.png'
    )


def test_analysis_table_preview_route_returns_bounded_csv_data() -> None:
    client = _client(_FakeRuntime())

    response = client.get(
        "/api/threads/thread-1/artifacts/table-1/table-preview?limit=1"
    )

    assert response.status_code == 200
    assert response.json() == {
        "columns": ["group", "n"],
        "rows": [{"group": "Good", "n": "10"}],
        "row_count": 1,
    }


def test_openapi_declares_api_thread_state_for_state_returning_routes() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "RePORT Agent API"
    api_thread_state_ref = {"$ref": "#/components/schemas/ApiThreadState"}
    routes = [
        ("get", "/api/threads/{thread_id}/state"),
        ("post", "/api/threads/{thread_id}/messages"),
        ("post", "/api/threads/{thread_id}/interrupts/{interrupt_id}/resume"),
    ]
    for method, path in routes:
        assert (
            schema["paths"][path][method]["responses"]["200"]["content"][
                "application/json"
            ]["schema"]
            == api_thread_state_ref
        )


def test_cors_allows_localhost_dev_origins_on_any_port() -> None:
    runtime = _FakeRuntime()
    client = _client(runtime)

    response = client.options(
        "/api/threads",
        headers={
            "Origin": "http://127.0.0.1:5177",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == (
        "http://127.0.0.1:5177"
    )


def test_conversation_history_routes_list_and_rename() -> None:
    client = _client(_FakeRuntime())

    listed = client.get("/api/conversations")
    renamed = client.patch("/api/conversations/thread-1", json={"title": "TB survival"})
    opened = client.post("/api/conversations/thread-1/open")

    assert listed.status_code == 200
    assert listed.json()["items"][0]["title"] == "TB analysis"
    assert listed.json()["items"][0]["awaiting_review"] is False
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "TB survival"
    assert opened.status_code == 200
    assert opened.json()["last_opened_at"] == "2026-07-30T18:24:00+00:00"
    assert client.patch("/api/conversations/missing", json={"title": "Nope"}).status_code == 404
    assert client.post("/api/conversations/missing/open").status_code == 404


def test_conversation_history_routes_archive_restore_and_delete() -> None:
    client = _client(_FakeRuntime())

    archived = client.post("/api/conversations/thread-1/archive")
    restored = client.post("/api/conversations/thread-1/restore")
    deleted = client.delete("/api/conversations/thread-1")

    assert archived.status_code == 200
    assert archived.json()["archived_at"] == "2026-07-30T01:00:00+00:00"
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    assert deleted.status_code == 204
    assert client.post("/api/conversations/missing/archive").status_code == 404
    assert client.post("/api/conversations/missing/restore").status_code == 404
    assert client.delete("/api/conversations/missing").status_code == 404


def test_changed_routes_forward_request_identity_to_runtime() -> None:
    class _IdentityOnlyRuntime(_FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.identities = []

        def list_conversations(self, identity):
            self.identities.append(identity)
            return super().list_conversations(identity)

    runtime = _IdentityOnlyRuntime()
    client = _client(runtime)

    response = client.get("/api/conversations")

    assert response.status_code == 200
    assert [identity.owner_user_id for identity in runtime.identities] == ["local-user"]


def test_owner_mismatch_routes_return_the_same_non_disclosing_404() -> None:
    class _MissingRuntime(_FakeRuntime):
        def stage_attachments(self, *_args): raise KeyError("hidden")
        def discard_staged_attachment(self, *_args): raise KeyError("hidden")
        def submit_message(self, *_args, **_kwargs): raise KeyError("hidden")
        def resume_interrupt(self, *_args, **_kwargs): raise KeyError("hidden")
        def reset(self, *_args): raise KeyError("hidden")
        def export_thread(self, *_args): raise KeyError("hidden")
        def export_thread_archive(self, *_args): raise KeyError("hidden")

    client = _client(_MissingRuntime())
    responses = [
        client.post("/api/threads/thread-a/attachments", files={"files": ("a.csv", b"id\n1\n", "text/csv")}),
        client.delete("/api/threads/thread-a/attachments/attachment-0123456789abcdef0123456789abcdef"),
        client.post("/api/threads/thread-a/messages", json={"text": "hello"}),
        client.post("/api/threads/thread-a/interrupts/i/resume", json={"action": "approve", "selected_column_keys": ["x"]}),
        client.post("/api/threads/thread-a/reset"),
        client.get("/api/threads/thread-a/export"),
        client.get("/api/threads/thread-a/export.zip"),
    ]

    assert all(response.status_code == 404 for response in responses)
    assert all(response.json() == {"detail": "Conversation not found"} for response in responses)
