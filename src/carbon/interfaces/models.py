"""Carbon core data models, types, and enumerations."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ReasoningDepth(StrEnum):
    """Reasoning depth tiers as defined in PRD FR-002."""

    INSTANT = "instant"  # Direct conversational/factual tasks, zero-thinking
    STANDARD = "standard"  # Balanced explanations, comparisons, ordinary planning
    DEEP = "deep"  # Complex analysis, multi-step problem solving, explicit thinking


class PermissionLevel(StrEnum):
    """Permission levels as defined in PRD FR-007."""

    P0 = "P0"  # Basic conversational operations (read-only, no side effects)
    P1 = "P1"  # Read approved information (system state, metrics, window titles)
    P2 = "P2"  # Reversible actions (app launch, window focus, clipboard)
    P3 = "P3"  # Important modifications / external submissions (explicit confirmation required)
    P4 = "P4"  # Sensitive, destructive, or high-impact (strong confirmation or prohibited)


class AutonomyLevel(StrEnum):
    """Autonomy levels as defined in PRD FR-007."""

    A0 = "A0"  # Observe only
    A1 = "A1"  # Assist and recommend (default)
    A2 = "A2"  # Execute approved actions (auto P0-P2)
    A3 = "A3"  # Bounded autonomy within explicitly defined policy boundaries


class TaskState(StrEnum):
    """Discrete lifecycle states for a Carbon task."""

    PENDING = "pending"
    ANALYZING = "analyzing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ModelCapability(StrEnum):
    """Capabilities supported by local or remote models."""

    COMPLETION = "completion"
    CHAT = "chat"
    TOOLS = "tools"
    THINKING = "thinking"
    VISION = "vision"
    EMBEDDING = "embedding"


class ModelMetadata(BaseModel):
    """Metadata describing a registered AI model in the Carbon Model Supervisor."""

    id: str = Field(description="Unique model identifier, e.g. 'qwen3.5:9b'")
    name: str = Field(description="Human-readable model name")
    provider: str = Field(default="ollama", description="Runtime provider name")
    family: str = Field(default="qwen35", description="Model architecture family")
    parameter_size: str = Field(default="9.0B", description="Model parameter size")
    quantization: str = Field(default="Q4_K_M", description="Quantization level")
    context_length: int = Field(default=4096, description="Active runtime context window in tokens")
    max_architectural_context: int = Field(
        default=262144, description="Model theoretical architectural context limit in tokens"
    )
    memory_footprint_mb: int = Field(default=6200, description="Estimated memory footprint in MB")
    capabilities: list[ModelCapability] = Field(default_factory=list)
    is_active: bool = Field(
        default=False, description="Whether model is currently loaded in memory"
    )
    is_healthy: bool = Field(default=True, description="Health check status")


class ModelSwitchRequest(BaseModel):
    """Request to transition active model to a target model."""

    target_model_id: str
    reason: str
    force: bool = False


class TaskIntent(BaseModel):
    """Structured classification of user intent by the Intelligence Engine."""

    category: str = Field(description="Intent category e.g. CONVERSATIONAL, SYSTEM_COMMAND")
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning_depth: ReasoningDepth = Field(default=ReasoningDepth.STANDARD)
    required_capabilities: list[ModelCapability] = Field(default_factory=list)
    summary: str = Field(description="One-line summary of intent")


class MemoryType(StrEnum):
    """Four memory categories defined in PRD FR-004."""

    WORKING = "working"  # Active task context
    EPISODIC = "episodic"  # Past interactions and events
    SEMANTIC = "semantic"  # Stable facts, preferences, knowledge
    PROCEDURAL = "procedural"  # Reusable instructions, workflows, recipes


class MemoryRecord(BaseModel):
    """Structured memory entry in persistent storage."""

    id: str = Field(description="Unique record ID (UUID)")
    memory_type: MemoryType
    key: str = Field(description="Lookup key or semantic tag")
    content: str = Field(description="Information payload")
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(default="user_interaction")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None


class ToolDefinition(BaseModel):
    """Metadata and schema for an approved system tool."""

    id: str
    name: str
    description: str
    permission_level: PermissionLevel
    parameters_schema: dict[str, Any]
    is_reversible: bool
    timeout_seconds: float = 10.0


class ToolExecutionResult(BaseModel):
    """Outcome of a tool execution."""

    tool_id: str
    success: bool
    data: Any | None = None
    error: str | None = None
    duration_ms: float = 0.0


class TaskRequest(BaseModel):
    """Inbound request from user interface."""

    prompt: str
    conversation_id: str | None = None
    reasoning_override: ReasoningDepth | None = None
    stream: bool = True


class ChatMessage(BaseModel):
    """Normalized chat message structure."""

    role: str = Field(description="'user', 'assistant', 'system', 'tool'")
    content: str
    thinking: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
