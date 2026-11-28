# Carbon Architecture Overview

**Stage 7.3 Alignment Status:** Aligned to the verified repository implementation.
**Scope Note:** This document provides the high-level architecture overview and the **High-Level Component Map**. For detailed dependency rules, see [System Design](./system-design.md); for component interface boundaries, see [Component Contracts](./component-contracts.md); for runtime flows, see [Data Flow](./data-flow.md); and for persistence tables and migrations, see [Database Schema](./database-schema.md).

---

## 1. Architectural Pattern & Runtime Topology

Carbon is a **local-first, modular-monolith AI assistant** built for macOS. Instead of distributing logic across external cloud services or many independent microservices, Carbon runs as **two local processes on loopback (`127.0.0.1`)** backed by a single-file SQLite database and a local Ollama runtime:

1. **SwiftUI Native Desktop Client & Process Supervisor** (`apps/macos/Carbon/Sources/`):
   A macOS desktop interface (`CarbonApp`) that acquires a `flock(2)` single-instance lock (`SingleInstanceLock`), starts or attaches to Ollama (`127.0.0.1:11434`) and the Python backend (`127.0.0.1:8765`), monitors Mach kernel memory pressure (`MemoryPressureMonitor`), and streams chat responses and human-in-the-loop (HITL) approval prompts over HTTP/WebSockets (`CarbonClient`).
2. **Python FastAPI Control & Intelligence Backend** (`src/carbon/`):
   A single asyncio process hosted by Uvicorn (`src/carbon/main.py`) that exposes authenticated loopback REST and `/ws/chat` endpoints and coordinates orchestration (`CarbonCore`), rule-based intent classification (`IntelligenceEngine`), model lifecycle supervision (`CarbonModelSupervisor`), SQLite memory (`SQLiteMemoryEngine`), permission governance and audit logging (`GovernanceEngine`), tool execution (`ToolRegistry`), and host status (`DeviceManager`).
3. **Local Inference & Persistence Backends**:
   - **Ollama** (`OllamaProvider` over HTTP on `127.0.0.1:11434`, with `FakeModelProvider` available for deterministic testing).
   - **SQLite** (configured via `CARBON_DB_PATH`, defaulting to `data/carbon.db`, with WAL journaling and FTS5 full-text indexing).

### Five-Layer Architecture

```text
+-------------------------------------------------------------------------+
| Layer 1: Presentation & Process Supervision (Swift / SwiftUI)           |
|   CarbonApp, ContentView, MarkdownRenderer, CarbonClient,               |
|   ProcessSupervisor, SingleInstanceLock, MemoryPressureMonitor          |
+------------------------------------+------------------------------------+
                                     | HTTP REST + WebSocket (/ws/chat)
                                     | Loopback 127.0.0.1 + Bearer Token
                                     v
+-------------------------------------------------------------------------+
| Layer 2: API & IPC Gateway (src/carbon/main.py - FastAPI / Uvicorn)     |
|   Loopback & Origin middleware, Bearer auth, REST routes, /ws/chat      |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
| Layer 3: Orchestration & Intent Routing (src/carbon/core & intelligence)|
|   CarbonCore (task lifecycle, fast paths, prompt assembly, metrics)     |
|   IntelligenceEngine (regex/heuristic classify_intent & validate_output)|
+--------+--------------------+--------------------+----------------------+
         |                    |                    |
         v                    v                    v
+-------------------------------------------------------------------------+
| Layer 4: Control Plane, Memory, Governance & Tools                      |
|   - Model Control (CarbonModelSupervisor, 9216 MB budget, single lease) |
|   - Persistent Memory (SQLiteMemoryEngine, 4 memory tiers + FTS5)       |
|   - Governance & Audit (GovernanceEngine P0-P4/A0-A3, chained audit.log)|
|   - Tool Execution (ToolRegistry, system_info, list/open_application)   |
|   - Device Info (DeviceManager: macOS host stats; Android placeholder)  |
+--------+-----------------------------------------+----------------------+
         |                                         |
         v                                         v
+----------------------------------+   +----------------------------------+
| Layer 5A: Local Model Runtime    |   | Layer 5B: Local Persistence      |
|   Ollama (127.0.0.1:11434)       |   |   SQLite (carbon.db + WAL)       |
|   Primary: qwen3.5:9b            |   |   Audit Log (data/audit.log)     |
|   Allowlist: ('qwen3.5:9b',)     |   |   Auth Token (.carbon_token)     |
+----------------------------------+   +----------------------------------+
```

---

## 2. Current Model & Resource Baseline

Verified defaults in `src/carbon/config/settings.py` and `src/carbon/model_control/cms.py`:

| Setting / Constant | Current Value | Notes |
| --- | --- | --- |
| `DEFAULT_PRIMARY_MODEL` | `qwen3.5:9b` | Default single active model |
| `DEFAULT_APPROVED_MODELS` | `("qwen3.5:9b",)` | Approved allowlist enforced by CMS |
| `DEFAULT_MODEL_MEMORY_BUDGET_MB` | `9216` (9 GiB) | Unified-memory budget gate enforced by CMS |
| Active Inference Concurrency | 1 leased task at a time | Single-model concurrency lease (`ADR-0003`) |

---

## 3. High-Level Component Map

Carbon uses this section as its canonical high-level component map (no separate `component-map.md` file is maintained). Every component is classified using Carbon's Stage 7 status labels:

- **IMPLEMENTED**: Present in the repository, wired at runtime, and aligned with current code.
- **PARTIALLY IMPLEMENTED**: Exists in code with core capability working, but incomplete compared to original target design.
- **DEFERRED**: Intentionally deferred out of current release scope.
- **PLANNED**: Designed in ADRs/roadmaps, not yet built in code.
- **UNVERIFIED**: Cannot be confirmed as verified in the active environment.

### Python Backend Components (`src/carbon/`)

| Component | Source Location | Layer | Responsibility | Key Dependencies | Status |
| --- | --- | --- | --- | --- | --- |
| **API & IPC Gateway** | `src/carbon/main.py` | Layer 2 | Loopback FastAPI app, REST endpoints, `/ws/chat`, origin & bearer-token middleware | `CarbonCore`, `Settings` | **IMPLEMENTED** |
| **Core Orchestrator** | `src/carbon/core/orchestrator.py` | Layer 3 | End-to-end task lifecycle, fast-paths (`open_application`, memory queries/writes/forgetting), prompt building, stream relay, task persistence, periodic maintenance | `IntelligenceEngine`, `CarbonModelSupervisor`, `SQLiteMemoryEngine`, `GovernanceEngine`, `ToolRegistry`, `DeviceManager`, `MetricsRegistry` | **PARTIALLY IMPLEMENTED** *(No `PriorityQueue`, `CHECKPOINTED` state, or multi-step LLM->tool loop)* |
| **Intelligence Engine** | `src/carbon/intelligence/engine.py` | Layer 3 | Rule/regex-based `classify_intent()` (intent, `TaskCategory`, `ReasoningDepth`) and schema `validate_output()` | `ToolArgumentValidationError` validator | **PARTIALLY IMPLEMENTED** *(Heuristic rules only; no LLM multi-step planner)* |
| **Model Control Plane (CMS)** | `src/carbon/model_control/cms.py` | Layer 4 | Approved model allowlist, `9216` MB budget gating, inference lease (`acquire_inference_lease`), safe model switching, drain guard on refresh, pressure-aware `num_ctx` calculation | `IModelProvider`, `GovernanceEngine` | **IMPLEMENTED** |
| **Model Providers** | `src/carbon/model_control/ollama_provider.py`, `fake_provider.py` | Layer 4/5 | Streaming `/api/generate` (`generate_stream_events`; unused `chat_stream` `/api/chat` helper present) + discovery (`/api/tags`, `/api/ps`, `/api/version`) for Ollama, plus `FakeProvider` test stub | `httpx.AsyncClient` -> Ollama (`127.0.0.1:11434`) | **IMPLEMENTED** |
| **Memory Engine** | `src/carbon/memory/sqlite_memory.py` | Layer 4/5 | SQLite persistence for 4 memory tiers, conversations, and tasks; FTS5 search; TTL purging; forensic quarantine on corruption; cancellation-safe write transactions (Stage 6.9-F) | `sqlite3` (asyncio executor + `_db_lock`) | **IMPLEMENTED** |
| **Governance & Audit Engine** | `src/carbon/governance/governance_engine.py` | Layer 4 | Default-deny authorization (`P0`–`P4` vs `A0`–`A3`), single-use argument-bound HITL approvals, SHA-256 hash-chained `audit.log` | `audit.log`, `sanitize_structured_data` | **IMPLEMENTED** |
| **Tool Framework & Built-ins** | `src/carbon/tools/base.py`, `builtin.py`, `validation.py` | Layer 4 | `ToolRegistry` centralized validation + permission gate; `system_info` (`P1`), `list_applications` (`P1`), `open_application` (`P2`) | `GovernanceEngine`, macOS `open` / app bundles | **PARTIALLY IMPLEMENTED** *(Registry works; `get_schemas_for_llm()` is not wired into live LLM streaming)* |
| **Device Manager** | `src/carbon/devices/manager.py` | Layer 4 | Local macOS host status (`sysctl hw.memsize` + `platform`, cached 30s) | `sysctl`, `platform` | **PARTIALLY IMPLEMENTED** *(Remote/Android `_connected_nodes` is an empty placeholder)* |
| **Configuration & Secrets** | `src/carbon/config/settings.py`, token helpers in `main.py` | Cross-cutting | Pydantic `Settings` + `CARBON_*` env overrides + `0600` `.carbon_token` / `CARBON_API_TOKEN` auth | Filesystem / Environment | **IMPLEMENTED** *(POSIX token file)* / **DEFERRED** *(macOS Keychain `ADR-0007`)* |
| **Observability** | `src/carbon/core/logging.py`, `metrics.py` | Cross-cutting | Structured logging with secret/token redaction + in-memory `MetricsRegistry` (`GET /api/v1/metrics`) | `logging` | **IMPLEMENTED** |
| **Interfaces & Models** | `src/carbon/interfaces/protocols.py`, `models.py` | Cross-cutting | `@runtime_checkable` protocols and Pydantic/enum contracts | `pydantic` | **IMPLEMENTED** |
| **Voice Subsystem** | `src/carbon/voice/` (empty directory, 0 files) | Unbuilt | Speech-to-text (`STT`) and text-to-speech (`TTS`) | — | **DEFERRED** *(Deferred in Stage 6.6; empty directory)* |

### Swift / macOS Client Components (`apps/macos/Carbon/Sources/`)

| Component | Source Location | Layer | Responsibility | Key Dependencies | Status |
| --- | --- | --- | --- | --- | --- |
| **App Entry & Lifecycle** | `CarbonApp.swift` | Layer 1 | SwiftUI entry (`swift run` executable), initializes `ProcessSupervisor`, `SingleInstanceLock`, and `MemoryPressureMonitor`; drives startup and clean termination | `ProcessSupervisor`, `SingleInstanceLock`, `MemoryPressureMonitor`, `CarbonClient` | **IMPLEMENTED** *(macOS UI verified as development SPM target; signed `.app` bundle & menu bar utility remain **PLANNED**)* |
| **Chat & Approval UI** | `ContentView.swift`, `MarkdownRenderer.swift` | Layer 1 | Chat stream view, reasoning depth selector, startup overlay, inline HITL approval banners, token-aware Markdown rendering | `CarbonClient`, `ProcessSupervisor` | **IMPLEMENTED** |
| **Backend IPC Client** | `CarbonClient.swift` | Layer 1 | Auth token reading, `URLSessionWebSocketTask` `/ws/chat` streaming, REST calls (approvals, cancel, system status, memory pressure) | Backend `127.0.0.1:8765` | **IMPLEMENTED** |
| **Process Supervisor** | `ProcessSupervisor.swift` | Layer 1 | Discovers/spawns Ollama and Python backend, checks health, manages 5-phase shutdown with `8.0s` global watchdog (`ShutdownTimeoutConfig`) | Local `ollama`, `.venv`/Python, `CarbonClient` | **IMPLEMENTED** |
| **Single-Instance Lock** | `SingleInstanceLock.swift` | Layer 1 | Exclusive `flock(2)` on `~/.carbon/carbon.lock` preventing duplicate macOS app launches | POSIX `flock` | **IMPLEMENTED** |
| **Mach Pressure Monitor** | `MemoryPressureMonitor.swift` | Layer 1 | Watches Darwin `DispatchSource.MemoryPressureEvent` (`normal=1`, `warning=2`, `critical=4`) with `0.5s` escalation debounce and `10.0s` recovery hysteresis | `DispatchSource`, `CarbonClient` | **IMPLEMENTED** *(Unit/isolation verified; kernel-triggered end-to-end tests are documented in [Dynamic Memory Pressure Test Results](./dynamic-memory-pressure-test-results.md))* |

### Cross-Cutting Deferred / Planned Capabilities

- **macOS Keychain Secret Storage (`ADR-0007`)**: **DEFERRED** — Carbon currently uses `CARBON_API_TOKEN` or a `0600` `.carbon_token` file (see [Keychain Implementation Status](./keychain-implementation-status.md)).
- **Voice Input/Output (STT/TTS)**: **DEFERRED** — Stage 6.6 closed as deferred; `src/carbon/voice/` contains zero files.
- **Distributed / Android Nodes**: **PLANNED** — `DeviceManager._connected_nodes` is an empty list.
- **Signed `.app` Distribution & Menu Bar UI**: **PLANNED** — Current client builds and runs via Swift Package Manager (`swift run` / `Package.swift`).
- **General Multi-Step Agent Tool Loop & Embeddings**: **PLANNED** — `ToolRegistry.get_schemas_for_llm()` is not wired into `CarbonCore` inference, and memory search uses SQLite FTS5 + `LIKE` rather than vector embeddings (`IEmbeddingEngine` does not exist in `protocols.py`).

---

## 4. Related Architecture Documents

- [System Design](./system-design.md) — Layer architecture, module dependencies, and verified boundaries
- [Data Flow](./data-flow.md) — Request, `/ws/chat`, HITL approval, CMS switch, memory pressure, and lifecycle flows
- [Database Schema](./database-schema.md) — SQLite tables, indexes, triggers, WAL behavior, v1–v3 migrations, and v2 vs. v3 DB state
- [Component Contracts](./component-contracts.md) — Architectural contracts between subsystems and client-backend IPC
- [CMS Lifecycle and State Machine](./cms-lifecycle-and-state-machine.md) — CMS state transitions and invariants
- [Failure Recovery and Shutdown](./failure-recovery-and-shutdown.md) — Recovery paths and multi-phase shutdown architecture
- [Setup Guide](../development/setup.md) & [Testing Guide](../development/testing.md) — Development setup and verification instructions
