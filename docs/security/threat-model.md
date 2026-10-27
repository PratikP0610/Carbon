# Carbon Security Threat Model & Security Controls

## 1. System Overview & Trust Boundaries

Carbon executes locally on macOS with access to user files, applications, and system state through controlled tools.

```
+──────────────────────────────────────────────────────────────────────────+
| UNTRUSTED EXTERNAL WORLD                                                 |
| - Malicious web documents / untrusted prompt injection payloads          |
| - Unauthorized network actors on local LAN                               |
+──────────────────────────────────────────────────────────────────────────+
                                     │
                                     ▼
════════════════════════════════════════════════════════════════════════════
[TRUST BOUNDARY 1: Network & Socket Isolation]
- Loopback-only socket binding strictly to 127.0.0.1:8765
- Strict Origin enforcement (NO wildcard CORS; loopback & native only)
- Cryptographic CSPRNG Bearer Token with POSIX 0600 file protection
- Strict WebSocket handshake authentication
════════════════════════════════════════════════════════════════════════════
                                     │
                                     ▼
+──────────────────────────────────────────────────────────────────────────+
| CARBON BACKEND APPLICATION                                               |
|                                                                          |
|  [LLM Worker: Qwen 3.5 9B via Ollama]                                    |
|   - Untrusted worker: Susceptible to hallucinations and prompt injections |
|   - Zero native execution privileges: No direct disk, shell, or socket  |
|                                                                          |
════════════════════════════════════════════════════════════════════════════
[TRUST BOUNDARY 2: Governance Engine & Carbon Core Authority]
- Pydantic v2 strict input schema validation
- P0-P4 Permission classification & user authorization modal
- Immutable append-only audit log with credential sanitization
════════════════════════════════════════════════════════════════════════════
                                     │
                                     ▼
+──────────────────────────────────────────────────────────────────────────+
| TRUSTED COMPUTING BASE (TCB)                                             |
|  - Carbon Core Orchestrator                                              |
|  - Governance Engine                                                     |
|  - Native macOS SwiftUI Frontend                                         |
|  - Approved Tool Handlers (macOS System Frameworks, Keychain Services)   |
+──────────────────────────────────────────────────────────────────────────+
```

---

## 2. STRIDE Threat Analysis & Mitigations

| Threat Category | Potential Attack Vector | Impact | Carbon Mitigation Controls |
|---|---|---|---|
| **Spoofing** | Rogue local processes attempting to impersonate frontend or invoke backend API. | Unauthorized command dispatch. | **Loopback-Only + Ephemeral Bearer Token:** Backend binds strictly to `127.0.0.1:8765`. Generates a 256-bit CSPRNG token (`secrets.token_urlsafe(32)`) on boot stored in `data/.carbon_token` with POSIX permissions `0600`. Non-authenticated requests are rejected with 401 Unauthorized. |
| **Tampering** | Indirect prompt injection via ingested document or file instructing LLM to delete files or run malicious actions. | Unintended side effects. | **Model Worker Containment:** LLMs have zero inherent authority. All proposed actions must match strict Pydantic schemas and pass through the Governance Engine. Consequential P3/P4 operations enforce human-in-the-loop modal dialogs showing arguments in red diffs. |
| **Repudiation** | Actions executed without attributable audit history. | Inability to diagnose actions. | **Append-Only Audit Logging:** Every permission decision, tool execution, parameter set, and outcome is logged to `data/audit.log` with UTC timestamp and signature. |
| **Information Disclosure** | Local API accessed across LAN, or private credentials leaked via logs. | Privacy breach, token exposure. | **Origin/CORS Filtering & Redaction:** Wildcard origins (`*`) are strictly prohibited. Permitted origins are restricted to `http://127.0.0.1:8765`, `http://localhost:8765`, and `null` (native app webview). Audit logger automatically sanitizes `Authorization` headers, passwords, and tokens. |
| **Denial of Service** | Infinite reasoning loops or multiple models exhausting 16 GB unified RAM. | System freeze, macOS OOM crash. | **Single-Model Concurrency Mutex:** CMS serializes chat LLM inference with `asyncio.Lock`. Hard per-task timeouts (60s). Dynamic memory pressure monitoring (`memorystatus_vm_pressure_level`). |
| **Elevation of Privilege** | LLM output attempts to grant itself higher autonomy (e.g. A1 -> A3) or override tool permission tier. | Policy bypass. | **Code-Level Policy Enforcement:** Autonomy and permission matrices are immutable Python class constants and SQLite policies, unreachable and unmodifiable by LLM text generation. |

---

## 3. Local API Security Architecture

### 3.1 Token Generation, Storage, and Rotation
1. **Generation:** During application startup, Carbon Core generates a 256-bit cryptographic token using Python's `secrets.token_urlsafe(32)`.
2. **Storage:** Written to `data/.carbon_token` using atomic write semantics (`open` with `O_CREAT | O_WRONLY | O_TRUNC` and `os.chmod(0o600)`), ensuring only the current local user account can read the token.
3. **Rotation:**
   - Client can issue `POST /api/v1/auth/rotate-token` providing the active token.
   - Core generates a new token, updates `data/.carbon_token`, maintains a 30-second transition grace period for active WebSocket reconnects, then revokes the previous token.

### 3.2 WebSocket Handshake Authentication
To prevent unauthorized WebSocket hijacking by rogue local scripts:
- When a WebSocket client connects to `/ws/chat`, it must provide the bearer token via:
  1. The `Authorization` header during upgrade (`Bearer <token>`), OR
  2. The query parameter (`ws://127.0.0.1:8765/ws/chat?token=<token>`), OR
  3. An initial authentication frame within **5 seconds** of socket acceptance:
     ```json
     {"action": "auth", "token": "<token>"}
     ```
- If valid authentication is not confirmed within 5 seconds, the socket is forcibly closed with code `4401 (Unauthorized)`.

### 3.3 Strict Origin & CORS Policy
Unlike typical web servers, Carbon is a local personal assistant. Permitting arbitrary browser web pages to send requests to `127.0.0.1:8765` poses a severe Cross-Origin Request Forgery (CSRF) risk.

**Enforced Policy:**
- CORS `allow_origins` is restricted strictly to:
  - `http://127.0.0.1:8765`
  - `http://localhost:8765`
  - `null` (Origin header sent by native macOS Safari webviews and desktop binaries)
- Requests carrying external browser origins (e.g. `https://malicious-website.com`) are dropped immediately with HTTP 403 Forbidden.

### 3.4 Outbound Network Egress Policy
- Carbon Core operates in an **isolated outbound network posture**.
- No unsolicited outbound cloud connections are established by the backend.
- Any future tool requiring external network access (e.g., weather API, RSS reader) must:
  1. Be registered with **Permission Level P3**.
  2. State its destination URL and parameters in the confirmation prompt.
  3. Require affirmative user confirmation prior to socket establishment.

### 3.5 Credential Handling & Keychain Integration
- **Zero Plaintext Secrets:** Passwords, API tokens, and private keys must never be stored in plaintext configuration files (`.env`), database tables, or logs.
- **macOS Keychain Services:** All credential storage must utilize the native macOS Keychain via `Security.framework` (`SecItemAdd`, `SecItemCopyMatching`), ensuring credentials are protected by the user's login keychain and Secure Enclave.
