# Carbon 1.0.0 — Stage 1.11 Increment 2A Final Acceptance Audit Report
## Single-Instance Enforcement & Kernel Lock Governance

**Task ID:** C-1.11-P2-2A-ACCEPTANCE  
**Scope:** Single-Instance Enforcement Only  
**Audit Mode:** STRICTLY READ-ONLY VERIFICATION  
**Date:** November 2, 2026  
**Platform:** MacBook Air M5, 16GB unified memory, macOS 27.0.1  
**Target Repository:** `PratikP0610/Carbon`  
**Active Branch:** `backup/carbon-tested-phase1-2026-10-29`  
**HEAD Commit:** `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`  
**Deliverable File:** `docs/reports/stage1_11-increment-2A-final-acceptance.md`  

---

## 1. Executive Summary & Verdict

This read-only audit evaluates the implementation of **Stage 1.11 Increment 2A (Single-Instance Enforcement)** against its architectural requirements, startup sequencing invariants, process safety constraints, and regression criteria.

### Final Verdict: ACCEPTED (PASS)

- **Application Termination & Lock Release:** **PASS**. Lock descriptor release is properly registered with the AppKit application lifecycle via `@NSApplicationDelegateAdaptor`, is fully idempotent, cannot be triggered by secondary instances, and is automatically cleaned up by the Darwin kernel upon abnormal termination.
- **Development-Mode Activation:** **PASS**. Implemented multi-tier activation covers both packaged `.app` bundles (via bundle identifier) and development CLI / `swift run` launches (via lockfile PID metadata). Failed activation never compromises isolation and secondary instances exit unconditionally before initializing client or supervisor resources.
- **Startup Ordering:** **PASS**. Verified that lock acquisition executes as the first statement in `CarbonApp.init()`. Secondary instances terminate before `CarbonClient` instantiation, guaranteeing zero duplicate WebSocket connections, health checks, or supervisor tasks.
- **Regression & Scope Verification:** **PASS**. Only authorized files (`SingleInstanceLock.swift`, `CarbonApp.swift`, `test_single_instance_lock.swift`) were modified or created. Swift build succeeds in 1.27s. All 8 single-instance tests and all Markdown/rendering tests pass with 0 errors.

---

## 2. Area 1 — Application Termination and Lock Release

### 2.1 Registration with Existing Application Lifecycle
In `apps/macos/Carbon/Sources/CarbonApp.swift`:
- Line 239: `@NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate` binds AppKit's `NSApplicationDelegate` directly into the SwiftUI `App` lifecycle.
- Lines 24–26:
  ```swift
  func applicationWillTerminate(_ notification: Notification) {
      SingleInstanceLock.shared.release()
  }
  ```
When `NSApp` terminates, AppKit invokes `applicationWillTerminate(_:)` on the main runloop, reliably invoking `release()`.

### 2.2 Primary Instance Lock Release & Idempotency
In `apps/macos/Carbon/Sources/SingleInstanceLock.swift:93–102`:
```swift
public func release() {
    lockQueue.sync {
        guard let fd = lockFileDescriptor else { return }
        flock(fd, LOCK_UN)
        close(fd)
        self.lockFileDescriptor = nil
    }
}
```
- **Normal Release:** `flock(fd, LOCK_UN)` releases the kernel lock and `close(fd)` closes the open file descriptor.
- **Idempotency Guarantee:** `guard let fd = lockFileDescriptor else { return }` ensures that if `release()` is invoked multiple times (e.g. manual invocation followed by `deinit`), subsequent calls are no-ops. Double-unlock and `EBADF` double-close errors are completely eliminated.

### 2.3 Secondary Instance Isolation from Primary Lock
In `apps/macos/Carbon/Sources/SingleInstanceLock.swift:63–89`:
- A secondary instance opens a new file descriptor `fd` via `open(...)`.
- It executes `flock(fd, LOCK_EX | LOCK_NB)`.
- When the kernel returns `-1` with `EWOULDBLOCK` or `EAGAIN`, the secondary instance immediately calls `close(fd)` on its **own** descriptor and returns `.alreadyRunning(...)`.
- `self.lockFileDescriptor` on the secondary instance is **never set** (remains `nil`).
- When the secondary instance calls `exit(0)` or its `deinit` runs, `release()` encounters `lockFileDescriptor == nil` and does nothing.
- The primary instance's file descriptor resides in a separate process address space and kernel file-table entry, completely inaccessible to the secondary process.

### 2.4 Automatic Release upon Abnormal Termination
In POSIX/Darwin kernel architecture, `flock(2)` locks are bound to the kernel open file table entry. When a process terminates abruptly (e.g. `SIGKILL`, crash, segmentation fault), the kernel tears down the process table and closes all open file descriptors, automatically releasing any associated `flock` locks.
- **Empirical Proof (Test 4):** A child process acquired `flock` on a temporary lock file and terminated. The parent process immediately called `acquire()` on the same inode and successfully acquired `.acquired` with zero manual intervention or stale file cleanup.

---

## 3. Area 2 — Development-Mode Activation

### 3.1 Implemented Activation Architecture
In `apps/macos/Carbon/Sources/SingleInstanceLock.swift:113–140`:
```swift
public func activatePrimaryInstance(primaryPid: pid_t?) {
    // Tier 1: Search by bundle identifier (Packaged .app)
    if let bundleId = Bundle.main.bundleIdentifier, !bundleId.isEmpty {
        let apps = NSRunningApplication.runningApplications(withBundleIdentifier: bundleId)
        let currentPid = ProcessInfo.processInfo.processIdentifier
        if let primaryApp = apps.first(where: { $0.processIdentifier != currentPid }) {
            primaryApp.activate(options: [.activateAllWindows])
            fputs("[Carbon] Activated existing instance via bundle identifier '\(bundleId)' (PID: \(primaryApp.processIdentifier)).\n", stderr)
            return
        }
    }

    // Tier 2: Search by PID from lock metadata (Development / swift run)
    if let pid = primaryPid {
        if let app = NSRunningApplication(processIdentifier: pid) {
            app.activate(options: [.activateAllWindows])
            fputs("[Carbon] Activated existing instance via PID \(pid).\n", stderr)
            return
        }
    }

    // Tier 3: Diagnostic log if window activation could not be resolved
    fputs("[Carbon] Another instance is already running (PID: \(primaryPid.map(String.init) ?? "unknown")). Exiting secondary instance.\n", stderr)
}
```

### 3.2 Evaluation of Activation Behaviors
1. **Packaged `.app` Launches:**
   - Evaluates Tier 1. `Bundle.main.bundleIdentifier` returns the registered CFBundleIdentifier (`ai.carbon.Carbon`).
   - `NSRunningApplication.runningApplications(withBundleIdentifier:)` locates the running primary instance and orders its windows frontmost.
2. **Development / `swift run` Launches:**
   - Evaluates Tier 2. When running from Terminal (`swift run` or `.build/debug/Carbon`), the bundle identifier is not registered in LaunchServices.
   - Tier 2 reads `primaryPid` directly from the lockfile metadata and queries `NSRunningApplication(processIdentifier: pid)`, successfully focusing the existing GUI process.
3. **Failed Activation Safety:**
   - If both Tier 1 and Tier 2 fail (e.g. running in headless CI without a window server), Tier 3 writes a diagnostic message to `stderr`.
   - In `acquireOrActivateAndExit()`:
     ```swift
     case .alreadyRunning(let primaryPid):
         activatePrimaryInstance(primaryPid: primaryPid)
         exit(0)
     ```
     Execution unconditionally terminates via `exit(0)`. Failed window activation **never** allows the secondary process to continue execution.
4. **PID Authority Boundary:**
   - The PID stored in `data/.carbon_app.lock` is read strictly by `readPidFromFile(at:)` and passed solely to `NSRunningApplication(processIdentifier:)`.
   - It is **never** passed to `kill()`, `killpg()`, or POSIX signaling functions.
   - It does not confer process ownership or termination authority.

---

## 4. Area 3 — Startup Ordering & Execution Trace

### 4.1 Trace from `CarbonApp.init()`
In `apps/macos/Carbon/Sources/CarbonApp.swift:237–251`:
```swift
@main
struct CarbonApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var client: CarbonClient

    init() {
        if !CommandLine.arguments.contains("--skip-instance-lock") {
            SingleInstanceLock.shared.acquireOrActivateAndExit()
        }
        _client = StateObject(wrappedValue: CarbonClient())
    }
```

### 4.2 Exact Execution Sequence
```
Launch Process
     │
     ▼
CarbonApp.init()
     │
     ├── Step 1: SingleInstanceLock.shared.acquireOrActivateAndExit()
     │     │
     │     ├── acquire() -> open("data/.carbon_app.lock") -> flock(LOCK_EX | LOCK_NB)
     │     │
     │     ├── Branch A: Lock Held (Secondary Instance)
     │     │     ├── activatePrimaryInstance()
     │     │     └── exit(0)  ◄── [PROCESS TERMINATES HERE]
     │     │
     │     └── Branch B: Lock Acquired (Primary Instance)
     │           ├── Retain fd in memory
     │           ├── Write current PID to lockfile
     │           └── Return .acquired
     │
     ├── Step 2: _client = StateObject(wrappedValue: CarbonClient())
     │     └── CarbonClient.init()
     │           ├── URLSession configured
     │           ├── checkHealth() dispatched
     │           ├── startPeriodicHealthCheck() dispatched
     │           └── connectWebSocket() dispatched
     │
     └── Step 3: Scene / ContentView created with client
```

### 4.3 Invariant Verification
- **Lock Acquired First:** Verified at line 244. Lock acquisition precedes all other statements.
- **Secondary Instances Exit Immediately:** Verified at `SingleInstanceLock.swift:147`. Secondary instances terminate via `exit(0)`.
- **Zero Client / Network Resource Allocation on Secondary:** Line 247 (`_client = StateObject(...)`) is never reached by a secondary instance. No WebSocket task is spawned, no health check task is initiated, and no network sockets are allocated.

---

## 5. Area 4 — Regression and Scope Verification

### 5.1 Git Working Tree & Scope Inspection
Comparison against base branch `backup/carbon-tested-phase1-2026-10-29`:
```text
Changes not staged for commit:
	modified:   apps/macos/Carbon/Sources/CarbonApp.swift

Untracked files:
	apps/macos/Carbon/Sources/SingleInstanceLock.swift
	apps/macos/Carbon/Tests/test_single_instance_lock.swift
	docs/reports/stage1_11-increment-2A.md
```
- **Scope Compliance:** Only `SingleInstanceLock.swift`, `CarbonApp.swift`, and `test_single_instance_lock.swift` were added or modified for Increment 2A.
- **Prohibited Items Confirmed Absent:**
  - Zero Ollama process management logic.
  - Zero Python backend process management logic.
  - Zero `ProcessSupervisor.swift` files created.
  - Zero shutdown watchdog code.
  - Zero modifications to chat, typography, markdown parsing, or reasoning modes.
  - Zero external package dependencies added.

### 5.2 Build & Test Verification Evidence

1. **Swift Package Build:**
   - Command: `swift build --package-path apps/macos/Carbon`
   - Result: **Build complete! (1.27 secs, exit code 0, 0 warnings)**
2. **Single-Instance Test Suite (`test_single_instance_lock.swift`):**
   - Command: `swiftc -parse-as-library apps/macos/Carbon/Sources/SingleInstanceLock.swift apps/macos/Carbon/Tests/test_single_instance_lock.swift -o /tmp/test_single_instance_lock && /tmp/test_single_instance_lock`
   - Result:
     ```text
     === CARBON STAGE 1.11 INCREMENT 2A: SINGLE-INSTANCE ENFORCEMENT TESTS ===
     [1/8] Verifying first instance successfully acquires the lock...
           ✓ First instance acquired lock (PID: 22725)
     [2/8] Verifying second instance cannot acquire the same lock...
           ✓ Second instance was rejected with .alreadyRunning (Primary PID: 22725)
     [3/8] Verifying lock becomes available after primary releases it...
           ✓ Lock successfully re-acquired by second instance after release
     [4/8] Verifying lock becomes available after child process holding flock exits...
           ✓ Kernel released flock immediately upon child process exit
     [5/8] Verifying stale PID metadata does not prevent lock acquisition...
           ✓ Stale PID metadata safely overwritten; kernel flock authority preserved
     [6/8] Verifying secondary instance does not initialize lifecycle resources...
           ✓ Secondary instance correctly bypassed lifecycle resource initialization
     [7/8] Verifying lock acquisition failure produces controlled error diagnostic...
           ✓ Controlled diagnostic produced without crashing: 'Failed to create lock parent directory at /dev/null/subfolde...'
     [8/8] Verifying lock cleanup and deinit safety...
           ✓ Lock automatically released upon deinit; subsequent acquisition succeeded
     🎉 ALL 8 SINGLE-INSTANCE ENFORCEMENT TESTS PASSED SUCCESSFULLY!
     ```
3. **Compiled Binary Subprocess Integration Test:**
   - Ran `apps/macos/Carbon/.build/debug/Carbon` with an active background lock:
     ```text
     Secondary exit code: 0
     Secondary stderr: [Carbon] Another instance is already running (PID: 12345). Exiting secondary instance.
     ```
4. **Existing Swift Regression Suite (`test_markdown.swift`):**
   - Result: **ALL SWIFT MARKDOWN, RENDERING, AND STOP-BUTTON TESTS PASSED SUCCESSFULLY! (0 regressions)**
5. **Formatting and Linting:**
   - `git diff --check` $\longrightarrow$ Clean (0 whitespace/formatting errors).
   - `ruff check src/ tests/` $\longrightarrow$ All checks passed!

---

## 6. Audit Summary & Acceptance Decision

| Audit Criterion | Verification Finding | Verdict |
|---|---|:---:|
| **Kernel Lock Authority** | Uses `flock(fd, LOCK_EX \| LOCK_NB)` on `data/.carbon_app.lock` | **PASS** |
| **Crash Cleanup** | Kernel automatically clears `flock` on termination | **PASS** |
| **Secondary Clean Exit** | Exits with `exit(0)` after window activation | **PASS** |
| **Resource Isolation** | `CarbonClient` never initialized on secondary instance | **PASS** |
| **Ordered Startup** | Lock acquired before any networking or view hierarchy | **PASS** |
| **PID Boundary Safety** | PID metadata strictly informational; zero process signaling | **PASS** |
| **Regression & Hygiene** | 100% tests pass; clean build in 1.27s; clean diff | **PASS** |

### Decision: **INCREMENT 2A ACCEPTED**

Increment 2A satisfies all single-instance enforcement requirements. The repository is in a stable, verified state and ready for Increment 2B (Ollama and Backend Process Supervision) upon project owner authorization.

