# Carbon Stage 1.11 — Increment 2D Defect Corrections Report
## Resolution of D1 (Cancellation Busy-Spin) and D2 (Untracked Watchdog Work)

**Task ID:** `C-1.11-P2-2D-FIX`  
**Mode:** Controlled defect correction & verification  
**Date:** 2026-11-25  
**Status:** COMPLETE & VERIFIED (Submitted for Independent Review — Do Not Claim Final Acceptance)

---

## 1. Exact Files Changed

| File | Action | Scope |
|---|---|---|
| `apps/macos/Carbon/Sources/ProcessSupervisor.swift` | Modified | Resolved D1 (`awaitExit` & `terminateAndAwaitExit` `!Task.isCancelled` checks) and D2 (`activeShutdownWorkTask`, `activeShutdownRunId`, `isRunStillActive()` guard on all backend/Ollama ownership mutations, overlap prevention on Retry Quit, and safe `terminationHandler` nil-clearance compatibility). |
| `apps/macos/Carbon/Tests/test_process_supervisor.swift` | Modified | Added Tests 34–35 covering D1 (`awaitExit` cancellation & `terminateAndAwaitExit` cancellation keeping `.owned` child + open pipes). |
| `apps/macos/Carbon/Tests/test_shutdown.swift` | Modified | Added Tests 17–18 covering D2 (retained `activeShutdownWorkTask` after watchdog expiry, blocked overlapping Retry Quit, zero stale ownership mutations, clean subsequent Retry Quit after unwind, external process preservation, and combined D1+D2 real `ManagedProcess` cancellation). |

---

## 2. D1 & D2 Root Causes and Implementation Details

### 2.1 D1 — `ManagedProcess` Cancellation Busy-Spin
- **Root Cause:** In `ManagedProcess.awaitExit(timeout:)` (`ProcessSupervisor.swift:276`) and `ManagedProcess.terminateAndAwaitExit(timeout:)` (`ProcessSupervisor.swift:300`), the wait loops executed `while isRunning && Date() < deadline { try? await Task.sleep(...) }`. Swallowing `CancellationError` via `try?` without checking `!Task.isCancelled` caused cancelled wait tasks (from the global watchdog or `cancelStartup()`) to spin instantaneously at high CPU until `Date() >= deadline`.
- **Correction:** Added `&& !Task.isCancelled` to both polling loop conditions (`ProcessSupervisor.swift:276, 300`).
- **Safety Preserved:** Return values still check `!isRunning` after loop exit; if still running, pipes remain open (never closed while child runs) and `.timedOut` is returned cleanly without falsely reporting `.exited`.

### 2.2 D2 — Untracked Shutdown Work & Overlapping Retries After Watchdog Expiry
- **Root Cause:** When the 8.0s global watchdog fired inside `performCoordinatedShutdown()`, `watchdogTask` cancelled `workTask` and immediately resumed `continuation`, causing `shutdown()` to clear `shutdownTask = nil` while an un-cancellable suspension in `workTask` (such as a hung `httpClient.send`) could still be running untracked. Additionally, Phase 3 cleanup (`!child.isRunning`) mutated `_childBackendProcess` / `backendOwnership` without guarding on `!settled` or an attempt identifier.
- **Correction:**
  1. **Tracked Work Task (`ProcessSupervisor.swift:455, 1697`):** Added `activeShutdownWorkTask: Task<Void, Never>?` (exposed read-only via `hasActiveShutdownWorkTask`) and `shutdownWorkLaunchCount: Int`. It is retained independently of `shutdownTask` even after watchdog expiry and cleared in `workTask`'s `defer` only after the task genuinely exits and matches `activeShutdownRunId`.
  2. **Per-Attempt Run Identity (`ProcessSupervisor.swift:456, 1295, 1464-1467`):** Every `shutdown()` invocation generates a `UUID` (`runId`) stored in `activeShutdownRunId`. `@MainActor func isRunStillActive() -> Bool` verifies `!settled && !Task.isCancelled && self.activeShutdownRunId == runId` before entering every phase AND before mutating backend or Ollama ownership/child-handle state (`lines 1505, 1521, 1545, 1572, 1581`).
  3. **Safe `terminationHandler` Compatibility:** When confirming child exit inside Phase 3 (`line 1545`) and Phase 4 (`line 1581`), checked `(self._childBackendProcess === child || (self._childBackendProcess == nil && !child.isRunning))` (and equivalent for Ollama) under `isRunStillActive()` so fast `terminationHandler` clearances do not cause `.notApplicable` outcome misreporting.
  4. **Overlap Prevention (`ProcessSupervisor.swift:1257-1276`):** If `activeShutdownWorkTask != nil` when Retry Quit is invoked, `shutdown()` refuses to launch an overlapping task (`shutdownWorkLaunchCount` does not increment), retains `activeShutdownWorkTask`, keeps the `SingleInstanceLock` held (`lockReleased: false`), and returns `canTerminate: false` until the earlier task finishes unwinding.
  5. **Watchdog Behaviour (`ProcessSupervisor.swift:1339-1425`):** Cancels `workTask`, preserves `.owned` for any child not confirmed exited, preserves `.external` untouched, keeps `SingleInstanceLock` held whenever owned children remain, and returns `canTerminate: false`.

---

## 3. Tests Added / Updated

- **D1 Tests (`apps/macos/Carbon/Tests/test_process_supervisor.swift`):**
  - **Test 34:** Cancels `awaitExit(timeout: 5.0)` on a live running child; asserts prompt exit (`< 0.5s`, actual `0.05s`), `.timedOut` return, child still running, and pipes kept open (`hasHandlerAttached == true`).
  - **Test 35:** Cancels `terminateAndAwaitExit(timeout: 5.0)` both directly and via `supervisor.terminateOwnedChildrenAndAwaitExit(timeout: 5.0)` against a live `SIGTERM`-ignoring child; asserts prompt abort (`0.05s`), child still running, pipes kept open, and `ollamaOwnership == .owned` preserved.
- **D2 Tests (`apps/macos/Carbon/Tests/test_shutdown.swift`):**
  - **Test 17:** Triggers global watchdog (`0.15s`) while `httpClient.send` hangs in a cancellation-insensitive `DispatchQueue` continuation (`0.55s`); asserts `canTerminate == false`, `hasActiveShutdownWorkTask == true`, immediate Retry Quit blocked (`shutdownWorkLaunchCount == 1`, `canTerminate == false`, lock held), zero stale mutations after old task unwinds (`terminateCallCount == 0`, `backendOwnership == .owned`, external Ollama `.external`), and clean subsequent Retry Quit (`shutdownWorkLaunchCount == 2`, `canTerminate == true`, lock released).
  - **Test 18:** Combined D1+D2 watchdog against a live `SIGTERM`-ignoring child under `ManagedProcess`; asserts watchdog triggers cleanly in `< 0.5s`, `workTask` exits in `< 50ms` (no 5.0s busy-spin), `.owned` preserved on Ollama, and `.external` preserved on backend.

---

## 4. Verification Results (Actual Executed Commands & Exit Codes)

All automated suites below were executed **after all edits to `ProcessSupervisor.swift`**, with both `test_process_supervisor` and `test_shutdown` re-executed after their final test additions:

| Command | Result | Exit Code | Status |
|---|---|---|---|
| `swiftc -parse-as-library apps/macos/Carbon/Sources/ProcessSupervisor.swift apps/macos/Carbon/Tests/test_process_supervisor.swift -o /tmp/test_process_supervisor && /tmp/test_process_supervisor` | **35 / 35 PASS** (including Tests 34–35 for D1) | `0` | Executed |
| `swiftc -parse-as-library apps/macos/Carbon/Sources/SingleInstanceLock.swift apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Sources/ProcessSupervisor.swift apps/macos/Carbon/Tests/test_shutdown.swift -o /tmp/test_shutdown && /tmp/test_shutdown` | **18 / 18 PASS** (including Tests 17–18 for D2) | `0` | Executed |
| `swiftc -parse-as-library apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Sources/ProcessSupervisor.swift apps/macos/Carbon/Tests/test_startup_state_machine.swift -o /tmp/test_startup_state_machine && /tmp/test_startup_state_machine` | **16 / 16 PASS** | `0` | Executed (post-source edit) |
| `swiftc -parse-as-library apps/macos/Carbon/Sources/SingleInstanceLock.swift apps/macos/Carbon/Tests/test_single_instance_lock.swift -o /tmp/test_single_instance_lock && /tmp/test_single_instance_lock` | **8 / 8 PASS** | `0` | Executed (post-source edit) |
| `swiftc -parse-as-library apps/macos/Carbon/Sources/MarkdownRenderer.swift apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Tests/test_markdown.swift -o /tmp/test_markdown && /tmp/test_markdown` | **ALL PASS** | `0` | Executed (post-source edit) |
| `swift build --package-path apps/macos/Carbon` | **Build complete! (1.31 secs)** | `0` | Executed (post-source edit) |
| `.venv/bin/pytest tests/test_desktop_connection_reliability.py tests/integration/ tests/security/ tests/unit/` | **85 passed, 1 skipped, 1 warning in 3.72s** (`test_e2e_inference.py` skipped: Ollama daemon not running) | `0` | Executed (post-source edit) |
| `.venv/bin/ruff check src/ tests/` | **All checks passed!** | `0` | Executed (post-source edit) |
| `git diff --check` | **Clean (no output)** | `0` | Executed (after all edits) |

### Tests Not Run
- **Interactive Manual macOS GUI Quit Test** & **`tests/integration/test_e2e_inference.py` (skipped):** Not executed because production Ollama and backend services are not running locally and starting/stopping production services is disallowed.

---

## 5. Safety, External Services & Git Confirmations

- **External Services:** No production Ollama or Carbon backend services were started, stopped, or modified.
- **No Raw PID Signalling / Force-Kill:** `grep -rn "SIGKILL\|\bkill(" apps/macos/Carbon/Sources/` returned **0 matches**. Only retained `Foundation.Process` handles (`Process.terminate()`) are used.
- **Git Branch, HEAD & Backup:**
  - Branch: `backup/carbon-tested-phase1-2026-10-29`
  - `HEAD`: `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`
  - Protected backup ref `backup/carbon-tested-phase1-2026-10-29`: `0c7d1d17c55c836715438c7d0c00887cf9aaf19c` (intact; zero commits, pushes, or branch changes).
- **Remaining Limitation:** If a custom HTTP or kernel operation is truly cancellation-insensitive and hangs beyond the watchdog, Retry Quit remains safely blocked (`canTerminate = false`, lock held) until that operation unblocks and cleans up, preventing overlapping work.

**Stop Condition:** Implementation and verification of D1 and D2 are complete. Increment 2E has not started. Submitted for independent project-owner review and acceptance.
