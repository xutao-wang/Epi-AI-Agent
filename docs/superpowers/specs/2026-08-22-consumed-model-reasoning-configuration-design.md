# Consumed Model Reasoning Configuration

**Date:** 2026-08-22

## Purpose

Make the built-in model registry the single source of truth for reasoning
behavior. Every reasoning value declared for a model must be translated into
the correct provider-client argument and affect the real API request.

The change replaces the descriptive `reasoning_tier` field with a structured
`ReasoningConfig`. OpenAI and Anthropic continue to use separate clients, but
their adapters consume the same model-level configuration instead of embedding
model policy in client constructors.

## Current problem

`ModelRuntimeProfile` currently carries two reasoning fields with different
semantics:

- `reasoning_tier` is returned in model descriptors but is not used by the
  frontend or either provider client.
- `reasoning_effort` is consumed only by the OpenAI adapter.

The Claude profiles therefore advertise High, Medium, and Low tiers without
configuring Anthropic reasoning. Claude Opus 5 and Claude Sonnet 5 omit both
`thinking` and `effort`, so Anthropic enables adaptive thinking at its default
high effort. In particular, the Sonnet profile's Medium metadata does not
produce medium-effort inference.

## Goals

- Define reasoning beside each model's name, provider, output limits, and
  timeouts in the central model registry.
- Derive every model's visible reasoning suffix from that consumed reasoning
  configuration, using `Standard` when reasoning is absent.
- Remove unused `reasoning_tier` metadata throughout Python, API schemas,
  TypeScript types, examples, and tests.
- Preserve existing OpenAI reasoning behavior.
- Configure Claude Opus 5 and Claude Sonnet 5 for adaptive thinking at medium
  effort.
- Keep Claude Haiku 4.5 in its current low-cost, thinking-off mode.
- Validate unsupported provider/configuration combinations before making an
  API request.
- Keep provider-specific keyword translation inside provider adapters.

## Non-goals

- Do not add an independently editable reasoning selector to the user
  interface; reasoning remains fixed by the selected model profile.
- Do not let reasoning settings change within an existing conversation.
- Do not change output-token limits, workflow limits, timeouts, pricing, model
  availability, or default-model selection.
- Do not enable extended thinking for Claude Haiku 4.5.
- Do not claim generic reasoning support for arbitrary OpenAI-compatible
  endpoints.

## Considered approaches

1. **Structured reasoning config in the model registry (selected).** Each
   profile owns an optional typed config, and provider adapters translate it to
   provider keywords. This keeps policy centralized while preserving explicit
   provider boundaries.
2. **Raw provider kwargs in every profile.** A `client_kwargs` dictionary would
   be flexible but would lose type safety, allow secrets or unrelated transport
   options into static policy, and defer spelling mistakes to billable calls.
3. **Model-name branches inside provider constructors.** This is initially
   small but creates a second model registry and allows the declared profile to
   disagree with runtime behavior again.

Approach 1 provides one authoritative model definition without pretending the
OpenAI and Anthropic APIs use identical request shapes.

## Configuration model

Introduce an immutable nested configuration:

```python
ReasoningMode = Literal["adaptive"]
ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]

@dataclass(frozen=True)
class ReasoningConfig:
    effort: ReasoningEffort
    mode: ReasoningMode | None = None
```

Replace both `reasoning_tier` and the top-level `reasoning_effort` profile field
with:

```python
reasoning: ReasoningConfig | None
```

The built-in registry declares actual behavior:

```python
"gpt-5.4": reasoning=None
"gpt-5.6-luna": reasoning=ReasoningConfig(effort="low")
"gpt-5.6-terra": reasoning=ReasoningConfig(effort="medium")
"gpt-5.6-sol": reasoning=ReasoningConfig(effort="medium")

"claude-opus-5": reasoning=ReasoningConfig(
    mode="adaptive",
    effort="medium",
)
"claude-sonnet-5": reasoning=ReasoningConfig(
    mode="adaptive",
    effort="medium",
)
"claude-haiku-4-5": reasoning=None
```

The internal lightweight title/naming model retains its current low effort by
using `ReasoningConfig(effort="low")`.

## Derived display labels

Visible reasoning text must not be another manually maintained field. A model
profile derives its suffix directly from `reasoning`:

```python
def reasoning_display(config: ReasoningConfig | None) -> str:
    return "Standard" if config is None else config.effort.title()

label = f"{base_label} ({reasoning_display(reasoning)})"
```

Profiles store `base_label` such as `Claude Sonnet 5`; their existing `label`
interface becomes a derived property. Consequently, model descriptors,
conversation state, output-limit messages, locked-model controls, and model
selectors all receive the same derived label. This produces the following
built-in UI labels:

```text
gpt-5.4 (Standard)
gpt-5.6-luna (Low)
gpt-5.6-terra (Medium)
gpt-5.6-sol (Medium)
Claude Opus 5 (Medium)
Claude Sonnet 5 (Medium)
Claude Haiku 4.5 (Standard)
```

`Standard` means that the application sends no explicit reasoning or thinking
configuration. It does not claim that the provider lacks ordinary model
inference. Custom compatible profiles also derive `Standard` while reasoning
is unsupported for that provider type. The frontend continues rendering the
server-provided `label`; it does not infer effort from model names.

## Provider translation

The OpenAI adapter consumes a non-null reasoning configuration by passing:

```python
reasoning_effort=profile.reasoning.effort
```

OpenAI profiles must not declare `mode`, because the current OpenAI adapter has
no corresponding mode parameter. The existing Responses API configuration and
response chaining remain unchanged.

The Anthropic adapter consumes a non-null reasoning configuration by passing:

```python
thinking={"type": profile.reasoning.mode}
effort=profile.reasoning.effort
```

Anthropic profiles with reasoning must declare `mode="adaptive"`. A null
configuration sends neither keyword. Consequently, Haiku does not receive an
unsupported adaptive-thinking or effort parameter.

The compatible-endpoint adapter initially requires `reasoning=None` and sends
no reasoning keyword. Endpoint-specific reasoning contracts can be designed
later rather than assuming OpenAI-compatible servers implement the same
extension.

## Validation and errors

Validate built-in profiles when the registry is constructed or first loaded:

- OpenAI accepts a null config or a config with no mode and an effort supported
  by the existing OpenAI integration.
- Anthropic Opus 5 and Sonnet 5 accept adaptive mode with a supported effort.
- Anthropic Haiku 4.5 and compatible endpoints use a null config in this
  design.
- A reasoning mode without an effort, an unsupported mode, or a provider/config
  mismatch raises a configuration error before provider-client construction.

Validation messages identify the model ID and invalid field without including
API keys or other secrets.

## Descriptor and custom-model cleanup

Remove `reasoning_tier` from model descriptors and the public runtime-options
schema. Remove the matching frontend `ModelOption` property and test fixtures.
The model picker continues rendering only the model descriptor's `label`, but
that label now includes the backend-derived reasoning suffix for every model.

Remove `reasoning_tier` from `CustomModelEntry` and
`config/custom_models.example.json`. Custom compatible models receive
`reasoning=None`. An operator file that still contains `reasoning_tier` fails
the existing strict schema with a clear extra-field error; the example and
documentation tell operators to delete that obsolete property.

## Testing

Focused tests exercise the production model factory with captured constructor
arguments:

1. Opus 5 sends adaptive thinking and medium effort.
2. Sonnet 5 sends adaptive thinking and medium effort.
3. Haiku 4.5 sends neither thinking nor effort.
4. GPT-5.6 Luna, Terra, and Sol retain low, medium, and medium
   `reasoning_effort` values respectively.
5. GPT-5.4 sends no reasoning effort.
6. Provider-incompatible reasoning configs fail before client construction.
7. Runtime-option API and frontend tests no longer require `reasoning_tier`.
8. Custom-model parsing rejects the removed property and accepts an otherwise
   equivalent entry without it.
9. A table-driven assertion covers every built-in model's reasoning config,
   provider constructor kwargs, and exact derived label.
10. The model picker renders every available built-in model with the exact
    backend-provided derived label and never constructs a suffix itself.

Per repository policy, add a dedicated backend smoke script that exercises the
real production `build_chat_llm` boundary. It checks every built-in model
available under the configured credentials exactly once, with the smallest
useful prompt, and records the selected model, derived reasoning display,
provider response model, and usage without printing credentials. The complete
smoke has one five-minute deadline and never retries a model automatically.

The smoke can make billable OpenAI and Anthropic requests. It requires funded
provider keys and access to each registered model. An exhausted account or an
unsupported model is preserved as that model's explicit verification failure;
it is not silently skipped, substituted, or reported as a passing check.

## Acceptance criteria

- There is one reasoning declaration per built-in model profile.
- No operational `reasoning_tier` reference remains in production code,
  schemas, examples, frontend types, or ordinary fixtures. The sole permitted
  test reference is the legacy-input rejection case proving that operators get
  an explicit validation failure for the removed custom-model property.
- Every model label suffix is derived from the consumed `reasoning` field;
  absent reasoning displays as `Standard`.
- Opus 5 and Sonnet 5 requests carry adaptive thinking and medium effort.
- Haiku 4.5 carries neither reasoning keyword.
- Existing OpenAI reasoning request arguments are unchanged.
- The exhaustive built-in model matrix, affected backend/frontend suites, and
  one-pass real-provider smoke produce recorded verification results for every
  model rather than sampling one representative model.
