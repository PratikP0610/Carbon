import Foundation
import AppKit
import Darwin

public enum LockResult: Equatable {
    case acquired
    case alreadyRunning(primaryPid: pid_t?)
    case error(String)
}

public final class SingleInstanceLock: @unchecked Sendable {
    public static let shared = SingleInstanceLock()

    public let lockURL: URL
    private var lockFileDescriptor: Int32?
    private let lockQueue = DispatchQueue(label: "ai.carbon.singleinstancelock")

    public var isHeld: Bool {
        lockQueue.sync {
            lockFileDescriptor != nil
        }
    }

    public init(lockURL: URL? = nil) {
        if let lockURL = lockURL {
            self.lockURL = lockURL
        } else {
            self.lockURL = Self.resolveDefaultLockURL()
        }
    }

    public static func resolveDefaultLockURL() -> URL {
        if let envPath = ProcessInfo.processInfo.environment["CARBON_LOCK_PATH"], !envPath.isEmpty {
            return URL(fileURLWithPath: envPath)
        }
        let currentDir = FileManager.default.currentDirectoryPath
        let relativeDataDir = URL(fileURLWithPath: currentDir).appendingPathComponent("data")
        var isDir: ObjCBool = false
        if FileManager.default.fileExists(atPath: relativeDataDir.path, isDirectory: &isDir) && isDir.boolValue {
            return relativeDataDir.appendingPathComponent(".carbon_app.lock")
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home.appendingPathComponent("Carbon/data/.carbon_app.lock")
    }

    @discardableResult
    public func acquire() -> LockResult {
        lockQueue.sync {
            if lockFileDescriptor != nil {
                return .acquired
            }

            let parentDir = lockURL.deletingLastPathComponent()
            do {
                try FileManager.default.createDirectory(
                    at: parentDir,
                    withIntermediateDirectories: true,
                    attributes: [.posixPermissions: 0o700]
                )
            } catch {
                return .error("Failed to create lock parent directory at \(parentDir.path): \(error.localizedDescription)")
            }

            let path = lockURL.path
            let fd = open(path, O_RDWR | O_CREAT, mode_t(0o600))
            if fd < 0 {
                let err = errno
                return .error("Failed to open lock file at \(path): errno \(err) (\(String(cString: strerror(err))))")
            }

            // Apply kernel-level non-blocking exclusive lock
            let result = flock(fd, LOCK_EX | LOCK_NB)
            if result == 0 {
                // Primary instance successfully acquired the kernel lock
                self.lockFileDescriptor = fd

                // Record current PID as metadata (informational only for secondary instance activation)
                ftruncate(fd, 0)
                let pidString = "\(ProcessInfo.processInfo.processIdentifier)\n"
                _ = pidString.withCString { ptr in
                    write(fd, ptr, strlen(ptr))
                }
                return .acquired
            } else {
                let err = errno
                close(fd)

                if err == EWOULDBLOCK || err == EAGAIN {
                    let pid = readPidFromFile(at: lockURL)
                    return .alreadyRunning(primaryPid: pid)
                } else {
                    return .error("flock failed on \(path): errno \(err) (\(String(cString: strerror(err))))")
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

    private func readPidFromFile(at url: URL) -> pid_t? {
        guard let data = try? Data(contentsOf: url),
              let str = String(data: data, encoding: .utf8) else {
            return nil
        }
        let trimmed = str.trimmingCharacters(in: .whitespacesAndNewlines)
        return pid_t(trimmed)
    }

    public func activatePrimaryInstance(primaryPid: pid_t?) {
        // Tier 1: Search by bundle identifier
        if let bundleId = Bundle.main.bundleIdentifier, !bundleId.isEmpty {
            let apps = NSRunningApplication.runningApplications(withBundleIdentifier: bundleId)
            let currentPid = ProcessInfo.processInfo.processIdentifier
            if let primaryApp = apps.first(where: { $0.processIdentifier != currentPid }) {
                primaryApp.activate(options: [.activateAllWindows])
                fputs("[Carbon] Activated existing instance via bundle identifier '\(bundleId)' (PID: \(primaryApp.processIdentifier)).\n", stderr)
                return
            }
        }

        // Tier 2: Search by PID from lock metadata (for development / swift run)
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

    public func acquireOrActivateAndExit() {
        let result = acquire()
        switch result {
        case .acquired:
            break
        case .alreadyRunning(let primaryPid):
            activatePrimaryInstance(primaryPid: primaryPid)
            exit(0)
        case .error(let message):
            fputs("[Carbon] Warning: Failed to acquire single-instance lock (\(message)). Continuing startup.\n", stderr)
        }
    }

    deinit {
        release()
    }
}
