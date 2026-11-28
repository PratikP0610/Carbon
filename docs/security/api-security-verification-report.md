# Carbon — API Security Verification Report

**Stage 7.5 Alignment Status:** Updated against [main.py](../../src/carbon/main.py), [settings.py](../../src/carbon/config/settings.py), [test_api_security.py](../../tests/security/test_api_security.py), [test_memory_api_security.py](../../tests/security/test_memory_api_security.py), and `tests/security/test_governance_and_tools_security.py`.

---

## 1. Executive Summary

Carbon's local HTTP and `/ws/chat` interfaces enforce loopback binding, strict `Origin` rejection (`Origin: null` and external origins blocked), constant-time bearer-token checks on `22` protected REST endpoints, `3.0s` WebSocket auth timeout without query-string tokens, immediate stale-socket revocation on token rotation (`4401`), single-use argument-bound human approvals (`P2`/`P3`), and SHA-256 hash-chained `audit.log` sanitization.

---

## 2. Verified WebSocket & Origin Hardening (`IMPLEMENTED`)

### 2.1 Removal of Query-String Token Authentication (`main.py:866-949`)

- `websocket_chat_endpoint(websocket: WebSocket)` defines **no** `token` query parameter; `?token=...` has no authentication effect (`test_websocket_query_string_token_rejected`).
- Accepted transports:
  1. HTTP Upgrade header `Authorization: Bearer <token>` verified prior to `websocket.accept()`.
  2. First WebSocket text frame within `WS_AUTH_TIMEOUT_SECONDS = 3.0` (`{"action": "auth", "token": "<token>"}`); timeouts or invalid frames close with `4401`.
- Concurrent connections are capped at `MAX_CONCURRENT_WS_CONNECTIONS = 5` (`1013` beyond cap), cleaned up inside `finally:`.

### 2.2 Blocking `Origin: null` & External Origins (`main.py:226-258, 872-876`, `settings.py:231-237`)

- Permitted origins come from `Settings.allowed_origins`:
  ```python
  {f"http://127.0.0.1:{self.api_port}", f"http://localhost:{self.api_port}"}
  ```
- HTTP requests with external origins or `Origin: null` return HTTP `403` via `loopback_security_middleware`; `/ws/chat` rejects unauthorized origins with WebSocket `1008`.
- **Middleware Boundary Note:** Starlette's `@app.middleware("http")` does not wrap WebSocket upgrades; WebSocket origin filtering is enforced inside `websocket_chat_endpoint` before `accept()`, relying on loopback server bind for socket isolation.

---

## 3. Token Lifecycle & Logging Verification (`IMPLEMENTED` / Keychain `DEFERRED`)

- **Storage:** `CARBON_API_TOKEN` environment variable or `data/.carbon_token` written atomically with mode `0600` (`_persist_token_atomic`). Native macOS Keychain integration (`ADR-0007`) remains **DEFERRED**.
- **Immediate Rotation:** `POST /api/v1/auth/rotate-token` writes a new token and immediately disconnects active pre-rotation `/ws/chat` sessions (`4401`) rather than keeping a grace period.
- **Test-Mode Note:** If `app.state.api_token` is unset (`None`), `verify_local_auth` returns `True`; under normal FastAPI `lifespan`, `app.state.api_token` is always initialized.
- **Log Redaction:** `TokenSanitizingFilter` (`logging.py`) masks bearer tokens, secrets, and passwords in stdout logs.

---

## 4. Security Test Evidence Inventory

*(Historical note: an earlier Oct 28 report cited 8 tests in `test_api_security.py`; the current repository contains 12 tests in `test_api_security.py` plus dedicated security suites in `test_memory_api_security.py` and `test_governance_and_tools_security.py`. Stage 7.5 did not re-execute live/full test suites — test statuses below cite verified test definitions in those files alongside Stage 6.8/6.9 manual verification.)*

- **[test_api_security.py](../../tests/security/test_api_security.py) (12 tests):**
  - `test_rest_unauthorized_access_rejected`, `test_rest_authorized_access_succeeds`, `test_origin_validation_and_cors` (`Origin: null` + `https://malicious-site.com`), `test_token_rotation_and_file_permissions` (`0o600`), `test_websocket_query_string_token_rejected`, `test_websocket_handshake_header_auth`, `test_websocket_origin_and_auth_rejection`, `test_token_sanitization_in_logging`, `test_models_registry_and_switch_authorization_codes`, `test_models_refresh_security_origin_and_allowlist_enforcement`, `test_models_switch_failure_status_mappings_drain_unload_load`, `test_metrics_endpoint_auth_origin_and_zero_pii_leakage`.
- **[test_memory_api_security.py](../../tests/security/test_memory_api_security.py) (4 tests):**
  - Covers `401` rejections on memory/conversation endpoints, 2-step `procedural` approval digest binding + replay/tamper/model-spoof `403` controls, `PATCH` audit non-leakage + pagination + ambiguous forget + conversation cascade wipe, and expired memory/task-payload masking.
- **`tests/security/test_governance_and_tools_security.py`:**
  - Covers `ToolRegistry` default-deny + `open_application` HITL approvals, WebSocket stale-token revocation after `/api/v1/auth/rotate-token`, and tool/approval auth checks.

### Unverified Automated Test Paths

- Direct REST automated assertions were not found in `tests/` for `POST /api/v1/tasks/{task_id}/cancel`, `GET /api/v1/tools`, or `GET /api/v1/system/status`; `GET /api/v1/approvals` only has an unauthenticated `401` assertion in `tests/`.
