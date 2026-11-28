# Carbon 1.0.0 — Stage 1.11 Desktop Lifecycle Architecture (v3 Final with v4 Safety Corrections)
## Comprehensive Process Supervision, Uvicorn Server Lifecycle, Single-Instance Governance, and Ownership Safety

**Task ID:** C-1.11-FINAL-CORRECTION  
**Audit Mode:** STRICTLY READ-ONLY ARCHITECTURAL SPECIFICATION  
**Date:** November 1, 2026  
**Platform:** MacBook Air M5, 16GB unified memory, macOS 27.0.1  
**Target Repository:** `PratikP0610/Carbon`  
**Current Branch:** `backup/carbon-tested-phase1-2026-10-29`  
**HEAD Commit:** `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`  
**Deliverable:** `docs/reports/stage1_11-lifecycle-audit-v3.md`  

---

## 1. Executive Summary & Audit Scope

This final specification document defines the complete desktop process lifecycle architecture for Carbon Stage 1.11. It incorporates all architectural consensus from revisions v1 through v3, and applies two mandatory **v4 safety corrections** instructed by project leadership:

1. **Elimination of Backend Self-SIGTERM Fallback:** Prohibits `os.kill(os.getpid(), signal.SIGTERM)` from within the Python backend. The HTTP shutdown endpoint strictly controls its managed Uvicorn server instance or returns HTTP 503 if unmanaged; it never terminates its own hosting process, test runner, or external ASGI server.
2. **Phase Budget Recalibration with 1.2s Watchdog Margin:** Recalibrates individual shutdown phase budgets to a total of 6.8 seconds, reserving a dedicated 1.2-second margin for OS scheduling and runloop latency before the overarching 8.0-second global watchdog ceiling.
3. **Immutable Process Safety Invariants:** Enforces that process termination authority exists solely through live child `Foundation.Process` handles held by the current supervisor session. A PID read from a file is never sufficient authority to deliver a termination signal, and the global watchdog can never signal an unowned process.

---

## 2. Recalibrated Shutdown Phase Budgets & Watchdog Interaction

### 2.1 The 6.8s Cumulative Phase Budget and 1.2s Watchdog Safety Margin
The global shutdown watchdog deadline is established at exactly **8.0 seconds**. To guarantee that normal sequential timeouts do not trigger the emergency watchdog due to thread scheduling jitter or OS context switching, the individual phase budgets are strictly capped at **6.8 seconds**:

| Phase | Target Operation | Maximum Phase Budget | Failure / Timeout Action |
|---|---|:---:|---|
| **Phase 1** | Active Inference Cancellation | **1.2 seconds** | If backend ACK is not received in 1.2s, socket closes and sequence advances immediately to Phase 2. |
| **Phase 2** | WebSocket Network Closure | **0.3 seconds** | Sends `.goingAway` close frame, invalidates `URLSession`, advances immediately to Phase 3. |
| **Phase 3** | Backend Process Shutdown | **3.0 seconds** | 1.0s for HTTP `POST /api/v1/system/shutdown` $\to$ wait on child `Process` exit. If still alive after 2.0s total, send `process.terminate()` (SIGTERM). Advance to Phase 4. |
| **Phase 4** | Ollama Process Shutdown | **2.0 seconds** | Send `process.terminate()` (SIGTERM) to child Ollama `Process`; wait up to 2.0s for exit. Advance to Phase 5. |
| **Phase 5** | Lock & State Cleanup | **0.3 seconds** | Close lockfile descriptor, release kernel `flock`, mark cleanup complete. |
| **TOTAL** | **Cumulative Phased Budget** | **6.8 seconds** | **Normal shutdown completes in < 800ms; max phased duration 6.8s.** |
| **MARGIN**| **Watchdog Safety Margin** | **1.2 seconds** | **Buffer for OS process cleanup, thread context switches, and runloop dispatch.** |
| **CEILING**| **Global Watchdog Deadline**| **8.0 seconds** | **Hard upper bound; forceful termination of owned child processes if sequence hangs.** |

### 2.2 Independent Operation of Individual Phase Timers
- Each phase enforces its own internal timeout via Swift concurrency `Task.sleep` or timeout races.
- If a phase completes early (e.g. no active inference $\implies$ Phase 1 finishes in 0ms; backend exits in 400ms $\implies$ saves 2.6s), the supervisor advances immediately to the next phase without sleeping.
- If a phase times out, it logs a warning, triggers that phase's fallback action, and advances immediately to the next phase.

### 2.3 Process-Exit Race Safety
When escalating to `SIGTERM` or `SIGKILL`:
1. The supervisor revalidates `childProcess.isRunning` immediately before issuing any POSIX signal.
2. If the child process exited normally while the timer expired (a common kernel process-exit race), the signal is skipped and the supervisor records clean exit.

---

## 3. Backend Shutdown Architecture & Uvicorn Server Lifecycle

### 3.1 Separation of Concerns: Core vs. Server
- **Core Orchestrator (`src/carbon/core/orchestrator.py`):** `core.shutdown()` manages internal subsystem state (cancelling in-flight tasks, closing SQLite database connections, recording governance audit logs).
- **HTTP Server (`uvicorn.Server`):** Manages the TCP listening socket on port 8765, active connection pools, and ASGI request lifecycles.
- Invoking `core.shutdown()` alone does not stop Uvicorn. To stop the backend process, Uvicorn's event loop must be commanded to exit.

### 3.2 Retaining the Uvicorn `Server` Instance
`src/carbon/main.py` explicitly constructs and retains the `uvicorn.Server` instance, binding it to `app.state.uvicorn_server` and a module-level reference:

```python
# In src/carbon/main.py
server_instance: uvicorn.Server | None = None

def main():
    """Main CLI entry point for running Carbon with explicit Server retention."""
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

### 3.3 Revised `POST /api/v1/system/shutdown` Endpoint (v4 Correction)

> [!CAUTION]
> **V4 Safety Invariant: No Self-SIGTERM**  
> The HTTP shutdown endpoint must **never** deliver `os.kill(os.getpid(), signal.SIGTERM)`. Self-signaling from within a request handler can terminate external ASGI runners (e.g. Gunicorn, Uvicorn CLI), background development servers, or test runners (e.g. Pytest TestClient). If no managed `uvicorn.Server` instance is bound, the endpoint returns a controlled HTTP 503 response.

```python
@app.post("/api/v1/system/shutdown", dependencies=[Depends(verify_local_auth)])
async def system_shutdown(background_tasks: BackgroundTasks, request: Request):
    """
    Gracefully request Uvicorn server termination.
    Restricted strictly to authenticated local loopback connections.
    """
    # 1. Enforce local loopback origin strictly
    client_host = request.client.host if request.client else "unknown"
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Shutdown restricted strictly to local loopback."
        )

    # 2. Retrieve managed Uvicorn server instance
    server: uvicorn.Server | None = getattr(app.state, "uvicorn_server", None) or server_instance

    if not server:
        logger.warning("HTTP shutdown rejected: no managed Uvicorn server instance registered.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Graceful programmatic shutdown unavailable: no managed Uvicorn server instance."
        )

    logger.info("Authenticated system shutdown requested via loopback HTTP.")

    def _trigger_graceful_stop():
        logger.info("Setting Uvicorn server.should_exit = True")
        server.should_exit = True

    # 3. Schedule server.should_exit = True via FastAPI BackgroundTasks
    # BackgroundTasks execute strictly AFTER the HTTP 200 response is flushed to the network socket
    background_tasks.add_task(_trigger_graceful_stop)

    return {
        "status": "shutting_down",
        "message": "Carbon backend is terminating gracefully."
    }
```

### 3.4 Lifespan Execution & Clean Exit Guarantee
When `server.should_exit = True` is set:
1. Uvicorn finishes sending the HTTP 200 response to the client.
2. Uvicorn stops accepting new TCP connections.
3. Uvicorn executes the `lifespan` context manager exit block in `src/carbon/main.py:102-107`:
   ```python
   # Clean shutdown
   global core
   if core:
       await core.shutdown()
       core = None
   logger.info("Carbon API Server shut down cleanly.")
   ```
4. `core.shutdown()` cancels tasks and closes database connections cleanly.
5. Uvicorn exits its event loop and returns from `main()` with code 0.

---

## 4. Single-Instance Governance & Fallback Activation

### 4.1 Kernel `flock` as the Absolute Authority
The sole authority for single-instance status is an exclusive, non-blocking BSD advisory lock on `data/.carbon_app.lock` (`flock(fd, LOCK_EX | LOCK_NB)`).
- The lock is kernel-managed: if the primary application crashes or is killed, the macOS kernel automatically releases the lock immediately.
- The single-instance guarantee applies identically across all launch methods, including bundled `.app` execution and CLI development (`swift run Carbon`).

### 4.2 Collision Sequence & Secondary Exit Guarantee
When a secondary process attempts `flock(fd, LOCK_EX | LOCK_NB)` and receives `EWOULDBLOCK`:
1. It reads the primary PID from `data/.carbon_app.lock`.
2. **Tier 1 Activation:** Calls `NSRunningApplication(processIdentifier: primaryPid).activate(options: [.activateIgnoringOtherApps])`.
3. **Tier 2 Activation (Fallback):** If Tier 1 returns nil (e.g. unbundled CLI binary in `swift run`), executes AppleScript:
   `tell application "System Events" to set frontmost of (every process whose unix id is <primaryPid>) to true`
4. **Tier 3 Fallback (Headless/No GUI):** Prints to stderr: `[Carbon] Already running (PID: <primaryPid>).`
5. **Strict Exit Invariant:** The secondary process **always** calls `exit(0)`. Under no circumstances does a secondary process initialize `ProcessSupervisor`, spawn backend processes, or open sockets.

---

## 5. Process Ownership and Watchdog Safety Invariants

```
┌────────────────────────────────────────────────────────────────────────┐
│                     IMMUTABLE SAFETY INVARIANTS                        │
├────────────────────────────────────────────────────────────────────────┤
│ 1. LIVE CHILD HANDLE AUTHORITY:                                        │
│    Carbon owns a process IF AND ONLY IF Carbon spawned it directly as  │
│    a child `Foundation.Process` within the active session.             │
├────────────────────────────────────────────────────────────────────────┤
│ 2. FILE PID NON-AUTHORITY:                                             │
│    A PID read from a file or descriptor is informational metadata      │
│    only. It is NEVER sufficient authority to issue a POSIX signal.     │
├────────────────────────────────────────────────────────────────────────┤
│ 3. STRICT ZERO-ADOPTION POLICY:                                        │
│    Pre-existing services (Ollama on 11434, Backend on 8765) are reused │
│    as unowned external services. Carbon NEVER terminates an unowned    │
│    process under any circumstances.                                    │
├────────────────────────────────────────────────────────────────────────┤
│ 4. UNOWNED PROCESS IMMUNITY FROM WATCHDOG:                             │
│    If the 8.0-second watchdog fires, it checks ONLY child processes    │
│    in `ProcessSupervisor`'s owned table where `isOwned == true` and     │
│    `child.isRunning == true`. It NEVER signals external processes.     │
├────────────────────────────────────────────────────────────────────────┤
│ 5. NO BACKEND SELF-TERMINATION:                                        │
│    The backend shutdown endpoint controls ONLY `server.should_exit`.   │
│    It NEVER issues `os.kill(os.getpid(), ...)` to its own process.    │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 6. Grounded Resource Telemetry (Confirmed vs. Prohibited)

### 6.1 Confirmed Telemetry Available in `GET /api/v1/health`
- Status: `status` (`healthy` / `degraded`)
- Subsystems: `core_running: true`, `ollama_connected: true`
- Active Model: `active_model.id` (`qwen3.5:9b`), `parameter_size` (`9.0B`), `quantization` (`Q4_K_M`), `context_length` (`4096`), `memory_footprint_mb` (`5824`)
- Host Hardware: `host.unified_memory_gb` (`16.0`), `host.cpu_count` (`8`), `host.os` (`Darwin`), `host.version` (`27.0.1`), `host.arch` (`arm64`)
- Tasks: `tasks_count` (`0`)

### 6.2 Prohibited Telemetry (Not in API Contract)
The UI must **not** render:
- ❌ Dynamic RAM consumption (RSS MB used/free)
- ❌ Metal GPU offload layer count (33/33)
- ❌ Real-time token generation speed (tok/s) during idle
- ❌ Operating system PIDs

---

## 7. Stage 1.11 Phase 1 Implementation Checklist (Backend Shutdown Only)

When authorization is granted, **Phase 1** implementation will execute strictly within the following checklist:

- [ ] **1.1 Server Instance Retention:** In `src/carbon/main.py`, instantiate `server_instance = uvicorn.Server(config=config)` inside `main()` and bind to `app.state.uvicorn_server`.
- [ ] **1.2 Authenticated Shutdown Route:** In `src/carbon/main.py`, define `@app.post("/api/v1/system/shutdown", dependencies=[Depends(verify_local_auth)])`.
- [ ] **1.3 Loopback Security Check:** Validate that `request.client.host` is strictly in `{"127.0.0.1", "::1", "localhost"}`; raise HTTP 403 otherwise.
- [ ] **1.4 Server Reference Verification:** Retrieve `server = getattr(app.state, "uvicorn_server", None) or server_instance`. If absent, return HTTP 503 (`detail="Graceful programmatic shutdown unavailable: no managed Uvicorn server instance."`).
- [ ] **1.5 Programmatic Exit Scheduling:** If server is present, add `server.should_exit = True` as a FastAPI `BackgroundTask` and return HTTP 200 (`{"status": "shutting_down", "message": "Carbon backend is terminating gracefully."}`).
- [ ] **1.6 No Self-SIGTERM Verification:** Audit file to confirm zero calls to `os.kill` or `signal.SIGTERM` exist in the endpoint.
- [ ] **1.7 Automated Unit & Integration Tests:** In `tests/integration/test_api_endpoints.py`:
  - Test unauthenticated request returns HTTP 401.
  - Test non-loopback request returns HTTP 403.
  - Test endpoint returns HTTP 503 when no managed Uvicorn server is bound to `app.state`.
  - Test endpoint returns HTTP 200 and schedules `server.should_exit = True` on a mock/managed server instance without terminating the test runner.
- [ ] **1.8 Regression Validation:** Run `./.venv/bin/pytest tests/` and `./.venv/bin/ruff check src/ tests/` (100% pass, 0 regressions).

---

## 8. Final Audit Status & Stop Condition

- **Audit Status:** SPECIFICATION FINALIZED (v4 SAFETY CORRECTIONS APPLIED)
- **Source Code Alterations:** ZERO (documentation only).
- **Service Lifecycle Changes:** ZERO (no services started or stopped).
- **Git Branch & Commit:** `backup/carbon-tested-phase1-2026-10-29` at `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`.
- **Implementation Status:** NOT STARTED. Execution is paused awaiting explicit project owner authorization to begin Stage 1.11 Phase 1.
