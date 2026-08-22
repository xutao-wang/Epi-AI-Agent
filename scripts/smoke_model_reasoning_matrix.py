#!/usr/bin/env python3
"""Exercise every credential-available built-in model exactly once."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path
import signal
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from langchain_core.messages import HumanMessage

from llm_vllm import build_chat_llm
from utils.env_loader import load_app_environment
from utils.llm_response import coerce_text_content
from utils.model_runtime_profiles import MODEL_RUNTIME_PROFILES
from utils.provider_errors import classify_llm_error


_PREFIX = "reasoning-matrix-ok"
_TIMEOUT_SECONDS = 300
_OUTPUT_BUDGET = 512


class _MatrixDeadlineExceeded(TimeoutError):
    """Raised only when the smoke's single global deadline expires."""


def _timeout(_signum, _frame) -> None:
    raise _MatrixDeadlineExceeded(
        "model reasoning matrix exceeded five minutes"
    )


def _response_model(response: Any) -> str:
    metadata = getattr(response, "response_metadata", {}) or {}
    for key in ("model_name", "model", "model_id"):
        value = metadata.get(key)
        if value:
            return str(value)
    return "unknown"


def run_smoke(
    environ: Mapping[str, str],
    *,
    llm_builder: Callable[..., Any] = build_chat_llm,
) -> int:
    failures = 0
    old_handler = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(_TIMEOUT_SECONDS)
    try:
        for model_id, profile in MODEL_RUNTIME_PROFILES.items():
            api_key = str(environ.get(profile.api_key_env) or "").strip()
            if not api_key:
                print(
                    f"SKIP model={model_id} label={profile.label} "
                    f"reason=missing-{profile.api_key_env}"
                )
                continue
            marker = f"{_PREFIX}:{model_id}"
            try:
                model = llm_builder(model_name=model_id, api_key=api_key)
                response = model.invoke(
                    [HumanMessage(content=f"Reply exactly: {marker}")],
                    **profile.output_budget_kwargs(_OUTPUT_BUDGET),
                )
                content = coerce_text_content(response.content).strip()
                if marker.casefold() not in content.casefold():
                    raise AssertionError("response marker was missing")
                usage = dict(response.usage_metadata or {})
                print(
                    f"PASS model={model_id} label={profile.label} "
                    f"response_model={_response_model(response)} "
                    f"input_tokens={int(usage.get('input_tokens') or 0)} "
                    f"output_tokens={int(usage.get('output_tokens') or 0)}"
                )
            except _MatrixDeadlineExceeded as exc:
                failures += 1
                print(
                    f"FAIL model={model_id} label={profile.label} "
                    f"code=MODEL_MATRIX_TIMEOUT message={exc}"
                )
                break
            except Exception as exc:
                failures += 1
                code, message = classify_llm_error(exc)
                print(
                    f"FAIL model={model_id} label={profile.label} "
                    f"code={code} message={message}"
                )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
    return 1 if failures else 0


def main() -> int:
    load_app_environment(REPO_ROOT)
    return run_smoke(os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
