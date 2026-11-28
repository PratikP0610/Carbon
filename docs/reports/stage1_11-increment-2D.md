# Carbon Stage 1.11 — Increment 2D Delivery Report
## Graceful Asynchronous Application Shutdown

**Task ID:** `C-1.11-P2-2D`  
**Phase:** Application Shutdown & Watchdog Integration  
**Mode:** Controlled implementation and verification  
**Date:** 2026-11-25  
**Status:** COMPLETE & VERIFIED (Awaiting Project-Owner Review & Acceptance)

---

## 1. Summary of Implementation

Increment 2D implements asynchronous, ownership-safe application shutdown for Carbon's macOS SwiftUI desktop client across `CarbonClient`, `ProcessSupervisor`, `AppDelegate`, and `ContentView`, strictly adhering to all approved phase budgets and the 8.0-second global watchdog.

Key architectural guarantees:
- **AppKit `.terminateLater` Lifecycle:** `AppDelegate.applicationShouldTerminate(_:)` coordinates async teardown on `@MainActor`, preventing multiple concurrent shutdown runs and replying to AppKit **exactly once** (`.terminateNow` equivalence via `reply(true)` when clean; `.terminateCancel` equivalence via `reply(false)` if an owned child refuses to exit).
- **Zero-Adoption Invariant Preserved:** Pre-existing `.external` Ollama and Python backend instances receive zero HTTP shutdown requests and zero termination signals.
- **Confirmed Exit Required:** Ownership (`.owned`) and retained `Foundation.Process` handles are cleared **only** after verifying `!child.isRunning`. No raw POSIX integer PID signaling and no `SIGKILL` fallback were introduced.
- **Persistent Lock Inode Safety:** `SingleInstanceLock.shared.release()` unlocks and closes the lock file descriptor idempotently upon clean shutdown without unlinking the persistent lock file inode.

---

## 2. Files Changed

| File | Action | Purpose |
|---|---|---|
| `apps/macos/Carbon/Sources/CarbonClient.swift` | Modified | Added `isShuttingDown`, `cancelActiveInferenceAndWait(timeout:)` (Phase 1), `closeWebSocketForShutdown(timeout:)` (Phase 2), `resetShutdownStateAfterCancelledQuit()`, `pendingCancelAckTaskId` ack clearing, session invalidation/recreation safety, and post-shutdown prompt/reconnect guards. |
| `apps/macos/Carbon/Sources/ProcessSupervisor.swift` | Modified | Added `ShutdownTimeoutConfig` (`.standard`: `1.2 / 0.3 / 3.0 / 2.0 / 0.3`, `globalWatchdog: 8.0s`), `ShutdownState`, `OwnedProcessShutdownOutcome`, `ShutdownReport`, `ProcessSupervisorHTTPClient.send(_:)`, `resolveLocalAuthToken()`, post-shutdown spawn guards inside startup & launch paths, and `shutdown(client:budgets:lockRelease:)` with `@MainActor` watchdog race. |
| `apps/macos/Carbon/Sources/CarbonApp.swift` | Modified | Wired `AppDelegate.configure(client:supervisor:)`, implemented `applicationShouldTerminate(_:)` with `.terminateLater` + duplicate-quit prevention + single `reply(toApplicationShouldTerminate:)`, keeping `AppDelegate.applicationWillTerminate` idempotent lock release. |
| `apps/macos/Carbon/Sources/ContentView.swift` | Modified | Added `shutdownBannerView` (spinner + active shutdown status, or failure warning with `"Retry Quit"` and `"Dismiss"` buttons) and `"STOPPING"` / `"QUIT FAILED"` header status pill states while leaving chat UI intact. |
| `apps/macos/Carbon/Tests/test_startup_state_machine.swift` | Modified | Updated `TestMockCarbonClient` with the 3 new `CarbonClientConnecting` shutdown protocol methods. |
| `apps/macos/Carbon/Tests/test_shutdown.swift` | Created | Added 16 comprehensive automated tests covering all Increment 2D shutdown scenarios, ownership invariants, watchdog expiry, flock release, retry recovery, and live ephemeral Uvicorn E2E shutdown. |

---

## 3. Shutdown Sequence and Timeout Handling

| Phase | Operation | Budget | Behaviour & Bounds |
|---|---|---|---|
| **Phase 1** | Cancel active inference | `1.2s` | `CarbonClient.cancelActiveInferenceAndWait` sets `isShuttingDown = true`, disables `autoReconnectEnabled`, sends `{"action": "cancel", "task_id": tid}` over existing WS protocol if `isGenerating` / `currentTaskId != nil`, awaits terminal ack (`cancelled`/`completed`/`failed`) up to `1.2s`, or skips immediately in 0ms when idle. |
| **Phase 2** | Close WebSocket | `0.3s` | `CarbonClient.closeWebSocketForShutdown` cancels `reconnectTask`, `healthCheckTask`, and `submissionTimeoutTask`, closes `webSocketTask` with `.goingAway`, invalidates `URLSession`, and blocks new reconnects/inference. |
| **Phase 3** | Stop backend (only if `.owned`) | `3.0s` | `.external` is left untouched (zero HTTP/process calls). If `.owned`, resolves local auth token without logging, sends bounded `POST http://127.0.0.1:8765/api/v1/system/shutdown` with Bearer token, awaits exit (`awaitExit`), falls back to `Process.terminate()` via retained handle within remaining Phase 3 budget if needed, and clears ownership only if `!child.isRunning`. |
| **Phase 4** | Stop Ollama (only if `.owned`) | `2.0s` | `.external` is left untouched. If `.owned`, calls `terminateAndAwaitExit` on the retained `Foundation.Process` handle within `2.0s` and clears ownership only after confirmed exit. |
| **Phase 5** | Lock & state cleanup | `0.3s` | Only if **zero** `.owned` children remain running: marks `hasCompletedShutdown = true` (blocking future startups/retries), calls idempotent `SingleInstanceLock.shared.release()`, sets `.completed`, and returns `canTerminate = true`. |
| **Global Watchdog** | Overarching deadline | `8.0s` | Concurrent `@MainActor` timer races `workTask` via `withCheckedContinuation`. On expiry, cancels `workTask`, forces `disconnectWebSocket()`, preserves `.owned` for any unconfirmed child, does **not** release `SingleInstanceLock`, sets `.failed`, and returns `canTerminate = false`. |

Total sum of individual phase budgets = **6.8s** (`1.2 + 0.3 + 3.0 + 2.0 + 0.3`), giving **1.2s** headroom inside the **8.0s** global watchdog.

---

## 4. Ownership Safety Verification

1. **External Processes Preserved:** Verified in Tests 1, 4, 6, 8, and 16 (`report.backendHttpRequested == false`, `backendOutcome == .preservedExternal`, `ollamaOutcome == .preservedExternal`).
2. **Owned Processes Signalled via Retained Handles Only:** Verified in Tests 5, 6, and 14. Zero raw-PID `kill(pid, ...)` calls exist anywhere in Swift sources.
3. **Timeouts Retain Ownership & Cancel Quit:** Verified in Tests 7, 8, 11, and 13: when a child ignores SIGTERM (`terminateAndAwaitExit` returns `.timedOut`) or when the global watchdog fires, ownership remains `.owned`, the child handle remains retained, `SingleInstanceLock` remains held, `canTerminate == false` cancels termination safely, and `ContentView` offers a working `"Retry Quit"` button.
4. **Zero Process Spawns During/After Shutdown:** Verified in Test 12: `isShuttingDown` and `hasCompletedShutdown` reject `startup()`, `ensureOllamaRunning()`, and `ensureBackendRunning()` before `processLauncher.launchProcess`.

---

## 5. `AppDelegate` Integration Details (Code Audit)

- **Location:** `apps/macos/Carbon/Sources/CarbonApp.swift` lines 4–90 and lines 318–331.
- **Coordination:** Uses weak references to the single `@StateObject` `CarbonClient` and `@StateObject` `ProcessSupervisor` instances registered in `CarbonApp.body` (`.onAppear` and `.task`). Zero duplicate client/supervisor objects exist.
- **Threading & AppKit Contract:** `applicationShouldTerminate(_:)` uses `MainActor.assumeIsolated` (AppKit dispatches delegate callbacks on the main thread) to check `hasCompletedShutdown` and `terminationInFlight`, prevents duplicate concurrent shutdown tasks, returns `.terminateLater`, and invokes `sender.reply(toApplicationShouldTerminate: report.canTerminate)` strictly once per termination cycle.
- **Direct Interactive GUI Verification:** Not executed live because neither Ollama nor Python backend is running locally (`curl` returned HTTP `000`), and starting/stopping production services is disallowed in this task.

---

## 6. Automated Test, Build & Lint Results

All automated checks below were executed cleanly **after the final code edit**:

| Suite / Command | Result | Exit Code | Evidence Category |
|---|---|---|---|
| `swiftc -parse-as-library apps/macos/Carbon/Sources/SingleInstanceLock.swift apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Sources/ProcessSupervisor.swift apps/macos/Carbon/Tests/test_shutdown.swift -o /tmp/test_shutdown && /tmp/test_shutdown` | **16 / 16 PASS** | `0` | Automated (mocks + real `CarbonClient`, `SingleInstanceLock`, and live ephemeral Uvicorn WS/HTTP process) |
| `swiftc -parse-as-library apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Sources/ProcessSupervisor.swift apps/macos/Carbon/Tests/test_startup_state_machine.swift -o /tmp/test_startup_state_machine && /tmp/test_startup_state_machine` | **16 / 16 PASS** | `0` | Automated (Increment 2C regression) |
| `swiftc -parse-as-library apps/macos/Carbon/Sources/ProcessSupervisor.swift apps/macos/Carbon/Tests/test_process_supervisor.swift -o /tmp/test_process_supervisor && /tmp/test_process_supervisor` | **33 / 33 PASS** | `0` | Automated (Increment 2B regression) |
| `swiftc -parse-as-library apps/macos/Carbon/Sources/SingleInstanceLock.swift apps/macos/Carbon/Tests/test_single_instance_lock.swift -o /tmp/test_single_instance_lock && /tmp/test_single_instance_lock` | **8 / 8 PASS** | `0` | Automated (Increment 2A regression) |
| `swiftc -parse-as-library apps/macos/Carbon/Sources/MarkdownRenderer.swift apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Tests/test_markdown.swift -o /tmp/test_markdown && /tmp/test_markdown` | **ALL PASS** | `0` | Automated (UI/Markdown/Cancel regression) |
| `swift build --package-path apps/macos/Carbon` | **Build complete! (1.41 secs)** | `0` | Automated Swift 6 package build |
| `.venv/bin/pytest tests/test_desktop_connection_reliability.py tests/integration/ tests/security/ tests/unit/` | **85 passed, 1 skipped, 1 warning in 3.74s** | `0` | Automated Python regression (`test_e2e_inference.py` cleanly skipped: live Ollama daemon not running) |
| `.venv/bin/ruff check src/ tests/` | **All checks passed!** | `0` | Automated Python lint |
| `git diff --check` | **Clean (no output)** | `0` | Automated whitespace/conflict check |

### Failed Tests & Root Causes
- **0 failed automated tests.**
- **1 expected skip:** `tests/integration/test_e2e_inference.py::test_real_qwen_inference_and_memory_logging` skipped cleanly via `pytest.skip("Ollama daemon is not running locally.")` (consistent with the restriction against starting production services).

---

## 7. Manual GUI Testing Status

- **Manual Observation (`curl` check):** `http://127.0.0.1:11434/api/version` -> `000` (offline); `http://127.0.0.1:8765/api/v1/health` -> `000` (offline).
- **Status:** Manual interactive Cmd+Q / Dock / Menu macOS GUI launch-and-quit QA was **not** executed because live Ollama/backend production services are not running and starting/stopping production services is disallowed in this task. `AppDelegate` termination wiring is verified by code audit and Swift 6 build, while all 5 shutdown phases and watchdog behavior are verified by the 16 automated tests in `test_shutdown.swift` (including Test 14's live ephemeral Uvicorn server).

---

## 8. Remaining Limitations

1. **No `SIGKILL` Force-Kill Fallback:** By design (and Increment 2D restriction), uncooperative children that ignore `SIGTERM` remain `.owned` and cancel app termination (`.terminateCancel`) rather than risking unsafe PID signalling or leaving orphan processes. Force-kill escalation requires separate review.
2. **Pre-Quit Unresponsive Owned Child Platform Reality:** If an owned child hangs indefinitely ignoring SIGTERM, the user must either close/kill that stubborn process externally or retry quit once it unblocks.
3. **Manual GUI QA Pending:** Interactive Cmd+Q / menu / Dock quit verification against a live macOS app window remains pending separate authorization (or Increment 2E/2F QA).

---

## 9. Git Branch, HEAD & Backup Verification

- **Current Branch:** `backup/carbon-tested-phase1-2026-10-29`
- **Current `HEAD`:** `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`
- **Protected Backup Ref (`backup/carbon-tested-phase1-2026-10-29`):** `0c7d1d17c55c836715438c7d0c00887cf9aaf19c` (intact; zero commits, pushes, or branch switches executed).

---

**Stop Condition:** Increment 2D is complete and verified. Increment 2E has **not** started. Awaiting project-owner review and explicit acceptance.
