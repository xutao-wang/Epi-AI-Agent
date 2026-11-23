"""Sidebar titles after an upload: the title model must not store a refusal as the title.

The title model only ever sees the typed first message (and, when files are
attached, the fact that they exist). These tests use a stub model, so no
provider is called.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.conversation_history import ConversationHistoryStore, ConversationTitleGenerator


class StubModel:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[list[object]] = []

    def invoke(self, messages):
        self.calls.append(list(messages))
        return SimpleNamespace(content=self.reply)


def _sent_text(model: StubModel) -> str:
    return "\n".join(str(message.content) for message in model.calls[-1])


CURLY_CANT = "can" + chr(0x2019) + "t"


@pytest.mark.parametrize(
    "reply",
    [
        "I don't have access to the data",
        "I don't see any attached file in your message. Could you please:\n\n1. Upload it",
        "I need more information to help you",
        "I'm unable to view the uploaded files",
        "I" + chr(0x2019) + "d need the data first",
        "Data you " + CURLY_CANT + " see yet",
        "Cannot read attached dataset",
        "No access to uploaded files",
        "Please share the data you would like me to analyze and summarize today",
    ],
)
def test_reply_or_refusal_falls_back_to_the_typed_message(reply: str) -> None:
    model = StubModel(reply)
    title = ConversationTitleGenerator(model).generate(
        "What is the mean age in this dataset?", has_attachments=True
    )
    assert title == "What is the mean age in this dataset?"


def test_refusal_with_only_whitespace_message_uses_attachment_title() -> None:
    model = StubModel("I don't have access to the data")
    assert ConversationTitleGenerator(model).generate("   ", has_attachments=True) == (
        ConversationHistoryStore.fallback_title("")
    )


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("Mean age in the uploaded cohort", "Mean age in the uploaded cohort"),
        ('"Data quality review."', "Data quality review"),
        ("Incidence by district and visit", "Incidence by district and visit"),
        ("IgG titres by visit", "IgG titres by visit"),
    ],
)
def test_real_titles_are_kept(reply: str, expected: str) -> None:
    model = StubModel(reply)
    assert ConversationTitleGenerator(model).generate("Check this data", has_attachments=True) == expected


def test_long_typed_message_fallback_is_cut_at_a_word_boundary() -> None:
    model = StubModel("# Summary Statistics by Arm\n\n| Arm | Participants | Mean Score |")
    message = (
        "Using the visits dataset (dataset_1.csv), give the number of participants, "
        "the mean score and the mean weight_kg for each arm, rounded to 2 decimals."
    )
    title = ConversationTitleGenerator(model).generate(message, has_attachments=True)
    assert title == "Using the visits dataset (dataset_1.csv), give the number of participants"
    assert len(title.split()) <= 10


def test_prompt_says_not_to_answer_the_request() -> None:
    model = StubModel("Mean age in the cohort")
    ConversationTitleGenerator(model).generate("What is the mean age?")
    assert "Do not answer or carry out the request" in str(model.calls[-1][0].content)


def test_access_as_a_study_word_falls_back_to_the_typed_message() -> None:
    # Known trade-off: "access" is also an epidemiology word. The title then
    # falls back to the user's own message, which is still a usable title.
    model = StubModel("Access to care by district")
    title = ConversationTitleGenerator(model).generate("Compare access to care by district")
    assert title == "Compare access to care by district"


def test_prompt_says_files_are_attached_but_not_shown() -> None:
    model = StubModel("Mean age in the cohort")
    ConversationTitleGenerator(model).generate("What is the mean age?", has_attachments=True)
    system = str(model.calls[-1][0].content)
    assert "attached data files" in system
    assert "cannot see" in system


def test_prompt_without_files_has_no_attachment_hint() -> None:
    model = StubModel("Mean age in the cohort")
    ConversationTitleGenerator(model).generate("What is the mean age?")
    assert "attached" not in str(model.calls[-1][0].content)


def test_only_the_typed_message_reaches_the_title_model() -> None:
    model = StubModel("Mean age in the cohort")
    ConversationTitleGenerator(model).generate("What is the mean age?", has_attachments=True)
    assert len(model.calls[-1]) == 2
    assert str(model.calls[-1][1].content) == "What is the mean age?"


def test_runtime_passes_only_a_flag_never_file_names(monkeypatch) -> None:
    """The runtime tells the title generator that files exist, and nothing about them."""
    from api import runtime as runtime_module

    seen: dict[str, object] = {}

    class RecordingGenerator:
        def generate(self, text, **kwargs):
            seen["args"] = (text,)
            seen["kwargs"] = kwargs
            return "Mean age in the cohort"

    class Store:
        def set_automatic_title(self, owner, thread, title):
            seen["stored"] = title

    fake = SimpleNamespace(history_store=Store())
    runtime_module.ReportAgentApiRuntime._generate_title(
        fake, "owner", "thread", "What is the mean age?", RecordingGenerator(), True
    )
    assert seen["args"] == ("What is the mean age?",)
    assert seen["kwargs"] == {"has_attachments": True}
    assert seen["stored"] == "Mean age in the cohort"


def _submit(tmp_path, messages, *, with_file: bool, rename: str | None = None):
    """Runs the real ReportAgentApiRuntime.submit_message with a fake graph, once per message, and
    records every call to the title model (there should be none)."""
    from api.auth import LOCAL_REQUEST_IDENTITY as identity
    from api.runtime import ReportAgentApiRuntime

    calls: list[tuple[str, dict]] = []

    class RecordingGenerator:
        def generate(self, text, **kwargs):
            calls.append((text, kwargs))
            return "Mean age in the cohort"

    store = ConversationHistoryStore(tmp_path / "history.db")
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda *args, **kwargs: None,
        default_runtime_settings={},
        models=[],
        runtime_root=tmp_path / "runtime",
        history_store=store,
        title_generator=RecordingGenerator(),
    )
    thread = SimpleNamespace(
        model_available=True,
        locked=False,
        settings=SimpleNamespace(model_name="m", max_steps=1, timeout_seconds=1),
    )
    runtime._require_owned_thread = lambda *args: thread
    runtime._thread = lambda *args, **kwargs: thread
    runtime._ensure_graph = lambda *args, **kwargs: None
    app = SimpleNamespace(get_state=lambda *a, **k: SimpleNamespace(values={"x": 1}, interrupts=[]))
    runner = SimpleNamespace(start_background_from_factory=lambda **kwargs: True)
    runtime._bound_graph = lambda *args: (app, runner)

    class InlineExecutor:
        def submit(self, fn, *args):
            fn(*args)

    runtime._title_executor = InlineExecutor()
    ids: list[str] = []
    if with_file:
        staged = runtime.stage_attachments(
            identity, "thread-1", [("study.csv", "text/csv", b"id,age\n1,40\n2,50\n")]
        )
        ids = [a.id for a in staged.attachments]
    titles = []
    for i, text in enumerate(messages):
        runtime.submit_message(identity, "thread-1", text, ids if i == 0 else [])
        if rename is not None and i == 0:
            store.rename(identity.owner_user_id, "thread-1", rename)
        titles.append(store.get(identity.owner_user_id, "thread-1").title)
    return calls, titles


def test_a_new_conversation_is_called_new_chat(tmp_path) -> None:
    store = ConversationHistoryStore(tmp_path / "history.db")
    record, _created = store.create_pending("owner", "t", model_name="m")
    assert record.title == "New chat"


def test_the_title_is_the_first_typed_message_and_no_model_writes_it(tmp_path) -> None:
    for with_file in (True, False):
        calls, titles = _submit(tmp_path / str(with_file), ["What is the mean age?"], with_file=with_file)
        assert calls == []  # the title model is never called, with or without files
        assert titles == ["What is the mean age?"]


def test_a_long_first_message_gives_its_first_ten_words(tmp_path) -> None:
    text = "Please compare the mean systolic blood pressure between men and women at baseline in the cohort"
    _calls, titles = _submit(tmp_path, [text], with_file=True)
    assert titles == ["Please compare the mean systolic blood pressure between men and"]


def test_a_message_of_files_only_keeps_new_chat_until_the_user_types(tmp_path) -> None:
    calls, titles = _submit(tmp_path, ["", "How many participants smoke?"], with_file=True)
    assert calls == []
    assert titles == ["New chat", "How many participants smoke?"]


def test_later_messages_and_a_manual_rename_keep_the_title(tmp_path) -> None:
    _calls, titles = _submit(tmp_path, ["What is the mean age?", "And the median?"], with_file=False)
    assert titles == ["What is the mean age?", "What is the mean age?"]
    _calls, titles = _submit(tmp_path / "r", ["What is the mean age?", "And the median?"], with_file=False,
                             rename="My cohort")
    assert titles == ["My cohort", "My cohort"]


def test_a_message_of_only_punctuation_keeps_new_chat(tmp_path) -> None:
    calls, titles = _submit(tmp_path, [";", "  .  ", "Mean age by sex"], with_file=False)
    assert calls == []
    assert titles == ["New chat", "New chat", "Mean age by sex"]


def test_an_old_untitled_conversation_row_is_named_by_the_typed_message(tmp_path) -> None:
    store = ConversationHistoryStore(tmp_path / "history.db")
    store.create_pending("owner", "t", model_name="m")
    with store._connect() as connection:
        connection.execute("UPDATE conversation_history SET title = 'Untitled conversation'")
    store.set_initial_automatic_title("owner", "t", "Mean age by sex")
    assert store.get("owner", "t").title == "Mean age by sex"


def test_a_manual_title_that_reads_untitled_conversation_is_kept(tmp_path) -> None:
    store = ConversationHistoryStore(tmp_path / "history.db")
    store.create_pending("owner", "t", model_name="m")
    store.rename("owner", "t", "Untitled conversation")
    store.set_initial_automatic_title("owner", "t", "Mean age by sex")
    assert store.get("owner", "t").title == "Untitled conversation"
