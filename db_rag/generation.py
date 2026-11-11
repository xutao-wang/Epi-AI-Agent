from __future__ import annotations

import json
import re
from typing import Any

_DANGEROUS_SQL = (
    "DROP",
    "DELETE",
    "UPDATE",
    "INSERT",
    "ALTER",
    "TRUNCATE",
    "ATTACH",
    "DETACH",
    "CREATE",
    "REPLACE",
    "COPY",
    "PRAGMA",
)


def extract_sql(text: str) -> str:
    text = str(text or "").strip()
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    starts: list[int] = []
    with_match = re.search(r"\bWITH\b", text, re.IGNORECASE)
    if with_match:
        starts.append(with_match.start())
    select_match = re.search(r"\bSELECT\b", text, re.IGNORECASE)
    if select_match:
        starts.append(select_match.start())
    if starts:
        return text[min(starts):].strip()
    return text


def is_unanswerable_response(text: str) -> bool:
    return str(text or "").strip().upper().startswith("UNANSWERABLE")


def parse_json_object(text: str) -> dict[str, Any]:
    raw_text = str(text or "").strip()
    if not raw_text:
        return {}

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}

    return parsed if isinstance(parsed, dict) else {}


def build_sql_policy_text() -> str:
    return (
        "JOIN HINTS:\n"
        "Use only exact join columns supplied by the study package catalog and relationship context.\n"
        "If one table suffices, do NOT invent joins.\n\n"
        "RULES:\n"
        "- Use DuckDB SQL.\n"
        "- Use ILIKE for case-insensitive string comparisons.\n"
        "- Prefer the stored/sample-value representation over coded allowed values when they differ.\n"
        "- Apply standard missing-code exclusions when row_filters.data_quality contains a policy. "
        "Values of 99, 999, or 9999 are often missing data codes and should be excluded unless the question is explicitly about missingness.\n"
        "- Do not compare VARCHAR/text columns to numeric missing-code literals; use quoted string literals "
        "if those codes are actually stored as text, or TRY_CAST(column AS INTEGER) before numeric missing-code comparisons.\n"
        "- Only add IS NOT NULL for high-NULL columns when row_filters.data_quality calls for excluding missing values."
    )


def _mask_sql_literals_identifiers_and_comments(sql: str) -> str:
    masked: list[str] = []
    quote_char: str | None = None
    i = 0
    while i < len(sql):
        char = sql[i]
        if quote_char:
            masked.append(" ")
            if char == quote_char:
                if i + 1 < len(sql) and sql[i + 1] == quote_char:
                    masked.append(" ")
                    i += 2
                    continue
                quote_char = None
            i += 1
            continue
        if char == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            masked.extend("  ")
            i += 2
            while i < len(sql) and sql[i] not in "\r\n":
                masked.append(" ")
                i += 1
            continue
        if char == "/" and i + 1 < len(sql) and sql[i + 1] == "*":
            masked.extend("  ")
            i += 2
            while i < len(sql):
                if sql[i] == "*" and i + 1 < len(sql) and sql[i + 1] == "/":
                    masked.extend("  ")
                    i += 2
                    break
                masked.append(" ")
                i += 1
            continue
        if char in {"'", '"'}:
            quote_char = char
            masked.append(" ")
            i += 1
            continue
        masked.append(char)
        i += 1
    return "".join(masked)


def check_sql_quality(sql: str) -> list[str]:
    warnings: list[str] = []
    policy_sql_upper = _mask_sql_literals_identifiers_and_comments(str(sql or "")).upper()
    if re.search(r"\bORDER\s+BY\b", policy_sql_upper) and not re.search(r"\bNULLS\s+(?:LAST|FIRST)\b", policy_sql_upper):
        warnings.append("ORDER BY without NULLS LAST or NULLS FIRST is not allowed.")
    if "/" in policy_sql_upper and not re.search(r"\bNULLIF\s*\(", policy_sql_upper):
        warnings.append("Division without NULLIF is not allowed.")
    if (
        re.search(r"\bCOUNT\s*\(\s*\*\s*\)", policy_sql_upper)
        and not re.search(r"\bWHERE\b", policy_sql_upper)
        and not re.search(r"\bGROUP\s+BY\b", policy_sql_upper)
    ):
        warnings.append("COUNT(*) without WHERE or GROUP BY is not allowed.")
    return warnings


def sql_validation_issues(sql: str, *, check_syntax: bool = True) -> list[dict[str, str]]:
    """Collect diagnostics; callers owning the parse may disable this syntax pass."""
    policy_sql = _mask_sql_literals_identifiers_and_comments(str(sql or ""))
    sql_upper = policy_sql.strip().upper()
    if is_unanswerable_response(sql):
        return []
    issues: list[dict[str, str]] = []

    def add(code: str, message: str, stage: str = "policy") -> None:
        issues.append({"stage": stage, "code": code, "message": message})

    if not sql_upper.startswith(("SELECT", "WITH")):
        add("SQL_READ_ONLY_REQUIRED", "Only read-only SELECT/WITH SQL is allowed.")
    if re.search(r";\s*\S", sql_upper):
        add("SQL_MULTIPLE_STATEMENTS", "SQL contains multiple statements, which is not allowed.")
    if any(re.search(rf"\b{re.escape(keyword)}\b", sql_upper) for keyword in _DANGEROUS_SQL):
        add("SQL_DISALLOWED_KEYWORDS", "SQL contains disallowed mutating or DDL keywords.")

    if check_syntax:
        try:
            import sqlglot

            sqlglot.parse_one(sql, dialect="duckdb")
        except ModuleNotFoundError:
            pass
        except Exception as exc:  # pragma: no cover
            add("SQL_SYNTAX_ERROR", str(exc), "syntax")
    for message in check_sql_quality(sql):
        add("SQL_QUALITY_ERROR", message)
    return issues


def validate_sql(sql: str) -> tuple[bool, str | None]:
    """Preserve the existing first-error interface for older callers."""
    issues = sql_validation_issues(sql)
    return not issues, issues[0]["message"] if issues else None
