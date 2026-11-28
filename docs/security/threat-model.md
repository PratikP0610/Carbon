# Carbon Security Threat Model & Security Controls

**Stage 7.5 Alignment Status:** Aligned strictly to [main.py](../../src/carbon/main.py), [governance_engine.py](../../src/carbon/governance/governance_engine.py), [orchestrator.py](../../src/carbon/core/orchestrator.py), [logging.py](../../src/carbon/core/logging.py), [settings.py](../../src/carbon/config/settings.py), and [ContentView.swift](../../apps/macos/Carbon/Sources/ContentView.swift).

---

## 1. System Overview & Trust Boundaries

Carbon runs locally on macOS as a SwiftUI desktop client connected over loopback (`127.0.0.1:8765`) to a FastAPI Python backend backed by SQLite and a local Ollama daemon (`127.0.0.1:11434`).

```text
+--------------------------------------------------------------------------+
| UNTRUSTED EXTERNAL / LOCAL-HOST ATTACKERS & INPUTS                       |
| - Malicious web pages in local browsers (CSRF / WebSocket hijacking)     |
| - Untrusted prompt text or recalled historical memory strings            |
| - Other local processes running under the same macOS UID                 |
+------------------------------------+-------------------------------------+
                                     |
                                     v
============================================================================
[TRUST BOUNDARY 1: Loopback Network, Origin & Bearer Token]
- Backend binds strictly to loopback (Settings.api_host validated against
  LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"})
- HTTP Origin/client-host check via loopback_security_middleware
  (Origin: null and external origins BLOCKED with HTTP 403)
- WebSocket Origin validation before accept (blocks external/null -> 1008)
- 256-bit CSPRNG token (CARBON_API_TOKEN or 0600 data/.carbon_token)
  [macOS Keychain integration is DEFERRED per ADR-0007]
============================================================================
                                     |
                                     v
+--------------------------------------------------------------------------+
| CARBON BACKEND APPLICATION                                               |
|                                                                          |
|  [LLM Worker: qwen3.5:9b via Ollama on 127.0.0.1:11434]                  |
|   - Untrusted worker: Susceptible to hallucinations/prompt injections    |
|   - No direct tool-calling loop wired into streaming chat inference      |
+------------------------------------+-------------------------------------+
                                     |
                                     v
============================================================================
[TRUST BOUNDARY 2: GovernanceEngine, ToolRegistry & CMS]
- Default-deny GovernanceEngine authorization (P0-P4 vs A0-A3)
- Pydantic + JSON-schema tool validation prior to authorization
- Single-use argument-bound HITL approvals (open_application P2, procedural P3)
- Hash-chained JSONL audit log (data/audit.log) + secret redaction filter
============================================================================
                                     |
                                     v
+--------------------------------------------------------------------------+
| LOCAL STORAGE & CONTROLLED ACTIONS                                       |
| - SQLite database (data/carbon.db, unencrypted at rest)                  |
| - Built-in tools: system_info (P1), list_applications (P1),              |
|   open_application (P2, macOS APPROVED_APPS allowlist)                   |
+--------------------------------------------------------------------------+
```

---

## 2. STRIDE Threat Analysis & Verified Control Status

| Threat Category | Attack Vector | Verified Current Control | Limitations & Status |
| :--- | :--- | :--- | :--- |
| **Spoofing** | Rogue local process impersonates UI or calls local API | HTTP binds to `127.0.0.1:8765` (loopback enforced by `Settings`). Protected routes (`22` of `24` REST routes) check `verify_local_auth` via constant-time `secrets.compare_digest` against `CARBON_API_TOKEN` or `data/.carbon_token` (`secrets.token_urlsafe(32)`, mode `0600`). `/ws/chat` requires upgrade `Authorization: Bearer` or `"auth"` frame within `3.0s`. | **IMPLEMENTED** (POSIX `0600` file). **Limitation:** Same-UID processes (`UID=501`) can read `data/.carbon_token`; macOS Keychain is **DEFERRED** (`ADR-0007`). Also if `app.state.api_token` is unset in non-lifespan test setups, `verify_local_auth` returns `True`. |
| **Tampering** | Untrusted prompt/memory tries to execute actions or promote rules | Stored memories & conversation history injected into prompts are tagged untrusted historical data. `open_application` (`P2`) and REST `procedural` writes (`P3`) require trusted `DecisionSource` (`USER_UI` / `USER_API`) single-use approvals bound via SHA-256 `arguments_digest`. `ContentView` displays inline `approvalBannerView` (permission badge, impact summary, arguments JSON). ToolRegistry validates arguments against JSON schema first. | **IMPLEMENTED** for `ToolRegistry`, `open_application` fast-path, and procedural memory. **PARTIALLY IMPLEMENTED** for general chat tool use (no general LLM->tool-call loop exists yet). Note `approvalBannerView` shows inline text/JSON, not "red diffs". |
| **Repudiation** | Security-critical or tool actions occur without tamper-evident logs | Governance authorization checks, approvals, tool executions, model switches/rejections, memory-pressure updates, and explicit memory mutations write to append-only JSONL `audit.log` linked by SHA-256 `prev_hash` / `entry_hash` (`verify_audit_log_chain()` checked at startup). Tool checks record sorted `arg_keys` rather than raw argument values. | **IMPLEMENTED** with scope note: uses SHA-256 hash chaining (not asymmetric signatures). Routine unauthenticated `GET /`, `GET /api/v1/health`, `GET /api/v1/system/status`, or general chat text prompts are not each audited; only security/governance/lifecycle events are recorded. |
| **Information Disclosure** | Browser CSRF/WebSocket hijacking or secret leakage in logs | `CORSMiddleware` + `loopback_security_middleware` + `/ws/chat` restrict `Origin` strictly to `http://127.0.0.1:<port>` and `http://localhost:<port>`; external origins and **`Origin: null` are blocked** (`403` / `1008`). `TokenSanitizingFilter` and `sanitize_structured_data` mask bearer tokens, passwords, secrets, prompts, and memory content in logs/audits. | **IMPLEMENTED** for Origin/log redaction. **UNVERIFIED / MISSING:** SQLite (`data/carbon.db`) and `data/.carbon_token` are unencrypted at rest. |
| **Denial of Service** | Unbounded socket exhaustion, concurrent LLM runs, or OOM | CMS enforces single active inference lease (`acquire_inference_lease`) and `9216` MB budget. `/ws/chat` caps active sockets at `5` (`1013` beyond cap) and pre-auth lifetime at `3.0s` (`4401`). `inference_timeout_seconds` defaults to `300.0s` (model load/approval timeouts default to `60.0s`). `MemoryPressureMonitor` reports Mach pressure (`1/2/4`) to scale CMS context limits. | **IMPLEMENTED** for concurrency caps, timeouts, and CMS pressure handling. Real kernel-triggered OOM behavior remains limited to verified pressure-simulation tests. |
| **Elevation of Privilege** | LLM output claims higher autonomy (`A3`) or bypasses `P3`/`P4` gates | Autonomy (`A0`–`A3`) and permission tiers (`P0`–`P4`) are Python `StrEnum` policies enforced in `GovernanceEngine.authorize_action()` and `Settings`, unreachable by LLM text. `P4` always evaluates to `authorized=False`. `metadata.auto_promoted` is explicitly rejected (`403`). | **IMPLEMENTED** in Python code/enums (no SQLite policy tables exist). |

---

## 3. Local API & IPC Security Architecture

### 3.1 Token Generation, Storage, and Rotation (`IMPLEMENTED` / Keychain `DEFERRED`)

1. **Generation:** Uses `secrets.token_urlsafe(32)` when generating an ephemeral token in `_get_or_create_token()` (`src/carbon/main.py`).
2. **Storage:** Reads `CARBON_API_TOKEN` if set; otherwise reads/writes `settings.token_file_path` (default `data/.carbon_token`) via `_persist_token_atomic()` using `os.open(..., os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)`, `os.chmod(..., 0o600)`, and atomic `temp_path.replace(token_path)`.
3. **Immediate Token Rotation (`POST /api/v1/auth/rotate-token`):**
   - Atomically replaces `data/.carbon_token` and updates `app.state.api_token`.
   - **There is no transition grace period.** All currently active `/ws/chat` sessions holding pre-rotation tokens receive `{"event": "error", "message": "Authentication token rotated. Re-authentication required."}` and are immediately closed with code `4401`. Any active socket attempting a subsequent message is also checked against `app.state.api_token` and closed (`4401`) on mismatch.

### 3.2 WebSocket Authentication, Limits, and Starlette Scope Behavior (`IMPLEMENTED`)

- **No Query-String Token Auth:** `?token=...` is ignored/unaccepted.
- **Two Supported Auth Paths:**
  1. HTTP Upgrade header: `Authorization: Bearer <token>` verified before `accept()`.
  2. Initial JSON frame within **`WS_AUTH_TIMEOUT_SECONDS = 3.0`** seconds of `accept()`: `{"action": "auth", "token": "<token>"}`. Missing, invalid, or timed-out authentication closes the socket with `4401`.
- **Connection Cap:** `MAX_CONCURRENT_WS_CONNECTIONS = 5`; excess attempts close with `1013` (`Try Again Later`), with connection counts decremented in `finally:`.
- **Important Middleware Scope Fact:** Starlette/FastAPI `@app.middleware("http")` (`loopback_security_middleware`) only intercepts HTTP scopes and **does not run on WebSocket upgrades**. WebSocket security relies on binding to loopback (`127.0.0.1`), checking `Origin` inside `websocket_chat_endpoint` before `accept()` (`1008`), enforcing the `5`-connection limit, and requiring bearer authentication.

### 3.3 Origin & CORS Policy (`IMPLEMENTED`)

- Allowed origins are dynamically derived from `Settings.allowed_origins`: `{f"http://127.0.0.1:{api_port}", f"http://localhost:{api_port}"}`.
- **Wildcard `*` and `Origin: null` are strictly prohibited.** External origins and `Origin: null` are rejected with HTTP `403` on HTTP routes and WebSocket close `1008` on `/ws/chat` (verified in `tests/security/test_api_security.py`).

### 3.4 Outbound Network Policy & Credential Limitations

- **Local-Only Runtime:** Backend does not connect to external cloud services; `OllamaProvider` calls `CARBON_OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`).
- **Known Limitations & Deferred Controls:**
  - **macOS Keychain (`ADR-0007`):** **DEFERRED** — Not implemented in Swift or Python. POSIX `0600` `.carbon_token` protects against other Unix users but not same-UID local processes (see [keychain-implementation-status.md](../architecture/keychain-implementation-status.md)).
  - **Database Encryption at Rest:** Not implemented (`data/carbon.db` is standard SQLite).
  - **Voice & Remote Node Security:** **DEFERRED** (Stage 6.6) / **PLANNED** (`DeviceManager._connected_nodes` empty).
