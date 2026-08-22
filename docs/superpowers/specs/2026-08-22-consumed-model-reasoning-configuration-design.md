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

- Do not add a reasoning selector to the user interface.
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
The model picker remains visually unchanged because it already renders only
the model label.

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

Per repository policy, add a dedicated backend smoke script that exercises the
real production `build_chat_llm` boundary. The real-provider portion must run
at most once, finish within five minutes, and report the response usage without
printing credentials. Because it makes a billable Anthropic request, it
requires a funded `ANTHROPIC_API_KEY`; if the account remains exhausted, retain
and report that provider error rather than retrying automatically.

## Acceptance criteria

- There is one reasoning declaration per built-in model profile.
- No `reasoning_tier` reference remains in production code, schemas, examples,
  frontend types, or maintained tests.
- Opus 5 and Sonnet 5 requests carry adaptive thinking and medium effort.
- Haiku 4.5 carries neither reasoning keyword.
- Existing OpenAI reasoning request arguments are unchanged.
- Focused tests, affected backend/frontend suites, and the required smoke
  produce recorded verification results.
