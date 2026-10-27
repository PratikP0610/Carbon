"""Carbon tool execution framework and registry."""

import asyncio
import logging
import time
from typing import Any

from carbon.interfaces.models import ToolDefinition, ToolExecutionResult
from carbon.interfaces.protocols import ITool

logger = logging.getLogger(__name__)


class BaseTool(ITool):
    """Base class for all Carbon tools."""

    @property
    def definition(self) -> ToolDefinition:
        raise NotImplementedError

    async def execute(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        raise NotImplementedError


class ToolRegistry:
    """Registry managing available tools and orchestrating safe execution."""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a new tool."""
        defn = tool.definition
        if defn.id in self._tools:
            logger.warning(f"Overwriting existing tool registration: {defn.id}")
        self._tools[defn.id] = tool
        logger.info(f"Registered tool '{defn.id}' [Permission: {defn.permission_level.value}]")

    def get_tool(self, tool_id: str) -> BaseTool | None:
        """Lookup tool by ID."""
        return self._tools.get(tool_id)

    def list_definitions(self) -> list[ToolDefinition]:
        """Return definitions of all registered tools."""
        return [tool.definition for tool in self._tools.values()]

    def get_schemas_for_llm(self) -> list[dict[str, Any]]:
        """Format registered tools for LLM tool call APIs."""
        schemas = []
        for tool in self._tools.values():
            defn = tool.definition
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": defn.id,
                        "description": defn.description,
                        "parameters": defn.parameters_schema,
                    },
                }
            )
        return schemas

    async def execute_tool(self, tool_id: str, arguments: dict[str, Any]) -> ToolExecutionResult:
        """Execute a tool with timeout isolation and error recovery."""
        tool = self.get_tool(tool_id)
        if not tool:
            return ToolExecutionResult(
                tool_id=tool_id,
                success=False,
                error=f"Tool '{tool_id}' is not registered in Carbon.",
                duration_ms=0.0,
            )

        start_time = time.perf_counter()
        timeout = tool.definition.timeout_seconds

        try:
            result = await asyncio.wait_for(tool.execute(arguments), timeout=timeout)
            duration_ms = (time.perf_counter() - start_time) * 1000
            result.duration_ms = duration_ms
            return result
        except TimeoutError:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(f"Tool '{tool_id}' timed out after {timeout}s.")
            return ToolExecutionResult(
                tool_id=tool_id,
                success=False,
                error=f"Execution timed out after {timeout} seconds.",
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(f"Tool '{tool_id}' failed with exception: {e}")
            return ToolExecutionResult(
                tool_id=tool_id,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )
