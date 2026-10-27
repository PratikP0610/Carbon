# Carbon 1.0.0 — API Security Verification Report

**Document Date:** 2026-10-28  
**Author:** Google Antigravity (Primary Development Agent)  
**Target:** Local Application API (`src/carbon/main.py`)  
**Status:** Hardened & Verified (Passes All 8 Automated Security Tests)  

---

## 1. Executive Summary

A comprehensive security audit of Carbon's local application API and inter-process communication (IPC) contracts was performed. All components were evaluated against unauthorized local process access, cross-origin request forgery (CSRF), credential leakage vectors, and unauthenticated WebSocket hijacking.

In accordance with user directives, **WebSocket query-string token authentication has been completely removed**. Authentication is now strictly confined to HTTP upgrade headers (`Authorization: Bearer <token>`) or a bounded in-socket authentication frame with strict connection limits and short timeouts.

The security architecture was actively hardened and verified via 8 automated tests in [test_api_security.py](file:///Users/pratikpradeshi/Carbon/tests/security/test_api_security.py).

---

## 2. Complete Elimination of WebSocket Query-String Authentication

Passing authentication tokens via URL query parameters (`ws://127.0.0.1:8765/ws/chat?token=...`) is an insecure antipattern that exposes secrets:

### 2.1 Threat Vectors of Query-String Authentication
1. **Plaintext Log Capture:** Web server access logs, reverse proxies, and local logging capture full request URLs including query strings. Log redaction cannot prevent upstream proxies or OS utilities from capturing query strings.
2. **Process Inspection:** In environments where client processes spawn CLI helpers with full URLs, command-line arguments are visible to other local processes via `ps aux`.
3. **Browser History & Referer Leakage:** Browsers persist visited WebSocket URLs in history, and external sub-resource requests can leak tokens via HTTP `Referer` headers.

### 2.2 Implemented Hardened WebSocket Authentication Architecture
1. **Zero Query-String Token Acceptance:** The `token` query parameter was completely stripped from `websocket_chat_endpoint(websocket: WebSocket)`. Passing `?token=...` is completely ignored for authentication; if no valid header or in-socket frame is provided, the connection is forcibly terminated.
2. **Preferred Transport — Handshake Header Authentication:**
   - Supported natively by URLSession in the macOS SwiftUI client.
   - The client sends the standard header during the HTTP Upgrade handshake:
     ```http
     GET /ws/chat HTTP/1.1
     Host: 127.0.0.1:8765
     Upgrade: websocket
     Connection: Upgrade
     Authorization: Bearer <256-bit-token>
     ```
   - Validated instantly prior to socket acceptance.
3. **Secondary Transport — Bounded In-Socket Authentication Frame:**
   - If no `Authorization` header is present (e.g., in environments where browser WebSocket APIs forbid custom headers), the server accepts the socket into an unauthenticated pre-auth quarantine state.
   - **Short Timeout:** The client must send an authentication frame within **3.0 seconds** (`WS_AUTH_TIMEOUT_SECONDS = 3.0`):
     ```json
     {"action": "auth", "token": "<256-bit-token>"}
     ```
   - **Enforced Connection Limits:** To prevent resource exhaustion attacks against the pre-auth quarantine, active concurrent WebSocket connections are strictly limited to **5** (`MAX_CONCURRENT_WS_CONNECTIONS = 5`). Excess connections are immediately rejected with WebSocket code `1013 (Try Again Later)`.
   - **Safe Failure Termination:** If authentication fails or times out, the server closes the connection with code `4401 (Unauthorized)` or `1008 (Policy Violation)` and decrements the connection counter within a reliable `finally:` block.

---

## 3. Strict Rejection of `Origin: null` and Web CSRF

Many local daemon servers erroneously include `"null"` in their CORS `allow_origins` list under the mistaken belief that native desktop webviews require it.

### The `Origin: null` Exploitation Vector
1. When a browser executes a sandboxed iframe (`<iframe sandbox="allow-scripts">`), an opaque origin is generated, and the browser sends `Origin: null`.
2. When a script runs inside a `data:` URL, `file://` URL, or cross-origin redirect, the browser sends `Origin: null`.
3. If a local API bound to `127.0.0.1` permits `Origin: null`, **any malicious website visited by the user in Google Chrome or Safari** can instantiate a hidden sandboxed iframe, issue cross-origin requests to `http://127.0.0.1:8765`, and completely bypass CORS protections.

### Carbon Enforced Policy
- Carbon **strictly prohibits `Origin: null`**.
- `ALLOWED_ORIGINS` is restricted strictly to loopback:
  ```python
  ALLOWED_ORIGINS = {
      "http://127.0.0.1:8765",
      "http://localhost:8765",
  }
  ```
- Incoming requests bearing `Origin: null` or untrusted web origins (e.g., `https://evil.com`) are dropped immediately with **HTTP 403 Forbidden** via loopback security middleware.
- Handshake WebSocket connections verifying untrusted origins are terminated with WebSocket code `1008 (Policy Violation)`.

---

## 4. Comprehensive Audit of Implemented Security Controls

### 4.1 REST Authentication & Coverage
- All sensitive endpoints (`/api/v1/models`, `/api/v1/models/switch`, `/api/v1/tasks`, `/api/v1/tools`, `/api/v1/system/status`, `/api/v1/auth/rotate-token`) enforce the `verify_local_auth` dependency.
- Requests lacking an `Authorization: Bearer <token>` header or providing an invalid token receive **HTTP 401 Unauthorized**.
- Only the unprivileged root diagnostic `/` and basic liveness `/api/v1/health` are accessible without authorization.

### 4.2 Token Generation, Storage, and Rotation
- **Generation:** Generated using Python's `secrets.token_urlsafe(32)` providing 256 bits of cryptographic entropy.
- **Storage:** Persisted to `data/.carbon_token` using atomic write semantics with POSIX file mode **`0600`** (`-rw-------`), ensuring only the current local user account can read the secret.
- **Rotation Endpoint (`POST /api/v1/auth/rotate-token`):**
  - Requires valid authorization with the active token.
  - Atomically generates a new token, updates `data/.carbon_token` with `0600` permissions, and updates runtime server state.
  - Immediately invalidates the old token for subsequent requests.

### 4.3 Multi-Layer Credential Redaction in Logging
- Configured in [logging.py](file:///Users/pratikpradeshi/Carbon/src/carbon/core/logging.py) via `TokenSanitizingFilter`.
- Intercepts all log records and applies regular expression masking to sanitize Bearer tokens, token parameters, and passwords:
  ```
  "Authorization: Bearer [REDACTED_TOKEN]"
  "'token': '[REDACTED_TOKEN]'"
  "'password': '[REDACTED_CREDENTIAL]'"
  ```
- Access loggers (`uvicorn.access`, `httpx`, `httpcore`) are dampened to `WARNING` severity.
- **Critical Policy:** Log sanitization is maintained as a defense-in-depth safeguard, NOT as a substitute for eliminating insecure authentication transports.

---

## 5. Automated Security Test Results

All 8 tests in [test_api_security.py](file:///Users/pratikpradeshi/Carbon/tests/security/test_api_security.py) pass cleanly in 0.30s:

| Test Case | Objective | Result |
| :--- | :--- | :--- |
| `test_rest_unauthorized_access_rejected` | Proves missing or invalid bearer token receives HTTP 401 | **PASSED** |
| `test_rest_authorized_access_succeeds` | Proves valid bearer token successfully authenticates | **PASSED** |
| `test_origin_validation_and_cors` | Proves `Origin: null` and `evil.com` are blocked with HTTP 403 | **PASSED** |
| `test_token_rotation_and_file_permissions` | Proves token rotation invalidates old token and maintains 0600 permissions | **PASSED** |
| `test_websocket_origin_and_auth_rejection` | Proves unauthorized WebSocket connections are rejected | **PASSED** |
| `test_websocket_query_string_token_rejected` | **Proves query-string token authentication is rejected with 4401** | **PASSED** |
| `test_websocket_handshake_header_auth` | **Proves standard Authorization header in handshake connects cleanly** | **PASSED** |
| `test_token_sanitization_in_logging` | Proves sensitive tokens are masked as `[REDACTED_TOKEN]` | **PASSED** |
