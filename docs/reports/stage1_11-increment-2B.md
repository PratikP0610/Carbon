# Carbon Stage 1.11 — Increment 2B Final Delivery & Acceptance Audit Report
## Ollama and Python Backend Process Supervision Foundation & Lifecycle Hardening

**Task ID:** `C-1.11-P2-2B-AUDIT` / `C-1.11-P2-2B-FIX` / `C-1.11-P2-2B-FINAL-VERIFY`  
**Phase:** Swift Process Supervision Foundation  
**Mode:** Controlled implementation, lifecycle audit, testing, and independent verification  
**Date:** 2026-11-02  
**Status:** COMPLETE & VERIFIED (Awaiting Increment 2C Authorization)

---

## 1. Executive Summary

Increment 2B implements, audits, and hardens the modular process-supervision foundation (`ProcessSupervisor.swift`) that will enable the Carbon macOS desktop application to detect, start, monitor, and clean up its Ollama and Python backend dependencies.

Following final acceptance review, this increment resolves all process-lifecycle termination concerns:
1. **Explicit Termination Outcome (`ProcessTerminationResult`)**: Replaced void returns with an explicit result enum (`.exited(terminationStatus: Int32)` vs. `.timedOut`). Successful termination is **never** assumed or reported unless exit has been confirmed by the kernel.
2. **Safe Pipe Lifecycle on Incomplete Exit**: If a child takes longer than the timeout or ignores SIGTERM, pipe file descriptors are **not** closed while the child is still executing. This prevents `SIGPIPE` crashes and pipe descriptor invalidation in active child processes.
3. **Ownership Retention on Timeout**: If exit is not confirmed within the deadline, the supervisor retains the `ProcessRunning` handle and maintains `.owned` ownership state. It never falsely transitions to `.notRunning` or orphans running children in the OS.
4. **Genuine Asynchronous Pipe Draining (`PipeDrainer`)**: Actively consumes available bytes via `handle.availableData` to prevent kernel pipe buffer deadlocks (>64KB) during high-throughput stdout/stderr output.
5. **Zero Descriptor Leaks**: Handlers are detached (`readabilityHandler = nil`) and descriptors closed safely upon EOF or confirmed exit, with thread-safe idempotent cleanup.
6. **Zero Raw-PID Signalling**: All process controls operate exclusively through retained `Foundation.Process` handles rather than raw POSIX integer PID signaling, completely eliminating race conditions and PID recycling vulnerabilities.
7. **External Process Preservation (Zero-Adoption Invariant)**: Pre-existing external processes (Ollama or Python backend) are detected, monitored, and never terminated or adopted by the supervisor during cleanup.
8. **Zero Automated Pull Requests**: Verifies model existence via `GET /api/tags`. If missing, throws `.modelNotFound("qwen3.5:9b")` with manual instructions; never issues automated pull requests.

---

## 2. Process Termination & Timeout Behaviour Audit

### 2.1 Investigation of `ManagedProcess.terminateAndAwaitExit(timeout:)`

| Scenario | Behaviour in Hardened Implementation | Safety Guarantee |
|---|---|---|
| **Child exits normally before deadline** | Sends SIGTERM (`process.terminate()`), polls `isRunning` at 10ms intervals until exit is observed, executes `cleanupPipes()`, and returns `.exited(terminationStatus: exitCode)`. | Process exit confirmed; pipes cleanly detached and closed; zero descriptor leak. |
| **Child takes longer than deadline** | Polling loop exhausts `Date() < deadline` while `isRunning == true`. `cleanupPipes()` is **bypassed**. Returns `.timedOut`. | Pipes remain open so child can continue writing; process handle is retained. |
| **Child ignores SIGTERM** (e.g. `signal(SIGTERM, SIG_IGN)`) | Child remains running after SIGTERM. Polling loop reaches deadline. Returns `.timedOut`. Supervisor retains child process handle and preserves `.owned` state. | No false success; no orphaning; supervisor knows child is still running. |
| **Child is writing while termination is underway** | `PipeDrainer` readability handlers remain active during the wait loop, consuming incoming bytes and discarding them safely without deadlock. Handlers are detached only after `!isRunning`. | Child never blocks on pipe write buffers during graceful shutdown. |

### 2.2 Core Implementations

#### `ProcessTerminationResult` & `ProcessRunning` Protocol
```swift
public enum ProcessTerminationResult: Equatable, Sendable {
    case exited(terminationStatus: Int32)
    case timedOut
}

public protocol ProcessRunning: AnyObject, Sendable {
    var isRunning: Bool { get }
    var processIdentifier: Int32 { get }
    var terminationHandler: (@Sendable (any ProcessRunning) -> Void)? { get set }
    func terminate()
    @discardableResult
    func terminateAndAwaitExit(timeout: TimeInterval) async -> ProcessTerminationResult
    @discardableResult
    func awaitExit(timeout: TimeInterval) async -> ProcessTerminationResult
}
```

#### Hardened `terminateAndAwaitExit` in `ManagedProcess`
```swift
@discardableResult
public func terminateAndAwaitExit(timeout: TimeInterval = 2.0) async -> ProcessTerminationResult {
    guard isRunning else {
        cleanupPipes()
        return .exited(terminationStatus: process.terminationStatus)
    }

    // Request graceful exit via SIGTERM
    process.terminate()

    let deadline = Date().addingTimeInterval(timeout)
    while isRunning && Date() < deadline {
        try? await Task.sleep(nanoseconds: 10_000_000) // 10ms
    }

    if !isRunning {
        // Process confirmed exited: safe to detach handlers and close pipes
        cleanupPipes()
        return .exited(terminationStatus: process.terminationStatus)
    } else {
        // Process did NOT exit within deadline: do NOT close pipes while still running
        return .timedOut
    }
}
```

#### Safe Ownership Retention in `ProcessSupervisor`
```swift
if let child = _childOllamaProcess {
    let res = await child.terminateAndAwaitExit(timeout: 2.0)
    if case .exited = res {
        _childOllamaProcess = nil
        ollamaOwnership = .notRunning
    }
    // If res == .timedOut, _childOllamaProcess is retained and ownership remains .owned
}
```

---

## 3. Test Coverage & Verification Evidence

The test suite was expanded to **33 comprehensive test scenarios** in `apps/macos/Carbon/Tests/test_process_supervisor.swift`.

### 3.1 Test Output
```
=== CARBON STAGE 1.11 INCREMENT 2B: PROCESS SUPERVISOR & PIPE-DRAIN TESTS ===
[1/33] Verifying existing Ollama process is classified as external...
       ✓ Existing Ollama classified as .external, no child process adopted
[2/33] Verifying existing backend process is classified as external...
       ✓ Existing backend classified as .external, no child process adopted
[3/33] Verifying newly spawned Ollama child is classified as owned...
       ✓ Newly spawned Ollama classified as .owned with retained process handle
[4/33] Verifying newly spawned backend child is classified as owned...
       ✓ Newly spawned backend classified as .owned with retained process handle
[5/33] Verifying process ownership resets to notRunning when child process terminates...
       ✓ Process ownership cleanly reset to .notRunning upon child termination
[6/33] Verifying external processes are never terminated by supervisor cleanup...
       ✓ Zero-adoption invariant verified: External processes untouched during cleanup
[7/33] Verifying Ollama binary resolution search order...
       ✓ Search order strictly honored: /opt/homebrew -> /usr/local -> /Applications -> PATH
[8/33] Verifying missing Ollama executable produces actionable error...
       ✓ Actionable error returned when Ollama executable is not found
[9/33] Verifying installed qwen3.5:9b model passes verification...
       ✓ Model 'qwen3.5:9b' successfully verified in installed tags
[10/33] Verifying missing model fails with exact manual pull instructions...
       ✓ Missing model threw exact manual pull instruction: "Model 'qwen3.5:9b' is not installed. Run: ollama pull qwen3.5:9b"
[11/33] Verifying model verification never issues an automated pull request...
       ✓ Invariant verified: Zero automated pull requests dispatched
[12/33] Verifying backend reuse detects existing healthy instance...
       ✓ Existing healthy backend detected and reused without launching child
[13/33] Verifying backend startup uses correct virtualenv Python and PYTHONPATH...
       ✓ Spawned backend with .venv/bin/python -m carbon.main and PYTHONPATH=/var/.../src
[14/33] Verifying degraded or wrong active model is detected and rejected...
         Sub-case 1: Rejected degraded state as expected: active model 'llama3.2:1b' does not match required 'qwen3.5:9b'
         Sub-case 2: Rejected degraded state as expected: core_running is false
         Sub-case 3: Rejected degraded state as expected: ollama_connected is false
         Sub-case 4: Rejected degraded state as expected: status is 'initializing'
       ✓ Degraded telemetry contract cases verified and rejected
[15/33] Verifying backend startup timeout is enforced...
       ✓ Backend timeout enforced and unready child process terminated
[16/33] Verifying unsuccessful child startup cleans up child process...
       ✓ Degraded child cleaned up immediately upon health rejection
[17/33] Verifying concurrent startup() calls are deduplicated...
       ✓ Concurrent startup() calls serialized and deduplicated into a single task
[18/33] Verifying process stdout/stderr pipe handlers are attached...
       ✓ DefaultProcessLauncher successfully configured asynchronous pipe drains
[19/33] Verifying cleanup preserves external processes and terminates owned children...
       ✓ Ownership-bounded cleanup strictly preserved external process while terminating owned child
[20/33] Verifying stdout > pipe buffer (256KB) drains asynchronously without blocking...
       ✓ Genuine stdout drain consumed 262144 bytes without blocking child
[21/33] Verifying stderr > pipe buffer (256KB) drains asynchronously without blocking...
       ✓ Genuine stderr drain consumed 262144 bytes without blocking child
[22/33] Verifying child processes exit cleanly with exit code 0...
       ✓ Child exited with status 0 after concurrent stdout/stderr streams
[23/33] Verifying readabilityHandlers are detached after termination...
       ✓ Both stdout and stderr readabilityHandlers cleared and file descriptors closed
[24/33] Verifying safe cleanup when termination occurs during active pipe reads...
       ✓ Abrupt termination during active reads completed safely without deadlocks or crashes
[25/33] Verifying Ollama service still starting (HTTP 503) does not trigger duplicate launch...
       ✓ In-progress starting Ollama service awaited and reused without duplicate spawn
[26/33] Verifying failed child startup terminates child and awaits exit...
       ✓ Child termination and exit cleanly awaited within bounded deadline
[27/33] Verifying duplicate launch prevented when owned child is already starting...
       ✓ Sequential and concurrent ensureOllamaRunning calls safely shared the active child
[28/33] Verifying child exits normally after SIGTERM with confirmed exit...
       ✓ Confirmed exit returned after SIGTERM: exited(terminationStatus: 15)
[29/33] Verifying child that ignores SIGTERM reports .timedOut and does NOT close pipes...
       ✓ .timedOut reported accurately; pipes were safely kept open
[30/33] Verifying child continues writing while termination is underway without pipe deadlocks...
       ✓ Active writes consumed safely during termination without deadlocks
[31/33] Verifying ownership is preserved when termination exit is not confirmed...
       ✓ Unconfirmed exit preserved .owned ownership and process handle
       ✓ Ownership cleanly transitioned to .notRunning once exit was actually confirmed
[32/33] Verifying repeated cleanup is idempotent and safe...
       ✓ Repeated cleanup calls were completely idempotent with zero crashes
[33/33] Verifying external processes are preserved during termination calls...
       ✓ External processes completely untouched and preserved

=== ALL 33 PROCESS SUPERVISOR TESTS PASSED SUCCESSFULLY ===
```

### 3.2 Regression Verification Summary

| Test Suite | Result | Execution Time | Exit Code | Scope / Commands |
|---|---|---|---|---|
| `test_process_supervisor.swift` | **33 / 33 PASS** | ~3.8s | 0 | Full ownership, pipe draining, termination timeout, unconfirmed exit handling, and external process preservation |
| `test_single_instance_lock.swift` | **8 / 8 PASS** | ~0.4s | 0 | Increment 2A lock enforcement and kernel flock cleanup |
| `test_markdown.swift` | **ALL PASS** | ~0.1s | 0 | Markdown parsing, UI streaming, and stop-button responsiveness |
| Swift Package Build | **Clean Build** | ~1.5s | 0 | `swift build --package-path apps/macos/Carbon` |
| Python Backend Tests | **86 / 86 PASS** | **5.16s** | **0** | Clean, synchronous, un-cancelled execution across `tests/test_desktop_connection_reliability.py`, `tests/integration/`, `tests/security/`, and `tests/unit/` |
| Python Linter | **0 errors** | ~0.2s | 0 | `ruff check src/ tests/` |
| Git Whitespace Check | **0 errors** | ~0.1s | 0 | `git diff --check` |

#### Resolution of Python Suite Exit Discrepancy
- **Root Cause Identified**: In `tests/integration/test_api_endpoints.py`, `test_task_submission_and_query` initializes `CarbonCore` via `POST /api/v1/tasks`. Under `httpx.ASGITransport`, FastAPI's lifespan teardown does not run. Consequently, `aiosqlite`'s non-daemon worker thread remained alive awaiting work. When pytest completed the tests, Python's runtime hung in `Py_FinalizeEx` / `wait_for_thread_shutdown` waiting on the unclosed thread.
- **Correction Applied**: Explicit cleanup of `carbon.main.core` was added to `test_task_submission_and_query`, awaiting `core.shutdown()` and clearing the reference.
- **Verification**: Pytest was executed independently:
  ```bash
  .venv/bin/pytest tests/test_desktop_connection_reliability.py tests/integration/ tests/security/ tests/unit/
  ```
  Result:
  ```
  86 passed, 1 warning in 5.16s
  ```
  The process exited cleanly with status code `0` in 5.16 seconds without any cancellation or signal intervention.

---

## 4. Distinct Process Lifecycle States & Remaining Limitations

### 4.1 Distinction: Confirmed Exit vs. Timeout
- **Confirmed Process Exit (`.exited(terminationStatus:)`)**: The operating system kernel reports `isRunning == false` for the child process. It is safe to detach readability handlers, close pipe file descriptors, clear the retained child handle, and transition ownership to `.notRunning`.
- **Termination Timeout (`.timedOut`)**: The wait deadline expired while `isRunning` was still `true`. The child has NOT exited. The supervisor preserves all pipe handlers (preventing `SIGPIPE`), retains the `ProcessRunning` handle, and maintains `.owned` ownership.

### 4.2 Handling of Uncooperative Children & Force Termination
- **Zero Raw-PID Signalling Policy**: The supervisor strictly forbids sending signals (such as `kill(pid, SIGKILL)`) to arbitrary numeric PIDs. POSIX PIDs are rapidly recycled by the operating system kernel once a process terminates; signaling raw integer PIDs creates severe hazards of terminating unrelated system processes.
- **Current Limitation**: In Increment 2B, if a child process ignores SIGTERM, the supervisor reports `.timedOut` and keeps the child handle retained. The child is not forcefully killed via SIGKILL.
- **Future Hardening in Phase 2D (Graceful Shutdown)**: In Increment 2D, the application-level shutdown coordinator will define escalation policies (HTTP shutdown -> SIGTERM via supervisor -> bounded wait -> escalating to `kill(process.processIdentifier, SIGKILL)` only while validating the still-active retained process handle within the application supervisor context).

---

## 5. Scope and Safety Verification

All authorization restrictions were strictly preserved:
- **No Modifications to `CarbonApp.swift` or `CarbonClient.swift`**: These files were not modified during Increment 2B.
- **No Premature State Machine Integration**: `ProcessSupervisor` is implemented as an uncoupled, modular component. It is not connected to `CarbonClient` or the application startup UI (deferred to Increment 2C).
- **No Premature Shutdown Integration**: `ProcessSupervisor` is not registered with `AppDelegate` or any termination delegates (deferred to Increment 2D).
- **Zero Model Downloads**: No automated model pulls or downloads were performed or permitted.
- **Zero Service Interference**: Production services were not stopped, restarted, or altered.
- **Strictly Read-Only Git**: No Git commits, branch switches, or push operations were performed. The backup branch `backup/carbon-tested-phase1-2026-10-29` and commit `0c7d1d17c55c836715438c7d0c00887cf9aaf19c` remain intact.

---

## 6. Stop Condition

Increment 2B is fully completed, audited, hardened, and verified with zero discrepancy.
Implementation of Increment 2C (Startup State Machine Integration) is on hold pending explicit project-owner authorization.
