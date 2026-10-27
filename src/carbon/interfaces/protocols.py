"""Carbon subsystem interface protocols."""

from collections.abc import AsyncGenerator
from typing import Any, Protocol, runtime_checkable

from carbon.interfaces.models import (
    AutonomyLevel,
    ChatMessage,
    MemoryRecord,
    MemoryType,
    ModelCapability,
    ModelMetadata,
    ModelSwitchRequest,
    PermissionLevel,
    ReasoningDepth,
    TaskIntent,
    ToolDefinition,
    ToolExecutionResult,
)


@runtime_checkable
class IModelProvider(Protocol):
    """Runtime provider interface for local or remote models (e.g. Ollama)."""

    async def list_available_models(self) -> list[ModelMetadata]: ...
    async def is_healthy(self) -> bool: ...
    async def generate_stream(
        self, model_id: str, prompt: str, system: str | None = None
    ) -> AsyncGenerator[str, None]: ...
    async def chat_stream(
        self,
        model_id: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]: ...
    async def load_model(self, model_id: str) -> bool: ...
    async def unload_model(self, model_id: str) -> bool: ...


@runtime_checkable
class ICarbonModelSupervisor(Protocol):
    """Sole authority for model registration, selection, switching, and concurrency control."""

    async def initialize(self) -> None: ...
    async def get_active_model(self) -> ModelMetadata | None: ...
    async def list_registered_models(self) -> list[ModelMetadata]: ...
    async def select_model_for_task(
        self,
        capabilities_required: list[ModelCapability],
        reasoning: ReasoningDepth,
    ) -> ModelMetadata: ...
    async def switch_model(self, request: ModelSwitchRequest) -> bool: ...
    async def acquire_inference_lease(self, model_id: str) -> Any: ...
    async def release_inference_lease(self, model_id: str) -> None: ...


@runtime_checkable
class IIntelligenceEngine(Protocol):
    """Understands user intent, evaluates task complexity, and structures tasks."""

    async def classify_intent(
        self, user_prompt: str, context: dict[str, Any] | None = None
    ) -> TaskIntent: ...
    async def validate_output(
        self, raw_output: str, expected_schema: dict[str, Any]
    ) -> dict[str, Any]: ...


@runtime_checkable
class IMemoryEngine(Protocol):
    """Four-tier persistent local memory engine."""

    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
    async def store(self, record: MemoryRecord) -> str: ...
    async def retrieve(
        self, key: str, memory_type: MemoryType | None = None
    ) -> MemoryRecord | None: ...
    async def search(
        self, query: str, memory_type: MemoryType | None = None, limit: int = 10
    ) -> list[MemoryRecord]: ...
    async def update(
        self, record_id: str, content: str, metadata: dict[str, Any] | None = None
    ) -> bool: ...
    async def forget(self, record_id: str) -> bool: ...
    async def forget_by_query(self, query: str) -> int: ...


@runtime_checkable
class ITool(Protocol):
    """Contract for registered system tools."""

    @property
    def definition(self) -> ToolDefinition: ...
    async def execute(self, arguments: dict[str, Any]) -> ToolExecutionResult: ...


@runtime_checkable
class IGovernanceEngine(Protocol):
    """Controls permissions, evaluates autonomy constraints, and maintains audit log."""

    async def initialize(self) -> None: ...
    async def authorize_action(
        self,
        tool_id: str,
        permission_level: PermissionLevel,
        arguments: dict[str, Any],
        autonomy_level: AutonomyLevel,
    ) -> bool: ...
    async def record_audit_entry(
        self, event_type: str, details: dict[str, Any], level: str = "INFO"
    ) -> None: ...


@runtime_checkable
class IDeviceEngine(Protocol):
    """Manages local host and future connected nodes."""

    async def get_host_status(self) -> dict[str, Any]: ...
