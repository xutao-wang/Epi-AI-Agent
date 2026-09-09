#!/usr/bin/env python3
"""Prove real provider usage survives the public thread-state projection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
from time import perf_counter
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from langchain_core.messages import HumanMessage

from api.runtime import project_thread_state
from epi_agent.runtime import _record_model_observation
from llm_vllm import build_chat_llm
from utils.env_loader import load_app_environment
from utils.model_runtime_profiles import model_runtime_profile


_TIMEOUT_SECONDS = 300


def _timeout(_signum, _frame) -> None:
    raise TimeoutError("agent usage export smoke exceeded five minutes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one real OpenRouter usage-export smoke."
    )
    parser.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    load_app_environment(REPO_ROOT)
    profile = model_runtime_profile(args.model)
    if profile.provider != "openrouter":
        raise ValueError("--model must identify an OpenRouter profile")

    old_handler = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(_TIMEOUT_SECONDS)
    try:
        model = build_chat_llm(model_name=args.model)
        started = perf_counter()
        response = model.invoke(
            [HumanMessage(content="Reply exactly: agent-usage-export-ok")],
            **profile.output_budget_kwargs(64),
        )
        duration_ms = int((perf_counter() - started) * 1_000)
        model_output_state, _ = _record_model_observation(
            {}, response, duration_ms=duration_ms
        )
        snapshot = SimpleNamespace(
            values={"model_output_state": model_output_state},
            next=(),
            interrupts=[],
        )
        public = project_thread_state(
            thread_id="agent-usage-export-smoke",
            snapshot=snapshot,
            run_status={"state": "done", "steps": 1, "error": None},
            model_provider=profile.provider,
            served_model_id=profile.served_model_id,
        ).model_dump(mode="json")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

    usage = public.get("agent_usage")
    if not isinstance(usage, dict):
        raise AssertionError("public state omitted agent_usage")
    if usage.get("input_tokens", 0) < 1 or usage.get("output_tokens", 0) < 1:
        raise AssertionError(f"public token totals are invalid: {usage}")
    if "telemetry" in usage or "response_id" in usage:
        raise AssertionError("public usage exposed internal response records")
    print(json.dumps(usage, indent=2, sort_keys=True))
    print("agent usage export smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
