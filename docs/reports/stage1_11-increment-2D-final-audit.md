# Carbon Stage 1.11 — Increment 2D Final Acceptance Audit
## Read-Only Code Audit: Graceful Shutdown, Global Watchdog, Retry Safety & AppKit Lifecycle

**Task ID:** `C-1.11-P2-2D-REVIEW`  
**Mode:** READ-ONLY CODE AUDIT  
**Date:** 2026-11-25  
**Target Sources Audited:**
- `apps/macos/Carbon/Sources/ProcessSupervisor.swift`
- `apps/macos/Carbon/Sources/CarbonClient.swift`
- `apps/macos/Carbon/Sources/CarbonApp.swift`
- `apps/macos/Carbon/Sources/ContentView.swift`
- `apps/macos/Carbon/Sources/SingleInstanceLock.swift`

**Overall Audit Verdict:** **CONDITIONAL FAIL (2 Real Concurrency/Cancellation Defects Identified — No Source Modified Pending Authorization)**

The core 5-phase shutdown sequence, AppKit `.terminateLater` contract, external-process preservation, `CarbonClient` inference cancellation, and lock-inode safety pass inspection. However, direct line-by-line inspection uncovered **two genuine concurrency/cancellation defects** around global watchdog expiry and `ManagedProcess` task cancellation that should be resolved before final acceptance.

---

## Section 1 — Global Watchdog and Concurrency (`ProcessSupervisor.shutdown`)

### 1.1 Continuation Mutual Exclusion
- **Status:** **PASS**
- **Exact References:** `apps/macos/Carbon/Sources/ProcessSupervisor.swift:1315-1599` (specifically `lines 1316, 1320-1325, 1387, 1405-1424, 1542-1544, 1571, 1596`).
- **Evidence & Reasoning:**
  `ProcessSupervisor` is annotated `@MainActor` (`line 412`). Both `watchdogTask` (`line 1320`) and `workTask` (`line 1405`) explicitly run on `@MainActor`. Because `@MainActor` serializes execution, both tasks check `!settled` and set `settled = true` synchronously (`lines 1323-1324` and `lines 1407-1408` / `1542-1543`) before any suspension point or `continuation.resume(returning:)`. Thus, `watchdogTask` and `workTask` cannot both resume the same `CheckedContinuation`.

### 1.2 Watchdog Expiry Leaving an Untracked Shutdown Operation Running
- **Status:** **FAIL (Defect #1 — Untracked In-Flight `workTask` After Watchdog Expiry)**
- **Exact References:**
  - `ProcessSupervisor.swift:1284-1288` (`shutdownTask` lifecycle)
  - `ProcessSupervisor.swift:1320-1387` (`watchdogTask` expiry path)
  - `ProcessSupervisor.swift:1464, 1471, 1484, 1490-1497, 1520` (`workTask` suspension & post-`await` mutation)
- **Evidence & Reasoning:**
  When `watchdogTask` wakes up after `budgets.globalWatchdog` (`line 1322`), it sets `settled = true` (`line 1324`), calls `workTask?.cancel()` (`line 1325`), resets `self.isShuttingDown = false` (`line 1360`), and resumes `continuation` immediately (`line 1387`). Consequently, `shutdown()` clears `self.shutdownTask = nil` (`line 1286`) and returns `canTerminate == false` while `workTask` may still be suspended inside `httpClient.send(request)` (`line 1464`), `child.awaitExit` (`line 1471`), or `child.terminateAndAwaitExit` (`lines 1484, 1520`).
  Once `continuation` has resumed, `workTask` is **no longer tracked** by `shutdownTask`. Furthermore, inside Phase 3 (`line 1490-1497`), if `workTask` resumes from `await` after `settled` became `true`, `if !child.isRunning { self._childBackendProcess = nil; self.backendOwnership = .notRunning ... }` executes **without** checking `!settled` or run identity (whereas Phase 4 `line 1511` and Phase 5 `line 1542` do guard on `!settled`).
- **Impact:**
  1. An untracked background `workTask` outlives the 8.0s watchdog return (`shutdown()` already returned `canTerminate = false` and cleared `shutdownTask = nil`).
  2. If the user clicks **Retry Quit** while the previous `workTask` is still finishing an un-cancelled HTTP call or child wait, the old `workTask` and the new retry `workTask` run concurrently, and `line 1490-1497` in the old task could race with retry state.
- **Smallest Safe Correction:**
  1. Track `private var activeShutdownWorkTask: Task<Void, Never>?` on `ProcessSupervisor` (separate from `shutdownTask`), and guard `line 1490` with `if !settled && !Task.isCancelled`.
  2. Capture a per-attempt `let runId = UUID()` / `activeShutdownRunId` and check `guard self.activeShutdownRunId == runId` before mutating ownership or clearing `_childBackendProcess` / `_childOllamaProcess`.
  3. At the start of `performCoordinatedShutdown`, if an earlier `activeShutdownWorkTask` is still running, cancel and wait a tiny bounded grace (or cancel its `httpClient` request) before launching the next attempt.

### 1.3 Cancellation Propagation & Busy-Spin Defect
- **Status:** **FAIL (Defect #2 — `ManagedProcess` Polling Loops Ignore `Task.isCancelled`)**
- **Exact References:**
  - `ProcessSupervisor.swift:269-287` (`ManagedProcess.awaitExit`)
  - `ProcessSupervisor.swift:290-312` (`ManagedProcess.terminateAndAwaitExit`)
  - `ProcessSupervisor.swift:1428, 1436, 1444, 1466, 1482, 1511, 1542` (`performCoordinatedShutdown`)
- **Evidence & Reasoning:**
  While `performCoordinatedShutdown` checks `!settled && !Task.isCancelled` at every phase boundary, both `ManagedProcess.awaitExit` (`lines 276-278`) and `ManagedProcess.terminateAndAwaitExit` (`lines 300-302`) implement:
  ```swift
  while isRunning && Date() < deadline {
      try? await Task.sleep(nanoseconds: 10_000_000) // 10ms
  }
  ```
  Because `try?` swallows `CancellationError` without breaking when `Task.isCancelled` becomes `true`, calling `workTask?.cancel()` (`line 1325`) causes `Task.sleep` to return immediately with zero suspension on every loop iteration, producing a **100% CPU busy-spin** until `Date() >= deadline` (up to `2.0s–3.0s`) and keeping `workTask` alive after watchdog cancellation.
- **Impact:** Watchdog cancellation triggers a tight busy-wait loop in `ManagedProcess` instead of yielding immediately, preventing prompt `workTask` exit and spiking CPU until the phase deadline passes.
- **Smallest Safe Correction:**
  Change lines `276` and `300` in `ProcessSupervisor.swift` from `while isRunning && Date() < deadline` to:
  ```swift
  while isRunning && Date() < deadline && !Task.isCancelled
  ```
  (This still preserves all Increment 2B semantics: if `!isRunning` is false, it skips `cleanupPipes()` and returns `.timedOut` / non-exited without falsely reporting `.exited`).

### 1.4 No Phase Continues Initiating Process Termination After Watchdog Failure
- **Status:** **PASS**
- **Exact References:** `ProcessSupervisor.swift:1324-1325, 1428, 1436, 1444, 1466, 1482, 1511, 1542`.
- **Evidence & Reasoning:** Setting `settled = true` and cancelling `workTask` prevents entry into Phase 1 (`line 1428`), Phase 2 (`line 1436`), Phase 3 (`line 1444`), Phase 3 fallback (`line 1482`), Phase 4 (`line 1511`), and Phase 5 (`line 1542`). An already-entered call unwinds without triggering subsequent phases (and fixing Defect #2 stops any `awaitExit`/`terminateAndAwaitExit` wait immediately).

### 1.5 Single-Instance Lock Never Released Prematurely
- **Status:** **PASS**
- **Exact References:** `ProcessSupervisor.swift:1354-1374` (`watchdogTask`) and `lines 1547-1596` (`workTask` Phase 5).
- **Evidence & Reasoning:** `lockRelease?()` is called **exclusively** when `!stillOwnedBackend && !stillOwnedOllama` (`lines 1371` and `1555`). Whenever either `.owned` child remains running (`backendOwnership == .owned || ollamaOwnership == .owned`), `lockRelease` is skipped, `canTerminate` is `false`, `hasCompletedShutdown` remains `false`, and the `SingleInstanceLock` descriptor stays held.

---

## Section 2 — Retry Quit Safety

### 2.1 Retry Cannot Overlap an Earlier Shutdown Task
- **Status:** **PASS (normal phase timeout) / FAIL (immediate Retry after Watchdog Expiry if `workTask` still unwinding)**
- **Exact References:** `ProcessSupervisor.swift:1248-1254, 1284-1288`, `CarbonApp.swift:52-55`, `ContentView.swift:198-200`.
- **Evidence & Reasoning:**
  During normal shutdown or phase timeout, `await task.value` completes before `shutdownTask = nil` and `terminationInFlight = false`. Concurrent quit attempts hit `CarbonApp.swift:53` (`terminationInFlight || supervisor.isShuttingDown`) and `ProcessSupervisor.swift:1252` (`if let existingShutdown = shutdownTask { return await existingShutdown.value }`), so overlap is impossible.
  However, if the global watchdog fires (`Defect #1`), `shutdownTask` becomes `nil` while the old `workTask` may still be completing an un-cancelled HTTP call or spin loop, allowing an immediate **Retry Quit** click (`ContentView.swift:199`) to start a second `workTask` before the first exits.

### 2.2 Retained `.owned` Process Handles Reused Safely
- **Status:** **PASS**
- **Exact References:** `ProcessSupervisor.swift:1445, 1512` (and startup Step 0 recovery at `lines 516, 784`).
- **Evidence & Reasoning:** On Retry Quit, Phase 3 (`line 1445`) and Phase 4 (`line 1512`) inspect `backendOwnership == .owned` / `ollamaOwnership == .owned` and read `_childBackendProcess` / `_childOllamaProcess` directly.

### 2.3 No Duplicate Process Termination Requests Create Races
- **Status:** **PASS (normal case; conditional on Defect #1 if watchdog fired during active wait)**
- **Exact References:** `ProcessSupervisor.swift:1252-1254, 1482-1488, 1518-1533`.
- **Evidence & Reasoning:** `shutdownTask` deduplicates concurrent calls cleanly; sequential retry after `shutdownState == .failed` issues one clean pass per retry once the prior task has finished (eliminating Defect #1 makes this unconditional even under watchdog expiry).

### 2.4 Previously Timed-Out Process Cannot Be Forgotten or Incorrectly Marked Stopped
- **Status:** **PASS**
- **Exact References:** `ProcessSupervisor.swift:1330-1347, 1490-1501, 1521-1533`.
- **Evidence & Reasoning:** Ownership clears (`_child*Process = nil; *Ownership = .notRunning`) ONLY when `!child.isRunning` evaluates `true` (`lines 1331, 1340, 1490, 1523`). If `child.isRunning` is still `true`, `backendOutcome` / `ollamaOutcome` is set to `.timedOut`, `.owned` is preserved, and the process handle remains retained.

### 2.5 Retry After a Child Exits Successfully Completes Cleanup
- **Status:** **PASS**
- **Exact References:** `ProcessSupervisor.swift:1446-1449, 1490-1497, 1513-1516, 1547-1571`, `CarbonApp.swift:47-50, 72-79`.
- **Evidence & Reasoning:** If the stubborn owned child exited between failure and Retry Quit, `!child.isRunning` clears ownership at `line 1446` / `1513`; otherwise Phase 3 / Phase 4 re-runs termination and confirms exit. Once `!stillOwnedBackend && !stillOwnedOllama`, Phase 5 runs `lockRelease?()`, sets `hasCompletedShutdown = true`, returns `canTerminate = true`, and `AppDelegate` replies `sender.reply(toApplicationShouldTerminate: true)`.

---

## Section 3 — Inference Cancellation (`CarbonClient.cancelActiveInferenceAndWait`)

| Check | Status | Exact Source Lines | Evidence & Reasoning |
|---|---|---|---|
| **3.1 Existing cancellation protocol used** | **PASS** | `CarbonClient.swift:635`, `597-610` | Calls `cancelCurrentTask()`, dispatching `{"action": "cancel", "task_id": tid}` over `webSocketTask`. No custom protocol added. |
| **3.2 1.2-second deadline enforced** | **PASS** | `ProcessSupervisor.swift:52, 1430`; `CarbonClient.swift:637-641` | `ShutdownTimeoutConfig.standard.inferenceCancel == 1.2`; `ProcessSupervisor` passes `min(1.2, remainingGlobal())`. Polling checks `Date() < deadline`. |
| **3.3 Missing ack cannot block shutdown** | **PASS** | `CarbonClient.swift:638-642, 647-650` | Loop terminates at `deadline`, `!isConnected`, or `Task.isCancelled`; clears `pendingCancelAckTaskId = nil`, cancels `submissionTimeoutTask`, resets `isGenerating = false` and `currentTaskId = nil`. |
| **3.4 Disconnected WebSocket handled safely** | **PASS** | `CarbonClient.swift:633, 643-645` | Checks `if let tid = currentTaskId, isConnected`; if offline, enters `else` branch and clears local state immediately without waiting 1.2s. |
| **3.5 Idle inference skips cancellation** | **PASS** | `CarbonClient.swift:629-631` | `guard isGenerating || currentTaskId != nil else { return }` exits immediately when idle. |
| **3.6 Submissions & reconnects blocked during shutdown** | **PASS** | `CarbonClient.swift:198, 204, 209, 216, 327, 352, 358, 519` | `sendMessage`, `connectWebSocket`, `scheduleReconnect`, `startMonitoring`, and `startPeriodicHealthCheck` all guard against `isShuttingDown`. |
| **3.7 Cancel acknowledgement state cleared cleanly** | **PASS** | `CarbonClient.swift:448, 460, 472, 642, 664` | Cleared on `"completed"`, `"cancelled"`, `"failed"`, loop exit, and `closeWebSocketForShutdown`. *(Minor note: `case "error"` at `line 493` relies on the bounded 1.2s deadline clear at `line 642`.)* |

- **Remaining Observation (`CarbonClient.swift:689-691`):** On a cancelled quit, `resetShutdownStateAfterCancelledQuit()` sets `isShuttingDown = false` so the user can interact or retry quit, while `autoReconnectEnabled` remains `false` (leaving WebSocket disconnected rather than silently reconnecting until startup/user reconnects).

---

## Section 4 — AppKit Termination Lifecycle (`CarbonApp.swift`)

| Check | Status | Exact Source Lines | Evidence & Reasoning |
|---|---|---|---|
| **4.1 `.terminateLater` returned correctly** | **PASS** | `CarbonApp.swift:34-38, 41-82` (`line 81`) | Starts `@MainActor` async teardown `Task` and returns `.terminateLater`. |
| **4.2 Exactly one reply per attempt / Duplicate quits safe** | **PASS** | `CarbonApp.swift:42-81` | `terminationInFlight || supervisor.isShuttingDown` (`line 53`) prevents launching a second teardown task; `Task` invokes `sender.reply(toApplicationShouldTerminate: ...)` once (`lines 62, 68, 78`). |
| **4.3 Watchdog & failure cancel termination** | **PASS** | `CarbonApp.swift:78` | Passes `report.canTerminate` (`false` when owned process timed out or watchdog expired), signalling `.terminateCancel` to AppKit. |
| **4.4 Retry Quit initiates valid new attempt** | **PASS** | `ContentView.swift:198-200`; `CarbonApp.swift:47-58, 72-78` | `"Retry Quit"` triggers `NSApp.terminate(nil)`; since `hasCompletedShutdown == false` and `terminationInFlight == false`, `AppDelegate` begins a fresh `.terminateLater` cycle. |
| **4.5 Safe `MainActor` assumptions** | **PASS** | `CarbonApp.swift:34-38, 321-325` | `MainActor.assumeIsolated` matches AppKit's guarantee that `NSApplicationDelegate` methods execute on the main thread. `appDelegate.configure(client:supervisor:)` runs synchronously before `supervisor.startup` inside `.task`. *(Minor theoretical note: if AppKit receives Quit before SwiftUI's first `.onAppear`/`.task`, `supervisor` is `nil` so `line 42` returns `.terminateNow`, which is safe because `startup` hasn't launched any processes yet.)* |

---

## Section 5 — Ownership and Lock Safety

| Check | Status | Exact Source Lines | Evidence & Reasoning |
|---|---|---|---|
| **5.1 `.external` services untouched** | **PASS** | `ProcessSupervisor.swift:1503-1506, 1535-1538` | Both Phase 3 and Phase 4 branch strictly on `== .external` -> `.preservedExternal` with zero HTTP requests and zero process calls. |
| **5.2 Only retained Carbon-owned handles terminated** | **PASS** | `ProcessSupervisor.swift:1445, 1484, 1512, 1520` | Verified across `apps/macos/Carbon/Sources/`: **0 occurrences** of `SIGKILL` or raw `kill(pid, ...)`. All termination uses `Foundation.Process.terminate()`. |
| **5.3 Ownership cleared only after confirmed exit** | **PASS** | `ProcessSupervisor.swift:1331-1346, 1490-1501, 1522-1533` | Cleared only when `!child.isRunning`. *(Note: see Defect #1 recommendation to guard `line 1490` with `!settled && !Task.isCancelled`.)* |
| **5.4 Lock released only after all owned children confirmed stopped** | **PASS** | `ProcessSupervisor.swift:1354-1374, 1547-1596` | `lockRelease?()` runs only when `!stillOwnedBackend && !stillOwnedOllama`. |
| **5.5 Lock file inode intact** | **PASS** | `SingleInstanceLock.swift:98-105` | Uses `flock(fd, LOCK_UN)` and `close(fd)`, guarding with `lockFileDescriptor` check so `close()` runs at most once without `unlink`/`removeItem`. |
| **5.6 Zero spawns or reconnects during shutdown** | **PASS** | `ProcessSupervisor.swift:678, 943, 1044`; `CarbonClient.swift:216, 327, 352, 358` | Both `startup()` and `launchProcess` check `!isShuttingDown && !hasCompletedShutdown`; `CarbonClient` blocks `connectWebSocket` and `scheduleReconnect`. |

---

## Summary of Actionable Defects Requiring Authorization Before Merge

| ID | Severity | Location | Issue Summary | Smallest Safe Correction |
|---|---|---|---|---|
| **D1** | Moderate | `ProcessSupervisor.swift:276, 300` | `ManagedProcess.awaitExit` and `terminateAndAwaitExit` swallow `CancellationError` (`try?`) without `!Task.isCancelled` in the `while` condition, causing a CPU spin until `deadline` if cancelled by watchdog or `cancelStartup()`. | Add `&& !Task.isCancelled` to both `while isRunning && Date() < deadline` conditions (`lines 276, 300`). |
| **D2** | Moderate | `ProcessSupervisor.swift:1284-1387, 1490` | After global watchdog expiry resumes `continuation`, `shutdownTask` becomes `nil` while an un-cancelled `workTask` (e.g. awaiting slow HTTP or process wait) can remain live untracked in the background, and `line 1490` mutates `_childBackendProcess`/`backendOwnership` without checking `!settled` or a run ID. | Retain `activeShutdownWorkTask` + `activeShutdownRunId`, guard `line 1490` with `!settled && !Task.isCancelled && self.activeShutdownRunId == runId`, and cancel/drain any prior `activeShutdownWorkTask` before a Retry Quit starts. |

---

## Restrictions & Git State Confirmation

- **No source files edited**, no tests added/executed, no dependencies added, no production services started/stopped.
- **Branch:** `backup/carbon-tested-phase1-2026-10-29`
- **HEAD & Protected Backup:** `0c7d1d17c55c836715438c7d0c00887cf9aaf19c` (intact; zero commits, pushes, or branch switches).

**Stop Condition:** Read-only final acceptance audit complete. Awaiting project-owner authorization before applying the two small concurrency/cancellation fixes or advancing to Increment 2E.
