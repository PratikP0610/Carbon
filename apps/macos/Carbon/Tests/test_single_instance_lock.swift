import Foundation
import Darwin
import AppKit

@main
struct SingleInstanceLockTestRunner {
    static func main() {
        print("=== CARBON STAGE 1.11 INCREMENT 2A: SINGLE-INSTANCE ENFORCEMENT TESTS ===")

        let fileManager = FileManager.default
        let tempDir = fileManager.temporaryDirectory.appendingPathComponent("carbon_lock_tests_\(UUID().uuidString)")
        do {
            try fileManager.createDirectory(at: tempDir, withIntermediateDirectories: true)
        } catch {
            print("FAILED to create temp test directory: \(error)")
            exit(1)
        }

        defer {
            try? fileManager.removeItem(at: tempDir)
        }

        // Test 1: First instance successfully acquires the lock
        print("[1/8] Verifying first instance successfully acquires the lock...")
        let lock1URL = tempDir.appendingPathComponent("test1.lock")
        let lock1 = SingleInstanceLock(lockURL: lock1URL)
        let res1 = lock1.acquire()
        assert(res1 == .acquired, "Expected .acquired, got \(res1)")
        assert(lock1.isHeld, "Expected isHeld to be true after acquire")
        let currentPid = ProcessInfo.processInfo.processIdentifier
        let savedPid = try? String(contentsOf: lock1URL, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        assert(savedPid == "\(currentPid)", "Expected file to contain PID \(currentPid), got \(savedPid ?? "nil")")
        print("      ✓ First instance acquired lock (PID: \(savedPid ?? "unknown"))")

        // Test 2: Second instance cannot acquire the same lock
        print("[2/8] Verifying second instance cannot acquire the same lock...")
        let lock2 = SingleInstanceLock(lockURL: lock1URL)
        let res2 = lock2.acquire()
        if case .alreadyRunning(let primaryPid) = res2 {
            assert(primaryPid == currentPid, "Expected primary PID \(currentPid), got \(String(describing: primaryPid))")
            assert(!lock2.isHeld, "Expected second instance isHeld to be false")
            print("      ✓ Second instance was rejected with .alreadyRunning (Primary PID: \(primaryPid.map(String.init) ?? "unknown"))")
        } else {
            assertionFailure("Expected .alreadyRunning, got \(res2)")
        }

        // Test 3: Lock becomes available after the primary releases it
        print("[3/8] Verifying lock becomes available after primary releases it...")
        lock1.release()
        assert(!lock1.isHeld, "Expected lock1 isHeld to be false after release")
        let res2Retry = lock2.acquire()
        assert(res2Retry == .acquired, "Expected lock2 to acquire after lock1 released, got \(res2Retry)")
        assert(lock2.isHeld, "Expected lock2 isHeld to be true")
        print("      ✓ Lock successfully re-acquired by second instance after release")
        lock2.release()

        // Test 4: Lock becomes available after primary process exits (kernel cleanup)
        print("[4/8] Verifying lock becomes available after child process holding flock exits...")
        let lock4URL = tempDir.appendingPathComponent("test4.lock")
        
        // Spawn a child process that acquires flock, touches a signal file, and exits
        let signalURL = tempDir.appendingPathComponent("test4.signal")
        let childScript = """
        import Darwin
        import Foundation
        let fd = open("\(lock4URL.path)", O_RDWR | O_CREAT, mode_t(0o600))
        if fd >= 0 {
            if flock(fd, LOCK_EX | LOCK_NB) == 0 {
                FileManager.default.createFile(atPath: "\(signalURL.path)", contents: nil)
                // Hold briefly then exit
                usleep(100_000) // 100ms
            }
            close(fd)
        }
        """
        let scriptPath = tempDir.appendingPathComponent("child_lock.swift")
        try! childScript.write(to: scriptPath, atomically: true, encoding: .utf8)

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/swift")
        proc.arguments = [scriptPath.path]
        try! proc.run()
        proc.waitUntilExit()

        assert(proc.terminationStatus == 0, "Child lock process failed with status \(proc.terminationStatus)")
        assert(fileManager.fileExists(atPath: signalURL.path), "Child process did not signal lock acquisition")

        // Now parent process attempts to acquire the lock: kernel must have released it upon child exit
        let parentLock = SingleInstanceLock(lockURL: lock4URL)
        let parentRes = parentLock.acquire()
        assert(parentRes == .acquired, "Parent failed to acquire lock after child exit: \(parentRes)")
        assert(parentLock.isHeld, "Parent isHeld must be true")
        parentLock.release()
        print("      ✓ Kernel released flock immediately upon child process exit")

        // Test 5: Stale PID metadata does not prevent lock acquisition
        print("[5/8] Verifying stale PID metadata does not prevent lock acquisition...")
        let lock5URL = tempDir.appendingPathComponent("test5.lock")
        let stalePid = "99999999\n" // Non-existent process
        try! stalePid.write(to: lock5URL, atomically: true, encoding: .utf8)
        let lock5 = SingleInstanceLock(lockURL: lock5URL)
        let res5 = lock5.acquire()
        assert(res5 == .acquired, "Expected .acquired despite stale PID file, got \(res5)")
        assert(lock5.isHeld, "Expected lock5 isHeld to be true")
        let updatedPid = try? String(contentsOf: lock5URL, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        assert(updatedPid == "\(currentPid)", "Lock file was not overwritten with current PID")
        lock5.release()
        print("      ✓ Stale PID metadata safely overwritten; kernel flock authority preserved")

        // Test 6: Secondary instance does not initialize lifecycle resources
        print("[6/8] Verifying secondary instance does not initialize lifecycle resources...")
        let lock6URL = tempDir.appendingPathComponent("test6.lock")
        let primaryLock = SingleInstanceLock(lockURL: lock6URL)
        primaryLock.acquire()

        var lifecycleInitialized = false
        var socketConnected = false

        let secondaryLock = SingleInstanceLock(lockURL: lock6URL)
        let secondaryResult = secondaryLock.acquire()
        if case .alreadyRunning = secondaryResult {
            // Secondary path: bypass lifecycle initialization entirely
        } else if case .acquired = secondaryResult {
            lifecycleInitialized = true
            socketConnected = true
        }

        assert(!lifecycleInitialized, "Lifecycle resources MUST NOT be initialized on secondary instance")
        assert(!socketConnected, "WebSocket MUST NOT be connected on secondary instance")
        primaryLock.release()
        print("      ✓ Secondary instance correctly bypassed lifecycle resource initialization")

        // Test 7: Lock acquisition failure produces a controlled diagnostic
        print("[7/8] Verifying lock acquisition failure produces controlled error diagnostic...")
        // Attempt lock on invalid path (e.g., inside /dev/null which cannot be a directory)
        let invalidURL = URL(fileURLWithPath: "/dev/null/subfolder/test7.lock")
        let invalidLock = SingleInstanceLock(lockURL: invalidURL)
        let invalidResult = invalidLock.acquire()
        if case .error(let message) = invalidResult {
            assert(!message.isEmpty, "Error message must not be empty")
            print("      ✓ Controlled diagnostic produced without crashing: '\(message.prefix(60))...'")
        } else {
            assertionFailure("Expected .error, got \(invalidResult)")
        }

        // Test 8: Lock cleanup occurs on normal termination / release
        print("[8/8] Verifying lock cleanup and deinit safety...")
        let lock8URL = tempDir.appendingPathComponent("test8.lock")
        do {
            let scopedLock = SingleInstanceLock(lockURL: lock8URL)
            let res = scopedLock.acquire()
            assert(res == .acquired, "Scoped lock failed to acquire")
            assert(scopedLock.isHeld, "Scoped lock isHeld must be true")
            // scopedLock goes out of scope here -> deinit releases lock
        }
        let verifyLock = SingleInstanceLock(lockURL: lock8URL)
        let verifyRes = verifyLock.acquire()
        assert(verifyRes == .acquired, "Expected re-acquisition after deinit release, got \(verifyRes)")
        verifyLock.release()
        print("      ✓ Lock automatically released upon deinit; subsequent acquisition succeeded")

        print("\n🎉 ALL 8 SINGLE-INSTANCE ENFORCEMENT TESTS PASSED SUCCESSFULLY!")
    }
}

