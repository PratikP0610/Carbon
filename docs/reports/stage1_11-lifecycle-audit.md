# Carbon 1.0.0 — Stage 1.11 Desktop Lifecycle Audit Report
## Process Supervision, Startup Orchestration, and Graceful Shutdown Architecture

**Task ID:** C-1.11-LIFECYCLE-AUDIT  
**Audit Mode:** STRICTLY READ-ONLY ARCHITECTURAL AUDIT  
**Date:** November 1, 2026  
**Platform:** MacBook Air M5, 16GB unified memory, macOS 27.0.1  
**Target Repository:** `PratikP0610/Carbon`  
**Current Branch:** `backup/carbon-tested-phase1-2026-10-29`  
**HEAD Commit:** `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`  
**Target Deliverable:** `docs/reports/stage1_11-lifecycle-audit.md`  

---

## 1. Executive Summary

Carbon 1.0.0 provides a native SwiftUI macOS desktop application connected over local loopback (`http://127.0.0.1:8765` and `ws://127.0.0.1:8765/ws/chat`) to a Python FastAPI backend, which interfaces with an Ollama runtime (`http://127.0.0.1:11434`) serving `qwen3.5:9b`.

Currently, **Carbon has no desktop process lifecycle supervision**. The macOS application functions as a passive network client. It assumes that both the Ollama runtime and the Python backend were launched manually by a developer via Terminal prior to opening the application. If either service is absent, the application launches into an unfunctional `OFFLINE` state, continuously attempting network retries without ever attempting to start the missing services, report why the service is absent, or guide the user. Furthermore, when the user quits Carbon, any backend or Ollama processes spawned remain running indefinitely as orphaned background tasks.

This audit establishes a concrete, safe, and verifiable lifecycle architecture for **Target Startup Mode (`launch`)** and **Target Shutdown Mode (`owned`)**:
1. **Startup (`launch`):** Automatically detect, verify, and start missing components in strict dependency order (Ollama $\to$ Model check $\to$ Carbon backend $\to$ Health check $\to$ Authenticated WebSocket connection) while reusing pre-existing services without stealing ownership.
2. **Shutdown (`owned`):** Explicitly track process ownership via parent-child process handles and verified instance descriptors. On application termination, cancel active inference, close WebSocket connections, and terminate **only** processes that Carbon itself spawned, leaving pre-existing user or system services untouched.

---

## 2. Existing Desktop Architecture & Call Paths

### 2.1 Existing Source Files and Functions

| Component | File Path | Key Functions / Symbols | Current Responsibility |
|---|---|---|---|
| **App Entry Point** | [`apps/macos/Carbon/Sources/CarbonApp.swift`](../../apps/macos/Carbon/Sources/CarbonApp.swift) | `AppDelegate`, `CarbonApp.body`, `runNativeVerification` | Configures `.regular` activation policy, sets window visibility, instantiates `@StateObject private var client = CarbonClient()`. |
| **Network Client** | [`apps/macos/Carbon/Sources/CarbonClient.swift`](../../apps/macos/Carbon/Sources/CarbonClient.swift) | `init()`, `checkHealth()`, `startPeriodicHealthCheck()`, `connectWebSocket()`, `scheduleReconnect()`, `sendMessage()`, `cancelCurrentTask()` | Manages URLSession WebSocket connection, in-socket/header auth, heartbeat polling every 10s, and message streaming. |
| **Main View** | [`apps/macos/Carbon/Sources/ContentView.swift`](../../apps/macos/Carbon/Sources/ContentView.swift) | `ContentView`, `headerView`, `statusColor`, `inputBarView`, `MessageBubbleView` | Renders the chat UI, status pill (`OFFLINE` / `ONLINE`), model tag, reasoning depth picker, and message transcript. |
| **Markdown Engine** | [`apps/macos/Carbon/Sources/MarkdownRenderer.swift`](../../apps/macos/Carbon/Sources/MarkdownRenderer.swift) | `MarkdownParser`, `MathFormatter`, `MarkdownCoordinator` | Parses AST blocks and renders formatted Markdown and mathematical expressions. |
| **Backend Server** | [`src/carbon/main.py`](../../src/carbon/main.py) | `create_app()`, `lifespan()`, `get_health()`, `/ws/chat`, `main()` | Hosts FastAPI REST and WebSocket endpoints on `127.0.0.1:8765`, enforces loopback security and auth token. |
| **Core Orchestrator** | [`src/carbon/core/orchestrator.py`](../../src/carbon/core/orchestrator.py) | `CarbonCore.initialize()`, `shutdown()`, `get_system_health()`, `execute_task_stream()` | Manages subsystem lifecycle (CMS, Memory, Governance, Devices), tracks tasks, coordinates model inference. |
| **Ollama Provider** | [`src/carbon/model_control/ollama_provider.py`](../../src/carbon/model_control/ollama_provider.py) | `is_healthy()`, `list_available_models()`, `generate_stream_events()` | Communicates with Ollama daemon on `127.0.0.1:11434`. |
| **Test Suite** | [`tests/test_desktop_connection_reliability.py`](../../tests/test_desktop_connection_reliability.py) | 11 connection reliability tests | Tests WebSocket auth, reconnect, error recovery, and active task cleanup on disconnect. |

### 2.2 Trace of Existing Startup Flow

```
[User Launches Carbon.app]
       │
       ▼
CarbonApp.init()
       │
       ▼
CarbonClient.init() (CarbonClient.swift:95-104)
       ├─► checkHealth() ──► HTTP GET http://127.0.0.1:8765/api/v1/health
       │     └─► If backend absent: network failure -> isHealthOk = false
       │
       ├─► startPeriodicHealthCheck() ──► repeats checkHealth() every 10.0s
       │
       └─► connectWebSocket() ──► ws://127.0.0.1:8765/ws/chat
             └─► If backend absent: URLSession error -> webSocketDidFail()
                   └─► scheduleReconnect() ──► exponential backoff (1.5s - 10.0s)
                         └─► Loop repeats indefinitely!
```

**Verified Finding:** At no point during startup does the desktop application check whether Ollama is running, check if `qwen3.5:9b` exists, check whether Python is installed, or launch the backend process.

### 2.3 Trace of Existing Shutdown Flow

```
[User Quits Carbon.app via Cmd+Q or Close Window]
       │
       ▼
NSApplication terminates immediately
       │
       ▼
CarbonClient.deinit() (CarbonClient.swift:106-111)
       ├─► reconnectTask?.cancel()
       ├─► submissionTimeoutTask?.cancel()
       ├─► healthCheckTask?.cancel()
       └─► webSocketTask?.cancel(with: .goingAway, reason: nil)
```

**Verified Finding:**
- `AppDelegate` has **no** `applicationWillTerminate(_:)` implementation.
- `webSocketTask?.cancel()` sends an asynchronous TCP close frame, but the OS abruptly tears down process memory immediately thereafter.
- No shutdown signal (HTTP or POSIX signal) is sent to the Carbon backend.
- If the backend was started in a background terminal or daemon, it continues executing indefinitely on port 8765.
- If inference was running, the backend's `_cleanup_connection_task()` receives a socket disconnect, but Ollama may continue generating tokens until its local HTTP streaming connection is exhausted.

---

## 3. Current Process Ownership Behaviour & Root Causes

| Entity | How It Is Currently Started | Process Ownership | How It Is Terminated | Risk / Failure Mode |
|---|---|---|---|---|
| **Ollama Daemon** | External: `brew services start ollama` or `ollama serve` in Terminal | Completely unowned by Carbon | External: `brew services stop` or Terminal `Ctrl+C` | If absent, app fails silently with `OFFLINE`. |
| **Carbon Backend** | External: `PYTHONPATH=src ./.venv/bin/python -m carbon.main` | Completely unowned by Carbon | External: Terminal `Ctrl+C` or `kill -9` | If absent, app fails silently with `OFFLINE`. If left running, leaks memory/ports. |
| **Model Runtime** | Pre-pulled in Ollama | Managed by Ollama | Managed by Ollama model unloading (`keep_alive`) | If model is missing, backend reports `degraded`, but desktop UI gives no explanation. |

---

## 4. Identified Bugs, Risks, and Fragilities

### 4.1 Fatal User-Facing Bugs
1. **The "Terminal Requirement" Defect:**
   The end user cannot use Carbon as a standalone macOS application. Launching `Carbon.app` from Finder or Applications without manually starting background processes in Terminal results in a permanent `OFFLINE` status.
2. **Missing Model Black Hole:**
   If Ollama is running but `qwen3.5:9b` has not been pulled, the backend reports `status: degraded`. The desktop UI shows an orange or red indicator, but the user receives zero diagnostic information explaining that the model is missing.
3. **No Active Task Cancellation on Fast App Exit:**
   Closing the app while the model is in a deep reasoning loop does not guarantee clean cancellation before the socket terminates, leaving the local GPU/NPU spinning on orphaned inference.

### 4.2 Critical Process Safety Risks
1. **Duplicate Backend Collision (`[Errno 48] Address already in use`):**
   If Carbon attempts to launch `python -m carbon.main` when an instance is already running on port 8765, Uvicorn crashes. If multiple processes attempt to access SQLite `carbon.db` simultaneously during migration, WAL locking timeouts occur.
2. **Duplicate Ollama Collision (`bind: address already in use`):**
   Attempting to start `ollama serve` when Ollama is already running (e.g. via Homebrew or menu bar) produces a port 11434 bind error. The app must not interpret this bind collision as an "Ollama failure" when Ollama is already fully functional.
3. **PID Wrap-Around and Identity Collision (The "Innocent Process Kill" Hazard):**
   Saving a raw PID (e.g. `8494`) in a file and blindly calling `kill(pid, SIGTERM)` upon subsequent launch or shutdown is unsafe on macOS. If Carbon crashed previously and the OS reused PID `8494` for an unrelated system process (e.g. `WindowServer`, `Safari`, `launchd`), sending a signal would terminate a foreign process.
4. **Indiscriminate Process-Name Killing (`killall` Hazard):**
   Using commands like `killall python` or `killall ollama` is strictly unacceptable. It would terminate the user's other Python programs, Jupyter servers, data science pipelines, or unrelated Ollama models.
5. **Port Conflict with Alien Services:**
   An unrelated web service might occupy port 8765. Probing only port connectivity is insufficient; the app must verify the Carbon API signature (`{"name": "Carbon", "version": "1.0.0"}`) before concluding the backend is present.

---

## 5. Recommended Architecture: The Carbon Process Supervisor

To solve these problems without modifying the core chat UI, Carbon should introduce a dedicated Swift process supervision module: `ProcessSupervisor.swift`.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        macOS Carbon Application                        │
│                                                                        │
│   ┌─────────────────────┐                 ┌────────────────────────┐   │
│   │    CarbonClient     │                 │   ProcessSupervisor    │   │
│   │  (WebSocket & REST) │◄───coordinates──┤  (Process Lifecycle)   │   │
│   └──────────┬──────────┘                 └───────────┬────────────┘   │
└──────────────┼────────────────────────────────────────┼────────────────┘
               │ (HTTP / WS)                            │ (POSIX Child Process)
               ▼                                        ▼
┌──────────────────────────────┐            ┌────────────────────────────┐
│    Carbon Backend Server     │            │   Ollama Inference Daemon  │
│  (127.0.0.1:8765, Python)    │            │     (127.0.0.1:11434)      │
│                              │            │                            │
│  • Reused if pre-existing    │            │  • Reused if pre-existing  │
│  • Spawned & owned if absent │            │  • Spawned & owned if absent│
└──────────────────────────────┘            └────────────────────────────┘
```

### 5.1 Process Ownership Contract
A process is **OWNED** if and only if:
1. Carbon launched it directly as a `Foundation.Process` child process during the current application session, OR
2. Carbon adopts an orphaned process from a previous abnormal exit whose identity, executable path, and unique Carbon session token are verified.

All pre-existing processes detected during startup are flagged as **EXTERNAL / REUSED** (`owned = false`). Carbon **never** sends termination signals to unowned processes.

### 5.2 Explicit Process Identity & State Tracking
Carbon will track process state using in-memory `Process` references and an atomic session descriptor file located at `data/.carbon_desktop_lifecycle.json`:

```json
{
  "session_id": "c9a2f1b4-7d8e-4a11-8e0b-2e9184518290",
  "app_pid": 14205,
  "created_at": "2026-11-01T12:30:00Z",
  "backend": {
    "owned": true,
    "pid": 14210,
    "executable": ".venv/bin/python",
    "port": 8765
  },
  "ollama": {
    "owned": false,
    "pid": 7593,
    "executable": "/opt/homebrew/opt/ollama/bin/ollama",
    "port": 11434
  }
}
```

### 5.3 Stale PID & Identity Verification
Before interacting with any PID recorded on disk:
1. Call `kill(pid, 0)` to verify the PID is active.
2. Call `proc_pidpath(pid)` (via `libproc`) to confirm the executable path matches `/opt/homebrew/.../ollama` or `.../.venv/bin/python`.
3. If the PID is dead or belongs to a different executable, the descriptor is declared stale and purged immediately.

---

## 6. Target Startup Flow (`launch` Mode)

When Carbon launches, `ProcessSupervisor` coordinates a deterministic 6-phase startup sequence:

```
[Phase 1: Ollama Verification]
  ├── Query: GET http://127.0.0.1:11434/api/version
  ├── If HTTP 200:
  │     ollamaOwned = false
  │     Proceed to Phase 2.
  └── If Connection Refused:
        ├── Locate Ollama binary:
        │     1. /opt/homebrew/bin/ollama
        │     2. /usr/local/bin/ollama
        │     3. /Applications/Ollama.app/Contents/Resources/ollama
        ├── If found:
        │     Launch `ollama serve` as child Process
        │     ollamaOwned = true
        │     Poll /api/version every 250ms (timeout: 15s)
        │     If success -> Proceed to Phase 2
        │     If timeout -> Transition to .error("Ollama failed to start within 15 seconds.")
        └── If not found:
              Transition to .error("Ollama runtime not found. Please install Ollama.")

[Phase 2: Model Verification]
  ├── Query: GET http://127.0.0.1:11434/api/tags
  ├── Parse models list for `qwen3.5:9b` (or `qwen3.5:9b-instruct-q4_K_M`)
  ├── If present -> Proceed to Phase 3.
  └── If missing:
        Transition to .error("Required model 'qwen3.5:9b' is not installed in Ollama. Run: ollama pull qwen3.5:9b")

[Phase 3: Backend Verification]
  ├── Query: GET http://127.0.0.1:8765/api/v1/health
  ├── If HTTP 200 and JSON contains "core_running": true:
  │     backendOwned = false
  │     Proceed to Phase 4.
  └── If Connection Refused:
        ├── Check port 8765 availability. If bound by foreign service -> .error("Port 8765 occupied by another program.")
        ├── Locate Python virtualenv: Resolve `Carbon/.venv/bin/python`
        ├── Launch child Process:
        │     Executable: `.venv/bin/python`
        │     Arguments: `["-m", "carbon.main"]`
        │     Environment: `PYTHONPATH=src`, `CARBON_PARENT_PID=<app_pid>`
        │     backendOwned = true
        ├── Poll GET http://127.0.0.1:8765/api/v1/health every 250ms (timeout: 15s)
        ├── If health returns status "healthy" -> Proceed to Phase 4
        └── If timeout -> Transition to .error("Carbon backend server failed to initialize.")

[Phase 4: Token Resolution & Sync]
  ├── Read `data/.carbon_token` (wait up to 2.0s if file is being created)
  └── Confirm token string is valid base64/urlsafe token.

[Phase 5: Authenticated WebSocket Connection]
  ├── Connect `ws://127.0.0.1:8765/ws/chat` with `Authorization: Bearer <token>`
  ├── Dispatch in-socket fallback auth frame
  ├── Await server `{"event": "authenticated"}` event
  └── Transition to Phase 6.

[Phase 6: Ready State]
  ├── App Lifecycle State = .ready
  └── Enable Chat UI for user interaction.
```

---

## 7. Target Shutdown Flow (`owned` Mode)

When the user quits the application (`Cmd+Q`, menu quit, or last window closed):

```
[User Initiates Quit]
       │
       ▼
1. Cancel Active Inference (Timeout: 1.0s)
   • If client.isGenerating == true:
     Dispatch WebSocket frame: {"action": "cancel", "task_id": currentTaskId}
     Allow 500ms for backend to process task cancellation and release model lease.

2. Disconnect WebSocket (Timeout: 500ms)
   • Send WebSocket close frame (.goingAway).
   • Invalidate network session.

3. Shutdown Backend (IF backendOwned == true)
   • Step 3a (Graceful): Send SIGTERM to backend child Process (pid).
   • Step 3b (Wait): Wait up to 3.0s for backend child process to exit cleanly.
   • Step 3c (Forced Fallback): If still running after 3.0s, send SIGKILL to backend pid.
   • If backendOwned == false:
     Leave pre-existing backend process running untouched!

4. Shutdown Ollama (ONLY IF ollamaOwned == true)
   • Step 4a (Graceful): Send SIGTERM to Ollama child Process (pid).
   • Step 4b (Wait): Wait up to 3.0s for Ollama process to exit cleanly.
   • Step 4c (Forced Fallback): If still running after 3.0s, send SIGKILL to Ollama pid.
   • If ollamaOwned == false:
     Leave pre-existing Ollama service (e.g. Homebrew daemon) running untouched!

5. Purge Lifecycle Descriptor File
   • Delete `data/.carbon_desktop_lifecycle.json` if all owned processes terminated.

6. Terminate Application Process
```

---

## 8. User Interface State Machine & Recovery UX

### 8.1 Lifecycle States in SwiftUI
In `CarbonClient.swift`, replace binary `isConnected` / `isHealthOk` with a unified `@Published public var lifecycleState: AppLifecycleState`:

```swift
public enum AppLifecycleState: Equatable {
    case startingOllama
    case checkingModel
    case startingBackend
    case connecting
    case ready
    case recovering(attempt: Int, delay: Double)
    case error(title: String, message: String, canRetry: Bool)
}
```

### 8.2 Startup & Error Overlay in `ContentView.swift`
When `lifecycleState != .ready`, `ContentView` presents a clean, non-intrusive status view instead of an unresponsive chat interface:

```
┌────────────────────────────────────────────────────────┐
│                        CARBON                          │
│                                                        │
│                    [ Animated Spinner ]                │
│                                                        │
│               Starting Carbon Backend...               │
│          Initializing local memory and intelligence    │
│                                                        │
└────────────────────────────────────────────────────────┘
```

When an error occurs:
```
┌────────────────────────────────────────────────────────┐
│                        CARBON                          │
│                                                        │
│                ⚠️  Required Model Missing               │
│                                                        │
│    The model 'qwen3.5:9b' is not installed in Ollama.  │
│    Please run the following command in Terminal:       │
│                                                        │
│               ollama pull qwen3.5:9b                   │
│                                                        │
│                    [ Retry Connection ]                │
└────────────────────────────────────────────────────────┘
```
Clicking **Retry Connection** restarts the lifecycle state machine from Phase 1 without requiring an app relaunch.

---

## 9. Minimal Resource Visibility (Zero New Dependencies)

Carbon already collects rich system status via `src/carbon/devices/manager.py` and returns it in `GET /api/v1/health` under the `"host"` key.

### 9.1 Available Real Telemetry
From [`src/carbon/devices/manager.py:36-45`](../../src/carbon/devices/manager.py#L36-L45):
- Host model and macOS version (`Darwin 27.0.1`, `Apple Silicon`)
- Total unified memory (`unified_memory_gb: 16.0`)
- Active CPU cores (`cpu_count: 8`)
- Active model ID (`active_model.id: "qwen3.5:9b"`)
- Active context length (`active_model.context_length: 4096`)
- Model memory footprint (`active_model.memory_footprint_mb: 5824 MB`)
- Ollama connection state (`ollama_connected: true`)
- Core orchestrator state (`core_running: true`)

### 9.2 Header Bar Resource Display Design
In `ContentView.swift:headerView`, enhance the existing header bar with a compact, native status group:

```
[cpu] CARBON   [Model: qwen3.5:9b]   [Memory: 16 GB]   [Runtime: Ollama (External)]   [ONLINE]
```
Clicking the status pill opens an unobtrusive SwiftUI popover detailing:
- **Carbon Backend:** `127.0.0.1:8765 (Managed PID: 14210)`
- **Inference Runtime:** `Ollama 0.33.0 (Homebrew Service, PID: 7593)`
- **Active Model:** `qwen3.5:9b (Q4_K_M, 33/33 Metal Layers)`
- **Host Memory:** `16.0 GB Unified Memory`
- **Active Tasks:** `0 queued / 1 executing`

*No third-party monitoring libraries or invasive CPU sampling threads will be introduced.*

---

## 10. Comprehensive Verification & Testing Plan

### Part A: Automated Unit & Integration Tests (Mocked/Scripted)

| Test ID | Test Name | Target File | Verification Method |
|---|---|---|---|
| **T-11.01** | `test_health_endpoint_degraded_when_ollama_down` | `tests/unit/test_orchestrator.py` | Mock `provider.is_healthy() -> False`, assert health returns `status: "degraded"`. |
| **T-11.02** | `test_health_endpoint_exposes_host_memory` | `tests/integration/test_api_endpoints.py` | Verify `GET /api/v1/health` includes `host.unified_memory_gb` and `host.cpu_count`. |
| **T-11.03** | `test_missing_model_detection` | `tests/unit/test_cms.py` | Mock Ollama tags without `qwen3.5:9b`, verify CMS returns `None` for active model and flags missing. |
| **T-11.04** | `test_auth_token_disk_synchronization` | `tests/test_desktop_connection_reliability.py` | Verify that desktop client waits for `.carbon_token` creation if backend is starting fresh. |
| **T-11.05** | `test_process_supervisor_state_machine` | `apps/macos/Carbon/Tests/test_lifecycle.swift` | Mock process launcher and verify state transitions: `.startingOllama` $\to$ `.checkingModel` $\to$ `.startingBackend` $\to$ `.connecting` $\to$ `.ready`. |
| **T-11.06** | `test_stale_pid_rejection` | `apps/macos/Carbon/Tests/test_lifecycle.swift` | Feed descriptor with dead PID and mismatched executable; verify supervisor treats it as absent. |

### Part B: Real macOS Process Lifecycle Scenarios

The 15 mandatory real-world scenarios:

1. **Scenario 1 — Ollama already running:**
   - Pre-condition: Ollama is running via Homebrew (`PID 7593`).
   - Action: Launch Carbon.
   - Verification: App connects to port 11434, logs `ollamaOwned = false`, does not spawn a second Ollama process.
2. **Scenario 2 — Ollama stopped before Carbon opens:**
   - Pre-condition: Ollama is terminated (`brew services stop ollama`).
   - Action: Launch Carbon.
   - Verification: App discovers absence, spawns `/opt/homebrew/bin/ollama serve`, polls `/api/version`, transitions to `.checkingModel`. Logs `ollamaOwned = true`.
3. **Scenario 3 — Backend already running:**
   - Pre-condition: Backend is running in background terminal.
   - Action: Launch Carbon.
   - Verification: App probes `GET /api/v1/health`, verifies `"core_running": true`, logs `backendOwned = false`, does not spawn duplicate Python process.
4. **Scenario 4 — Backend absent:**
   - Pre-condition: Backend is not running on port 8765.
   - Action: Launch Carbon.
   - Verification: App detects connection refused, spawns `.venv/bin/python -m carbon.main`, polls `/api/v1/health`, connects WebSocket, logs `backendOwned = true`.
5. **Scenario 5 — Missing model in Ollama:**
   - Pre-condition: Ollama running, but `qwen3.5:9b` temporarily removed or renamed.
   - Action: Launch Carbon.
   - Verification: App transitions to `.error("Required model 'qwen3.5:9b' is missing in Ollama")`. UI shows error and instructions.
6. **Scenario 6 — Ollama startup failure (corrupted binary):**
   - Pre-condition: Ollama path overridden with invalid binary.
   - Action: Launch Carbon.
   - Verification: Process launch fails immediately; app transitions to `.error` with `Retry` button.
7. **Scenario 7 — Backend health timeout:**
   - Pre-condition: Backend spawned but blocked on simulated deadlock/hang.
   - Action: Launch Carbon.
   - Verification: App polls for 15.0s, times out, flags startup failure, and does not hang UI.
8. **Scenario 8 — Duplicate application launch:**
   - Pre-condition: Carbon Instance A is running.
   - Action: Launch Carbon Instance B from terminal or Finder.
   - Verification: Instance B detects existing healthy backend on 8765, attaches cleanly over WebSocket, does not spawn duplicate backend, and sets `backendOwned = false`.
9. **Scenario 9 — Quit during active inference:**
   - Pre-condition: Carbon actively generating a 60-second Standard response.
   - Action: Press `Cmd+Q`.
   - Verification: App sends `cancel` frame; backend logs cancellation; socket closes cleanly; no crash or hang.
10. **Scenario 10 — Carbon-owned Ollama shutdown:**
    - Pre-condition: Carbon started Ollama in Scenario 2 (`ollamaOwned = true`).
    - Action: Quit Carbon.
    - Verification: Carbon sends SIGTERM to the owned Ollama process; process exits within 3s.
11. **Scenario 11 — Pre-existing Ollama remains running:**
    - Pre-condition: Carbon reused external Ollama in Scenario 1 (`ollamaOwned = false`).
    - Action: Quit Carbon.
    - Verification: Carbon quits; Ollama process `PID 7593` remains running, healthy, and untouched.
12. **Scenario 12 — Carbon-owned backend shutdown:**
    - Pre-condition: Carbon started backend in Scenario 4 (`backendOwned = true`).
    - Action: Quit Carbon.
    - Verification: Carbon sends SIGTERM to backend; Python process exits cleanly; port 8765 is freed.
13. **Scenario 13 — Stale process identity handling:**
    - Pre-condition: Mock `data/.carbon_desktop_lifecycle.json` with PID of an active system process (e.g. `launchd` or `Finder`).
    - Action: Launch Carbon.
    - Verification: App checks process name/path; detects that PID is not Carbon; does NOT terminate it; purges stale file and starts clean backend.
14. **Scenario 14 — Port conflict with foreign application:**
    - Pre-condition: An unrelated dummy HTTP server (`python3 -m http.server 8765`) is running on port 8765.
    - Action: Launch Carbon.
    - Verification: App probes `GET /api/v1/health`, fails Carbon JSON signature check, transitions to `.error("Port 8765 is in use by another application")`. Does not kill foreign server.
15. **Scenario 15 — Retry action after recovery:**
    - Pre-condition: Startup failed at Phase 1 because Ollama was stopped.
    - Action: User starts Ollama in terminal and clicks "Retry Connection" in Carbon UI.
    - Verification: App re-enters Phase 1, succeeds, progresses through all phases, and displays `.ready` chat interface.

---

## 11. Proposed Implementation Steps (For Future Authorization)

When approved for implementation in Stage 1.11, the work should be structured in 4 sequential increments:

1. **Step 1 — Backend Health & Graceful Shutdown Endpoint:**
   - In [`src/carbon/main.py`](../../src/carbon/main.py), add an authenticated `POST /api/v1/system/shutdown` endpoint (loopback only) that invokes `core.shutdown()` and triggers a graceful Uvicorn server stop.
   - This provides a clean HTTP-level shutdown path in addition to POSIX SIGTERM.
2. **Step 2 — ProcessSupervisor Module in Swift:**
   - Create `apps/macos/Carbon/Sources/ProcessSupervisor.swift`.
   - Implement process probing (HTTP `/api/version` and `/api/v1/health`), process launching (`Process`), ownership tracking, and graceful termination (SIGTERM $\to$ timeout $\to$ SIGKILL).
3. **Step 3 — App & Client Lifecycle Integration:**
   - Update `CarbonClient.swift` to introduce `AppLifecycleState` and bind to `ProcessSupervisor`.
   - Update `AppDelegate` in `CarbonApp.swift` to implement `applicationWillTerminate(_:)` and coordinate clean shutdown.
4. **Step 4 — SwiftUI Lifecycle & Status Overlays:**
   - In `ContentView.swift`, add startup progress cards, error/retry views, and the minimal resource display popover in `headerView`.

---

## 12. Security & Governance Considerations

1. **Loopback-Only Invariance:**
   All spawned processes and probes bind strictly to `127.0.0.1`. No non-loopback network listening or external exposure is permitted.
2. **Token File Permissions:**
   The backend generates `data/.carbon_token` with POSIX permissions `0600` (read/write by owner only). The Swift client resolves this token securely without passing credentials via CLI arguments or URLs.
3. **Privilege Boundary:**
   Carbon runs strictly as a non-root standard user process. It does not require or request `sudo` privileges. If Ollama requires installation, Carbon displays user-actionable instructions rather than attempting unauthenticated system-level package installation.

---

## 13. Files Expected to Change During Implementation

| File Path | Scope of Expected Changes |
|---|---|
| [`apps/macos/Carbon/Sources/ProcessSupervisor.swift`](../../apps/macos/Carbon/Sources) *(New)* | Process probing, spawning, ownership tracking, PID verification, and shutdown. |
| [`apps/macos/Carbon/Sources/CarbonClient.swift`](../../apps/macos/Carbon/Sources/CarbonClient.swift) | Add `AppLifecycleState`, connect/retry orchestration with supervisor. |
| [`apps/macos/Carbon/Sources/CarbonApp.swift`](../../apps/macos/Carbon/Sources/CarbonApp.swift) | Add `applicationWillTerminate` in `AppDelegate` for clean shutdown. |
| [`apps/macos/Carbon/Sources/ContentView.swift`](../../apps/macos/Carbon/Sources/ContentView.swift) | Add startup/error/retry overlay views and resource indicators. |
| [`src/carbon/main.py`](../../src/carbon/main.py) | Add `POST /api/v1/system/shutdown` endpoint for authenticated clean stop. |
| [`apps/macos/Carbon/Tests/test_lifecycle.swift`](../../apps/macos/Carbon/Tests) *(New)* | Automated Swift unit tests for lifecycle state transitions and PID verification. |

---

## 14. Unresolved Technical Decisions for Project Owner Review

1. **Ollama Installation Path Resolution:**
   - On macOS Apple Silicon, Homebrew installs Ollama to `/opt/homebrew/bin/ollama`. If installed via the official standalone dmg, it resides at `/Applications/Ollama.app/Contents/Resources/ollama` or `/usr/local/bin/ollama`.
   - *Recommendation:* Check `/opt/homebrew/bin/ollama` first, fallback to `/usr/local/bin/ollama`, then `/Applications/Ollama.app/...`, and finally query `which ollama` via user environment PATH.
2. **Backend Packaging in Development vs Release:**
   - In the development repository, the backend is run via `./.venv/bin/python -m carbon.main`.
   - In a bundled `.app` release, Python could either be embedded via PyInstaller / standalone runtime or executed from the user's Carbon virtualenv.
   - *Recommendation:* For Stage 1.11, target the project virtualenv path (`~/Carbon/.venv/bin/python`), falling back to environment variable `CARBON_PYTHON_PATH`.
3. **HTTP Shutdown vs POSIX SIGTERM:**
   - Should Carbon shut down the backend via `POST /api/v1/system/shutdown` or POSIX `kill(pid, SIGTERM)`?
   - *Recommendation:* Use both: attempt `POST /api/v1/system/shutdown` first (allows Uvicorn to flush logs and Core to record governance audit); if unresponsive after 1.5s, dispatch POSIX `SIGTERM`.

---

## 15. Audit Conclusion

The existing Carbon macOS desktop application has a clean UI and solid WebSocket streaming foundation, but completely lacks process lifecycle management. Implementing the proposed `ProcessSupervisor` will make Carbon a true standalone desktop application that requires zero terminal intervention, safely respects existing processes, and cleanly manages its own resources.

**This audit is strictly read-only. No code changes have been made. Ready for project owner review.**

