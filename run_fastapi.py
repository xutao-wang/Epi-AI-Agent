from __future__ import annotations

import argparse
import getpass
import ipaddress
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, MutableMapping, Sequence
from pathlib import Path

from api.deployment import (
    native_checkpoint_db_path,
    native_runtime_root,
    native_static_dir,
    native_study_root,
)
from utils.env_loader import (
    load_app_environment,
    persist_local_env_values,
    remove_local_env_values,
)
from utils.model_availability import (
    ModelAvailability,
    ProviderEndpoint,
    build_model_availability,
    configured_provider_endpoints,
    model_availability_from_configured_credentials,
    profile_endpoint,
    registered_model_profiles,
)
from utils.model_runtime_profiles import (
    ENABLED_CUSTOM_ENDPOINTS_ENV,
    OPENROUTER_BASE_URL,
    PROVIDER_API_KEY_ENVS,
    PROVIDER_OPENAI_COMPATIBLE,
    PROVIDER_OPENROUTER,
    enabled_custom_endpoint_ids,
    load_compatible_model_profiles,
    load_custom_endpoint_entries,
)
from utils.provider_startup import (
    ProviderCredentialError,
    discover_openai_compatible_models,
    verify_active_provider,
    verify_provider_credential,
)


PROJECT_ROOT = Path(__file__).resolve().parent

_DEPRECATED_LOCAL_ENV_KEYS = {
    "REPORT_AGENT_MODEL",
    "REPORT_AGENT_ALLOWED_MODELS",
    "OPENAI_MODEL",
    "REPORT_AGENT_TITLE_MODEL",
    "REPORT_AGENT_CHECKPOINT_DB_PATH",
}


class StartupConfigurationError(RuntimeError):
    """A startup problem the participant can correct without a traceback."""


def normalize_secret_input(value: str) -> str:
    normalized = str(value or "").strip()
    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {"'", '"'}
    ):
        return normalized[1:-1].strip()
    return normalized


def _provider_label(provider: str) -> str:
    return {
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        PROVIDER_OPENAI_COMPATIBLE: "Compatible endpoint",
        PROVIDER_OPENROUTER: "OpenRouter",
    }.get(provider, provider)


def _builtin_endpoint(provider: str) -> ProviderEndpoint:
    return ProviderEndpoint(
        provider=provider,
        api_key_env=PROVIDER_API_KEY_ENVS[provider],
        # OpenRouter is keyed per endpoint, so this must match the base_url
        # carried by its registered profiles.
        base_url=(
            OPENROUTER_BASE_URL if provider == PROVIDER_OPENROUTER else None
        ),
    )


def _verify_endpoint(
    endpoint: ProviderEndpoint,
    key: str,
    verifier: Callable[..., None],
) -> None:
    verifier(endpoint.provider, key, base_url=endpoint.base_url)


def _prompt_verified_builtin_key(
    endpoint: ProviderEndpoint,
    *,
    project_root: str | Path,
    environ: MutableMapping[str, str],
    getpass_fn: Callable[[str], str],
    verifier: Callable[..., None],
    persist: Callable[[str | Path, dict[str, str]], None],
    output_fn: Callable[[str], None],
) -> bool:
    label = _provider_label(endpoint.provider)
    while True:
        try:
            candidate = normalize_secret_input(
                getpass_fn(
                    f"Paste your {label} API key and press Enter to validate\n"
                    "(empty + Enter returns to provider setup): "
                )
            )
        except (EOFError, KeyboardInterrupt) as error:
            raise StartupConfigurationError(
                f"{label} API key setup was cancelled."
            ) from error
        if not candidate:
            return False
        try:
            _verify_endpoint(endpoint, candidate, verifier)
        except ProviderCredentialError as error:
            output_fn(f"{label} API key validation failed: {error}")
            continue
        environ[endpoint.api_key_env] = candidate
        persist(project_root, {endpoint.api_key_env: candidate})
        output_fn(f"{label} API key verified and saved to .env.")
        return True


def _configure_provider_menu(
    *,
    project_root: str | Path,
    environ: MutableMapping[str, str],
    input_fn: Callable[[str], str],
    getpass_fn: Callable[[str], str],
    verifier: Callable[..., None],
    persist: Callable[[str | Path, dict[str, str]], None],
    output_fn: Callable[[str], None],
    force: bool,
) -> None:
    while True:
        profiles = registered_model_profiles(environ)
        discovery_endpoints = load_custom_endpoint_entries(environ=environ)
        has_custom = any(
            profile.provider == PROVIDER_OPENAI_COMPATIBLE
            for profile in profiles.values()
        ) or bool(discovery_endpoints)
        has_openrouter = any(
            profile.provider == PROVIDER_OPENROUTER
            for profile in profiles.values()
        )
        if force:
            prompt = (
                "Configure AI providers. Existing providers are retained.\n\n"
                "1. Configure or replace OpenAI (preferred)\n"
                "2. Configure or replace Anthropic\n"
                "3. Configure or replace OpenRouter\n"
                "4. Connect to a compatible endpoint (beta)\n"
                "5. Remove OpenAI\n"
                "6. Remove Anthropic\n"
                "7. Remove OpenRouter\n"
                "8. Keep current providers\n"
                "Selection [8]: "
            )
            default = "8"
        else:
            prompt = (
                "No AI provider is configured.\n\n"
                "1. Configure OpenAI (preferred)\n"
                "2. Configure Anthropic\n"
                "3. Configure OpenRouter\n"
                "4. Connect to a compatible endpoint (beta)\n"
                "Selection: "
            )
            default = ""
        try:
            selection = input_fn(prompt).strip() or default
        except (EOFError, KeyboardInterrupt) as error:
            raise StartupConfigurationError(
                "AI provider setup was cancelled."
            ) from error

        providers = {
            "1": ("openai",),
            "2": ("anthropic",),
            "3": (PROVIDER_OPENROUTER,),
        }.get(selection)
        if providers is not None:
            configured_any = False
            for provider in providers:
                configured_any = (
                    _prompt_verified_builtin_key(
                        _builtin_endpoint(provider),
                        project_root=project_root,
                        environ=environ,
                        getpass_fn=getpass_fn,
                        verifier=verifier,
                        persist=persist,
                        output_fn=output_fn,
                    )
                    or configured_any
                )
            if configured_any:
                if PROVIDER_OPENROUTER in providers and not has_openrouter:
                    output_fn(
                        "No OpenRouter models are registered. Copy "
                        "config/custom_models.example.json to "
                        "config/custom_models.json and list the OpenRouter "
                        "models to enable."
                    )
                return
            continue
        if selection == "4":
            if has_custom:
                custom_options = {
                    profile.custom_endpoint_id: profile.base_label
                    for profile in profiles.values()
                    if profile.provider == PROVIDER_OPENAI_COMPATIBLE
                    and profile.custom_endpoint_id
                }
                custom_options.update(
                    {
                        endpoint_id: entry.label or endpoint_id
                        for endpoint_id, entry in discovery_endpoints.items()
                    }
                )
                endpoint_ids = tuple(custom_options)
                selected_endpoint = endpoint_ids[0]
                if len(endpoint_ids) > 1:
                    listing = "\n".join(
                        f"{index}. {custom_options[endpoint_id]} ({endpoint_id})"
                        for index, endpoint_id in enumerate(endpoint_ids, start=1)
                    )
                    try:
                        endpoint_choice = (
                            input_fn(
                                "Choose the compatible endpoint:\n"
                                f"{listing}\nSelection [1]: "
                            ).strip()
                            or "1"
                        )
                        selected_index = int(endpoint_choice)
                        if not 1 <= selected_index <= len(endpoint_ids):
                            raise ValueError
                        selected_endpoint = endpoint_ids[selected_index - 1]
                    except (IndexError, ValueError):
                        output_fn("Select one of the listed compatible endpoints.")
                        continue
                    except (EOFError, KeyboardInterrupt) as error:
                        raise StartupConfigurationError(
                            "Compatible endpoint setup was cancelled."
                        ) from error
                configured = selected_endpoint
                environ[ENABLED_CUSTOM_ENDPOINTS_ENV] = configured
                persist(
                    project_root,
                    {ENABLED_CUSTOM_ENDPOINTS_ENV: configured},
                )
                return
            output_fn(
                "No compatible models are registered. Copy "
                "config/custom_models.example.json to "
                "config/custom_models.json, edit the endpoint, and retry."
            )
            continue
        if force and selection in {"5", "6", "7"}:
            key = PROVIDER_API_KEY_ENVS[
                {"5": "openai", "6": "anthropic", "7": PROVIDER_OPENROUTER}[selection]
            ]
            environ.pop(key, None)
            remove_local_env_values(project_root, {key})
            return
        if force and selection == "8":
            return
        output_fn("Select one of the listed provider options.")


def configure_and_verify_providers(
    *,
    project_root: str | Path = PROJECT_ROOT,
    environ: MutableMapping[str, str] = os.environ,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    output_fn: Callable[[str], None] = print,
    verifier: Callable[..., None] = verify_provider_credential,
    discoverer: Callable[..., Sequence[object]] = discover_openai_compatible_models,
    persist: Callable[[str | Path, dict[str, str]], None] = persist_local_env_values,
    force: bool = False,
) -> ModelAvailability:
    """Configure providers, verify each one, and return usable model IDs."""
    remove_local_env_values(project_root, _DEPRECATED_LOCAL_ENV_KEYS)
    for key in _DEPRECATED_LOCAL_ENV_KEYS - {"REPORT_AGENT_CHECKPOINT_DB_PATH"}:
        environ.pop(key, None)

    has_builtin = any(
        normalize_secret_input(environ.get(key, ""))
        for key in PROVIDER_API_KEY_ENVS.values()
    )
    has_selected_custom = bool(enabled_custom_endpoint_ids(environ))
    if force or not (has_builtin or has_selected_custom):
        _configure_provider_menu(
            project_root=project_root,
            environ=environ,
            input_fn=input_fn,
            getpass_fn=getpass_fn,
            verifier=verifier,
            persist=persist,
            output_fn=output_fn,
            force=force,
        )

    verified: set[ProviderEndpoint] = set()
    profiles = registered_model_profiles(environ)
    discovered_profiles = {}
    discovery_endpoints = load_custom_endpoint_entries(environ=environ)
    for endpoint_id in enabled_custom_endpoint_ids(environ):
        entry = discovery_endpoints.get(endpoint_id)
        if entry is None:
            continue
        key = normalize_secret_input(
            environ.get(entry.api_key_env, "") if entry.api_key_env else ""
        )
        if entry.api_key_env and not key:
            output_fn(
                f"Warning: {entry.api_key_env} is required by compatible "
                f"endpoint {entry.base_url}; its models are unavailable."
            )
            continue
        try:
            remote_models = tuple(discoverer(key, base_url=entry.base_url))
            served_model_ids = tuple(
                str(getattr(value, "model_id", value) or "").strip()
                for value in remote_models
            )
            compatible_model_profiles = load_compatible_model_profiles(
                environ=environ,
                model_ids=served_model_ids,
            )
            endpoint_profiles = entry.profiles_for(
                remote_models,
                model_profiles=compatible_model_profiles,
            )
        except (ProviderCredentialError, ValueError) as error:
            output_fn(
                f"Compatible endpoint discovery failed for "
                f"{entry.base_url}: {error}"
            )
            continue
        duplicate_ids = sorted(
            set(endpoint_profiles) & (set(profiles) | set(discovered_profiles))
        )
        if duplicate_ids:
            raise StartupConfigurationError(
                "Discovered custom model id duplicates an existing model: "
                + ", ".join(duplicate_ids)
            )
        discovered_profiles.update(endpoint_profiles)
        verified.add(
            ProviderEndpoint(
                provider=PROVIDER_OPENAI_COMPATIBLE,
                api_key_env=entry.api_key_env,
                base_url=entry.base_url,
            )
        )
        output_fn(
            f"Compatible endpoint verified: {len(endpoint_profiles)} "
            f"model(s) discovered."
        )
    for endpoint in configured_provider_endpoints(environ):
        if endpoint in verified:
            continue
        key = normalize_secret_input(
            environ.get(endpoint.api_key_env, "") if endpoint.api_key_env else ""
        )
        endpoint_profiles = [
            profile
            for profile in profiles.values()
            if profile_endpoint(profile) == endpoint
        ]
        key_required = any(profile.api_key_required for profile in endpoint_profiles)
        if endpoint.api_key_env and key_required and not key:
            output_fn(
                f"Warning: {endpoint.api_key_env} is required by endpoint "
                f"{endpoint.base_url}; its models are unavailable."
            )
            continue
        try:
            _verify_endpoint(endpoint, key, verifier)
        except ProviderCredentialError as error:
            label = _provider_label(endpoint.provider)
            output_fn(f"{label} validation failed: {error}")
            if endpoint.provider == PROVIDER_OPENAI_COMPATIBLE:
                continue
            if _prompt_verified_builtin_key(
                endpoint,
                project_root=project_root,
                environ=environ,
                getpass_fn=getpass_fn,
                verifier=verifier,
                persist=persist,
                output_fn=output_fn,
            ):
                verified.add(endpoint)
        else:
            if endpoint.api_key_env:
                environ[endpoint.api_key_env] = key
            verified.add(endpoint)
            output_fn(f"{_provider_label(endpoint.provider)} verified.")

    try:
        return build_model_availability(
            environ,
            verified,
            discovered_profiles,
        )
    except ValueError as error:
        raise StartupConfigurationError(str(error)) from error


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


def _loopback_host(value: str) -> str:
    host = value.strip()
    if host.casefold() == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "--host must be localhost or a loopback IP address"
        ) from error
    if not address.is_loopback:
        raise argparse.ArgumentTypeError(
            "--host must be localhost or a loopback IP address"
        )
    return host


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start the Epidemiology Research Agent demo."
    )
    parser.add_argument("--host", type=_loopback_host, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
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
    model_availability: ModelAvailability | None = None,
) -> None:
    validate_python_version(python_version)

    if model_availability is None:
        try:
            model_availability_from_configured_credentials(environ)
        except ValueError as error:
            raise StartupConfigurationError(str(error)) from error

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
        load_app_environment(
            PROJECT_ROOT,
            local_api_keys_only=True,
        )
        configure_native_runtime()
        prepare_environment()
        model_availability = configure_and_verify_providers(
            force=args.reconfigure
        )
        validate_startup(model_availability=model_availability)
    except StartupConfigurationError as exc:
        print(f"Startup configuration error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Startup configuration error: {exc}", file=sys.stderr)
        return 2

    runtime = Path(os.environ["REPORT_AGENT_RUNTIME_ROOT"])
    runtime.mkdir(parents=True, exist_ok=True)

    import uvicorn
    from api.app import build_application

    application = build_application(
        environ=os.environ,
        model_availability=model_availability,
    )

    print(f"Epidemiology Research Agent: http://{args.host}:{args.port}")
    uvicorn.run(
        application,
        host=args.host,
        port=args.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
