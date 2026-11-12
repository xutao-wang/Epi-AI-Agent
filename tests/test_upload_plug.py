"""Upload plug switch tests.

Baseline section: the plain attachments route behaves as before when no upload
plug is configured. These tests run against a fake runtime so they need no
model provider, database or network.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient

from api.schemas import (
    AttachmentManifestSummary,
    AttachmentUploadResult,
    RuntimeInfo,
)
from api.server import create_app
from utils.attachment_artifacts import AttachmentLimits
from utils.upload_plug import UploadPlugSettings

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


# --- Plug on -----------------------------------------------------------------


PLUG_URL = "http://127.0.0.1:8090"
SECRET = "s" * 40
HANDLE = "h" * 32


def _manifest(files, contract="1"):
    return {
        "contract": contract,
        "files": [
            {
                "name": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "mime": "text/csv",
            }
            for name, content in files
        ],
    }


class FakePlug:
    def __init__(self, files, *, manifest=None, status=200, file_override=None):
        self.files = files
        self.manifest = manifest if manifest is not None else _manifest(files)
        self.status = status
        self.file_override = file_override or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {SECRET}":
            return httpx.Response(401)
        if request.url.params.get("thread_id") != THREAD_ID:
            return httpx.Response(404)
        if self.status != 200:
            return httpx.Response(self.status)
        path = request.url.path
        base = f"/v1/deliveries/{HANDLE}"
        if path == base:
            return httpx.Response(200, content=json.dumps(self.manifest).encode())
        if path.startswith(base + "/files/"):
            index = int(path.rsplit("/", 1)[1])
            content = self.file_override.get(index, self.files[index][1])
            return httpx.Response(200, content=content)
        return httpx.Response(404)


def _plug_client(runtime: FakeRuntime, plug: FakePlug) -> TestClient:
    settings = UploadPlugSettings(
        url=PLUG_URL, secret=SECRET, transport=httpx.MockTransport(plug)
    )
    return TestClient(
        create_app(runtime, provider_api_key="unused", upload_plug=settings)
    )


def _pull(client: TestClient, handle: str = HANDLE, thread: str = THREAD_ID):
    return client.post(
        f"/api/threads/{thread}/plug-attachments", json={"handle": handle}
    )


FILES = [("dataset_1.csv", b"id,year\nS001,2020\n"), ("dictionary_1.xlsx", b"PK..")]


def test_plug_off_hides_plug_routes(client: TestClient) -> None:
    assert client.get("/api/upload-plug").json() is None
    assert _pull(client).status_code == 404


def test_plug_on_reports_origin_only() -> None:
    client = _plug_client(FakeRuntime(), FakePlug(FILES))
    limits = AttachmentLimits()
    assert client.get("/api/upload-plug").json() == {
        "origin": PLUG_URL,
        "contract": "1",
        "caps": {
            "max_files": limits.max_files_per_message,
            "max_file_bytes": limits.max_bytes,
            "max_total_bytes": limits.max_message_bytes,
            "max_table_rows": limits.max_table_rows,
            "max_table_columns": limits.max_table_columns,
            "max_table_cells": limits.max_table_cells,
        },
    }


def test_plug_caps_follow_the_configured_limits() -> None:
    runtime = FakeRuntime(attachment_limits=AttachmentLimits(max_files_per_message=3))
    caps = _plug_client(runtime, FakePlug(FILES)).get("/api/upload-plug").json()["caps"]
    assert caps["max_files"] == 3


def test_plug_on_refuses_plain_upload() -> None:
    runtime = FakeRuntime()
    client = _plug_client(runtime, FakePlug(FILES))
    response = client.post(
        f"/api/threads/{THREAD_ID}/attachments",
        files=[("files", ("raw.csv", b"name\nJane\n", "text/csv"))],
    )
    assert response.status_code == 409
    assert response.json()["detail"].startswith("UPLOAD_PLUG_REQUIRED")
    assert runtime.staged == []


def test_plug_pull_stages_verified_files() -> None:
    runtime = FakeRuntime()
    plug = FakePlug(FILES)
    response = _pull(_plug_client(runtime, plug))
    assert response.status_code == 200
    assert [a["filename"] for a in response.json()["attachments"]] == [
        "dataset_1.csv",
        "dictionary_1.xlsx",
    ]
    assert runtime.staged[0][1][0] == ("dataset_1.csv", "text/csv", FILES[0][1])
    assert all(r.headers["x-gate-contract"] == "1" for r in plug.requests)


@pytest.mark.parametrize(
    "handle", ["short", "../" + "a" * 30, "a" * 200, "a b" + "c" * 20]
)
def test_plug_bad_handle_never_calls_plug(handle: str) -> None:
    plug = FakePlug(FILES)
    response = _pull(_plug_client(FakeRuntime(), plug), handle=handle)
    assert response.status_code == 422
    assert plug.requests == []


def test_plug_unknown_thread_is_404() -> None:
    plug = FakePlug(FILES)
    assert _pull(_plug_client(FakeRuntime(), plug), thread="other").status_code == 404
    assert plug.requests == []


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"status": 403}, "PLUG_REJECTED"),
        ({"status": 409}, "CONTRACT_MISMATCH"),
        ({"manifest": _manifest(FILES, contract="2")}, "CONTRACT_MISMATCH"),
        ({"file_override": {1: b"tampered"}}, "PLUG_REJECTED"),
        ({"manifest": _manifest([("../evil.csv", b"x")])}, "PLUG_REJECTED"),
        ({"manifest": _manifest([("a.csv", b"x"), ("A.csv", b"y")])}, "PLUG_REJECTED"),
        ({"manifest": {"contract": "1", "files": []}}, "PLUG_REJECTED"),
        (
            {"manifest": _manifest([(f"f{i}.csv", b"x") for i in range(11)])},
            "TOO_MANY_FILES",
        ),
    ],
)
def test_plug_failures_stage_nothing(kwargs, code: str) -> None:
    runtime = FakeRuntime()
    response = _pull(_plug_client(runtime, FakePlug(FILES, **kwargs)))
    assert response.status_code == 422
    assert response.json()["detail"].startswith(code)
    assert runtime.staged == []


def test_plug_unreachable_is_502_and_stages_nothing() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    runtime = FakeRuntime()
    settings = UploadPlugSettings(
        url=PLUG_URL, secret=SECRET, transport=httpx.MockTransport(down)
    )
    client = TestClient(
        create_app(runtime, provider_api_key="unused", upload_plug=settings)
    )
    response = _pull(client)
    assert response.status_code == 502
    assert response.json()["detail"].startswith("PLUG_UNREACHABLE")
    assert runtime.staged == []


def test_plug_redirect_is_not_followed() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://example.com/x"})

    settings = UploadPlugSettings(
        url=PLUG_URL, secret=SECRET, transport=httpx.MockTransport(redirect)
    )
    client = TestClient(
        create_app(FakeRuntime(), provider_api_key="unused", upload_plug=settings)
    )
    assert _pull(client).status_code == 422


def test_plug_partial_staging_is_rolled_back() -> None:
    class PartialRuntime(FakeRuntime):
        discarded: list[str] = []

        def stage_attachments(self, identity, thread_id, uploads):
            result = super().stage_attachments(identity, thread_id, uploads[:1])
            result.errors.append(
                {"filename": uploads[1][0], "code": "UNSUPPORTED_FORMAT", "message": "x"}
            )
            return result

        def discard_staged_attachment(self, identity, thread_id, attachment_id):
            self.discarded.append(attachment_id)

    runtime = PartialRuntime()
    response = _pull(_plug_client(runtime, FakePlug(FILES)))
    assert response.status_code == 422
    assert runtime.discarded == ["attachment-" + "0" * 32]


@pytest.mark.parametrize(
    "url, secret",
    [
        (PLUG_URL, ""),
        (PLUG_URL, "short"),
        ("http://example.com", SECRET),
        ("ftp://127.0.0.1:8090", SECRET),
        ("http://127.0.0.1:8090/?x=1", SECRET),
    ],
)
def test_half_configured_plug_refuses_to_start(url: str, secret: str) -> None:
    with pytest.raises(ValueError):
        create_app(
            FakeRuntime(),
            provider_api_key="unused",
            upload_plug=UploadPlugSettings(url=url, secret=secret),
        )
