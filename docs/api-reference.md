# Carbon API & WebSocket Reference

**Stage 7.4 Status:** Source-verified against [main.py](../src/carbon/main.py), [orchestrator.py](../src/carbon/core/orchestrator.py), [models.py](../src/carbon/interfaces/models.py), [settings.py](../src/carbon/config/settings.py), [metrics.py](../src/carbon/core/metrics.py), and [manager.py](../src/carbon/devices/manager.py).

**Route Verification Count:** `24` REST routes (`APIRoute`) + `1` WebSocket route (`APIWebSocketRoute` at `/ws/chat`).

> [!IMPORTANT]
> Status labels used in this reference follow Carbon's Stage 7 definitions: **IMPLEMENTED**, **PARTIALLY IMPLEMENTED**, **DEFERRED**, **PLANNED**, and **UNVERIFIED**. Where a registered endpoint exists in source code without dedicated assertions in `tests/`, that automated test gap is noted explicitly.

---

## 1. Common API Behavior

### 1.1 Base URL & Local-Only Networking

- **Default Base URL:** `http://127.0.0.1:8765` (`DEFAULT_API_HOST = "127.0.0.1"`, `DEFAULT_API_PORT = 8765`, WebSocket at `ws://127.0.0.1:8765/ws/chat`).
- **Loopback Enforcement:** `Settings.api_host` rejects non-loopback bindings at startup (`LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}`). At runtime, `loopback_security_middleware` rejects HTTP requests whose client host is not in `ALLOWED_CLIENT_HOSTS` (`LOOPBACK_HOSTS | {"testclient"}`) with HTTP `403`.
- **Strict CORS & `Origin` Checks:**
  - `CORSMiddleware` and `loopback_security_middleware` only allow `Origin` headers matching `{f"http://127.0.0.1:{api_port}", f"http://localhost:{api_port}"}` (verified in `test_origin_validation_and_cors`).
  - Allowed CORS methods: `GET`, `POST`, `PATCH`, `DELETE`, `OPTIONS`; allowed headers: `Authorization`, `Content-Type`, `Accept`.
  - Requests with an external `Origin` or `Origin: null` are rejected (`403` on HTTP, WebSocket close `1008` on `/ws/chat`).

### 1.2 Authentication Mechanism

- **Protected Routes (`verify_local_auth`):** `22` of the `24` REST endpoints depend on `Depends(verify_local_auth)`. Only `GET /` and `GET /api/v1/health` are unauthenticated.
- **Bearer Token Lifecycle:**
  - During FastAPI `lifespan` startup, `_get_or_create_token(settings)` reads `CARBON_API_TOKEN` if set, otherwise reads `settings.token_file_path` (default `data/.carbon_token`), or generates `secrets.token_urlsafe(32)` and writes it atomically with `0600` POSIX permissions.
  - Requests must pass `Authorization: Bearer <token>`. Verification uses `secrets.compare_digest` via `_constant_time_token_matches`.
  - **Code observation (test vs. server):** If `app.state.api_token` is unset/falsy (for example, ASGI test clients invoked without running FastAPI `lifespan`), `verify_local_auth` returns `True`. Under normal server `lifespan` startup, `app.state.api_token` is always populated.

### 1.3 Request Validation, Timeouts & Error Conventions

- **Validation:** Inbound JSON bodies and query parameters use FastAPI/Pydantic validation; malformed types/missing fields return FastAPI's standard HTTP `422` JSON validation payload (`{"detail": [...]}`). Custom blank-string guards raise `HTTPException(422, detail=...)`.
- **Error Response Format:** Explicit HTTP errors return `{"detail": "<message>"}`. Note that `POST /api/v1/tools/{tool_id}/execute` returns HTTP `200` with `ToolExecutionResult` (`success: false`, `error: "..."`) on permission/validation denial rather than raising an HTTP error status.
- **Configured Timeouts (`Settings` defaults):** `inference_timeout_seconds = 300.0`, `model_load_timeout_seconds = 60.0`, `discovery_timeout_seconds = 5.0`, `approval_timeout_seconds = 60.0`, `drain_timeout_seconds = 10.0` (CMS), and `WS_AUTH_TIMEOUT_SECONDS = 3.0`.
- **Secret Hygiene:** `TokenSanitizingFilter` redacts bearer tokens in logs; audit entries sanitize payloads via `sanitize_structured_data`.

---

## 2. REST API Endpoint Inventory (`24` Registered Routes)

| # | Method & Path | Auth | Handler | Status |
| --- | --- | --- | --- | --- |
| 1 | `GET /` | Public | `root` | **IMPLEMENTED** |
| 2 | `GET /api/v1/health` | Public | `get_health` | **IMPLEMENTED** |
| 3 | `GET /api/v1/metrics` | Bearer | `get_metrics` | **IMPLEMENTED** |
| 4 | `POST /api/v1/auth/rotate-token` | Bearer | `rotate_token` | **IMPLEMENTED** |
| 5 | `GET /api/v1/models` | Bearer | `list_models` | **IMPLEMENTED** |
| 6 | `POST /api/v1/models/refresh` | Bearer | `refresh_models` | **IMPLEMENTED** |
| 7 | `POST /api/v1/models/switch` | Bearer | `switch_model` | **IMPLEMENTED** |
| 8 | `POST /api/v1/tasks` | Bearer | `submit_task` | **IMPLEMENTED** |
| 9 | `GET /api/v1/tasks/{task_id}` | Bearer | `get_task_status` | **IMPLEMENTED** |
| 10 | `POST /api/v1/tasks/{task_id}/cancel` | Bearer | `cancel_task` | **IMPLEMENTED** *(No direct REST test assertion found in `tests/`)* |
| 11 | `GET /api/v1/tools` | Bearer | `list_tools` | **IMPLEMENTED** *(No direct REST test assertion found in `tests/`)* |
| 12 | `POST /api/v1/tools/{tool_id}/execute` | Bearer | `execute_tool_endpoint` | **IMPLEMENTED** |
| 13 | `GET /api/v1/approvals` | Bearer | `list_pending_approvals` | **IMPLEMENTED** *(Only `401` unauthenticated check found in `tests/`)* |
| 14 | `POST /api/v1/approvals/{approval_id}/decision` | Bearer | `submit_approval_decision` | **IMPLEMENTED** |
| 15 | `GET /api/v1/system/status` | Bearer | `get_system_status` | **IMPLEMENTED** *(No direct REST test assertion found in `tests/`)* |
| 16 | `POST /api/v1/system/memory-pressure` | Bearer + Loopback | `report_memory_pressure` | **IMPLEMENTED** |
| 17 | `POST /api/v1/system/shutdown` | Bearer + Loopback | `system_shutdown` | **IMPLEMENTED** |
| 18 | `GET /api/v1/memory` | Bearer | `list_memories_endpoint` | **IMPLEMENTED** |
| 19 | `POST /api/v1/memory/procedural/requests` | Bearer | `request_procedural_approval_endpoint` | **IMPLEMENTED** |
| 20 | `POST /api/v1/memory` | Bearer | `create_memory_endpoint` | **IMPLEMENTED** |
| 21 | `PATCH /api/v1/memory/{record_id}` | Bearer | `update_memory_endpoint` | **IMPLEMENTED** |
| 22 | `DELETE /api/v1/memory/{record_id}` | Bearer | `delete_memory_endpoint` | **IMPLEMENTED** |
| 23 | `POST /api/v1/memory/forget` | Bearer | `forget_memories_endpoint` | **IMPLEMENTED** |
| 24 | `DELETE /api/v1/conversations/{conversation_id}` | Bearer | `delete_conversation_endpoint` | **IMPLEMENTED** |

*(Legacy/absent endpoints documented in older drafts — such as REST `/api/v1/chat`, voice STT/TTS routes, and Android remote-node routes — are **DEFERRED** or **PLANNED** and are not registered on `app.routes`.)*

---

## 3. REST Endpoint Reference

### 3.1 Status, Metrics & Token Rotation

#### 1. `GET /`
- **Purpose:** Basic service identity check confirming loopback-only mode.
- **Parameters / Body:** None.
- **Authentication:** None (public, still subject to `loopback_security_middleware`).
- **Response (`200 OK`):** `{"name": "Carbon", "version": "1.0.0", "status": "online", "loopback_only": true}`
- **Validation & Authorization:** Loopback host and allowed `Origin` checks only.
- **Failure Cases:** `403` for non-loopback client host or unauthorized `Origin`.
- **Status:** **IMPLEMENTED** (verified in [test_api_endpoints.py](../tests/integration/test_api_endpoints.py)).

#### 2. `GET /api/v1/health`
- **Purpose:** Aggregates subsystem health from `CarbonCore.get_system_health()`, Ollama runtime, CMS active/approved models, and host status.
- **Parameters / Body:** None.
- **Authentication:** None (public).
- **Response (`200 OK`):** JSON object containing `status` (`"healthy"` or `"degraded"`), `core_running` (`bool`), `ollama_connected` (`bool`), `active_model` (`ModelMetadata | null`), `approved_models` (`list[ModelMetadata]`), `installed_models_count` (`int`), `model_availability` (`str`), `autonomy_level` (`str`), `host` (`dict` from `DeviceManager.get_host_status()`), `tasks_count` (`int`).
- **Validation & Authorization:** Loopback and `Origin` middleware; triggers lazy `get_core()` initialization if not yet initialized.
- **Failure Cases:** Returns `status: "degraded"` (`200 OK`) when core is not running, Ollama is unreachable, or no approved model is running/healthy; `403` for non-loopback/`Origin`.
- **Status:** **IMPLEMENTED** (verified in `test_api_endpoints.py`, [test_desktop_connection_reliability.py](../tests/test_desktop_connection_reliability.py)).

#### 3. `GET /api/v1/metrics`
- **Purpose:** Exposes zero-PII backend monotonic latency distributions, counters, throughput, and CMS state from `MetricsRegistry.snapshot()`.
- **Parameters / Body:** None.
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** JSON object with `schema_version` (`"5a.1"`), `clock_basis` (`"backend_monotonic_perf_counter"`), `window_sample_count`, `window_max_samples`, `counters` (`total_tasks`, `completed_tasks`, `failed_tasks`, `cancelled_tasks`, `fast_path_tasks`, `switched_tasks`, `switch_attempts`, `switch_failures`, `model_rejections`, `failure_kind_counts`), `cms_state` (`active_model_id`, `ollama_available`, `current_pressure_level`, `max_context_tokens`), `latencies_ms` (`overall`, `by_reasoning_depth`, `by_routing_path`), and `throughput` (`provider_eval_tokens_per_sec`).
- **Validation & Authorization:** Rejects missing/invalid bearer token (`401`) and untrusted origins (`403`).
- **Failure Cases:** `401` unauthenticated; `403` bad `Origin`/non-loopback.
- **Status:** **IMPLEMENTED** (verified in [test_api_security.py](../tests/security/test_api_security.py) and `tests/unit/test_telemetry_metrics_5_a.py`).

#### 4. `POST /api/v1/auth/rotate-token`
- **Purpose:** Generates a new 32-byte URL-safe token, writes `settings.token_file_path` atomically (`0600`), updates `app.state.api_token`, and immediately closes active `/ws/chat` sessions holding pre-rotation tokens.
- **Parameters / Body:** None.
- **Authentication:** Required (`verify_local_auth` with current valid token).
- **Response (`200 OK`):** `{"status": "rotated", "message": "Token rotated successfully."}`
- **Validation & Authorization:** Existing active WebSocket clients whose stored session token does not match the rotated token receive `{"event": "error", "message": "Authentication token rotated. Re-authentication required."}` and close code `4401` immediately.
- **Failure Cases:** `401` invalid token; `500` if atomic token file persistence fails (`"Failed to persist rotated token."`).
- **Status:** **IMPLEMENTED** (verified in `test_api_security.py` and `tests/security/test_governance_and_tools_security.py`).

---

### 3.2 Model Control & Supervision (`CMS`)

#### 5. `GET /api/v1/models`
- **Purpose:** Returns active model state, approved models (currently `qwen3.5:9b`), and discovered installed Ollama models.
- **Parameters / Body:** None.
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `{"active_model": <ModelMetadata | null>, "registered_models": [<ModelMetadata>, ...], "approved_models": [<ModelMetadata>, ...], "installed_models": [<ModelMetadata>, ...]}` (`registered_models` mirrors `approved_models`).
- **Validation & Authorization:** Standard loopback + bearer auth.
- **Failure Cases:** `401` unauthenticated; `403` bad `Origin`/non-loopback.
- **Status:** **IMPLEMENTED** (verified in `test_api_security.py` and `tests/unit/test_model_lifecycle.py`).

#### 6. `POST /api/v1/models/refresh`
- **Purpose:** Re-probes Ollama runtime health and model discovery (`/api/tags`, `/api/ps`) through `CMS.refresh_models(probe_health=True)` and records an audit entry.
- **Parameters / Body:** None.
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `{"status": "refreshed", "summary": <dict>, "active_model": <ModelMetadata | null>, "registered_models": [...], "approved_models": [...], "installed_models": [...]}`
- **Validation & Authorization:** Audits `MODEL_REGISTRY_REFRESHED` (`INFO`) on success or `MODEL_REGISTRY_REFRESH_REJECTED` (`WARNING`) on drain timeout.
- **Failure Cases:** `401` unauthenticated; `409 Conflict` when `ModelUnavailableError` has `reason == DRAIN_TIMEOUT` (an active inference lease did not drain); `503 Service Unavailable` for other `ModelUnavailableError` causes.
- **Status:** **IMPLEMENTED** (verified in `test_api_security.py` and `tests/unit/test_model_lifecycle.py`).

#### 7. `POST /api/v1/models/switch`
- **Purpose:** Switches or activates an approved model via `CarbonModelSupervisor.switch_model()`.
- **Request Body (`ModelSwitchRequest`):** `target_model_id` (`str`), `reason` (`str`), `force` (`bool = False`).
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `{"status": "success", "active_model": <ModelMetadata | null>}`
- **Validation & Authorization:** Strips `target_model_id`; checks `cms.is_model_approved(clean_target)` strictly against `DEFAULT_APPROVED_MODELS` (`("qwen3.5:9b",)`). Rejections record `MODEL_SWITCH_REJECTED` in `audit.log`.
- **Failure Cases:** `422` if `target_model_id` is blank after stripping; `403 Forbidden` if unapproved (`UNAPPROVED`); `409 Conflict` if `NOT_INSTALLED`; `503 Service Unavailable` if `OLLAMA_UNAVAILABLE`, `UNAVAILABLE`, `DRAIN_TIMEOUT`, `UNLOAD_FAILED`, or `LOAD_FAILED`; `400 Bad Request` for other mapped failures.
- **Status:** **IMPLEMENTED** (verified in `test_api_security.py`, `tests/integration/test_model_routing_e2e_4_4d.py`, and `tests/unit/test_model_lifecycle.py`).

---

### 3.3 Tasks

#### 8. `POST /api/v1/tasks`
- **Purpose:** Creates a `TaskRecord` in `CarbonCore`, optionally executing it asynchronously in the background when `stream=false`.
- **Request Body (`TaskRequest`):** `prompt` (`str`), `conversation_id` (`str | null = null`), `reasoning_override` (`"instant" | "standard" | "deep" | "auto" | "none" | "" | null = null`; `"auto"`, `"none"`, `""` normalize to `null`), `stream` (`bool = true`).
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `{"task_id": <str>, "conversation_id": <str>, "state": <TaskState value>}` (initial state is `"pending"`).
- **Validation & Authorization:** Rejects submissions when `core.is_accepting_tasks` is `False` during shutdown.
- **Code Behavior Note:** Because this REST route does not return a streaming body, background execution via `create_tracked_task` runs **only when `stream=false`**. When `stream=true` (the model default), `submit_task` creates the task record without starting execution (streaming clients submit via `/ws/chat`).
- **Failure Cases:** `401` unauthenticated; `422` invalid body/reasoning value; `503` if backend is shutting down.
- **Status:** **IMPLEMENTED** (verified with `stream: false` in `test_api_endpoints.py` and `test_model_routing_e2e_4_4d.py`).

#### 9. `GET /api/v1/tasks/{task_id}`
- **Purpose:** Queries task status and persisted/in-memory details via `CarbonCore.get_task_persistent(task_id)`.
- **Path Parameters:** `task_id` (`str`).
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `PersistedTaskRecord` / `TaskRecord.to_dict()` JSON (`task_id`, `conversation_id`, `prompt`, `state`, `intent`, `reasoning_depth`, `model_used`, `result`, `error`, `truncated`, `created_at`, `completed_at`, plus retention metadata on persisted records). Expired task payloads past `task_payload_ttl_days` have `prompt`, `result`, `error`, and `intent` masked (`null`).
- **Validation & Authorization:** Enforces `task_ttl_days` and `task_payload_ttl_days`.
- **Failure Cases:** `401` unauthenticated; `404 Not Found` (`"Task not found."`) if unknown or expired past `task_ttl_days`.
- **Status:** **IMPLEMENTED** (verified in `test_api_endpoints.py`, `test_memory_api_security.py`, `test_model_routing_e2e_4_4d.py`).

#### 10. `POST /api/v1/tasks/{task_id}/cancel`
- **Purpose:** Signals cooperative cancellation (`cancel_event.set()`) on an active in-memory task.
- **Path Parameters:** `task_id` (`str`).
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `{"task_id": <str>, "status": "cancelled"}`
- **Validation & Authorization:** Returns `False` from `core.cancel_task(task_id)` if the task is unknown in memory or already in a terminal state (`completed`, `failed`, `cancelled`).
- **Failure Cases:** `401` unauthenticated; `400 Bad Request` (`"Task cannot be cancelled or does not exist."`).
- **Status:** **IMPLEMENTED** *(Direct REST endpoint assertions were not found in `tests/`; runtime cancellation is exercised via `/ws/chat` tests).*

---

### 3.4 Tools & Human-in-the-Loop Approvals

#### 11. `GET /api/v1/tools`
- **Purpose:** Lists all tools registered in `ToolRegistry`.
- **Parameters / Body:** None.
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** JSON array of `ToolDefinition` objects (`id`, `name`, `description`, `permission_level`, `parameters_schema`, `is_reversible`, `timeout_seconds`). Currently registered built-ins: `system_info` (`P1`), `list_applications` (`P1`), `open_application` (`P2`).
- **Validation & Authorization:** Loopback + bearer auth.
- **Failure Cases:** `401` unauthenticated; `403` bad `Origin`.
- **Status:** **IMPLEMENTED** *(No direct `GET /api/v1/tools` assertion found in `tests/`).*

#### 12. `POST /api/v1/tools/{tool_id}/execute`
- **Purpose:** Executes a registered tool through `ToolRegistry.execute_tool()` with JSON schema validation, default-deny `GovernanceEngine` authorization, timeout enforcement, and audit logging.
- **Path Parameters:** `tool_id` (`str`).
- **Request Body (`ToolExecuteRequest`):** `arguments` (`dict[str, Any] = {}`), `approval_id` (`str | null = null`), `task_id` (`str | null = null`).
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `ToolExecutionResult` (`{"tool_id": <str>, "success": <bool>, "data": <any | null>, "error": <str | null>, "duration_ms": <float>}`).
- **Validation & Authorization:** Validates arguments against the tool's `parameters_schema`; checks `approval_id` (`consume_approval`) if provided or trusted policy (`authorize_action(autonomy_level)`). Rejections do **not** raise HTTP `403` — they return HTTP `200` with `success: false` and `error` set.
- **Failure Cases:** `401` unauthenticated; `422` invalid body schema; `200` with `success: false` on unregistered tool, invalid tool arguments, governance denial (`PERMISSION_DENIED`), or execution timeout/exception.
- **Status:** **IMPLEMENTED** (verified in `tests/security/test_governance_and_tools_security.py`).

#### 13. `GET /api/v1/approvals`
- **Purpose:** Returns all currently pending approvals held by `GovernanceEngine`.
- **Parameters / Body:** None.
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** Array of objects: `approval_id`, `task_id`, `tool_id`, `permission_level`, `summary`, `is_reversible`, `expires_at` (ISO-8601), `status`.
- **Validation & Authorization:** Loopback + bearer auth.
- **Failure Cases:** `401` unauthenticated; `403` bad `Origin`.
- **Status:** **IMPLEMENTED** *(Only unauthenticated `401` check observed in `tests/security/test_governance_and_tools_security.py`; authenticated listing shape verified by code inspection).*

#### 14. `POST /api/v1/approvals/{approval_id}/decision`
- **Purpose:** Approves or rejects a pending HITL approval using trusted `DecisionSource.USER_API`.
- **Path Parameters:** `approval_id` (`str`).
- **Request Body (`ApprovalDecisionRequest`):** `approved` (`bool`), `task_id` (`str | null = null`).
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `{"approval_id": <str>, "status": <message str>}`
- **Validation & Authorization:** Binds decision to `approval_id` and optional `task_id`; rejects expired, already-resolved, missing, or mismatched approvals.
- **Failure Cases:** `401` unauthenticated; `422` invalid body; `400 Bad Request` (`detail=message`) when `resolve_approval` returns `False`.
- **Status:** **IMPLEMENTED** (verified in [test_memory_api_security.py](../tests/security/test_memory_api_security.py)).

---

### 3.5 System Telemetry, Memory Pressure & Shutdown

#### 15. `GET /api/v1/system/status`
- **Purpose:** Returns local macOS host hardware telemetry (`DeviceManager.get_host_status()`, 30s cache TTL).
- **Parameters / Body:** None.
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `{"device_id": "mac-local", "name": <hostname>, "os": <str>, "version": <str>, "arch": <str>, "cpu_count": <int>, "unified_memory_bytes": <int>, "unified_memory_gb": <float>, "status": "online"}`
- **Validation & Authorization:** Loopback + bearer auth.
- **Failure Cases:** `401` unauthenticated; `403` bad `Origin`/non-loopback.
- **Status:** **IMPLEMENTED** *(No direct `GET /api/v1/system/status` assertion found in `tests/`; `DeviceManager` itself is covered by code and device checks).*

#### 16. `POST /api/v1/system/memory-pressure`
- **Purpose:** Accepts native macOS Mach memory-pressure transition reports on loopback, forwards them to `CMS.handle_memory_pressure(level)`, and logs `MEMORY_PRESSURE_UPDATED`.
- **Request Body (`MemoryPressureReportRequest`):** `level` (`int`, strictly `1` normal, `2` warning, or `4` critical; booleans rejected), `source` (`str = "macos_dispatch_source"`, length `1..64`).
- **Authentication:** Required (`verify_local_auth`) + explicit non-loopback `request.client.host` check inside handler.
- **Response (`200 OK`):** `{"status": "applied", "level": <int>, "active_model_id": <str | null>, "deferred_critical_unload": <bool>, "max_context_tokens": <int>, "summary": <dict>}`
- **Validation & Authorization:** Audits severity `"INFO"` (1), `"WARNING"` (2), or `"CRITICAL"` (4). Rejects reports when shutting down.
- **Failure Cases:** `401` unauthenticated; `403` non-loopback; `422` non-integer, boolean, or unsupported level (`not in (1, 2, 4)`); `503` when `core.is_accepting_tasks` is `False`.
- **Status:** **IMPLEMENTED** (verified in `tests/unit/test_stage5d_pressure_and_watchdog.py`).

#### 17. `POST /api/v1/system/shutdown`
- **Purpose:** Triggers graceful Uvicorn shutdown (`server.should_exit = True`) via FastAPI `BackgroundTasks` strictly after flushing the HTTP `200` response.
- **Parameters / Body:** None.
- **Authentication:** Required (`verify_local_auth`) + explicit loopback client check.
- **Response (`200 OK`):** `{"status": "shutting_down", "message": "Carbon backend is terminating gracefully."}` or idempotent `{"status": "already_shutting_down", "message": "Carbon backend is already stopping."}` if `server.should_exit` is already `True`.
- **Validation & Authorization:** Checks `app.state.uvicorn_server or server_instance`.
- **Failure Cases:** `401` unauthenticated; `403` non-loopback client; `503 Service Unavailable` when no managed Uvicorn `server` instance is registered.
- **Status:** **IMPLEMENTED** (verified in [test_api_endpoints.py](../tests/integration/test_api_endpoints.py)).

---

### 3.6 Persistent Memory & Conversations

#### 18. `GET /api/v1/memory`
- **Purpose:** Lists active (non-expired) memory records with pagination and optional filters.
- **Query Parameters:** `memory_type` (`MemoryType | null = null`: `working | episodic | semantic | procedural`), `conversation_id` (`str | null = null`), `limit` (`int = 50`, `1..200`), `offset` (`int = 0`, `>= 0`).
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `{"items": [<MemoryRecord JSON>, ...], "total": <int>, "limit": <int>, "offset": <int>}`
- **Validation & Authorization:** Rejects invalid `memory_type`, out-of-range `limit`/`offset` (`422`); expired records are excluded.
- **Failure Cases:** `401` unauthenticated; `422` invalid query parameter bounds.
- **Status:** **IMPLEMENTED** (verified in [test_memory_api_security.py](../tests/security/test_memory_api_security.py)).

#### 19. `POST /api/v1/memory/procedural/requests`
- **Purpose:** Creates a `P3` `GovernanceEngine` single-use approval challenge (`202 Accepted`) required before writing or updating a `procedural` memory record. Nothing is persisted by this call.
- **Request Body (`ProceduralApprovalRequest`):** `key` (`str`, `1..256`), `content` (`str`, `1..16384`), `task_id` (`str | null = null`), `record_id` (`str | null = null`), `metadata` (`dict[str, Any] = {}`).
- **Authentication:** Required (`verify_local_auth`).
- **Response (`202 Accepted`):** `{"approval_id": <str>, "status": <str>, "tool_id": <str>, "arguments_digest": <sha256 str>, "expires_at": <ISO-8601 str>}`
- **Validation & Authorization:** Strips `key` and `content`; rejects `metadata["auto_promoted"]` truthy attempts with `403`.
- **Failure Cases:** `401` unauthenticated; `422` whitespace-only/invalid `key` or `content`; `403` automatic promotion attempt.
- **Status:** **IMPLEMENTED** (verified in `test_memory_api_security.py`).

#### 20. `POST /api/v1/memory`
- **Purpose:** Persists an explicit memory record (`201 Created`). `PROCEDURAL` writes strictly enforce a consumed, argument-digest-matched human approval created via `/api/v1/memory/procedural/requests` and resolved via `/api/v1/approvals/{approval_id}/decision`.
- **Request Body (`MemoryCreateRequest`):** `memory_type` (`MemoryType`), `key` (`str`, `1..256`), `content` (`str`, `1..16384`), `conversation_id` (`str | null = null`), `approval_id` (`str | null = null`), `task_id` (`str | null = null`), `source` (`str = "user_api"`), `metadata` (`dict[str, Any] = {}`).
- **Authentication:** Required (`verify_local_auth`).
- **Response (`201 Created`):** `{"status": "stored", "record_id": <uuid str>, "memory_type": <MemoryType value>}`
- **Validation & Authorization:** Rejects untrusted `source` values (such as `"model_output"`), missing/unapproved/replayed/tampered `approval_id` for `procedural` records (`403`), or blank stripped `key`/`content` (`422`).
- **Failure Cases:** `401` unauthenticated; `422` blank fields; `403 Forbidden` on governance denial (`PermissionError` mapped cleanly).
- **Status:** **IMPLEMENTED** (verified in `test_memory_api_security.py`).

#### 21. `PATCH /api/v1/memory/{record_id}`
- **Purpose:** Updates content and optional metadata on an existing non-expired memory record (`PROCEDURAL` records require a fresh record-bound `approval_id`), emitting sanitized `MEMORY_UPDATED` audit entries.
- **Path Parameters:** `record_id` (`str`).
- **Request Body (`MemoryUpdateRequest`):** `content` (`str`, `1..16384`), `metadata` (`dict[str, Any] | null = null`), `approval_id` (`str | null = null`), `task_id` (`str | null = null`).
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `{"status": "updated", "record_id": <str>}`
- **Validation & Authorization:** Maps `PermissionError` to `403`; rejects blank `content` (`422`).
- **Failure Cases:** `401` unauthenticated; `422` blank content; `403` governance denial; `404 Not Found` (`"Memory record not found."`).
- **Status:** **IMPLEMENTED** (verified in `test_memory_api_security.py`).

#### 22. `DELETE /api/v1/memory/{record_id}`
- **Purpose:** Deletes a single memory record by `id` and records `MEMORY_FORGOTTEN` (`deleted_count: 1`).
- **Path Parameters:** `record_id` (`str`).
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `{"status": "deleted", "record_id": <str>, "deleted_count": 1}`
- **Validation & Authorization:** Standard loopback + bearer auth.
- **Failure Cases:** `401` unauthenticated; `404 Not Found` (`"Memory record not found."`).
- **Status:** **IMPLEMENTED** (verified in `test_memory_api_security.py`).

#### 23. `POST /api/v1/memory/forget`
- **Purpose:** Confirmation-safe memory forgetting by query and/or explicit `selected_ids`. When multiple/ambiguous records match with `confirm=false`, returns candidate records without deleting anything.
- **Request Body (`MemoryForgetRequest`):** `query` (`str = ""`), `confirm` (`bool = false`), `selected_ids` (`list[str] | null = null`), `memory_type` (`MemoryType | null = null`).
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `{"status": <"deleted" | "confirmation_required" | "not_found">, "deleted_count": <int>, "candidates": [<MemoryRecord JSON>, ...]}`
- **Validation & Authorization:** Requires non-empty `query.strip()` or non-empty `selected_ids`; audits `MEMORY_FORGOTTEN` when `deleted_count > 0`.
- **Failure Cases:** `401` unauthenticated; `422` when both `query` and `selected_ids` are empty.
- **Status:** **IMPLEMENTED** (verified in `test_memory_api_security.py`).

#### 24. `DELETE /api/v1/conversations/{conversation_id}`
- **Purpose:** Cascades deletion of a conversation row, its persisted `tasks` (`ON DELETE CASCADE`), and linked `memory_records` + FTS entries.
- **Path Parameters:** `conversation_id` (`str`).
- **Authentication:** Required (`verify_local_auth`).
- **Response (`200 OK`):** `{"status": "deleted", "conversation_id": <str>, "conversations_deleted": <int>, "tasks_deleted": <int>, "memories_deleted": <int>}` *(unpacks `counts` returned by `core.delete_conversation`)*.
- **Validation & Authorization:** Standard loopback + bearer auth.
- **Failure Cases:** `401` unauthenticated; `404 Not Found` (`"Conversation not found."`) when both `conversations_deleted == 0` and `memories_deleted == 0`.
- **Status:** **IMPLEMENTED** (verified in `test_memory_api_security.py`).

---

## 4. WebSocket Reference: `/ws/chat`

- **Endpoint:** `ws://127.0.0.1:8765/ws/chat` (**IMPLEMENTED** — handler `websocket_chat_endpoint` in [main.py](../src/carbon/main.py)).

### 4.1 Connection Limits, Origin Checks & Authentication

1. **Origin Validation:** Before `websocket.accept()`, checks `websocket.headers.get("origin")`. If present and not in `allowed_origins` (`http://127.0.0.1:{api_port}` or `http://localhost:{api_port}`), closes immediately with WebSocket code `1008` (`WS_1008_POLICY_VIOLATION`).
   *(Note: `@app.middleware("http")` does not wrap WebSocket scopes in Starlette, so client host filtering is enforced at the loopback socket bind + `Origin` validation.)*
2. **Concurrency Cap:** `MAX_CONCURRENT_WS_CONNECTIONS = 5`. A 6th concurrent connection is closed immediately with `1013` (`WS_1013_TRY_AGAIN_LATER`).
3. **Authentication Modes (`query-string tokens disallowed`):**
   - **Preferred (Upgrade Header):** `Authorization: Bearer <token>` verified with constant-time comparison before entering the message loop (no separate `"authenticated"` event is sent on header auth).
   - **Fallback (First-Message Auth within `3.0s`):** If unauthenticated after `websocket.accept()`, the server waits at most `WS_AUTH_TIMEOUT_SECONDS = 3.0` for `{"action": "auth", "token": "<token>"}`.
     - Valid token -> stores session token, replies `{"event": "authenticated"}`.
     - Missing/invalid token or wrong action -> sends `{"event": "error", "message": "Authentication failed. In-socket auth frame required."}` and closes with code `4401`.
     - Timeout or malformed JSON -> closes with `4401`.
   - **Query-String Rejection:** Passing `?token=...` does **not** authenticate; a subsequent non-`auth` frame fails as unauthenticated (`test_websocket_query_string_token_rejected`).
   - **Continuous Token Rotation Check:** Rotating the token via `POST /api/v1/auth/rotate-token` immediately closes stale sockets with `4401`, and the receive loop also re-checks `session_token` against `app.state.api_token` before handling any subsequent message (`4401` + `"Authentication token rotated. Re-authentication required."`).

### 4.2 Client-to-Server Messages (JSON)

| `action` | Fields | Behavior |
| --- | --- | --- |
| `"auth"` | `token` (`str`) | Initial authentication fallback (within `3.0s`) or mid-session token refresh (`{"event": "authenticated"}` or `{"event": "error", "message": "Invalid token."}`). |
| `"submit"` *(default if `"action"` omitted)* | `prompt` (`str`, non-empty after strip), `conversation_id` (`str \| optional`), `reasoning` (`"instant" \| "standard" \| "deep" \| "auto" \| optional`) | Creates `TaskRequest(stream=True)` using `conversation_id` or the socket's sticky `session_conversation_id` (`conv-<8 hex chars>`), emits `"task_created"`, and launches a tracked background task `_stream_task_events(task)` so `approve`/`deny`/`cancel` frames can arrive concurrently. |
| `"cancel"` | `task_id` (`str`) | Calls `core.cancel_task(task_id)`, clears `active_task_id` if matching, and sends `{"event": "cancelled", "task_id": task_id}`. |
| `"approve"` / `"deny"` | `approval_id` (`str`, required), `task_id` (`str \| optional`) | Calls `core.governance.resolve_approval(..., approved=(action == "approve"), source=DecisionSource.USER_UI, task_id=...)`. Sends `{"event": "error", "message": ...}` if `approval_id` is empty or resolution fails. |

*(Validation note: Unrecognized `action` values are ignored cleanly without crashing the socket; invalid JSON replies `{"event": "error", "message": "Invalid JSON format."}` without closing the connection, verified by `test_websocket_failed_submission_recovery`.)*

### 4.3 Server-to-Client Events (JSON)

Events are serialized behind `ws_send_lock` from `websocket_chat_endpoint` and `CarbonCore.execute_task_stream()`:

| `event` | Payload Fields Verified in Source | Source |
| --- | --- | --- |
| `"authenticated"` | `{"event": "authenticated"}` | `main.py` (`auth` frame success) |
| `"task_created"` | `task_id`, `conversation_id`, `reasoning_mode` (`"auto"` or `ReasoningDepth`), `t_backend_receipt` (`float` epoch) | `main.py` (`submit`) |
| `"state_change"` | `state` (`"analyzing"`, `"awaiting_approval"`, `"executing"`, `"streaming"`), plus `t_inference_start` (`float`) when entering `streaming` | `orchestrator.py` |
| `"intent_classified"` | `category`, `task_category`, `reasoning_depth` | `orchestrator.py` |
| `"model_switching"` | `from_model` (`str \| null`), `target_model` (`str`) | `orchestrator.py` |
| `"acquiring_model_lease"` | `model` (`str`) | `orchestrator.py` |
| `"tool_call_started"` | `tool` (`"open_application"`), `args` (`dict`) | `orchestrator.py` (`SYSTEM_COMMAND` fast-path) |
| `"awaiting_approval"` | `approval_id`, `task_id`, `tool_id`, `permission_level`, `title`, `arguments`, `impact_summary`, `is_reversible`, `timeout_seconds` | `orchestrator.py` (`P2` approval pause) |
| `"approval_resolved"` | `approval_id`, `status` (`"approved"`, `"cancelled"`, `"expired"`, or rejection status) | `orchestrator.py` |
| `"thinking"` | `text` (`str`), `t_reasoning` (`float` epoch) | `orchestrator.py` (model reasoning stream) |
| `"token"` | `text` (`str`), and `t_token` (`float` epoch) on LLM / truncation / empty-fallback emissions | `orchestrator.py` |
| `"completed"` | `task` (`TaskRecord.to_dict()`), plus optional `backend_timings_ms` (`dict` when recorded) | `orchestrator.py` |
| `"cancelled"` | `task_id` | `main.py` / `orchestrator.py` |
| `"error"` | `message` (`str`) | `main.py` / `orchestrator.py` |
| `"failed"` | `error` (`str`), and `incomplete` (`bool`) + `failure_kind` (`str`) on post-lease/top-level exceptions *(model rejection yields `error` then `{"event": "failed", "error": ...}`)* | `main.py` / `orchestrator.py` |

### 4.4 Streaming, Governance, Cancellation & Disconnect Limitations

- **Streaming Concurrency:** Each `/ws/chat` task runs in its own background `asyncio.Task` (`ws-task:{task_id}`) while the socket loop continues reading incoming frames, allowing `approve`, `deny`, or `cancel` mid-task. Underlying CMS inference holds a single active lease (`acquire_inference_lease`).
- **Governance During `/ws/chat`:** `SYSTEM_COMMAND` prompts starting with `"open "` (`open_application`, `P2`) pause for up to `60.0s` emitting `awaiting_approval` until resolved by trusted `DecisionSource.USER_UI` (`approve`/`deny` on WS) or `DecisionSource.USER_API` (REST), expired, or cancelled. There is no general LLM -> tool-call -> re-prompting loop.
- **Disconnect & Stage 6.9-F Cancellation Guard:** On `WebSocketDisconnect` or error, `_cleanup_connection_task()` cancels active `stream_tasks` and active `owned_task_ids` **unless** the task already reached a terminal state (`completed`, `failed`, `cancelled`) — in which case cancellation is deferred so post-completion SQLite persistence finishes uninterrupted.
- **Reconnection Limitations:** Reconnection creates a fresh `session_conversation_id` unless the client supplies the previous `conversation_id` in subsequent `"submit"` payloads; in-flight WebSocket streams are **not** resumed/replayed after disconnect (`test_websocket_reconnect` verifies clean new authentication and task execution on reconnect).
- **Truncated Generations:** If Ollama ends with `done_reason == "length"`, `task.truncated` becomes `True` and a Markdown warning callout is emitted as an extra `"token"` event before `"completed"`.

---

## 5. Evidence Sources Inspected

- **Primary Source Files:**
  - [main.py](../src/carbon/main.py) — FastAPI `create_app()`, middleware, `verify_local_auth`, all 24 REST handlers, `/ws/chat` handler
  - [orchestrator.py](../src/carbon/core/orchestrator.py) — `TaskRecord`, `get_system_health()`, `get_task_persistent()`, `cancel_task()`, `execute_task_stream()`, `_record_model_rejection()`, `_handle_inference_failure()`
  - [models.py](../src/carbon/interfaces/models.py) — `ModelSwitchRequest`, `MemoryType`, `MemoryRecord`, `ToolDefinition`, `ToolExecutionResult`, `TaskRequest`, `ForgetResult`
  - [settings.py](../src/carbon/config/settings.py) — `Settings`, loopback/origin/timeout/TTL defaults
  - [metrics.py](../src/carbon/core/metrics.py) — `MetricsRegistry.snapshot()`
  - [manager.py](../src/carbon/devices/manager.py) — `DeviceManager.get_host_status()`
- **Supporting Test Files Inspected:**
  - [test_api_endpoints.py](../tests/integration/test_api_endpoints.py)
  - [test_api_security.py](../tests/security/test_api_security.py)
  - [test_memory_api_security.py](../tests/security/test_memory_api_security.py)
  - [test_desktop_connection_reliability.py](../tests/test_desktop_connection_reliability.py)
  - Grep coverage references across `tests/security/test_governance_and_tools_security.py`, `tests/integration/test_model_routing_e2e_4_4d.py`, `tests/unit/test_model_lifecycle.py`, `tests/unit/test_stage5d_pressure_and_watchdog.py`, and `tests/unit/test_telemetry_metrics_5_a.py`
