# Consumed Model Reasoning Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace decorative reasoning tiers with one consumed per-model reasoning configuration, translate it correctly for OpenAI and Anthropic, and derive every visible model label from that configuration.

**Architecture:** `utils/model_runtime_profiles.py` remains the authoritative model registry and gains an immutable `ReasoningConfig`. Provider adapters in `llm_vllm.py` translate it into OpenAI's `reasoning_effort` or Anthropic's `thinking` and `effort`; the API exposes a backend-derived label, and the frontend renders that label unchanged.

**Tech Stack:** Python 3.12, frozen dataclasses, Pydantic v2, LangChain `ChatOpenAI`/`ChatAnthropic`, FastAPI, React, TypeScript, pytest, Vitest.

## Global Constraints

- Run Python through `.venv/bin/python`, never the system `python3`.
- Use red-green-refactor and observe each new test fail for the intended reason before production edits.
- Opus 5 and Sonnet 5 use adaptive thinking with medium effort.
- Haiku 4.5 sends no reasoning arguments and displays `Standard`.
- Preserve OpenAI behavior: GPT-5.4 Standard, Luna Low, Terra Medium, Sol Medium.
- Derive every visible suffix from `reasoning`; do not infer it in the frontend or store a second tier.
- Compatible custom models use `reasoning=None` and display `Standard`.
- Do not alter token limits, timeouts, prices, availability, or default selection.
- Rebuild the tracked frontend and refresh its manifest after frontend-source changes.
- Run the dedicated real-provider smoke once, without retries, under one five-minute deadline.

---

## File map

- `utils/model_runtime_profiles.py`: typed reasoning config, validation, declarations, derived labels.
- `llm_vllm.py`: provider-specific request translation.
- `api/schemas.py`: public model option without `reasoning_tier`.
- `config/custom_models.example.json`: compatible example without the removed field.
- `tests/test_model_runtime_profiles.py`: exhaustive profile, validation, label, custom-entry tests.
- `tests/test_llm_vllm.py`: exhaustive provider-constructor tests.
- `tests/test_api_runtime.py`, `tests/test_api_server.py`: backend descriptor/API coverage.
- `frontend/src/types.ts`: frontend contract cleanup.
- `frontend/src/RuntimeSettingsPanel.test.tsx`: all built-in labels rendered verbatim.
- `frontend/src/App.test.tsx`, `frontend/src/apiClient.test.ts`: fixture cleanup.
- `frontend/dist/**`: rebuilt production bundle.
- `scripts/smoke_model_reasoning_matrix.py`: one-pass real model matrix.
- `tests/test_model_reasoning_matrix_smoke.py`: non-billable smoke-runner tests.

---

### Task 1: Authoritative reasoning registry and provider translation

**Files:**
- Create: `tests/test_model_runtime_profiles.py`
- Modify: `tests/test_llm_vllm.py`
- Modify: `tests/test_api_runtime.py`
- Modify: `utils/model_runtime_profiles.py`
- Modify: `llm_vllm.py`
- Modify: `api/schemas.py`
- Modify: `config/custom_models.example.json`

**Interfaces:**
- Produces: `ReasoningConfig(effort: ReasoningEffort, mode: ReasoningMode | None = None)`.
- Produces: `ModelRuntimeProfile.reasoning: ReasoningConfig | None`, stored
  `base_label: str`, and derived `label: str`.
- Produces: `descriptor()` without `reasoning_tier`; its `label` is the derived
  `profile.label`.
- Preserves: `build_chat_llm(...)` and `build_openai_llm(...)` signatures.

- [ ] **Step 1: Write failing exhaustive registry tests**

Create `tests/test_model_runtime_profiles.py`:

```python
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from utils.model_runtime_profiles import (
    MODEL_RUNTIME_PROFILES,
    ReasoningConfig,
    load_custom_model_profiles,
    model_runtime_profile,
)


@pytest.mark.parametrize(
    ("model_id", "mode", "effort", "label"),
    [
        ("gpt-5.4", None, None, "gpt-5.4 (Standard)"),
        ("gpt-5.6-luna", None, "low", "gpt-5.6-luna (Low)"),
        ("gpt-5.6-terra", None, "medium", "gpt-5.6-terra (Medium)"),
        ("gpt-5.6-sol", None, "medium", "gpt-5.6-sol (Medium)"),
        ("claude-opus-5", "adaptive", "medium", "Claude Opus 5 (Medium)"),
        ("claude-sonnet-5", "adaptive", "medium", "Claude Sonnet 5 (Medium)"),
        ("claude-haiku-4-5", None, None, "Claude Haiku 4.5 (Standard)"),
    ],
)
def test_every_builtin_declares_consumed_reasoning_and_label(
    model_id: str,
    mode: str | None,
    effort: str | None,
    label: str,
) -> None:
    assert len(MODEL_RUNTIME_PROFILES) == 7
    profile = model_runtime_profile(model_id)
    assert getattr(profile.reasoning, "mode", None) == mode
    assert getattr(profile.reasoning, "effort", None) == effort
    assert profile.label == label
    assert profile.descriptor()["label"] == label
    assert "reasoning_tier" not in profile.descriptor()


def test_openai_rejects_anthropic_reasoning_mode() -> None:
    profile = model_runtime_profile("gpt-5.6-terra")
    with pytest.raises(ValueError, match="gpt-5.6-terra.*mode"):
        replace(
            profile,
            reasoning=ReasoningConfig(mode="adaptive", effort="medium"),
        )


def test_compatible_provider_rejects_reasoning() -> None:
    profile = model_runtime_profile("gpt-5.6-terra")
    with pytest.raises(ValueError, match="reasoning.*openai_compatible"):
        replace(profile, provider="openai_compatible")


def test_custom_model_derives_standard_label(tmp_path) -> None:
    path = tmp_path / "custom_models.json"
    path.write_text(
        json.dumps([{
            "id": "cluster-model",
            "label": "Cluster Model",
            "base_url": "https://llm.internal/v1",
        }]),
        encoding="utf-8",
    )
    profile = load_custom_model_profiles(path)["cluster-model"]
    assert profile.reasoning is None
    assert profile.label == "Cluster Model (Standard)"


def test_custom_model_rejects_removed_reasoning_tier(tmp_path) -> None:
    path = tmp_path / "custom_models.json"
    path.write_text(
        json.dumps([{
            "id": "cluster-model",
            "base_url": "https://llm.internal/v1",
            "reasoning_tier": "standard",
        }]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reasoning_tier"):
        load_custom_model_profiles(path)
```

- [ ] **Step 2: Write failing provider-constructor tests**

Extend `tests/test_llm_vllm.py`:

```python
import pytest


def _capture_chat_anthropic(monkeypatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    class FakeChatAnthropic:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "langchain_anthropic",
        SimpleNamespace(ChatAnthropic=FakeChatAnthropic),
    )
    return captured


@pytest.mark.parametrize(
    ("model_id", "effort"),
    [
        ("gpt-5.4", None),
        ("gpt-5.6-luna", "low"),
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-sol", "medium"),
    ],
)
def test_every_openai_model_sends_configured_reasoning(
    monkeypatch, model_id: str, effort: str | None
) -> None:
    captured = _capture_chat_openai(monkeypatch)
    llm_vllm.build_chat_llm(model_name=model_id, api_key="session-key")
    assert captured.get("reasoning_effort") == effort


@pytest.mark.parametrize(
    ("model_id", "thinking", "effort"),
    [
        ("claude-opus-5", {"type": "adaptive"}, "medium"),
        ("claude-sonnet-5", {"type": "adaptive"}, "medium"),
        ("claude-haiku-4-5", None, None),
    ],
)
def test_every_anthropic_model_sends_configured_reasoning(
    monkeypatch,
    model_id: str,
    thinking: dict[str, str] | None,
    effort: str | None,
) -> None:
    captured = _capture_chat_anthropic(monkeypatch)
    llm_vllm.build_chat_llm(model_name=model_id, api_key="session-key")
    assert captured.get("thinking") == thinking
    assert captured.get("effort") == effort
```

- [ ] **Step 3: Write a failing API-schema assertion**

In `tests/test_api_runtime.py`, import `ModelOption` and append to
`test_model_profiles_declare_sampling_control_support`:

```python
    option = ModelOption(
        **model_runtime_profile("claude-sonnet-5").descriptor()
    )
    assert option.label == "Claude Sonnet 5 (Medium)"
    assert "reasoning_tier" not in option.model_dump()
```

- [ ] **Step 4: Run focused tests and verify RED**

```bash
.venv/bin/python -m pytest \
  tests/test_model_runtime_profiles.py \
  tests/test_llm_vllm.py \
  tests/test_api_runtime.py::test_model_profiles_declare_sampling_control_support \
  -q
```

Expected: FAIL because the structured config and derived label do not exist,
Claude omits reasoning arguments, and `ModelOption` requires a tier.

- [ ] **Step 5: Implement typed reasoning and derived labels**

In `utils/model_runtime_profiles.py`, define:

```python
ReasoningMode = Literal["adaptive"]
ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class ReasoningConfig:
    effort: ReasoningEffort
    mode: ReasoningMode | None = None
```

Replace the two old profile fields with `reasoning: ReasoningConfig | None` and
add:

```python
    def __post_init__(self) -> None:
        if self.provider == PROVIDER_OPENAI:
            if self.reasoning is not None and self.reasoning.mode is not None:
                raise ValueError(
                    f"{self.model_id} reasoning mode is unsupported by openai"
                )
            if (
                self.reasoning is not None
                and self.reasoning.effort not in {"low", "medium", "high"}
            ):
                raise ValueError(
                    f"{self.model_id} reasoning effort is unsupported by openai"
                )
        elif self.provider == PROVIDER_ANTHROPIC:
            if self.reasoning is not None and self.reasoning.mode != "adaptive":
                raise ValueError(
                    f"{self.model_id} reasoning mode must be adaptive for anthropic"
                )
        elif self.reasoning is not None:
            raise ValueError(
                f"reasoning is unsupported by {self.provider} for {self.model_id}"
            )

    @property
    def reasoning_display(self) -> str:
        return "Standard" if self.reasoning is None else self.reasoning.effort.title()

    @property
    def label(self) -> str:
        return f"{self.base_label} ({self.reasoning_display})"
```

Rename the dataclass storage field from `label` to `base_label` and store base
labels without suffixes. Use `ReasoningConfig(effort="low")` for Luna and the
internal Luna-Light model, medium for Terra/Sol, adaptive-medium for
Opus/Sonnet, and `None` for GPT-5.4/Haiku/custom models. Continue returning
`profile.label` from `descriptor()` and remove its tier key. Remove
`reasoning_tier` from `CustomModelEntry`, its conversion, exports, and
`config/custom_models.example.json`. In `CustomModelEntry.to_profile()`, map
the operator-facing input with `base_label=self.label or self.id` and
`reasoning=None`.

- [ ] **Step 6: Implement provider translation and schema cleanup**

In both OpenAI paths in `llm_vllm.py`, use:

```python
    if profile.reasoning is not None:
        kwargs["reasoning_effort"] = profile.reasoning.effort
```

Replace the Anthropic constructor body with:

```python
    kwargs: dict[str, object] = {
        "model": profile.served_model_id,
        "api_key": api_key,
        "timeout": profile.request_timeout_seconds,
        "max_retries": 0,
        "max_tokens": profile.initial_output_tokens,
    }
    if profile.reasoning is not None:
        kwargs["thinking"] = {"type": profile.reasoning.mode}
        kwargs["effort"] = profile.reasoning.effort
    return ChatAnthropic(**kwargs)
```

Remove `reasoning_tier` from `api.schemas.ModelOption`.

- [ ] **Step 7: Run backend and API contract tests and verify GREEN**

```bash
.venv/bin/python -m pytest \
  tests/test_model_runtime_profiles.py \
  tests/test_llm_vllm.py \
  tests/test_model_availability.py \
  tests/test_api_runtime.py::test_model_profiles_declare_sampling_control_support \
  tests/test_api_server.py::test_runtime_options_route_returns_backend_supported_choices \
  -q
```

Expected: PASS with all seven public built-ins covered.

- [ ] **Step 8: Commit**

```bash
git add utils/model_runtime_profiles.py llm_vllm.py api/schemas.py \
  config/custom_models.example.json tests/test_model_runtime_profiles.py \
  tests/test_llm_vllm.py tests/test_api_runtime.py
git commit -m "fix: consume per-model reasoning configuration"
```

---

### Task 2: Frontend contract and consistent labels

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/RuntimeSettingsPanel.test.tsx`
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/apiClient.test.ts`
- Modify: `frontend/dist/**`

**Interfaces:**
- Consumes: server-provided `ModelOption.label` with its derived suffix.
- Produces: frontend `ModelOption` without `reasoning_tier`.
- Preserves: selectors render `model.label` verbatim.

- [ ] **Step 1: Write a failing all-model rendering test**

Remove the tier argument/property from the test helper in
`frontend/src/RuntimeSettingsPanel.test.tsx`, add all seven built-ins to its
fixture, and add:

```typescript
it("renders every backend-derived reasoning label verbatim", () => {
  render(
    <RuntimeSettingsPanel
      locked={false}
      onChange={vi.fn()}
      options={options}
      settings={standardSettings}
    />,
  );
  const labels = [
    "gpt-5.4 (Standard)",
    "gpt-5.6-luna (Low)",
    "gpt-5.6-terra (Medium)",
    "gpt-5.6-sol (Medium)",
    "Claude Opus 5 (Medium)",
    "Claude Sonnet 5 (Medium)",
    "Claude Haiku 4.5 (Standard)",
  ];
  for (const label of labels) {
    expect(screen.getByRole("option", { name: label })).toBeInTheDocument();
  }
});
```

Give Claude fixtures `provider: "anthropic"` and
`provider_label: "Anthropic"`.

- [ ] **Step 2: Run component test/build and verify RED**

```bash
npm --prefix frontend test -- --run src/RuntimeSettingsPanel.test.tsx
npm --prefix frontend run build
```

Expected: test or build FAILS while fixtures/types still require the removed
tier and before the complete label matrix is present.

- [ ] **Step 3: Clean the frontend contract**

Delete this field from `frontend/src/types.ts`:

```typescript
reasoning_tier: "standard" | "low" | "medium" | "high";
```

Remove every tier assignment/override from
`RuntimeSettingsPanel.test.tsx`, `App.test.tsx`, and `apiClient.test.ts`. Do not
change `App.tsx` or `RuntimeSettingsPanel.tsx`; both already render
`{model.label}`.

- [ ] **Step 4: Run affected frontend tests and verify GREEN**

```bash
npm --prefix frontend test -- --run \
  src/RuntimeSettingsPanel.test.tsx src/App.test.tsx src/apiClient.test.ts
```

Expected: PASS with all seven labels rendered verbatim.

- [ ] **Step 5: Rebuild and verify tracked delivery artifacts**

```bash
npm --prefix frontend run build
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
.venv/bin/python scripts/verify_working_demo_delivery.py
```

Expected: build and delivery verification PASS with a refreshed manifest.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/types.ts frontend/src/RuntimeSettingsPanel.test.tsx \
  frontend/src/App.test.tsx frontend/src/apiClient.test.ts frontend/dist
git commit -m "fix: display reasoning derived from model profiles"
```

---

### Task 3: One-pass real-provider model matrix smoke

**Files:**
- Create: `scripts/smoke_model_reasoning_matrix.py`
- Create: `tests/test_model_reasoning_matrix_smoke.py`

**Interfaces:**
- Consumes: `MODEL_RUNTIME_PROFILES`, `build_chat_llm`, derived `label`, and
  `output_budget_kwargs`.
- Produces: `run_smoke(environ, llm_builder=build_chat_llm) -> int`.
- Produces: one sanitized result per credential-available built-in, no retries.

- [ ] **Step 1: Write failing smoke-runner tests**

Create `tests/test_model_reasoning_matrix_smoke.py`:

```python
from __future__ import annotations

from langchain_core.messages import AIMessage

from scripts.smoke_model_reasoning_matrix import run_smoke
from utils.model_runtime_profiles import MODEL_RUNTIME_PROFILES


class _FakeModel:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def invoke(self, messages, **kwargs):
        assert messages and kwargs
        return AIMessage(
            content=f"reasoning-matrix-ok:{self.model_id}",
            usage_metadata={"input_tokens": 4, "output_tokens": 2},
        )


def test_checks_every_available_builtin_once_without_secrets(capsys) -> None:
    calls: list[str] = []

    def builder(*, model_name: str, api_key: str):
        assert api_key in {"openai-secret", "anthropic-secret"}
        calls.append(model_name)
        return _FakeModel(model_name)

    result = run_smoke(
        {
            "OPENAI_API_KEY": "openai-secret",
            "ANTHROPIC_API_KEY": "anthropic-secret",
        },
        llm_builder=builder,
    )
    output = capsys.readouterr().out
    assert result == 0
    assert calls == list(MODEL_RUNTIME_PROFILES)
    assert len(calls) == len(set(calls)) == 7
    assert "Claude Opus 5 (Medium)" in output
    assert "Claude Haiku 4.5 (Standard)" in output
    assert "secret" not in output


def test_records_failure_once_and_continues(capsys) -> None:
    calls: list[str] = []

    def builder(*, model_name: str, api_key: str):
        calls.append(model_name)
        if model_name == "claude-sonnet-5":
            raise RuntimeError("provider rejected model")
        return _FakeModel(model_name)

    result = run_smoke(
        {"ANTHROPIC_API_KEY": "anthropic-secret"},
        llm_builder=builder,
    )
    output = capsys.readouterr().out
    assert result == 1
    assert calls == [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ]
    assert calls.count("claude-sonnet-5") == 1
    assert "RUN_FAILED" in output
```

- [ ] **Step 2: Run test and verify RED**

```bash
.venv/bin/python -m pytest tests/test_model_reasoning_matrix_smoke.py -q
```

Expected: collection ERROR because the smoke module does not exist.

- [ ] **Step 3: Implement the matrix smoke**

Create `scripts/smoke_model_reasoning_matrix.py` with:

```python
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


def _timeout(_signum, _frame) -> None:
    raise TimeoutError("model reasoning matrix exceeded five minutes")


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
                    f"input_tokens={int(usage.get('input_tokens') or 0)} "
                    f"output_tokens={int(usage.get('output_tokens') or 0)}"
                )
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
```

- [ ] **Step 4: Run unit test and verify GREEN**

```bash
.venv/bin/python -m pytest tests/test_model_reasoning_matrix_smoke.py -q
```

Expected: PASS; seven calls with both keys and three with Anthropic only.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_model_reasoning_matrix.py \
  tests/test_model_reasoning_matrix_smoke.py
git commit -m "test: add real model reasoning matrix smoke"
```

---

### Task 4: Thorough all-model verification

**Files:**
- Verify only; do not alter code to hide a remote model failure.

**Interfaces:**
- Consumes all Task 1–3 deliverables.
- Produces pass/fail evidence for registry, adapters, API, UI, bundle, and each available real model.

- [ ] **Step 1: Prove the decorative field is gone**

```bash
if rg -n "reasoning_tier" utils api config frontend/src tests \
  --glob '!frontend/dist/**' \
  --glob '!tests/test_model_runtime_profiles.py'; then exit 1; fi
rg -n "reasoning_tier" tests/test_model_runtime_profiles.py
```

Expected: the first command exits 0 with no operational matches. The second
finds only the explicit removed-property rejection test and its legacy JSON
input.

- [ ] **Step 2: Run exhaustive backend/API verification**

```bash
.venv/bin/python -m pytest \
  tests/test_model_runtime_profiles.py tests/test_llm_vllm.py \
  tests/test_model_availability.py tests/test_api_runtime.py \
  tests/test_api_server.py tests/test_model_reasoning_matrix_smoke.py -q
```

Expected: PASS and all seven public built-ins appear in parameterized tests.

- [ ] **Step 3: Run complete frontend and bundle verification**

```bash
npm --prefix frontend test -- --run
npm --prefix frontend run build
.venv/bin/python scripts/verify_working_demo_delivery.py
```

Expected: all tests and delivery checks PASS.

- [ ] **Step 4: Run the real-provider smoke exactly once**

```bash
.venv/bin/python scripts/smoke_model_reasoning_matrix.py
```

Expected with funded, fully authorized keys: seven `PASS` lines and exit 0.
Missing keys produce explicit `SKIP` lines. Exhausted credits or unavailable
model IDs produce explicit `FAIL` lines and exit 1; preserve the output and do
not rerun.

- [ ] **Step 5: Inspect final state**

```bash
git status --short
git log -n 6 --oneline
```

Expected: no `.env` changes, no unintended files, and separate backend,
frontend, and smoke commits. Report failed/skipped real models as unverified.
