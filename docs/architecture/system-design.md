# Carbon System Design

## 1. System Topology

Carbon operates as a cooperating pair of processes running locally on macOS:

1. **Carbon Desktop Frontend (SwiftUI):** Native macOS GUI and menu-bar application managing UI events, audio I/O, native dialogs, and user input.
2. **Carbon Core Backend (Python 3.12+):** Async service managing orchestration, intelligence, CMS, memory, governance, and tools.

Inter-process communication (IPC) is conducted exclusively over loopback TCP (`127.0.0.1`) using HTTP/REST for state queries/actions and WebSocket/SSE for real-time bi-directional streaming.

---

## 2. Subsystem Deep Dive

### 2.1 Carbon Core (Orchestrator)

The Core Orchestrator is the central nervous system of Carbon.

- **Lifecycle Coordinator:**
  - Performs dependency order startup: Config -> Database/WAL -> Audit Logger -> Model Registry & CMS -> Memory Engine -> Governance Engine -> Tool Registry -> Device Engine -> API Server.
  - Intercepts POSIX signals (`SIGINT`, `SIGTERM`) to trigger clean drain and shutdown, closing active client sockets, flushing database WAL commits, and releasing model locks.
- **Task Management State Machine:**
  - Tasks represent discrete user instructions or workflows.
  - States: `PENDING` -> `ANALYZING` -> `AWAITING_APPROVAL` -> `EXECUTING` -> `STREAMING` -> `COMPLETED` | `FAILED` | `CANCELLED`.
  - Tasks maintain immutable context, execution history, and cancellation tokens.

```mermaid
stateDiagram-v2
    [*] --> PENDING: User Request
    PENDING --> ANALYZING: Core dispatches to Intelligence
    ANALYZING --> AWAITING_APPROVAL: Tool requires P2+ approval
    ANALYZING --> EXECUTING: P0/P1 auto-approved
    AWAITING_APPROVAL --> EXECUTING: User Approves
    AWAITING_APPROVAL --> CANCELLED: User Denies
    EXECUTING --> STREAMING: Tokens / Status Events
    STREAMING --> COMPLETED: Execution Finished
    EXECUTING --> FAILED: Tool / Model Error
    PENDING --> CANCELLED: Request Cancelled
```

---

### 2.2 Carbon Model Supervisor (CMS)

CMS is the sole authority governing model selection, verification, inference leases, and lifecycle transitions.

- **Exclusive Inference Lease Mechanism:**
  - All chat model access requires acquiring a tokenized `InferenceLease` bounded by a strict timeout (default 60s).
  - Leases are managed via async context managers (`async with cms.lease(task_id): ...`), guaranteeing release on cancellation or exception.
- **Fair Reader-Writer Concurrency Gate (RWLock):**
  - **Inference (Shared Reader):** Grants leases to tasks targeting the resident active model. The single-active-chat-LLM invariant enforces `max_concurrent_chat_leases = 1`.
  - **Switching (Exclusive Writer):** Initiated when a task requires capabilities absent in the active model. Immediately sets `switch_pending = True`, blocking new inference leases, and awaits full drain (`active_leases == 0`) before safely commanding model unload.
  - Guarantees model switching **can never unload a model during active inference**.
- **Request Queuing, Cancellation, and Checkpointing:**
  - Queuing via `asyncio.PriorityQueue` (Priority 0: interactive/cancellation, Priority 1: standard tasks, Priority 2: background tasks).
  - Rapid cancellation via per-task cancellation tokens (`asyncio.Event`), instantly severing HTTP streaming sockets and releasing leases.
  - Task checkpointing in SQLite (`tasks` table) before tool dispatches to allow safe resumption or rollback.
- **Switching & Failure Recovery Protocol:**
  - Unloads active weights via Ollama `keep_alive: 0` and verifies Metal buffer deallocation.
  - Pre-warms target model with health checks.
  - **Failed Unload Recovery:** Retries up to 3 times; if unloading fails, CMS **aborts the switch** to prevent running two models concurrently and crashing host unified memory.
  - **Failed Load Recovery:** If the target model fails to load within 15s, CMS automatically rolls back to the primary approved model (`qwen3.5:9b`).
- **Dynamic Unified Memory Management:**
  - Discards static fixed memory budgets in favor of real-time Mach kernel pressure monitoring (`kern.memorystatus_vm_pressure_level`).
  - Distinguishes **Disk Footprint** (6.55 GB APFS file), **Resident VRAM** (~6.10 GB Metal allocation), and **Host Memory Pressure** (Normal=1, Warning=2, Critical=4).
  - **Pressure Reaction Ladder:**
    - *Level 1 (Normal):* Model kept warm (`keep_alive: 5m`), full context window (up to 32K tokens).
    - *Level 2 (Warning):* Unload idle specialist models, cap context window to 8,192 tokens, enforce `keep_alive: 0` for non-interactive tasks.
    - *Level 4 (Critical):* Unload chat model immediately post-inference, disallow deep reasoning, notify UI.
- **Specialist Workload Isolation:**
  - Speech-to-Text (STT), Text-to-Speech (TTS), and Embedding engines have separate interfaces and strict independent memory budgets (≤ 500 MB).
  - Specialist workloads run on Apple Neural Engine (ANE) or CPU cores to avoid Metal GPU buffer contention with the primary Chat LLM.

---

### 2.3 Intelligence Engine

The Intelligence Engine bridges human language and computational execution.

- **Intent & Complexity Assessment:**
  - Fast-path heuristic and model-guided classification of user queries into categories: `CONVERSATIONAL`, `FACTUAL_QUERY`, `SYSTEM_COMMAND`, `MULTI_STEP_PLAN`.
  - Determines required reasoning depth:
    - **Instant:** Direct responses without chain-of-thought, low latency (< 1s first-token target).
    - **Standard:** Single-stage reasoning, structured answers.
    - **Deep:** Explicit thinking phase enabled (`qwen3.5:9b` native thinking block), multi-step verification.
- **Task Planner:**
  - For complex tasks, constructs an execution plan with discrete tool actions.
  - Formats instructions using constrained prompt templates ensuring deterministic JSON tool-call schema conformance.
- **Output Validator:**
  - Parses and validates LLM generation against expected schemas before dispatching actions to Carbon Core.
  - Filters malformed hallucinations or unapproved action requests.

---

### 2.4 Persistent Memory Engine

Memory is organized across four distinct functional categories stored in local SQLite tables:

1. **Working Memory:** In-memory + fast-cached task context for the active session. Flushed upon task termination or reset.
2. **Episodic Memory:** Chronological records of past user conversations, executed actions, and outcomes. Indexed by timestamp and conversation ID.
3. **Semantic Memory:** Stable user preferences, profiles, project contexts, and verified facts.
4. **Procedural Memory:** Reusable multi-step workflows, tool call templates, and system operation recipes.

- **Retention & Forgetting:**
  - Implements a privacy-first retention policy.
  - User can execute explicit forgetting commands ("Forget what I said about...", "Clear my work history from today").
  - Automated relevance decay avoids prompt bloat and unnecessary retrieval overhead.

---

### 2.5 Governance & Permissions Engine

The Governance Engine serves as Carbon's security perimeter.

- **Permission Tiers:**
  - **P0 (Conversational):** Read-only factual chat, system status query. No side effects.
  - **P1 (Read Approved):** Read permitted application state, list active windows, check system metrics.
  - **P2 (Reversible Execution):** Launch approved macOS applications, switch workspace, copy to clipboard.
  - **P3 (State Modification):** Modify files, update settings, send network requests, trigger calendar entries. Requires explicit user confirmation dialog.
  - **P4 (High-Impact / Destructive):** File deletion, credential operations, system restarts. Requires explicit double-confirmation or hard prohibition.
- **Autonomy Levels:**
  - **A0 (Observe):** Carbon only observes and answers; no tool executions permitted.
  - **A1 (Assist & Recommend):** Carbon generates action plans and presents them to the user for one-click approval. (Default mode).
  - **A2 (Execute Approved):** Carbon automatically executes P0–P2 operations; requests approval only for P3+.
  - **A3 (Bounded Autonomy):** Carbon autonomously executes multi-step plans within strict pre-declared policy boundaries and limits.

---

### 2.6 Tool Execution Framework

All external computer interactions occur strictly through registered tools implementing the `BaseTool` contract:

- **Strict Validation:** Every tool inputs and outputs must adhere to a Pydantic `BaseModel` schema.
- **Isolated Execution:** Tools execute in isolated async worker tasks with configurable timeouts (default 10s).
- **Audit Logging:** Every tool request, authorization status, parameter set, return value, and execution duration is cryptographically hashed and logged to `data/audit.log`.

---

### 2.7 Device Engine

Manages the local host environment and prepares for multi-device topology:

- **Local Mac Node:** Inspects macOS hardware capabilities, battery state, thermal throttling status, and available unified memory.
- **Connected Node Protocol (Future):** Pre-defines cryptographic handshake interfaces, mutual TLS, and capability negotiation for companion Android devices (Samsung Galaxy S23 FE).
