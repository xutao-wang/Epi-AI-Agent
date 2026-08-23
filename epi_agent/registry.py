from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from epi_agent.protocol import (
    AgentTool,
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
)


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
            raise ToolExecutionError(
                "INVALID_ARGUMENTS",
                f"Invalid arguments for tool {name}: {error}",
                recoverable=True,
            ) from error

        return tool.invoke(validated.model_dump(), context)

    def spec(self, name: str) -> ToolSpec:
        return self._require_tool(name).spec

    def model_schemas(self) -> list[dict[str, Any]]:
        return [tool.spec.model_schema() for tool in self._tools.values()]

    def _require_tool(self, name: str) -> AgentTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolExecutionError(
                "UNKNOWN_TOOL",
                f"Unknown tool: {name}",
                recoverable=True,
            )
        return tool
