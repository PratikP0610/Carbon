# Carbon 1.0.0 — Stage 1.11 Increment 2A Report
## Single-Instance Enforcement & Kernel Lock Governance

**Task ID:** C-1.11-P2-2A  
**Scope:** Single-Instance Enforcement  
**Mode:** Controlled Implementation and Testing  
**Date:** November 2, 2026  
**Platform:** MacBook Air M5, 16GB unified memory, macOS 27.0.1  
**Target Repository:** `PratikP0610/Carbon`  
**Active Branch:** `backup/carbon-tested-phase1-2026-10-29`  
**HEAD Commit:** `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`  
**Status:** IMPLEMENTATION COMPLETE & VERIFIED (Increment 2A Approved Scope Only)

---

## 1. Executive Summary

In accordance with the Stage 1.11 Phase 2 implementation plan and project owner authorization, **Increment 2A (Single-Instance Enforcement)** has been implemented and verified in the macOS Swift application (`apps/macos/Carbon`).

### Core Objectives Realized
1. **Kernel-Level Lock Authority:** Implemented `SingleInstanceLock.swift` utilizing POSIX `open()` and `flock(fd, LOCK_EX | LOCK_NB)` on `data/.carbon_app.lock`. The kernel lock is the sole authority; file existence is never treated as evidence of a running instance.
2. **Immediate Secondary Instance Exit:** When lock acquisition fails with `EWOULDBLOCK` or `EAGAIN`, the secondary instance activates the primary instance window via `NSRunningApplication`, logs an informative diagnostic, and immediately terminates with `exit(0)`.
3. **Strict Resource Isolation Invariant:** The single-instance check executes in `CarbonApp.init()` before `CarbonClient` initialization. Secondary instances are guaranteed to never initialize `CarbonClient`, never open WebSockets, and never spawn background tasks.
4. **Crash & Exit Resilience:** The OS kernel automatically releases `flock` upon process termination or crash. Stale PID metadata remaining on disk after an abnormal termination does not hinder subsequent launches.
5. **Comprehensive Verification:** All 8 unit tests in `apps/macos/Carbon/Tests/test_single_instance_lock.swift` passed 100%. An end-to-end binary execution test confirmed the compiled `Carbon` executable exits with code 0 when an active lock is present.

---

## 2. Files Inspected and Modified

| File | Status | Description |
|---|:---:|---|
| `apps/macos/Carbon/Sources/SingleInstanceLock.swift` | **Created** | Encapsulates kernel `flock` acquisition, PID metadata writing, multi-tier `NSRunningApplication` window activation, and descriptor release. |
| `apps/macos/Carbon/Sources/CarbonApp.swift` | **Modified** | Extends `CarbonApp.init()` to acquire lock prior to `CarbonClient` instantiation; adds `applicationWillTerminate` in `AppDelegate` to release lock. |
| `apps/macos/Carbon/Tests/test_single_instance_lock.swift` | **Created** | Comprehensive standalone test suite verifying 8 discrete single-instance lifecycle conditions. |
| `docs/reports/stage1_11-increment-2A.md` | **Created** | Engineering delivery report and verification documentation. |

---

## 3. Implementation Details

### 3.1 Kernel Lock Architecture (`SingleInstanceLock.swift`)
```swift
public final class SingleInstanceLock: @unchecked Sendable {
    public static let shared = SingleInstanceLock()
    public let lockURL: URL
    private var lockFileDescriptor: Int32?
    private let lockQueue = DispatchQueue(label: "ai.carbon.singleinstancelock")

    @discardableResult
    public func acquire() -> LockResult {
        lockQueue.sync {
            if lockFileDescriptor != nil { return .acquired }

            let parentDir = lockURL.deletingLastPathComponent()
            try? FileManager.default.createDirectory(at: parentDir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])

            let fd = open(lockURL.path, O_RDWR | O_CREAT, mode_t(0o600))
            guard fd >= 0 else {
                return .error("Failed to open lock file: errno \(errno)")
            }

            // Apply non-blocking exclusive lock
            let result = flock(fd, LOCK_EX | LOCK_NB)
            if result == 0 {
                self.lockFileDescriptor = fd
                ftruncate(fd, 0)
                let pidString = "\(ProcessInfo.processInfo.processIdentifier)\n"
                _ = pidString.withCString { write(fd, $0, strlen($0)) }
                return .acquired
            } else {
                let err = errno
                close(fd)
                if err == EWOULDBLOCK || err == EAGAIN {
                    let pid = readPidFromFile(at: lockURL)
                    return .alreadyRunning(primaryPid: pid)
                } else {
                    return .error("flock failed with errno \(err)")
                }
            }
        }
    }

    public func release() {
        lockQueue.sync {
            guard let fd = lockFileDescriptor else { return }
            flock(fd, LOCK_UN)
            close(fd)
            self.lockFileDescriptor = nil
        }
    }
}
```

### 3.2 Multi-Tier Activation & Clean Secondary Exit
When `acquire()` returns `.alreadyRunning(primaryPid:)`:
1. **Tier 1 (Packaged `.app`):** Searches `NSRunningApplication.runningApplications(withBundleIdentifier:)` matching `ai.carbon.Carbon` and invokes `primaryApp.activate(options: [.activateAllWindows])`.
2. **Tier 2 (CLI / `swift run`):** Queries `NSRunningApplication(processIdentifier: pid)` using the primary PID metadata and calls `app.activate(...)`.
3. **Tier 3 (Diagnostic Fallback):** Outputs `[Carbon] Another instance is already running (PID: ...). Exiting secondary instance.` to stderr.
4. **Immediate Exit:** Calls `exit(0)`, terminating the secondary process before SwiftUI view creation or network initialization.

### 3.3 Application Integration (`CarbonApp.swift`)
```swift
final class AppDelegate: NSObject, NSApplicationDelegate {
    ...
    func applicationWillTerminate(_ notification: Notification) {
        SingleInstanceLock.shared.release()
    }
}

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
    ...
}
```

---

## 4. Test Evidence & Verification Results

### 4.1 Standalone Swift Test Suite (`test_single_instance_lock.swift`)
Executed via:
```bash
swiftc -parse-as-library apps/macos/Carbon/Sources/SingleInstanceLock.swift \
  apps/macos/Carbon/Tests/test_single_instance_lock.swift -o /tmp/test_single_instance_lock && /tmp/test_single_instance_lock
```

**Output:**
```text
=== CARBON STAGE 1.11 INCREMENT 2A: SINGLE-INSTANCE ENFORCEMENT TESTS ===
[1/8] Verifying first instance successfully acquires the lock...
      ✓ First instance acquired lock (PID: 22527)
[2/8] Verifying second instance cannot acquire the same lock...
      ✓ Second instance was rejected with .alreadyRunning (Primary PID: 22527)
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

### 4.2 Compiled Binary Integration Test
Verified with an active background lock:
```python
fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.write(fd, b'12345\n')

res = subprocess.run(['apps/macos/Carbon/.build/debug/Carbon'], env={'CARBON_LOCK_PATH': lock_file}, capture_output=True, text=True)
```
**Observed Result:**
```text
Secondary exit code: 0
Secondary stderr: [Carbon] Another instance is already running (PID: 12345). Exiting secondary instance.
```
Secondary process terminated cleanly with exit code 0 without allocating UI or network resources.

### 4.3 Regression Testing
1. **Swift Build:** `swift build --package-path apps/macos/Carbon` completed with code 0 in 1.13s.
2. **Swift Regression Suite:** `test_markdown.swift` passed 100% of all typography, Markdown, rendering, and reasoning tests.
3. **Linter Check:** `ruff check src/ tests/` $\to$ All checks passed!
4. **Git Formatting:** `git diff --check` $\to$ Clean (0 whitespace/formatting errors).

---

## 5. Scope Boundary Compliance

| Directive Restriction | Verification |
|---|:---:|
| No Ollama process management implemented | **CONFIRMED** |
| No Python backend process management implemented | **CONFIRMED** |
| No `ProcessSupervisor.swift` created | **CONFIRMED** |
| No shutdown watchdog implemented | **CONFIRMED** |
| No unrelated UI or chat modifications | **CONFIRMED** |
| No external dependencies added | **CONFIRMED** |
| No Git commits or branch switches performed | **CONFIRMED** |
| Stopped cleanly after Increment 2A completion | **CONFIRMED** |

Increment 2A implementation is complete and ready for project owner review.

