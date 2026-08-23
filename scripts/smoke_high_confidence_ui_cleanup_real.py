#!/usr/bin/env python3
"""Verify active UI paths after the high-confidence dead-code cleanup."""

from __future__ import annotations

import argparse
from typing import Any

import e2e_agent_activity_timeline_real as harness
from smoke_active_db_rag_styles_real import _assert_active_styles


def run(args: argparse.Namespace) -> int:
    original_wait = harness._wait_for_dataset_plan_review

    def wait_and_assert(page: Any, **kwargs: Any) -> None:
        original_wait(page, **kwargs)
        _assert_active_styles(page)
        if page.locator(".runtime-settings-panel, .db-rag-sql-panel").count():
            raise AssertionError("Retired UI markup appeared in the live review.")

    harness._wait_for_dataset_plan_review = wait_and_assert
    return harness.run(args)


if __name__ == "__main__":
    raise SystemExit(run(harness._parser().parse_args()))
