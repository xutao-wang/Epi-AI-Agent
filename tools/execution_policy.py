from __future__ import annotations

import ast


ERROR_CATEGORY_POLICY_BLOCKED = "policy_blocked"

DISALLOWED_IMPORT_ROOTS = {
    "aiohttp",
    "ctypes",
    "httpx",
    "importlib",
    "multiprocessing",
    "pip",
    "requests",
    "socket",
    "subprocess",
    "urllib",
}

DISALLOWED_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "open",
    "input",
}

DISALLOWED_ATTR_CALLS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "spawnl"),
    ("os", "spawnle"),
    ("os", "spawnlp"),
    ("os", "spawnlpe"),
    ("os", "spawnv"),
    ("os", "spawnve"),
    ("os", "spawnvp"),
    ("os", "spawnvpe"),
    ("socket", "socket"),
    ("subprocess", "Popen"),
    ("subprocess", "call"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
    ("subprocess", "run"),
}

DISALLOWED_FS_FUNCTION_CALLS = {
    ("os", "remove"),
    ("os", "unlink"),
    ("os", "rename"),
    ("os", "replace"),
    ("os", "mkdir"),
    ("os", "makedirs"),
    ("os", "rmdir"),
    ("os", "removedirs"),
    ("shutil", "rmtree"),
    ("shutil", "move"),
    ("shutil", "copy"),
    ("shutil", "copy2"),
    ("shutil", "copyfile"),
    ("shutil", "copytree"),
}

DISALLOWED_FS_METHOD_CALLS = {
    "mkdir",
    "rmdir",
    "rename",
    "replace",
    "unlink",
    "write_bytes",
    "write_text",
    "touch",
    "symlink_to",
    "hardlink_to",
    "savefig",
    "to_csv",
    "to_excel",
    "to_feather",
    "to_hdf",
    "to_json",
    "to_parquet",
    "to_pickle",
    "to_sql",
}


def _policy_error(message: str) -> dict[str, str]:
    return {
        "type": "PolicyBlockedError",
        "message": message,
        "category": ERROR_CATEGORY_POLICY_BLOCKED,
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


def _format_fs_call_name(
    node: ast.Call,
    name_parts: tuple[str, ...],
    fallback_attr: str | None,
) -> str:
    if isinstance(node.func, ast.Attribute):
        value = node.func.value
        if isinstance(value, ast.Call):
            callee = _attribute_name(value.func)
            if callee and callee[-1] == "Path":
                return f"Path.{node.func.attr}"
        if isinstance(value, ast.Name) and value.id == "Path":
            return f"Path.{node.func.attr}"
    if name_parts:
        if len(name_parts) >= 2 and name_parts[0] in {"os", "shutil", "Path"}:
            return ".".join(name_parts[:2])
        if len(name_parts) >= 2:
            return ".".join(name_parts)
        if len(name_parts) == 1:
            return name_parts[0]
    return fallback_attr or "filesystem call"


def _is_explicit_path_method_call(func: ast.Attribute) -> bool:
    value = func.value
    if isinstance(value, ast.Call):
        callee = _attribute_name(value.func)
        return bool(callee and callee[-1] == "Path")
    return isinstance(value, ast.Name) and value.id == "Path"


def _is_disallowed_fs_method_call(node: ast.Call) -> bool:
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in DISALLOWED_FS_METHOD_CALLS:
        return False
    if node.func.attr == "replace":
        return _is_explicit_path_method_call(node.func)
    return True


def _collect_import_aliases(tree: ast.AST) -> dict[str, tuple[str, ...]]:
    aliases: dict[str, tuple[str, ...]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                aliases[alias.asname or root] = tuple(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_parts = tuple(node.module.split("."))
            for alias in node.names:
                aliases[alias.asname or alias.name] = module_parts + (alias.name,)
    return aliases


def _canonical_call_name(
    name_parts: tuple[str, ...],
    import_aliases: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    if not name_parts:
        return ()
    return import_aliases.get(name_parts[0], (name_parts[0],)) + name_parts[1:]


def validate_generated_code(code: str) -> dict[str, str] | None:
    try:
        tree = ast.parse((code or "").strip())
    except SyntaxError as exc:
        return _policy_error(f"Code failed policy parsing: {exc.msg}")

    import_aliases = _collect_import_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in DISALLOWED_IMPORT_ROOTS:
                    return _policy_error(f"Disallowed import: {root}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if root in DISALLOWED_IMPORT_ROOTS:
                return _policy_error(f"Disallowed import: {root}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in DISALLOWED_CALLS:
                return _policy_error(f"Disallowed call: {node.func.id}")

            if isinstance(node.func, ast.Name):
                imported_name = import_aliases.get(node.func.id)
                if imported_name in DISALLOWED_FS_FUNCTION_CALLS:
                    return _policy_error(
                        f"Disallowed filesystem mutation: {'.'.join(imported_name[:2])}"
                    )

            name_parts = _attribute_name(node.func)
            canonical_name = _canonical_call_name(name_parts, import_aliases)
            if canonical_name[:2] == ("matplotlib", "use"):
                return _policy_error(
                    "Disallowed Matplotlib backend selection: matplotlib.use"
                )
            if len(canonical_name) >= 2 and canonical_name[:2] in DISALLOWED_ATTR_CALLS:
                return _policy_error(f"Disallowed call: {'.'.join(canonical_name[:2])}")
            if (
                len(canonical_name) >= 2
                and canonical_name[:2] in DISALLOWED_FS_FUNCTION_CALLS
            ):
                return _policy_error(
                    f"Disallowed filesystem mutation: {'.'.join(canonical_name[:2])}"
                )
            if _is_disallowed_fs_method_call(node):
                return _policy_error(
                    "Disallowed filesystem mutation: "
                    f"{_format_fs_call_name(node, name_parts, getattr(node.func, 'attr', None))}"
                )
    return None
