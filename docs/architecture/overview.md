# Carbon Architecture Overview

## 1. Architectural Philosophy

Carbon 1.0.0 is architected as a **modular monolith** optimized for local execution on Apple Silicon macOS hardware with 16 GB of unified memory. The system enforces strict separation of concerns across five cohesive layers while avoiding the operational latency, serialization penalties, and failure domains of distributed microservices.

```
+-------------------------------------------------------------------------+
| Layer 1: Native macOS Interface (Swift / SwiftUI)                       |
|   - Native Desktop Window & Menu Bar      - Audio Capture & Playback    |
|   - Task & Conversation Stream View       - Permission & Confirmation   |
+-------------------------------------------------------------------------+
                                    ▲
                         Loopback HTTP / WS (127.0.0.1)
                                    ▼
+-------------------------------------------------------------------------+
| Layer 2: Carbon Core (Python 3.12+ Orchestrator)                        |
|   - System Lifecycle & Event Loop         - Task Scheduler & State      |
|   - Subsystem Coordinator                 - Concurrency & Error Shield  |
+-------------------------------------------------------------------------+
       │                             │                             │
       ▼                             ▼                             ▼
+---------------+             +---------------+             +---------------+
| Layer 3:      |             | Layer 4:      |             | Layer 4:      |
| Intelligence  |             | Memory Engine |             | Governance    |
| & Model Plane |             | (Tiered Store)|             | & Tool Exec   |
| - Intent      |             | - Working     |             | - P0-P4 Rules |
| - Complexity  |             | - Episodic    |             | - A0-A3 Modes |
| - CMS Router  |             | - Semantic    |             | - Tool Schema |
| - Ollama Host |             | - Procedural  |             | - Audit Log   |
+---------------+             +---------------+             +---------------+
       │                             │                             │
       └─────────────────────────────┼─────────────────────────────┘
                                     ▼
+-------------------------------------------------------------------------+
| Layer 5: Infrastructure & Platform Abstractions                         |
|   - Ollama Local Provider (Qwen 3.5 9B)   - SQLite Async Storage        |
|   - macOS System Framework Integrations   - Local Audit Logging Engine  |
+-------------------------------------------------------------------------+
```

---

## 2. Five Architectural Layers

### Layer 1: User Interface (Native macOS)
- **Technology:** Swift 6+ with SwiftUI.
- **Responsibilities:**
  - Rendering conversational UI, markdown output, reasoning activity indicators.
  - Native microphone audio capture and system audio playback.
  - User authorization dialogs for P2–P4 operations.
  - Operating system lifecycle integration (sleep/wake, menu bar, notifications).
- **Communication:** Communicates with the backend over authenticated loopback HTTP and WebSocket connections (`127.0.0.1:8765`).

### Layer 2: Carbon Core
- **Technology:** Python 3.12+ asynchronous event loop (`asyncio`).
- **Responsibilities:**
  - Final system authority and event dispatching.
  - Task state machine (Created -> Planned -> Authorized -> Executing -> Completed / Failed / Cancelled).
  - Clean startup, graceful shutdown, signal handling (`SIGINT`, `SIGTERM`).
  - Coordination between Intelligence, Memory, Governance, and Device subsystems.

### Layer 3: Intelligence & Model Control (CMS)
- **Subcomponents:**
  - **Intelligence Engine:** Intent classification, task complexity estimation, reasoning depth selection (**Instant**, **Standard**, **Deep**), prompt formatting, model response validation.
  - **Carbon Model Supervisor (CMS):** Sole model authority. Manages model registry, capability metadata, health probes, automated selection, fallback cascades, and single-chat-model lifecycle enforcement.
  - **Model Runtime Provider:** Pluggable provider abstraction interfacing directly with the local Ollama daemon (`qwen3.5:9b`).

### Layer 4: Functional Engines
- **Memory Engine:** SQLite-backed four-tier memory management (Working, Episodic, Semantic, Procedural) with time-decay and explicit forgetting controls.
- **Voice Engine:** Pluggable Speech-to-Text (STT) and Text-to-Speech (TTS) pipeline supporting interruptible streaming audio.
- **Governance Engine:** Strict permission evaluation (P0–P4) and autonomy tier enforcement (A0–A3). Validates all tool invocation requests against user consent.
- **Device Engine:** Mac local host device manager with future-ready abstractions for authenticated Android node pairings.

### Layer 5: Infrastructure & Storage
- **Local AI Provider:** Ollama local daemon hosting verified GGUF weights.
- **Persistence:** SQLite with Write-Ahead Logging (`WAL` mode) for concurrent reads and crash resilience.
- **Logging & Auditing:** Tamper-resistant append-only structured audit logs (`JSONL`) recording all permission grants, tool executions, and system state transitions.

---

## 3. Core Architectural Invariants

1. **CMS Sole Authority:** Individual models and prompts have zero authority to select models, unload weights, or invoke system tools directly.
2. **Single Active Chat LLM Concurrency:** To safeguard against Out-Of-Memory (OOM) conditions and system swap thrashing on 16 GB Apple Silicon unified memory, exactly one chat LLM may hold active inference resources at any point.
3. **No Unrestricted System Access:** Carbon never passes raw shell commands or raw file access to an LLM. Tools must have strictly validated Pydantic schemas, clear permission classifications, and human-in-the-loop gates for consequential side-effects.
4. **Local Loopback Isolation:** Network interfaces bind strictly to `127.0.0.1`. No public inbound ports or unsolicited outbound cloud transmissions.
