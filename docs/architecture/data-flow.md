# Carbon Data Flow

**Stage 7.3 Alignment Status:** Aligned to the verified repository implementation.
**Scope Note:** All flows below are traced directly from `apps/macos/Carbon/Sources/` and `src/carbon/` (`main.py`, `orchestrator.py`, `cms.py`, `sqlite_memory.py`, `governance_engine.py`, `tools/base.py`). Flows or capabilities that do not exist in code (such as general LLM-driven tool loops or voice pipelines) are explicitly labeled rather than assumed.

---

## 1. Real-Time Chat & Streaming Inference Flow (`/ws/chat`)

This is the primary user interaction path when submitting a prompt from the SwiftUI client (or any authenticated local WebSocket client).

```text
SwiftUI (ContentView / CarbonClient)          FastAPI (/ws/chat)             CarbonCore + CMS + Ollama
------------------------------------          ------------------             -------------------------
1. Connect ws://127.0.0.1:8765/ws/chat -----> Origin check + conn cap (<=5)
2. Send {"action":"auth","token":...} ------> Verify within 3.0s timeout
   (or Authorization: Bearer at upgrade)      Reply {"event":"authenticated"}
3. Send {"action":"submit", ...} -----------> Create conversation if needed
                                              Spawn background task --------> Persist task row (QUEUED)
                                                                              classify_intent() (regex/rules)
                                                                              Recall semantic/episodic memories
                                                                              + recent conversation turns
                                                                              (marked UNTRUSTED historical data)
                                                                              CMS select_model_for_task()
                                                                              (optional model_switching + switch)
                                                                              CMS acquire_inference_lease(task_id)
                                                                              Post-lease verify + num_ctx snapshot
                                                                              State -> STREAMING
4. Render streaming tokens <----------------- Forward stream events <-------- OllamaProvider /api/generate stream
5. Display completed response <-------------- State -> COMPLETED <----------- Release lease (finally)
                                                                              Persist episodic turn + final task
```

### Key Runtime Details

1. **WebSocket Connection & Authentication (`src/carbon/main.py`)**:
   - Up to `5` concurrent `/ws/chat` connections (`MAX_CONCURRENT_WS_CONNECTIONS = 5`) are accepted.
   - Non-loopback or missing/external HTTP `Origin` headers (including `"null"`) are rejected (`403` / `1008`).
   - Authentication succeeds either from an `Authorization: Bearer <token>` upgrade header or an initial JSON message `{"action": "auth", "token": "..."}` received within `WS_AUTH_TIMEOUT_SECONDS = 3.0`. Query-string tokens are not accepted.
2. **Task Setup & Context Retrieval (`CarbonCore.process_task_stream`)**:
   - The prompt is classified locally by `IntelligenceEngine.classify_intent()` into a category, `TaskCategory`, `ReasoningDepth` (`instant`, `standard`, `deep`), and required `ModelCapability` list.
   - If not handled by a fast path (Section 3), `CarbonCore` fetches relevant saved memories (`SQLiteMemoryEngine`) and recent conversation turns (`get_recent_conversation_turns`), appending them to the system prompt with explicit instructions to treat stored content strictly as **untrusted historical reference data** that cannot grant permissions or override instructions.
3. **Pre-Lease Planning, Switching & Lease Acquisition**:
   - `CMS.select_model_for_task()` selects an approved model (default `qwen3.5:9b`) within the `9216` MB memory budget.
   - If activation or switching is required, `CarbonCore` emits `model_switching`, calls `CMS.switch_model()` **before** taking the inference lease, audits `MODEL_SWITCH_REQUESTED` / `MODEL_SWITCH_SUCCEEDED` (or `MODEL_SWITCH_FAILED`), and checks post-switch authorization.
   - `CarbonCore` yields `acquiring_model_lease` and acquires the single-model concurrency lease (`CMS.acquire_inference_lease(task_id)`).
4. **Post-Lease Verification & Streaming**:
   - While holding the lease, `CarbonCore` verifies the active authorized model (`resolve_model_for_leased_task` + `assert_model_authorized`), captures a single `effective_num_ctx` snapshot for the request, builds reasoning options, and streams provider events (`token`, completion metrics, truncation flag) until done or cancelled.
   - In `finally`, the inference lease is always released, final task state (`completed`, `failed`, `cancelled`, `rejected`) is persisted using cancellation-safe SQLite transactions, and completed assistant turns are recorded to episodic memory.

---

## 2. Task Cancellation & WebSocket Disconnect Flow

Cancellation can be triggered by the client over WebSocket (`{"action": "cancel", "task_id": "..."}`), over REST (`POST /api/v1/tasks/{task_id}/cancel`), or by a WebSocket disconnect while the task is still running.

```text
Client / Disconnect --> Task cancel_event.set()
                           |
                           +--> Mid-stream wait loop (asyncio.wait FIRST_COMPLETED on next_task vs cancel_wait)
                           |       Cancels active next_task, closes Ollama event stream (`aclosing`),
                           |       marks task CANCELLED (preserving any partial tokens if emitted),
                           |       releases CMS inference lease, persists CANCELLED task row, emits "cancelled"
                           |
                           +--> Stage 6.9-F Disconnect Guard (_cleanup_connection_task in main.py):
                                   If task ALREADY reached a terminal state (e.g. completed/failed) when
                                   the client disconnected, connection cleanup does NOT cancel the
                                   post-completion persistence coroutine, preventing interrupted commits.
```

---

## 3. Human-in-the-Loop (HITL) Approval & Fast-Path Flows

There are **two implemented HITL approval triggers** in current code:

### 3A. Chat Fast-Path: `open_application` (`P2`)

When a prompt matches `open <app>` (`SYSTEM_COMMAND` intent), `CarbonCore` **bypasses LLM inference** and runs a deterministic tool fast-path:

```text
User prompt: "open Safari"
   |
   v
CarbonCore resolves app match via OpenApplicationTool (APPROVED_APPS allowlist)
   |
   +--> Request P2 approval in GovernanceEngine (60.0s TTL, bound to tool_id + arguments SHA-256 digest)
   +--> Emit state "awaiting_approval" + event "approval_required" (approval_id, summary)
   |
   v  (waits for human decision from USER_UI or USER_API)
Decision received via WS {"action":"approve"|"deny", "approval_id":...}
   OR REST POST /api/v1/approvals/{approval_id}/decision
   |
   +--> Denied / Timed out / Untrusted source -> Task fails or denies; audit recorded; tool never runs
   +--> Approved -> Task state "executing_tool" -> ToolRegistry.execute_tool(..., approval_id=...)
                    GovernanceEngine.consume_approval() verifies unused + matching arguments + valid TTL
                    OpenApplicationTool executes (`timeout_seconds=5.0`), returns result & audit entry
```

### 3B. REST Procedural Memory Write Gate (`P3`)

Procedural memory writes (`MemoryType.PROCEDURAL`) are gated behind `P3` approval via `POST /api/v1/memory/procedural/requests` (returns `202 Accepted` with `approval_id`). Once approved via `POST /api/v1/approvals/{approval_id}/decision` (trusted `USER_API` / `USER_UI` decision source), the client commits the approved write (`status: "committed"`).

### Important Implementation Distinction (No General LLM Tool Loop Yet)

- **STATUS: PARTIALLY IMPLEMENTED / PLANNED** — Although `ToolRegistry.get_schemas_for_llm()` exists to format tool definitions as JSON schemas, `CarbonCore` does **not** pass tool schemas to `OllamaProvider.generate_stream_events()`, and does **not** parse model tool-call outputs to re-prompt the model in a multi-step agent loop. Only the deterministic `open_application` fast-path and `POST /api/v1/tools/{tool_id}/execute` invoke `ToolRegistry.execute_tool()`.

---

## 4. Persistent Memory Recall & Forgetting Flows

1. **Memory Write & Recall (`MEMORY_STORE` / `MEMORY_QUERY`)**:
   - Fast-path store prompts (e.g., `"Remember that my editor is Neovim"`) extract content and persist to `memory_records` (default `EPISODIC` / metadata tags), updating `memory_fts` automatically via SQLite `AFTER INSERT`/`UPDATE` triggers (`memory_ai`, `memory_au`).
   - Queries search `memory_fts` (`MATCH`) with sanitized terms and fall back cleanly to `LIKE` matching when FTS returns no rows or syntax sanitization skips FTS.
2. **Confirmation-Safe Memory Forgetting (`MEMORY_FORGET` / `POST /api/v1/memory/forget`)**:
   - Deletion never removes arbitrary rows blindly on a single unconfirmed phrase: matching targets are first returned for preview/confirmation (`confirm=false`), and records are deleted only after confirmation (`confirm=true`), updating `memory_fts` via `memory_ad` trigger and recording audit entries.
3. **Periodic Maintenance (`CarbonCore` maintenance loop)**:
   - Serializes expired TTL purging (`working`, `episodic`, `tasks`, and clearing payloads past `payload_expires_at` while keeping task metadata until `task_ttl_days`) and triggers best-effort SQLite `WAL` checkpointing.

---

## 5. Model Switching & Refresh Flow

1. **Manual Model Switch (`POST /api/v1/models/switch`)**:
   - Rejects unapproved models immediately (`403 Forbidden`, `ModelNotApprovedError`).
   - Checks memory budget (`9216` MB) and provider availability; waits/coordinates safely so an active leased inference task is not switched mid-stream; audits `MODEL_SWITCH_REQUESTED` -> `MODEL_SWITCH_SUCCEEDED` or `MODEL_SWITCH_FAILED`.
2. **Runtime Model Refresh (`POST /api/v1/models/refresh`)**:
   - Queries Ollama `/api/tags` and `/api/ps`; returns `409 Conflict` if an active inference lease cannot drain cleanly within the refresh drain window (`ADR-0003` single-model isolation).

---

## 6. Mach Memory Pressure Event Flow (`Swift` -> `Python CMS`)

```text
macOS Kernel (Mach DispatchSource.MemoryPressureEvent)
   |  (.normal=1, .warning=2, .critical=4; highest severity wins)
   v
MemoryPressureMonitor / MemoryPressureDebouncer (SwiftUI @MainActor)
   |  Escalation debounce: 0.5s  |  Recovery hysteresis: 10.0s
   v
CarbonClient -> POST /api/v1/system/memory-pressure (Bearer Token)
   |
   v
CarbonCore / CMS updates pressure state & dynamic context cap (`num_ctx`)
   (Captured as a stable snapshot per leased request so num_ctx does not drift mid-stream)
```

---

## 7. Startup & Multi-Phase Shutdown Flow

1. **Startup (`ProcessSupervisor` -> `CarbonCore.initialize()`)**:
   - `SingleInstanceLock` acquires `~/.carbon/carbon.lock`.
   - `ProcessSupervisor` checks `127.0.0.1:11434` (reuses healthy Ollama or spawns child Ollama), verifies `qwen3.5:9b`, checks `127.0.0.1:8765` (reuses healthy backend or spawns Python Uvicorn), and waits for backend health.
   - Python startup verifies `audit.log` SHA-256 chain tail (`AuditChainCorruptionError` fail-closed if corrupted), verifies SQLite header + `PRAGMA integrity_check` (`DatabaseIntegrityError` + `forensics/` quarantine if corrupted), applies pending schema migrations, initializes `DeviceManager`, `ToolRegistry`, and CMS discovery, and recovers stale interrupted `tasks` rows from prior crashes.
2. **Shutdown (`ProcessSupervisor.ShutdownTimeoutConfig.standard`)**:
   - Enforces a strict `8.0s` global watchdog across bounded sequential/guarded phases: cancel active inference (`1.2s`) -> close WebSocket (`0.3s`) -> request Python backend `/api/v1/system/shutdown` and wait for owned child exit (`3.0s`) -> terminate owned Ollama child only, preserving external user Ollama (`2.0s`) -> release lock cleanup (`0.3s`). See [Failure Recovery and Shutdown](./failure-recovery-and-shutdown.md) for full phase state machines.
