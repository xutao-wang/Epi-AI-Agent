"""Central runtime defaults for the native multi-provider application."""

from collections.abc import Mapping

DEFAULT_OPENAI_MODEL = "gpt-5.6-terra"

DEFAULT_TEMPERATURE = 0.0

DEFAULT_TOP_P = 1.0

DEFAULT_EPI_AGENT_MAX_ITERATIONS = 50
DEFAULT_MAX_AUTO_STEPS = 4

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
