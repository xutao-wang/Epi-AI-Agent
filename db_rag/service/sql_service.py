from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from utils.performance import timing_stage

from .models import (
    SqlExecutionResult,
    ValidatedExtractionSql,
)
from ..generation import validate_sql


def _quote_duckdb_identifier(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _validate_approved_schema_references(
    sql: str,
    *,
    approved_tables: list[str],
    approved_columns: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    try:
        import sqlglot
        from sqlglot import exp
    except ModuleNotFoundError:
        return True, None

    try:
        expression = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception:
        return True, None

    approved_table_names = {str(table or "").strip().casefold() for table in approved_tables if str(table or "").strip()}
    approved_column_names = {
        str(column.get("column") or "").strip().casefold()
        for column in approved_columns
        if str(column.get("column") or "").strip()
    }
    cte_names = {
        str(cte.alias_or_name or "").strip().casefold()
        for cte in expression.find_all(exp.CTE)
        if str(cte.alias_or_name or "").strip()
    }
    for select in expression.find_all(exp.Select):
        if any(
            isinstance(projection, exp.Star)
            or bool(getattr(projection, "is_star", False))
            for projection in select.expressions
        ):
            return False, "SQL wildcard projections are not approved."
    table_aliases: dict[str, str] = {}
    for table in expression.find_all(exp.Table):
        table_name = str(table.name or "").strip()
        if not table_name:
            continue
        normalized_table = table_name.casefold()
        if normalized_table in cte_names:
            continue
        if approved_table_names and normalized_table not in approved_table_names:
            return False, f"SQL references unapproved table: {table_name}"
        alias = str(table.alias_or_name or table_name).strip()
        if alias:
            table_aliases[alias.casefold()] = normalized_table

    for column in expression.find_all(exp.Column):
        column_name = str(column.name or "").strip()
        if not column_name:
            continue
        qualifier = str(column.table or "").strip().casefold()
        if qualifier and qualifier in cte_names:
            continue
        if qualifier and qualifier not in table_aliases and qualifier not in approved_table_names:
            continue
        if approved_column_names and column_name.casefold() not in approved_column_names:
            return False, f"SQL references unapproved column: {column_name}"

    return True, None


def _validate_relation_provenance(
    sql: str,
    *,
    approved_tables: list[str],
) -> tuple[bool, str | None]:
    try:
        import sqlglot
        from sqlglot import exp

        expression = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception:
        return True, None

    approved = {
        str(table or "").strip().casefold()
        for table in approved_tables
        if str(table or "").strip()
    }
    cte_names = {
        str(cte.alias_or_name or "").strip().casefold()
        for cte in expression.find_all(exp.CTE)
        if str(cte.alias_or_name or "").strip()
    }

    for select in expression.find_all(exp.Select):
        direct_relations = [
            *(
                [select.args["from_"].this]
                if select.args.get("from_") is not None
                else []
            ),
            *(
                join.this
                for join in select.args.get("joins") or []
            ),
        ]
        if not direct_relations:
            return (
                False,
                (
                    "SQL relation provenance requires every query branch and "
                    "CTE to derive from an approved physical table."
                ),
            )

    relation_nodes = [
        *(node.this for node in expression.find_all(exp.From)),
        *(node.this for node in expression.find_all(exp.Join)),
    ]
    for relation in relation_nodes:
        if isinstance(relation, exp.Subquery):
            continue
        if not isinstance(relation, exp.Table):
            return (
                False,
                (
                    "SQL relation provenance is not derived from an approved physical table: "
                    f"{relation.sql(dialect='duckdb')}."
                ),
            )
        if not isinstance(relation.this, exp.Identifier):
            return (
                False,
                (
                    "SQL relation provenance is unapproved: table functions "
                    "and external scans are forbidden."
                ),
            )
        if relation.args.get("db") is not None or relation.args.get("catalog") is not None:
            return (
                False,
                "SQL relation provenance is unapproved: qualified external relations are forbidden.",
            )
        relation_name = str(relation.name or "").strip()
        normalized_name = relation_name.casefold()
        if normalized_name in cte_names:
            continue
        if normalized_name not in approved:
            return (
                False,
                f"SQL relation provenance is unapproved: {relation_name or '<anonymous>'}.",
            )
    return True, None


def _validate_runtime_schema_sql(
    sql: str,
    *,
    database_path: str | Path | None,
) -> tuple[bool, str | None]:
    if database_path is None:
        return False, "SQL runtime schema validation requires a selected study database."
    runtime_path = Path(database_path)
    if not runtime_path.exists():
        return True, None

    try:
        import duckdb
    except ModuleNotFoundError:
        return True, None

    sql_to_describe = str(sql or "").strip().rstrip(";")
    if not sql_to_describe:
        return False, "SQL is empty."

    with timing_stage("db_rag.sql.runtime_schema_preflight"):
        db = duckdb.connect(str(runtime_path), read_only=True)
        try:
            db.execute(f"DESCRIBE {sql_to_describe}")
        except Exception as exc:
            return False, (
                "SQL failed DuckDB runtime schema validation before execution: "
                f"{type(exc).__name__}: {exc}. "
                "Use only tables and columns that exist in the DuckDB runtime schema."
            )
        finally:
            db.close()
    return True, None


def validate_extraction_sql(
    *,
    sql: str,
    approved_tables: list[str],
    approved_columns: list[dict[str, Any]],
    database_path: str | Path,
) -> ValidatedExtractionSql:
    checks = (
        validate_sql(sql),
        _validate_relation_provenance(
            sql,
            approved_tables=approved_tables,
        ),
        _validate_approved_schema_references(
            sql,
            approved_tables=approved_tables,
            approved_columns=approved_columns,
        ),
    )
    for valid, error in checks:
        if not valid:
            raise ValueError(
                error or "SQL extraction safety validation failed."
            )
    valid, error = _validate_runtime_schema_sql(
        sql,
        database_path=database_path,
    )
    if not valid:
        raise ValueError(error or "SQL runtime schema validation failed.")
    return ValidatedExtractionSql(
        sql=sql,
        sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
    )


def execute_validated_extraction_sql(
    validated: ValidatedExtractionSql,
    *,
    source_tables: list[str],
    database_path: str | Path,
) -> SqlExecutionResult:
    import duckdb

    db = duckdb.connect(str(database_path), read_only=True)
    try:
        dataframe = db.execute(validated.sql).fetchdf()
    finally:
        db.close()
    return SqlExecutionResult(
        answer=(
            "Read-only SQL execution completed with "
            f"{len(dataframe)} result row(s)."
        ),
        sql=validated.sql,
        dataframe=dataframe,
        source_tables=list(source_tables),
    )
