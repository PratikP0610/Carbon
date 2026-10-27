"""Carbon Core Orchestrator implementation."""

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from carbon.config.settings import Settings, get_settings
from carbon.devices.manager import DeviceManager
from carbon.governance.governance_engine import GovernanceEngine
from carbon.intelligence.engine import IntelligenceEngine
from carbon.interfaces.models import (
    AutonomyLevel,
    MemoryRecord,
    MemoryType,
    ReasoningDepth,
    TaskIntent,
    TaskRequest,
    TaskState,
)
from carbon.memory.sqlite_memory import SQLiteMemoryEngine
from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.ollama_provider import OllamaProvider
from carbon.tools.base import ToolRegistry
from carbon.tools.builtin import (
    ListApplicationsTool,
    OpenApplicationTool,
    SystemInfoTool,
)

logger = logging.getLogger(__name__)


class TaskRecord:
    """In-memory tracking record for a Carbon task."""

    def __init__(self, task_id: str, prompt: str, conversation_id: str):
        self.task_id = task_id
        self.prompt = prompt
        self.conversation_id = conversation_id
        self.state: TaskState = TaskState.PENDING
        self.intent: TaskIntent | None = None
        self.reasoning_depth: ReasoningDepth = ReasoningDepth.STANDARD
        self.model_used: str | None = None
        self.result: str | None = None
        self.error: str | None = None
        self.created_at: datetime = datetime.now(UTC)
        self.completed_at: datetime | None = None
        self.cancel_event: asyncio.Event = asyncio.Event()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "prompt": self.prompt,
            "conversation_id": self.conversation_id,
            "state": self.state.value,
            "intent": self.intent.model_dump() if self.intent else None,
            "reasoning_depth": self.reasoning_depth.value,
            "model_used": self.model_used,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class CarbonCore:
    """Central orchestrator for the Carbon modular monolith."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.is_running: bool = False

        # Subsystems
        self.provider = OllamaProvider(
            base_url=self.settings.ollama_base_url,
            timeout_seconds=self.settings.inference_timeout_seconds,
        )
        self.cms = CarbonModelSupervisor(
            provider=self.provider,
            default_model_id=self.settings.primary_model,
        )
        self.memory = SQLiteMemoryEngine(db_path=self.settings.database_path)
        self.governance = GovernanceEngine(
            audit_log_path=self.settings.audit_log_path,
            enable_audit=self.settings.enable_audit_log,
        )
        self.intelligence = IntelligenceEngine()
        self.tools = ToolRegistry()
        self.devices = DeviceManager()

        # Task Management
        self._tasks: dict[str, TaskRecord] = {}

    async def initialize(self) -> None:
        """Start up and coordinate all Carbon subsystems."""
        logger.info("Starting Carbon Core Orchestrator...")
        self.settings.ensure_directories()

        # 1. Initialize persistence & auditing
        await self.governance.initialize()
        await self.memory.initialize()

        # 2. Register approved system tools
        self.tools.register(SystemInfoTool())
        self.tools.register(ListApplicationsTool())
        self.tools.register(OpenApplicationTool())

        # 3. Initialize Model Supervisor (CMS)
        await self.cms.initialize()

        self.is_running = True
        await self.governance.record_audit_entry(
            "CARBON_CORE_INITIALIZED",
            {"status": "running", "primary_model": self.settings.primary_model},
        )
        logger.info("Carbon Core Orchestrator is fully initialized and operational.")

    async def shutdown(self) -> None:
        """Cleanly shut down all subsystems and release resources."""
        if not self.is_running:
            return
        logger.info("Shutting down Carbon Core...")
        self.is_running = False

        # Cancel any active running tasks
        for task in self._tasks.values():
            if task.state in (
                TaskState.PENDING,
                TaskState.ANALYZING,
                TaskState.EXECUTING,
                TaskState.STREAMING,
            ):
                task.cancel_event.set()
                task.state = TaskState.CANCELLED
                task.completed_at = datetime.now(UTC)

        # Release memory connection
        await self.memory.close()

        await self.governance.record_audit_entry("CARBON_CORE_SHUTDOWN", {"status": "stopped"})
        logger.info("Carbon Core shutdown completed cleanly.")

    async def get_system_health(self) -> dict[str, Any]:
        """Aggregate health telemetry from all subsystems."""
        ollama_healthy = await self.provider.is_healthy()
        active_model = await self.cms.get_active_model()
        host_status = await self.devices.get_host_status()

        return {
            "status": "healthy" if self.is_running and ollama_healthy else "degraded",
            "core_running": self.is_running,
            "ollama_connected": ollama_healthy,
            "active_model": active_model.model_dump() if active_model else None,
            "autonomy_level": self.settings.autonomy_level,
            "host": host_status,
            "tasks_count": len(self._tasks),
        }

    async def create_task(self, request: TaskRequest) -> TaskRecord:
        """Create and register a new task."""
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        conversation_id = request.conversation_id or f"conv-{uuid.uuid4().hex[:8]}"
        task = TaskRecord(task_id=task_id, prompt=request.prompt, conversation_id=conversation_id)

        if request.reasoning_override:
            task.reasoning_depth = request.reasoning_override

        self._tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> TaskRecord | None:
        """Lookup task record by ID."""
        return self._tasks.get(task_id)

    def cancel_task(self, task_id: str) -> bool:
        """Cancel an ongoing task."""
        task = self.get_task(task_id)
        if not task:
            return False
        if task.state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            return False
        task.cancel_event.set()
        task.state = TaskState.CANCELLED
        task.completed_at = datetime.now(UTC)
        logger.info(f"Task '{task_id}' was cancelled by user.")
        return True

    async def execute_task_stream(self, task: TaskRecord) -> AsyncGenerator[dict[str, Any], None]:
        """Execute a task and yield streaming state events and tokens."""
        task.state = TaskState.ANALYZING
        yield {"event": "state_change", "state": task.state.value}

        try:
            # 1. Intent Classification & Reasoning Depth
            intent = await self.intelligence.classify_intent(task.prompt)
            task.intent = intent
            if not task.reasoning_depth or task.reasoning_depth == ReasoningDepth.STANDARD:
                task.reasoning_depth = intent.reasoning_depth

            yield {
                "event": "intent_classified",
                "category": intent.category,
                "reasoning_depth": task.reasoning_depth.value,
            }

            # 2. Check for simple tool commands on fast-path (e.g. open application)
            if intent.category == "SYSTEM_COMMAND" and task.prompt.lower().startswith("open "):
                app_name = task.prompt[5:].strip()
                yield {
                    "event": "tool_call_started",
                    "tool": "open_application",
                    "args": {"app_name": app_name},
                }

                # Evaluate governance authorization
                autonomy = AutonomyLevel(self.settings.autonomy_level)
                tool = self.tools.get_tool("open_application")
                if not tool:
                    raise RuntimeError("Tool 'open_application' not found.")

                authorized = await self.governance.authorize_action(
                    tool_id="open_application",
                    permission_level=tool.definition.permission_level,
                    arguments={"app_name": app_name},
                    autonomy_level=autonomy,
                )

                if not authorized:
                    task.state = TaskState.AWAITING_APPROVAL
                    yield {
                        "event": "awaiting_approval",
                        "tool_id": "open_application",
                        "permission_level": tool.definition.permission_level.value,
                        "args": {"app_name": app_name},
                    }
                    task.result = f"Action 'open_application' requires user authorization ({tool.definition.permission_level.value})."
                    task.state = TaskState.COMPLETED
                    task.completed_at = datetime.now(UTC)
                    yield {"event": "token", "text": task.result}
                    yield {"event": "completed", "task": task.to_dict()}
                    return

                # Execute tool
                tool_res = await self.tools.execute_tool("open_application", {"app_name": app_name})
                if tool_res.success:
                    task.result = f"Successfully launched {app_name}."
                else:
                    task.result = f"Could not launch {app_name}: {tool_res.error}"

                task.state = TaskState.COMPLETED
                task.completed_at = datetime.now(UTC)
                yield {"event": "token", "text": task.result}
                yield {"event": "completed", "task": task.to_dict()}
                return

            # 3. Model Selection via CMS
            model = await self.cms.select_model_for_task(
                capabilities_required=intent.required_capabilities,
                reasoning=task.reasoning_depth,
            )
            task.model_used = model.id

            # 4. Acquire single-model concurrency lease (ADR-0003)
            yield {"event": "acquiring_model_lease", "model": model.id}
            await self.cms.acquire_inference_lease(task.task_id)
            try:
                task.state = TaskState.STREAMING
                yield {"event": "state_change", "state": task.state.value}

                # Construct system prompt with reasoning directives
                system_prompt = (
                    "You are Carbon, an intelligent personal AI computing assistant on macOS.\n"
                    "Provide clear, accurate, and direct responses.\n"
                )
                if task.reasoning_depth == ReasoningDepth.INSTANT:
                    system_prompt += "Provide direct, concise answers without verbose reasoning.\n"

                accumulated_tokens: list[str] = []
                async for chunk in self.provider.generate_stream_events(
                    model_id=model.id,
                    prompt=task.prompt,
                    system=system_prompt,
                ):
                    if task.cancel_event.is_set():
                        logger.info(f"Task '{task.task_id}' was cancelled mid-stream.")
                        task.state = TaskState.CANCELLED
                        yield {"event": "cancelled"}
                        return

                    thinking_part = chunk.get("thinking", "")
                    if thinking_part:
                        yield {"event": "thinking", "text": thinking_part}

                    resp_part = chunk.get("response", "")
                    if resp_part:
                        accumulated_tokens.append(resp_part)
                        yield {"event": "token", "text": resp_part}

                full_response = "".join(accumulated_tokens)
                task.result = full_response
                task.state = TaskState.COMPLETED
                task.completed_at = datetime.now(UTC)

                # 5. Persist interaction into episodic memory
                memory_entry = MemoryRecord(
                    id=str(uuid.uuid4()),
                    memory_type=MemoryType.EPISODIC,
                    key=f"task:{task.task_id}",
                    content=f"User: {task.prompt}\nCarbon: {task.result[:200]}",
                    metadata={"conversation_id": task.conversation_id, "model": model.id},
                    source="user_interaction",
                )
                await self.memory.store(memory_entry)

                yield {"event": "completed", "task": task.to_dict()}
            finally:
                await self.cms.release_inference_lease(task.task_id)

        except Exception as e:
            logger.error(f"Error during task '{task.task_id}' execution: {e}", exc_info=True)
            task.state = TaskState.FAILED
            task.error = str(e)
            task.completed_at = datetime.now(UTC)
            yield {"event": "failed", "error": str(e)}
