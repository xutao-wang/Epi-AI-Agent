from __future__ import annotations

import argparse
import getpass
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
from utils.env_loader import load_app_environment, persist_local_env_values
from utils.provider_startup import ProviderCredentialError, verify_active_provider


PROJECT_ROOT = Path(__file__).resolve().parent


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


def ensure_active_provider_credential(
    *,
    project_root: str | Path = PROJECT_ROOT,
    environ: MutableMapping[str, str] = os.environ,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    verifier: Callable[[str, str], None] = verify_active_provider,
    persist: Callable[[str | Path, dict[str, str]], None] = persist_local_env_values,
    output_fn: Callable[[str], None] = print,
) -> None:
    provider = str(environ.get("REPORT_AGENT_PROVIDER", "openai") or "").strip().lower()
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
            raise StartupConfigurationError("OpenAI API key setup was cancelled.")

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
    verifier(environ=environ)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the RePORT Agent demo.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
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


def validate_startup(
    *,
    project_root: str | Path = PROJECT_ROOT,
    environ: MutableMapping[str, str] = os.environ,
    python_version: Sequence[int] = sys.version_info,
) -> None:
    if tuple(python_version[:2]) != (3, 12):
        found = ".".join(str(part) for part in python_version[:2])
        raise StartupConfigurationError(
            f"Python 3.12 is required; this interpreter is Python {found}."
        )

    if not environ.get("OPENAI_API_KEY", "").strip():
        raise StartupConfigurationError(
            "OPENAI_API_KEY is missing. Copy .env.example to .env and add "
            "your OpenAI API key."
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
        load_app_environment(PROJECT_ROOT)
        configure_native_runtime()
        prepare_environment()
        prepare_provider_credentials(
            os.environ,
            verifier=lambda **_kwargs: ensure_active_provider_credential(),
        )
        validate_startup()
    except StartupConfigurationError as exc:
        print(f"Startup configuration error: {exc}", file=sys.stderr)
        return 2

    runtime = Path(os.environ["REPORT_AGENT_RUNTIME_ROOT"])
    runtime.mkdir(parents=True, exist_ok=True)

    import uvicorn

    print(f"RePORT Agent: http://{args.host}:{args.port}")
    uvicorn.run(
        "api.app:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
