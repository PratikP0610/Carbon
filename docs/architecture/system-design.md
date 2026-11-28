# Carbon System Design

**Stage 7.3 Alignment Status:** Aligned to the verified repository implementation.
**Scope Note:** This document describes Carbon's architectural layers, module dependencies, and what each component **does** and **does not** do in current code. For the high-level component table, see [Overview](./overview.md); for protocol and boundary contracts, see [Component Contracts](./component-contracts.md).

---

## 1. Design Principles in the Current Codebase

Carbon follows a **single-host modular monolith** design (see [ADR-0001](./adr/0001-modular-monolith-architecture.md) through [ADR-0007](./adr/0007-secret-storage-keychain-vs-posix-file.md)) structured around five engineering constraints visible across the source tree:

1. **Loopback-Only Local Execution**: FastAPI binds to `127.0.0.1:8765`, rejects non-loopback `Host`/client origins on HTTP and WebSocket upgrades, and requires constant-time bearer-token authentication for protected routes.
2. **Strict Model Authority (`CMS`)**: Every inference request passes through `CarbonModelSupervisor` (`cms.py`), which enforces an approved allowlist (`DEFAULT_APPROVED_MODELS = ("qwen3.5:9b",)`), a `9216` MB memory budget (`DEFAULT_MODEL_MEMORY_BUDGET_MB = 9216`), and a single-model concurrency lease (`acquire_inference_lease`).
3. **Fail-Closed Governance**: Tools and protected actions execute only after centralized validation and `GovernanceEngine` authorization (`default DENY`), with sensitive operations requiring single-use argument-bound human approvals (`HITL`) and writing to a SHA-256 hash-chained `audit.log`.
4. **Cancellation-Safe Local Persistence**: Conversations, tasks, and four memory tiers are stored locally in SQLite (`sqlite_memory.py`) with WAL journaling, forensic preservation on corruption, and Stage 6.9-F cancellation-safe transactions (`_db_lock` + uninterruptible rollback/commit cleanup).
5. **Owned-Process Discipline in Swift**: The SwiftUI client (`ProcessSupervisor.swift`) reuses already-running healthy local processes without adopting ownership of them, tracks only child processes it spawned, and enforces a bounded 5-phase shutdown with an `8.0s` global watchdog.

---

## 2. Layer Dependency Architecture

Dependencies flow strictly downward from UI supervision through the API gateway and orchestrator to the control-plane services and local storage/inference engines:

```text
[Layer 1: SwiftUI App & ProcessSupervisor]
   (CarbonApp, ContentView, CarbonClient, ProcessSupervisor, SingleInstanceLock, MemoryPressureMonitor)
       |
       |  HTTP REST + WebSocket (/ws/chat) over 127.0.0.1:8765
       v
[Layer 2: FastAPI API & IPC Gateway]  (src/carbon/main.py)
       |
       +---> verify_local_auth() + loopback/Origin middleware
       |
       v
[Layer 3: Orchestrator & Intelligence]
   - CarbonCore (src/carbon/core/orchestrator.py)
   - IntelligenceEngine (src/carbon/intelligence/engine.py)
   - MetricsRegistry & logging (src/carbon/core/metrics.py, logging.py)
       |
       +-------------------+-------------------+-------------------+-------------------+
       |                   |                   |                   |                   |
       v                   v                   v                   v                   v
[CarbonModelSupervisor] [SQLiteMemoryEngine] [GovernanceEngine] [ToolRegistry]  [DeviceManager]
(model_control/cms.py)  (memory/sqlite...)   (governance/...)   (tools/...)     (devices/...)
       |                   |                   |                   |                   |
       v                   v                   v                   v                   v
[OllamaProvider /     [SQLite + WAL + FTS5] [audit.log +       [system_info,     [platform +
 FakeProvider]        (carbon.db)           _pending_approvals] list_apps,        sysctl hw.memsize]
       |                                                         open_application]
       v
[Ollama 127.0.0.1:11434]
```

**Runtime vs. Test Substitution:** In production, `CarbonCore` uses `OllamaProvider` (pointing to `CARBON_OLLAMA_URL`, default `http://127.0.0.1:11434`). When `FakeModelProvider` is injected (used in automated tests and isolated harness scenarios), `CarbonModelSupervisor` operates against the same approval, budget, and lease contracts without requiring live GPU/Ollama inference.

---

## 3. Component-by-Component System Design

### 3.1 Presentation & macOS Supervision (`apps/macos/Carbon/Sources/`)

- **Components**: `CarbonApp.swift`, `ContentView.swift`, `MarkdownRenderer.swift`, `CarbonClient.swift`, `ProcessSupervisor.swift`, `SingleInstanceLock.swift`, `MemoryPressureMonitor.swift`.
- **What it does**:
  - Acquires an exclusive advisory lock (`flock(LOCK_EX | LOCK_NB)`) on `~/.carbon/carbon.lock` via `SingleInstanceLock` so only one UI instance manages startup/shutdown at a time.
  - Discovers Ollama (`/opt/homebrew/bin/ollama`, `/usr/local/bin/ollama`, `/Applications/Ollama.app/Contents/Resources/ollama`, plus `$PATH`) and Python/Uvicorn, spawns them only if not already running locally, and records ownership (`_childOllamaProcess`, `_childBackendProcess`).
  - Streams `/ws/chat` events into SwiftUI (`ContentView`), renders code/markdown blocks (`MarkdownRenderer`), and displays inline approval prompts for HITL decisions.
  - Listens for kernel memory-pressure notifications (`MemoryPressureMonitor` + `MemoryPressureDebouncer`: `0.5s` escalation debounce, `10.0s` recovery hysteresis) and reports pressure levels (`1=normal`, `2=warning`, `4=critical`) to `/api/v1/system/memory-pressure`.
  - Runs a 5-phase shutdown (`ShutdownTimeoutConfig.standard`: inference cancel `1.2s`, WebSocket close `0.3s`, backend HTTP shutdown `3.0s`, owned Ollama termination `2.0s`, lock cleanup `0.3s`, global watchdog `8.0s`).
- **Depends on**: Local Ollama CLI, local Python virtual environment (`src/carbon/main.py`), Carbon REST + WebSocket API (`127.0.0.1:8765`), auth token file (`.carbon_token`).
- **What it does NOT do**: Does not run as a signed `.app` bundle or macOS menu-bar accessory yet; does not implement voice capture/playback UI; does not read tokens from macOS Keychain (`ADR-0007` is deferred).
- **Status**: **IMPLEMENTED** (SPM executable target; packaging/menu bar remain **PLANNED**, Keychain remains **DEFERRED**).

### 3.2 API & IPC Gateway (`src/carbon/main.py`)

- **What it does**:
  - Creates the FastAPI application, binds lifespan initialization/shutdown directly to `CarbonCore`, enforces loopback host/origin validation on HTTP and WebSocket connections, and protects non-public endpoints with `verify_local_auth` (constant-time comparison against the active token).
  - Supports token rotation (`POST /api/v1/auth/rotate-token`), which atomically replaces `.carbon_token` (`0600`) and immediately disconnects stale WebSocket sessions with code `4401` (there is no 30-second grace period).
  - Hosts 24 REST routes (`health`, `metrics`, `auth/rotate-token`, `models/*`, `tasks/*`, `tools/*`, `approvals/*`, `system/*`, `memory/*`, `conversations/*`) and the single `/ws/chat` WebSocket channel (`MAX_CONCURRENT_WS_CONNECTIONS = 5`, `WS_AUTH_TIMEOUT_SECONDS = 3.0`).
  - Disconnects from active streaming tasks cancel the backend execution, whereas disconnects after a task has already reached a terminal state preserve completion persistence (`Stage 6.9-F`).
- **Depends on**: `CarbonCore`, `Settings`.
- **What it does NOT do**: Does not listen on non-loopback interfaces; does not accept query-string tokens on WebSockets; does not expose a legacy `/api/v1/chat` REST route; does not implement voice endpoints.
- **Status**: **IMPLEMENTED**.

### 3.3 Core Orchestrator (`src/carbon/core/orchestrator.py`)

- **What it does**:
  - Initializes and owns `SQLiteMemoryEngine`, `GovernanceEngine`, `DeviceManager`, `ToolRegistry`, `CarbonModelSupervisor`, `IntelligenceEngine`, and `MetricsRegistry`.
  - Executes tasks via `process_task_stream()`, managing `TaskState` transitions (`idle`, `queued`, `planning`, `awaiting_approval`, `executing_tool`, `streaming`, `completed`, `cancelled`, `failed`, `rejected`).
  - Implements deterministic fast-paths before falling back to LLM streaming:
    - `SYSTEM_COMMAND` matching `"open <app>"`: validates with `OpenApplicationTool`, requests `P2` HITL approval (`awaiting_approval`, `60.0s` timeout), and executes directly on user approval.
    - `MEMORY_STORE`, `MEMORY_FORGET`, and `MEMORY_QUERY` intents: persist records, evaluate confirmation-safe forgetting, or retrieve matching memory context.
  - Injects matching persistent memories and recent conversation turns into the model system prompt marked explicitly as **untrusted historical data** so stored text cannot override instructions or grant permissions.
  - Coordinates CMS pre-lease planning, optional model switch, `acquire_inference_lease`, post-lease synchronous verification, effective `num_ctx` calculation, token streaming, task persistence, and periodic background TTL purge + WAL checkpointing.
- **Depends on**: `SQLiteMemoryEngine`, `GovernanceEngine`, `CarbonModelSupervisor`, `IntelligenceEngine`, `ToolRegistry`, `DeviceManager`, `MetricsRegistry`.
- **What it does NOT do**: Does not maintain an `asyncio.PriorityQueue` scheduler; does not emit or resume a `CHECKPOINTED` state; does not run a general LLM -> tool-call -> re-prompting agent loop during chat streaming.
- **Status**: **PARTIALLY IMPLEMENTED** (core streaming, fast-paths, and cancellation/recovery are implemented; general autonomous tool-calling loop and priority scheduler are not present).

### 3.4 Intelligence Engine (`src/carbon/intelligence/engine.py`)

- **What it does**:
  - Implements `classify_intent(prompt, context)` using ordered, compiled regular expressions and heuristics (deep-reasoning keywords, memory store/forget/query patterns, greetings/`open <app>`, arithmetic, coding detection, standard analytical indicators, simple factual patterns, and concise-clause detection) to emit a `TaskIntent` (`category`, `task_category`, `reasoning_depth`, `required_capabilities`, `confidence`).
  - Implements `validate_output(raw_output, expected_schema)` to parse JSON (optionally inside Markdown fences) and validate against schema constraints.
- **Depends on**: `carbon.interfaces.models`, `carbon.tools.validation`.
- **What it does NOT do**: Does not execute a second LLM call for planning or multi-step decomposition (`plan_execution` does not exist in `IIntelligenceEngine` or `IntelligenceEngine`).
- **Status**: **PARTIALLY IMPLEMENTED** (regex/heuristic classification + JSON schema output validator are implemented; multi-step planning is **PLANNED**).

### 3.5 Carbon Model Supervisor (`src/carbon/model_control/cms.py`) & Providers

- **What it does**:
  - Enforces strict model governance: only models in `approved_models` (default `("qwen3.5:9b",)`), with `model_memory_budget_mb` (default `9216` MB) and required capabilities, can be planned or activated.
  - Discovers available and currently loaded models from `IModelProvider` (`/api/tags`, `/api/ps`, `/api/version` via `OllamaProvider`).
  - Serializes inference across tasks using an exclusive inference lease (`acquire_inference_lease` / release) and guards `/api/v1/models/refresh` with a drain check (`409 Conflict` if an active lease does not drain).
  - Applies dynamic context sizing (`num_ctx`) based on reported Mach memory pressure (`1`, `2`, `4`) and active lease snapshots.
- **Depends on**: `IModelProvider` (`OllamaProvider` or `FakeModelProvider`), `GovernanceEngine`, `Settings`.
- **What it does NOT do**: Does not allow unapproved Ollama tags to run even if installed locally in Ollama; does not run concurrent inference across multiple tasks or multiple loaded models at once (`ADR-0003`).
- **Status**: **IMPLEMENTED** (see [CMS Lifecycle and State Machine](./cms-lifecycle-and-state-machine.md) for detailed state transitions).

### 3.6 Persistent Memory Engine (`src/carbon/memory/sqlite_memory.py`)

- **What it does**:
  - Persists four `MemoryType` tiers (`working`, `episodic`, `semantic`, `procedural`) in `memory_records` alongside `conversations`, `tasks`, `schema_migrations`, and FTS5 table `memory_fts`.
  - Runs SQLite in `WAL` mode (`foreign_keys=ON`, `synchronous=NORMAL`, `busy_timeout=5000`), checks database header + `PRAGMA integrity_check` before applying migrations (`initialize()`), and quarantines corrupt DB + WAL/SHM artifacts into `forensics/` rather than silently recreating or proceeding.
  - Serializes database operations with `_db_lock` (`asyncio.Lock`) and uses Stage 6.9-F cancellation-safe `_write_transaction()` wrappers (`_in_write_tx`, defensive pre-begin rollback, shielded `_await_task_resolving_cancellation` for `BEGIN IMMEDIATE`, transaction body, `_rollback_uninterruptible`, and `COMMIT`).
  - Supports confirmation-safe forgetting (`forget_memory()` preview before confirmation / delete) and periodic TTL purging (`DEFAULT_EPISODIC_TTL_DAYS = 7`, `DEFAULT_WORKING_TTL_HOURS = 24`, `DEFAULT_TASK_TTL_DAYS = 30`, `DEFAULT_TASK_PAYLOAD_TTL_DAYS = 7`).
- **Depends on**: Python `sqlite3` via `asyncio.get_running_loop().run_in_executor(...)`, filesystem (`CARBON_DB_PATH`).
- **What it does NOT do**: Does not store a `model_registry` table in SQLite; does not perform vector-embedding similarity search (`IEmbeddingEngine` does not exist). Note that the live user database `data/carbon.db` is currently on schema **v2** while current code defines migrations up to **v3** (see [Database Schema](./database-schema.md)).
- **Status**: **IMPLEMENTED**.

### 3.7 Governance, Tool Execution & Device Management

- **GovernanceEngine (`src/carbon/governance/governance_engine.py`)**: **IMPLEMENTED**. Maps `PermissionLevel` (`P0`–`P4`) against `AutonomyLevel` (`A0`–`A3`), denies `P4` unconditionally, permits `P0` unconditionally, and gates higher tiers cleanly. Creates single-use approval records in memory (`_pending_approvals`) bound to `(tool_id, arguments_digest)` (`SHA-256` of canonical JSON arguments), tracks `DecisionSource` so only `USER_UI` / `USER_API` can resolve approvals, and appends SHA-256 hash-chained records to `audit.log` after verifying chain tail integrity on startup.
- **ToolRegistry & Built-ins (`src/carbon/tools/`)**: **PARTIALLY IMPLEMENTED**. Enforces schema validation (`validation.py`), permission checks (`base.py`), timeouts (`5.0s` on built-ins), and audit logging for three built-in tools (`system_info` `P1`, `list_applications` `P1`, `open_application` `P2`). `ToolRegistry.get_schemas_for_llm()` exists on the registry but is not passed into LLM inference calls.
- **DeviceManager (`src/carbon/devices/manager.py`)**: **PARTIALLY IMPLEMENTED**. Queries local macOS CPU count, OS version, and unified memory (`sysctl hw.memsize` + `platform`) with a 30-second cache TTL (`DEFAULT_HARDWARE_CACHE_TTL_SECONDS = 30.0`), while `_connected_nodes` remains an empty list reserved for future device pairing (**PLANNED**).
- **Voice Subsystem (`src/carbon/voice/`)**: **DEFERRED**. Empty directory (`0` files); Stage 6.6 closed as deferred.

---

## 4. Environment & Configuration Isolation

`Settings` (`src/carbon/config/settings.py`) uses the `CARBON_` prefix (plus `CARBOND_` fallback on select paths via `Settings`). Testing and isolated runs rely on five required environment variables to prevent touching user data:

- `CARBON_DB_PATH` (default `data/carbon.db`)
- `CARBON_AUDIT_LOG_PATH` (default `data/audit.log`)
- `CARBON_LOG_DIR` (default `data/logs`)
- `CARBON_API_TOKEN_PATH` (default `.carbon_token`)
- `CARBON_PORT` (default `8765`, integer port validation)
