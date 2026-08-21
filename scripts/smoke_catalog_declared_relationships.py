#!/usr/bin/env python3
"""Exercise catalog-v2 relationships through production DB-RAG tools."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import signal
import sys
import tempfile
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from epi_agent.artifacts import StateArtifactStore
from epi_agent.db_rag.tools import build_db_rag_tool_registry
from epi_agent.protocol import ToolContext, ToolExecutionError
from study_package.installer import install_study_archives
from study_package.registry import discover_studies


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-archive", type=Path, required=True)
    parser.add_argument("--nhanes-archive", type=Path, required=True)
    return parser


def _timeout(_signum: int, _frame: object) -> None:
    raise TimeoutError("catalog relationship smoke exceeded 300 seconds")


def _table_ref(study_id: str, table: str) -> dict[str, str]:
    return {
        "study_id": study_id,
        "source_id": study_id,
        "table": table,
    }


def _field_ref(study_id: str, table: str, column: str) -> dict[str, str]:
    return {
        **_table_ref(study_id, table),
        "column": column,
    }


def _run(args: argparse.Namespace, artifact_root: Path) -> None:
    archives = [
        args.report_archive.expanduser().resolve(),
        args.nhanes_archive.expanduser().resolve(),
    ]
    for archive in archives:
        if not archive.is_file():
            raise FileNotFoundError(f"Study archive not found: {archive}")

    studies_root = artifact_root / "studies"
    install_study_archives(archives, studies_root)
    studies = discover_studies(studies_root)
    context = ToolContext(
        studies=studies,
        artifact_store=StateArtifactStore(),
        thread_id="catalog-relationships-smoke",
        policy=object(),
    )
    tools = build_db_rag_tool_registry()
    tool_messages: list[dict[str, Any]] = []
    diagnostics_path = artifact_root / "tool-messages.json"

    def record(name: str, message_text: str) -> dict[str, Any]:
        message = json.loads(message_text)
        tool_messages.append({"tool": name, "message": message})
        diagnostics_path.write_text(
            json.dumps(tool_messages, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return message

    report_id = "report-india-synthetic"
    report_forward = tools.invoke(
        "dbrag-profile_relationship",
        {
            "left_table_ref": _table_ref(report_id, "Enrollment Cohort B"),
            "right_table_ref": _table_ref(report_id, "Enrollment Cohort A"),
            "key_pairs": [
                {"left_column": "INDEXPID", "right_column": "SUBJID"}
            ],
        },
        context=context,
    )
    forward = record("report_explicit_forward", report_forward.message)
    evidence = forward["profile"]["relationship_evidence"][0]
    assert evidence["relationship_id"] == "cohort_b_index_case"
    assert evidence["expected_cardinality"] == "many_to_one"
    assert evidence["direction"] == "forward"
    assert evidence["note"]
    assert forward["profile"]["matched_keys"] > 0

    report_reverse = tools.invoke(
        "dbrag-profile_relationship",
        {
            "left_table_ref": _table_ref(report_id, "Enrollment Cohort A"),
            "right_table_ref": _table_ref(report_id, "Enrollment Cohort B"),
            "key_pairs": [
                {"left_column": "SUBJID", "right_column": "INDEXPID"}
            ],
        },
        context=context,
    )
    reverse = record("report_explicit_reverse", report_reverse.message)
    reverse_evidence = reverse["profile"]["relationship_evidence"][0]
    assert reverse_evidence["left_column"] == "SUBJID"
    assert reverse_evidence["right_column"] == "INDEXPID"
    assert reverse_evidence["direction"] == "reverse"
    assert reverse_evidence["expected_cardinality"] == "one_to_many"

    report_shared = tools.invoke(
        "dbrag-find_join_paths",
        {
            "required_fields": [
                _field_ref(report_id, "Enrollment Cohort A", "SUBJID"),
                _field_ref(
                    report_id,
                    "Baseline Clinical and Demographic Information Cohort A",
                    "SUBJID",
                ),
            ],
            "max_hops": 3,
            "max_paths": 10,
        },
        context=context,
    )
    report_path = record("report_shared_path", report_shared.message)
    report_path_evidence = report_path["paths"][0]["profiles"][0][
        "relationship_evidence"
    ][0]
    assert report_path_evidence["source"] == "shared_join_key"
    assert report_path_evidence["left_join_key"] == "subjid"
    assert report_path_evidence["right_join_key"] == "subjid"

    nhanes_id = "nhanes-2017-2018"
    nhanes_shared = tools.invoke(
        "dbrag-find_join_paths",
        {
            "required_fields": [
                _field_ref(nhanes_id, "DEMO_J", "SEQN"),
                _field_ref(nhanes_id, "DIQ_J", "SEQN"),
            ],
            "max_hops": 3,
            "max_paths": 10,
        },
        context=context,
    )
    nhanes_path = record("nhanes_shared_path", nhanes_shared.message)
    nhanes_evidence = nhanes_path["paths"][0]["profiles"][0][
        "relationship_evidence"
    ][0]
    assert nhanes_evidence["source"] == "shared_join_key"
    assert nhanes_evidence["left_join_key"] == "seqn"
    assert nhanes_evidence["right_join_key"] == "seqn"

    try:
        tools.invoke(
            "dbrag-profile_relationship",
            {
                "left_table_ref": _table_ref(nhanes_id, "DEMO_J"),
                "right_table_ref": _table_ref(nhanes_id, "DIQ_J"),
                "key_pairs": [
                    {"left_column": "RIAGENDR", "right_column": "DIQ010"}
                ],
            },
            context=context,
        )
    except ToolExecutionError as error:
        if error.code != "RELATIONSHIP_UNAVAILABLE":
            raise
        tool_messages.append(
            {"tool": "nhanes_undeclared_pair", "error_code": error.code}
        )
        diagnostics_path.write_text(
            json.dumps(tool_messages, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        raise AssertionError("NHANES undeclared non-key pair was accepted")

    print(
        "catalog-v2 relationship smoke passed: RePORT explicit forward/reverse, "
        "RePORT shared path, NHANES shared path, and undeclared-pair rejection"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    artifact_root = Path(
        tempfile.mkdtemp(prefix="catalog-relationships-smoke-")
    )
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(300)
    try:
        _run(args, artifact_root)
    except BaseException:
        print(f"Smoke artifacts preserved at: {artifact_root}", file=sys.stderr)
        raise
    else:
        shutil.rmtree(artifact_root)
    finally:
        signal.alarm(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
