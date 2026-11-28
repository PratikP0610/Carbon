# Carbon Component Contracts

**Stage 7.3 Alignment Status:** Aligned strictly to `src/carbon/interfaces/protocols.py`, `src/carbon/interfaces/models.py`, `src/carbon/main.py`, and `apps/macos/Carbon/Sources/`.
**Scope Boundary:** This document specifies **architectural contracts and component boundaries** (Purpose, Inputs, Outputs, Validation/Auth, Failure Behavior, Status). Endpoint-by-endpoint HTTP/WebSocket payload schemas belong to the API reference work in Stage 7.4 and are summarized here only at the boundary level.

---

## 1. API & IPC Gateway Boundaries (`src/carbon/main.py`)

### 1.1 HTTP REST Boundary

- **Purpose:** Local-only control-plane and state inspection interface between `CarbonClient` (or authorized local tooling) and `CarbonCore`.
- **Exposed Surface (24 REST routes):**
  - Public: `GET /`, `GET /api/v1/health`
  - Protected (`verify_local_auth` Bearer token): `GET /api/v1/metrics`, `POST /api/v1/auth/rotate-token`, `GET /api/v1/models`, `POST /api/v1/models/refresh`, `POST /api/v1/models/switch`, `POST /api/v1/tasks`, `GET /api/v1/tasks/{task_id}`, `POST /api/v1/tasks/{task_id}/cancel`, `GET /api/v1/tools`, `POST /api/v1/tools/{tool_id}/execute`, `GET /api/v1/approvals`, `POST /api/v1/approvals/{approval_id}/decision`, `GET /api/v1/system/status`, `POST /api/v1/system/memory-pressure`, `POST /api/v1/system/shutdown`, `GET /api/v1/memory`, `POST /api/v1/memory/procedural/requests`, `POST /api/v1/memory`, `PATCH /api/v1/memory/{record_id}`, `DELETE /api/v1/memory/{record_id}`, `POST /api/v1/memory/forget`, `DELETE /api/v1/conversations/{conversation_id}`.
- **Validation & Auth Contract:** Loopback-only client/host middleware (`127.0.0.1` / `localhost` / `[::1]`) + `Origin` enforcement + constant-time `Authorization: Bearer` validation against `CARBON_API_TOKEN` / `.carbon_token`. Token rotation swaps `.carbon_token` (`0600`) atomically and immediately closes stale WebSocket sessions (`4401`).
- **Failure Behavior:** Invalid tokens return `401`/`403`; unapproved model switches return `403`; refresh during an active non-draining lease returns `409 Conflict`; unknown resources return `404`.
- **Status:** **IMPLEMENTED** *(Legacy `/api/v1/chat` does not exist; detailed field schemas belong to Stage 7.4).*

### 1.2 WebSocket `/ws/chat` Boundary

- **Purpose:** Full-duplex streaming session for chat submission, progressive token delivery, cancellation, and interactive HITL approvals.
- **Input Contract (Client -> Server actions):** `auth` (if not authenticated via upgrade header), `submit` (prompt, optional `conversation_id`, `reasoning_depth`, `autonomy_level`), `cancel` (`task_id`), `approve` (`approval_id`), `deny` (`approval_id`).
- **Output Contract (Server -> Client events):** `authenticated`, `task_created`, `state_change`, `model_switching`, `acquiring_model_lease`, `approval_required`, `token`, `completed`, `cancelled`, `error`, `rejected`.
- **Validation & Auth Contract:** Loopback + strict `Origin` header validation (blocks `null` and external origins with `403`/`1008`); concurrency capped at `MAX_CONCURRENT_WS_CONNECTIONS = 5`; auth required via upgrade header or initial `auth` message within `WS_AUTH_TIMEOUT_SECONDS = 3.0` (no query-string tokens).
- **Failure Behavior:** Auth timeout or invalid token closes connection with `4401`; disconnect before terminal state cancels the running task, whereas disconnect after a terminal state leaves persistence uninterrupted (`Stage 6.9-F`).
- **Status:** **IMPLEMENTED**.

---

## 2. Core Python Protocol Contracts (`src/carbon/interfaces/protocols.py`)

### 2.1 `ICarbonModelSupervisor` & `IModelProvider` (CMS Boundary)

- **Purpose:** Separates model governance, allowlist policy, `9216` MB memory budget enforcement, and concurrency leasing (`CarbonModelSupervisor`) from raw runtime transport (`OllamaProvider` / `FakeModelProvider`).
- **Input / Output Contract:**
  - `IModelProvider`: health probe (`check_health() -> HealthProbeResult`), runtime model discovery (`discover_models() -> RuntimeDiscoverySnapshot`), lifecycle operations (`ensure_model_ready`, `load_model`, `unload_model`), streaming chat (`generate_stream_events(model_id, prompt, system, think, options)`), plus `set_approved_models()` / `is_model_approved()`.
  - `ICarbonModelSupervisor`: `select_model_for_task(capabilities_required, reasoning, task_category) -> ModelMetadata`, `switch_model(ModelSwitchRequest) -> bool`, `get_active_model()`, `list_models()`, `get_memory_budget_mb() -> int`, `get_available_memory_mb()`, `get_current_memory_usage_mb()`. Concrete `CarbonModelSupervisor` also enforces `assert_model_authorized()`, `acquire_inference_lease()`, `resolve_model_for_leased_task()`, `refresh_models()`, and pressure-scaled `num_ctx`.
- **Validation & Failure Behavior:** Raises `ModelNotApprovedError` (non-allowlisted model), `ModelUnavailableError` (offline runtime, load failure, or insufficient budget within `9216` MB), `ModelIncompatibleError`, `ResourcePolicyError`, or `ProviderInferenceError` (`InferenceFailureKind`: `timeout`, `connect_error`, `stream_interrupted`, `empty_response`, `truncated`, etc.). Never falls back to an unapproved model.
- **Status:** **IMPLEMENTED**.

### 2.2 `IIntelligenceEngine` & `CarbonCore` (Orchestration Boundary)

- **Purpose:** Maps user natural language to structured `TaskIntent` (`IntelligenceEngine`) and coordinates lifecycle, memory, governance, tools, and CMS (`CarbonCore`).
- **Input / Output Contract:**
  - `IIntelligenceEngine`: `classify_intent(user_prompt, context) -> TaskIntent` (includes `TaskCategory`, `ReasoningDepth`, `required_capabilities`) and `validate_output(raw_output, expected_schema) -> dict`.
  - `CarbonCore`: async `initialize()`, `shutdown()`, and `process_task_stream(TaskRequest) -> AsyncIterator[dict]` plus `cancel_task()`, `resolve_approval()`, and status/metrics accessors.
- **Validation & Failure Behavior:** Always assigns an explicit `ReasoningDepth` (`instant`, `standard`, `deep`); never allows stored memory or tool arguments to overwrite system instructions; persists terminal task state (`completed`, `cancelled`, `failed`, `rejected`) even on mid-stream disconnect or provider errors.
- **Status:** `CarbonCore` & `IntelligenceEngine` **PARTIALLY IMPLEMENTED** (heuristic/regex intent classification and `open_application` fast-path work cleanly; `plan_execution()` and a multi-step LLM -> tool-call -> re-prompt loop do **not** exist).

### 2.3 `IMemoryEngine` (`SQLiteMemoryEngine`)

- **Purpose:** Persists conversations, tasks, and four memory tiers (`working`, `episodic`, `semantic`, `procedural`) in local SQLite.
- **Input / Output Contract:** `initialize()`, `shutdown()`, `store_memory(MemoryRecord) -> str`, `update_memory()`, `delete_memory()`, `forget_memory(query, conversation_id, memory_type, confirm) -> ForgetResult`, `retrieve_relevant()`, `list_memories()`, `purge_expired()`, `clear_working_memory()`, `forget_conversation()`, `get_recent_conversation_turns()`, plus conversation/task CRUD and interrupted-task recovery.
- **Validation & Failure Behavior:** Verifies DB header + `PRAGMA integrity_check` before migrations (fail-closed `DatabaseIntegrityError` with forensic quarantine under `forensics/`); serializes all operations behind `_db_lock`; protects write transactions against async cancellation (`Stage 6.9-F`); requires `confirm=True` before deleting matched memories in `forget_memory()`.
- **Status:** **IMPLEMENTED**.

### 2.4 `IGovernanceEngine` & `ITool` (Security & Tool Boundaries)

- **Purpose:** Enforces default-deny permission checks, single-use human approvals (`HITL`), argument validation, and immutable-chain audit logging.
- **Input / Output Contract:**
  - `IGovernanceEngine`: `initialize()`, `authorize_action(tool_id, permission_level, arguments, autonomy_level) -> bool`, `record_audit_entry(event_type, details, level)`, plus concrete `request_approval()`, `resolve_approval()`, `consume_approval()`, and `verify_audit_log_chain()`.
  - `ITool` / `ToolRegistry`: `definition -> ToolDefinition` (`P0`–`P4`, JSON `parameters_schema`, `timeout_seconds`), `execute(arguments) -> ToolExecutionResult` wrapped by `ToolRegistry.execute_tool()`.
- **Validation & Failure Behavior:** `P4` actions are always denied; `P0` allowed; `P1`–`P3` governed by trusted `AutonomyLevel` (`A0`–`A3`) or single-use `approval_id`. Approvals bind via SHA-256 digest to `(tool_id, arguments)`, expire (`60.0s` default), reject untrusted `DecisionSource` (`MODEL_OUTPUT`, `MEMORY_RECORD`), and can only be consumed once. Startup verifies `audit.log` hash-chain tail (`AuditChainCorruptionError` if tampered). Secret material is redacted via `sanitize_structured_data`.
- **Status:** `GovernanceEngine` **IMPLEMENTED**; `ToolRegistry` & built-ins (`system_info`, `list_applications`, `open_application`) **PARTIALLY IMPLEMENTED** (`get_schemas_for_llm()` is not wired into chat LLM streaming).

### 2.5 `IDeviceEngine` (`DeviceManager`)

- **Purpose:** Exposes local hardware capacity/telemetry (`get_host_status() -> dict`) to `CarbonCore` and `GET /api/v1/system/status`.
- **Validation & Failure Behavior:** Uses macOS `sysctl hw.memsize` + `platform` cached for `30.0s` (`DEFAULT_HARDWARE_CACHE_TTL_SECONDS`).
- **Status:** **PARTIALLY IMPLEMENTED** (local macOS host reporting is implemented; remote/Android `_connected_nodes` is an empty placeholder — **PLANNED**).

---

## 3. Swift-to-Python & Supervision Contracts (`apps/macos/Carbon/Sources/`)

### 3.1 `CarbonClient` (Swift Client Boundary)

- **Purpose:** Manages authenticated HTTP + WebSocket communication from macOS UI to `127.0.0.1:8765`.
- **Contract:** Reads active token from `.carbon_token`, authenticates `/ws/chat`, maps server events into `@Published` UI state (`messages`, streaming tokens, `pendingApproval`, connection errors), and exposes typed REST methods for cancellation, approval decisions, system status, memory pressure, and shutdown requests.
- **Status:** **IMPLEMENTED**.

### 3.2 `ProcessSupervisor`, `SingleInstanceLock` & `MemoryPressureMonitor`

- **Purpose:** Guarantees single UI ownership, safe child-process lifecycle, and memory-pressure reporting.
- **Contract:**
  - `SingleInstanceLock`: exclusive non-blocking `flock` on `~/.carbon/carbon.lock`.
  - `ProcessSupervisor`: detects/reuses healthy Ollama/backend processes without assuming termination ownership of pre-existing external instances; spawns missing processes cleanly; terminates owned processes inside a 5-phase `ShutdownTimeoutConfig.standard` (`1.2s` / `0.3s` / `3.0s` / `2.0s` / `0.3s`, `8.0s` global watchdog).
  - `MemoryPressureMonitor` / `MemoryPressureDebouncer`: maps Darwin Mach memory pressure (`normal=1`, `warning=2`, `critical=4`) with `0.5s` escalation debounce and `10.0s` recovery hysteresis before posting to `/api/v1/system/memory-pressure`.
- **Status:** **IMPLEMENTED**.

---

## 4. Deferred Protocol Contracts (Absent from `protocols.py`)

Earlier exploratory architecture drafts listed protocols that **do not exist** in `src/carbon/interfaces/protocols.py` and remain unimplemented:

| Legacy Protocol Name | Current Code Reality | Status |
| --- | --- | --- |
| `ISpeechToTextEngine` | Not defined in `protocols.py`; `src/carbon/voice/` has 0 files | **DEFERRED** (Stage 6.6) |
| `ITextToSpeechEngine` | Not defined in `protocols.py`; `src/carbon/voice/` has 0 files | **DEFERRED** (Stage 6.6) |
| `IEmbeddingEngine` | Not defined in `protocols.py`; search uses SQLite FTS5 + `LIKE` | **PLANNED** |
| `IVisionEngine` | Not defined in `protocols.py` | **PLANNED** |
