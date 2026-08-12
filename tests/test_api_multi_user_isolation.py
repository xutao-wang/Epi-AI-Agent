from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt
from jwt.exceptions import InvalidTokenError
import pandas as pd
import pytest

from api.auth import AuthenticatedUser
from api.conversation_history import ConversationHistoryStore
from api.provider_credentials import ProviderCredentialStore
from api.runtime import ReportAgentApiRuntime
from api.server import create_app
from utils.attachment_artifacts import AttachmentLimits, LocalAttachmentStore
from utils.dataset_artifacts import persist_dataset_artifact


_DEFAULT_RUNTIME_SETTINGS = {
    "model_name": "gpt-5.4",
    "temperature": 0.1,
    "top_p": 0.9,
    "max_steps": 4,
    "timeout_seconds": 300,
    "db_rag_embedding_model": "OpenAI/text-embedding-3-large",
    "db_rag_reranker_model": "disabled",
}
_SESSIONS = {
    "user-a": "11111111-1111-4111-8111-111111111111",
    "user-b": "22222222-2222-4222-8222-222222222222",
}


class _SignedTokenVerifier:
    def __init__(self, public_key: rsa.RSAPublicKey) -> None:
        self.public_key = public_key

    def verify(self, authorization: str | None) -> AuthenticatedUser:
        if authorization is None or not authorization.startswith("Bearer "):
            raise InvalidTokenError("invalid access token")
        claims = jwt.decode(
            authorization.removeprefix("Bearer "),
            self.public_key,
            algorithms=["RS256"],
            options={"require": ["exp", "iat", "sub"]},
        )
        return AuthenticatedUser(
            owner_user_id=str(claims["sub"]),
            token_expires_at_epoch=int(claims["exp"]),
        )


class _NoProviderValidation:
    def validate(self, provider: str, api_key: str) -> None:
        return None


class _IsolationCheckpointer:
    def __init__(self) -> None:
        self.deleted_threads: list[str] = []

    def delete_thread(self, thread_id: str) -> None:
        self.deleted_threads.append(thread_id)


class _IsolationGraph:
    def __init__(
        self,
        values: dict[str, Any] | None = None,
        *,
        fail_invocation: bool = False,
    ) -> None:
        self.checkpointer = _IsolationCheckpointer()
        self.invoke_calls: list[tuple[Any, dict[str, Any]]] = []
        self.values = values or {}
        self.fail_invocation = fail_invocation

    def get_state(
        self,
        _config: dict[str, Any],
        *,
        subgraphs: bool = False,
    ) -> SimpleNamespace:
        return SimpleNamespace(values=self.values, next=(), interrupts=[])

    def invoke(self, payload: Any, config: dict[str, Any]) -> None:
        self.invoke_calls.append((payload, config))
        if self.fail_invocation:
            raise RuntimeError("first invoke failed")
        if isinstance(payload, dict):
            self.values.update(payload)


class _GraphFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.fail_invocation = False

    def __call__(self, settings: Any, context: Any) -> _IsolationGraph:
        self.calls.append((settings, context))
        dataset = persist_dataset_artifact(
            runtime_root=context.storage,
            thread_id=context.thread_id,
            dataset_id="dataset-owned",
            kind="db_rag_subset",
            dataframe=pd.DataFrame([{"person_id": 1, "value": 10}]),
            schema={
                "person_id": {"dataType": "integer"},
                "value": {"dataType": "integer"},
            },
            provenance={
                "description": "Owned synthetic dataset",
                "sql": "SELECT person_id, value FROM synthetic",
            },
            artifact_version=1,
            artifact_status="active",
        )
        analysis = {
            "artifact_id": "analysis-owned",
            "kind": "analysis_run",
            "producer": "epi_agent",
            "mime": "application/json",
            "summary": "Owned analysis",
            "status": "active",
            "created_at": "2026-08-06T00:00:00+00:00",
            "content": {
                "id": "analysis-owned",
                "kind": "analysis_run",
                "version": 1,
                "status": "active",
                "content": {
                    "schema_version": "1.0",
                    "method": "descriptive",
                    "dataset": {
                        "id": "dataset-owned",
                        "kind": "db_rag_subset",
                        "version": 1,
                    },
                    "specification": {},
                    "output_text": "Owned result",
                    "runtime": {"language": "python"},
                    "estimates": [],
                    "diagnostics": {},
                    "warnings": [],
                    "tables": [],
                    "figures": [],
                },
                "provenance": {"producer": "epi_agent"},
            },
        }
        figure = {
            "artifact_id": "figure-owned",
            "kind": "figure",
            "producer": "executor",
            "mime": "image/png",
            "summary": "Owned figure",
            "status": "approved",
            "created_at": "2026-08-06T00:00:00+00:00",
            "content": {"data_base64": "cGxvdA=="},
        }
        table = {
            "artifact_id": "table-owned",
            "kind": "table",
            "producer": "epi_agent",
            "mime": "text/csv",
            "summary": "Owned table",
            "status": "active",
            "created_at": "2026-08-06T00:00:00+00:00",
            "content": {
                "id": "table-owned",
                "kind": "table",
                "version": 1,
                "status": "active",
                "content": {"text": "group,n\nOwned,1\n"},
                "provenance": {"producer": "epi_agent"},
            },
        }
        return _IsolationGraph(
            {
                "artifacts": {
                    "datasets": {"dataset-owned": dataset},
                    "files": {
                        "analysis-owned": analysis,
                        "figure-owned": figure,
                        "table-owned": table,
                    },
                    "conversation_events": [],
                }
            },
            fail_invocation=self.fail_invocation,
        )


@dataclass(frozen=True)
class _UserClient:
    client: TestClient
    headers: dict[str, str]

    def get(self, path: str, **kwargs: Any):
        return self.client.get(path, headers=self.headers, **kwargs)

    def post(self, path: str, **kwargs: Any):
        return self.client.post(path, headers=self.headers, **kwargs)

    def patch(self, path: str, **kwargs: Any):
        return self.client.patch(path, headers=self.headers, **kwargs)

    def delete(self, path: str, **kwargs: Any):
        return self.client.delete(path, headers=self.headers, **kwargs)

    def create_thread(self) -> str:
        response = self.post("/api/threads", json={})
        assert response.status_code == 200
        return str(response.json()["thread_id"])

    def configure_key(self) -> None:
        response = self.client.put(
            "/api/session/provider-key",
            headers=self.headers,
            json={"api_key": "fixture-provider-key"},
        )
        assert response.status_code == 200

    def upload_text(
        self,
        thread_id: str,
        *,
        filename: str,
        content: bytes,
    ) -> str:
        response = self.post(
            f"/api/threads/{thread_id}/attachments",
            files={"files": (filename, content, "text/csv")},
        )
        assert response.status_code == 200
        return str(response.json()["attachments"][0]["id"])


class MultiUserClient:
    def __init__(
        self,
        client: TestClient,
        private_key: rsa.RSAPrivateKey,
    ) -> None:
        self.client = client
        self.private_key = private_key

    def as_user(self, owner_user_id: str) -> _UserClient:
        now = int(time.time())
        token = jwt.encode(
            {
                "exp": now + 300,
                "iat": now - 1,
                "sub": owner_user_id,
            },
            self.private_key,
            algorithm="RS256",
        )
        return _UserClient(
            self.client,
            {
                "Authorization": f"Bearer {token}",
                "X-Epi-Session-ID": _SESSIONS[owner_user_id],
            },
        )


@pytest.fixture
def multi_user_client(
    tmp_path: Path,
) -> tuple[MultiUserClient, ReportAgentApiRuntime, _GraphFactory]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    graph_factory = _GraphFactory()
    runtime = ReportAgentApiRuntime(
        graph_factory=graph_factory,
        default_runtime_settings=_DEFAULT_RUNTIME_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=tmp_path / "runtime",
        history_store=ConversationHistoryStore(tmp_path / "history.db"),
    )
    runtime._attachment_store = LocalAttachmentStore(
        tmp_path / "runtime",
        limits=AttachmentLimits(
            max_bytes=64,
            max_files_per_message=2,
            max_message_bytes=96,
        ),
    )
    client = TestClient(
        create_app(
            runtime,
            token_verifier=_SignedTokenVerifier(private_key.public_key()),
            credential_store=ProviderCredentialStore(),
            provider_key_validator=_NoProviderValidation(),
        ),
        raise_server_exceptions=False,
    )
    return MultiUserClient(client, private_key), runtime, graph_factory


def _filesystem_snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_conversation_routes_are_owner_scoped(
    multi_user_client: tuple[MultiUserClient, ReportAgentApiRuntime, _GraphFactory],
) -> None:
    clients, _runtime, _factory = multi_user_client
    user_a = clients.as_user("user-a")
    user_b = clients.as_user("user-b")
    thread_id = user_a.create_thread()

    assert user_b.get("/api/conversations").json() == {"items": []}
    responses = [
        user_b.post(f"/api/conversations/{thread_id}/open"),
        user_b.patch(
            f"/api/conversations/{thread_id}",
            json={"title": "Stolen title"},
        ),
        user_b.post(f"/api/conversations/{thread_id}/archive"),
        user_b.post(f"/api/conversations/{thread_id}/restore"),
        user_b.delete(f"/api/conversations/{thread_id}"),
    ]

    assert [response.status_code for response in responses] == [404] * 5
    assert user_a.get("/api/conversations").json() == {"items": []}

    user_a.configure_key()
    submitted = user_a.post(
        f"/api/threads/{thread_id}/messages",
        json={"text": "Create a cohort"},
    )
    assert submitted.status_code == 200, submitted.json()
    deadline = time.time() + 2
    while time.time() < deadline:
        state = user_a.get(f"/api/threads/{thread_id}/state").json()
        if state["run"]["state"] != "running":
            break
        time.sleep(0.01)

    owned = user_a.get("/api/conversations").json()["items"]
    assert [item["thread_id"] for item in owned] == [thread_id]
    assert owned[0]["title"] == "Untitled conversation"


def test_authenticated_failed_first_turn_stays_out_of_history(
    multi_user_client: tuple[MultiUserClient, ReportAgentApiRuntime, _GraphFactory],
) -> None:
    clients, _runtime, graph_factory = multi_user_client
    graph_factory.fail_invocation = True
    user_a = clients.as_user("user-a")
    user_a.configure_key()
    thread_id = user_a.create_thread()

    assert user_a.get("/api/conversations").json() == {"items": []}
    submitted = user_a.post(
        f"/api/threads/{thread_id}/messages",
        json={"text": "Create a cohort"},
    )
    assert submitted.status_code == 200, submitted.json()

    deadline = time.time() + 2
    state = submitted.json()
    while time.time() < deadline and state["run"]["state"] == "running":
        time.sleep(0.01)
        state = user_a.get(f"/api/threads/{thread_id}/state").json()

    assert state["run"]["state"] == "error"
    assert user_a.get("/api/conversations").json() == {"items": []}


def test_thread_state_options_message_reset_and_export_are_owner_scoped(
    multi_user_client: tuple[MultiUserClient, ReportAgentApiRuntime, _GraphFactory],
) -> None:
    clients, _runtime, graph_factory = multi_user_client
    user_a = clients.as_user("user-a")
    user_b = clients.as_user("user-b")
    thread_id = user_a.create_thread()

    assert user_b.get("/api/runtime/options").status_code == 200
    responses = [
        user_b.get(f"/api/threads/{thread_id}/state"),
        user_b.post(
            f"/api/threads/{thread_id}/messages",
            json={"text": "Use the guessed thread"},
        ),
        user_b.post(f"/api/threads/{thread_id}/reset"),
        user_b.get(f"/api/threads/{thread_id}/export"),
        user_b.get(f"/api/threads/{thread_id}/export.zip"),
    ]

    assert [response.status_code for response in responses] == [404] * 5
    assert graph_factory.calls == []
    assert user_a.get(f"/api/threads/{thread_id}/state").status_code == 200


def test_other_user_cannot_stage_read_or_delete_guessed_attachment(
    multi_user_client: tuple[MultiUserClient, ReportAgentApiRuntime, _GraphFactory],
) -> None:
    clients, runtime, _factory = multi_user_client
    user_a = clients.as_user("user-a")
    user_b = clients.as_user("user-b")
    thread_id = user_a.create_thread()
    attachment_id = user_a.upload_text(
        thread_id,
        filename="synthetic.csv",
        content=b"person_id,value\n1,10\n",
    )
    user_a.configure_key()
    submit = user_a.post(
        f"/api/threads/{thread_id}/messages",
        json={"text": "Use this file", "attachment_ids": [attachment_id]},
    )
    assert submit.status_code == 200, submit.json()
    deadline = time.time() + 2
    owned_download = user_a.get(
        f"/api/threads/{thread_id}/attachments/{attachment_id}"
    )
    while owned_download.status_code != 200 and time.time() < deadline:
        time.sleep(0.01)
        owned_download = user_a.get(
            f"/api/threads/{thread_id}/attachments/{attachment_id}"
        )
    assert owned_download.content == b"person_id,value\n1,10\n"
    before = _filesystem_snapshot(runtime.attachment_store.root)

    responses = [
        user_b.post(
            f"/api/threads/{thread_id}/attachments",
            files={"files": ("stolen.csv", b"id\n2\n", "text/csv")},
        ),
        user_b.get(f"/api/threads/{thread_id}/attachments/{attachment_id}"),
        user_b.delete(f"/api/threads/{thread_id}/attachments/{attachment_id}"),
    ]

    assert [response.status_code for response in responses] == [404] * 3
    assert _filesystem_snapshot(runtime.attachment_store.root) == before
    assert user_a.get(
        f"/api/threads/{thread_id}/attachments/{attachment_id}"
    ).content == b"person_id,value\n1,10\n"


def test_dataset_routes_hide_another_users_thread_and_dataset_ids(
    multi_user_client: tuple[MultiUserClient, ReportAgentApiRuntime, _GraphFactory],
) -> None:
    clients, runtime, _factory = multi_user_client
    user_a = clients.as_user("user-a")
    user_b = clients.as_user("user-b")
    thread_id = user_a.create_thread()
    user_a.configure_key()
    assert user_a.get(f"/api/threads/{thread_id}/state").status_code == 200
    dataset_id = "dataset-owned"
    assert user_a.get(
        f"/api/threads/{thread_id}/datasets/{dataset_id}/preview"
    ).status_code == 200
    assert user_a.get(
        f"/api/threads/{thread_id}/datasets/{dataset_id}/schema"
    ).status_code == 200
    assert user_a.get(
        f"/api/threads/{thread_id}/datasets/{dataset_id}/provenance"
    ).status_code == 200
    assert user_a.get(
        f"/api/threads/{thread_id}/datasets/{dataset_id}/download"
    ).status_code == 200
    before = _filesystem_snapshot(runtime.attachment_store.root)

    responses = [
        user_b.get(f"/api/threads/{thread_id}/datasets/{dataset_id}/preview"),
        user_b.get(f"/api/threads/{thread_id}/datasets/{dataset_id}/schema"),
        user_b.get(f"/api/threads/{thread_id}/datasets/{dataset_id}/provenance"),
        user_b.get(f"/api/threads/{thread_id}/datasets/{dataset_id}/download"),
    ]

    assert [response.status_code for response in responses] == [404] * 4
    assert _filesystem_snapshot(runtime.attachment_store.root) == before


def test_analysis_result_route_hides_another_users_ids(
    multi_user_client: tuple[MultiUserClient, ReportAgentApiRuntime, _GraphFactory],
) -> None:
    clients, runtime, _factory = multi_user_client
    user_a = clients.as_user("user-a")
    thread_id = user_a.create_thread()
    user_a.configure_key()
    assert user_a.get(f"/api/threads/{thread_id}/state").status_code == 200
    assert user_a.get(
        f"/api/threads/{thread_id}/analysis-runs/analysis-owned"
    ).status_code == 200
    before = _filesystem_snapshot(runtime.attachment_store.root)

    response = clients.as_user("user-b").get(
        f"/api/threads/{thread_id}/analysis-runs/analysis-owned"
    )

    assert response.status_code == 404
    assert _filesystem_snapshot(runtime.attachment_store.root) == before


def test_artifact_body_and_table_preview_hide_another_users_ids(
    multi_user_client: tuple[MultiUserClient, ReportAgentApiRuntime, _GraphFactory],
) -> None:
    clients, runtime, _factory = multi_user_client
    user_a = clients.as_user("user-a")
    thread_id = user_a.create_thread()
    user_a.configure_key()
    assert user_a.get(f"/api/threads/{thread_id}/state").status_code == 200
    assert user_a.get(
        f"/api/threads/{thread_id}/artifacts/figure-owned"
    ).content == b"plot"
    assert user_a.get(
        f"/api/threads/{thread_id}/artifacts/table-owned/table-preview"
    ).status_code == 200
    before = _filesystem_snapshot(runtime.attachment_store.root)

    responses = [
        clients.as_user("user-b").get(
            f"/api/threads/{thread_id}/artifacts/figure-owned"
        ),
        clients.as_user("user-b").get(
            f"/api/threads/{thread_id}/artifacts/table-owned/table-preview"
        ),
    ]

    assert [response.status_code for response in responses] == [404, 404]
    assert _filesystem_snapshot(runtime.attachment_store.root) == before


def test_interrupt_resume_hides_owner_before_provider_key_status(
    multi_user_client: tuple[MultiUserClient, ReportAgentApiRuntime, _GraphFactory],
) -> None:
    clients, _runtime, graph_factory = multi_user_client
    thread_id = clients.as_user("user-a").create_thread()

    response = clients.as_user("user-b").post(
        f"/api/threads/{thread_id}/interrupts/interrupt-guessed/resume",
        json={"action": "approve", "selected_column_keys": ["age"]},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Conversation not found"}
    assert graph_factory.calls == []


def test_nonexistent_provider_work_is_not_disclosed_as_a_missing_key(
    multi_user_client: tuple[MultiUserClient, ReportAgentApiRuntime, _GraphFactory],
) -> None:
    clients, _runtime, graph_factory = multi_user_client
    user_a = clients.as_user("user-a")

    responses = [
        user_a.post(
            "/api/threads/nonexistent/messages",
            json={"text": "hello"},
        ),
        user_a.post(
            "/api/threads/nonexistent/interrupts/interrupt/resume",
            json={"action": "continue"},
        ),
    ]

    assert [response.status_code for response in responses] == [404, 404]
    assert [response.json() for response in responses] == [
        {"detail": "Conversation not found"},
        {"detail": "Conversation not found"},
    ]
    assert graph_factory.calls == []


def test_anonymous_and_malformed_session_requests_fail_before_runtime_invocation(
    multi_user_client: tuple[MultiUserClient, ReportAgentApiRuntime, _GraphFactory],
) -> None:
    clients, _runtime, graph_factory = multi_user_client
    signed = clients.as_user("user-a")
    anonymous = clients.client.post(
        "/api/threads/guessed/messages",
        headers={"X-Epi-Session-ID": _SESSIONS["user-a"]},
        json={"text": "hello"},
    )
    malformed = clients.client.post(
        "/api/threads/guessed/messages",
        headers={**signed.headers, "X-Epi-Session-ID": "not-a-uuid"},
        json={"text": "hello"},
    )

    assert anonymous.status_code == 401
    assert malformed.status_code == 400
    assert graph_factory.calls == []


def test_oversized_attachment_is_rejected_after_identity_and_ownership(
    multi_user_client: tuple[MultiUserClient, ReportAgentApiRuntime, _GraphFactory],
) -> None:
    clients, runtime, graph_factory = multi_user_client
    user_a = clients.as_user("user-a")
    user_b = clients.as_user("user-b")
    thread_id = user_a.create_thread()
    oversized = b"x" * (1024 * 1024 + 1024)
    files = {"files": ("oversized.txt", oversized, "text/plain")}
    before = _filesystem_snapshot(runtime.attachment_store.root)

    anonymous = clients.client.post(
        f"/api/threads/{thread_id}/attachments",
        files=files,
    )
    malformed = clients.client.post(
        f"/api/threads/{thread_id}/attachments",
        headers={**user_a.headers, "X-Epi-Session-ID": "not-a-uuid"},
        files=files,
    )
    guessed = user_b.post(
        f"/api/threads/{thread_id}/attachments",
        files=files,
    )
    owned = user_a.post(
        f"/api/threads/{thread_id}/attachments",
        files=files,
    )

    assert anonymous.status_code == 401
    assert malformed.status_code == 400
    assert guessed.status_code == 404
    assert owned.status_code == 413
    assert _filesystem_snapshot(runtime.attachment_store.root) == before
    assert graph_factory.calls == []


def test_sensitive_identity_fields_are_not_accepted_from_request_bodies(
    multi_user_client: tuple[MultiUserClient, ReportAgentApiRuntime, _GraphFactory],
) -> None:
    clients, _runtime, graph_factory = multi_user_client
    user_a = clients.as_user("user-a")
    thread_id = user_a.create_thread()

    responses = [
        user_a.post("/api/threads", json={"owner_user_id": "user-b"}),
        user_a.client.put(
            "/api/session/provider-key",
            headers=user_a.headers,
            json={"api_key": "key", "owner_user_id": "user-b"},
        ),
        user_a.patch(
            f"/api/conversations/{thread_id}",
            json={"title": "Injected", "sub": "user-b"},
        ),
        user_a.post(
            "/api/threads/guessed/messages",
            json={"text": "hello", "session_id": _SESSIONS["user-b"]},
        ),
        user_a.post(
            "/api/threads/guessed/interrupts/interrupt/resume",
            json={"action": "continue", "sub": "user-b"},
        ),
    ]

    assert [response.status_code for response in responses] == [422] * 5
    assert graph_factory.calls == []
