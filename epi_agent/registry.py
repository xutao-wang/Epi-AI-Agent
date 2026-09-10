from __future__ import annotations

from collections.abc import Iterable
from typing import Any, get_args

from pydantic import ValidationError
from pydantic_core import ErrorType

from epi_agent.protocol import (
    AgentTool,
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
)


_PYDANTIC_ERROR_TYPES = frozenset(get_args(ErrorType))
_SAFE_FIELD_SCHEMA_KEYS = frozenset(
    {
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "multipleOf",
        "type",
    }
)


def _resolve_schema_node(
    root: dict[str, Any], node: Any
) -> dict[str, Any]:
    while isinstance(node, dict):
        reference = node.get("$ref")
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            return node
        node = dict(root.get("$defs") or {}).get(reference.removeprefix("#/$defs/"))
    return {}


def _safe_error_location(
    schema: dict[str, Any], location: list[Any]
) -> tuple[list[str | int], dict[str, Any]]:
    node: Any = schema
    safe_location: list[str | int] = []
    for part in location:
        node = _resolve_schema_node(schema, node)
        properties = node.get("properties")
        if (
            isinstance(part, str)
            and isinstance(properties, dict)
            and part in properties
        ):
            safe_location.append(part)
            node = properties[part]
            continue
        items = node.get("items")
        if isinstance(part, int) and isinstance(items, dict):
            safe_location.append(part)
            node = items
            continue
        safe_location.append("<arguments>")
        additional = node.get("additionalProperties")
        node = additional if isinstance(additional, dict) else {}
    return safe_location or ["<arguments>"], _resolve_schema_node(schema, node)


def _safe_expected_schema(
    root: dict[str, Any], field_schema: dict[str, Any]
) -> dict[str, Any]:
    node = _resolve_schema_node(root, field_schema)
    expected = {
        key: value
        for key, value in node.items()
        if key in _SAFE_FIELD_SCHEMA_KEYS
    }

    properties = node.get("properties")
    if isinstance(properties, dict):
        declared_properties = {}
        for name, property_schema in properties.items():
            if not isinstance(name, str) or not isinstance(property_schema, dict):
                continue
            resolved = _resolve_schema_node(root, property_schema)
            property_expected = {
                key: value
                for key, value in resolved.items()
                if key in _SAFE_FIELD_SCHEMA_KEYS
            }
            if property_expected:
                declared_properties[name] = property_expected
        if declared_properties:
            expected["properties"] = declared_properties

        required = node.get("required")
        if isinstance(required, list):
            declared_required = [
                name
                for name in required
                if isinstance(name, str) and name in properties
            ]
            if declared_required:
                expected["required"] = declared_required

    if isinstance(node.get("additionalProperties"), bool):
        expected["additionalProperties"] = node["additionalProperties"]

    return expected


class ToolRegistry:
    def __init__(self, tools: Iterable[AgentTool] = ()) -> None:
        self._tools: dict[str, AgentTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AgentTool) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def tools(self) -> tuple[AgentTool, ...]:
        return tuple(self._tools.values())

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: ToolContext,
    ) -> ToolResult:
        tool = self._require_tool(name)

        try:
            validated = tool.spec.args_model.model_validate(arguments)
        except ValidationError as error:
            schema = tool.spec.args_model.model_json_schema()
            issues = []
            for item in error.errors(
                include_url=True,
                include_context=True,
                include_input=False,
            )[:50]:
                location = list(item.get("loc") or [])
                safe_location, field_schema = _safe_error_location(
                    schema, location
                )
                error_type = str(item.get("type") or "")
                error_url = str(item.get("url") or "")
                safe_type = (
                    error_type
                    if error_type in _PYDANTIC_ERROR_TYPES
                    and error_url.startswith("https://errors.pydantic.dev/")
                    else "validation_error"
                )
                issue: dict[str, Any] = {
                    "loc": safe_location,
                    "msg": "Input does not match the tool schema.",
                    "type": safe_type,
                }
                if safe_type != "validation_error" and isinstance(
                    field_schema, dict
                ):
                    expected = _safe_expected_schema(schema, field_schema)
                    if expected:
                        issue["expected"] = expected
                issues.append(issue)
            raise ToolExecutionError(
                "INVALID_ARGUMENTS",
                f"Invalid arguments for tool {name}.",
                recoverable=True,
                details={"issues": issues},
            ) from error

        return tool.invoke(validated.model_dump(), context)

    def spec(self, name: str) -> ToolSpec:
        return self._require_tool(name).spec

    def model_schemas(
        self,
        *,
        inline_local_references: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            tool.spec.model_schema(
                inline_local_references=inline_local_references,
            )
            for tool in self._tools.values()
        ]

    def _require_tool(self, name: str) -> AgentTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolExecutionError(
                "UNKNOWN_TOOL",
                f"Unknown tool: {name}",
                recoverable=True,
            )
        return tool
