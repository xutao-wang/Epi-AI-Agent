from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from api.deployment import (
    native_checkpoint_db_path,
    native_runtime_root,
    native_static_dir,
    native_study_root,
)
from utils.env_loader import load_app_environment, persist_local_env_values
from utils.model_runtime_profiles import (
    ModelRuntimeProfile,
    PROVIDER_OPENAI_COMPATIBLE,
    configured_model_profiles,
    load_custom_model_profiles,
    model_runtime_profile,
)
from utils.provider_startup import (
    ProviderCredentialError,
    verify_active_provider,
    verify_provider_credential,
)
from utils.runtime_defaults import configured_models, configured_title_model


PROJECT_ROOT = Path(__file__).resolve().parent


class StartupConfigurationError(RuntimeError):
    """A startup problem the participant can correct without a traceback."""


def required_secret_names(
    profiles: Sequence[ModelRuntimeProfile],
) -> tuple[str, ...]:
    """Return required local environment keys for configured model profiles."""
    names: dict[str, None] = {}
    for profile in profiles:
        if profile.api_key_required and profile.api_key_env:
            names.setdefault(profile.api_key_env)
    return tuple(names)


def normalize_secret_input(value: str) -> str:
    normalized = str(value or "").strip()
    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {"'", '"'}
    ):
        return normalized[1:-1].strip()
    return normalized


@dataclass(frozen=True)
class ProviderCredentialRequirement:
    provider: str
    label: str
    api_key_env: str
    key_required: bool
    base_url: str | None = None


def _configured_profiles(
    environ: MutableMapping[str, str],
) -> tuple[ModelRuntimeProfile, ...]:
    # Raises ValueError early when the default model is not allowlisted,
    # inside main()'s guarded startup phase instead of at api.app import.
    configured_models(environ)
    profiles = [model_runtime_profile(model) for model in configured_models(environ)]
    profiles.append(model_runtime_profile(configured_title_model(environ)))
    return tuple(profiles)


def provider_credential_requirements(
    environ: MutableMapping[str, str] = os.environ,
) -> tuple[ProviderCredentialRequirement, ...]:
    """One credential requirement per provider/endpoint backing configured models."""
    requirements: dict[tuple[str, str, str | None], ProviderCredentialRequirement] = {}
    for profile in _configured_profiles(environ):
        key = (profile.provider, profile.api_key_env, profile.base_url)
        if key in requirements:
            continue
        requirements[key] = ProviderCredentialRequirement(
            provider=profile.provider,
            label=profile.provider_label,
            api_key_env=profile.api_key_env,
            key_required=profile.api_key_required,
            base_url=profile.base_url,
        )
    return tuple(requirements.values())


def _verify_requirement(
    requirement: ProviderCredentialRequirement,
    api_key: str,
    verifier: Callable[..., None],
) -> None:
    verifier(
        requirement.provider,
        api_key,
        base_url=requirement.base_url,
    )


_PROVIDER_PRESETS: tuple[tuple[str, str, str], ...] = (
    (
        "OpenAI",
        "gpt-5.6-terra",
        "gpt-5.4,gpt-5.6-luna,gpt-5.6-terra,gpt-5.6-sol",
    ),
    (
        "Anthropic Claude",
        "claude-opus-5",
        "claude-opus-5,claude-sonnet-5,claude-haiku-4-5",
    ),
)


def configure_model_provider(
    *,
    project_root: str | Path = PROJECT_ROOT,
    environ: MutableMapping[str, str] = os.environ,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    persist: bool = True,
    force: bool = False,
) -> None:
    """First-run (or --reconfigure) interactive provider/model selection.

    Persists REPORT_AGENT_MODEL and REPORT_AGENT_ALLOWED_MODELS to .env so
    later starts skip the menu. Mixed-provider allowlists can still be
    hand-edited in .env or config/app.env.
    """
    configured_model = str(environ.get("REPORT_AGENT_MODEL", "") or "").strip()
    if configured_model and not force:
        return

    custom_profiles = load_custom_model_profiles(environ=environ)
    while True:
        lines = ["Choose the AI model provider:"]
        for index, (label, default_model, _models) in enumerate(
            _PROVIDER_PRESETS, start=1
        ):
            lines.append(f"{index}. {label} (default model: {default_model})")
        if custom_profiles:
            default_custom = next(iter(custom_profiles))
            lines.append(
                "3. Custom OpenAI-compatible endpoint "
                f"({len(custom_profiles)} registered, e.g. {default_custom})"
            )
        else:
            lines.append(
                "3. Custom OpenAI-compatible endpoint "
                "(none registered; see config/custom_models.example.json)"
            )
        if force and configured_model:
            lines.append(
                f"Press Enter to keep the current model ({configured_model})."
            )
            prompt_suffix = "Selection [keep current]: "
        else:
            prompt_suffix = "Selection [1]: "
        try:
            selection = input_fn("\n".join(lines) + "\n" + prompt_suffix).strip()
        except EOFError:
            # Non-interactive native start: keep configured defaults.
            return
        except KeyboardInterrupt as error:
            raise StartupConfigurationError(
                "Model provider selection was cancelled."
            ) from error

        if not selection:
            if force and configured_model:
                return
            selection = "1"
        if selection in {"1", "2"}:
            _label, model, models = _PROVIDER_PRESETS[int(selection) - 1]
        elif selection == "3":
            if not custom_profiles:
                output_fn(
                    "No custom models are registered. Copy "
                    "config/custom_models.example.json to "
                    "config/custom_models.json, edit it for your endpoint, "
                    "and restart."
                )
                continue
            custom_ids = list(custom_profiles)
            if len(custom_ids) == 1:
                model = custom_ids[0]
            else:
                listing = "\n".join(
                    f"{index}. {model_id}"
                    for index, model_id in enumerate(custom_ids, start=1)
                )
                choice = input_fn(
                    "Choose the default custom model:\n"
                    f"{listing}\nSelection [1]: "
                ).strip() or "1"
                try:
                    model = custom_ids[int(choice) - 1]
                except (IndexError, ValueError):
                    output_fn("Select one of the listed custom models.")
                    continue
            models = ",".join(custom_ids)
        else:
            output_fn("Select 1, 2, or 3.")
            continue

        environ["REPORT_AGENT_MODEL"] = model
        environ["REPORT_AGENT_ALLOWED_MODELS"] = models
        if persist:
            persist_local_env_values(
                project_root,
                {
                    "REPORT_AGENT_MODEL": model,
                    "REPORT_AGENT_ALLOWED_MODELS": models,
                },
            )
        output_fn(f"Default model set to {model} (saved to .env).")
        return


def ensure_provider_credentials(
    *,
    project_root: str | Path = PROJECT_ROOT,
    environ: MutableMapping[str, str] = os.environ,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    verifier: Callable[..., None] = verify_provider_credential,
    persist: Callable[[str | Path, dict[str, str]], None] = persist_local_env_values,
    output_fn: Callable[[str], None] = print,
) -> None:
    for requirement in provider_credential_requirements(environ):
        if requirement.provider == PROVIDER_OPENAI_COMPATIBLE:
            saved_key = normalize_secret_input(
                environ.get(requirement.api_key_env, "")
                if requirement.api_key_env
                else ""
            )
            try:
                _verify_requirement(requirement, saved_key, verifier)
            except ProviderCredentialError as error:
                # A custom endpoint that is down or misconfigured should not
                # block startup for the other providers; requests against it
                # fail with a clear runtime error instead.
                output_fn(
                    "Warning: custom endpoint check failed for "
                    f"{requirement.base_url}: {error}"
                )
            else:
                output_fn(f"Custom endpoint verified: {requirement.base_url}")
            continue

        saved_key = normalize_secret_input(environ.get(requirement.api_key_env, ""))
        if saved_key:
            try:
                _verify_requirement(requirement, saved_key, verifier)
            except ProviderCredentialError as error:
                output_fn(
                    f"Saved {requirement.label} credential check failed: {error}"
                )
            else:
                environ[requirement.api_key_env] = saved_key
                output_fn(f"{requirement.label} API key verified.")
                continue

        while True:
            prompt = (
                f"Paste your {requirement.label} API key (press Enter to continue): "
                if requirement.api_key_env == "OPENAI_API_KEY"
                else (
                    f"Paste your {requirement.label} API key "
                    f"({requirement.api_key_env}, press Enter to continue): "
                )
            )
            try:
                entered_key = normalize_secret_input(getpass_fn(prompt))
            except (EOFError, KeyboardInterrupt) as error:
                raise StartupConfigurationError(
                    f"{requirement.label} API key setup was cancelled."
                ) from error
            if not entered_key:
                raise StartupConfigurationError(
                    f"{requirement.label} API key setup was cancelled."
                )

            try:
                _verify_requirement(requirement, entered_key, verifier)
            except ProviderCredentialError as error:
                output_fn(f"{requirement.label} credential check failed: {error}")
                continue

            environ[requirement.api_key_env] = entered_key
            persist(project_root, {requirement.api_key_env: entered_key})
            output_fn(
                f"{requirement.label} API key verified and saved to .env."
            )
            break


def ensure_active_provider_credential(
    *,
    project_root: str | Path = PROJECT_ROOT,
    environ: MutableMapping[str, str] = os.environ,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    verifier: Callable[[str, str], None] = verify_active_provider,
    persist: Callable[[str | Path, dict[str, str]], None] = persist_local_env_values,
    output_fn: Callable[[str], None] = print,
) -> None:
    """Verify the legacy OpenAI-only startup configuration."""
    provider = str(
        environ.get("REPORT_AGENT_PROVIDER", "openai") or ""
    ).strip().lower()
    if provider != "openai":
        raise StartupConfigurationError(
            "Startup verification is not configured for the active provider."
        )

    saved_key = normalize_secret_input(environ.get("OPENAI_API_KEY", ""))
    if saved_key:
        try:
            verifier(provider, saved_key)
        except ProviderCredentialError as error:
            output_fn(f"Saved OpenAI credential check failed: {error}")
        else:
            environ["OPENAI_API_KEY"] = saved_key
            output_fn("OpenAI API key verified.")
            return

    while True:
        try:
            entered_key = normalize_secret_input(
                getpass_fn("Paste your OpenAI API key (press Enter to continue): ")
            )
        except (EOFError, KeyboardInterrupt) as error:
            raise StartupConfigurationError(
                "OpenAI API key setup was cancelled."
            ) from error
        if not entered_key:
            raise StartupConfigurationError(
                "OpenAI API key setup was cancelled."
            )

        try:
            verifier(provider, entered_key)
        except ProviderCredentialError as error:
            output_fn(f"OpenAI credential check failed: {error}")
            continue

        environ["OPENAI_API_KEY"] = entered_key
        persist(project_root, {"OPENAI_API_KEY": entered_key})
        output_fn("OpenAI API key verified and saved to .env.")
        return


def prepare_provider_credentials(
    environ: MutableMapping[str, str],
    *,
    verifier: Callable[..., None] = ensure_active_provider_credential,
) -> None:
    """Compatibility hook for callers of the former local-only launcher."""
    verifier(environ=environ)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start the Epidemiology Research Agent demo."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--reconfigure",
        action="store_true",
        help="Re-open the model provider selection even if one is saved.",
    )
    return parser.parse_args(argv)


def choose_native_directory() -> Path | None:
    if sys.platform != "darwin":
        return None
    result = subprocess.run(
        ["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose RePORT Agent data folder")'],
        check=False,
        capture_output=True,
        text=True,
    )
    selected = result.stdout.strip()
    return Path(selected) if result.returncode == 0 and selected else None


def _require_writable_directory(path: Path, *, create: bool) -> Path:
    resolved = path.expanduser().resolve()
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.is_dir():
        raise StartupConfigurationError(f"Data folder is not a directory: {resolved}")
    try:
        with tempfile.NamedTemporaryFile(dir=resolved, delete=True):
            pass
    except OSError as exc:
        raise StartupConfigurationError(
            f"Data folder is not writable: {resolved}"
        ) from exc
    return resolved


def configure_native_runtime(
    *,
    project_root: str | Path = PROJECT_ROOT,
    environ: MutableMapping[str, str] = os.environ,
    input_fn: Callable[[str], str] = input,
    choose_directory: Callable[[], Path | None] = choose_native_directory,
    persist: bool = True,
) -> Path:
    configured = environ.get("REPORT_AGENT_RUNTIME_ROOT", "").strip()
    if configured:
        return _require_writable_directory(Path(configured), create=False)
    root = Path(project_root).resolve()
    default = root / "runtime"
    selected_option = input_fn(
        f"Choose RePORT Agent data folder:\n1. {default} (default)\n2. Choose another folder\nSelection [1]: "
    ).strip()
    target = default
    if selected_option == "2":
        target = choose_directory()
        if target is None:
            entered = input_fn("Enter an absolute data folder path: ").strip()
            if not entered:
                raise StartupConfigurationError("A data folder is required to start RePORT Agent.")
            target = Path(entered)
        if not target.exists():
            answer = input_fn(f"Create data folder {target}? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                raise StartupConfigurationError(f"Data folder was not created: {target}")
    elif selected_option not in {"", "1"}:
        raise StartupConfigurationError("Select 1 for the default folder or 2 for another folder.")
    selected = _require_writable_directory(target, create=True)
    checkpoint = selected / "agent_memory_fastapi.db"
    environ["REPORT_AGENT_RUNTIME_ROOT"] = str(selected)
    environ["REPORT_AGENT_CHECKPOINT_DB_PATH"] = str(checkpoint)
    if persist:
        persist_local_env_values(
            root,
            {
                "REPORT_AGENT_RUNTIME_ROOT": str(selected),
                "REPORT_AGENT_CHECKPOINT_DB_PATH": str(checkpoint),
            },
        )
    return selected


def prepare_environment(
    *,
    project_root: str | Path = PROJECT_ROOT,
    environ: MutableMapping[str, str] = os.environ,
) -> None:
    root = Path(project_root).resolve()
    environ.setdefault("REPORT_AGENT_STATIC_DIR", str(native_static_dir(root)))
    environ.setdefault("REPORT_AGENT_RUNTIME_ROOT", str(native_runtime_root(root)))
    environ.setdefault("REPORT_AGENT_STUDY_ROOT", str(native_study_root(root)))
    environ.setdefault(
        "REPORT_AGENT_CHECKPOINT_DB_PATH",
        str(native_checkpoint_db_path(root)),
    )


def validate_python_version(
    python_version: Sequence[int] = sys.version_info,
) -> None:
    if tuple(python_version[:2]) != (3, 12):
        found = ".".join(str(part) for part in python_version[:2])
        raise StartupConfigurationError(
            f"Python 3.12 is required; this interpreter is Python {found}. "
            "Activate the project environment first: source .venv/bin/activate"
        )


def validate_startup(
    *,
    project_root: str | Path = PROJECT_ROOT,
    environ: MutableMapping[str, str] = os.environ,
    python_version: Sequence[int] = sys.version_info,
) -> None:
    validate_python_version(python_version)

    for secret_name in required_secret_names(_configured_profiles(environ)):
        if not environ.get(secret_name, "").strip():
            if secret_name == "OPENAI_API_KEY":
                raise StartupConfigurationError(
                    "OPENAI_API_KEY is missing. Copy .env.example to .env and "
                    "add your OpenAI API key."
                )
            raise StartupConfigurationError(
                f"{secret_name} is missing. Copy .env.example to .env and add "
                "the API key for the configured provider."
            )

    configured_static = environ.get("REPORT_AGENT_STATIC_DIR", "").strip()
    static_root = (
        Path(configured_static)
        if configured_static
        else native_static_dir(project_root)
    )
    index_path = static_root / "index.html"
    if not index_path.is_file():
        expected = Path(project_root) / "frontend" / "dist" / "index.html"
        display = "frontend/dist/index.html" if index_path == expected else index_path
        raise StartupConfigurationError(
            f"The built browser UI is missing at '{display}'. "
            "Restore the committed frontend/dist files."
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_python_version()
        load_app_environment(PROJECT_ROOT)
        configure_native_runtime()
        prepare_environment()
        configure_model_provider(force=args.reconfigure)
        ensure_provider_credentials()
        validate_startup()
    except StartupConfigurationError as exc:
        print(f"Startup configuration error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Startup configuration error: {exc}", file=sys.stderr)
        return 2

    runtime = Path(os.environ["REPORT_AGENT_RUNTIME_ROOT"])
    runtime.mkdir(parents=True, exist_ok=True)

    import uvicorn

    print(f"Epidemiology Research Agent: http://{args.host}:{args.port}")
    uvicorn.run(
        "api.app:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
