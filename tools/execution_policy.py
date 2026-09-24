"""Validate generated Python against execution restrictions.

See python_execution_policy.md for the execution contract.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from typing import Literal


ERROR_CATEGORY_POLICY_BLOCKED = "policy_blocked"
POLICY_VIOLATION_LIMIT = 10
ValidationFailureCode = Literal["INVALID_PYTHON_SYNTAX", "POLICY_BLOCKED"]


@dataclass(frozen=True)
class PythonPolicyViolation:
    rule_id: str
    operation: str
    message: str
    correction: str
    line: int
    column: int


@dataclass(frozen=True)
class PythonCodeValidation:
    failure_code: ValidationFailureCode | None = None
    message: str = ""
    violations: tuple[PythonPolicyViolation, ...] = ()
    truncated: bool = False
    line: int | None = None
    column: int | None = None

    def as_details(self) -> dict[str, object]:
        details: dict[str, object] = {}
        if self.violations:
            details["violations"] = [asdict(item) for item in self.violations]
            details["violations_truncated"] = self.truncated
        if self.line is not None:
            details["line"] = self.line
        if self.column is not None:
            details["column"] = self.column
        return details


PYTHON_EXECUTION_POLICY_GUIDANCE = (
    "Custom Python may run with or without a selected dataset. When supplied, "
    "use only the preloaded dataset for real-data analysis. Import only "
    "installed analysis libraries. Do not read, write, or discover files or "
    "paths; access the host environment or secrets; use the network; create "
    "or control processes; execute dynamically generated code; install "
    "packages; request interactive input; or change plotting backends. Print "
    "reviewable results or create a Matplotlib figure. If a call is "
    "policy-blocked, remove every reported violation in the single "
    "corrected-code attempt; never repeat blocked code unchanged."
)

DISALLOWED_IMPORT_RULES = {
    "pip": "PY_PACKAGE_INSTALLATION",
    "ensurepip": "PY_PACKAGE_INSTALLATION",
    "aiohttp": "PY_NETWORK_ACCESS",
    "ftplib": "PY_NETWORK_ACCESS",
    "http": "PY_NETWORK_ACCESS",
    "httpx": "PY_NETWORK_ACCESS",
    "requests": "PY_NETWORK_ACCESS",
    "smtplib": "PY_NETWORK_ACCESS",
    "socket": "PY_NETWORK_ACCESS",
    "urllib": "PY_NETWORK_ACCESS",
    "ctypes": "PY_PROCESS_CONTROL",
    "multiprocessing": "PY_PROCESS_CONTROL",
    "signal": "PY_PROCESS_CONTROL",
    "subprocess": "PY_PROCESS_CONTROL",
    "importlib": "PY_DYNAMIC_EXECUTION",
    "os": "PY_HOST_ENVIRONMENT_ACCESS",
    "pathlib": "PY_FILESYSTEM_ACCESS",
    "shutil": "PY_FILESYSTEM_ACCESS",
    "glob": "PY_FILESYSTEM_ACCESS",
    "tempfile": "PY_FILESYSTEM_ACCESS",
}

DISALLOWED_NAME_CALL_RULES = {
    "eval": "PY_DYNAMIC_EXECUTION",
    "exec": "PY_DYNAMIC_EXECUTION",
    "compile": "PY_DYNAMIC_EXECUTION",
    "__import__": "PY_DYNAMIC_EXECUTION",
    "open": "PY_FILESYSTEM_ACCESS",
    "input": "PY_INTERACTIVE_INPUT",
    "exit": "PY_PROCESS_CONTROL",
    "quit": "PY_PROCESS_CONTROL",
}

DISALLOWED_METHOD_RULES = {
    "read_bytes": "PY_FILESYSTEM_ACCESS",
    "read_text": "PY_FILESYSTEM_ACCESS",
    "write_bytes": "PY_FILESYSTEM_ACCESS",
    "write_text": "PY_FILESYSTEM_ACCESS",
    "mkdir": "PY_FILESYSTEM_ACCESS",
    "rmdir": "PY_FILESYSTEM_ACCESS",
    "unlink": "PY_FILESYSTEM_ACCESS",
    "touch": "PY_FILESYSTEM_ACCESS",
    "symlink_to": "PY_FILESYSTEM_ACCESS",
    "hardlink_to": "PY_FILESYSTEM_ACCESS",
    "savefig": "PY_FILESYSTEM_ACCESS",
    "to_csv": "PY_FILESYSTEM_ACCESS",
    "to_excel": "PY_FILESYSTEM_ACCESS",
    "to_feather": "PY_FILESYSTEM_ACCESS",
    "to_hdf": "PY_FILESYSTEM_ACCESS",
    "to_json": "PY_FILESYSTEM_ACCESS",
    "to_parquet": "PY_FILESYSTEM_ACCESS",
    "to_pickle": "PY_FILESYSTEM_ACCESS",
    "to_sql": "PY_FILESYSTEM_ACCESS",
}

PRELOADED_IMPORT_ALIASES = {
    "np": ("numpy",),
    "pd": ("pandas",),
    "plt": ("matplotlib", "pyplot"),
    "sm": ("statsmodels", "api"),
}

DISALLOWED_LIBRARY_READER_METHODS = {
    "pandas": {
        "ExcelFile",
        "HDFStore",
        "read_clipboard",
        "read_csv",
        "read_excel",
        "read_feather",
        "read_fwf",
        "read_gbq",
        "read_hdf",
        "read_html",
        "read_json",
        "read_orc",
        "read_parquet",
        "read_pickle",
        "read_sas",
        "read_spss",
        "read_sql",
        "read_sql_query",
        "read_sql_table",
        "read_stata",
        "read_table",
        "read_xml",
    },
    "numpy": {
        "DataSource",
        "fromfile",
        "genfromtxt",
        "load",
        "loadtxt",
        "memmap",
    },
    "matplotlib": {"imread"},
}

_PROTECTED_READER_ALIAS = "__protected_reader_library__"
_PROTECTED_READER_ROOTS = frozenset(DISALLOWED_LIBRARY_READER_METHODS)
_ALL_PROTECTED_READER_METHODS = frozenset(
    method
    for methods in DISALLOWED_LIBRARY_READER_METHODS.values()
    for method in methods
)

_RULE_TEXT = {
    "PY_DYNAMIC_EXECUTION": (
        "Dynamic code execution is prohibited.",
        "Use explicit Python expressions and calls already present in the submitted code.",
    ),
    "PY_PACKAGE_INSTALLATION": (
        "Generated code cannot install packages.",
        "Use an installed analysis library; package changes require a separate approved workflow.",
    ),
    "PY_FILESYSTEM_ACCESS": (
        "Generated code cannot access or modify files.",
        "Use in-memory values or the preloaded dataset, then print results or "
        "create an in-memory figure.",
    ),
    "PY_NETWORK_ACCESS": (
        "Generated code cannot access the network.",
        "Use evidence already supplied to the analysis runtime.",
    ),
    "PY_PROCESS_CONTROL": (
        "Generated code cannot create, control, or terminate processes.",
        "Perform the analysis directly in the current Python worker.",
    ),
    "PY_HOST_ENVIRONMENT_ACCESS": (
        "Generated code cannot inspect the host environment.",
        "Use in-memory values, an optional preloaded dataset, and explicitly "
        "imported analysis libraries.",
    ),
    "PY_RENDERER_OVERRIDE": (
        "Generated code cannot change the plotting backend.",
        "Use the runtime-configured Matplotlib backend.",
    ),
    "PY_INTERACTIVE_INPUT": (
        "Generated code cannot request interactive input.",
        "Use explicit in-memory values, the optional preloaded dataset, or "
        "tool arguments.",
    ),
}


def _attribute_name(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return tuple(reversed(parts))


def _remember_import_alias(
    aliases: dict[str, tuple[str, ...]],
    name: str,
    target: tuple[str, ...],
) -> None:
    existing = aliases.get(name, ())
    existing_root = existing[0] if existing else ""
    target_root = target[0] if target else ""
    if (
        existing_root == _PROTECTED_READER_ALIAS
        or existing_root in _PROTECTED_READER_ROOTS
    ):
        if (
            target_root in _PROTECTED_READER_ROOTS
            and target_root != existing_root
        ):
            aliases[name] = (_PROTECTED_READER_ALIAS,)
        return
    aliases[name] = target


def _collect_import_aliases(tree: ast.AST) -> dict[str, tuple[str, ...]]:
    aliases = dict(PRELOADED_IMPORT_ALIASES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                _remember_import_alias(
                    aliases,
                    alias.asname or root,
                    tuple(alias.name.split(".")),
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_parts = tuple(node.module.split("."))
            for alias in node.names:
                if alias.name == "*":
                    continue
                _remember_import_alias(
                    aliases,
                    alias.asname or alias.name,
                    module_parts + (alias.name,),
                )
    return aliases


def _canonical_name(
    name_parts: tuple[str, ...],
    import_aliases: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    if not name_parts:
        return ()
    return import_aliases.get(name_parts[0], (name_parts[0],)) + name_parts[1:]


def _safe_operation(value: str) -> str:
    return value[:160]


def _violation(
    rule_id: str,
    operation: str,
    node: ast.AST,
) -> PythonPolicyViolation:
    message, correction = _RULE_TEXT[rule_id]
    return PythonPolicyViolation(
        rule_id=rule_id,
        operation=_safe_operation(operation),
        message=message,
        correction=correction,
        line=max(1, int(getattr(node, "lineno", 1) or 1)),
        column=max(0, int(getattr(node, "col_offset", 0) or 0)),
    )


def _import_violations(node: ast.Import | ast.ImportFrom) -> list[PythonPolicyViolation]:
    violations: list[PythonPolicyViolation] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            rule_id = DISALLOWED_IMPORT_RULES.get(root)
            if rule_id is not None:
                violations.append(_violation(rule_id, f"import {alias.name}", node))
        return violations

    if node.module is None:
        return violations
    root = node.module.split(".", 1)[0]
    if (
        root in _PROTECTED_READER_ROOTS
        and any(alias.name == "*" for alias in node.names)
    ):
        violations.append(
            _violation("PY_FILESYSTEM_ACCESS", f"from {node.module} import *", node)
        )
    rule_id = DISALLOWED_IMPORT_RULES.get(root)
    if rule_id is None:
        return violations
    for alias in node.names:
        violations.append(
            _violation(
                rule_id,
                f"from {node.module} import {alias.name}",
                node,
            )
        )
    return violations


def _call_violation(
    node: ast.Call,
    import_aliases: dict[str, tuple[str, ...]],
) -> PythonPolicyViolation | None:
    name_parts = _attribute_name(node.func)
    canonical_name = _canonical_name(name_parts, import_aliases)
    operation_parts = (
        name_parts
        if canonical_name[:1] == (_PROTECTED_READER_ALIAS,)
        else canonical_name or name_parts
    )
    operation = ".".join(operation_parts) or "call"

    if isinstance(node.func, ast.Name):
        rule_id = DISALLOWED_NAME_CALL_RULES.get(node.func.id)
        if rule_id is not None:
            return _violation(rule_id, operation, node)

    if canonical_name in {
        ("sys", "exit"),
        ("builtins", "exit"),
        ("builtins", "quit"),
    }:
        return _violation("PY_PROCESS_CONTROL", operation, node)

    if canonical_name in {
        ("matplotlib", "use"),
        ("matplotlib", "pyplot", "switch_backend"),
    }:
        return _violation("PY_RENDERER_OVERRIDE", operation, node)

    if canonical_name:
        if canonical_name == (_PROTECTED_READER_ALIAS,):
            return _violation("PY_FILESYSTEM_ACCESS", operation, node)
        blocked_readers = (
            _ALL_PROTECTED_READER_METHODS
            if canonical_name[0] == _PROTECTED_READER_ALIAS
            else DISALLOWED_LIBRARY_READER_METHODS.get(
                canonical_name[0],
                set(),
            )
        )
        if canonical_name[-1] in blocked_readers:
            return _violation("PY_FILESYSTEM_ACCESS", operation, node)

    if isinstance(node.func, ast.Attribute):
        rule_id = DISALLOWED_METHOD_RULES.get(node.func.attr)
        if rule_id is not None:
            return _violation(rule_id, operation, node)

    return None


def validate_generated_code(code: str) -> PythonCodeValidation:
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return PythonCodeValidation(
            failure_code="INVALID_PYTHON_SYNTAX",
            message="Generated Python contains invalid syntax.",
            line=max(1, int(exc.lineno or 1)),
            column=max(0, int(exc.offset or 1) - 1),
        )

    import_aliases = _collect_import_aliases(tree)
    findings: list[PythonPolicyViolation] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            findings.extend(_import_violations(node))
        elif isinstance(node, ast.Call):
            violation = _call_violation(node, import_aliases)
            if violation is not None:
                findings.append(violation)

    distinct = {
        (item.rule_id, item.operation, item.line, item.column): item
        for item in findings
    }
    ordered = sorted(
        distinct.values(),
        key=lambda item: (item.line, item.column, item.rule_id, item.operation),
    )
    if not ordered:
        return PythonCodeValidation()

    return PythonCodeValidation(
        failure_code="POLICY_BLOCKED",
        message="Generated Python violates the execution policy.",
        violations=tuple(ordered[:POLICY_VIOLATION_LIMIT]),
        truncated=len(ordered) > POLICY_VIOLATION_LIMIT,
    )
