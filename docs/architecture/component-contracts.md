# Carbon Component Contracts & Interfaces

This document formalizes the abstract interfaces, method signatures, and data contracts connecting Carbon's subsystems. All implementations must strictly conform to these contracts.

---

## 1. Core Models & Enumerations

```python
from enum import Enum, auto
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from datetime import datetime


class ReasoningDepth(str, Enum):
    INSTANT = "instant"  # Direct low-latency answers, no thinking phase
    STANDARD = "standard"  # Balanced reasoning, single-stage decomposition
    DEEP = "deep"  # Full chain-of-thought, multi-step validation


class PermissionLevel(str, Enum):
    P0 = "P0"  # Conversational / informational
    P1 = "P1"  # Read approved system state
    P2 = "P2"  # Reversible actions (app launch, window focus)
    P3 = "P3"  # Consequential modifications (file write, outbound network)
    P4 = "P4"  # Sensitive / destructive operations (delete, credential access)


class AutonomyLevel(str, Enum):
    A0 = "A0"  # Observe only
    A1 = "A1"  # Assist and recommend (user approves all actions)
    A2 = "A2"  # Auto-execute P0-P2; prompt for P3+
    A3 = "A3"  # Bounded autonomy within predefined guardrails


class TaskState(str, Enum):
    PENDING = "pending"
    ANALYZING = "analyzing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

---

## 2. Model Supervisor Contracts (`CMS`)

```python
class ModelCapability(str, Enum):
    COMPLETION = "completion"
    CHAT = "chat"
    TOOLS = "tools"
    THINKING = "thinking"
    VISION = "vision"
    EMBEDDING = "embedding"


class ModelMetadata(BaseModel):
    id: str  # e.g. "qwen3.5:9b"
    name: str  # Display name
    provider: str  # e.g. "ollama"
    family: str  # e.g. "qwen35"
    parameter_size: str  # e.g. "9.0B"
    quantization: str  # e.g. "Q4_K_M"
    context_length: int  # e.g. 262144
    memory_footprint_mb: int  # e.g. 6200
    capabilities: List[ModelCapability]
    is_active: bool = False
    is_healthy: bool = True


class ModelSwitchRequest(BaseModel):
    target_model_id: str
    reason: str
    force: bool = False


class IModelProvider(Protocol):
    """Protocol for low-level inference runtimes (Ollama, etc.)."""

    async def list_available_models(self) -> List[ModelMetadata]: ...
    async def is_healthy(self) -> bool: ...
    async def generate_stream(self, model_id: str, prompt: str, system: Optional[str] = None): ...
    async def chat_stream(
        self,
        model_id: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ): ...
    async def load_model(self, model_id: str) -> bool: ...
    async def unload_model(self, model_id: str) -> bool: ...


class ICarbonModelSupervisor(Protocol):
    """Sole authority controlling model selection, concurrency, and switching."""

    async def get_active_model(self) -> Optional[ModelMetadata]: ...
    async def list_registered_models(self) -> List[ModelMetadata]: ...
    async def select_model_for_task(
        self, capabilities_required: List[ModelCapability], reasoning: ReasoningDepth
    ) -> ModelMetadata: ...
    async def switch_model(self, request: ModelSwitchRequest) -> bool: ...
    async def acquire_inference_lease(self, model_id: str): ...
    async def release_inference_lease(self, model_id: str): ...
```

---

## 3. Intelligence Engine Contracts

```python
class TaskIntent(BaseModel):
    category: str
    confidence: float
    reasoning_depth: ReasoningDepth
    required_capabilities: List[ModelCapability]
    summary: str


class PlanStep(BaseModel):
    step_id: int
    tool_name: str
    arguments: Dict[str, Any]
    permission_level: PermissionLevel
    description: str


class ExecutionPlan(BaseModel):
    intent: TaskIntent
    steps: List[PlanStep]
    estimated_duration_seconds: float


class IIntelligenceEngine(Protocol):
    async def classify_intent(
        self, user_prompt: str, context: Optional[Dict[str, Any]] = None
    ) -> TaskIntent: ...
    async def create_plan(
        self, prompt: str, intent: TaskIntent, available_tools: List[str]
    ) -> ExecutionPlan: ...
    async def validate_output(
        self, raw_output: str, expected_schema: Dict[str, Any]
    ) -> Dict[str, Any]: ...
```

---

## 4. Persistent Memory Engine Contracts

```python
class MemoryType(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryRecord(BaseModel):
    id: str
    memory_type: MemoryType
    key: str
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    source: str
    confidence: float = 1.0
    created_at: datetime
    updated_at: datetime
    expires_at: Optional[datetime] = None


class IMemoryEngine(Protocol):
    async def initialize(self) -> None: ...
    async def store(self, record: MemoryRecord) -> str: ...
    async def retrieve(
        self, key: str, memory_type: Optional[MemoryType] = None
    ) -> Optional[MemoryRecord]: ...
    async def search(
        self, query: str, memory_type: Optional[MemoryType] = None, limit: int = 10
    ) -> List[MemoryRecord]: ...
    async def update(
        self, record_id: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> bool: ...
    async def forget(self, record_id: str) -> bool: ...
    async def forget_by_query(self, query: str) -> int: ...
```

---

## 5. Governance & Tool Contracts

```python
class ToolParameterSchema(BaseModel):
    name: str
    type: str
    description: str
    required: bool = True
    default: Optional[Any] = None


class ToolDefinition(BaseModel):
    id: str
    name: str
    description: str
    permission_level: PermissionLevel
    parameters_schema: Dict[str, Any]
    is_reversible: bool
    timeout_seconds: float = 10.0


class ToolExecutionResult(BaseModel):
    tool_id: str
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: float


class BaseTool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...
    async def execute(self, arguments: Dict[str, Any]) -> ToolExecutionResult: ...


class IGovernanceEngine(Protocol):
    async def authorize_action(
        self,
        tool_id: str,
        permission_level: PermissionLevel,
        arguments: Dict[str, Any],
        autonomy_level: AutonomyLevel,
    ) -> bool: ...
    async def record_audit_entry(self, event_type: str, details: Dict[str, Any]) -> None: ...
```
