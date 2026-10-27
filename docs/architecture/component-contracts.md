# Carbon Component Contracts & Subsystem Interfaces

This document formalizes the abstract interfaces, method signatures, data models, and specialist model contracts connecting Carbon's subsystems.

---

## 1. Core Enumerations & Data Contracts

```python
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from pydantic import BaseModel, Field

class ReasoningDepth(StrEnum):
    INSTANT = "instant"      # Direct conversational/factual tasks (< 500ms TTFT target)
    STANDARD = "standard"    # Balanced explanations, comparisons, ordinary planning
    DEEP = "deep"            # Complex analysis, multi-step problem solving, explicit thinking

class PermissionLevel(StrEnum):
    P0 = "P0"  # Conversational / informational (read-only, no side effects)
    P1 = "P1"  # Read approved system state (metrics, window titles, hardware status)
    P2 = "P2"  # Reversible actions (app launching with whitelist, clipboard)
    P3 = "P3"  # Consequential modifications (file write, calendar, network) - User Approval
    P4 = "P4"  # Sensitive / destructive operations (file deletion, credentials) - Double Confirmation

class AutonomyLevel(StrEnum):
    A0 = "A0"  # Observe only
    A1 = "A1"  # Assist and recommend (default)
    A2 = "A2"  # Execute approved actions (auto P0-P2)
    A3 = "A3"  # Bounded autonomy within explicitly defined policy envelopes

class TaskState(StrEnum):
    PENDING = "pending"
    ANALYZING = "analyzing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class ModelWorkloadType(StrEnum):
    CHAT_LLM = "chat_llm"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_TO_SPEECH = "text_to_speech"
    EMBEDDING = "embedding"
    VISION = "vision"
```

---

## 2. Model Control & Specialist Workload Contracts

Specialist workloads (STT, TTS, Embeddings) are explicitly decoupled from the primary Chat LLM.

```python
class ModelMetadata(BaseModel):
    id: str
    name: str
    workload_type: ModelWorkloadType
    provider: str
    family: str
    parameter_size: str
    quantization: str
    context_length: int
    memory_footprint_mb: int
    is_active: bool = False
    is_healthy: bool = True

@runtime_checkable
class IChatModelProvider(Protocol):
    """Contract for Chat LLMs governed by Single Active Chat LLM Concurrency."""
    async def is_healthy(self) -> bool: ...
    async def generate_stream_events(self, model_id: str, prompt: str, system: str | None = None): ...
    async def chat_stream(self, model_id: str, messages: list[Any], tools: list[dict[str, Any]] | None = None): ...
    async def load_model(self, model_id: str) -> bool: ...
    async def unload_model(self, model_id: str) -> bool: ...

@runtime_checkable
class ISpeechToTextEngine(Protocol):
    """Contract for local audio transcription (e.g. Whisper.cpp via CoreML/ANE)."""
    @property
    def max_memory_budget_mb(self) -> int: return 500
    async def transcribe_chunk(self, pcm_bytes: bytes, sample_rate: int = 16000) -> str: ...
    async def reset(self) -> None: ...

@runtime_checkable
class ITextToSpeechEngine(Protocol):
    """Contract for local speech synthesis (e.g. Kokoro ONNX/native)."""
    @property
    def max_memory_budget_mb(self) -> int: return 500
    async def synthesize_stream(self, text: str) -> AsyncGenerator[bytes, None]: ...
    async def interrupt(self) -> None: ...

@runtime_checkable
class IEmbeddingEngine(Protocol):
    """Contract for local dense vector embeddings."""
    @property
    def max_memory_budget_mb(self) -> int: return 400
    async def embed_text(self, text: str) -> list[float]: ...
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

@runtime_checkable
class IVisionEngine(Protocol):
    """Contract for visual processing / screen inspection."""
    async def analyze_frame(self, image_bytes: bytes, prompt: str) -> str: ...
```

---

## 3. Carbon Model Supervisor (CMS) Contract

```python
class ModelSwitchRequest(BaseModel):
    target_model_id: str
    reason: str
    force: bool = False

@runtime_checkable
class ICarbonModelSupervisor(Protocol):
    """Sole authority for model registration, selection, switching, and concurrency control."""
    async def initialize(self) -> None: ...
    async def get_active_model(self) -> ModelMetadata | None: ...
    async def list_registered_models(self) -> list[ModelMetadata]: ...
    async def select_model_for_task(self, capabilities: list[str], reasoning: ReasoningDepth) -> ModelMetadata: ...
    async def switch_model(self, request: ModelSwitchRequest) -> bool: ...
    async def acquire_inference_lease(self, model_id: str) -> None: ...
    async def release_inference_lease(self, model_id: str) -> None: ...
```

---

## 4. Intelligence & Governance Contracts

```python
class TaskIntent(BaseModel):
    category: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning_depth: ReasoningDepth
    required_capabilities: list[str]
    summary: str

@runtime_checkable
class IIntelligenceEngine(Protocol):
    async def classify_intent(self, user_prompt: str, context: dict[str, Any] | None = None) -> TaskIntent: ...
    async def validate_output(self, raw_output: str, expected_schema: dict[str, Any]) -> dict[str, Any]: ...

@runtime_checkable
class IGovernanceEngine(Protocol):
    async def initialize(self) -> None: ...
    async def authorize_action(self, tool_id: str, permission: PermissionLevel, args: dict[str, Any], autonomy: AutonomyLevel) -> bool: ...
    async def record_audit_entry(self, event_type: str, details: dict[str, Any], level: str = "INFO") -> None: ...
```

---

## 5. Tool & Memory Contracts

```python
class ToolDefinition(BaseModel):
    id: str
    name: str
    description: str
    permission_level: PermissionLevel
    parameters_schema: dict[str, Any]
    is_reversible: bool
    timeout_seconds: float = 10.0

class ToolExecutionResult(BaseModel):
    tool_id: str
    success: bool
    data: Any | None = None
    error: str | None = None
    duration_ms: float = 0.0

@runtime_checkable
class ITool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...
    async def execute(self, arguments: dict[str, Any]) -> ToolExecutionResult: ...

@runtime_checkable
class IMemoryEngine(Protocol):
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
    async def store(self, record: Any) -> str: ...
    async def retrieve(self, key: str, memory_type: str | None = None) -> Any | None: ...
    async def search(self, query: str, memory_type: str | None = None, limit: int = 10) -> list[Any]: ...
    async def update(self, record_id: str, content: str, metadata: dict[str, Any] | None = None) -> bool: ...
    async def forget(self, record_id: str) -> bool: ...
    async def forget_by_query(self, query: str) -> int: ...
```
