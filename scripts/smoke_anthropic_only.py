"""Make one real Anthropic chat request with embedding-provider keys removed."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from langchain_core.messages import HumanMessage

from llm_vllm import build_chat_llm
from utils.env_loader import load_app_environment
from utils.llm_response import coerce_text_content
from utils.model_runtime_profiles import model_runtime_profile


_EMBEDDING_CREDENTIAL_ENVS = ("OPENAI_API_KEY", "OPENROUTER_API_KEY")
_EXPECTED_MARKER = "anthropic-only-smoke-ok"


@contextmanager
def _without_embedding_credentials() -> Iterator[None]:
    saved = {
        name: os.environ.pop(name)
        for name in _EMBEDDING_CREDENTIAL_ENVS
        if name in os.environ
    }
    try:
        yield
    finally:
        os.environ.update(saved)


def run_smoke(
    *,
    model_name: str,
    anthropic_api_key: str,
    llm_builder: Callable[..., Any] = build_chat_llm,
) -> int:
    profile = model_runtime_profile(model_name)
    if profile.provider != "anthropic":
        raise ValueError("Anthropic-only smoke requires an Anthropic model.")
    if not str(anthropic_api_key or "").strip():
        raise ValueError("ANTHROPIC_API_KEY is required for the real smoke test.")

    with _without_embedding_credentials():
        model = llm_builder(
            model_name=model_name,
            api_key=anthropic_api_key,
        )
        response = model.invoke(
            [
                HumanMessage(
                    content=(
                        "Reply with exactly this text and nothing else: "
                        f"{_EXPECTED_MARKER}"
                    )
                )
            ],
            max_tokens=32,
        )

    content = coerce_text_content(getattr(response, "content", "")).strip()
    if _EXPECTED_MARKER not in content.casefold():
        raise AssertionError("Anthropic smoke response did not contain the marker.")
    print(
        "anthropic-only smoke passed: "
        f"model={model_name}, response_chars={len(content)}"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one real Anthropic request without embedding credentials."
    )
    parser.add_argument(
        "--model",
        default="",
        help="Registered Anthropic model ID (default: claude-haiku-4-5).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    load_app_environment(REPO_ROOT)
    model_name = str(
        args.model
        or os.environ.get("ANTHROPIC_SMOKE_MODEL")
        or "claude-haiku-4-5"
    ).strip()
    api_key = str(os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    return run_smoke(
        model_name=model_name,
        anthropic_api_key=api_key,
    )


if __name__ == "__main__":
    raise SystemExit(main())
