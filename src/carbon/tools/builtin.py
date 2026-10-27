"""Built-in controlled macOS tools satisfying PRD FR-006."""

import asyncio
import os
import platform
import shutil
from typing import Any

from carbon.interfaces.models import PermissionLevel, ToolDefinition, ToolExecutionResult
from carbon.tools.base import BaseTool


class SystemInfoTool(BaseTool):
    """Retrieves approved system status and resource metrics (Permission P1)."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            id="system_info",
            name="System Information",
            description="Retrieve hardware architecture, macOS version, unified memory size, and host metrics.",
            permission_level=PermissionLevel.P1,
            parameters_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            is_reversible=True,
            timeout_seconds=5.0,
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        try:
            # Memory size via sysctl
            proc = await asyncio.create_subprocess_exec(
                "sysctl",
                "-n",
                "hw.memsize",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            mem_bytes = int(stdout.decode().strip() or "0")
            mem_gb = round(mem_bytes / (1024**3), 2)

            info = {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.mac_ver()[0] or platform.version(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "unified_memory_gb": mem_gb,
                "python_version": platform.python_version(),
            }
            return ToolExecutionResult(tool_id="system_info", success=True, data=info)
        except Exception as e:
            return ToolExecutionResult(tool_id="system_info", success=False, error=str(e))


class ListApplicationsTool(BaseTool):
    """Lists approved applications installed on macOS (Permission P1)."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            id="list_applications",
            name="List Installed Applications",
            description="List installed macOS applications available in /Applications.",
            permission_level=PermissionLevel.P1,
            parameters_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of applications to list (default 25)",
                        "default": 25,
                    }
                },
                "additionalProperties": False,
            },
            is_reversible=True,
            timeout_seconds=5.0,
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        try:
            limit = int(arguments.get("limit", 25))
            app_dir = "/Applications"
            if not os.path.exists(app_dir):
                return ToolExecutionResult(tool_id="list_applications", success=True, data=[])

            apps = [
                f.replace(".app", "")
                for f in os.listdir(app_dir)
                if f.endswith(".app") and not f.startswith(".")
            ]
            apps.sort()
            return ToolExecutionResult(tool_id="list_applications", success=True, data=apps[:limit])
        except Exception as e:
            return ToolExecutionResult(tool_id="list_applications", success=False, error=str(e))


class OpenApplicationTool(BaseTool):
    """Opens an approved macOS application using native 'open -a' (Permission P2)."""

    # Whitelist of common approved applications that are safe to launch
    APPROVED_APPS = {
        "safari",
        "notes",
        "calendar",
        "calculator",
        "terminal",
        "iterm",
        "system settings",
        "messages",
        "reminders",
        "music",
        "photos",
        "textedit",
        "preview",
        "finder",
    }

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            id="open_application",
            name="Open macOS Application",
            description="Launch an approved macOS desktop application.",
            permission_level=PermissionLevel.P2,
            parameters_schema={
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Name of the application to launch (e.g. 'Safari', 'Notes', 'Calculator')",
                    }
                },
                "required": ["app_name"],
                "additionalProperties": False,
            },
            is_reversible=True,
            timeout_seconds=5.0,
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        app_name = arguments.get("app_name", "").strip()
        if not app_name:
            return ToolExecutionResult(
                tool_id="open_application", success=False, error="Parameter 'app_name' is required."
            )

        app_name_clean = app_name.lower().replace(".app", "")
        # Enforce whitelist policy
        if app_name_clean not in self.APPROVED_APPS:
            return ToolExecutionResult(
                tool_id="open_application",
                success=False,
                error=f"Application '{app_name}' is not in the approved application whitelist.",
            )

        open_bin = shutil.which("open")
        if not open_bin:
            return ToolExecutionResult(
                tool_id="open_application", success=False, error="'open' command not found."
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                open_bin,
                "-a",
                app_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                return ToolExecutionResult(
                    tool_id="open_application",
                    success=True,
                    data={"opened": app_name, "status": "launched"},
                )
            else:
                return ToolExecutionResult(
                    tool_id="open_application",
                    success=False,
                    error=stderr.decode().strip() or "Failed to open application.",
                )
        except Exception as e:
            return ToolExecutionResult(tool_id="open_application", success=False, error=str(e))
