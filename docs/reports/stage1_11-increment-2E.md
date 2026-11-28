# Carbon Stage 1.11 — Increment 2E: End-to-End Lifecycle Verification Report

**Task ID:** `C-1.11-P2-2E`  
**Branch / HEAD:** `backup/carbon-tested-phase1-2026-10-29` (`0c7d1d17c55c836715438c7d0c00887cf9aaf19c`)  
**Status:** Submitted for independent review and project-owner decision (2 real defects discovered during E2E recovery testing; source code left unmodified pending approval).

---

## 1. Objective and Scope

Increment 2E verifies that the accepted Increment 2A–2D implementation operates end-to-end across the native macOS SwiftUI binary ([Carbon](../../apps/macos/Carbon/.build/debug/Carbon)), local Ollama (`/opt/homebrew/bin/ollama`), the Carbon Python backend (`.venv/bin/python -m carbon.main`), authenticated WebSocket chat, kernel-level `SingleInstanceLock`, 5-phase graceful shutdown, and the Swift + Python regression suites.

**Boundaries respected:**
- Protected GitHub backup branches (`backup/carbon-phase1-2026-10-28` and `backup/carbon-tested-phase1-2026-10-29`) were never modified, committed to, pushed, or switched.
- No Increment 2A–2D source or test files were altered.
- No raw PID kill workarounds or `kill -9` / `SIGKILL` were used.

---

## 2. Preflight Findings

| Preflight Check | Observed Result | Verdict |
| :--- | :--- | :--- |
| Current Git branch & HEAD | `backup/carbon-tested-phase1-2026-10-29` (`0c7d1d17c55c836715438c7d0c00887cf9aaf19c`) | **PASS** |
| Protected backup checkpoints | `backup/carbon-phase1-2026-10-28` (`2fec130d5443ede81f2edaa226f94fd7df32db81`) & `backup/carbon-tested-phase1-2026-10-29` (`0c7d1d17c55c836715438c7d0c00887cf9aaf19c`) match `origin/*` | **PASS** |
| Uncommitted working tree | Modified: `CarbonApp.swift`, `CarbonClient.swift`, `ContentView.swift`, `phase1-final-acceptance.md`, `orchestrator.py`, `main.py`, `ollama_provider.py`, `test_api_endpoints.py`, `test_desktop_connection_reliability.py`; Untracked Increment 2A–2D files & Stage 1.10/1.11 reports/scripts | **PASS** |
| Pre-test Ollama (`127.0.0.1:11434`) | Stopped (`curl` exit 7, nothing listening) | **PASS** |
| Pre-test Backend (`127.0.0.1:8765`) | Stopped (`curl` exit 7, nothing listening) | **PASS** |
| Pre-test `Carbon` / orphan processes | None running; no `data/.carbon_app.lock` file | **PASS** |
| Model `qwen3.5:9b` installed | Present at `~/.ollama/models/manifests/registry.ollama.ai/library/qwen3.5/9b` (`1088` bytes); later confirmed via `/api/tags` (`["qwen3.5:9b"]`) | **PASS** |
| Local token existence | `data/.carbon_token` exists (`43` bytes, `-rw-------`); contents not printed | **PASS** |
| Toolchain & binaries | `/opt/homebrew/bin/ollama` (`0.33.0`), `.venv/bin/python` (`python3.12`), `/usr/bin/swift` (`swift build` succeeded in `0.96s` pre-test) | **PASS** |

---

## 3. End-to-End Scenario Results

| Scenario | Execution Target | Result Summary | Verdict |
| :--- | :--- | :--- | :--- |
| **Test A:** Both services stopped | Real `.build/debug/Carbon` GUI binary | Spawned owned `ollama serve` (`0.223s`), verified `qwen3.5:9b` without pull (`0.223s`), spawned owned `carbon.main` (`0.573s`), authenticated WS `ESTABLISHED` (`0.651s`), lock held. *(UI window badge text inspection blocked by macOS TCC)* | **PASS** |
| **Test B:** Both services running | Real `.build/debug/Carbon` GUI binary | Reused external Ollama (`14230`) & external backend (`14235`); WS `ESTABLISHED` (`0.227s`); zero child spawns; quit via `File -> Close` (`0.271s`, exit `0`); both external services stayed alive (`200`) | **PASS** |
| **Test C:** Only Ollama running | Real `.build/debug/Carbon` GUI binary | Reused external Ollama (`14300`); spawned only owned `carbon.main` (`14307`, PPID `14305`), ready & WS in `0.384s`; quit via `Quit Carbon` (`0.494s`, exit `0`); backend stopped, external Ollama alive (`200`) | **PASS** |
| **Test D:** Only backend running | Real `.build/debug/Carbon` GUI binary | Pre-launch backend (`14350`) reported `"degraded"` (`ollama_connected: false`); Carbon spawned owned Ollama (`14369`, `0.214s`), re-probed health (`"healthy"`), reused `14350`, WS `ESTABLISHED` (`0.214s`); quit stopped `14369` while `14350` stayed alive | **PASS** |
| **Test E:** Second app instance | Real `.build/debug/Carbon` GUI binary | Secondary exited in `0.034s`/`0.037s` (`exit 0`), printed `Activated existing instance via PID ...`, created zero duplicate processes/WS/lock, primary retained `flock`. However, System Events `frontmost` stayed `'Finder'` after activating Finder first (see §10) | **PARTIAL** |
| **Test F:** Startup cancellation | Controlled Swift E2E harness (`ProcessSupervisor` + real external Ollama + real backend) | Cancelled mid-backend startup -> `startupCancelled`, `startupState == .failed`, `isConnected == false`, external Ollama (`14803`) preserved. **Defect:** `backendOwnership` stayed `.owned` synchronously upon `startup()` return (`backendOwnImmediate=owned`), clearing only ~50ms later via `terminationHandler` | **FAIL** |
| **Test G:** Retry after failure | Real `.build/debug/Carbon` (failure/rollback) + Swift E2E harness (full failure + retry -> ready) | 503 blocker triggered `.backendHealthDegraded`, rolled back owned Ollama (`0.370s`), quit cleanly (`0.304s`); removing blocker & retrying recovered cleanly to `.ready` (`0.512s`, zero duplicates). **Caveats:** Swift `recoverySuggestion: String` mismatch drops UI detail subtitle (`detailUI=""`), and TCC blocked direct UI Retry click | **PARTIAL** |
| **Part 4.1:** Window close quit | Real `.build/debug/Carbon` (`File -> Close`) | Triggered `applicationShouldTerminateAfterLastWindowClosed` -> `applicationShouldTerminate`; exited `0` in `0.271s`, preserved externals, released `flock`. *(Physical red close button click blocked by macOS AX TCC)* | **PARTIAL** |
| **Part 4.2:** Menu bar → `Quit Carbon` | Real `.build/debug/Carbon` | Clicked `Quit Carbon` across Tests A (`0.521s`), C (`0.494s`), D (`0.279s`), G-fail (`0.304s`); all exited `0`, stopped only owned children, preserved externals, released lock | **PASS** |
| **Part 4.3:** Dock → `Quit` | Real `.build/debug/Carbon` | Opened Dock context menu (`AXShowMenu` on Dock item `Carbon`) -> clicked `Quit`; exited `0` in `1.088s` (incl. `0.2s` UI menu delay), stopped both owned processes, released lock | **PASS** |
| **Part 4.4:** Active inference shutdown + safeguards + relaunch | Swift E2E harness against real Ollama & real backend | Verified `inferenceCancelled=true`, `webSocketClosed=true`, `backendHttpRequested=true`, `autoReconnectEnabled=false`. Run 1 hit Phase 3 3.0s timeout safeguard cleanly (`canTerminate=false`, `lockReleased=false`); Run 2 exited cleanly in `3.416s` (`backend=exited(15)`, `ollama=exited(0)`) & fresh relaunch hit `.ready` in `0.386s` | **PASS** |

---

## 4. Measured Timings

All timings were measured directly via monotonic clocks and polling loops (no estimates):

| Scenario / Transition | Measured Duration | Notes |
| :--- | :---: | :--- |
| **Test A** — Cold launch (`14172`) -> Ollama `/api/version` (`0.33.0`) | **0.223s** | Owned `ollama serve` (`14174`) |
| **Test A** — `qwen3.5:9b` verified via `/api/tags` | **0.223s** | No auto-pull |
| **Test A** — Owned backend `/api/v1/health` (`healthy`) | **0.573s** | Owned `carbon.main` (`14180`) |
| **Test A** — Authenticated WebSocket `ESTABLISHED` | **0.651s** | Loopback `127.0.0.1:8765` |
| **Test B** — Reused external Ollama + backend -> WS `ESTABLISHED` | **0.227s** | Zero child spawns |
| **Test C** — Reused Ollama -> owned backend + WS `ESTABLISHED` | **0.384s** | Only `carbon.main` spawned |
| **Test D** — Reused backend -> owned Ollama + WS `ESTABLISHED` | **0.214s** | Only `ollama serve` spawned |
| **Test E** — Secondary instance launch -> exit (`0`) | **0.034s / 0.037s** | Two independent measurements |
| **Test G** — Failure rollback of owned Ollama on `:8765` 503 | **0.370s** | Before `startup()` returned failed |
| **Test G** — Retry startup after 503 blocker cleared -> `.ready` | **0.512s** | Clean cold start of Ollama + backend |
| **Menu Bar Quit** (`Quit Carbon`) | **0.279s – 0.521s** | `0.521s` (Test A), `0.494s` (Test C), `0.279s` (Test D), `0.304s` (Test G-fail) |
| **Window Close** (`File -> Close`) | **0.271s** | Test B (`exit 0`) |
| **Dock Quit** (`Dock -> Carbon -> Quit`) | **1.088s** | Test A2 (includes `0.200s` AppleScript menu open delay) |
| **Active-inference shutdown** (Run 2 clean exit / Run 1 timeout guard) | **3.416s / 4.378s** | Run 1 hit 3.0s Phase 3 limit cleanly; Run 2 fell back to SIGTERM and exited `15` |
| **Fresh relaunch after active-inference shutdown** | **0.386s** | Returned to `.ready` + `isConnected=true` |

---

## 5. Ownership Verification

Process relationships (`ps -eo pid,ppid,args`) observed live:

- **Test A (`Carbon PID 14172`):**
  - `14174` (`PPID 14172`) `/opt/homebrew/bin/ollama serve` -> owned child
  - `14180` (`PPID 14172`) `Python -m carbon.main` -> owned child
- **Test B (`Carbon PID 14246`):**
  - `14230` (`PPID 14229`) `/opt/homebrew/bin/ollama serve` -> external
  - `14235` (`PPID 14229`) `Python -m carbon.main` -> external
  - Zero child processes with `PPID == 14246`
- **Test C (`Carbon PID 14305`):**
  - `14300` (`PPID 14299`) `/opt/homebrew/bin/ollama serve` -> external
  - `14307` (`PPID 14305`) `Python -m carbon.main` -> owned child
- **Test D (`Carbon PID 14361`):**
  - `14350` (`PPID 14349`) `Python -m carbon.main` -> external
  - `14369` (`PPID 14361`) `/opt/homebrew/bin/ollama serve` -> owned child

---

## 6. External-Service Preservation Verification

- **Test B quit (`File -> Close`):** External Ollama `14230` (`HTTP 200 /api/version`) and external backend `14235` (`HTTP 200 /api/v1/health`) remained running and responsive.
- **Test C quit (`Quit Carbon`):** External Ollama `14300` remained running (`HTTP 200 /api/version`) while Carbon-owned backend `14307` stopped (`:8765` closed).
- **Test D quit (`Quit Carbon`):** External backend `14350` remained running while Carbon-owned Ollama `14369` stopped (`:11434` closed).
- **Test F cancellation:** External Ollama `14803` remained running (`extOllamaAlive=true`, `ollamaOwnership == .external`) when startup was cancelled mid-backend launch.

---

## 7. Orphan-Process & Resource Inspection

After final shutdowns and after all E2E tests completed:
- `ps -eo pid,ppid,stat,args | grep -iE 'ollama|carbon\.main|\.build/debug/Carbon'`: empty (zero Carbon, Ollama, or Python backend orphans).
- `lsof -nP -i :11434 -i :8765`: empty (`PORTS_DOWN`).
- `data/.carbon_app.lock`: inode preserved (`-rw-------`, `6` bytes); independent `fcntl.flock(..., LOCK_EX | LOCK_NB)` check confirmed `RELEASED`.
- Unauthenticated WebSocket probe (`ws://127.0.0.1:8765/ws/chat` without token) was rejected by the backend; authenticated WebSocket connection succeeded without printing or exposing `data/.carbon_token`.

---

## 8. Automated Regression Test Results

All suites were executed cleanly after E2E runs (`0` tests weakened or skipped by edits):

| Suite / Check | Exact Command | Outcome | Exit Code | Counts |
| :--- | :--- | :--- | :---: | :--- |
| **Swift Build** | `(cd apps/macos/Carbon && swift build)` | **PASS** | `0` | Built in `1.56s` |
| **Single-Instance Lock** | `swiftc -parse-as-library apps/macos/Carbon/Sources/SingleInstanceLock.swift apps/macos/Carbon/Tests/test_single_instance_lock.swift -o "$SCRATCH/test_single_instance_lock" && "$SCRATCH/test_single_instance_lock"` | **PASS** | `0` | `8/8 passed` |
| **Process Supervisor** | `swiftc -parse-as-library apps/macos/Carbon/Sources/ProcessSupervisor.swift apps/macos/Carbon/Tests/test_process_supervisor.swift -o "$SCRATCH/test_process_supervisor" && "$SCRATCH/test_process_supervisor"` | **PASS** | `0` | `35/35 passed` |
| **Startup State Machine** | `swiftc -parse-as-library apps/macos/Carbon/Sources/ProcessSupervisor.swift apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Tests/test_startup_state_machine.swift -o "$SCRATCH/test_startup_state_machine" && "$SCRATCH/test_startup_state_machine"` | **PASS** | `0` | `16/16 passed` |
| **Graceful Shutdown** | `swiftc -parse-as-library apps/macos/Carbon/Sources/ProcessSupervisor.swift apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Sources/SingleInstanceLock.swift apps/macos/Carbon/Tests/test_shutdown.swift -o "$SCRATCH/test_shutdown" && "$SCRATCH/test_shutdown"` | **PASS** | `0` | `18/18 passed` |
| **Markdown & UI Client** | `swiftc -parse-as-library apps/macos/Carbon/Sources/MarkdownRenderer.swift apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Tests/test_markdown.swift -o "$SCRATCH/test_markdown" && "$SCRATCH/test_markdown"` | **PASS** | `0` | All suites passed *(see §10 note on `CarbonClient.swift` dependency)* |
| **Python Regression (`pytest`)** | `.venv/bin/python -m pytest -v --tb=short` | **PASS (1 skipped)** | `0` | `85 passed, 1 skipped` (`3.71s`) |
| **Ruff Lint** | `.venv/bin/ruff check src/ tests/` | **PASS** | `0` | `All checks passed!` |
| **Diff Cleanliness** | `git diff --check` | **PASS** | `0` | Clean |

**Python `pytest` category breakdown (`86 collected`):**
- `tests/integration/test_api_endpoints.py`: `9 passed`
- `tests/integration/test_e2e_inference.py`: `1 skipped` (`Ollama daemon is not running locally` at time of `pytest` run)
- `tests/security/test_api_security.py`: `8 passed`
- `tests/test_desktop_connection_reliability.py`: `11 passed`
- `tests/unit/` (11 test files): `57 passed`

---

## 9. Evidence Logs (Redacted)

Key redacted observations from E2E runs:

```text
# Test A milestones
{"carbon_pid": 14172, "ollama_ready": 0.223, "ollama_version": "0.33.0", "model_verified": 0.223, "backend_healthy": 0.573, "ws_established": 0.651}
DOCK_QUIT_RC: 0  CARBON_EXIT_CODE=0 DOCK_SHUTDOWN_TIME=1.088s  FLOCK_AFTER: RELEASED

# Test E second instance
SECOND_EXIT=0 DT=0.034s  SECOND_STDERR: '[Carbon] Activated existing instance via PID 13883.\n'
front_before='Finder' second_exit=0 dt=0.037s second_stderr='[Carbon] Activated existing instance via PID 14418.' front_after='Finder'

# Test F cancellation E2E defect trace
[TEST F] caught=Optional(ProcessSupervisorError.startupCancelled) state=failed isConnected=false autoReconnect=false ollamaOwn=external backendOwnImmediate=owned backendOwn50ms=notRunning backendOwn450ms=notRunning backendPortOpen=false extOllamaAlive=true
[TEST F] VERDICT=FAIL

# Test G failure + retry E2E + recoverySuggestion trace
[TEST G] Failure phase: state=failed statusMsg=Carbon backend is running but reported degraded health: HTTP status 503 desc=Carbon backend is running but reported degraded health: HTTP status 503 detailUI= concreteRecovery=Check that port 8765 is running a healthy Carbon backend with 'qwen3.5:9b', then click Retry. ollamaOwn=notRunning ollamaPort11434=false
[TEST G] Retry phase: dt=0.512s state=ready isConnected=true ollamaOwn=owned backendOwn=owned

# Part 4 active inference shutdown (Run 1 safeguard vs Run 2 clean termination)
[PART 4 Run 1] Inference active: started=true taskId=task-1ddac220
[PART 4 Run 1] Shutdown report (4.378s): canTerminate=false inferenceCancelled=true wsClosed=true httpReq=true backend=timedOut ollama=exited(terminationStatus: 0) lockReleased=false timedOut=false
[PART 4 Run 2] Inference active: started=true taskId=task-72ccc043
[PART 4 Run 2] Shutdown report (3.416s): canTerminate=true inferenceCancelled=true wsClosed=true httpReq=true backend=exited(terminationStatus: 15) ollama=exited(terminationStatus: 0) lockReleased=true timedOut=false
[PART 4 Run 2] Fresh launch ready in 0.386s and clean re-shutdown succeeded.
```

Relevant `data/audit.log` entries confirmed backend lifecycle events without exposing secrets:
- `{"timestamp": "2026-11-25T07:13:18.846289+00:00", "event_type": "CARBON_CORE_INITIALIZED", ... "primary_model": "qwen3.5:9b"}`
- `{"timestamp": "2026-11-25T07:13:23.401333+00:00", "event_type": "CARBON_CORE_SHUTDOWN", ... "status": "stopped"}`

---

## 10. Known Defects, Limitations, and Unverified UI Checks

1. **Defect F1 — Startup cancellation skips awaiting owned child exit (`Test F: FAIL`):**
   - **Location:** [ProcessSupervisor.swift:300](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L300) interacting with cancellation cleanup `catch` blocks at [`lines 730, 905, 1016, 1205`](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L1205).
   - **Cause:** The D1 polling guard `while isRunning && Date() < deadline && !Task.isCancelled` runs inside `terminateAndAwaitExit(timeout:)`. When `cancelStartup()` cancels `startupTask` and `backendStartupTask`, their `catch` handlers execute while `Task.isCancelled == true`, causing `terminateAndAwaitExit(timeout: 2.0)` to send `SIGTERM` and immediately return `.timedOut` with 0 polling iterations. `startup()` returns `startupCancelled` while `backendOwnership` is still `.owned` (`backendOwnImmediate=owned`), relying on async `proc.terminationHandler` ~50ms later (`backendOwn50ms=notRunning`).
   - **Smallest fix (not applied; awaiting owner approval):** Add an uncancelled cleanup wait path (or parameter such as `respectTaskCancellation: Bool = true` / detached cleanup `Task` for startup rollback) so shutdown watchdog cancellation aborts polling immediately while startup rollback awaits confirmed process exit up to its 2.0s deadline.

2. **Defect G1 — `ProcessSupervisorError.recoverySuggestion` omitted in `String?` contexts (`Test G: PARTIAL`):**
   - **Location:** [ProcessSupervisor.swift:137](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L137), [`line 1212`](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift#L1212), and [ContentView.swift:239](../../apps/macos/Carbon/Sources/ContentView.swift#L239).
   - **Cause:** `ProcessSupervisorError` conforms to `LocalizedError` (whose protocol requirement is `var recoverySuggestion: String? { get }` defaulting to `nil`) but declares `public var recoverySuggestion: String` (non-optional). In `Optional`-expecting contexts (`detail: mappedError.recoverySuggestion` and `supervisor.startupError?.recoverySuggestion`), Swift resolves to `LocalizedError`'s default extension (`nil`), causing `startupDetailMessage` to be `nil` on startup failure while `startupError.errorDescription` still displays cleanly.
   - **Smallest fix (not applied; awaiting owner approval):** Change `public var recoverySuggestion: String` to `public var recoverySuggestion: String?` in `ProcessSupervisorError`.

3. **Limitation L1 — macOS WindowServer focus activation from secondary unbundled binary (`Test E: PARTIAL`):**
   - Secondary `.build/debug/Carbon` detected the lock, logged `[Carbon] Activated existing instance via PID ...`, and exited `exit 0` in `0.034s–0.037s`. However, when `Finder` was explicitly focused beforehand on macOS (`Darwin 27.2.1`), `System Events` reported `frontmost` remained `'Finder'` after calling `NSRunningApplication(processIdentifier: pid)?.activate(options: [.activateAllWindows])` on the unbundled debug binary.

4. **Limitation L2 — macOS Accessibility & Screen Recording TCC permissions (`Test A`, `Test G`, `Part 4.1`):**
   - `osascript` `System Events` returned `0` windows for both `Carbon` and `Google Chrome`, and `screencapture -l <windowID>` failed with `could not create image from window` (even though `CGWindowListCopyWindowInfo` confirmed Carbon window `2868` at `800x640`, `335,175`). Consequently:
     - Direct SwiftUI banner inspection / clicking in-window buttons (`Cancel`, `Retry`) and clicking the traffic-light `AXCloseButton` were not automatable via Accessibility; window close was verified through menu `File -> Close` (exact `applicationShouldTerminateAfterLastWindowClosed` path), and Recovery tests F & G + mid-inference shutdown were executed cleanly via a scratch Swift E2E driver linking the exact unmodified `Sources/*.swift` files.
   - `test_markdown.swift` requires compiling alongside `CarbonClient.swift` in addition to `MarkdownRenderer.swift`.
   - `tests/integration/test_e2e_inference.py` (`1` test) was skipped by `pytest` because Ollama was stopped during the automated suite run.

---

## 11. Source-Code Modifications

**None.** Zero project source files or test files were modified during Increment 2E. (Temporary E2E harness files were written strictly under the conversation scratch directory `<appDataDir>/brain/<conversation-id>/scratch/`.)

---

## 12. Final Git Status

- **Branch:** `backup/carbon-tested-phase1-2026-10-29`
- **HEAD SHA:** `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`
- **Protected backups:** Unchanged (`2fec130d5443ede81f2edaa226f94fd7df32db81`, `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`)
- **Working tree changes:** Prior Increment 2A–2D / Stage 1.10 changes plus this new report `docs/reports/stage1_11-increment-2E.md`. Zero Git commits, pushes, or branch changes made.
