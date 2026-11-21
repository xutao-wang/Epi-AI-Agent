"""Upload plug switch tests.

Baseline section: the plain attachments route behaves as before when no upload
plug is configured. These tests run against a fake runtime so they need no
model provider, database or network.
"""

from __future__ import annotations

import hashlib
import io
import json
import warnings
import zipfile
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
    deidentified: list[bool] = field(default_factory=list)

    def authorize_thread(self, identity, thread_id: str) -> None:
        if thread_id != THREAD_ID:
            raise KeyError(thread_id)

    def runtime_info(self) -> RuntimeInfo:
        return RuntimeInfo(model_name="fake-model")

    def stage_attachments(self, identity, thread_id, uploads, *, deidentified=False):
        self.staged.append((thread_id, list(uploads)))
        self.deidentified.append(deidentified)
        return AttachmentUploadResult(
            attachments=[
                AttachmentManifestSummary.model_validate(
                    _summary(index, name, content, deidentified)
                )
                for index, (name, _mime, content) in enumerate(uploads)
            ]
        )


def _summary(index: int, name: str, content: bytes, deidentified: bool = False) -> dict:
    fields = AttachmentManifestSummary.model_fields
    values = {
        "id": f"attachment-{index:032x}",
        "filename": name,
        "kind": "table",
        "mime": "text/csv",
        "byte_size": len(content),
        "status": "staged",
        "deidentified": deidentified,
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

# Synthetic study files only. Repetitive rows keep DEFLATE well under the raw
# size, so a fixture with one broken rule still fits the archive size bound and
# is refused by the unzip checks, not before the fetch.
DATASET = b"id,year,visit\n" + b"".join(
    b"S%03d,2020,%d\n" % (row, row % 3) for row in range(200)
)
DICTIONARY = json.dumps(
    {"variables": [{"name": "year", "label": "Year of visit"}] * 40}
).encode()
MEMBERS = [
    ("dataset/dataset_1.csv", "text/csv", DATASET),
    ("dictionary/dictionary_1.json", "application/json", DICTIONARY),
]
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def _zip(entries, *, method=zipfile.ZIP_DEFLATED) -> bytes:
    """Build a ZIP in memory from ``(name, content)`` or ``(name, content, method)``."""
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate-name fixtures warn on write
        with zipfile.ZipFile(buffer, "w") as bundle:
            for name, content, *rest in entries:
                info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
                info.compress_type = rest[0] if rest else method
                bundle.writestr(info, content)
    return buffer.getvalue()


REPORT_PATH = "report/report.json"


def _report(members=MEMBERS, **override) -> bytes:
    """Contract 3: the plug's per-upload report for ``members`` (codes, counts, hashes only)."""
    body = {
        "contract": "3",
        "files": [
            {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for path, _mime, content in members
        ],
        "gate": "0.7.0",
        "report_version": 1,
        "second_scan": {"files": len(members), "open_items": 0, "result": "PASS"},
        "skipped_files": 0,
        "tallies": [{"action": "REMOVED_BY_GATE", "category": "NAME", "items": 3}],
        **override,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("ascii")


def _member_zip(members=MEMBERS, *, method=zipfile.ZIP_DEFLATED, report: bytes | None = None) -> bytes:
    report = _report(members) if report is None else report
    return _zip([(path, content) for path, _mime, content in members] + [(REPORT_PATH, report)], method=method)


def _size_bound(members, report: bytes | None = None) -> int:
    """v1_design 15.2: sum(sizes) + 22 + sum(76 + 2 * len(path)), the report counted as an entry."""
    report = _report(members) if report is None else report
    entries = [(path, content) for path, _mime, content in members] + [(REPORT_PATH, report)]
    return sum(len(content) for _path, content in entries) + 22 + sum(76 + 2 * len(path) for path, _c in entries)


def _manifest(members=MEMBERS, archive: bytes | None = None, contract="3", report: bytes | None = None) -> dict:
    report = _report(members) if report is None else report
    archive = _member_zip(members, report=report) if archive is None else archive
    return {
        "contract": contract,
        "archive": {
            "size": len(archive),
            "sha256": hashlib.sha256(archive).hexdigest(),
        },
        "members": [
            {
                "path": path,
                "mime": mime,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, mime, content in members
        ],
        "report": {
            "path": REPORT_PATH,
            "mime": "application/json",
            "size": len(report),
            "sha256": hashlib.sha256(report).hexdigest(),
        },
    }


def _patch_first_entry(
    archive: bytes, local_at: int, central_at: int | None, value: bytes
) -> bytes:
    """Overwrite bytes of the first entry in its local header and, unless None, its central entry."""
    data = bytearray(archive)
    data[local_at : local_at + len(value)] = value
    if central_at is not None:
        central = int.from_bytes(data[-6:-2], "little")  # end record: central directory offset
        data[central + central_at : central + central_at + len(value)] = value
    return bytes(data)


class FakePlug:
    """A contract-2 plug: the manifest at the delivery URL, the ZIP at ``/archive``."""

    def __init__(
        self,
        members=MEMBERS,
        *,
        archive: bytes | None = None,
        manifest: dict | None = None,
        status: int = 200,
        archive_status: int = 200,
        body: bytes = b"",
    ):
        self.archive = _member_zip(members) if archive is None else archive
        self.manifest = manifest if manifest is not None else _manifest(members, self.archive)
        self.status = status
        self.archive_status = archive_status
        self.body = body
        self.requests: list[httpx.Request] = []

    @property
    def archive_requested(self) -> bool:
        return any(r.url.path.endswith("/archive") for r in self.requests)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {SECRET}":
            return httpx.Response(401)
        if request.url.params.get("thread_id") != THREAD_ID:
            return httpx.Response(404)
        if self.status != 200:
            return httpx.Response(self.status, content=self.body)
        path = request.url.path
        base = f"/v1/deliveries/{HANDLE}"
        if path == base:
            return httpx.Response(200, content=json.dumps(self.manifest).encode())
        if path == base + "/archive":
            if self.archive_status != 200:
                return httpx.Response(self.archive_status, content=self.body)
            return httpx.Response(200, content=self.archive)
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


def test_plug_off_hides_plug_routes(client: TestClient) -> None:
    assert client.get("/api/upload-plug").json() is None
    assert _pull(client).status_code == 404


def test_plug_on_reports_origin_only() -> None:
    client = _plug_client(FakeRuntime(), FakePlug())
    limits = AttachmentLimits()
    assert client.get("/api/upload-plug").json() == {
        "origin": PLUG_URL,
        "contract": "3",
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
    caps = _plug_client(runtime, FakePlug()).get("/api/upload-plug").json()["caps"]
    assert caps["max_files"] == 3


def test_plug_on_refuses_plain_upload() -> None:
    runtime = FakeRuntime()
    client = _plug_client(runtime, FakePlug())
    response = client.post(
        f"/api/threads/{THREAD_ID}/attachments",
        files=[("files", ("raw.csv", b"name\nJane\n", "text/csv"))],
    )
    assert response.status_code == 409
    assert response.json()["detail"].startswith("UPLOAD_PLUG_REQUIRED")
    assert runtime.staged == []


def test_plug_pull_unzips_and_stages_members_by_basename() -> None:
    runtime = FakeRuntime()
    plug = FakePlug()
    response = _pull(_plug_client(runtime, plug))
    assert response.status_code == 200
    assert [a["filename"] for a in response.json()["attachments"]] == [
        "dataset_1.csv",
        "dictionary_1.json",
    ]
    assert runtime.staged == [
        (
            THREAD_ID,
            [
                ("dataset_1.csv", "text/csv", DATASET),
                ("dictionary_1.json", "application/json", DICTIONARY),
            ],
        )
    ]
    # Contract 3 only: the manifest, then the one archive; never the per-file route.
    assert [r.url.path for r in plug.requests] == [
        f"/v1/deliveries/{HANDLE}",
        f"/v1/deliveries/{HANDLE}/archive",
    ]
    assert all(r.headers["x-gate-contract"] == "3" for r in plug.requests)
    assert all(r.url.params["thread_id"] == THREAD_ID for r in plug.requests)


def test_plug_pull_stages_files_flagged_deidentified() -> None:
    runtime = FakeRuntime()
    response = _pull(_plug_client(runtime, FakePlug()))
    assert response.status_code == 200
    assert runtime.deidentified == [True]
    assert [a["deidentified"] for a in response.json()["attachments"]] == [True, True]


def test_plain_upload_is_never_flagged_even_if_the_form_asks(
    client: TestClient, runtime: FakeRuntime
) -> None:
    # The multipart route has no such field; an extra form field changes nothing.
    response = client.post(
        f"/api/threads/{THREAD_ID}/attachments",
        files=[("files", ("study.csv", b"a,b\n1,2\n", "text/csv"))],
        data={"deidentified": "true"},
    )
    assert response.status_code == 200
    assert runtime.deidentified == [False]
    assert response.json()["attachments"][0]["deidentified"] is False


def test_plug_request_refuses_a_deidentified_field() -> None:
    runtime = FakeRuntime()
    plug = FakePlug()
    response = _plug_client(runtime, plug).post(
        f"/api/threads/{THREAD_ID}/plug-attachments",
        json={"handle": HANDLE, "deidentified": True},
    )
    assert response.status_code == 422
    assert plug.requests == []
    assert runtime.staged == []


def test_message_request_refuses_a_deidentified_field(client: TestClient) -> None:
    response = client.post(
        f"/api/threads/{THREAD_ID}/messages",
        json={"text": "hello", "attachment_ids": [], "deidentified": True},
    )
    assert response.status_code == 422
    assert "deidentified" in json.dumps(response.json())


def test_plug_pull_accepts_a_stored_archive_at_the_size_bound() -> None:
    archive = _member_zip(method=zipfile.ZIP_STORED)
    assert len(archive) == _size_bound(MEMBERS)
    runtime = FakeRuntime()
    response = _pull(_plug_client(runtime, FakePlug(archive=archive)))
    assert response.status_code == 200
    assert [name for name, _mime, _content in runtime.staged[0][1]] == [
        "dataset_1.csv",
        "dictionary_1.json",
    ]


def test_plug_pull_stages_in_manifest_order_whatever_the_archive_order() -> None:
    runtime = FakeRuntime()
    archive = _member_zip(list(reversed(MEMBERS)), report=_report(MEMBERS))
    response = _pull(_plug_client(runtime, FakePlug(archive=archive)))
    assert response.status_code == 200
    assert [name for name, _mime, _content in runtime.staged[0][1]] == [
        "dataset_1.csv",
        "dictionary_1.json",
    ]


@pytest.mark.parametrize(
    "handle", ["short", "../" + "a" * 30, "a" * 200, "a b" + "c" * 20]
)
def test_plug_bad_handle_never_calls_plug(handle: str) -> None:
    plug = FakePlug()
    response = _pull(_plug_client(FakeRuntime(), plug), handle=handle)
    assert response.status_code == 422
    assert plug.requests == []


def test_plug_unknown_thread_is_404() -> None:
    plug = FakePlug()
    assert _pull(_plug_client(FakeRuntime(), plug), thread="other").status_code == 404
    assert plug.requests == []


def _refused(plug: FakePlug, runtime: FakeRuntime | None = None) -> str:
    """Pull, require a 422 with nothing staged, and return the detail."""
    runtime = runtime or FakeRuntime()
    response = _pull(_plug_client(runtime, plug))
    assert response.status_code == 422
    assert runtime.staged == []
    return response.json()["detail"]


# Manifest refusals: refused before the archive is fetched, so the delivery is not spent.


def _with_paths(*paths: object) -> dict:
    manifest = _manifest()
    manifest["members"] = [
        {**manifest["members"][0], "path": path} for path in paths
    ]
    return manifest


@pytest.mark.parametrize(
    "path",
    [
        "data/dataset_1.csv",
        "dataset/form_1.csv",
        "forms/forms_1.json",
        "dictionary/dataset_1.csv",
        "metadata/dictionary_1.md",
        "dataset/dataset_0.csv",
        "dataset/dataset_01.csv",
        "dataset/dataset_10000.csv",
        "dataset/dataset_1.pdf",
        "dataset/dataset_1.CSV",
        "Dataset/dataset_1.csv",
        "dataset/dataset_1.csv\n",
        "dataset/dataset_1.csv/",
        "dataset_1.csv",
        "study/dataset/dataset_1.csv",
        "/dataset/dataset_1.csv",
        "dataset/../dataset_1.csv",
        "dataset\\dataset_1.csv",
        "dataset//dataset_1.csv",
        "",
        5,
        None,
    ],
)
def test_plug_bad_path_is_refused_before_fetch(path: object) -> None:
    plug = FakePlug(manifest=_with_paths(path))
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


def test_plug_duplicate_path_is_refused_before_fetch() -> None:
    plug = FakePlug(manifest=_with_paths("dataset/dataset_1.csv", "dataset/dataset_1.csv"))
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


def test_plug_basenames_equal_ignoring_case_are_refused_before_fetch() -> None:
    plug = FakePlug(manifest=_with_paths("dataset/dataset_1.csv", "dataset/DATASET_1.csv"))
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


def test_plug_basename_check_ignores_case() -> None:
    # The path pattern is lowercase only; the name check still holds on its own.
    from utils.upload_plug import UploadPlugError, _unique_names

    _unique_names(["dataset_1.csv", "dataset_2.csv"])
    with pytest.raises(UploadPlugError) as caught:
        _unique_names(["dataset_1.csv", "DATASET_1.csv"])
    assert caught.value.code == "PLUG_REJECTED"


def _many_members(count: int) -> list[tuple[str, str, bytes]]:
    return [(f"dataset/dataset_{i}.csv", "text/csv", b"a\n1\n") for i in range(1, count + 1)]


@pytest.mark.parametrize(
    "limits, members, code",
    [
        (AttachmentLimits(), _many_members(11), "TOO_MANY_FILES"),
        (AttachmentLimits(max_bytes=len(DATASET) - 1), MEMBERS, "FILE_TOO_LARGE"),
        (
            AttachmentLimits(max_message_bytes=len(DATASET) + len(DICTIONARY) - 1),
            MEMBERS,
            "MESSAGE_ATTACHMENTS_TOO_LARGE",
        ),
    ],
)
def test_plug_over_cap_manifest_is_not_fetched(limits, members, code: str) -> None:
    plug = FakePlug(members)
    detail = _refused(plug, FakeRuntime(attachment_limits=limits))
    assert detail.startswith(code)
    assert not plug.archive_requested


def test_plug_archive_larger_than_the_size_bound_is_not_fetched() -> None:
    manifest = _manifest()
    manifest["archive"]["size"] = _size_bound(MEMBERS) + 1
    plug = FakePlug(manifest=manifest)
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.pop("members"),
        lambda m: m.update(members=[]),
        lambda m: m.pop("archive"),
        lambda m: m["archive"].update(size=-1),
        lambda m: m["archive"].update(size=True),
        lambda m: m["archive"].update(sha256="0" * 63),
        lambda m: m["members"][0].update(size="12"),
        lambda m: m["members"][0].update(sha256="A" * 64),
        lambda m: m["members"][0].update(mime=5),
        lambda m: m.update(files=m.pop("members")),
    ],
)
def test_plug_malformed_manifest_is_not_fetched(change) -> None:
    manifest = _manifest()
    change(manifest)
    plug = FakePlug(manifest=manifest)
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


@pytest.mark.parametrize("contract", ["1", 2, "2.0", None])
def test_plug_manifest_on_another_contract_is_a_mismatch(contract) -> None:
    manifest = _manifest()
    if contract is None:
        manifest.pop("contract")
    else:
        manifest["contract"] = contract
    plug = FakePlug(manifest=manifest)
    assert _refused(plug).startswith("CONTRACT_MISMATCH")
    assert not plug.archive_requested


@pytest.mark.parametrize("on_archive", [False, True])
def test_plug_409_is_a_fixed_contract_mismatch(on_archive: bool) -> None:
    body = b"gate says: dataset/dataset_1.csv contract 1 only"
    plug = (
        FakePlug(archive_status=409, body=body)
        if on_archive
        else FakePlug(status=409, body=body)
    )
    assert _refused(plug) == (
        "CONTRACT_MISMATCH: the upload plug uses a different contract version"
    )
    assert plug.archive_requested is on_archive


def test_plug_403_and_spent_delivery_are_rejected() -> None:
    assert _refused(FakePlug(status=403)).startswith("PLUG_REJECTED")
    assert _refused(FakePlug(archive_status=404)).startswith("PLUG_REJECTED")


# Archive refusals: the archive was fetched; nothing is staged.


def _archive_refusal(archive: bytes, members=MEMBERS) -> str:
    plug = FakePlug(members, archive=archive)
    detail = _refused(plug)
    assert plug.archive_requested
    return detail


def _stored_one(name: str = "dataset/dataset_1.csv", content: bytes = DATASET) -> bytes:
    # the report (contract 3) follows the member, so the member stays the first entry
    report = _report([(name, "text/csv", content)])
    return _zip([(name, content), (REPORT_PATH, report)], method=zipfile.ZIP_STORED)


ONE = MEMBERS[:1]


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param([*MEMBERS, ("metadata/metadata_1.txt", "", b"x")], id="extra"),
        pytest.param(MEMBERS[:1], id="missing"),
        pytest.param([*MEMBERS, MEMBERS[0]], id="duplicate"),
        pytest.param([*MEMBERS, ("dataset/", "", b"")], id="directory"),
        pytest.param([*MEMBERS, ("/dataset/dataset_1.csv", "", b"x")], id="absolute"),
        pytest.param([*MEMBERS, ("dataset/../x.csv", "", b"x")], id="dotdot"),
        pytest.param([*MEMBERS, ("dataset\\x.csv", "", b"x")], id="backslash"),
    ],
)
def test_plug_archive_entries_must_equal_the_manifest(entries) -> None:
    archive = _zip([(path, content) for path, _mime, content in entries])
    assert len(archive) <= _size_bound(MEMBERS)  # refused by the unzip, not the bound
    detail = _archive_refusal(archive)
    assert detail == "PLUG_REJECTED: the prepared archive does not match its manifest"


def test_plug_entry_name_with_a_nul_byte_is_refused() -> None:
    # zipfile cuts filename at a NUL, which would make it equal the manifest path.
    name = "dataset/dataset_1.csv"
    archive = _zip([(name + "Z", DATASET)])
    archive = _patch_first_entry(archive, 30 + len(name), 46 + len(name), b"\x00")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        assert bundle.infolist()[0].filename == name
    detail = _archive_refusal(archive, ONE)
    assert detail == "PLUG_REJECTED: the prepared archive does not match its manifest"


def test_plug_encrypted_entry_is_refused() -> None:
    archive = _patch_first_entry(_stored_one(), 6, 8, (1).to_bytes(2, "little"))
    detail = _archive_refusal(archive, ONE)
    assert detail == "PLUG_REJECTED: the prepared archive holds an encrypted file"


def test_plug_other_compression_is_refused() -> None:
    archive = _zip([("dataset/dataset_1.csv", DATASET, zipfile.ZIP_BZIP2)])
    assert len(archive) <= _size_bound(ONE)
    detail = _archive_refusal(archive, ONE)
    assert detail == (
        "PLUG_REJECTED: the prepared archive uses an unsupported compression method"
    )


def test_plug_entry_size_differing_from_the_manifest_is_refused() -> None:
    archive = _zip([("dataset/dataset_1.csv", DATASET + b"S999,2020,0\n")])
    detail = _archive_refusal(archive, ONE)
    assert detail == "PLUG_REJECTED: the prepared archive does not match its manifest"


def test_plug_entry_hash_differing_from_the_manifest_is_refused() -> None:
    tampered = DATASET.replace(b"S007", b"S070", 1)
    assert len(tampered) == len(DATASET)
    archive = _zip([("dataset/dataset_1.csv", tampered), (REPORT_PATH, _report(ONE))])
    detail = _archive_refusal(archive, ONE)
    assert detail == "PLUG_REJECTED: a prepared file did not match its checksum"


def test_plug_archive_hash_mismatch_is_refused() -> None:
    manifest = _manifest()
    manifest["archive"]["sha256"] = hashlib.sha256(b"another archive").hexdigest()
    plug = FakePlug(manifest=manifest)
    assert _refused(plug) == "PLUG_REJECTED: the prepared archive did not match its checksum"
    assert plug.archive_requested


def test_plug_archive_longer_than_declared_is_refused() -> None:
    archive = _member_zip()
    manifest = _manifest(archive=archive)
    manifest["archive"]["size"] = len(archive) - 1
    plug = FakePlug(manifest=manifest, archive=archive)
    assert _refused(plug) == "PLUG_REJECTED: the upload plug sent more data than declared"


@pytest.mark.parametrize(
    "archive",
    [
        pytest.param(b"not a zip archive at all", id="not-a-zip"),
        # A wrong CRC: zipfile names the entry in its error.
        pytest.param(_patch_first_entry(_stored_one(), 14, 16, b"\x00\x00\x00\x00"), id="crc"),
        # Local header name differs from the central directory: zipfile echoes both names.
        pytest.param(
            _patch_first_entry(_stored_one(), 30, None, b"dataset/dataset_2.csv"),
            id="header-name",
        ),
        pytest.param(_stored_one()[: len(_stored_one()) // 2], id="truncated"),
    ],
)
def test_plug_zipfile_error_text_is_not_echoed(archive: bytes) -> None:
    detail = _archive_refusal(archive, ONE)
    assert detail == "PLUG_REJECTED: the prepared archive could not be read safely"


def test_plug_zipfile_error_is_not_chained() -> None:
    from utils.upload_plug import UploadPlugError, fetch_delivery

    archive = _patch_first_entry(_stored_one(), 14, 16, b"\x00\x00\x00\x00")
    settings = UploadPlugSettings(
        url=PLUG_URL,
        secret=SECRET,
        transport=httpx.MockTransport(FakePlug(ONE, archive=archive)),
    )
    with pytest.raises(UploadPlugError) as caught:
        fetch_delivery(
            settings, HANDLE, THREAD_ID, max_files=10, max_file_bytes=10**6, max_total_bytes=10**7
        )
    assert caught.value.code == "PLUG_REJECTED"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert "dataset_1" not in str(caught.value)


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

        def stage_attachments(self, identity, thread_id, uploads, *, deidentified=False):
            result = super().stage_attachments(
                identity, thread_id, uploads[:1], deidentified=deidentified
            )
            result.errors.append(
                {"filename": uploads[1][0], "code": "UNSUPPORTED_FORMAT", "message": "x"}
            )
            return result

        def discard_staged_attachment(self, identity, thread_id, attachment_id):
            self.discarded.append(attachment_id)

    runtime = PartialRuntime()
    response = _pull(_plug_client(runtime, FakePlug()))
    assert response.status_code == 422
    assert "UNSUPPORTED_FORMAT" in response.json()["detail"]
    assert runtime.discarded == ["attachment-" + "0" * 32]


@pytest.mark.parametrize(
    "url, secret",
    [
        (PLUG_URL, ""),
        (PLUG_URL, "short"),
        ("http://example.com", SECRET),
        ("ftp://127.0.0.1:8090", SECRET),
        ("http://127.0.0.1:8090/?x=1", SECRET),
        ("http://127.0.0.1:8090/gate", SECRET),
        ("http://127.0.0.1:99999", SECRET),
    ],
)
def test_half_configured_plug_refuses_to_start(url: str, secret: str) -> None:
    with pytest.raises(ValueError):
        create_app(
            FakeRuntime(),
            provider_api_key="unused",
            upload_plug=UploadPlugSettings(url=url, secret=secret),
        )


def test_plug_on_refuses_plain_upload_before_reading_the_body() -> None:
    """Raw bytes must never reach the app process: the 409 comes before any body is read."""
    import asyncio

    runtime = FakeRuntime()
    app = _plug_client(runtime, FakePlug()).app
    scope = {
        "type": "http", "method": "POST", "path": f"/api/threads/{THREAD_ID}/attachments",
        "raw_path": f"/api/threads/{THREAD_ID}/attachments".encode(), "query_string": b"",
        "headers": [(b"content-type", b"multipart/form-data; boundary=x"), (b"content-length", b"1000")],
        "scheme": "http", "server": ("testserver", 80), "client": ("127.0.0.1", 1),
        "http_version": "1.1", "root_path": "",
    }
    sent: list[dict] = []

    async def receive():
        raise AssertionError("the request body was read")

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 409
    assert runtime.staged == []


def test_gateway_errors_are_unreachable_not_rejected() -> None:
    from utils.upload_plug import UploadPlugError, _raise_for_status

    for status in (502, 503, 504):
        with pytest.raises(UploadPlugError) as caught:
            _raise_for_status(httpx.Response(status))
        assert caught.value.code == "PLUG_UNREACHABLE"


@pytest.mark.parametrize(
    "url, origin",
    [
        ("http://127.0.0.1:8090", "http://127.0.0.1:8090"),
        ("http://LOCALHOST:8090", "http://localhost:8090"),
        ("https://Gate.Example.org:443", "https://gate.example.org"),
        ("http://127.0.0.1:80/", "http://127.0.0.1"),
        ("http://[::1]:8090", "http://[::1]:8090"),
    ],
)
def test_plug_origin_is_what_the_browser_reports(url: str, origin: str) -> None:
    assert UploadPlugSettings(url=url, secret=SECRET).origin == origin


def test_plug_secret_is_not_in_the_settings_repr() -> None:
    assert SECRET not in repr(UploadPlugSettings(url=PLUG_URL, secret=SECRET))


def test_plug_off_leaves_http_logging_alone() -> None:
    import logging

    logger = logging.getLogger("httpx")
    logger.setLevel(logging.NOTSET)
    create_app(FakeRuntime(), provider_api_key="unused", upload_plug=UploadPlugSettings())
    assert logger.level == logging.NOTSET
    create_app(
        FakeRuntime(),
        provider_api_key="unused",
        upload_plug=UploadPlugSettings(url=PLUG_URL, secret=SECRET),
    )
    assert logger.level == logging.WARNING


def test_plug_pull_logs_no_handle_secret_or_thread(caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    response = _pull(_plug_client(FakeRuntime(), FakePlug()))
    assert response.status_code == 200
    text = caplog.text + json.dumps(response.json())
    assert SECRET not in text
    assert HANDLE not in caplog.text
    # Nothing from the archive is logged: no member paths, contents or hashes.
    assert "dataset/" not in caplog.text
    assert "S001" not in caplog.text
    assert hashlib.sha256(DATASET).hexdigest() not in caplog.text


def test_plug_refusal_logs_no_archive_detail(caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    archive = _patch_first_entry(_stored_one(), 14, 16, b"\x00\x00\x00\x00")
    _archive_refusal(archive, ONE)
    assert "dataset_1" not in caplog.text
    assert "CRC" not in caplog.text


# Review follow-ups: fixed manifest keys, fixed media types, the shared size bound, no
# compressed transfer.


def test_plug_size_bound_matches_the_checker_formula() -> None:
    # The same example is pinned in the gate's tests: one 100-byte member at
    # dataset/dataset_1.csv (21 characters) gives 100 + 22 + 76 + 42 = 240. Contract 3 adds the
    # report as one more entry: a 50-byte report at report/report.json (18 characters) adds
    # 50 + 76 + 36.
    assert _size_bound([("dataset/dataset_1.csv", "text/csv", b"x" * 100)], report=b"y" * 50) == 240 + 50 + 76 + 36


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda m: m.update(names=["visits.csv"]), id="manifest-extra"),
        pytest.param(lambda m: m["archive"].update(name="study.zip"), id="archive-extra"),
        pytest.param(lambda m: m["members"][0].update(original="visits.csv"), id="member-extra"),
        pytest.param(lambda m: m["members"][0].pop("mime"), id="member-missing-mime"),
    ],
)
def test_plug_manifest_with_unexpected_fields_is_refused_before_fetch(change) -> None:
    manifest = _manifest()
    change(manifest)
    plug = FakePlug(manifest=manifest)
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


@pytest.mark.parametrize("mime", ["text/plain", "application/octet-stream", "text/csv\n", ""])
def test_plug_media_type_must_be_the_one_written_for_the_extension(mime) -> None:
    manifest = _manifest()
    manifest["members"][0]["mime"] = mime
    plug = FakePlug(manifest=manifest)
    assert _refused(plug) == "PLUG_REJECTED: the upload plug returned an invalid media type"
    assert not plug.archive_requested


def test_plug_asks_for_no_compressed_transfer_and_refuses_one() -> None:
    plug = FakePlug()
    runtime = FakeRuntime()
    assert _pull(_plug_client(runtime, plug)).status_code == 200
    assert all(r.headers.get("accept-encoding") == "identity" for r in plug.requests)

    class GzipPlug(FakePlug):
        def __call__(self, request):
            response = super().__call__(request)
            if request.url.path.endswith("/archive"):
                import gzip

                return httpx.Response(
                    200, content=gzip.compress(self.archive), headers={"Content-Encoding": "gzip"}
                )
            return response

    assert _refused(GzipPlug()) == "PLUG_REJECTED: the upload plug sent an encoded response"


# Contract 3: the plug's per-upload report is checked and never staged.


def test_plug_report_is_checked_and_never_staged() -> None:
    runtime = FakeRuntime()
    response = _pull(_plug_client(runtime, FakePlug()))
    assert response.status_code == 200
    staged = [name for name, _mime, _content in runtime.staged[0][1]]
    assert staged == ["dataset_1.csv", "dictionary_1.json"]
    assert all("report" not in a["filename"] for a in response.json()["attachments"])


def test_plug_contract_2_manifest_is_a_contract_mismatch() -> None:
    manifest = _manifest()
    manifest["contract"] = "2"
    manifest.pop("report")
    plug = FakePlug(manifest=manifest)
    assert _refused(plug).startswith("CONTRACT_MISMATCH")
    assert not plug.archive_requested


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda m: m.pop("report"), id="missing"),
        pytest.param(lambda m: m["report"].update(path="metadata/report.json"), id="path"),
        pytest.param(lambda m: m["report"].update(mime="text/plain"), id="mime"),
        pytest.param(lambda m: m["report"].update(size=0), id="empty"),
        pytest.param(lambda m: m["report"].update(size=64 * 1024 + 1), id="too-large"),
        pytest.param(lambda m: m["report"].update(sha256="A" * 64), id="digest"),
        pytest.param(lambda m: m["report"].update(name="x"), id="extra-field"),
    ],
)
def test_plug_bad_report_entry_is_refused_before_fetch(change) -> None:
    manifest = _manifest()
    change(manifest)
    plug = FakePlug(manifest=manifest)
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


def _report_refusal(report: bytes, archive: bytes | None = None) -> str:
    manifest = _manifest(report=report, archive=archive)
    archive = _member_zip(report=report) if archive is None else archive
    plug = FakePlug(manifest=manifest, archive=archive)
    detail = _refused(plug)
    assert plug.archive_requested
    return detail


@pytest.mark.parametrize(
    "report",
    [
        pytest.param(_report(MEMBERS[:1]), id="fewer-files"),
        pytest.param(_report([(p, m, c + b"x") for p, m, c in MEMBERS]), id="other-hash"),
        pytest.param(_report(list(reversed(MEMBERS))), id="other-order"),
        pytest.param(_report(second_scan={"files": 2, "open_items": 1, "result": "PASS"}), id="open-items"),
        pytest.param(_report(second_scan={"files": 2, "open_items": 0, "result": "FAIL"}), id="scan-failed"),
        pytest.param(_report(contract="2"), id="contract"),
        pytest.param(_report(report_version=2), id="version"),
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"[1, 2]", id="not-object"),
        pytest.param('{"contract": "3", "x": "\u00e9"}'.encode("utf-8"), id="not-ascii"),
    ],
)
def test_plug_report_that_does_not_match_is_refused(report: bytes) -> None:
    detail = _report_refusal(report)
    assert detail == "PLUG_REJECTED: the plug's report does not match the delivered files"


def test_plug_report_hash_differing_from_the_manifest_is_refused() -> None:
    report = _report()
    tampered = report.replace(b'"items":3', b'"items":4')
    assert len(tampered) == len(report)
    detail = _report_refusal(report, archive=_member_zip(report=tampered))
    assert detail == "PLUG_REJECTED: the plug's report did not match its checksum"


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param([(p, c) for p, _m, c in MEMBERS], id="report-missing"),
        pytest.param([(REPORT_PATH, _report())] + [(p, c) for p, _m, c in MEMBERS], id="report-first"),
        pytest.param([(p, c) for p, _m, c in MEMBERS] + [(REPORT_PATH, _report())] * 2, id="report-twice"),
    ],
)
def test_plug_archive_without_the_report_last_is_refused(entries) -> None:
    detail = _archive_refusal(_zip(entries))
    assert detail == "PLUG_REJECTED: the prepared archive does not match its manifest"
