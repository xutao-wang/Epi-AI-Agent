from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PreparedSqlCandidate:
    question: str
    sql: str
    tables: list[str]
    columns: list[dict[str, str]]
    selection_id: str
    status: str = "prepared"
    variable_roles: dict[str, Any] = field(default_factory=dict)
    row_filters: dict[str, Any] = field(default_factory=dict)
    protected_columns: list[dict[str, Any]] = field(default_factory=list)
    applied_filters: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ValidatedExtractionSql:
    sql: str
    sha256: str


@dataclass
class SqlExecutionResult:
    answer: str
    sql: str
    dataframe: Any
    source_tables: list[str]
