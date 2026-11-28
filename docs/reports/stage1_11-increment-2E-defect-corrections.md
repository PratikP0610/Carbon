# Carbon Stage 1.11 — Increment 2E Defect Corrections (F1 + G1)

**Task ID:** `C-1.11-P2-2E-FIX`  
**Status:** Submitted for independent review and project-owner acceptance (NOT marked accepted by agent)  
**Branch / HEAD:** `backup/carbon-tested-phase1-2026-10-29` (`0c7d1d17c55c836715438c7d0c00887cf9aaf19c`)

---

## 1. Root Cause Analysis

### Defect F1 — Startup cancellation skipped awaiting owned child termination
- **Root cause:** In Increment 2D-FIX (Defect D1), `ManagedProcess.terminateAndAwaitExit(timeout:)` ([ProcessSupervisor.swift:290-312](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L290-L312)) added `&& !Task.isCancelled` to its exit polling loop so Increment 2D shutdown watchdog cancellation stops polling immediately without busy-spinning. However, when `cancelStartup()` cancelled `startupTask`, `ollamaStartupTask`, and `backendStartupTask`, their `catch` rollback handlers invoked `terminateAndAwaitExit(timeout: 2.0)` **inside those already-cancelled tasks** (`Task.isCancelled == true`).
- **Observed failure in Increment 2E Test F:** `terminateAndAwaitExit(timeout: 2.0)` sent `SIGTERM`, immediately exited the polling loop on iteration 0 (`!Task.isCancelled == false`), and returned `.timedOut` while the backend child was still finishing `SIGTERM` shutdown. As a result, `startup()` returned `.startupCancelled` while `backendOwnership` remained `.owned` synchronously (`backendOwnImmediate=owned`), only clearing asynchronously ~50ms later via `proc.terminationHandler`.

### Defect G1 — `ProcessSupervisorError.recoverySuggestion` missing in `String?` contexts
- **Root cause:** `ProcessSupervisorError` ([ProcessSupervisor.swift:97-163](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L97-L163)) conforms to `Foundation.LocalizedError`, whose protocol requirement is `var recoverySuggestion: String? { get }` (with a default protocol extension returning `nil`). `ProcessSupervisorError` previously declared `public var recoverySuggestion: String` (`line 137`, non-optional).
- **Observed failure in Increment 2E Test G:** At optional-typed call sites — `setState(.failed, ..., detail: mappedError.recoverySuggestion)` ([ProcessSupervisor.swift:1268](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L1268)) and `supervisor.startupError?.recoverySuggestion` ([ContentView.swift:239](../../apps/macos/Carbon/Sources/ContentView.swift#L239)) — Swift resolved to `LocalizedError`'s default `nil` extension, leaving `startupDetailMessage` empty (`nil`) on startup failure while `errorDescription` still rendered cleanly.

---

## 2. Files Modified

Only the three files strictly required for F1, G1, and their regression tests were modified:

1. [ProcessSupervisor.swift](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift) — Fixed G1 (`recoverySuggestion: String?`), added isolated F1 startup cleanup helper (`cleanupOwnedChildDuringStartup` / `cleanupAllOwnedChildrenDuringStartup`), run-identity and cleanup-task tracking (`activeStartupRunId`, `activeStartupCleanupTask`, `attemptedStartupCleanupChildIds`), startup serialization against active rollback cleanup, and shutdown handover/cancellation in `performCoordinatedShutdown`.
2. [test_startup_state_machine.swift](../../apps/macos/Carbon/Tests/test_startup_state_machine.swift) — Updated optional unwraps in Tests 5–6 (`err.recoverySuggestion?.contains(...) == true`) and added Tests 17–19 covering F1 real-subprocess `SIGTERM` await, F1 timeout + no-duplicate Retry with retained `.owned`/handle/pipes, and G1 `LocalizedError` + `startupDetailMessage` parity.
3. [test_shutdown.swift](../../apps/macos/Carbon/Tests/test_shutdown.swift) — Added Test 19 verifying `shutdown()` safely interrupts an active F1 startup rollback wait, enforces the global watchdog without stale ownership mutations, and completes cleanly on Retry Quit.

---

## 3. F1 Cleanup Design & Safety Architecture

To fix F1 without weakening Increment 2D shutdown's cancellation-aware behavior (`Task.isCancelled` checks at `ProcessSupervisor.swift:276, 300` remain completely intact):

1. **Uncancelled unstructured rollback wait (`cleanupOwnedChildDuringStartup` at [`lines 579-630`](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L579-L630)):**
   - All startup failure, timeout, and cancellation rollback paths (`lines 704, 707, 804, 811, 960, 968, 972, 1058, 1067, 1074, 1261`) delegate to `cleanupOwnedChildDuringStartup(isOllama:child:runId:timeout:)` / `cleanupAllOwnedChildrenDuringStartup(runId:timeout:)`.
   - Inside that helper, an **unstructured** `Task<Void, Never> { @MainActor [weak self] in ... }` runs `await child.terminateAndAwaitExit(timeout: 2.0)`. Because an unstructured `Task` does not inherit `startupTask`'s cancellation flag, `terminateAndAwaitExit` awaits confirmed child exit up to `2.0s` even when invoked from inside a cancelled startup `catch` block.
   - Because `startup()` awaits `cleanupTask.value` before throwing `.startupCancelled`, callers of `startup(...)` (and subsequent `startup(...)` retries waiting at `lines 1100-1107`) observe confirmed process termination (`.notRunning` and `child == nil`) synchronously upon return when the child exits within budget.

2. **Timeout safety & pipe preservation:**
   - No raw PID signalling, `kill -9`, or `SIGKILL` is used anywhere.
   - If the bounded `2.0s` rollback wait expires while the child is still alive (`res == .timedOut`), `.owned`, `_childBackendProcess`/`_childOllamaProcess`, and `PipeDrainer` handlers remain completely intact, and Step 0 (`lines 679, 938`) reuses/monitors the retained running child on Retry without spawning a duplicate.

3. **Run identity & shutdown handover (`lines 454-457, 564-630, 1105-1143, 1383-1394`):**
   - Each `startup(...)` execution assigns a fresh `runId = UUID()` to `activeStartupRunId` (`line 1116`), and retries await both `existingTask` and any `activeStartupCleanupTask` before beginning (`lines 1100-1107`).
   - `attemptedStartupCleanupChildIds` avoids redundant back-to-back 2.0s waits on the exact same uncooperative child across nested `catch` blocks within a single `runId`.
   - `cleanupTask` checks `self.activeStartupRunId == runId && !self.isShuttingDown && !self.hasCompletedShutdown` and `=== child && == .owned && !child.isRunning` before mutating any state (`lines 608-625`), and clears `activeStartupCleanupTask = nil` in `defer` only if `self.activeStartupRunId == runId`.
   - When `performCoordinatedShutdown` begins (`lines 1383-1394`), it immediately invalidates `self.activeStartupRunId = nil`, cancels `startupTask`/`ollamaStartupTask`/`backendStartupTask`/`activeStartupCleanupTask`, awaits them cleanly (triggering D1's `!Task.isCancelled` exit inside `activeStartupCleanupTask` so shutdown does not stall behind a 2.0s rollback wait), and proceeds directly into Phases 1–5 + the 8.0s global watchdog.

---

## 4. Tests Added and Verification Results

### Newly Added Regression Tests
- `test_startup_state_machine.swift` `[17/19]`: Real `/usr/bin/python3` child trapping `SIGTERM` (`time.sleep(0.22)` before exit) via `DefaultProcessLauncher`; cancelling startup verifies rollback waited `0.23s` (`>= 0.15s`), child dead, `backendOwnership == .notRunning` immediately upon return, pipes closed, handle cleared, and external Ollama untouched.
- `test_startup_state_machine.swift` `[18/19]`: Real `/usr/bin/python3` child ignoring `SIGTERM` (`SIG_IGN`) via `DefaultProcessLauncher`; cancelling startup verifies bounded `~2.0s` wait retains `.owned`, handle identity, and open `PipeDrainer`, and subsequent Retry succeeds cleanly with `0` duplicate launches (`launches.count == 1`).
- `test_startup_state_machine.swift` `[19/19]`: Verifies all 11 `ProcessSupervisorError` cases return non-nil expected text through `err.recoverySuggestion`, `optErr?.recoverySuggestion`, and `(err as any LocalizedError).recoverySuggestion`, and verifies live failed `startup()` sets `startupError?.recoverySuggestion == startupDetailMessage`.
- `test_shutdown.swift` `[19/19]`: Cancels startup on a real `SIGTERM`-ignoring child, triggers `shutdown(budgets: ..., globalWatchdog: 0.25)` while F1 rollback is active (`hasActiveStartupCleanupTask == true`), verifies shutdown interrupts rollback and enforces the watchdog in `0.25s` (`< 0.7s`), holds `SingleInstanceLock`, preserves `.owned` + `.external`, clears cleanup tracking without stale mutation, and succeeds on Retry Quit once the child exits.

### Full Verification Suite Results

| Check | Exact Command | Outcome | Exit Code | Counts / Result |
| :--- | :--- | :--- | :---: | :--- |
| **Swift Build** | `(cd apps/macos/Carbon && swift build)` | **PASS** | `0` | Built cleanly (`1.42s`) |
| **Single-Instance Lock** | `swiftc -parse-as-library apps/macos/Carbon/Sources/SingleInstanceLock.swift apps/macos/Carbon/Tests/test_single_instance_lock.swift -o "$SCRATCH/test_single_instance_lock" && "$SCRATCH/test_single_instance_lock"` | **PASS** | `0` | `8/8 passed` |
| **Process Supervisor** | `swiftc -parse-as-library apps/macos/Carbon/Sources/ProcessSupervisor.swift apps/macos/Carbon/Tests/test_process_supervisor.swift -o "$SCRATCH/test_process_supervisor" && "$SCRATCH/test_process_supervisor"` | **PASS** | `0` | `35/35 passed` (incl. D1 Tests 34–35) |
| **Startup State Machine** | `swiftc -parse-as-library apps/macos/Carbon/Sources/ProcessSupervisor.swift apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Tests/test_startup_state_machine.swift -o "$SCRATCH/test_startup_state_machine" && "$SCRATCH/test_startup_state_machine"` | **PASS** | `0` | `19/19 passed` (incl. F1 & G1 Tests 17–19) |
| **Shutdown & Watchdog** | `swiftc -parse-as-library apps/macos/Carbon/Sources/ProcessSupervisor.swift apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Sources/SingleInstanceLock.swift apps/macos/Carbon/Tests/test_shutdown.swift -o "$SCRATCH/test_shutdown" && "$SCRATCH/test_shutdown"` | **PASS** | `0` | `19/19 passed` (incl. D2 Tests 17–18 & F1 handover Test 19) |
| **Markdown & UI Client** | `swiftc -parse-as-library apps/macos/Carbon/Sources/MarkdownRenderer.swift apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Tests/test_markdown.swift -o "$SCRATCH/test_markdown" && "$SCRATCH/test_markdown"` | **PASS** | `0` | All suites passed |
| **Python Regression (`pytest`)** | `.venv/bin/python -m pytest -v --tb=short` | **PASS (1 skipped)** | `0` | `85 passed, 1 skipped` (`3.72s`) |
| **Ruff Lint** | `.venv/bin/ruff check src/ tests/` | **PASS** | `0` | `All checks passed!` |
| **Diff Check** | `git diff --check` | **PASS** | `0` | Clean |

*Note on `pytest` skip:* `tests/integration/test_e2e_inference.py` (`1` test) was skipped because the local Ollama daemon was intentionally stopped during automated `pytest` execution.

---

## 5. Repeated Real-World E2E & Shutdown Verification

Executed against the updated Swift code with real `/opt/homebrew/bin/ollama`, `.venv/bin/python -m carbon.main`, `qwen3.5:9b`, `data/.carbon_token`, and the rebuilt `.build/debug/Carbon` macOS binary:

### Test F (Repeated) — **PASS**
- **Run:** Started external `ollama serve` (`PID 15754`), launched real backend via `ProcessSupervisor.startup(client:)`, cancelled via `cancelStartup()` mid-backend startup.
- **Measured Result:**
  - `caught=Optional(...startupCancelled)`, `state=failed`, `isConnected=false`, `autoReconnect=false`
  - `backendOwnImmediate=notRunning` (immediately upon `startup()` returning — F1 resolved!), `backendPortOpen=false`
  - `ollamaOwn=external`, `extOllamaAlive=true` (external Ollama untouched)
  - Zero duplicate retries enforced in Test 18 (`test_startup_state_machine`).

### Test G (Repeated) — **PASS** *(via Swift E2E + real binary 503 check)*
- **Controlled Swift E2E (`e2e_recovery_and_shutdown`):**
  - Temporary HTTP 503 blocker on `127.0.0.1:8765` failed with `backendHealthDegraded("HTTP status 503")`, rolled back owned Ollama (`ollamaOwn=notRunning`, `ollamaPort11434=false`), and populated **both** `startupDetailMessage` and `startupError?.recoverySuggestion` (`"Check that port 8765 is running a healthy Carbon backend with 'qwen3.5:9b', then click Retry."` — G1 resolved!).
  - Removing blocker and retrying recovered to `state=ready`, `isConnected=true`, `ollamaOwn=owned`, `backendOwn=owned` in **`0.417s`**.
- **Rebuilt `.build/debug/Carbon` Binary 503 Check:**
  - Spawned Ollama, probed `:8765` 503, automatically rolled back owned Ollama at **`0.971s`**, remained responsive for `Quit Carbon` (`quit_rc=0`, `exit_code=0`, **`0.312s`**).

### Shutdown Regression (Repeated) — **PASS**
- **Active-inference E2E (`task-ad1d553f`):** `shutdown` cancelled active inference (`inferenceCancelled=true`), closed WebSocket (`webSocketClosed=true`), sent authenticated HTTP shutdown (`backendHttpRequested=true`), exited backend via fallback SIGTERM (`exited(15)`), exited Ollama (`exited(0)`), and released `SingleInstanceLock` (`lockReleased=true`) in **`3.529s`**; fresh relaunch reached `.ready` in **`0.678s`** and shut down cleanly afterward.
- **Post-run process/port check:** `NO_ORPHANS` (`ollama`, `carbon.main`, `.build/debug/Carbon` empty) and `PORTS_DOWN` (`:11434`, `:8765` closed).
- **Watchdog & Retry Suite (`test_shutdown.swift`):** `19/19` passed cleanly (including D2 Tests 17–18 and F1 rollback handover Test 19).

---

## 6. Remaining Limitations

- macOS Accessibility TCC (`count of windows == 0` in `System Events`) prevented automated clicking of the in-window SwiftUI `Retry` / `Cancel` buttons directly inside `.build/debug/Carbon`; UI recovery text availability (`startupDetailMessage`, `startupError?.recoverySuggestion`) and programmatic retry/cancel flows were verified end-to-end against real `ollama serve` and `.venv/bin/python -m carbon.main`, while the rebuilt `.build/debug/Carbon` binary was directly verified on HTTP 503 failure rollback and menu `Quit Carbon`.
- `tests/integration/test_e2e_inference.py` (`1` test) remained skipped during offline `pytest`.

---

## 7. Final Git State

- **Branch:** `backup/carbon-tested-phase1-2026-10-29`
- **HEAD SHA:** `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`
- **Protected backups:** Unchanged (`2fec130d5443ede81f2edaa226f94fd7df32db81`, `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`)
- **Working tree (`git status -s`):** Prior Increment 2A–2D / Stage 1.10 files, `ProcessSupervisor.swift`, `test_startup_state_machine.swift`, `test_shutdown.swift`, `docs/reports/stage1_11-increment-2E.md`, and this report `docs/reports/stage1_11-increment-2E-defect-corrections.md`. No commits, pushes, branch switches, or backup operations performed.
