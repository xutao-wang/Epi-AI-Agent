"""Central runtime defaults for the native multi-provider application."""

from collections.abc import Mapping

from utils.model_availability import (
    ModelAvailability,
    model_availability_from_configured_credentials,
)
from utils.model_runtime_profiles import (
    MODEL_RUNTIME_PROFILES,
    PROVIDER_OPENAI,
)


AVAILABLE_OPENAI_MODELS = tuple(
    model_id
    for model_id, profile in MODEL_RUNTIME_PROFILES.items()
    if profile.provider == PROVIDER_OPENAI
)

DEFAULT_OPENAI_MODEL = "gpt-5.6-terra"

DEFAULT_TEMPERATURE = 0.0
TEMPERATURE_RANGE = (0.0, 1.0)
TEMPERATURE_STEP = 0.05

DEFAULT_TOP_P = 1.0
TOP_P_RANGE = (0.5, 1.0)
TOP_P_STEP = 0.05

DEFAULT_EPI_AGENT_MAX_ITERATIONS = 50
DEFAULT_MAX_AUTO_STEPS = 4
MAX_AUTO_STEPS_RANGE = (1, 8)

DEFAULT_EXECUTION_TIMEOUT_SEC = 60
EXECUTION_TIMEOUT_RANGE = (5, 120)
EXECUTION_TIMEOUT_STEP = 5

def configured_epi_agent_max_iterations(
    environ: Mapping[str, str],
) -> int:
    name = "REPORT_AGENT_MAX_ITERATIONS"
    if name not in environ:
        return DEFAULT_EPI_AGENT_MAX_ITERATIONS
    configured = str(environ[name]).strip()
    try:
        value = int(configured)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be a positive integer"
        ) from exc
    if value < 1 or str(value) != configured:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _availability(
    environ: Mapping[str, str],
    model_availability: ModelAvailability | None = None,
) -> ModelAvailability:
    return model_availability or model_availability_from_configured_credentials(
        environ
    )


def configured_default_model(
    environ: Mapping[str, str],
    model_availability: ModelAvailability | None = None,
) -> str:
    return _availability(environ, model_availability).default_model_id


def configured_models(
    environ: Mapping[str, str],
    model_availability: ModelAvailability | None = None,
) -> tuple[str, ...]:
    return _availability(environ, model_availability).available_model_ids


def configured_openai_models(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Deprecated alias for configured_models."""
    return configured_models(environ)


def configured_title_model(
    environ: Mapping[str, str],
    model_availability: ModelAvailability | None = None,
) -> str:
    return _availability(environ, model_availability).title_model_id
