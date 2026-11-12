"""Upload plug switch tests.

Baseline section: the plain attachments route behaves as before when no upload
plug is configured. These tests run against a fake runtime so they need no
model provider, database or network.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from api.schemas import (
    AttachmentManifestSummary,
    AttachmentUploadResult,
    RuntimeInfo,
)
from api.server import create_app
from utils.attachment_artifacts import AttachmentLimits

THREAD_ID = "thread-1"


@dataclass
class FakeRuntime:
    attachment_limits: AttachmentLimits = field(default_factory=AttachmentLimits)
    staged: list[tuple[str, list[tuple[str, str, bytes]]]] = field(
        default_factory=list
    )

    def authorize_thread(self, identity, thread_id: str) -> None:
        if thread_id != THREAD_ID:
            raise KeyError(thread_id)

    def runtime_info(self) -> RuntimeInfo:
        return RuntimeInfo(model_name="fake-model")

    def stage_attachments(self, identity, thread_id, uploads):
        self.staged.append((thread_id, list(uploads)))
        return AttachmentUploadResult(
            attachments=[
                AttachmentManifestSummary.model_validate(
                    _summary(index, name, content)
                )
                for index, (name, _mime, content) in enumerate(uploads)
            ]
        )


def _summary(index: int, name: str, content: bytes) -> dict:
    fields = AttachmentManifestSummary.model_fields
    values = {
        "id": f"attachment-{index:032x}",
        "filename": name,
        "kind": "table",
        "mime": "text/csv",
        "byte_size": len(content),
        "status": "staged",
    }
    return {key: value for key, value in values.items() if key in fields}


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
def client(runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("REPORT_AGENT_UPLOAD_PLUG_URL", raising=False)
    monkeypatch.delenv("REPORT_AGENT_UPLOAD_PLUG_SECRET", raising=False)
    return TestClient(create_app(runtime, provider_api_key="unused"))


def test_baseline_plain_upload_stages_files(
    client: TestClient, runtime: FakeRuntime
) -> None:
    response = client.post(
        f"/api/threads/{THREAD_ID}/attachments",
        files=[("files", ("study.csv", b"a,b\n1,2\n", "text/csv"))],
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["filename"] for item in body["attachments"]] == ["study.csv"]
    assert runtime.staged == [
        (THREAD_ID, [("study.csv", "text/csv", b"a,b\n1,2\n")])
    ]


def test_baseline_unknown_thread_is_404(client: TestClient) -> None:
    response = client.post(
        "/api/threads/other/attachments",
        files=[("files", ("study.csv", b"a\n", "text/csv"))],
    )
    assert response.status_code == 404


def test_baseline_too_many_files_is_400(client: TestClient) -> None:
    files = [
        ("files", (f"f{i}.csv", b"a\n", "text/csv")) for i in range(11)
    ]
    response = client.post(f"/api/threads/{THREAD_ID}/attachments", files=files)
    assert response.status_code == 400


def test_baseline_runtime_info_shape(client: TestClient) -> None:
    response = client.get("/api/runtime")
    assert response.status_code == 200
    assert response.json()["model_name"] == "fake-model"
