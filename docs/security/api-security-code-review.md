# Carbon — API Security Implementation Code Review

**Stage 7.5 Alignment Status:** Updated against [main.py](../../src/carbon/main.py) (1,187 lines), [logging.py](../../src/carbon/core/logging.py) (129 lines), and [settings.py](../../src/carbon/config/settings.py) (251 lines).

---

## 1. Summary of Verified Security Mechanisms

| Security Control | Verified Mechanism | Code Reference | Status |
| :--- | :--- | :--- | :--- |
| **HTTP Loopback & Origin Middleware** | Rejects non-loopback `request.client.host` (`ALLOWED_CLIENT_HOSTS`) and unauthorized/`null` `Origin` with `403` | `main.py:226-258` | **IMPLEMENTED** |
| **REST Bearer Auth Coverage** | `verify_local_auth` (`secrets.compare_digest`) applied to `22` of `24` REST routes (`/` and `/api/v1/health` are public) | `main.py:259-294` | **IMPLEMENTED** *(See test-mode fallback note)* |
| **WebSocket Handshake & Query Removal** | Upgrade `Authorization: Bearer` validated before `accept()`; no `token` query parameter exists | `main.py:866-907` | **IMPLEMENTED** |
| **In-Socket Auth Timeout** | Bounded `asyncio.wait_for(..., timeout=3.0)` waiting for `{"action": "auth", "token": ...}`, else closes `4401` | `main.py:864, 911-948` | **IMPLEMENTED** |
| **WebSocket Origin & Cap** | Checks `Origin` before `accept()` (`1008`); caps `active_ws_connections` at `5` (`1013`), decrements in `finally:` | `main.py:862-882, 950, 1159-1163` | **IMPLEMENTED** |
| **Token Storage & Rotation** | `_persist_token_atomic` writes `0600` temp file + atomic `.replace`; `/api/v1/auth/rotate-token` immediately closes stale WS sessions (`4401`) | `main.py:130-144, 307-339, 1063-1074` | **IMPLEMENTED** *(Keychain **DEFERRED**)* |
| **Log & Audit Sanitization** | `TokenSanitizingFilter` + `sanitize_structured_data` redact tokens, credentials, prompts, and memory content | `logging.py:9-95, 121` | **IMPLEMENTED** |

---

## 2. Line-by-Line Inspection Notes

### 2.1 REST Authentication (`main.py:259-275` & Routes `281-858`)

`verify_local_auth` compares `HTTPAuthorizationCredentials.credentials` in constant time (`_constant_time_token_matches` -> `secrets.compare_digest`) against `app.state.api_token`:

```python
async def verify_local_auth(
    auth: HTTPAuthorizationCredentials | None = Security(security_scheme),
) -> bool:
    expected_token = getattr(app.state, "api_token", None)
    if not expected_token:
        return True
    if (
        not auth
        or auth.scheme.lower() != "bearer"
        or not _constant_time_token_matches(auth.credentials, expected_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing local authorization token.",
        )
    return True
```

- **Auth fallback caveat:** When `app.state.api_token` is `None`/falsy (such as ASGI test harnesses constructed without FastAPI `lifespan`), `verify_local_auth` returns `True`. Under normal `lifespan` startup (`main.py:199-200`), `_get_or_create_token()` always sets `app.state.api_token`.
- **Full route inventory:** All `24` REST routes (`2` public + `22` protected) are catalogued in [api-reference.md](../api-reference.md). Note that `POST /api/v1/system/memory-pressure` (`main.py:566-574`) and `POST /api/v1/system/shutdown` (`main.py:615-621`) also perform explicit handler-level `request.client.host` loopback checks in addition to middleware.

### 2.2 WebSocket `/ws/chat` Hardening (`main.py:862-1163`)

- **Important Starlette behavior:** `@app.middleware("http")` (`loopback_security_middleware`) intercepts HTTP requests only and **does not run on WebSocket upgrades**.
- Inside `websocket_chat_endpoint`, `main.py` directly checks `Origin` against `settings.allowed_origins` (`http://127.0.0.1:<port>` and `http://localhost:<port>`, blocking external origins and `Origin: null` with code `1008`) and enforces `MAX_CONCURRENT_WS_CONNECTIONS = 5` (`1013`) before `websocket.accept()`.
- Query-string `?token=...` is ignored. Authentication requires either `Authorization: Bearer <token>` on upgrade or `{"action": "auth", "token": "<token>"}` within `WS_AUTH_TIMEOUT_SECONDS = 3.0`, otherwise closing with code `4401`.

### 2.3 Token Persistence & Immediate WebSocket Revocation (`main.py:130-185, 307-339`)

- Token resolution prefers `settings.api_token` (`CARBON_API_TOKEN`); otherwise reads/creates `settings.token_file_path` (default `data/.carbon_token`) with mode `0o600` using `os.open(..., 0o600)`, `os.chmod(..., 0o600)`, and atomic `temp_path.replace(token_path)`.
- `POST /api/v1/auth/rotate-token` issues a new 32-byte URL-safe token, updates `app.state.api_token`, and iterates `authenticated_sockets` to send `{"event": "error", "message": "Authentication token rotated. Re-authentication required."}` and immediately close stale sockets with code `4401` (no 30-second grace period). Each incoming WebSocket action (`main.py:1064-1074`) also verifies `session_token` against `app.state.api_token`.

### 2.4 Credential & Content Redaction (`logging.py:9-95, 121-129`)

- `configure_logging()` attaches `TokenSanitizingFilter` to stdout logging, masking `Bearer ...`, `'token': ...`, `'password': ...`, `token=...`, `api_key`, `secret`, and `authorization`, and sets `uvicorn.access`, `httpcore`, and `httpx` to `WARNING`.
- `sanitize_structured_data()` recursively redacts `REDACTED_KEY_TOKENS` (including `token`, `password`, `secret`, `prompt`, `raw_prompt`, `content`, `memory_content`, `query`) before `audit.log` persistence.

---

## 3. Remaining Security Gaps

- **Keychain Deferral (`ADR-0007`):** Token storage uses `CARBON_API_TOKEN` or POSIX `0600` `.carbon_token`, which does not isolate secrets from other processes under the same macOS UID.
- **Unverified REST Tests:** `POST /api/v1/tasks/{task_id}/cancel`, `GET /api/v1/tools`, `GET /api/v1/system/status` lack direct REST assertions in `tests/`, and `GET /api/v1/approvals` only has an unauthenticated `401` check in `tests/`.
