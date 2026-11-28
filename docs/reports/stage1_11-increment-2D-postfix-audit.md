# Carbon Stage 1.11 — Increment 2D Final Post-Fix Audit

**Task ID:** `C-1.11-P2-2D-POSTFIX-REVIEW`  
**Audit Mode:** Strictly READ-ONLY source & test inspection (no source edits, no test edits, no test execution)  
**Scope:** Independent verification of `C-1.11-P2-2D-FIX` corrections for Defects **D1** (`ManagedProcess` cancellation busy-spin) and **D2** (untracked shutdown work and overlapping retries after global watchdog expiry), plus watchdog/AppKit termination lifecycle and regression test quality.

---

## Executive Verdict

**FINAL VERDICT:** **`ACCEPTABLE FOR PROJECT-OWNER ACCEPTANCE`**

All inspected code fixes for **D1** and **D2** in [ProcessSupervisor.swift](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift), [CarbonApp.swift](../../apps/macos/Carbon/Sources/CarbonApp.swift), [SingleInstanceLock.swift](../../apps/macos/Carbon/Sources/SingleInstanceLock.swift), and [ContentView.swift](../../apps/macos/Carbon/Sources/ContentView.swift), along with the newly added tests in [test_process_supervisor.swift](../../apps/macos/Carbon/Tests/test_process_supervisor.swift) and [test_shutdown.swift](../../apps/macos/Carbon/Tests/test_shutdown.swift), satisfy the safety invariants required by Increment 2D. Runtime execution of tests was excluded by the read-only constraint of this audit (`NOT VERIFIED` here).

---

## Audit Summary Matrix

| Section | Check | Status | Primary Evidence |
| :--- | :--- | :--- | :--- |
| **1. D1 — Cancellation** | Polling loops check `Task.isCancelled` | **PASS** | `ProcessSupervisor.swift:276, 300` |
| **1. D1 — Cancellation** | Cancellation exits promptly without busy-spin | **PASS** | `ProcessSupervisor.swift:276-278, 300-302` |
| **1. D1 — Cancellation** | Still-running child remains `.owned` | **PASS** | `ProcessSupervisor.swift:280-286, 304-311, 1542-1555, 1577-1591, 1695-1706` |
| **1. D1 — Cancellation** | Pipe handlers remain open while child is alive | **PASS** | `ProcessSupervisor.swift:280-286, 304-311, 314-326` |
| **1. D1 — Cancellation** | Existing timeout & exit-result semantics preserved | **PASS** | `ProcessSupervisor.swift:215-218, 270-311` |
| **2. D2 — Shutdown Work Tracking** | `activeShutdownWorkTask` & `activeShutdownRunId` lifecycle | **PASS** | `ProcessSupervisor.swift:455-458, 1287-1288, 1435-1441, 1657` |
| **2. D2 — Shutdown Work Tracking** | Cancelled/stale `workTask` cannot mutate state or act after watchdog | **PASS** | `ProcessSupervisor.swift:1352-1354, 1464-1655` |
| **2. D2 — Shutdown Work Tracking** | Overlapping Retry Quit blocked while prior work unwinds | **PASS** | `ProcessSupervisor.swift:1261-1279` |
| **2. D2 — Shutdown Work Tracking** | `defer` clears tracking cleanly; subsequent Retry Quit works | **PASS** | `ProcessSupervisor.swift:1437-1441, 1261-1317` |
| **3. Watchdog & AppKit Lifecycle** | Continuation resumed strictly once (`settled`) | **PASS** | `ProcessSupervisor.swift:1344-1461, 1600-1655` |
| **3. Watchdog & AppKit Lifecycle** | Watchdog timeout behavior (`canTerminate = false` for owned children; external-only caveat noted) | **PASS** *(see §3 caveat)* | `ProcessSupervisor.swift:1384-1417` |
| **3. Watchdog & AppKit Lifecycle** | `AppDelegate` termination coordination & Retry Quit safety | **PASS** | `CarbonApp.swift:34-87`, `ContentView.swift:192-200` |
| **3. Watchdog & AppKit Lifecycle** | `SingleInstanceLock` held on failed/blocked quit, idempotent release on clean quit | **PASS** | `SingleInstanceLock.swift:98-105`, `ProcessSupervisor.swift:1384-1404, 1609-1655` |
| **3. Watchdog & AppKit Lifecycle** | External process isolation & no startup spawn during/after shutdown | **PASS** | `ProcessSupervisor.swift:684-686, 950-952, 1050-1052, 1326-1331, 1557-1595` |
| **4. Test Quality** | Tests 34–35 & 17–18 genuinely exercise D1/D2 without cheating | **PASS** | `test_process_supervisor.swift:1329-1441`, `test_shutdown.swift:1044-1223` |
| **4. Test Quality** | Earlier regression suite coverage (Tests 1–31 / 1–13) | **NOT VERIFIED** | Excluded from line-by-line post-fix read scope |
| **4. Test Quality** | Runtime test compilation & execution | **NOT VERIFIED** | Not run (strictly read-only audit) |

---

## 1. D1 — `ManagedProcess` Cancellation Behaviour

Inspected target: [`ManagedProcess`](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L231-L327).

1. **Both polling loops check `Task.isCancelled` — PASS**
   - [`awaitExit(timeout:)`](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L269-L287) uses `while isRunning && Date() < deadline && !Task.isCancelled` (`line 276`).
   - [`terminateAndAwaitExit(timeout:)`](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L290-L312) uses `while isRunning && Date() < deadline && !Task.isCancelled` (`line 300`).

2. **Cancellation exits promptly without busy-spinning — PASS**
   - Inside both loops (`lines 277` and `301`), `try? await Task.sleep(nanoseconds: 10_000_000)` throws `CancellationError` immediately upon task cancellation. With `!Task.isCancelled` in the `while` guard, control drops out of the loop immediately on the wake from `Task.sleep` rather than spinning `Task.sleep` repeatedly until `deadline` or child exit.

3. **Still-running child remains owned — PASS**
   - When cancellation exits the loop while `isRunning == true`, both `awaitExit` (`lines 280-286`) and `terminateAndAwaitExit` (`lines 304-311`) take the `else` branch and return `.timedOut` without altering any supervisor ownership state.
   - All callers guard ownership clearance:
     - `terminateOwnedChildrenOnly(awaitExitTimeout:)` (`lines 1695-1706`) clears `_childOllamaProcess` / `_childBackendProcess` and sets `.notRunning` only inside `if case .exited = res`.
     - `performCoordinatedShutdown(...)` (`lines 1542-1555`, `1577-1591`) checks both `isRunStillActive()` and `!child.isRunning`, retaining the child handle and `.owned` whenever the child still runs or the run was cancelled/settled.
     - Startup cleanup/timeout paths (`lines 630-633`, `894-897`, `906-909`, `914-917`, `1004-1007`, `1017-1020`, `1028-1031`) likewise clear ownership only on `.exited`.

4. **Pipe handlers are not closed while child is alive — PASS**
   - [`cleanupPipes()`](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L314-L322) (which clears `readabilityHandler` and closes pipe descriptors via `PipeDrainer.close()` at `lines 202-210`) is invoked strictly in the `if !isRunning` branch (`lines 271, 281, 292, 306`, plus `terminate()` fast-path check at `line 264` and `terminationHandler` at `line 255`).
   - Returning `.timedOut` on cancellation while `isRunning == true` bypasses `cleanupPipes()`, leaving stdout/stderr drainers attached.

5. **Existing timeout and exit-result semantics preserved — PASS**
   - [`ProcessTerminationResult`](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L215-L218) remains unchanged (`.exited(terminationStatus: Int32)` and `.timedOut`).
   - Natural exit before cancellation/deadline — even if coinciding with task cancellation — reaches `if !isRunning` cleanly (`lines 280, 304`), closes pipes, and returns `.exited(terminationStatus: process.terminationStatus)`. Running past deadline or aborting wait via cancellation while alive returns `.timedOut`.

> [!NOTE]
> Minor observation (non-blocking): In `terminateAndAwaitExit(timeout:)` (`line 297`), `process.terminate()` is invoked *before* entering `while ... && !Task.isCancelled`. Coordinated shutdown always checks `isRunStillActive()` (`lines 1532, 1565`) before entering `terminateAndAwaitExit`, so a cancelled shutdown task will never trigger an unwanted `SIGTERM`; only a direct caller on an already-cancelled `Task` would still send `SIGTERM` before returning.

---

## 2. D2 — Active Shutdown Work Tracking & Overlap Protection

Inspected targets: [`ProcessSupervisor`](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L454-L458) (`lines 1246-1659`).

1. **Lifecycle of `activeShutdownWorkTask`, `activeShutdownRunId`, and `shutdownWorkLaunchCount` — PASS**
   - `shutdown(...)` assigns `let runId = UUID()` and `self.activeShutdownRunId = runId` (`lines 1287-1288`) before launching `shutdownTask` (`line 1290`).
   - Inside `performCoordinatedShutdown(...)`, after cancelling/awaiting any in-flight `startupTask` (`lines 1326-1331`), `self.shutdownWorkLaunchCount += 1` (`line 1435`), `workTask` is created on `@MainActor` (`line 1436`), and `self.activeShutdownWorkTask = workTask` (`line 1657`) is stored synchronously on `@MainActor` inside `withCheckedContinuation` before suspending.
   - `workTask` clears tracking strictly inside its `defer` block (`lines 1437-1441`):
     ```swift
     defer {
         if let self = self, self.activeShutdownRunId == runId {
             self.activeShutdownWorkTask = nil
         }
     }
     ```
   - Watchdog expiry (`lines 1349-1433`) intentionally leaves `activeShutdownWorkTask` and `activeShutdownRunId` untouched after calling `workTask?.cancel()` (`line 1354`), keeping `hasActiveShutdownWorkTask == true` until `workTask` truly returns on `@MainActor`.

2. **Cancelled / post-watchdog `workTask` cannot mutate state or trigger subsequent phases — PASS**
   - [`isRunStillActive()`](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L1464-L1467) checks `guard !settled && !Task.isCancelled else { return false }; return self.activeShutdownRunId == runId`.
   - When `watchdogTask` expires (`lines 1352-1354`), `@MainActor` atomically executes `settled = true` and `workTask?.cancel()` before resuming `continuation`, permanently flipping `isRunStillActive()` to `false` for that run.
   - Verified every phase boundary and mutation site in `workTask`:
     - Phase 1 (`lines 1470, 1474`): gates entry and `inferenceCancelled = true`.
     - Phase 2 (`lines 1480, 1484`): gates entry and `webSocketClosed = true`.
     - Phase 3 (`lines 1490, 1493, 1513, 1521, 1532, 1535, 1542`): gates entry, post-HTTP flags/`awaitExit`, `child.terminateAndAwaitExit(timeout:)` fallback (so a stalled HTTP call unwinding after watchdog cannot subsequently send `SIGTERM`), and `_childBackendProcess` / `backendOwnership` mutations (`line 1542` also combines `(self._childBackendProcess === child || (self._childBackendProcess == nil && !child.isRunning))` to tolerate async `terminationHandler` clearing at `lines 974-982`).
     - Phase 4 (`lines 1565, 1568, 1577`): gates entry and `_childOllamaProcess` / `ollamaOwnership` mutations (`line 1577` similarly handles `terminationHandler` identity at `lines 705-713`).
     - Phase 5 (`line 1600`): `guard isRunStillActive() else { return }` prevents `settled = true`, `watchdogTask?.cancel()`, `hasCompletedShutdown = true`, `lockRelease?()`, `shutdownState` changes, or double continuation resumption.

3. **Overlapping Retry Quit while prior work unwinds is blocked safely — PASS**
   - [`shutdown(...)`](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L1261-L1279) checks `if activeShutdownWorkTask != nil` right after `hasCompletedShutdown` and `shutdownTask` deduplication.
   - When triggered while an old `workTask` is still finishing, it:
     - Launches no new `workTask` and leaves `shutdownWorkLaunchCount` and `activeShutdownRunId` untouched.
     - Updates UI-visible failure feedback (`shutdownState = .failed`, `shutdownStatusMessage = "Previous shutdown still finishing"`, `shutdownErrorMessage = msg`).
     - Leaves `backendOwnership` and `ollamaOwnership` untouched, does **not** invoke `lockRelease?()`, and returns `blockedReport` with `canTerminate: false`, `lockReleased: false`, `timedOutByWatchdog: true`.

4. **Subsequent Retry Quit succeeds cleanly after prior work finishes — PASS**
   - Once the cancelled `workTask` finishes unwinding and executes its `defer` (`lines 1437-1441`), `activeShutdownWorkTask` becomes `nil`.
   - The next `shutdown(...)` call passes `line 1261`, generates a fresh `runId`, sets `isShuttingDown = true`, starts a fresh `workTask`, and proceeds cleanly through Phases 1–5.

---

## 3. Watchdog, AppKit Termination, and Ownership Lifecycle

Inspected targets: [ProcessSupervisor.swift](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L1319-L1659), [CarbonApp.swift](../../apps/macos/Carbon/Sources/CarbonApp.swift#L4-L88), [SingleInstanceLock.swift](../../apps/macos/Carbon/Sources/SingleInstanceLock.swift#L46-L156), [ContentView.swift](../../apps/macos/Carbon/Sources/ContentView.swift#L172-L203).

1. **Single continuation resumption guarantee — PASS**
   - Both `watchdogTask` and `workTask` are `@MainActor`-isolated, sharing `settled: Bool` (`line 1345`). Every resumption path checks `!settled` on `@MainActor`, sets `settled = true` first, and either cancels `watchdogTask` (`lines 1446, 1602`) or cancels `workTask` (`line 1354`).
   - Because both tasks are top-level unstructured `@MainActor` `Task`s, caller cancellation cannot silently drop `watchdogTask` before `workTask` completes (or vice versa); `continuation` is resumed strictly once.

2. **Watchdog expiry semantics — PASS *(with minor external-only caveat)***
   - **When any owned child remains running (`stillOwnedBackend || stillOwnedOllama`, `lines 1384-1395`):** `watchdogTask` sets `canQuit = false`, `isShuttingDown = false`, `hasCompletedShutdown = false`, `shutdownState = .failed`, resets client state (`resetShutdownStateAfterCancelledQuit()`), does **not** release the lock (`lockReleased: false`), and keeps `_child*Process` / `.owned` intact.
   - **Caveat when zero owned children exist (`!stillOwnedBackend && !stillOwnedOllama`, `lines 1396-1404`):** `watchdogTask` sets `canQuit = true`, `hasCompletedShutdown = true`, and invokes `lockRelease?()` (explicitly asserted by [test_shutdown.swift:1034-1035](../../apps/macos/Carbon/Tests/test_shutdown.swift#L1034-L1035) when only external services exist and Phase 1 exceeds the watchdog). Because `activeShutdownWorkTask` may still be unwinding a cancellation-insensitive Phase 1 wait at that moment, AppKit proceeds with termination and releases the lock slightly before `workTask` returns, though zero Carbon-owned child processes exist to abandon. *Smallest optional change if strict "all watchdog timeouts cancel Quit" semantics were ever desired for external-only Phase 1 stalls: remove the `else` branch at `line 1396` and align Test 16 accordingly (not requested here).*

3. **AppKit `AppDelegate` & UI Retry Quit integration — PASS**
   - [`applicationShouldTerminate(_:)`](../../apps/macos/Carbon/Sources/CarbonApp.swift#L34-L38) delegates on `@MainActor` to `handleApplicationShouldTerminate(_:)` (`lines 41-82`).
   - If `terminationInFlight || supervisor.isShuttingDown` (`line 53`), duplicate quit triggers return `.terminateLater` without starting concurrent shutdown work.
   - Upon completion (`lines 77-78`), `self.terminationInFlight = false` executes immediately before `sender.reply(toApplicationShouldTerminate: report.canTerminate)`, ensuring both `"Retry Quit"` (`ContentView.swift:198-200`, calling `NSApp.terminate(nil)`) and `Cmd+Q` remain unblocked after `canTerminate == false`.
   - `hasCompletedShutdown` short-circuits `shutdown(...)` (`ProcessSupervisor.swift:1252-1254`) and `handleApplicationShouldTerminate` (`CarbonApp.swift:47-50`) cleanly to `.terminateNow`.

4. **Single-instance lock & process isolation — PASS**
   - [`SingleInstanceLock.release()`](../../apps/macos/Carbon/Sources/SingleInstanceLock.swift#L98-L105) is thread-safe and idempotent (`flock(fd, LOCK_UN)` + `close(fd)` + `lockFileDescriptor = nil`).
   - Failed/blocked quits never invoke `lockRelease?()` and reply `false` to AppKit so `applicationWillTerminate` (`CarbonApp.swift:84-87`) is not reached.
   - External dependencies (`.external`) are never signaled or terminated (`ProcessSupervisor.swift:1557-1558, 1593-1594, 1668, 1676, 1692, 1700`).
   - Startup paths guard `!isShuttingDown && !hasCompletedShutdown` at entry (`line 1050`) and prior to every process spawn (`lines 684, 950`), while shutdown cancels and awaits `startupTask` (`lines 1326-1331`) before running phases.

---

## 4. Test Quality Assessment

Inspected targets: [test_process_supervisor.swift](../../apps/macos/Carbon/Tests/test_process_supervisor.swift#L1266-L1444) (Tests 32–35) and [test_shutdown.swift](../../apps/macos/Carbon/Tests/test_shutdown.swift#L950-L1226) (Tests 14–18).

1. **Test 34 ([test_process_supervisor.swift:1329-1364](../../apps/macos/Carbon/Tests/test_process_supervisor.swift#L1329-L1364)) — PASS**
   - Spawns a real `python3` child (`time.sleep(2.0)`) via `DefaultProcessLauncher` -> real `ManagedProcess` + `PipeDrainer`. Cancels `awaitExit(timeout: 5.0)` after `50ms`, asserting `elapsed < 0.5`, `.timedOut`, `proc.isRunning`, and `stdoutDrainer?.hasHandlerAttached == true`. Without D1, this test reliably fails (~`2.0s` >= `0.5s`).

2. **Test 35 ([test_process_supervisor.swift:1366-1441](../../apps/macos/Carbon/Tests/test_process_supervisor.swift#L1366-L1441)) — PASS *(minor timing-margin note)***
   - Spawns a real `SIGTERM`-ignoring `python3` child (`time.sleep(0.8)`) and verifies `terminateAndAwaitExit(timeout: 5.0)` both directly (Part A, `lines 1385-1399`) and via `supervisor.terminateOwnedChildrenAndAwaitExit(timeout: 5.0)` (Part B, `lines 1422-1434`), asserting `< 0.5s` completion, open pipes, `.isRunning`, and preserved `.owned` + handle identity.
   - *Minor limitation:* Part B reuses the same `0.8s` child launched before Part A; under severe CPU starvation (`> ~0.3s` spent across Part A + Part B wait), natural child exit could trigger `assert(realUncooperative.isRunning)` (`line 1432`) spuriously rather than causing a false pass. Using a separate `2.0s` child per sub-part (matching Test 34) would eliminate host scheduling sensitivity.

3. **Test 17 ([test_shutdown.swift:1044-1153](../../apps/macos/Carbon/Tests/test_shutdown.swift#L1044-L1153)) — PASS *(structural note on `runId`)***
   - Uses a genuinely cancellation-insensitive HTTP `send` closure (`withCheckedContinuation` + `DispatchQueue.global().asyncAfter` for `0.55s > 0.15s` watchdog) and a real `SingleInstanceLock`.
   - Proves: (a) `hasActiveShutdownWorkTask == true` and `lock.isHeld == true` after watchdog expiry; (b) overlapping Retry Quit is rejected with `canTerminate == false` and `shutdownWorkLaunchCount == 1`; (c) after the suspended HTTP call finishes, `hasActiveShutdownWorkTask` clears while `backendProc.terminateCallCount == 0`, `backendOwnership == .owned`, and `lock.isHeld == true`; and (d) subsequent clean Retry Quit executes (`shutdownWorkLaunchCount == 2`), terminates backend, and releases lock.
   - *Note:* The `activeShutdownRunId == runId` check inside `isRunStillActive()` and `defer` acts as defense-in-depth behind the primary `activeShutdownWorkTask != nil` guard and is verified structurally rather than by forcing two simultaneous `runId` instances.

4. **Test 18 ([test_shutdown.swift:1156-1223](../../apps/macos/Carbon/Tests/test_shutdown.swift#L1156-L1223)) — PASS**
   - Combines a real `SIGTERM`-ignoring `ManagedProcess` (`0.8s` sleep) as owned Ollama with an external backend, `ollamaShutdown: 5.0`, and `globalWatchdog: 0.15`.
   - Verifies `elapsed < 0.5`, `canTerminate == false`, `.owned` Ollama / `.external` backend preservation, and `hasActiveShutdownWorkTask == false` `50ms` after shutdown returns (well before the `0.8s` script naturally ends, confirming D1+D2 exits the polling wait promptly without spinning `5.0s`).

5. **Runtime execution & earlier test ranges — NOT VERIFIED**
   - Live `swift build` / test suite execution was not performed in this strictly read-only audit (`NOT VERIFIED`).
   - Tests 1–31 of `test_process_supervisor.swift` and Tests 1–13 of `test_shutdown.swift` were not inspected in full in this focused D1/D2 pass (`NOT VERIFIED`).

---

## Remaining Limitations & Non-Blocking Observations

1. **Live runtime verification pending owner/verified test run:** Compilation and live pass/fail of the 35 supervisor tests and 18 shutdown tests are not independently executed by this read-only audit (`NOT VERIFIED`).
2. **External-only watchdog path (`ProcessSupervisor.swift:1396-1404`):** Returns `canTerminate = true` and releases `SingleInstanceLock` when zero owned children exist even if `activeShutdownWorkTask` is still unwinding Phase 1 (documented architectural choice exercised by Test 16).
3. **Host scheduling margin in Test 35 Part B & Test 18:** Reusing a `0.8s` child process leaves a comfortable ~`0.55-0.60s` margin on normal hardware, though extreme CI throttling could cause early natural child exit before Part B asserts `isRunning`.
4. **Already-cancelled direct `terminateAndAwaitExit` calls (`ProcessSupervisor.swift:297`):** Safe under `performCoordinatedShutdown` (gated by `isRunStillActive()`), though direct callers in an already-cancelled `Task` still invoke `process.terminate()` once before exiting the loop.

---

## Final Determination

No blocking defects remain in the **D1** or **D2** corrections.  
**Verdict:** **`ACCEPTABLE FOR PROJECT-OWNER ACCEPTANCE`** (awaiting project-owner acceptance / independent test-run evidence if not already archived).
