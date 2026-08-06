from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

import pytest

from run_fastapi import (
    StartupConfigurationError,
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
    assert environ["REPORT_AGENT_AUTH_MODE"] == "local"


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


def test_prepare_provider_credentials_verifies_only_local_mode() -> None:
    import run_fastapi

    verifier = Mock()

    run_fastapi.prepare_provider_credentials(
        {"REPORT_AGENT_AUTH_MODE": "local", "OPENAI_API_KEY": "key"},
        verifier=verifier,
    )
    verifier.assert_called_once()
    verifier.reset_mock()

    run_fastapi.prepare_provider_credentials(
        {
            "REPORT_AGENT_AUTH_MODE": "cognito",
            "REPORT_AGENT_AWS_REGION": "us-east-1",
            "REPORT_AGENT_COGNITO_USER_POOL_ID": "us-east-1_example",
            "REPORT_AGENT_COGNITO_APP_CLIENT_ID": "client-123",
            "REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT": "https://auth.example/logout",
            "REPORT_AGENT_AUTH_REDIRECT_URI": "https://demo.example/callback",
            "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": "https://demo.example/",
        },
        verifier=verifier,
    )

    verifier.assert_not_called()


def test_prepare_provider_credentials_rejects_invalid_mode() -> None:
    import run_fastapi

    with pytest.raises(
        StartupConfigurationError,
        match="REPORT_AGENT_AUTH_MODE must be 'local' or 'cognito'",
    ):
        run_fastapi.prepare_provider_credentials(
            {"REPORT_AGENT_AUTH_MODE": "shared"},
            verifier=Mock(),
        )


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
    environ = {
        "OPENAI_MODEL": "gpt-5.4",
        "REPORT_AGENT_ALLOWED_MODELS": (
            "gpt-5.4,gpt-5.6-luna,gpt-5.6-terra,gpt-5.6-sol"
        ),
        "REPORT_AGENT_TITLE_MODEL": "gpt-5.6-luna",
    }

    assert configured_openai_models(environ) == (
        "gpt-5.4",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    )
    assert configured_title_model(environ) == "gpt-5.6-luna"


def test_configured_models_reject_removed_gpt55() -> None:
    with pytest.raises(ValueError, match="not an allowed application model"):
        configured_openai_models(
            {
                "OPENAI_MODEL": "gpt-5.4",
                "REPORT_AGENT_ALLOWED_MODELS": "gpt-5.4,gpt-5.5",
            }
        )


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


def test_configured_models_reject_disallowed_default() -> None:
    with pytest.raises(ValueError, match="OPENAI_MODEL"):
        configured_openai_models(
            {
                "OPENAI_MODEL": "gpt-5.6-sol",
                "REPORT_AGENT_ALLOWED_MODELS": "gpt-5.6-terra",
            }
        )


def test_validate_startup_requires_openai_api_key(tmp_path: Path) -> None:
    (tmp_path / "frontend" / "dist").mkdir(parents=True)
    (tmp_path / "frontend" / "dist" / "index.html").write_text(
        "<!doctype html>",
        encoding="utf-8",
    )

    with pytest.raises(
        StartupConfigurationError,
        match="OPENAI_API_KEY",
    ):
        validate_startup(
            project_root=tmp_path,
            environ={},
            python_version=(3, 12),
        )


def test_validate_startup_cognito_does_not_require_server_openai_key(
    tmp_path: Path,
) -> None:
    static_dir = tmp_path / "frontend" / "dist"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")

    validate_startup(
        project_root=tmp_path,
        environ={
            "REPORT_AGENT_AUTH_MODE": "cognito",
            "REPORT_AGENT_AWS_REGION": "us-east-1",
            "REPORT_AGENT_COGNITO_USER_POOL_ID": "us-east-1_example",
            "REPORT_AGENT_COGNITO_APP_CLIENT_ID": "client-123",
            "REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT": "https://auth.example/logout",
            "REPORT_AGENT_AUTH_REDIRECT_URI": "https://demo.example/callback",
            "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": "https://demo.example/",
        },
        python_version=(3, 12),
    )


def test_validate_startup_cognito_requires_complete_configuration(
    tmp_path: Path,
) -> None:
    static_dir = tmp_path / "frontend" / "dist"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")

    with pytest.raises(
        StartupConfigurationError,
        match="REPORT_AGENT_COGNITO_APP_CLIENT_ID",
    ):
        validate_startup(
            project_root=tmp_path,
            environ={
                "REPORT_AGENT_AUTH_MODE": "cognito",
                "REPORT_AGENT_AWS_REGION": "us-east-1",
                "REPORT_AGENT_COGNITO_USER_POOL_ID": "us-east-1_example",
                "REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT": "https://auth.example/logout",
                "REPORT_AGENT_AUTH_REDIRECT_URI": "https://demo.example/callback",
                "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": "https://demo.example/",
            },
            python_version=(3, 12),
        )


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("WEB_CONCURRENCY", "2"),
        ("REPORT_AGENT_WEB_CONCURRENCY", "4"),
        ("WEB_CONCURRENCY", "many"),
    ],
)
def test_validate_startup_cognito_requires_one_worker(
    tmp_path: Path,
    variable: str,
    value: str,
) -> None:
    static_dir = tmp_path / "frontend" / "dist"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")

    with pytest.raises(StartupConfigurationError, match="one worker"):
        validate_startup(
            project_root=tmp_path,
            environ={
                "REPORT_AGENT_AUTH_MODE": "cognito",
                "REPORT_AGENT_AWS_REGION": "us-east-1",
                "REPORT_AGENT_COGNITO_USER_POOL_ID": "us-east-1_example",
                "REPORT_AGENT_COGNITO_APP_CLIENT_ID": "client-123",
                "REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT": "https://auth.example/logout",
                "REPORT_AGENT_AUTH_REDIRECT_URI": "https://demo.example/callback",
                "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": "https://demo.example/",
                variable: value,
            },
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
        "ensure_active_provider_credential",
        lambda: (_ for _ in ()).throw(
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
        "ensure_active_provider_credential",
        lambda: events.append("credentials"),
    )
    monkeypatch.setattr(
        run_fastapi,
        "validate_startup",
        lambda: events.append("startup"),
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
        "uvicorn:api.app:app:0.0.0.0:9000:info",
    ]
