"""De-identified flag chain for files pulled through the upload plug.

The flag is set only server side by the plug route, carried through every hop
that copies or projects an attachment, and never set on a plain upload or a
derived file or dataset. Synthetic data only; no model provider, database or
network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ValidationError

import api.schemas as schemas
import api.server as server
from api.runtime import ReportAgentApiRuntime, _conversation, _message_attachments
from utils.attachment_artifacts import LocalAttachmentStore
from utils.attachment_readers import AttachmentReaderService, _manifest_card
from utils.display_history import build_display_history

THREAD_ID = "thread-1"
CSV = b"id,year\nS001,2020\nS002,2021\n"
TEXT = b"Synthetic form text: the visit was on [DATE].\n"


def _on_disk(store: LocalAttachmentStore, scope, attachment_id: str) -> dict:
    return json.loads(store._manifest_path(scope, attachment_id).read_text(encoding="utf-8"))


def _runtime(tmp_path) -> ReportAgentApiRuntime:
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda *args, **kwargs: None,
        default_runtime_settings={},
        models=[],
        runtime_root=tmp_path,
    )
    runtime._thread = lambda *args, **kwargs: None  # no thread store in these tests
    return runtime


# --- Store: written to manifest.json, keyword only --------------------------------


def test_store_writes_the_flag_to_the_manifest(tmp_path) -> None:
    store = LocalAttachmentStore(tmp_path)
    flagged = store.stage(THREAD_ID, "dataset_1.csv", "text/csv", CSV, deidentified=True)
    plain = store.stage(THREAD_ID, "study.csv", "text/csv", CSV)
    assert flagged["deidentified"] is True
    assert "deidentified" not in plain  # plain uploads keep their manifest shape
    assert _on_disk(store, THREAD_ID, flagged["id"])["deidentified"] is True
    assert "deidentified" not in _on_disk(store, THREAD_ID, plain["id"])  # only plug files carry the key


def test_store_flag_is_keyword_only(tmp_path) -> None:
    store = LocalAttachmentStore(tmp_path)
    with pytest.raises(TypeError):
        store.stage(THREAD_ID, "dataset_1.csv", "text/csv", CSV, True)


def test_staged_to_available_promotion_keeps_the_flag(tmp_path) -> None:
    store = LocalAttachmentStore(tmp_path)
    bound = store.stage(THREAD_ID, "dataset_1.csv", "text/csv", CSV, deidentified=True)
    assert store.begin_binding(THREAD_ID, bound["id"])["deidentified"] is True
    assert store.record_inspection(THREAD_ID, bound["id"], {"id": bound["id"]})["deidentified"] is True
    assert store.commit_binding(THREAD_ID, bound["id"])["deidentified"] is True
    assert store.rollback_available(THREAD_ID, bound["id"])["deidentified"] is True
    marked = store.stage(THREAD_ID, "form_1.txt", "text/plain", TEXT, deidentified=True)
    assert store.mark_available(THREAD_ID, marked["id"])["deidentified"] is True
    assert _on_disk(store, THREAD_ID, marked["id"])["deidentified"] is True


# --- Runtime staging -----------------------------------------------------------


def test_runtime_stage_attachments_passes_the_flag(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    flagged = runtime.stage_attachments(
        THREAD_ID, [("dataset_1.csv", "text/csv", CSV)], deidentified=True
    )
    plain = runtime.stage_attachments(THREAD_ID, [("study.csv", "text/csv", CSV)])
    assert [a.deidentified for a in flagged.attachments] == [True]
    assert [a.deidentified for a in plain.attachments] == [False]
    scope = runtime._attachment_scope(THREAD_ID, THREAD_ID)
    assert runtime.attachment_store.require(scope, flagged.attachments[0].id)["deidentified"] is True
    assert "deidentified" not in runtime.attachment_store.require(scope, plain.attachments[0].id)  # only plug files carry the key


def _bound_state(tmp_path) -> tuple[dict, str, str]:
    """Bind one flagged and one plain upload to a message, as sending it does."""
    runtime = _runtime(tmp_path)
    scope = runtime._attachment_scope(THREAD_ID, THREAD_ID)
    store = runtime.attachment_store
    flagged = store.stage(scope, "dataset_1.csv", "text/csv", CSV, deidentified=True)
    plain = store.stage(scope, "study.csv", "text/csv", CSV)
    payload = runtime._bind_message_payload(
        thread_id=THREAD_ID,
        attachment_thread_id=scope,
        snapshot=SimpleNamespace(values={}),
        message=HumanMessage(content="Describe these files", id="message-1"),
        manifests=[flagged, plain],
    )
    assert store.require(scope, flagged["id"])["deidentified"] is True
    assert "deidentified" not in store.require(scope, plain["id"])  # only plug files carry the key
    return payload, flagged["id"], plain["id"]


def test_message_binding_keeps_the_flag_in_thread_state(tmp_path) -> None:
    payload, flagged_id, plain_id = _bound_state(tmp_path)
    attachments = payload["artifacts"]["attachments"]
    assert attachments[flagged_id]["status"] == "available"
    assert attachments[flagged_id]["deidentified"] is True
    assert "deidentified" not in attachments[plain_id]  # only plug files carry the key


def test_message_history_and_api_carry_the_flag(tmp_path) -> None:
    payload, flagged_id, plain_id = _bound_state(tmp_path)
    user_message = build_display_history(payload)[0]
    projected = {a["id"]: a["deidentified"] for a in user_message.additional_kwargs["attachments"]}
    assert projected == {flagged_id: True, plain_id: False}
    conversation = _conversation(payload)
    assert {a.id: a.deidentified for a in conversation[0].attachments} == projected


# --- Display history and API messages -------------------------------------------


def _history_state() -> dict:
    upload = {
        "id": "attachment-" + "a" * 32,
        "filename": "dataset_1.csv",
        "kind": "tabular",
        "mime": "text/csv",
        "byte_size": len(CSV),
        "status": "available",
        "deidentified": True,
    }
    events = [
        {"type": "user", "event_id": "user-1", "text": "Describe the dataset"},
        {
            "type": "attachment",
            "event_id": "event-1",
            "artifact_id": upload["id"],
            "relationship": "input",
            "parent_event_id": "user-1",
        },
        {"type": "assistant", "event_id": "assistant-1", "text": "Done", "user_turn_hash": "turn-1"},
        {
            "type": "attachment",
            "event_id": "event-2",
            "artifact_id": upload["id"],
            "relationship": "used",
            "parent_event_id": "assistant-1",
        },
        # Derived outputs: a planted flag on their records must never show.
        {
            "type": "attachment",
            "event_id": "event-3",
            "artifact_id": "uploaded-000000000001",
            "relationship": "output",
            "parent_event_id": "assistant-1",
        },
        {
            "type": "attachment",
            "event_id": "event-4",
            "artifact_id": "file-1",
            "relationship": "output",
            "parent_event_id": "assistant-1",
        },
        {
            "type": "attachment",
            "event_id": "event-5",
            "artifact_id": "file-1",
            "relationship": "used",
            "parent_event_id": "assistant-1",
        },
        {
            "type": "attachment",
            "event_id": "event-6",
            "artifact_id": "uploaded-000000000002",
            "relationship": "used",
            "parent_event_id": "assistant-1",
        },
    ]
    return {
        "artifacts": {
            "conversation_events": events,
            "attachments": {upload["id"]: upload},
            "datasets": {
                "uploaded-000000000001": {
                    "kind": "uploaded",
                    "status": "active",
                    "summary": "Derived table",
                    "deidentified": True,
                },
                "uploaded-000000000002": {
                    "kind": "uploaded",
                    "status": "available",
                    "summary": "Derived table",
                    "deidentified": True,
                },
            },
            "files": {
                "file-1": {
                    "kind": "text",
                    "mime": "text/plain",
                    "summary": "Derived note",
                    "status": "available",
                    "deidentified": True,
                }
            },
        }
    }


def test_display_history_flags_uploads_but_never_derived_artifacts() -> None:
    user_message, assistant_message = build_display_history(_history_state())
    assert [a["deidentified"] for a in user_message.additional_kwargs["attachments"]] == [True]
    assert {
        a["relationship"] + ":" + a["id"]: a["deidentified"]
        for a in assistant_message.additional_kwargs["attachments"]
    } == {
        "used:attachment-" + "a" * 32: True,
        "output:uploaded-000000000001": False,
        "output:file-1": False,
        "used:file-1": False,
        "used:uploaded-000000000002": False,
    }


def test_message_attachments_take_only_a_true_flag() -> None:
    message = HumanMessage(
        content="",
        additional_kwargs={
            "attachments": [
                {"id": "a", "relationship": "input", "deidentified": True},
                {"id": "b", "relationship": "input", "deidentified": "true"},
                {"id": "c", "relationship": "input", "deidentified": 1},
                {"id": "d", "relationship": "input"},
            ]
        },
    )
    assert [a.deidentified for a in _message_attachments(message)] == [True, False, False, False]


# --- Artifact state cards -------------------------------------------------------


def test_manifest_card_carries_only_a_true_flag() -> None:
    assert _manifest_card({"deidentified": True})["deidentified"] is True
    assert "deidentified" not in _manifest_card({"deidentified": "true"})  # only plug files carry the key
    assert "deidentified" not in _manifest_card({})  # only plug files carry the key


def test_inspect_cards_carry_the_flag(tmp_path) -> None:
    store = LocalAttachmentStore(tmp_path)
    table = store.stage(THREAD_ID, "dataset_1.csv", "text/csv", CSV, deidentified=True)
    form = store.stage(THREAD_ID, "form_1.txt", "text/plain", TEXT, deidentified=True)
    plain = store.stage(THREAD_ID, "notes.txt", "text/plain", TEXT)
    for manifest in (table, form, plain):
        store.mark_available(THREAD_ID, manifest["id"])
    cards = AttachmentReaderService(store, tmp_path).inspect(
        THREAD_ID, [table["id"], form["id"], plain["id"]]
    )
    assert [card.get("deidentified") for card in cards] == [True, True, None]
    assert cards[0]["columns"] == ["id", "year"]


def test_record_cards_for_derived_artifacts_are_never_flagged(tmp_path) -> None:
    store = LocalAttachmentStore(tmp_path)
    reader = AttachmentReaderService(
        store,
        tmp_path,
        conversation_artifacts={
            "datasets": {"uploaded-000000000001": {"status": "active", "deidentified": True}},
            "files": {"file-1": {"kind": "text", "mime": "text/plain", "deidentified": True}},
        },
    )
    assert "deidentified" not in reader._available_manifest(THREAD_ID, "uploaded-000000000001")  # only plug files carry the key
    assert "deidentified" not in reader._available_manifest(THREAD_ID, "file-1")  # only plug files carry the key


# --- Schemas: response models default to unflagged; request models refuse it ------


def test_response_models_default_to_unflagged() -> None:
    summary = schemas.AttachmentManifestSummary(
        id="attachment-" + "0" * 32,
        filename="study.csv",
        kind="tabular",
        mime="text/csv",
        byte_size=1,
        status="staged",
    )
    assert summary.deidentified is False
    assert schemas.ConversationAttachment(id="a", relationship="input").deidentified is False


def _request_models() -> list[type[BaseModel]]:
    found = {}
    for module in (schemas, server):
        for name, value in vars(module).items():
            if (
                isinstance(value, type)
                and issubclass(value, BaseModel)
                and name.endswith("Request")
            ):
                found[name] = value
    return [found[name] for name in sorted(found)]


def test_no_request_model_accepts_the_flag() -> None:
    models = _request_models()
    assert {model.__name__ for model in models} >= {
        "PlugAttachmentRequest",
        "SubmitMessageRequest",
    }
    for model in models:
        assert model.model_config.get("extra") == "forbid", model.__name__
        assert "deidentified" not in model.model_fields, model.__name__
    with pytest.raises(ValidationError):
        server.PlugAttachmentRequest.model_validate({"handle": "h" * 32, "deidentified": True})
    with pytest.raises(ValidationError):
        schemas.SubmitMessageRequest.model_validate({"text": "hi", "deidentified": True})


# --- Model context -------------------------------------------------------------

# Pinned here on purpose: a change to the sentence must change this test too.
PINNED_SENTENCE = (
    "Attachments marked deidentified were de-identified by the upload checker, "
    "which can miss some identifiers, names especially. If an identifier remains, "
    "do not repeat it, infer from it or try to re-identify anyone. Bracketed "
    "tokens such as [NAME] or [DATE] are placeholders for removed values: treat "
    "them as removed, not as missing or corrupt data, and do not try to "
    "reconstruct them. Date columns may hold day offsets from each participant's "
    "first date instead of calendar dates, and ID columns should hold study IDs."
)


def _prompt_cards(prompt: str) -> list[dict]:
    artifact_context = prompt.split("\n\n", 1)[0]
    return json.loads(artifact_context.split(":\n", 1)[1])


def test_deidentified_sentence_is_the_pinned_constant() -> None:
    from epi_agent.agent import DEIDENTIFIED_ATTACHMENTS_NOTE

    assert DEIDENTIFIED_ATTACHMENTS_NOTE == PINNED_SENTENCE


def test_context_prompt_marks_flagged_cards_and_adds_the_sentence_once(tmp_path) -> None:
    from epi_agent.agent import build_epi_agent_context_prompt

    payload, flagged_id, plain_id = _bound_state(tmp_path)
    prompt = build_epi_agent_context_prompt(payload)
    cards = {card["id"]: card for card in _prompt_cards(prompt)}
    assert cards[flagged_id]["deidentified"] is True
    assert "deidentified" not in cards[plain_id]
    assert prompt.count(PINNED_SENTENCE) == 1
    assert prompt.split("\n\n")[1] == PINNED_SENTENCE


def test_context_prompt_without_flagged_files_is_unchanged(tmp_path) -> None:
    from epi_agent.agent import build_epi_agent_context_prompt

    payload, flagged_id, _plain_id = _bound_state(tmp_path)
    payload["artifacts"]["attachments"][flagged_id].pop("deidentified")
    prompt = build_epi_agent_context_prompt(payload)
    assert "deidentified" not in prompt
    assert PINNED_SENTENCE not in prompt
    assert len(_prompt_cards(prompt)) == 2


def test_context_prompt_never_flags_a_dataset_derived_from_a_flagged_file() -> None:
    from epi_agent.agent import build_epi_agent_context_prompt

    upload_id = "attachment-" + "a" * 32
    state = {
        "authorized_attachment_ids": [upload_id],
        "artifacts": {
            "attachments": {
                upload_id: {
                    "id": upload_id,
                    "kind": "tabular",
                    "status": "available",
                    "filename": "dataset_1.csv",
                    "deidentified": True,
                }
            },
            "datasets": {
                "uploaded-000000000001": {
                    "kind": "uploaded",
                    "status": "active",
                    "columns": ["id", "year"],
                    "provenance": {
                        "source": "message_attachment",
                        "source_attachment_ids": [upload_id],
                    },
                    "deidentified": True,
                }
            },
        },
    }
    cards = {card["id"]: card for card in _prompt_cards(build_epi_agent_context_prompt(state))}
    assert cards[upload_id]["deidentified"] is True
    assert "deidentified" not in cards["uploaded-000000000001"]


# --- End to end through the routes, with the real store ------------------------


def _route_runtime(tmp_path) -> ReportAgentApiRuntime:
    runtime = _runtime(tmp_path)
    runtime.authorize_thread = lambda *args, **kwargs: None
    return runtime


def _plug_settings(members: list[tuple[str, str, bytes]]):
    import hashlib
    import io
    import zipfile

    import httpx

    from utils.upload_plug import UploadPlugSettings

    files = [
        {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for path, _mime, content in members
    ]
    report = json.dumps(
        {
            "contract": "3",
            "files": files,
            "report_version": 1,
            "second_scan": {"files": len(members), "open_items": 0, "result": "PASS"},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for path, content in [(p, c) for p, _m, c in members] + [("report/report.json", report)]:
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            bundle.writestr(info, content)
    archive = buffer.getvalue()
    manifest = {
        "contract": "3",
        "report": {
            "path": "report/report.json",
            "mime": "application/json",
            "size": len(report),
            "sha256": hashlib.sha256(report).hexdigest(),
        },
        "archive": {"size": len(archive), "sha256": hashlib.sha256(archive).hexdigest()},
        "members": [
            {
                "path": path,
                "mime": mime,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, mime, content in members
        ],
    }
    handle = "h" * 32

    def plug(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/deliveries/{handle}":
            return httpx.Response(200, content=json.dumps(manifest).encode())
        if request.url.path == f"/v1/deliveries/{handle}/archive":
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    settings = UploadPlugSettings(
        url="http://127.0.0.1:8090", secret="s" * 40, transport=httpx.MockTransport(plug)
    )
    return settings, handle


def test_plug_route_stages_flagged_files_in_the_real_store(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from api.server import create_app

    runtime = _route_runtime(tmp_path)
    settings, handle = _plug_settings(
        [
            ("dataset/dataset_1.csv", "text/csv", CSV),
            ("forms/form_1.txt", "text/plain", TEXT),
        ]
    )
    client = TestClient(create_app(runtime, provider_api_key="unused", upload_plug=settings))
    response = client.post(f"/api/threads/{THREAD_ID}/plug-attachments", json={"handle": handle})
    assert response.status_code == 200, response.text
    attachments = response.json()["attachments"]
    assert [(a["filename"], a["deidentified"]) for a in attachments] == [
        ("dataset_1.csv", True),
        ("form_1.txt", True),
    ]
    scope = runtime._attachment_scope(THREAD_ID, THREAD_ID)
    for attachment in attachments:
        assert _on_disk(runtime.attachment_store, scope, attachment["id"])["deidentified"] is True
    # Canary: the archive itself never reaches the app's storage, only the staged files.
    for stored in tmp_path.rglob("*"):
        if stored.is_file():
            data = stored.read_bytes()
            assert not data.startswith(b"PK\x03\x04"), stored
            assert b"dataset/dataset_1.csv" not in data, stored


def test_plain_route_stages_unflagged_files_in_the_real_store(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from api.server import create_app
    from utils.upload_plug import UploadPlugSettings

    runtime = _route_runtime(tmp_path)
    client = TestClient(
        create_app(runtime, provider_api_key="unused", upload_plug=UploadPlugSettings())
    )
    response = client.post(
        f"/api/threads/{THREAD_ID}/attachments",
        files=[("files", ("study.csv", CSV, "text/csv"))],
        data={"deidentified": "true"},
    )
    assert response.status_code == 200, response.text
    attachment = response.json()["attachments"][0]
    assert attachment["deidentified"] is False
    scope = runtime._attachment_scope(THREAD_ID, THREAD_ID)
    assert "deidentified" not in _on_disk(runtime.attachment_store, scope, attachment["id"])  # only plug files carry the key
