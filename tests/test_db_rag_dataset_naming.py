from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_rag.service import dataset_naming


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = type("Msg", (), {"content": content})()


class _FakeCompletions:
    def __init__(self, owner: type["_FakeOpenAI"]) -> None:
        self._owner = owner

    def create(self, **kwargs):
        self._owner.last_create_kwargs = kwargs
        return type("Resp", (), {"choices": [_FakeChoice(self._owner.response_content)]})()


class _FakeChat:
    def __init__(self, owner: type["_FakeOpenAI"]) -> None:
        self.completions = _FakeCompletions(owner)


class _FakeOpenAI:
    response_content = ""
    last_init_kwargs: dict[str, object] | None = None
    last_create_kwargs: dict[str, object] | None = None

    def __init__(self, **kwargs) -> None:
        type(self).last_init_kwargs = kwargs
        self.chat = _FakeChat(type(self))


def test_generate_dataset_name_uses_lightweight_llm(monkeypatch) -> None:
    monkeypatch.delenv("DB_RAG_DATASET_NAMING_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _FakeOpenAI.response_content = '{"name":"TB outcome and HIV diabetes status"}'
    _FakeOpenAI.last_init_kwargs = None
    _FakeOpenAI.last_create_kwargs = None
    monkeypatch.setattr(dataset_naming, "_resolve_openai_client", lambda: _FakeOpenAI)

    name = dataset_naming.generate_dataset_name(
        goal_text="Query my database, what variables related to tb outcome, hiv status, and diabetes status",
        source_question="",
        columns=[{"table": "Final Outcome", "column": "FOA_COHAOUT", "description": "TB outcome status"}],
        api_key="test-key",
        resolve_model=lambda: "gpt-5.6-luna",
    )

    assert name == "TB outcome and HIV diabetes status"
    assert _FakeOpenAI.last_init_kwargs == {"api_key": "test-key"}
    assert _FakeOpenAI.last_create_kwargs["model"] == "gpt-5.6-luna"
    messages = _FakeOpenAI.last_create_kwargs["messages"]
    assert "Generate a concise human-readable name" in messages[0]["content"]
    assert "tb outcome" in messages[1]["content"].lower()


def test_generate_dataset_name_does_not_fall_back_to_environment_key(monkeypatch) -> None:
    monkeypatch.setenv("DB_RAG_DATASET_NAMING_API_KEY", "must-not-be-used")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")

    name = dataset_naming.generate_dataset_name(
        goal_text="Create a household-contact dataset with age, sex, household size, TST result, and IGRA result.",
        resolve_model=lambda: "gpt-5.6-luna",
    )

    assert name == "Household Contact Dataset"


def test_generate_dataset_name_ignores_overlong_model_name(monkeypatch) -> None:
    monkeypatch.delenv("DB_RAG_DATASET_NAMING_API_KEY", raising=False)
    _FakeOpenAI.response_content = (
        '{"name":"Create a household-contact dataset with age, sex, household size, TST result, and IGRA result"}'
    )
    monkeypatch.setattr(dataset_naming, "_resolve_openai_client", lambda: _FakeOpenAI)

    name = dataset_naming.generate_dataset_name(
        goal_text="Create a household-contact dataset with age and sex.",
        api_key="test-key",
        resolve_model=lambda: "gpt-5.6-luna",
    )

    assert name == "Household Contact Dataset"
