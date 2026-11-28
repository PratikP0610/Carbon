# Carbon 1.0.0 — Stage 1.11 Desktop Lifecycle Architecture Review & Revision (v2)
## Process Supervision, Single-Instance Governance, Asynchronous Termination, and Ownership Safety

**Task ID:** C-1.11-ARCHITECTURE-REVISION  
**Audit Mode:** STRICTLY READ-ONLY ARCHITECTURAL SPECIFICATION  
**Date:** November 1, 2026  
**Platform:** MacBook Air M5, 16GB unified memory, macOS 27.0.1  
**Target Repository:** `PratikP0610/Carbon`  
**Current Branch:** `backup/carbon-tested-phase1-2026-10-29`  
**HEAD Commit:** `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`  
**Deliverable:** `docs/reports/stage1_11-lifecycle-audit-v2.md`  

---

## 1. Executive Summary & Review Scope

This revised report supersedes `stage1_11-lifecycle-audit.md` following project owner review. It resolves five core architectural questions required before Stage 1.11 implementation:

1. **Single-Instance Enforcement:** Establishes a kernel-level single-instance mechanism using non-blocking BSD file locks (`flock`) paired with `NSRunningApplication` activation, ensuring a secondary launch activates the running UI rather than spawning competing supervisors.
2. **Asynchronous Application Termination:** Designs the graceful shutdown flow using AppKit's `applicationShouldTerminate(_:) -> .terminateLater` and `NSApp.reply(toApplicationShouldTerminate:)`, enforcing deterministic timeouts across active task cancellation, WebSocket closure, and child process termination.
3. **Strict Process Identity & Zero-Adoption Policy:** Clarifies why PID and executable path alone are insufficient due to PID wrap-around and concurrent external scripts. Prohibits orphan adoption in Stage 1.11; Carbon will **never** terminate a process unless it holds an active child `Foundation.Process` reference spawned in the current session.
4. **Backend Shutdown Correctness:** Solves the boundary between Core orchestrator shutdown (`core.shutdown()`) and Uvicorn HTTP server termination. Details an authenticated, loopback-only `POST /api/v1/system/shutdown` endpoint that triggers Uvicorn's native server exit loop after returning HTTP 200.
5. **Resource Telemetry Grounding:** Strictly separates confirmed telemetry currently returned by `GET /api/v1/health` from proposed or speculative metrics, ensuring the UI displays only verified real telemetry.

---

## 2. Issue 1: Reliable macOS Single-Instance Enforcement

### 2.1 The Vulnerability of Multiple UI Instances
If a user launches a second Carbon application (e.g. clicking the Dock icon while an instance is already running or launching via Terminal/Alfred), running multiple uncoordinated lifecycle supervisors creates destructive race conditions:
- **Process Hijacking / Collision:** If Instance 1 started the backend (`backendOwned = true`), Instance 2 detects the backend on port 8765 and flags `backendOwned = false`. If Instance 1 quits, it terminates the backend process, leaving Instance 2 broken and disconnected.
- **Port Conflict Crashes:** If both instances attempt to launch child backends simultaneously, one will crash with `[Errno 48] Address already in use`.
- **Database Lock Contention:** Multiple instances attempting migrations on `data/carbon.db` risk SQLite WAL lock contention.

### 2.2 Recommended Mechanism: Kernel `flock` + `NSRunningApplication`
To achieve robust single-instance enforcement that works across both bundled `.app` launches and CLI development executions:

```
[User Launches Carbon Application]
       │
       ▼
1. Open Lockfile: `data/.carbon_app.lock`
       │
       ▼
2. Call `flock(fd, LOCK_EX | LOCK_NB)`
       │
       ├─► LOCK SUCCEEDED (Primary Instance):
       │     • Write current PID to lockfile.
       │     • Retain open file descriptor for the entire application lifetime.
       │     • Initialize ProcessSupervisor & CarbonClient.
       │     • Display MainWindow.
       │
       └─► LOCK FAILED (Secondary Instance, EWOULDBLOCK):
             • Read Primary PID from lockfile.
             • Locate running application via `NSRunningApplication(processIdentifier: primaryPid)`.
             • Call `primaryApp.activate(options: [.activateIgnoringOtherApps])`.
             • Bring existing Carbon window to front.
             • Secondary instance exits immediately (`exit(0)`) without touching any services.
```

### 2.3 Why Kernel `flock` is Superior to Stale File Flags
- **Kernel-Managed Cleanup:** Unlike a plain PID file or mutex file, a BSD `flock` advisory lock is tied directly to the open file table in the macOS kernel. If the primary Carbon instance crashes, is killed via `SIGKILL`, or experiences a kernel panic, the OS kernel **automatically releases the lock immediately**. No stale lock state can ever block a subsequent legitimate launch.
- **Atomic Mutual Exclusion:** The `flock` call is atomic at the kernel level; two simultaneous launches cannot both acquire `LOCK_EX`.
- **Absolute Ownership Isolation:** Because only one Carbon UI instance can ever run, process ownership ambiguity between competing desktop instances is mathematically eliminated.

---

## 3. Issue 2: Asynchronous Application Termination via AppKit

### 3.1 The Limitation of Synchronous Termination
By default, macOS terminates an application synchronously. If `AppDelegate` attempts to perform asynchronous network requests (cancelling active inference) or waits on POSIX child process termination inside `applicationWillTerminate(_:)`, the macOS runloop may abort before the tasks complete, leaving the child process orphaned or inference running on the GPU.

### 3.2 The Solution: `applicationShouldTerminate(_:) -> .terminateLater`
AppKit provides a first-class asynchronous termination protocol designed for clean subsystem teardown:

```swift
func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
    // 1. If already cleaned up, allow immediate exit
    if ProcessSupervisor.shared.isCleanedUp {
        return .terminateNow
    }

    // 2. Halt termination, keep runloop alive, initiate async shutdown sequence
    Task { @MainActor in
        await ProcessSupervisor.shared.performGracefulShutdown(client: self.client)
        // 3. Notify AppKit that cleanup is complete
        NSApp.reply(toApplicationShouldTerminate: true)
    }

    return .terminateLater
}
```

### 3.3 The Phased Termination Sequence & Timeout Budgets

```
[User Initiates Quit: Cmd+Q, Menu -> Quit, or Dock -> Quit]
       │
       ▼
applicationShouldTerminate(_:) returns .terminateLater
       │
       ├─► PHASE 1: Active Inference Cancellation (Budget: 1.0s)
       │     • If `client.isGenerating == true` and `client.currentTaskId != nil`:
       │         Send WebSocket cancellation frame: `{"action": "cancel", "task_id": tid}`
       │         Wait up to 500ms for backend acknowledgment.
       │
       ├─► PHASE 2: WebSocket Network Session Closure (Budget: 0.5s)
       │     • Cancel `URLSessionWebSocketTask` with `.goingAway` close code.
       │     • Invalidate `URLSession`.
       │
       ├─► PHASE 3: Backend Process Shutdown (Budget: 3.0s, ONLY if backendOwned == true)
       │     • Step 3a: Attempt HTTP `POST /api/v1/system/shutdown` (timeout 1.0s).
       │     • Step 3b: If HTTP succeeds or times out, wait on child `Process` exit.
       │     • Step 3c: If child `Process` is still alive after 2.0s total, send `process.terminate()` (SIGTERM).
       │     • Step 3d: If still alive after 3.0s total, send `kill(pid, SIGKILL)` as a last resort.
       │     • If backendOwned == false: DO NOT terminate; leave running untouched.
       │
       ├─► PHASE 4: Ollama Process Shutdown (Budget: 2.0s, ONLY if ollamaOwned == true)
       │     • Step 4a: Send `process.terminate()` (SIGTERM) to child Ollama `Process`.
       │     • Step 4b: Wait up to 1.5s for process to exit.
       │     • Step 4c: If still alive, send `kill(pid, SIGKILL)`.
       │     • If ollamaOwned == false: DO NOT terminate; leave running untouched.
       │
       ├─► PHASE 5: Lock & State Cleanup
       │     • Release `flock` and close lockfile descriptor.
       │     • Set `ProcessSupervisor.shared.isCleanedUp = true`.
       │
       └─► PHASE 6: Complete AppKit Termination
             • Call `NSApp.reply(toApplicationShouldTerminate: true)`.
             • Application exits cleanly.
```

### 3.4 Hard Watchdog Timer Failure Protection
To ensure that a network deadlock, frozen subprocess, or hung socket never prevents the user from quitting Carbon:
- A top-level watchdog timer runs concurrently with a strict **5.0-second total deadline**.
- If the shutdown sequence exceeds 5.0 seconds, the watchdog triggers, logs an emergency warning, immediately signals `kill(pid, SIGKILL)` to any owned child processes still marked alive, and forcefully invokes `NSApp.reply(toApplicationShouldTerminate: true)`.
- The application will **never hang indefinitely on quit**.

---

## 4. Issue 3: Process Identity, Ownership Boundaries, and Zero-Adoption Policy

### 4.1 Why PID Plus Executable Path Alone is Insufficient
In naive lifecycle management, an application saves a PID and an executable path (e.g. `12345` and `/opt/homebrew/bin/ollama`) to disk, and upon shutdown or restart checks: *"Does PID 12345 exist and is its path Ollama?"*
This is **fundamentally unsafe** on macOS for three reasons:
1. **PID Wrap-Around & Reuse:** macOS assigns PIDs sequentially up to 99,999 before wrapping. In a developer environment where processes frequently start and stop, a PID can be recycled in hours or days.
2. **Concurrent User/Developer Workloads:** If a developer runs a background Python script inside `~/Carbon` (e.g. running `pytest`, a training script, or `benchmark_100_runs.py` using `.venv/bin/python`), its executable path is identical (`/Users/.../Carbon/.venv/bin/python`). If Carbon crashes, restarts, and checks only PID + path, it could misidentify the developer's benchmark as its own backend and terminate it!
3. **External Service Identity:** A user may have started Ollama via `brew services` or terminal. Its path matches `/opt/homebrew/bin/ollama`. Carbon did not spawn it and must not control it.

### 4.2 Explicit Process Ownership Definition
Carbon enforces an unambiguous process ownership boundary:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        OWNERSHIP CLASSIFICATION                        │
├───────────────────────────────────┬────────────────────────────────────┤
│           OWNED PROCESS           │          UNOWNED PROCESS           │
├───────────────────────────────────┼────────────────────────────────────┤
│ • Launched directly as a child    │ • Discovered already running       │
│   `Foundation.Process` by Carbon  │   during startup probes            │
│ • Parent PID == Carbon's PID      │ • Parent PID != Carbon's PID       │
│ • In-memory `Process` handle      │ • Managed by launchd, brew,        │
│   held by `ProcessSupervisor`     │   terminal, or another tool        │
│ • Injected with unique env token: │ • No Carbon instance token         │
│   `CARBON_PARENT_UUID=<uuid>`     │                                    │
├───────────────────────────────────┼────────────────────────────────────┤
│ ACTION ON QUIT:                   │ ACTION ON QUIT:                    │
│   Gracefully terminate (SIGTERM)  │   NEVER TERMINATE; LEAVE RUNNING   │
└───────────────────────────────────┴────────────────────────────────────┘
```

### 4.3 Strict Zero-Adoption Policy for Stage 1.11
- **Policy Decision:** Carbon Stage 1.11 will **NOT** implement automatic orphan adoption.
- If Carbon launches and discovers a backend already responding on port 8765 (from a previous crashed run or a terminal session), Carbon will:
  1. Probe `GET /api/v1/health` and verify the Carbon signature.
  2. Treat the backend as an **EXTERNAL / UNOWNED** service (`backendOwned = false`).
  3. Attach its WebSocket client and provide normal chat functionality.
  4. On quit, Carbon will disconnect its WebSocket, but will **NOT** terminate the backend process.
- **Why Zero-Adoption is the Safest Architecture:**
  - Guarantees zero false-positive process terminations.
  - Guarantees Carbon will never terminate a developer's terminal session.
  - Keeps Stage 1.11 implementation simple, reliable, and strictly verifiable.

---

## 5. Issue 4: Backend Shutdown Correctness & Uvicorn Lifecycle

### 5.1 Distinguishing Core Shutdown from HTTP Server Shutdown
In `src/carbon/core/orchestrator.py:166-190`, `CarbonCore.shutdown()` exists:
- Cancels in-memory tasks (`task.cancel_event.set()`).
- Closes SQLite memory connection (`await self.memory.close()`).
- Records audit event in governance engine.

**However, `core.shutdown()` does NOT stop Uvicorn!**
`uvicorn.run()` in `src/carbon/main.py:476` manages an asynchronous socket server. Calling `core.shutdown()` releases internal resources, but Uvicorn continues listening on port 8765 indefinitely.

### 5.2 Implementation of `POST /api/v1/system/shutdown`
To allow clean, controlled shutdown via HTTP, `src/carbon/main.py` requires a dedicated shutdown route that coordinates with Uvicorn's event loop:

```python
@app.post("/api/v1/system/shutdown", dependencies=[Depends(verify_local_auth)])
async def system_shutdown(request: Request):
    """Gracefully terminate the Carbon backend server and Core orchestrator."""
    # 1. Enforce local loopback strictly
    client_host = request.client.host if request.client else "unknown"
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="Shutdown restricted strictly to local loopback.")

    logger.info("Authenticated system shutdown request received via HTTP.")

    # 2. Schedule clean exit on the asyncio event loop after flushing response
    async def _delayed_shutdown():
        # Brief pause allows FastAPI to transmit HTTP 200 response to client
        await asyncio.sleep(0.15)
        # Deliver SIGTERM to own process: triggers Uvicorn's native graceful shutdown handler
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_delayed_shutdown())
    return {"status": "shutting_down", "message": "Carbon backend is terminating gracefully."}
```

### 5.3 Why Self-Signaling via `os.kill(os.getpid(), signal.SIGTERM)` is Correct
1. **Flushes Network Response:** The 150ms delay ensures the HTTP 200 response is fully transmitted over the TCP socket before the socket is closed.
2. **Triggers Native Uvicorn Lifespan:** When Uvicorn catches `SIGTERM`:
   - It stops accepting new HTTP/WebSocket connections.
   - It drains active in-flight requests.
   - It executes the `lifespan` context manager exit block in `src/carbon/main.py:103-107`, which calls `await core.shutdown()`!
   - Uvicorn exits cleanly with return code 0.
3. **Fallback Resiliency:**
   If the HTTP request times out (e.g. backend event loop blocked), the Swift `ProcessSupervisor` waits 1.5s and then issues POSIX `process.terminate()` directly to the child process handle. If the backend was already stopping or dead, the network connection error is caught cleanly and the supervisor proceeds to process verification.

---

## 6. Issue 5: Resource Telemetry — Confirmed vs. Proposed

To prevent displaying invented, approximated, or unverified values in the native UI, telemetry is strictly categorized against the current production code in `src/carbon/core/orchestrator.py` and `src/carbon/devices/manager.py`:

### 6.1 Confirmed Telemetry Currently Returned by `GET /api/v1/health`

| Metric Field | JSON Path | Data Type | Verified Source in Code | Current Production Example |
|---|---|---|---|---|
| **System Status** | `status` | String | `orchestrator.py:198` | `"healthy"` / `"degraded"` |
| **Core Running** | `core_running` | Boolean | `orchestrator.py:199` | `true` |
| **Ollama Connected** | `ollama_connected` | Boolean | `orchestrator.py:200` | `true` |
| **Active Model ID** | `active_model.id` | String | `cms.py` via `orchestrator.py:201` | `"qwen3.5:9b"` |
| **Parameter Size** | `active_model.parameter_size` | String | `ollama_provider.py:88` | `"9.0B"` |
| **Quantization** | `active_model.quantization` | String | `ollama_provider.py:89` | `"Q4_K_M"` |
| **Active Context** | `active_model.context_length` | Integer | `ollama_provider.py:90` | `4096` |
| **Model Footprint** | `active_model.memory_footprint_mb` | Integer | `ollama_provider.py:92` | `5824` (MB) |
| **Host Memory** | `host.unified_memory_gb` | Float | `devices/manager.py:44` | `16.0` (GB) |
| **Host CPU Cores** | `host.cpu_count` | Integer | `devices/manager.py:42` | `8` |
| **Host Platform** | `host.os` / `host.version` | String | `devices/manager.py:39-40` | `"Darwin"` / `"27.0.1"` |
| **Host Architecture** | `host.arch` | String | `devices/manager.py:41` | `"arm64"` |
| **Active Tasks Count** | `tasks_count` | Integer | `orchestrator.py:204` | `0` |

### 6.2 Proposed Telemetry (NOT Present in Current Health Contract)
The following metrics are **PROPOSED ONLY**. They **MUST NOT** be rendered in the Stage 1.11 UI until backend telemetry probes are authored:
- ❌ **Real-Time Dynamic RAM Consumption (MB used / free):** `devices/manager.py` queries only static hardware memory (`hw.memsize`), not active process RSS or available memory.
- ❌ **Metal GPU Offloaded Layer Ratio (e.g. 33/33):** Exists only in Ollama log streams or `ollama ps`; not parsed into Carbon's health schema.
- ❌ **Live Token Generation Speed (tok/s):** Computed per task upon completion, not exposed as a continuous health metric.
- ❌ **Ollama & Backend OS PIDs:** Known to the Swift process launcher, but not returned by `GET /api/v1/health`.

### 6.3 Grounded UI Display Specification
In `ContentView.swift:headerView`, the resource display must bind **strictly** to verified fields:

```
[cpu] CARBON   [Model: qwen3.5:9b]   [Hardware: 16 GB Unified]   [ONLINE]
```

Popover detail card (when clicking the status pill):
- **Core Status:** `Online (healthy)`
- **Active Model:** `qwen3.5:9b (9.0B, Q4_K_M)`
- **Context Length:** `4,096 tokens`
- **Model Footprint:** `5,824 MB`
- **Host System:** `Apple Silicon (8 cores, 16.0 GB Unified Memory)`
- **Runtime Mode:** `Ollama Connected`
- **Active Tasks:** `0 running`

---

## 7. Compatibility Review of Environment & Tooling

1. **Ollama Launch Method Compatibility:**
   - On the target testbed (MacBook Air M5, macOS 27.0.1), Ollama is installed at `/opt/homebrew/bin/ollama` (version 0.33.0).
   - Probing `GET http://127.0.0.1:11434/api/version` successfully confirms health without spawning.
   - If stopped, launching `/opt/homebrew/bin/ollama serve` runs cleanly in user space without requiring root or modifying system daemons.
2. **Python Executable Resolution Compatibility:**
   - The virtual environment exists at `.venv/bin/python` (Python 3.12.14).
   - Executing `PYTHONPATH=src ./.venv/bin/python -m carbon.main` is identical to the verified invocation used by benchmark harnesses and tests.
   - For desktop launches, Swift resolves the path dynamically via current working directory or `FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Carbon/.venv/bin/python")`.
3. **Token Synchronization Compatibility:**
   - `src/carbon/main.py:59` writes `data/.carbon_token` with `0600` permissions.
   - `apps/macos/Carbon/Sources/CarbonClient.swift:118` already resolves `~/Carbon/data/.carbon_token`.
   - `ProcessSupervisor` will poll for the file existence (up to 2.0s) immediately after backend health reports readiness, guaranteeing the token is present before WebSocket connection begins.

---

## 8. Revised, Ordered Implementation Plan (Stage 1.11)

When authorized, Stage 1.11 implementation will proceed in 4 strictly isolated increments:

### Phase 1: Backend Shutdown & Health Hardening (Python)
1. Add `POST /api/v1/system/shutdown` to [`src/carbon/main.py`](../../src/carbon/main.py), enforcing loopback origin and `verify_local_auth`.
2. Ensure the endpoint returns HTTP 200 before asynchronously signaling Uvicorn's event loop to trigger `core.shutdown()` and server exit.
3. Add automated tests in `tests/integration/test_api_endpoints.py` verifying loopback restriction, authentication requirement, and clean shutdown signaling.

### Phase 2: Single-Instance & Process Supervisor (Swift)
1. Create `apps/macos/Carbon/Sources/ProcessSupervisor.swift`:
   - Implement `flock` single-instance check on `data/.carbon_app.lock` with `NSRunningApplication` activation of primary window.
   - Implement sequential startup probe and launcher (`checkOllama` $\to$ `checkModel` $\to$ `checkBackend` $\to$ `syncToken`).
   - Implement process ownership tracking (live `Foundation.Process` references only; zero adoption of external processes).
   - Implement graceful shutdown sequence with timeout budgets.
2. Update `AppDelegate` in [`apps/macos/Carbon/Sources/CarbonApp.swift`](../../apps/macos/Carbon/Sources/CarbonApp.swift):
   - Implement `applicationShouldTerminate(_:) -> .terminateLater` coordinating with `ProcessSupervisor`.

### Phase 3: Client Lifecycle Integration & State Machine (Swift)
1. In [`apps/macos/Carbon/Sources/CarbonClient.swift`](../../apps/macos/Carbon/Sources/CarbonClient.swift):
   - Replace disconnected polling with explicit `AppLifecycleState` (`.startingOllama`, `.checkingModel`, `.startingBackend`, `.connecting`, `.ready`, `.recovering`, `.error`).
   - Bind connection initiation to `ProcessSupervisor` completion.
   - Support manual `retryStartup()` trigger.

### Phase 4: SwiftUI Lifecycle Overlay & Telemetry Popover (SwiftUI)
1. In [`apps/macos/Carbon/Sources/ContentView.swift`](../../apps/macos/Carbon/Sources/ContentView.swift):
   - Add non-intrusive startup progress and error/retry cards when `lifecycleState != .ready`.
   - Update `headerView` with grounded telemetry indicators (Model, Memory, Status) and detail popover.
2. Compile and run clean regression build:
   - Python unit & integration tests (100% pass).
   - Swift compilation and desktop test runner (100% pass).

---

## 9. Remaining Project Owner Decisions (Open Questions)

Before implementation commences, the project owner should confirm preferences on the following 3 operational details:

1. **Ollama Search Path Order:**
   - *Proposed:* Check `/opt/homebrew/bin/ollama` $\to$ `/usr/local/bin/ollama` $\to$ `/Applications/Ollama.app/Contents/Resources/ollama` $\to$ `which ollama`.
   - *Decision:* Confirm whether this resolution order matches all intended local test environments.
2. **Missing Model UX Guidance:**
   - When `qwen3.5:9b` is missing from Ollama, Carbon will display an error card stating: `"Required model 'qwen3.5:9b' is not installed. Run: ollama pull qwen3.5:9b"`.
   - *Decision:* Confirm whether Carbon should remain manual-pull only (recommended for Phase 1), or if an automated pull trigger via Ollama API should be considered in future phases.
3. **Shutdown Watchdog Timeout:**
   - The proposed global watchdog gives the app at most **5.0 seconds** to cancel active inference, close WebSockets, and stop owned child processes before forcing termination via `reply(toApplicationShouldTerminate: true)`.
   - *Decision:* Confirm whether 5.0 seconds is acceptable for desktop user responsiveness.

---

## 10. Final Verification & Stop Condition

- **Audit Status:** COMPLETE (REVISION v2)
- **Source Code Alterations:** ZERO (strictly read-only review).
- **Working Tree & Git State:** Completely intact on `backup/carbon-tested-phase1-2026-10-29` at commit `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`.
- **Next Step:** Await explicit project owner approval of this v2 architecture before beginning Phase 1 implementation.

