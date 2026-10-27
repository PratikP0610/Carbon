# CARBON 1.0.0 — PHASE 0 ARCHITECTURE & ENVIRONMENT VALIDATION REPORT

**Document Version:** 1.0.0  
**Phase:** Phase 0 — Product and Architecture Foundation  
**Target Hardware:** Apple MacBook Air M5 (Apple Silicon ARM64, 16 GB Unified Memory)  
**Author:** Google Antigravity (Primary Development Agent)  
**Status:** Submitted for Architecture Review & Phase 1 Approval  

---

## Executive Summary

Phase 0 inspection and architecture formulation for **Carbon 1.0.0** is complete. The target Apple Silicon host environment has been thoroughly inspected without intrusive changes, verifying hardware, memory headroom, developer toolchains, local model runtimes, and inference latencies.

All core assumptions of the Product Requirements Document (PRD) have been validated against active machine telemetry. The proposed modular monolith architecture, model governance framework (CMS), single-chat-model concurrency invariant, tiered memory system, and security containment boundaries are technically feasible and optimized for 16 GB Apple Silicon hardware.

Application coding (Phase 1) remains paused pending formal user review and approval of this specification.

---

## 1. Environment & Hardware Inspection Telemetry

### 1.1 Hardware & Operating System
- **Host Architecture:** Apple Silicon ARM64 (`arm64`)
- **CPU:** Apple Silicon 10-core SoC (`hw.ncpu: 10`)
- **Operating System:** macOS 27.0.1 (Build 26A434, Darwin Kernel)
- **Total Unified Memory:** 17,179,869,184 bytes (**16.0 GB Unified Memory**)
- **Storage Subsystem:**
  - Filesystem: APFS (`/dev/disk3s1s1`) mounted on `/`
  - Total Capacity: 460 GiB
  - Consumed: 13 GiB (4% utilization)
  - **Available Headroom:** **333 GiB** available for models, databases, logs, and build artifacts.
- **Memory Pressure State (Clean Baseline):**
  - Free Pages: 32,060 (~525 MB immediately unallocated)
  - Inactive Pages: 328,131 (~5.37 GB readily reclaimable memory)
  - Active Pages: 370,915 (~6.07 GB in active use)
  - Wired Pages: 132,679 (~2.17 GB system wired)
  - Purgeable Pages: 9,956 (~163 MB)
  - Reclaimable Memory Headroom: **~5.90 GB**, providing safe operational margins for resident model weights.

### 1.2 Developer Toolchains
- **Version Control:** `git version 2.54.0 (Apple Git-157)` — Active Git repository initialized in `/Users/pratikpradeshi/Carbon`.
- **Python Environments:**
  - Primary Development Python: `/opt/homebrew/bin/python3.12` (**Python 3.12.14**) — Selected for production stability, broad wheel availability, and type system maturity.
  - Secondary / System Python: `/opt/homebrew/bin/python3` (**Python 3.14.7**).
  - Virtual Environment: `.venv` configured on Python 3.12.
- **Native macOS Toolchain:**
  - Swift Compiler: `Apple Swift version 6.4 (swiftlang-6.4.0.34.1 clang-2100.3.34.1)`
  - Target: `arm64-apple-macosx27.0.0`
  - Developer Tools: `/Library/Developer/CommandLineTools` active and verified.

### 1.3 Local AI Runtime & Primary Model Inspection
- **Runtime:** Ollama Daemon v0.33.0 verified active on loopback `http://127.0.0.1:11434`.
- **Installed Primary Model:** `qwen3.5:9b` (Exact tag verified via `/api/tags`).
- **Model Digest:** `sha256:56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90`.
- **Model Architecture & Details:**
  - Family: `qwen35`
  - Parameter Size: 9.0B parameters
  - Quantization: `Q4_K_M`
  - Format: GGUF (`model.gguf` + `mmproj.gguf` vision projector)
  - Context Window: 262,144 tokens (256K native context)
  - Disk Footprint: 6,550,825,550 bytes (**~6.55 GB**)
  - Advertised Capabilities: `completion`, `tools`, `thinking`, `vision`.
- **Model Exclusions:** Llama 3.1 8B is **not** present, strictly adhering to PRD Section 2.2.
- **Measured Empirical Performance on Apple Silicon:**
  - Cold Model Load Time (Disk to GPU Unified Memory): **5.22 seconds**.
  - Warm Model Load Time (Resident in GPU Memory): **4.08 milliseconds**.
  - Prompt Processing Rate: **50.1 tokens/second**.
  - Token Generation Rate: **22.2 tokens/second**.
  - Warm Time-To-First-Token (TTFT): **~240 milliseconds** (Exceeds PRD target of P95 ≤ 2.0s).

---

## 2. Final Proposed Technology Stack

| Layer | Selected Technology | Version / Specification | Rationale & Compatibility |
|---|---|---|---|
| **Native Frontend UI** | **Swift + SwiftUI** | Swift 6.4 / macOS 14+ SDK | Apple-native UI responsiveness, menu-bar integration, low-overhead native audio capture (`AVFoundation`), macOS Keychain services, and native notification permissions. |
| **Backend & Orchestrator** | **Python** | Python 3.12.14 | Excellent ecosystem support for async orchestration (`asyncio`), Pydantic data modeling, and typed interfaces without Python 3.14 wheel instability. |
| **Local Application API** | **FastAPI + Uvicorn** | FastAPI ≥ 0.115, Uvicorn ≥ 0.30 | High-performance asynchronous REST & WebSocket framework; strictly bound to loopback `127.0.0.1:8765`. |
| **Data Contracts & Validation** | **Pydantic v2** | Pydantic ≥ 2.9, Pydantic-Settings | Rust-backed validation (`pydantic-core`), strict serialization, and schema generation for LLM tool calling. |
| **Local AI Provider** | **Ollama Local Daemon** | Ollama ≥ 0.33.0 | Manages Metal-accelerated inference for GGUF weights on Apple Silicon; supports dynamic loading/unloading. |
| **Primary Chat Model** | **Qwen 3.5 9B** | Q4_K_M GGUF (6.55 GB) | Primary conversational, reasoning, tool-using, and thinking model. Fits comfortably in 16 GB memory pool. |
| **Persistence Database** | **SQLite (WAL Mode)** | SQLite 3.43+ via `aiosqlite` | Zero-configuration, zero-daemon, ACID-compliant local database. Configured with Write-Ahead Logging (`WAL`) and full-text search (`FTS5`). |
| **Testing & Quality Assurance** | **pytest + ruff + pyright** | pytest 8.3, ruff 0.6 | Fast linting, formatting, type checking, and automated async test harnesses. |

---

## 3. Complete System Architecture

Carbon is engineered as a **Modular Monolith** containing five clearly demarcated layers:

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 1: Native macOS Interface (Swift 6 / SwiftUI)                     │
│  - Conversational Canvas & Stream Views   - Native Audio Capture / TTS  │
│  - P2-P4 Permission Dialog Modals         - Menu Bar Status / Hotkeys   │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ Loopback IPC (REST + WebSocket)
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 2: Carbon Core (Python 3.12+ Orchestrator)                        │
│  - Application Lifecycle & Signal Traps   - Task State Machine Scheduler│
│  - Event Bus & Concurrency Shield         - Subsystem Coordination Hub  │
└───────┬────────────────────────────┼────────────────────────────┬───────┘
        │                            │                            │
        ▼                            ▼                            ▼
┌───────────────────────┐   ┌───────────────────────┐   ┌─────────────────┐
│ Layer 3: Intelligence │   │ Layer 4: Memory Engine│   │ Layer 4:        │
│ & Model Control Plane │   │  - Working Memory     │   │ Governance &    │
│  - Intent Classifier  │   │  - Episodic Memory    │   │ Tool Execution  │
│  - Complexity Estimator│  │  - Semantic Memory    │   │  - P0-P4 Rules  │
│  - CMS Authority      │   │  - Procedural Memory  │   │  - A0-A3 Modes  │
│  - Concurrency Mutex  │   │  - FTS5 Keyword Search│   │  - Tool Registry│
│  - Switching Engine   │   │  - Privacy Forgetting │   │  - Audit Logger │
└───────┬───────────────┘   └───────────┬───────────┘   └────────┬────────┘
        │                               │                        │
        └───────────────────────────────┼────────────────────────┘
                                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 5: Infrastructure & Platform Abstractions                         │
│  - Ollama HTTP Client (127.0.0.1:11434)   - aiosqlite Async Driver      │
│  - macOS Frameworks (sysctl, open -a)     - Append-Only Audit Log File  │
└─────────────────────────────────────────────────────────────────────────┘
```

### Architectural Invariants:
1. **Carbon Core Retains Final Authority:** Subsystems report to and are scheduled by Core; no worker component can trigger system-wide actions uncoordinated.
2. **CMS Sole Model Authority:** Individual LLMs are workers with zero administrative privileges. Models cannot select models, change leases, or access system tools directly.
3. **Single Active Chat LLM Concurrency:** To protect 16 GB Apple Silicon unified memory from swap thrashing and OOM events, exactly one chat LLM may hold an active inference lease at any time.
4. **Controlled Autonomy & Sandboxed Tools:** LLMs propose structured tool calls; the Governance Engine authorizes them; Carbon Core executes them in isolated async tasks with strict timeouts.
5. **Strict Network Loopback Isolation:** API binds exclusively to `127.0.0.1` and uses ephemeral token authentication.

---

## 4. Carbon Core and CMS Architecture

### 4.1 Carbon Core Orchestration & Task Lifecycle
Carbon Core models every user instruction as a managed **Task** adhering to a deterministic finite state machine:

```
[PENDING] ──► [ANALYZING] ──► [AWAITING_APPROVAL] ──► [EXECUTING] ──► [STREAMING] ──► [COMPLETED]
     │              │                  │                   │                          │
     └──────────────┴──────────────────┴───────────────────┴──────────────────────────┴─► [CANCELLED / FAILED]
```

- **Task Cancellation:** Tasks maintain cancellation tokens. If the user cancels or issues a voice barge-in, active model generation and tool execution are aborted instantly.
- **Graceful Lifecycle Management:** Core intercepts `SIGINT` and `SIGTERM`, flushes SQLite WAL buffers, releases model leases, terminates background tasks, and closes loopback sockets cleanly.

### 4.2 Carbon Model Supervisor (CMS)
CMS is the central control plane for all model operations:
- **Model Registry:** Catalogs all verified local models with parameter size, quantization, memory footprint, context window, and capability flags (`completion`, `chat`, `tools`, `thinking`, `vision`, `embedding`).
- **Single-Model Mutex (`asyncio.Lock`):** Serializes chat inference leases. If Task B requests inference while Task A is executing, Task B queues cleanly behind the lock or receives a resource-busy notice.
- **Controlled Switching Protocol:**
  1. Core requests model with required capabilities.
  2. If active model matches, lease is granted immediately.
  3. If a switch is needed, CMS acquires the exclusive switch lock.
  4. CMS commands Ollama to unload the current model (`keep_alive: 0`), releasing unified memory.
  5. CMS issues a warm-up probe to the target model.
  6. Upon target readiness, CMS updates the active registry state and releases the switch lock.
  7. If target fails, CMS rolls back to `qwen3.5:9b`.

---

## 5. Model Inventory & Evaluation Strategy

### 5.1 Initial Model Inventory

| Model Identifier | Provider / Engine | Role | Footprint (Disk / RAM) | Context Window | Status |
|---|---|---|---|---|---|
| **`qwen3.5:9b`** | Ollama / llama.cpp | Primary Chat & Reasoning LLM | 6.55 GB / ~6.2 GB unified RAM | 262,144 tokens | **Verified & Installed** |
| **`whisper.cpp`** *(Planned)* | C++ native / CoreML | Local Speech-to-Text (STT) | ~150 MB (base.en) to ~500 MB (small.en) | Audio chunks | Candidate for Phase 5 |
| **`kokoro`** *(Planned)* | ONNX / Python native | Local Text-to-Speech (TTS) | ~350 MB RAM | Text buffers | Candidate for Phase 5 |
| **Local Embedder** *(Planned)* | Ollama / ONNX (`nomic-embed-text` or `bge-small`) | Semantic Memory Retrieval | ~300 MB RAM | 8,192 tokens | Candidate for Phase 4 |

### 5.2 Model Governance Rules
1. **Zero Unapproved Installations:** No models will be downloaded, pulled, or installed without explicit user authorization.
2. **Hardware Budget Constraint:** Total resident model footprint must never exceed **9.0 GB** on the 16 GB MacBook Air, leaving ≥ 7.0 GB for macOS system services and applications.
3. **Specialist Model Isolation:** STT, TTS, and embedding models run in separate isolated memory budgets and do not hold the chat LLM inference lock.
4. **Empirical Benchmarking Protocol:** Every model must be evaluated for:
   - Cold load latency (disk -> Metal).
   - Time to first token (TTFT).
   - Generation throughput (tokens/second).
   - Tool calling JSON reliability (schema adherence).

---

## 6. Component Communication Contracts

Subsystems communicate via typed interfaces and Pydantic models:

```python
# Key Enumerations
class ReasoningDepth(str, Enum):
    INSTANT = "instant"  # Direct conversational/factual tasks (< 1.0s TTFT)
    STANDARD = "standard"  # Balanced explanations, comparisons, ordinary planning
    DEEP = "deep"  # Complex analysis, multi-step problem solving, explicit thinking


class PermissionLevel(str, Enum):
    P0 = "P0"  # Conversational / read-only (no side-effects)
    P1 = "P1"  # Read approved system state (metrics, window titles)
    P2 = "P2"  # Reversible actions (app launching, clipboard)
    P3 = (
        "P3"  # Consequential modifications (file write, calendar, network) - User Approval Required
    )
    P4 = "P4"  # Sensitive / destructive operations (file deletion, credentials) - Double Confirmation


class AutonomyLevel(str, Enum):
    A0 = "A0"  # Observe only
    A1 = "A1"  # Assist and recommend (default)
    A2 = "A2"  # Auto-execute P0-P2; prompt for P3+
    A3 = "A3"  # Bounded autonomy within predefined guardrails
```

### Protocol Interfaces:
- `ICarbonModelSupervisor`: Manages model selection, switching, registry, and inference lease allocation.
- `IModelProvider`: Abstracts runtime provider interactions (`generate_stream`, `chat_stream`, `load_model`, `unload_model`).
- `IIntelligenceEngine`: Classifies intent, determines reasoning depth, plans task steps, and validates model output.
- `IMemoryEngine`: Manages four-tier memory store, FTS retrieval, and user-directed forgetting.
- `IGovernanceEngine`: Evaluates tool authorizations against autonomy policies and records immutable audit records.
- `ITool` & `ToolRegistry`: Validates inputs, executes tools with timeout isolation, and returns structured `ToolExecutionResult`.

---

## 7. Database Architecture

Carbon uses local **SQLite** with Write-Ahead Logging (`WAL` mode) at `data/carbon.db`.

### 7.1 Schema Definitions
```sql
-- Core Memory Records (Partitioned across Working, Episodic, Semantic, Procedural)
CREATE TABLE IF NOT EXISTS memory_records (
    id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,         -- 'working', 'episodic', 'semantic', 'procedural'
    key TEXT NOT NULL,                 -- Lookup key or semantic tag
    content TEXT NOT NULL,             -- Payload
    metadata TEXT NOT NULL,            -- JSON object
    source TEXT NOT NULL,              -- Originating interaction
    confidence REAL NOT NULL,          -- Relevance/confidence score (0.0 - 1.0)
    created_at TEXT NOT NULL,          -- ISO-8601 UTC timestamp
    updated_at TEXT NOT NULL,          -- ISO-8601 UTC timestamp
    expires_at TEXT                    -- Optional expiration timestamp
);

-- Fast Indexing
CREATE INDEX IF NOT EXISTS idx_memory_type_key ON memory_records(memory_type, key);
CREATE INDEX IF NOT EXISTS idx_memory_created_at ON memory_records(created_at);

-- Full-Text Search (FTS5) Virtual Table
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    id UNINDEXED,
    key,
    content,
    content='memory_records',
    content_rowid='rowid'
);

-- Triggers for Automatic FTS Synchronization
CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory_records BEGIN
    INSERT INTO memory_fts(rowid, id, key, content) VALUES (new.rowid, new.id, new.key, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory_records BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, id, key, content) VALUES('delete', old.rowid, old.id, old.key, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory_records BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, id, key, content) VALUES('delete', old.rowid, old.id, old.key, old.content);
    INSERT INTO memory_fts(rowid, id, key, content) VALUES (new.rowid, new.id, new.key, new.content);
END;
```

### 7.2 Database Pragmas for High Performance & Safety:
- `PRAGMA journal_mode = WAL;` (Concurrent reads while writing; crash resilient)
- `PRAGMA synchronous = NORMAL;` (Maximum I/O performance on Apple Silicon NVMe SSD)
- `PRAGMA foreign_keys = ON;` (Referential integrity enforcement)

---

## 8. Security & Permission Architecture

```
════════════════════════════════════════════════════════════════════════════
[PERIMETER 1: Network Isolation]
- API binds strictly to 127.0.0.1:8765 (Loopback IPv4 only)
- Ephemeral bearer token authentication (mode 0600 on runtime token file)
- Zero external listening ports; zero unsolicited outbound telemetry
════════════════════════════════════════════════════════════════════════════
                                  │
                                  ▼
════════════════════════════════════════════════════════════════════════════
[PERIMETER 2: Model Containment]
- LLM is an unprivileged worker; has zero direct filesystem, shell, or network access
- LLM requests actions strictly via validated JSON schemas
════════════════════════════════════════════════════════════════════════════
                                  │
                                  ▼
════════════════════════════════════════════════════════════════════════════
[PERIMETER 3: Governance & Authorization Gate]
- P0: Auto-approved in all autonomy levels
- P1: Auto-approved in A1-A3
- P2: Auto-approved in A2/A3; single-click in A1
- P3: Mandatory User Confirmation Modal (Arguments & Side-effects displayed)
- P4: Mandatory High-Impact Confirmation or Hard Prohibition
════════════════════════════════════════════════════════════════════════════
                                  │
                                  ▼
════════════════════════════════════════════════════════════════════════════
[PERIMETER 4: Audit Accountability]
- Append-only structured log (data/audit.log)
- Records timestamp, event_type, authorization outcome, parameters, and duration
════════════════════════════════════════════════════════════════════════════
```

---

## 9. Repository Structure

```
carbon/
├── README.md                          # Project overview and entry point
├── LICENSE                            # MIT License
├── pyproject.toml                     # Packaging, dependencies, and toolchain config
├── .gitignore                         # Comprehensive macOS, Python, Swift exclusions
├── .env.example                       # Local environment variables template
│
├── docs/                              # Formal Architecture & Product Documentation
│   ├── product/PRD.md                 # Authoritative Product Requirements Document
│   ├── architecture/
│   │   ├── overview.md                # System architectural philosophy & layers
│   │   ├── system-design.md           # Subsystem topology and execution states
│   │   ├── component-contracts.md     # Typed class protocols & schema definitions
│   │   ├── data-flow.md               # End-to-end sequences & lifecycle flow
│   │   ├── phase-0-validation-report.md # This validation report
│   │   └── adr/                       # Architecture Decision Records
│   │       ├── 0001-modular-monolith-architecture.md
│   │       ├── 0002-carbon-model-supervisor-authority.md
│   │       ├── 0003-single-active-chat-concurrency.md
│   │       ├── 0004-sqlite-persistence-and-tiered-memory.md
│   │       └── 0005-loopback-api-ipc-contracts.md
│   ├── security/
│   │   ├── threat-model.md            # STRIDE threat model & attack mitigations
│   │   ├── permissions.md             # Permission matrix (P0-P4) & autonomy levels
│   │   └── privacy.md                 # Privacy guarantees & forgetting policies
│   ├── development/
│   │   ├── setup.md                   # Environment setup guide
│   │   ├── testing.md                 # QA testing methodology
│   │   └── contributing.md            # Architecture contribution guidelines
│   └── benchmarks/
│       └── methodology.md             # Latency & resource benchmark protocol
│
├── apps/
│   └── macos/Carbon/                  # Native SwiftUI macOS desktop client
│
├── src/carbon/                        # Backend Modular Monolith Package
│   ├── main.py                        # Application entry point & FastAPI factory
│   ├── config/                        # Settings & loopback security validation
│   ├── core/                          # Orchestrator, task state machine, lifecycle
│   ├── intelligence/                  # Intent classification & complexity estimation
│   ├── model_control/                 # CMS, model registry, Ollama provider
│   ├── memory/                        # SQLite four-tier memory & FTS engine
│   ├── voice/                         # STT / TTS pipeline interfaces
│   ├── governance/                    # Permissions, autonomy constraints, audit logs
│   ├── devices/                       # Local Mac host and future Android node stubs
│   ├── tools/                         # Validated tool execution framework & builtins
│   └── interfaces/                    # Shared models and typing protocols
│
├── tests/                             # Automated Test Suites
│   ├── unit/                          # Subsystem unit tests
│   ├── integration/                   # Provider and IPC integration tests
│   ├── security/                      # Permission boundary and injection tests
│   └── performance/                   # Latency and memory benchmarks
│
├── benchmarks/                        # Dedicated benchmark harnesses
├── scripts/
│   ├── diagnostics.sh                 # Environment diagnostics verification script
│   └── benchmark.sh                   # Performance measurement runner
└── data/                              # Local runtime database and audit logs (git-ignored)
```

---

## 10. Development Roadmap

- **Phase 0 — Product and Architecture Foundation:** *(Current Phase — Complete & Awaiting Approval)*
- **Phase 1 — Carbon Core & Local API:** Implement Core orchestrator, task state machine, local loopback FastAPI REST/WebSocket endpoints, diagnostics logging, and basic native Mac client shell.
- **Phase 2 — Model Control Plane (CMS):** Connect Ollama provider client, model registry, automated selection, single-model concurrency locks, and switching engine.
- **Phase 3 — Intelligence Engine:** Implement intent classification, complexity assessment, adaptive reasoning routing (**Instant**, **Standard**, **Deep**), prompt formatting, and output validation.
- **Phase 4 — Persistent Memory Engine:** Implement SQLite persistence, tiered storage (Working, Episodic, Semantic, Procedural), FTS5 full-text search, and user-directed forgetting.
- **Phase 5 — Voice Engine:** Benchmark and integrate local STT (`whisper.cpp`) and local TTS (`kokoro`), audio capture pipeline, and interruption handling.
- **Phase 6 — Governance & Controlled System Access:** Implement permission evaluator (P0–P4), tool execution runner, approval dialog protocol, and append-only audit logging.
- **Phase 7 — Controlled Autonomy:** Implement multi-step task planning, bounded execution loops, and rollback checkpoints.
- **Phase 8 — Performance Optimization:** Fine-tune TTFT, memory footprints, model switching latencies, and run 60-minute stability stress tests.
- **Phase 9 — Android Node Preparation:** Define authenticated pairing protocol, encrypted transport, and capability negotiation for Samsung Galaxy S23 FE.
- **Phase 10 — Release Candidate Verification:** End-to-end integration tests, security audit, user documentation, and release packaging.

---

## 11. Identified Technical Risks & Mitigations

| Risk Description | Severity | Probability | Impact | Mitigation Strategy |
|---|---|---|---|---|
| **Unified Memory Pressure (16 GB Mac)** | High | Medium | Potential swap thrashing or OOM if multiple models load concurrently. | Strictly enforce Single Active Chat LLM Concurrency via CMS `asyncio.Lock`. Verify memory headroom before pre-warming target models. |
| **Model Cold Start Delay (5.2s)** | Medium | High | First request to an unloaded model incurs a ~5.2s cold-load delay. | CMS keeps primary model pre-warmed using Ollama's `keep_alive` parameter (default 5m/persistent). Fast-path simple tasks bypass heavy reasoning. |
| **Indirect Prompt Injection** | High | Medium | Malicious text in user documents instructing LLM to execute destructive commands. | Dual-boundary defense: LLM cannot invoke tools directly; all actions pass through Governance Engine. P3/P4 operations enforce explicit user confirmation with full parameter inspection. |
| **Loopback Socket Port Conflicts** | Low | Low | Port 8765 occupied by another local service. | Make port configurable via `.env` (`CARBON_API_PORT`), with automatic fallback detection to the next available loopback port if needed. |
| **Heavy Voice Pipeline Overhead** | Medium | Medium | Simultaneous STT/TTS execution competing with LLM for CPU/GPU cycles. | Decouple STT/TTS workloads; utilize Apple Silicon Neural Engine (CoreML) or quantized models (`base.en` / ONNX) to prevent Metal GPU contention. |

---

## 12. Conclusion & Request for Phase 1 Approval

Phase 0 has confirmed that the MacBook Air M5 development environment is fully operational, healthy, and exceptionally well-suited for Carbon 1.0.0. The architecture is validated, verified against real hardware telemetry, and ready for execution.

**Antigravity requests the User's formal review and approval of this Phase 0 Architecture Report to begin Phase 1 (Carbon Core & Local API implementation).**
