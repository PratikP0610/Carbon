# Carbon Data Flow & Execution Lifecycle

## 1. End-to-End User Interaction Flow

```
[User Input] (Native macOS GUI or Voice STT)
      │
      ▼
[Carbon Loopback API] (FastAPI: /api/v1/chat or WebSocket /ws/chat)
      │
      ▼
[Carbon Core Orchestrator]
      │
      ├─► [Memory Engine] Query working/semantic memory for context
      │
      ├─► [Intelligence Engine]
      │         ├─ Intent Classification & Complexity Estimation
      │         └─ Select Reasoning Depth: Instant | Standard | Deep
      │
      ├─► [Carbon Model Supervisor (CMS)]
      │         ├─ Validate active model capabilities
      │         ├─ If mismatch: Trigger Controlled Model Switch
      │         └─ Acquire Single-Model Inference Lease (Mutex)
      │
      ├─► [Model Provider: Ollama (Qwen 3.5 9B)]
      │         ├─ Stream tokens back to Core
      │         └─ Parse structured action calls (if any)
      │
      ▼ (If tools requested)
[Governance Engine]
      ├─ Evaluate Permission Level (P0..P4) vs Autonomy Level (A0..A3)
      ├─ If P2+: Dispatch Confirmation Request to Native UI
      └─ Write decision to append-only Audit Log
      │
      ▼ (Upon Authorization)
[Tool Execution Engine]
      ├─ Validate arguments against Pydantic schema
      ├─ Run tool in bounded async sandbox with timeout
      └─ Return ToolExecutionResult to Core -> Re-prompt Model
      │
      ▼
[Response Finalization]
      ├─ Write episodic record to SQLite Memory
      ├─ Stream final response tokens to SwiftUI UI
      └─ Release CMS Inference Lease
```

---

## 2. Model Switching Sequence

```mermaid
sequenceDiagram
    autonumber
    actor Core as Carbon Core
    participant CMS as Model Supervisor
    participant Lock as Concurrency Mutex
    participant Prov as Ollama Provider
    participant Audit as Audit Log

    Core->>CMS: Request Model for Task(Capabilities, ReasoningDepth)
    CMS->>CMS: Evaluate active model vs required capabilities
    alt Active model meets requirements
        CMS-->>Core: Active Model Ready (qwen3.5:9b)
    else Capabilities or resources require switch
        CMS->>Lock: Acquire exclusive model switch lock
        CMS->>Prov: Unload active model (keep_alive: 0)
        Prov-->>CMS: Model memory freed
        CMS->>Prov: Pre-warm target model
        Prov-->>CMS: Target model loaded & healthy
        CMS->>Audit: Record Model Switch Event
        CMS->>Lock: Release switch lock
        CMS-->>Core: Switch Complete (Target Model Active)
    end
```

---

## 3. Tool Authorization & Governance Flow

```mermaid
sequenceDiagram
    autonumber
    participant LLM as Model Worker
    participant Core as Carbon Core
    participant Gov as Governance Engine
    participant UI as Native SwiftUI
    participant Tool as Tool Executor
    participant Audit as Audit Log

    LLM->>Core: Request Tool Execution(tool_id, args)
    Core->>Gov: Authorize(tool_id, permission_level, args)
    Gov->>Gov: Check against current Autonomy Level (A0-A3)
    alt Auto-approved (e.g. P0/P1 in A2)
        Gov->>Audit: Log Auto-Authorization
        Gov-->>Core: Authorized
    else Requires User Consent (P2+ or A1)
        Gov->>UI: Prompt User Approval Dialog(tool_id, args, impact)
        UI-->>Gov: User Response (Approved / Denied)
        Gov->>Audit: Log User Decision (Approved/Denied, Timestamp)
        Gov-->>Core: Result
    end
    alt If Authorized
        Core->>Tool: Execute with timeout
        Tool-->>Core: ToolExecutionResult
        Core->>Audit: Log Execution Result
    else If Denied
        Core->>LLM: Tool Cancelled by User
    end
```
