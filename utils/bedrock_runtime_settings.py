"""Apply saved runtime allowances without replacing AWS identity or capabilities.

Precedence: explicit Bedrock profiles keep their runtime settings; otherwise a
unique same-provider-family OpenRouter profile supplies them, with native Terra
as fallback (and always for AWS Terra). Verified AWS maxima win in every case.
Gemma's separate, exact Mantle counterpart mapping is intentionally untouched.
"""
from collections.abc import Mapping
from dataclasses import replace

from utils.model_runtime_profiles import ModelRuntimeProfile

ALIGNED_BASES = frozenset({
    "deepseek.v3.2", "minimax.minimax-m2.5", "qwen.qwen3-coder-next", "openai.gpt-5.6-terra",
})
RUNTIME_FIELDS = (
    "initial_output_tokens", "automatic_output_token_ceiling", "user_output_token_increment",
    "absolute_output_token_ceiling", "request_timeout_seconds", "workflow_timeout_seconds",
    "routing_context_char_ceiling", "output_approval_required",
)


def runtime_source(profile: ModelRuntimeProfile, registry: Mapping[str, ModelRuntimeProfile]) -> ModelRuntimeProfile:
    """Select one saved family source, never an arbitrary first match."""
    from utils.bedrock import _canonical_base_identity

    base = _canonical_base_identity(profile.bedrock_base_model_id or profile.served_model_id)
    family = base.partition(".")[0]
    sources = [p for p in registry.values() if p.provider == "openrouter"
               and p.served_model_id.partition("/")[0].lower() == family]
    if family != "openai" and len(sources) == 1:
        return sources[0]
    return registry["gpt-5.6-terra"]


def align_bedrock_runtime_settings(
    profiles: Mapping[str, ModelRuntimeProfile], *, explicit_ids: set[str] | frozenset[str] = frozenset(),
) -> dict[str, ModelRuntimeProfile]:
    """Align selected native families and bound explicit known AWS overrides.

The registry includes saved OpenRouter profiles and native GPT Terra. Runtime
ordering is initial <= automatic <= absolute; increments also fit absolute.
Only explicit profiles are clamped outside the four amended native models.
"""
    from utils.bedrock import _canonical_base_identity, _capabilities

    result = dict(profiles)
    for model_id, profile in profiles.items():
        if profile.provider != "bedrock":
            continue
        base = _canonical_base_identity(profile.bedrock_base_model_id or profile.served_model_id)
        cap = _capabilities(base)
        if cap is None or (base not in ALIGNED_BASES and model_id not in explicit_ids):
            continue
        if model_id not in explicit_ids:
            source = runtime_source(profile, profiles)
            profile = replace(profile, **{name: getattr(source, name) for name in RUNTIME_FIELDS})
        absolute = min(profile.absolute_output_token_ceiling, cap.output)
        initial = min(profile.initial_output_tokens, absolute)
        automatic = max(initial, min(profile.automatic_output_token_ceiling, absolute))
        result[model_id] = replace(
            profile, initial_output_tokens=initial, automatic_output_token_ceiling=automatic,
            user_output_token_increment=min(profile.user_output_token_increment, absolute),
            absolute_output_token_ceiling=absolute,
            routing_context_char_ceiling=min(profile.routing_context_char_ceiling, cap.context * 2),
        )
    return result
