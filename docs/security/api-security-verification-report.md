# Carbon 1.0.0 — API Security Verification Report

**Document Date:** 2026-10-28  
**Author:** Google Antigravity (Primary Development Agent)  
**Target:** Local Application API (`src/carbon/main.py`)  
**Status:** Complete Security Audit & Verification  

---

## 1. Executive Summary

A comprehensive security audit of Carbon's local application API and inter-process communication (IPC) contracts was performed. All components were evaluated against unauthorized local process access, cross-origin request forgery (CSRF), token leakage vectors, and unauthenticated WebSocket hijacking.

The security architecture was actively hardened and verified via automated tests in [test_api_security.py](file:///Users/pratikpradeshi/Carbon/tests/security/test_api_security.py).

---

## 2. Deep Dive: High-Risk Security Vectors

### 2.1 WebSocket Query-String Authentication (`?token=...`)
Passing authentication tokens via URL query parameters in WebSocket handshakes is a widespread convenience pattern that introduces critical attack vectors:

- **Vulnerabilities of Query-String Authentication:**
  1. **Log Exposure:** Web server access logs, reverse proxies, and local diagnostic logging record full URLs including query strings by default. A bearer token passed in the query string is permanently captured in plaintext logs.
  2. **Process Inspection:** In environments where client processes spawn CLI helpers with full URLs, command-line arguments are visible to other local processes via `ps aux`.
  3. **Browser History & Referer Leakage:** Browsers persist visited WebSocket URLs in session history, and external sub-resource requests can leak tokens via HTTP `Referer` headers.
- **Carbon Mitigations:**
  - **Primary Transport:** The native macOS SwiftUI client passes the bearer token via the standard HTTP `Authorization: Bearer <token>` header during the WebSocket upgrade handshake.
  - **In-Socket Authentication Frame:** For environments where custom headers during WebSocket upgrade are restricted by platform APIs, Carbon supports a 5-second in-socket authentication frame:
    ```json
    {"action": "auth", "token": "<256-bit-token>"}
    ```
    If valid authentication is not confirmed within 5.0 seconds of socket acceptance, the server forcibly terminates the connection with WebSocket code `4401`.
  - **Query-String Policy:** Query-string tokens are accepted solely as an ephemeral legacy fallback on loopback IPv4 and are immediately stripped from any internal logging.

### 2.2 Security Implications of `null` Origin Allowance
Many local daemon servers erroneously include `"null"` in their CORS `allow_origins` list under the mistaken belief that native desktop webviews require it.

- **The `Origin: null` Exploitation Vector:**
  1. When a browser executes a sandboxed iframe (`<iframe sandbox="allow-scripts">`), an opaque origin is generated, and the browser sends `Origin: null`.
  2. When a script runs inside a `data:` URL, `file://` URL, or cross-origin redirect, the browser sends `Origin: null`.
  3. If a local API bound to `127.0.0.1` permits `Origin: null`, **any malicious website visited by the user in Google Chrome or Safari** can instantiate a hidden sandboxed iframe, issue cross-origin requests to `http://127.0.0.1:8765`, and completely bypass CORS protections.
- **Carbon Enforced Policy:**
  - Carbon **strictly prohibits `Origin: null`**.
  - `ALLOWED_ORIGINS` is restricted strictly to:
    ```python
    ALLOWED_ORIGINS = {
        "http://127.0.0.1:8765",
        "http://localhost:8765",
    }
    ```
  - Incoming requests bearing `Origin: null` or untrusted web origins (e.g. `https://malicious-site.com`) are dropped immediately with **HTTP 403 Forbidden**.
  - Verified by automated test: `test_origin_validation_and_cors` in [test_api_security.py](file:///Users/pratikpradeshi/Carbon/tests/security/test_api_security.py#L58-L86).

---

## 3. Audit of Implemented Security Mechanisms

### 3.1 REST Authentication & Unauthorized Access Rejection
- All sensitive endpoints (`/api/v1/models`, `/api/v1/models/switch`, `/api/v1/tasks`, `/api/v1/tools`, `/api/v1/system/status`, `/api/v1/auth/rotate-token`) enforce the `verify_local_auth` dependency.
- Requests lacking an `Authorization: Bearer <token>` header or providing an invalid token receive **HTTP 401 Unauthorized**.
- Only the unprivileged root diagnostic `/` and basic liveness `/api/v1/health` are accessible without authorization.

### 3.2 Token Generation, Storage, and Rotation
- **Generation:** Generated using Python's `secrets.token_urlsafe(32)` providing 256 bits of cryptographic entropy.
- **Storage:** Persisted to `data/.carbon_token` using atomic write semantics with POSIX file mode **`0600`** (`-rw-------`), ensuring only the current local user account can read the secret.
- **Rotation:** Verified endpoint `POST /api/v1/auth/rotate-token`:
  - Requires valid authorization with the active token.
  - Atomically generates a new token, updates `data/.carbon_token` with `0600` permissions, and updates runtime server state.
  - Immediately invalidates the old token for subsequent requests.

### 3.3 Token and Sensitive Data Logging Sanitization
- Configured in [logging.py](file:///Users/pratikpradeshi/Carbon/src/carbon/core/logging.py) via `TokenSanitizingFilter`.
- Intercepts all log records and applies regular expression masking to sanitize Bearer tokens, token parameters, and passwords:
  ```
  "Authorization: Bearer [REDACTED_TOKEN]"
  "'token': '[REDACTED_TOKEN]'"
  "'password': '[REDACTED_CREDENTIAL]'"
  ```
- Third-party access loggers (`uvicorn.access`, `httpx`, `httpcore`) are dampened to `WARNING` severity to prevent accidental query parameter logging.

---

## 4. Automated Security Test Results

All six tests in `tests/security/test_api_security.py` execute cleanly and pass without warnings:

1. `test_rest_unauthorized_access_rejected`: Verified rejection of missing and invalid tokens with HTTP 401.
2. `test_rest_authorized_access_succeeds`: Verified valid bearer token permits access.
3. `test_origin_validation_and_cors`: Verified rejection of external web origins and `Origin: null` with HTTP 403 Forbidden.
4. `test_token_rotation_and_file_permissions`: Verified token rotation invalidates old token, accepts new token, and enforces `0600` file permissions on disk.
5. `test_websocket_origin_and_auth_rejection`: Verified WebSocket drops invalid origins with WS code 1008 and rejects invalid auth frames.
6. `test_token_sanitization_in_logging`: Verified automatic masking of sensitive bearer tokens in log output.
