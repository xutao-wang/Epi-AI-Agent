from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

import pytest
from dotenv import dotenv_values

from run_fastapi import (
    StartupConfigurationError,
    configure_and_verify_providers,
    configure_native_runtime,
    ensure_active_provider_credential,
    normalize_secret_input,
    parse_args,
    prepare_environment,
    validate_startup,
)
from utils.provider_startup import ProviderCredentialError
from utils.runtime_defaults import (
    DEFAULT_EPI_AGENT_MAX_ITERATIONS,
    DEFAULT_OPENAI_MODEL,
    configured_epi_agent_max_iterations,
    configured_openai_models,
    configured_title_model,
)


def test_default_openai_model_is_gpt56_terra() -> None:
    assert DEFAULT_OPENAI_MODEL == "gpt-5.6-terra"


def test_prepare_environment_sets_project_local_defaults(tmp_path: Path) -> None:
    environ: dict[str, str] = {}

    prepare_environment(project_root=tmp_path, environ=environ)

    assert environ["REPORT_AGENT_STATIC_DIR"] == str(
        (tmp_path / "frontend" / "dist").resolve()
    )
    assert environ["REPORT_AGENT_RUNTIME_ROOT"] == str(
        (tmp_path / "runtime").resolve()
    )
    assert environ["REPORT_AGENT_STUDY_ROOT"] == str(
        (tmp_path / "study_data").resolve()
    )
    assert environ["REPORT_AGENT_CHECKPOINT_DB_PATH"] == str(
        (tmp_path / "runtime" / "agent_memory_fastapi.db").resolve()
    )
    assert "REPORT_AGENT_AUTH_MODE" not in environ


def test_prepare_environment_preserves_explicit_paths(tmp_path: Path) -> None:
    environ = {
        "REPORT_AGENT_STATIC_DIR": "/configured/static",
        "REPORT_AGENT_RUNTIME_ROOT": "/configured/runtime",
        "REPORT_AGENT_STUDY_ROOT": "/configured/studies",
        "REPORT_AGENT_CHECKPOINT_DB_PATH": "/configured/checkpoints.db",
    }

    prepare_environment(project_root=tmp_path, environ=environ)

    assert environ["REPORT_AGENT_STATIC_DIR"] == "/configured/static"
    assert environ["REPORT_AGENT_RUNTIME_ROOT"] == "/configured/runtime"
    assert environ["REPORT_AGENT_STUDY_ROOT"] == "/configured/studies"
    assert (
        environ["REPORT_AGENT_CHECKPOINT_DB_PATH"]
        == "/configured/checkpoints.db"
    )


def test_prepare_provider_credentials_always_verifies_local_key() -> None:
    import run_fastapi

    environ = {"OPENAI_API_KEY": "key"}
    verifier = Mock()

    run_fastapi.prepare_provider_credentials(
        environ,
        verifier=verifier,
    )

    verifier.assert_called_once_with(environ=environ)


def test_configure_native_runtime_uses_project_runtime_after_default_confirmation(
    tmp_path: Path,
) -> None:
    environ: dict[str, str] = {}

    selected = configure_native_runtime(
        project_root=tmp_path,
        environ=environ,
        input_fn=lambda _prompt: "",
        choose_directory=lambda: None,
        persist=False,
    )

    assert selected == (tmp_path / "runtime").resolve()
    assert selected.is_dir()
    assert environ["REPORT_AGENT_RUNTIME_ROOT"] == str(selected)
    assert environ["REPORT_AGENT_CHECKPOINT_DB_PATH"] == str(
        selected / "agent_memory_fastapi.db"
    )


def test_configure_native_runtime_creates_confirmed_custom_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "Research" / "RePORT data"
    answers = iter(["2", "y"])

    selected = configure_native_runtime(
        project_root=tmp_path,
        environ={},
        input_fn=lambda _prompt: next(answers),
        choose_directory=lambda: target,
        persist=False,
    )

    assert selected == target.resolve()
    assert selected.is_dir()


def test_configure_native_runtime_rejects_unconfirmed_missing_custom_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "Research" / "RePORT data"
    answers = iter(["2", "n"])

    with pytest.raises(StartupConfigurationError, match="not created"):
        configure_native_runtime(
            project_root=tmp_path,
            environ={},
            input_fn=lambda _prompt: next(answers),
            choose_directory=lambda: target,
            persist=False,
        )


def test_configured_models_offer_only_profiled_models() -> None:
    environ = {"OPENAI_API_KEY": "configured"}

    assert configured_openai_models(environ) == (
        "gpt-5.4",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    )
    assert configured_title_model(environ) == "gpt-5.6-luna"


def test_epi_agent_max_iterations_defaults_to_fifty() -> None:
    assert DEFAULT_EPI_AGENT_MAX_ITERATIONS == 50
    assert configured_epi_agent_max_iterations({}) == 50


def test_epi_agent_max_iterations_uses_environment_override() -> None:
    assert configured_epi_agent_max_iterations(
        {"REPORT_AGENT_MAX_ITERATIONS": "64"}
    ) == 64


@pytest.mark.parametrize(
    "configured",
    ["", "0", "-1", "2.5", "many"],
)
def test_epi_agent_max_iterations_rejects_invalid_values(
    configured: str,
) -> None:
    with pytest.raises(
        ValueError,
        match="REPORT_AGENT_MAX_ITERATIONS must be a positive integer",
    ):
        configured_epi_agent_max_iterations(
            {"REPORT_AGENT_MAX_ITERATIONS": configured}
        )


def test_validate_startup_requires_a_configured_provider(tmp_path: Path) -> None:
    (tmp_path / "frontend" / "dist").mkdir(parents=True)
    (tmp_path / "frontend" / "dist" / "index.html").write_text(
        "<!doctype html>",
        encoding="utf-8",
    )

    with pytest.raises(
        StartupConfigurationError,
        match="No verified AI model provider",
    ):
        validate_startup(
            project_root=tmp_path,
            environ={},
            python_version=(3, 12),
        )


def test_validate_startup_requires_built_frontend(tmp_path: Path) -> None:
    with pytest.raises(
        StartupConfigurationError,
        match=r"frontend/dist/index\.html",
    ):
        validate_startup(
            project_root=tmp_path,
            environ={"OPENAI_API_KEY": "test-key"},
            python_version=(3, 12),
        )


def test_validate_startup_requires_python_3_12(tmp_path: Path) -> None:
    (tmp_path / "frontend" / "dist").mkdir(parents=True)
    (tmp_path / "frontend" / "dist" / "index.html").write_text(
        "<!doctype html>",
        encoding="utf-8",
    )

    with pytest.raises(
        StartupConfigurationError,
        match=r"Python 3\.12",
    ):
        validate_startup(
            project_root=tmp_path,
            environ={"OPENAI_API_KEY": "test-key"},
            python_version=(3, 13),
        )


def test_parse_args_does_not_import_api_app(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "api.app", raising=False)

    args = parse_args(["--host", "0.0.0.0", "--port", "9000"])

    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert "api.app" not in sys.modules


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" sk-example ", "sk-example"),
        ('"sk-example"', "sk-example"),
        ("'sk-example'", "sk-example"),
        ('"sk-example', '"sk-example'),
    ],
)
def test_normalize_secret_input(value: str, expected: str) -> None:
    assert normalize_secret_input(value) == expected


def test_existing_openai_key_exposes_only_registered_gpt_models(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, str | None]] = []
    persisted: list[dict[str, str]] = []
    environ = {"OPENAI_API_KEY": "openai-key"}

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        verifier=lambda provider, key, *, base_url=None: calls.append(
            (provider, key, base_url)
        ),
        persist=lambda _root, values: persisted.append(values),
        input_fn=lambda _prompt: pytest.fail("provider menu should not open"),
        getpass_fn=lambda _prompt: pytest.fail("key prompt should not open"),
        output_fn=lambda _message: None,
    )

    assert calls == [("openai", "openai-key", None)]
    assert catalog.available_model_ids == (
        "gpt-5.4",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    )
    assert persisted == []


def test_existing_anthropic_key_exposes_only_registered_claude_models(
    tmp_path: Path,
) -> None:
    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ={"ANTHROPIC_API_KEY": "anthropic-key"},
        verifier=lambda _provider, _key, **_kwargs: None,
        persist=lambda _root, _values: None,
        input_fn=lambda _prompt: pytest.fail("provider menu should not open"),
        getpass_fn=lambda _prompt: pytest.fail("key prompt should not open"),
        output_fn=lambda _message: None,
    )

    assert catalog.available_model_ids == (
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    )


def test_first_run_menu_and_key_prompt_are_unambiguous(tmp_path: Path) -> None:
    menu_prompts: list[str] = []
    key_prompts: list[str] = []

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ={},
        input_fn=lambda prompt: menu_prompts.append(prompt) or "1",
        getpass_fn=lambda prompt: key_prompts.append(prompt) or "openai-key",
        verifier=lambda _provider, _key, **_kwargs: None,
        persist=lambda _root, _values: None,
        output_fn=lambda _message: None,
    )

    assert menu_prompts == [
        "No AI provider is configured.\n\n"
        "1. Configure OpenAI\n"
        "2. Configure Anthropic\n"
        "3. Connect to a compatible endpoint\n"
        "Selection: "
    ]
    assert key_prompts == [
        "Paste your OpenAI API key and press Enter to validate\n"
        "(empty + Enter returns to provider setup): "
    ]
    assert "gpt-5.6-terra" in catalog.available_model_ids


def test_reconfigure_menu_has_independent_provider_actions(tmp_path: Path) -> None:
    prompts: list[str] = []

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ={"OPENAI_API_KEY": "openai-key"},
        input_fn=lambda prompt: prompts.append(prompt) or "6",
        getpass_fn=lambda _prompt: pytest.fail("key prompt should not open"),
        verifier=lambda _provider, _key, **_kwargs: None,
        persist=lambda _root, _values: None,
        output_fn=lambda _message: None,
        force=True,
    )

    assert prompts == [
        "Configure AI providers. Existing providers are retained.\n\n"
        "1. Configure or replace OpenAI\n"
        "2. Configure or replace Anthropic\n"
        "3. Connect to a compatible endpoint\n"
        "4. Remove OpenAI\n"
        "5. Remove Anthropic\n"
        "6. Keep current providers\n"
        "Selection [6]: "
    ]
    assert "gpt-5.6-terra" in catalog.available_model_ids


def test_empty_key_returns_to_provider_setup(tmp_path: Path) -> None:
    menu_prompts: list[str] = []
    keys = iter(["", "anthropic-key"])

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ={},
        input_fn=lambda prompt: menu_prompts.append(prompt) or "2",
        getpass_fn=lambda _prompt: next(keys),
        verifier=lambda _provider, _key, **_kwargs: None,
        persist=lambda _root, _values: None,
        output_fn=lambda _message: None,
    )

    assert len(menu_prompts) == 2
    assert "claude-opus-5" in catalog.available_model_ids


@pytest.mark.parametrize(
    ("selection", "provider", "key", "expected_model", "expected_saved"),
    [
        (
            "1",
            "OpenAI",
            "openai-key",
            "gpt-5.6-terra",
            {"OPENAI_API_KEY": "openai-key"},
        ),
        (
            "2",
            "Anthropic",
            "anthropic-key",
            "claude-opus-5",
            {"ANTHROPIC_API_KEY": "anthropic-key"},
        ),
    ],
)
def test_no_keys_can_configure_one_provider_independently(
    tmp_path: Path,
    selection: str,
    provider: str,
    key: str,
    expected_model: str,
    expected_saved: dict[str, str],
) -> None:
    saved: list[dict[str, str]] = []
    key_prompts: list[str] = []

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ={},
        input_fn=lambda _prompt: selection,
        getpass_fn=lambda prompt: key_prompts.append(prompt) or key,
        verifier=lambda _provider, _key, **_kwargs: None,
        persist=lambda _root, values: saved.append(values),
        output_fn=lambda _message: None,
    )

    assert saved == [expected_saved]
    assert expected_model in catalog.available_model_ids
    assert key_prompts == [
        f"Paste your {provider} API key and press Enter to validate\n"
        "(empty + Enter returns to provider setup): "
    ]


def test_failed_compatible_endpoint_is_omitted_when_builtin_provider_works(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "custom_models.json"
    registry.write_text(
        '[{"id":"cluster-model","base_url":"https://llm.internal/v1"}]',
        encoding="utf-8",
    )
    environ = {
        "OPENAI_API_KEY": "openai-key",
        "REPORT_AGENT_CUSTOM_MODELS_PATH": str(registry),
    }

    def verifier(_provider: str, _key: str, *, base_url=None) -> None:
        if base_url:
            raise ProviderCredentialError("network", "endpoint unavailable")

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        verifier=verifier,
        persist=lambda _root, _values: None,
        input_fn=lambda _prompt: "3",
        getpass_fn=lambda _prompt: pytest.fail("key prompt should not open"),
        output_fn=lambda _message: None,
    )

    assert "gpt-5.6-terra" in catalog.available_model_ids
    assert "cluster-model" not in catalog.available_model_ids


def test_native_runtime_persists_root_but_not_derived_checkpoint(
    tmp_path: Path,
) -> None:
    selected = configure_native_runtime(
        project_root=tmp_path,
        environ={},
        input_fn=lambda _prompt: "",
        choose_directory=lambda: None,
        persist=True,
    )

    saved = dotenv_values(tmp_path / ".env")
    assert saved["REPORT_AGENT_RUNTIME_ROOT"] == str(selected)
    assert "REPORT_AGENT_CHECKPOINT_DB_PATH" not in saved


def test_existing_verified_key_does_not_prompt_or_persist(tmp_path: Path) -> None:
    prompts: list[str] = []
    persisted: list[dict[str, str]] = []
    output: list[str] = []

    ensure_active_provider_credential(
        project_root=tmp_path,
        environ={"REPORT_AGENT_PROVIDER": "openai", "OPENAI_API_KEY": "saved"},
        getpass_fn=lambda prompt: prompts.append(prompt) or pytest.fail(
            "unexpected prompt"
        ),
        verifier=lambda provider, key: (
            (provider, key) == ("openai", "saved")
            or pytest.fail("wrong key")
        ),
        persist=lambda _root, values: persisted.append(values),
        output_fn=output.append,
    )

    assert prompts == []
    assert persisted == []
    assert output == ["OpenAI API key verified."]


def test_missing_key_retries_then_persists_only_verified_normalized_key(
    tmp_path: Path,
) -> None:
    answers = iter(['"bad"', "'good'"])
    checked: list[tuple[str, str]] = []
    persisted: list[dict[str, str]] = []
    output: list[str] = []

    def verifier(provider: str, key: str) -> None:
        checked.append((provider, key))
        if key == "bad":
            raise ProviderCredentialError("authentication", "rejected")

    ensure_active_provider_credential(
        project_root=tmp_path,
        environ={"REPORT_AGENT_PROVIDER": "openai"},
        getpass_fn=lambda _prompt: next(answers),
        verifier=verifier,
        persist=lambda _root, values: persisted.append(values),
        output_fn=output.append,
    )

    assert checked == [("openai", "bad"), ("openai", "good")]
    assert persisted == [{"OPENAI_API_KEY": "good"}]
    assert all("bad" not in message and "good" not in message for message in output)
    assert output[-1] == "OpenAI API key verified and saved to .env."


def test_missing_key_prompts_to_continue(tmp_path: Path) -> None:
    prompts: list[str] = []

    ensure_active_provider_credential(
        project_root=tmp_path,
        environ={"REPORT_AGENT_PROVIDER": "openai"},
        getpass_fn=lambda prompt: prompts.append(prompt) or "valid-key",
        verifier=lambda _provider, _key: None,
        persist=lambda _root, _values: None,
        output_fn=lambda _message: None,
    )

    assert prompts == ["Paste your OpenAI API key (press Enter to continue): "]


def test_failed_saved_key_is_replaced_only_after_successful_check(
    tmp_path: Path,
) -> None:
    persisted: list[dict[str, str]] = []
    output: list[str] = []

    def verifier(_provider: str, key: str) -> None:
        if key == "expired":
            raise ProviderCredentialError("authentication", "rejected")

    ensure_active_provider_credential(
        project_root=tmp_path,
        environ={"REPORT_AGENT_PROVIDER": "openai", "OPENAI_API_KEY": "expired"},
        getpass_fn=lambda _prompt: "replacement",
        verifier=verifier,
        persist=lambda _root, values: persisted.append(values),
        output_fn=output.append,
    )

    assert persisted == [{"OPENAI_API_KEY": "replacement"}]
    assert all("expired" not in message for message in output)
    assert all("replacement" not in message for message in output)


@pytest.mark.parametrize(
    "getpass_fn",
    [
        lambda _prompt: "",
        lambda _prompt: (_ for _ in ()).throw(EOFError()),
        lambda _prompt: (_ for _ in ()).throw(KeyboardInterrupt()),
    ],
)
def test_cancelled_key_setup_never_persists(
    tmp_path: Path,
    getpass_fn,
) -> None:
    persisted: list[dict[str, str]] = []

    with pytest.raises(StartupConfigurationError, match="cancelled"):
        ensure_active_provider_credential(
            project_root=tmp_path,
            environ={"REPORT_AGENT_PROVIDER": "openai"},
            getpass_fn=getpass_fn,
            verifier=lambda _provider, _key: pytest.fail("unexpected check"),
            persist=lambda _root, values: persisted.append(values),
            output_fn=lambda _message: None,
        )

    assert persisted == []


def test_unsupported_provider_does_not_prompt_or_persist(tmp_path: Path) -> None:
    persisted: list[dict[str, str]] = []

    with pytest.raises(StartupConfigurationError, match="provider"):
        ensure_active_provider_credential(
            project_root=tmp_path,
            environ={"REPORT_AGENT_PROVIDER": "anthropic"},
            getpass_fn=lambda _prompt: pytest.fail("unexpected prompt"),
            verifier=lambda _provider, _key: pytest.fail("unexpected check"),
            persist=lambda _root, values: persisted.append(values),
            output_fn=lambda _message: None,
        )

    assert persisted == []


def test_main_does_not_start_uvicorn_when_credential_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_fastapi

    started: list[str] = []
    monkeypatch.setattr(run_fastapi, "load_app_environment", lambda _root: None)
    monkeypatch.setattr(run_fastapi, "configure_native_runtime", lambda: None)
    monkeypatch.setattr(run_fastapi, "prepare_environment", lambda: None)
    monkeypatch.setattr(
        run_fastapi,
        "configure_and_verify_providers",
        lambda **_kwargs: (_ for _ in ()).throw(
            StartupConfigurationError("OpenAI API key setup was cancelled.")
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(run=lambda **_kwargs: started.append("run")),
    )

    assert run_fastapi.main([]) == 2
    assert started == []


def test_main_verifies_credentials_before_starting_uvicorn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_fastapi

    events: list[str] = []
    catalog = SimpleNamespace(default_model_id="gpt-5.6-terra")
    application = object()
    monkeypatch.setenv("REPORT_AGENT_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr(
        run_fastapi,
        "load_app_environment",
        lambda _root: events.append("load"),
    )
    monkeypatch.setattr(
        run_fastapi,
        "configure_native_runtime",
        lambda: events.append("runtime"),
    )
    monkeypatch.setattr(
        run_fastapi,
        "prepare_environment",
        lambda: events.append("environment"),
    )
    monkeypatch.setattr(
        run_fastapi,
        "configure_and_verify_providers",
        lambda **_kwargs: events.append("credentials") or catalog,
    )
    monkeypatch.setattr(
        run_fastapi,
        "validate_startup",
        lambda **kwargs: (
            kwargs["model_availability"] is catalog
            or pytest.fail("catalog not validated")
        ) and events.append("startup"),
    )
    monkeypatch.setitem(
        sys.modules,
        "api.app",
        SimpleNamespace(
            build_application=lambda **kwargs: (
                kwargs["model_availability"] is catalog
                or pytest.fail("catalog not injected")
            ) and application
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(
            run=lambda app, host, port, log_level: events.append(
                f"uvicorn:{app}:{host}:{port}:{log_level}"
            )
        ),
    )

    assert run_fastapi.main(["--host", "0.0.0.0", "--port", "9000"]) == 0
    assert events == [
        "load",
        "runtime",
        "environment",
        "credentials",
        "startup",
        f"uvicorn:{application}:0.0.0.0:9000:info",
    ]
