from __future__ import annotations

import argparse
import io
import json
from importlib import metadata
from pathlib import Path
import platform
import sys
import warnings


MAX_STDOUT_BYTES = 100_000
MAX_RESULT_BYTES = 1_000_000
MAX_FIGURE_BYTES = 10_000_000


class _OutputLimitError(RuntimeError):
    pass


class _GeneratedDatasetInputError(RuntimeError):
    pass


class _BoundedTextBuffer(io.StringIO):
    def write(self, value: str) -> int:
        if len((self.getvalue() + value).encode("utf-8")) > MAX_STDOUT_BYTES:
            raise _OutputLimitError("Generated stdout exceeded the output limit.")
        return super().write(value)


def _package_manifest() -> list[dict[str, str]]:
    packages: list[dict[str, str]] = []
    for name in (
        "pandas",
        "numpy",
        "scipy",
        "statsmodels",
        "lifelines",
        "matplotlib",
        "seaborn",
        "openpyxl",
        "xlrd",
        "pyarrow",
    ):
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        packages.append({"name": name, "version": version})
    return packages


def _write_json(path: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_RESULT_BYTES:
        raise _OutputLimitError("Generated result exceeded the output limit.")
    path.write_bytes(encoded)


def _error_payload(
    *,
    code: str,
    message: str,
    category: str,
    recoverable: bool,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {
        "code": code,
        "type": "PythonRuntimeError",
        "message": message[:2_000],
        "category": category,
        "recoverable": recoverable,
    }
    if details is not None:
        error["details"] = details
    return {"status": "error", "error": error}


def _load_selected_dataset(input_dir: Path):
    selection_path = input_dir / "selected_dataset_id.txt"
    if not selection_path.exists():
        return None

    import pandas as pd

    manifest = json.loads((input_dir / "datasets.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Dataset manifest must be an object.")
    selected_dataset_id = selection_path.read_text(encoding="utf-8")
    filename = manifest.get(selected_dataset_id)
    if not isinstance(filename, str) or not filename:
        raise ValueError("Selected dataset is absent from the runtime manifest.")
    path = input_dir / "datasets" / filename
    return pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)


def execute(input_dir: Path, output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_buffer = _BoundedTextBuffer()
    previous_stdout = sys.stdout
    try:
        dataset = _load_selected_dataset(input_dir)
        code = (input_dir / "code.py").read_text(encoding="utf-8")
        globals_for_code = {"__name__": "__main__"}
        if dataset is not None:
            globals_for_code["dataset"] = dataset
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            sys.stdout = stdout_buffer
            try:
                exec(compile(code, "<generated-python>", "exec"), globals_for_code)
            except FileNotFoundError as exc:
                raise _GeneratedDatasetInputError(
                    "Analysis inputs are already loaded as `dataset`; analyze "
                    "that DataFrame instead of loading a file."
                ) from exc
        sys.stdout = previous_stdout

        figure_png = b""
        pyplot = sys.modules.get("matplotlib.pyplot")
        figure_numbers = list(pyplot.get_fignums()) if pyplot is not None else []
        if pyplot is not None and figure_numbers:
            buffer = io.BytesIO()
            pyplot.figure(figure_numbers[-1]).savefig(
                buffer,
                format="png",
                bbox_inches="tight",
            )
            figure_png = buffer.getvalue()
            if len(figure_png) > MAX_FIGURE_BYTES:
                raise _OutputLimitError("Generated figure exceeded the output limit.")
            (output_dir / "figure.png").write_bytes(figure_png)

        output_text = stdout_buffer.getvalue()
        _write_json(
            output_dir / "result.json",
            {
                "status": "ok",
                "output_text": output_text,
                "warnings": [
                    str(item.message)[:8_000] for item in caught_warnings[:100]
                ],
                "runtime": {
                    "language": "Python",
                    "version": platform.python_version(),
                    "packages": _package_manifest(),
                    "execution_mode": "guarded_local",
                    "security_boundary": "none",
                },
                "has_figure": bool(figure_png),
            },
        )
        return 0
    except _OutputLimitError as exc:
        sys.stdout = previous_stdout
        code = (
            "STDOUT_TOO_LARGE"
            if "stdout" in str(exc).casefold()
            else "OUTPUT_TOO_LARGE"
        )
        payload = _error_payload(
            code=code,
            message=str(exc),
            category="invalid_output",
            recoverable=True,
        )
    except (TypeError, ValueError) as exc:
        sys.stdout = previous_stdout
        payload = _error_payload(
            code="INVALID_RESULT",
            message=str(exc),
            category="invalid_output",
            recoverable=True,
        )
    except ModuleNotFoundError as exc:
        sys.stdout = previous_stdout
        missing_module = str(exc.name or "unknown")[:512]
        payload = _error_payload(
            code="DEPENDENCY_NOT_AVAILABLE",
            message=f"Package “{missing_module}” isn’t installed.",
            category="unsupported_runtime",
            recoverable=True,
            details={
                "available_packages": [
                    package["name"] for package in _package_manifest()
                ],
                "missing_module": missing_module,
            },
        )
    except _GeneratedDatasetInputError as exc:
        sys.stdout = previous_stdout
        payload = _error_payload(
            code="DATASET_INPUT_ALREADY_LOADED",
            message=str(exc),
            category="retryable_code",
            recoverable=True,
        )
    except Exception as exc:
        sys.stdout = previous_stdout
        payload = _error_payload(
            code="EXECUTION_FAILED",
            message=str(exc) or type(exc).__name__,
            category="retryable_code",
            recoverable=True,
        )
    finally:
        pyplot = sys.modules.get("matplotlib.pyplot")
        if pyplot is not None:
            pyplot.close("all")

    try:
        _write_json(output_dir / "result.json", payload)
    except Exception:
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    return execute(args.input_dir.resolve(), args.output_dir.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
