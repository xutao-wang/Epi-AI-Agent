#!/usr/bin/env python3
"""Verify active DB-RAG review styles through the real backend and compiled UI."""

from __future__ import annotations

import argparse
from typing import Any

import e2e_agent_activity_timeline_real as harness


def _assert_active_styles(page: Any) -> None:
    concept_heading = page.locator(".db-rag-concept-card h3").first
    concept_heading.wait_for(state="visible")
    concept_style = concept_heading.evaluate(
        "element => ({ fontSize: getComputedStyle(element).fontSize, "
        "fontWeight: getComputedStyle(element).fontWeight })"
    )
    if concept_style != {"fontSize": "20px", "fontWeight": "650"}:
        raise AssertionError(f"Unexpected concept heading style: {concept_style!r}")

    linkage = page.locator(".db-rag-linkage-section").first
    linkage.wait_for(state="visible")
    linkage_style = linkage.evaluate(
        "element => { const style = getComputedStyle(element); return { "
        "display: style.display, gap: style.gap, marginTop: style.marginTop, "
        "borderTopStyle: style.borderTopStyle, paddingTop: style.paddingTop }; }"
    )
    expected = {
        "display": "grid",
        "gap": "14px",
        "marginTop": "20px",
        "borderTopStyle": "solid",
        "paddingTop": "16px",
    }
    if linkage_style != expected:
        raise AssertionError(f"Unexpected linkage style: {linkage_style!r}")


def run(args: argparse.Namespace) -> int:
    original_wait = harness._wait_for_dataset_plan_review

    def wait_and_assert(page: Any, **kwargs: Any) -> None:
        original_wait(page, **kwargs)
        _assert_active_styles(page)

    harness._wait_for_dataset_plan_review = wait_and_assert
    return harness.run(args)


if __name__ == "__main__":
    raise SystemExit(run(harness._parser().parse_args()))
