from __future__ import annotations

import json
import logging
from typing import Any

from epi_agent.runtime import ContextPromptError
from epi_agent.studies import StudyBundle, StudyRegistry


_MAX_ERROR_CHARS = 300
_DEFAULT_MAX_CONTEXT_CHARS = 262_144
_LOGGER = logging.getLogger(__name__)


class StudyRoutingContextError(ContextPromptError):
    """Installed study evidence cannot be represented safely."""


def _unavailable(study: StudyBundle, error: str) -> dict[str, Any]:
    return {
        "study_id": study.study_id,
        "label": study.label,
        "overview_available": False,
        "error": str(error).strip()[:_MAX_ERROR_CHARS],
    }


def _entry(study: StudyBundle) -> dict[str, Any]:
    provider = study.study_overview
    render_context = getattr(provider, "render_context", None)
    if not callable(render_context):
        return _unavailable(
            study,
            "This installed study does not provide overview.md routing evidence.",
        )
    try:
        overview = str(render_context() or "").strip()
    except Exception:
        _LOGGER.warning(
            "Unable to render installed study overview for routing",
            extra={"study_id": study.study_id},
            exc_info=True,
        )
        return _unavailable(study, "overview_unreadable")
    if not overview:
        return _unavailable(study, "The installed study overview is empty.")
    return {
        "study_id": study.study_id,
        "label": study.label,
        "overview_available": True,
        "overview": overview,
    }


def render_installed_study_context(
    studies: StudyRegistry,
    *,
    max_chars: int = _DEFAULT_MAX_CONTEXT_CHARS,
) -> str:
    payload = {
        "context_kind": "installed_study_routing_evidence",
        "study_count": len(studies.values),
        "studies": [
            _entry(study)
            for study in sorted(
                studies.values,
                key=lambda item: item.study_id,
            )
        ],
    }
    rendered = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(rendered) > max_chars:
        raise StudyRoutingContextError(
            "Complete installed-study routing context exceeds the configured "
            f"{max_chars}-character input ceiling; no overview was omitted."
        )
    return rendered


__all__ = [
    "StudyRoutingContextError",
    "render_installed_study_context",
]
