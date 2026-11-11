#!/usr/bin/env python3.12
"""Once-only real installed-study smoke for plan filter validation.

Exercises production package loading, catalog, DuckDB and save/validate tools.
No model inference or embedding search is involved in this internal validator.
The installed data and reference files are read-only; this does not repair XPT
decoding or certify the scientific correctness of the installed data.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import signal
import sys
import traceback

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

import duckdb

from db_rag.study import build_study_bundle
from epi_agent.artifacts import StateArtifactStore
from epi_agent.db_rag.tools import build_db_rag_tool_registry, _quote_duckdb_identifier
from epi_agent.protocol import ToolContext, ToolExecutionError
from epi_agent.studies import StudyRegistry
from epi_agent.tool_call_protocol import tool_error_content
from study_package.installer import load_installed_study
from study_package.registry import load_registry, package_root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studies-root", type=Path, default=APP_ROOT / "study_data/studies")
    parser.add_argument("--study-id", default="nhanes-2017-2018")
    parser.add_argument("--table", default="GLU_J")
    parser.add_argument("--column", default="WTSAF2YR")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output_root or APP_ROOT / "runtime/validation" / f"filter-threshold-smoke-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    report = {"status": "running", "cases": [], "actor_calls": 0, "judge_calls": 0,
        "command": sys.argv, "scope": __doc__}
    database_path = None
    before = None

    def deadline(*_):
        raise TimeoutError("Five-minute smoke deadline exceeded")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(300)
    try:
        version = load_registry(args.studies_root).active[args.study_id]
        installed = load_installed_study(package_root(args.studies_root, args.study_id, version))
        study = build_study_bundle(installed)
        database_path = study.db_rag_paths.duckdb_path
        before = hashlib.sha256(database_path.read_bytes()).hexdigest()
        store = StateArtifactStore()
        context = ToolContext(studies=StudyRegistry([study]), artifact_store=store,
            thread_id="filter-threshold-smoke", policy=object())
        registry = build_db_rag_tool_registry()
        table, column = map(_quote_duckdb_identifier, (args.table, args.column))
        with duckdb.connect(str(database_path), read_only=True) as connection:
            minimum = connection.execute(f"SELECT MIN({column}) FROM {table}").fetchone()[0]
            assert minimum is not None and math.isfinite(minimum)
            absent = math.floor(minimum) - 1
            assert connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", [absent]).fetchone()[0] == 0
            zero_count = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = 0").fetchone()[0]
            empty_count = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} < ?", [absent]).fetchone()[0]
            assert empty_count == 0
        report.update(study_id=args.study_id, package_version=version, database=str(database_path),
            observed_minimum=minimum, absent_threshold=absent, stored_zero_count=zero_count)

        def run_case(operator, value, expected):
            c = {"source": study.source_id, "table": args.table, "column": args.column,
                "operator": operator, **({"values": value} if isinstance(value, list) else {"value": value})}
            arguments = {"plan": {"study_id": args.study_id,
                "goal": "Validate a filter without requiring comparison thresholds to occur.",
                "concepts": [{"concept_id": "measurement", "label": "Measurement", "fields": [
                    {"source": study.source_id, "table": args.table, "column": args.column}]}],
                "filters": [{"description": "Requested condition", "value_constraints": [c]}]}}
            case = {"operator": operator, "value": value, "expected_valid": expected, "save_arguments": arguments}
            report["cases"].append(case)
            saved = registry.invoke("dbrag-save_dataset_plan", arguments, context=context)
            reference = saved.artifacts[0]
            try:
                result = registry.invoke("dbrag-validate_dataset_plan", {
                    "plan_id": reference.id, "plan_version": reference.version}, context=context)
            except ToolExecutionError as error:
                case.update(valid=False, model_visible_error=json.loads(tool_error_content(error)))
                assert not expected, case
                assert error.code == "PLAN_FILTER_VALUE_UNAVAILABLE", case
            else:
                case.update(valid=True, message=result.message)
                assert expected, case

        for operator, value, expected in [
            (">", 0, True), (">", absent, True), ("<", absent, True),
            ("!=", absent, True), ("NOT IN", [absent], True), ("!=", None, True),
            ("=", minimum, True), ("IN", [minimum], True),
            ("=", absent, False), ("IN", [minimum, absent], False),
            (">", "not-a-number", False),
        ]:
            run_case(operator, value, expected)
        (output / "artifacts.json").write_text(json.dumps(store.snapshot(), indent=2) + "\n")
        report["status"] = "passed"
    except BaseException:
        report["status"] = "failed"
        report["traceback"] = traceback.format_exc()
        raise
    finally:
        signal.alarm(0)
        if database_path and before:
            after = hashlib.sha256(database_path.read_bytes()).hexdigest()
            report.update(database_sha256_before=before, database_sha256_after=after, database_unchanged=before == after)
            if before != after:
                report["status"] = "failed"
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "cases": len(report["cases"]), "report": str(output / "report.json")}))
    assert report["status"] == "passed"


if __name__ == "__main__":
    main()
