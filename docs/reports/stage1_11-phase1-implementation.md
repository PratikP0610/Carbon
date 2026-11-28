# Carbon 1.0.0 — Stage 1.11 Phase 1 Implementation Report
## Python Backend Programmatic Graceful Shutdown & Server Retention

**Task ID:** C-1.11-P1  
**Phase:** Python Backend Programmatic Graceful Shutdown  
**Date:** November 2, 2026  
**Platform:** MacBook Air M5, 16GB unified memory, macOS 27.0.1  
**Target Repository:** `PratikP0610/Carbon`  
**Active Branch:** `backup/carbon-tested-phase1-2026-10-29`  
**HEAD Commit:** `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`  
**Status:** IMPLEMENTATION COMPLETE & VERIFIED (Phase 1 Approved Scope Only)

---

## 1. Executive Summary

In accordance with the **Stage 1.11 Desktop Lifecycle Architecture (v4 Safety Corrections)** and project owner authorization, Stage 1.11 Phase 1 has been implemented and verified. This phase introduces safe, graceful programmatic shutdown capability to the existing Carbon Python backend (`src/carbon/main.py`), allowing the upcoming macOS desktop process supervisor to terminate the backend cleanly when Carbon owns the backend process.

### Core Achievements
1. **Explicit `uvicorn.Server` Retention:** Refactored backend entry point `main()` in `src/carbon/main.py` from generic `uvicorn.run(...)` to explicit configuration and instantiation via `uvicorn.Server(config=config)`. Retains reference on `app.state.uvicorn_server` and module-level `server_instance`.
2. **Authenticated Loopback Shutdown Endpoint (`POST /api/v1/system/shutdown`):**
   - Protected by `verify_local_auth` (bearer token validation) and strict loopback client address checks (`127.0.0.1`, `::1`, `localhost`, `testclient`).
   - Retrieves the retained managed server instance.
   - Returns **HTTP 503** if no managed Uvicorn server is bound (e.g. running under external test harnesses or generic ASGI runners).
   - Schedules `server.should_exit = True` via FastAPI `BackgroundTasks`, guaranteeing the HTTP 200 payload is fully written to the network socket before server shutdown commences.
   - Idempotently handles redundant shutdown requests without race conditions or errors.
3. **Strict Compliance with v4 Safety Invariant (Zero Self-SIGTERM):**
   - Completely eliminates self-termination calls (`os.kill(os.getpid(), signal.SIGTERM)`).
   - Guaranteed immunity against terminating external test runners, ASGI hosts, or unowned processes.
4. **Full Lifespan Teardown Guaranteed:**
   - Setting `server.should_exit = True` causes Uvicorn to exit its event loop, automatically triggering the ASGI lifespan exit block which awaits `core.shutdown()` and cleanly frees memory, SQLite databases, and background tasks.
5. **100% Test Suite Verification:**
   - Test suite expanded from **80 baseline tests to 86 tests** (+6 new integration tests).
   - Full test suite execution: **86 passed, 0 failures in 5.11s**.
   - Ruff linting: 100% clean.
   - Git whitespace diff: 100% clean.

---

## 2. Pre-Implementation Inspection Audit

Prior to making any modifications, a pre-implementation inspection was conducted:
1. **`src/carbon/main.py`:**
   - Verified existing FastAPI initialization, CORS settings, and `lifespan` context manager.
   - Observed that `lifespan` on exit already performs `await core.shutdown()` and unsets `core`.
   - Identified that `main()` previously called `uvicorn.run("carbon.main:app", ...)`, which created an internal Uvicorn server inaccessible to endpoints.
2. **Authentication & Middleware:**
   - Inspected `loopback_security_middleware` which enforces access from `{"127.0.0.1", "localhost", "::1", "testclient"}`.
   - Inspected `verify_local_auth` which enforces `app.state.api_token`.
3. **Integration Test Suite:**
   - Inspected `tests/integration/test_api_endpoints.py`, verifying existing test patterns using `httpx.AsyncClient` with `httpx.ASGITransport`.
4. **Git Repository Status:**
   - Branch: `backup/carbon-tested-phase1-2026-10-29`
   - Commit: `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`
   - Working tree: Checked existing unstaged performance optimization files from Stage 1.10; preserved intact without regression.

---

## 3. Implementation Details

### 3.1 Explicit Server Retention in `src/carbon/main.py`

At module level:
```python
# Retained Uvicorn server instance for graceful programmatic termination
server_instance: uvicorn.Server | None = None
```

In `create_app()`:
```python
app = FastAPI(
    title="Carbon Local API",
    version="1.0.0",
    description="Local loopback API for Carbon Personal AI Assistant",
    lifespan=lifespan,
)
app.state.uvicorn_server = None
```

In `main()`:
```python
def main():
    """Main CLI entry point for running Carbon."""
    global server_instance
    settings = get_settings()
    config = uvicorn.Config(
        app=app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        reload=False,
    )
    server_instance = uvicorn.Server(config=config)
    app.state.uvicorn_server = server_instance
    server_instance.run()
```

### 3.2 Programmatic Graceful Shutdown Endpoint

In `create_app()` REST endpoints section:
```python
@app.post("/api/v1/system/shutdown", dependencies=[Depends(verify_local_auth)])
async def system_shutdown(background_tasks: BackgroundTasks, request: Request):
    """Gracefully request Uvicorn server termination.

    Restricted strictly to authenticated local loopback connections.
    """
    # 1. Enforce local loopback origin strictly
    client_host = request.client.host if request.client else "unknown"
    allowed_hosts = {"127.0.0.1", "localhost", "::1", "testclient"}
    if client_host not in allowed_hosts:
        logger.error(f"Rejected shutdown attempt from non-loopback address: {client_host}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Shutdown restricted strictly to local loopback.",
        )

    # 2. Retrieve managed Uvicorn server instance
    server: uvicorn.Server | None = (
        getattr(app.state, "uvicorn_server", None) or server_instance
    )

    if not server:
        logger.warning("HTTP shutdown rejected: no managed Uvicorn server instance registered.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Graceful programmatic shutdown unavailable: no managed Uvicorn server instance.",
        )

    # 3. Check if already shutting down (idempotent / duplicate safety)
    if getattr(server, "should_exit", False):
        logger.info("System shutdown already in progress.")
        return {
            "status": "already_shutting_down",
            "message": "Carbon backend is already stopping.",
        }

    logger.info("Authenticated system shutdown requested via loopback HTTP.")

    def _trigger_graceful_stop():
        logger.info("Setting Uvicorn server.should_exit = True")
        server.should_exit = True

    # 4. Schedule server.should_exit = True via FastAPI BackgroundTasks
    # BackgroundTasks execute strictly AFTER the HTTP 200 response is flushed to the network socket
    background_tasks.add_task(_trigger_graceful_stop)

    return {
        "status": "shutting_down",
        "message": "Carbon backend is terminating gracefully.",
    }
```

### 3.3 Lifespan Teardown Interaction
The shutdown lifecycle operates through the following sequence:
$$\text{HTTP Request} \longrightarrow \text{HTTP 200 Sent} \longrightarrow \text{BackgroundTask fires: } \texttt{server.should\_exit = True} \longrightarrow \text{Uvicorn Drains Connections} \longrightarrow \text{Lifespan Exits: } \texttt{await core.shutdown()}$$

No threads or processes are forcefully killed. The event loop concludes naturally.

---

## 4. Test Suite & Verification Results

### 4.1 New Integration Tests in `tests/integration/test_api_endpoints.py`

| Test Function | Verification Scenario | Expected Result | Status |
|---|---|---|:---:|
| `test_shutdown_unauthenticated_rejected` | Request without valid `Authorization: Bearer <token>` | HTTP 401 Unauthorized | **PASS** |
| `test_shutdown_non_loopback_rejected` | Request from non-loopback client IP (`192.168.1.100`) | HTTP 403 Forbidden | **PASS** |
| `test_shutdown_missing_managed_server_returns_503` | Valid request when `uvicorn_server` is `None` | HTTP 503 Service Unavailable | **PASS** |
| `test_shutdown_managed_server_success` | Valid request with managed server mock | HTTP 200 `shutting_down`, sets `server.should_exit = True` | **PASS** |
| `test_shutdown_already_shutting_down_idempotent` | Re-issuing request when `server.should_exit = True` | HTTP 200 `already_shutting_down`, idempotent | **PASS** |
| `test_lifespan_invokes_core_shutdown` | Lifespan context manager exit during server termination | `await core.shutdown()` called, `core` set to `None` | **PASS** |

### 4.2 Full Regression Suite Run

Execution command:
```bash
./.venv/bin/pytest tests/
```
Output:
```text
86 passed, 1 warning in 5.11s
```
*(Baseline before Phase 1: 80 passed. Increase: +6 tests).*

### 4.3 Static Analysis & Code Quality

1. **Ruff Linter:**
   ```bash
   ./.venv/bin/ruff check src/ tests/
   ```
   Output: `All checks passed!`
2. **Git Diff Check:**
   ```bash
   git diff --check
   ```
   Output: Clean (0 whitespace/formatting errors).

---

## 5. Scope Boundary Compliance

| Directive Restriction | Verification |
|---|:---:|
| No Swift or macOS app code modified | **CONFIRMED** |
| No changes outside authorized files (`src/carbon/main.py`, `tests/integration/test_api_endpoints.py`) | **CONFIRMED** |
| Zero calls to `os.kill(os.getpid(), ...)` or self-SIGTERM | **CONFIRMED** |
| No external dependencies added | **CONFIRMED** |
| No git commits or branch switches performed | **CONFIRMED** |
| Phase 2 not started; stopped cleanly at Phase 1 completion | **CONFIRMED** |

Phase 1 implementation of Stage 1.11 is complete and ready for project owner review.

