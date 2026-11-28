import Foundation

public enum ProcessOwnership: Equatable, Sendable {
    case notRunning
    case external
    case owned
}

public enum StartupState: String, Equatable, Sendable {
    case idle
    case checkingOllama
    case startingOllama
    case verifyingModel
    case checkingBackend
    case startingBackend
    case waitingForBackend
    case connecting
    case ready
    case failed
}

@MainActor
public protocol CarbonClientConnecting: AnyObject {
    var isConnected: Bool { get }
    var lastAuthError: String? { get }
    var lastConnectionError: String? { get }
    var autoReconnectEnabled: Bool { get set }
    func resolveAuthToken() -> String?
    func connectWebSocket()
    func disconnectWebSocket()
    func startMonitoring()
    func cancelActiveInferenceAndWait(timeout: TimeInterval) async
    func closeWebSocketForShutdown(timeout: TimeInterval) async
    func resetShutdownStateAfterCancelledQuit()
}

public struct ShutdownTimeoutConfig: Equatable, Sendable {
    public let inferenceCancel: TimeInterval
    public let webSocketClose: TimeInterval
    public let backendShutdown: TimeInterval
    public let ollamaShutdown: TimeInterval
    public let lockCleanup: TimeInterval
    public let globalWatchdog: TimeInterval

    public static let standard = ShutdownTimeoutConfig(
        inferenceCancel: 1.2,
        webSocketClose: 0.3,
        backendShutdown: 3.0,
        ollamaShutdown: 2.0,
        lockCleanup: 0.3,
        globalWatchdog: 8.0
    )

    public init(
        inferenceCancel: TimeInterval = 1.2,
        webSocketClose: TimeInterval = 0.3,
        backendShutdown: TimeInterval = 3.0,
        ollamaShutdown: TimeInterval = 2.0,
        lockCleanup: TimeInterval = 0.3,
        globalWatchdog: TimeInterval = 8.0
    ) {
        self.inferenceCancel = inferenceCancel
        self.webSocketClose = webSocketClose
        self.backendShutdown = backendShutdown
        self.ollamaShutdown = ollamaShutdown
        self.lockCleanup = lockCleanup
        self.globalWatchdog = globalWatchdog
    }
}

public enum ShutdownState: String, Equatable, Sendable {
    case idle
    case shuttingDown
    case completed
    case failed
}

public enum OwnedProcessShutdownOutcome: Equatable, Sendable {
    case notApplicable
    case preservedExternal
    case exited(terminationStatus: Int32)
    case timedOut
}

public struct ShutdownReport: Equatable, Sendable {
    public let canTerminate: Bool
    public let inferenceCancelled: Bool
    public let webSocketClosed: Bool
    public let backendHttpRequested: Bool
    public let backendOutcome: OwnedProcessShutdownOutcome
    public let ollamaOutcome: OwnedProcessShutdownOutcome
    public let lockReleased: Bool
    public let timedOutByWatchdog: Bool
    public let failureMessage: String?
}

public enum ProcessSupervisorError: Error, LocalizedError, Equatable {
    case ollamaExecutableNotFound
    case ollamaStartupFailed(String)
    case ollamaTimeout
    case modelNotFound(String)
    case pythonExecutableNotFound(String)
    case backendStartupFailed(String)
    case backendHealthDegraded(String)
    case backendTimeout
    case authenticationFailed(String)
    case webSocketConnectionFailed(String)
    case startupCancelled

    public var errorDescription: String? {
        switch self {
        case .ollamaExecutableNotFound:
            return "Ollama executable not found in standard paths (/opt/homebrew/bin/ollama, /usr/local/bin/ollama, /Applications/Ollama.app/Contents/Resources/ollama). Please install Ollama from https://ollama.ai."
        case .ollamaStartupFailed(let msg):
            return "Failed to launch Ollama engine: \(msg)"
        case .ollamaTimeout:
            return "Timed out waiting for Ollama engine to become ready."
        case .modelNotFound(let model):
            return "Model '\(model)' is not installed. Run: ollama pull \(model)"
        case .pythonExecutableNotFound(let path):
            return "Python environment not found at \(path). Please ensure .venv is configured."
        case .backendStartupFailed(let msg):
            return "Failed to launch Carbon backend: \(msg)"
        case .backendHealthDegraded(let msg):
            return "Carbon backend is running but reported degraded health: \(msg)"
        case .backendTimeout:
            return "Timed out waiting for Carbon backend to report healthy."
        case .authenticationFailed(let msg):
            return "Local authentication failed: \(msg)"
        case .webSocketConnectionFailed(let msg):
            return "Failed to connect to Carbon backend WebSocket: \(msg)"
        case .startupCancelled:
            return "Carbon startup was cancelled."
        }
    }

    public var recoverySuggestion: String? {
        switch self {
        case .ollamaExecutableNotFound:
            return "Install Ollama from https://ollama.ai (or ensure 'ollama' is in PATH), then click Retry."
        case .ollamaStartupFailed:
            return "Verify Ollama can run via 'ollama serve' in Terminal or launch Ollama.app, then click Retry."
        case .ollamaTimeout:
            return "Ensure port 11434 is free or Ollama is healthy, then click Retry."
        case .modelNotFound(let model):
            return "Open Terminal and run: ollama pull \(model)\nCarbon does not download models automatically. Click Retry once complete."
        case .pythonExecutableNotFound:
            return "Ensure the Carbon Python virtual environment (.venv) exists in the project root, then click Retry."
        case .backendStartupFailed:
            return "Verify Python dependencies in .venv and project files are intact, then click Retry."
        case .backendHealthDegraded:
            return "Check that port 8765 is running a healthy Carbon backend with '\( "qwen3.5:9b" )', then click Retry."
        case .backendTimeout:
            return "Verify port 8765 is not blocked by another process and click Retry."
        case .authenticationFailed:
            return "Verify data/.carbon_token exists or CARBON_API_TOKEN matches the running backend, then click Retry."
        case .webSocketConnectionFailed:
            return "Confirm the Carbon backend is reachable at http://127.0.0.1:8765 and click Retry."
        case .startupCancelled:
            return "Click Retry when you are ready to launch Carbon."
        }
    }
}

// MARK: - Pipe Drainer

public final class PipeDrainer: @unchecked Sendable {
    private let handle: FileHandle
    private let lock = NSLock()
    private var isClosed = false
    public private(set) var hasHandlerAttached: Bool = true
    public private(set) var totalBytesDrained: Int = 0

    public init(handle: FileHandle) {
        self.handle = handle
        handle.readabilityHandler = { [weak self] h in
            self?.readAvailableData(from: h)
        }
    }

    private func readAvailableData(from h: FileHandle) {
        // Read available data outside lock to avoid blocking during I/O
        let data = h.availableData

        lock.lock()
        defer { lock.unlock() }
        guard !isClosed else { return }

        if data.isEmpty {
            // EOF reached: detach readabilityHandler and close descriptor safely
            isClosed = true
            hasHandlerAttached = false
            h.readabilityHandler = nil
            try? h.close()
        } else {
            totalBytesDrained += data.count
            // Discard data to prevent unbounded memory growth.
            // Never print or log raw output to prevent leaking authentication tokens.
        }
    }

    public func close() {
        lock.lock()
        defer { lock.unlock() }
        guard !isClosed else { return }
        isClosed = true
        hasHandlerAttached = false
        handle.readabilityHandler = nil
        try? handle.close()
    }
}

// MARK: - Process Execution Protocols & Wrappers

public enum ProcessTerminationResult: Equatable, Sendable {
    case exited(terminationStatus: Int32)
    case timedOut
}

public protocol ProcessRunning: AnyObject, Sendable {
    var isRunning: Bool { get }
    var processIdentifier: Int32 { get }
    var terminationHandler: (@Sendable (any ProcessRunning) -> Void)? { get set }
    func terminate()
    @discardableResult
    func terminateAndAwaitExit(timeout: TimeInterval) async -> ProcessTerminationResult
    @discardableResult
    func awaitExit(timeout: TimeInterval) async -> ProcessTerminationResult
}

public final class ManagedProcess: ProcessRunning, @unchecked Sendable {
    public let process: Process
    public let stdoutDrainer: PipeDrainer?
    public let stderrDrainer: PipeDrainer?
    private let cleanupLock = NSLock()
    private var isCleanedUp = false

    public init(
        process: Process,
        stdoutDrainer: PipeDrainer? = nil,
        stderrDrainer: PipeDrainer? = nil
    ) {
        self.process = process
        self.stdoutDrainer = stdoutDrainer
        self.stderrDrainer = stderrDrainer
    }

    public var isRunning: Bool { process.isRunning }
    public var processIdentifier: Int32 { process.processIdentifier }

    public var terminationHandler: (@Sendable (any ProcessRunning) -> Void)? {
        didSet {
            process.terminationHandler = { [weak self] _ in
                guard let self = self else { return }
                self.cleanupPipes()
                self.terminationHandler?(self)
            }
        }
    }

    public func terminate() {
        process.terminate()
        if !isRunning {
            cleanupPipes()
        }
    }

    @discardableResult
    public func awaitExit(timeout: TimeInterval = 2.0) async -> ProcessTerminationResult {
        guard isRunning else {
            cleanupPipes()
            return .exited(terminationStatus: process.terminationStatus)
        }

        let deadline = Date().addingTimeInterval(timeout)
        while isRunning && Date() < deadline && !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 10_000_000) // 10ms
        }

        if !isRunning {
            cleanupPipes()
            return .exited(terminationStatus: process.terminationStatus)
        } else {
            // Process did NOT exit within deadline (or wait was cancelled): do NOT close pipes while still running
            return .timedOut
        }
    }

    @discardableResult
    public func terminateAndAwaitExit(timeout: TimeInterval = 2.0) async -> ProcessTerminationResult {
        guard isRunning else {
            cleanupPipes()
            return .exited(terminationStatus: process.terminationStatus)
        }

        // Request graceful exit via SIGTERM
        process.terminate()

        let deadline = Date().addingTimeInterval(timeout)
        while isRunning && Date() < deadline && !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 10_000_000) // 10ms
        }

        if !isRunning {
            // Process confirmed exited: safe to detach handlers and close pipes
            cleanupPipes()
            return .exited(terminationStatus: process.terminationStatus)
        } else {
            // Process did NOT exit within deadline (or wait was cancelled): do NOT close pipes while still running
            return .timedOut
        }
    }

    public func cleanupPipes() {
        cleanupLock.lock()
        defer { cleanupLock.unlock() }
        guard !isCleanedUp else { return }
        isCleanedUp = true

        stdoutDrainer?.close()
        stderrDrainer?.close()
    }

    deinit {
        cleanupPipes()
    }
}

public protocol ProcessSupervisorProcessLauncher: Sendable {
    func launchProcess(
        executableURL: URL,
        arguments: [String],
        currentDirectoryURL: URL?,
        environment: [String: String]?
    ) throws -> any ProcessRunning
}

public struct DefaultProcessLauncher: ProcessSupervisorProcessLauncher {
    public init() {}

    public func launchProcess(
        executableURL: URL,
        arguments: [String],
        currentDirectoryURL: URL?,
        environment: [String: String]?
    ) throws -> any ProcessRunning {
        let proc = Process()
        proc.executableURL = executableURL
        proc.arguments = arguments
        proc.currentDirectoryURL = currentDirectoryURL
        if let env = environment {
            proc.environment = env
        }

        // Drain stdout and stderr asynchronously with genuine reads to prevent pipe buffer deadlocks
        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe

        let stdoutDrainer = PipeDrainer(handle: outPipe.fileHandleForReading)
        let stderrDrainer = PipeDrainer(handle: errPipe.fileHandleForReading)

        try proc.run()
        return ManagedProcess(
            process: proc,
            stdoutDrainer: stdoutDrainer,
            stderrDrainer: stderrDrainer
        )
    }
}

public protocol ProcessSupervisorHTTPClient: Sendable {
    func data(from url: URL) async throws -> (Data, HTTPURLResponse)
    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

public extension ProcessSupervisorHTTPClient {
    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        guard let url = request.url else {
            throw URLError(.badURL)
        }
        return try await data(from: url)
    }
}

public struct DefaultProcessSupervisorHTTPClient: ProcessSupervisorHTTPClient {
    private let session: URLSession

    public init(session: URLSession = .shared) {
        self.session = session
    }

    public func data(from url: URL) async throws -> (Data, HTTPURLResponse) {
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.0
        return try await send(request)
    }

    public func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await session.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        return (data, httpResponse)
    }
}

// MARK: - Process Supervisor

@MainActor
public final class ProcessSupervisor: ObservableObject {
    public nonisolated static let defaultModelLoadTimeout: TimeInterval = 60.0
    public nonisolated static let defaultBackendTimeout: TimeInterval = defaultModelLoadTimeout + 15.0

    @Published public private(set) var ollamaOwnership: ProcessOwnership = .notRunning
    @Published public private(set) var backendOwnership: ProcessOwnership = .notRunning

    @Published public private(set) var startupState: StartupState = .idle
    @Published public private(set) var startupStatusMessage: String = "Ready to initialize Carbon"
    @Published public private(set) var startupDetailMessage: String? = nil
    @Published public private(set) var startupError: ProcessSupervisorError? = nil
    @Published public private(set) var isOllamaReused: Bool = false
    @Published public private(set) var isBackendReused: Bool = false
    public private(set) var stateTransitionHistory: [StartupState] = []

    @Published public private(set) var shutdownState: ShutdownState = .idle
    @Published public private(set) var shutdownStatusMessage: String = ""
    @Published public private(set) var shutdownErrorMessage: String? = nil
    @Published public private(set) var isShuttingDown: Bool = false
    @Published public private(set) var hasCompletedShutdown: Bool = false
    @Published public private(set) var lastShutdownReport: ShutdownReport? = nil

    public var childOllamaProcess: (any ProcessRunning)? {
        get { _childOllamaProcess }
    }
    public var childBackendProcess: (any ProcessRunning)? {
        get { _childBackendProcess }
    }

    private var _childOllamaProcess: (any ProcessRunning)?
    private var _childBackendProcess: (any ProcessRunning)?

    public let projectRoot: URL
    public let ollamaBaseURL: URL
    public let backendBaseURL: URL
    public let requiredModel: String

    private let httpClient: any ProcessSupervisorHTTPClient
    private let processLauncher: any ProcessSupervisorProcessLauncher
    private let fileManager: FileManager

    // Serialized deduplication tasks for concurrent startup / shutdown requests
    private var startupTask: Task<Void, Error>?
    private var ollamaStartupTask: Task<Void, Error>?
    private var backendStartupTask: Task<Void, Error>?
    private var activeStartupRunId: UUID?
    private var activeStartupCleanupTask: Task<Void, Never>?
    private var attemptedStartupCleanupChildIds: (runId: UUID, ids: Set<ObjectIdentifier>)?
    public var hasActiveStartupCleanupTask: Bool { activeStartupCleanupTask != nil }
    private var shutdownTask: Task<ShutdownReport, Never>?
    private var activeShutdownWorkTask: Task<Void, Never>?
    private var activeShutdownRunId: UUID?
    public var hasActiveShutdownWorkTask: Bool { activeShutdownWorkTask != nil }
    public private(set) var shutdownWorkLaunchCount: Int = 0
    private var cancellationRequested: Bool = false
    private weak var activeClient: (any CarbonClientConnecting)?

    private func setState(_ newState: StartupState, message: String, detail: String? = nil) {
        startupState = newState
        startupStatusMessage = message
        startupDetailMessage = detail
        if stateTransitionHistory.last != newState {
            stateTransitionHistory.append(newState)
        }
    }

    public static let standardOllamaPaths = [
        "/opt/homebrew/bin/ollama",
        "/usr/local/bin/ollama",
        "/Applications/Ollama.app/Contents/Resources/ollama"
    ]

    public init(
        projectRoot: URL? = nil,
        ollamaBaseURL: URL = URL(string: "http://127.0.0.1:11434")!,
        backendBaseURL: URL = URL(string: "http://127.0.0.1:8765")!,
        requiredModel: String = "qwen3.5:9b",
        httpClient: (any ProcessSupervisorHTTPClient)? = nil,
        processLauncher: (any ProcessSupervisorProcessLauncher)? = nil,
        fileManager: FileManager = .default
    ) {
        self.projectRoot = projectRoot ?? Self.resolveDefaultProjectRoot()
        self.ollamaBaseURL = ollamaBaseURL
        self.backendBaseURL = backendBaseURL
        self.requiredModel = requiredModel
        self.httpClient = httpClient ?? DefaultProcessSupervisorHTTPClient()
        self.processLauncher = processLauncher ?? DefaultProcessLauncher()
        self.fileManager = fileManager
    }

    public static func resolveDefaultProjectRoot() -> URL {
        let currentDir = FileManager.default.currentDirectoryPath
        let currentURL = URL(fileURLWithPath: currentDir)
        if FileManager.default.fileExists(atPath: currentURL.appendingPathComponent("src/carbon/main.py").path) {
            return currentURL
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home.appendingPathComponent("Carbon")
    }

    // MARK: - Ollama Resolution & Discovery

    public func resolveOllamaExecutable(
        customSearchPaths: [String]? = nil,
        pathEnvironment: String? = nil
    ) -> URL? {
        let paths = customSearchPaths ?? Self.standardOllamaPaths

        // 1-3. Standard search paths in strict approved order
        for path in paths {
            if fileManager.isExecutableFile(atPath: path) {
                return URL(fileURLWithPath: path)
            }
        }

        // 4. PATH fallback (uses passed pathEnvironment, or system PATH if customSearchPaths is nil)
        let resolvedPathEnv = pathEnvironment ?? (customSearchPaths == nil ? ProcessInfo.processInfo.environment["PATH"] : nil)
        if let pathEnv = resolvedPathEnv {
            for dir in pathEnv.components(separatedBy: ":") where !dir.isEmpty {
                let candidate = (dir as NSString).appendingPathComponent("ollama")
                if fileManager.isExecutableFile(atPath: candidate) {
                    return URL(fileURLWithPath: candidate)
                }
            }
        }

        return nil
    }

    public enum OllamaProbeStatus: Equatable {
        case ready
        case starting
        case notResponding
    }

    public func probeOllamaStatus() async -> OllamaProbeStatus {
        let versionURL = ollamaBaseURL.appendingPathComponent("api/version")
        do {
            let (data, response) = try await httpClient.data(from: versionURL)
            if response.statusCode == 200,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               json["version"] != nil {
                return .ready
            }
            // HTTP server is actively responding on the Ollama port, but not yet ready
            return .starting
        } catch {
            return .notResponding
        }
    }

    public func isOllamaHealthy() async -> Bool {
        return await probeOllamaStatus() == .ready
    }

    private func ensureStartupRunId() -> UUID {
        if let existing = activeStartupRunId, startupTask != nil {
            return existing
        }
        let id = UUID()
        activeStartupRunId = id
        attemptedStartupCleanupChildIds = nil
        return id
    }

    /// Terminates a single Carbon-owned child during startup failure, timeout, or cancellation.
    /// Uses an unstructured @MainActor Task so `cancelStartup()` does not short-circuit
    /// bounded `terminateAndAwaitExit` exit polling via `Task.isCancelled`, while allowing
    /// coordinated app `shutdown()` to cancel the wait via `activeStartupCleanupTask`
    /// and preventing stale mutation across newer startup/shutdown runs.
    private func cleanupOwnedChildDuringStartup(
        isOllama: Bool,
        child: any ProcessRunning,
        runId: UUID,
        timeout: TimeInterval = 2.0
    ) async {
        guard !isShuttingDown && !hasCompletedShutdown else { return }
        guard activeStartupRunId == runId else { return }

        let isStillTrackedOwned: Bool = isOllama
            ? (ollamaOwnership == .owned && _childOllamaProcess === child)
            : (backendOwnership == .owned && _childBackendProcess === child)
        guard isStillTrackedOwned else { return }

        let childObjectId = ObjectIdentifier(child)
        if let record = attemptedStartupCleanupChildIds, record.runId == runId, record.ids.contains(childObjectId) {
            return
        }
        var updatedIds = (attemptedStartupCleanupChildIds?.runId == runId ? attemptedStartupCleanupChildIds?.ids : nil) ?? Set<ObjectIdentifier>()
        updatedIds.insert(childObjectId)
        attemptedStartupCleanupChildIds = (runId, updatedIds)

        let cleanupTask = Task<Void, Never> { @MainActor [weak self] in
            defer {
                if let self = self, self.activeStartupRunId == runId {
                    self.activeStartupCleanupTask = nil
                }
            }
            let res = await child.terminateAndAwaitExit(timeout: timeout)
            guard let self = self,
                  self.activeStartupRunId == runId,
                  !self.isShuttingDown,
                  !self.hasCompletedShutdown else {
                return
            }
            if case .exited = res {
                if isOllama {
                    if self._childOllamaProcess === child && self.ollamaOwnership == .owned && !child.isRunning {
                        self._childOllamaProcess = nil
                        self.ollamaOwnership = .notRunning
                    }
                } else {
                    if self._childBackendProcess === child && self.backendOwnership == .owned && !child.isRunning {
                        self._childBackendProcess = nil
                        self.backendOwnership = .notRunning
                    }
                }
            }
        }
        self.activeStartupCleanupTask = cleanupTask
        await cleanupTask.value
    }

    private func cleanupAllOwnedChildrenDuringStartup(runId: UUID, timeout: TimeInterval = 2.0) async {
        if ollamaOwnership == .owned, let ollamaChild = _childOllamaProcess {
            await cleanupOwnedChildDuringStartup(isOllama: true, child: ollamaChild, runId: runId, timeout: timeout)
        }
        if backendOwnership == .owned, let backendChild = _childBackendProcess {
            await cleanupOwnedChildDuringStartup(isOllama: false, child: backendChild, runId: runId, timeout: timeout)
        }
    }

    public func ensureOllamaRunning(
        timeout: TimeInterval = 10.0,
        customSearchPaths: [String]? = nil,
        pathEnvironment: String? = nil
    ) async throws {
        if let existing = ollamaStartupTask {
            return try await existing.value
        }

        let task = Task { [weak self] () throws in
            guard let self = self else { return }
            try await self.performEnsureOllamaRunning(
                timeout: timeout,
                customSearchPaths: customSearchPaths,
                pathEnvironment: pathEnvironment
            )
        }
        self.ollamaStartupTask = task

        do {
            try await task.value
            self.ollamaStartupTask = nil
        } catch {
            self.ollamaStartupTask = nil
            throw error
        }
    }

    private func performEnsureOllamaRunning(
        timeout: TimeInterval,
        customSearchPaths: [String]?,
        pathEnvironment: String?
    ) async throws {
        try Task.checkCancellation()
        let runId = ensureStartupRunId()
        setState(
            .checkingOllama,
            message: "Checking local Ollama engine...",
            detail: "Probing \(ollamaBaseURL.absoluteString)/api/version"
        )

        // Step 0: If we already own an active running child that is starting up, await it
        if let staleChild = _childOllamaProcess, !staleChild.isRunning {
            _childOllamaProcess = nil
            ollamaOwnership = .notRunning
        }

        if let child = _childOllamaProcess, child.isRunning {
            setState(
                .startingOllama,
                message: "Waiting for Ollama engine...",
                detail: "Awaiting readiness of managed Ollama child process"
            )
            let deadline = Date().addingTimeInterval(timeout)
            do {
                while Date() < deadline {
                    try Task.checkCancellation()
                    if await isOllamaHealthy() {
                        return
                    }
                    try await Task.sleep(nanoseconds: 100_000_000)
                }
            } catch {
                await cleanupOwnedChildDuringStartup(isOllama: true, child: child, runId: runId, timeout: 2.0)
                throw error
            }
            await cleanupOwnedChildDuringStartup(isOllama: true, child: child, runId: runId, timeout: 2.0)
            throw ProcessSupervisorError.ollamaTimeout
        }

        // Step 1: Probe existing Ollama instance
        let initialStatus = await probeOllamaStatus()
        try Task.checkCancellation()

        switch initialStatus {
        case .ready:
            if ollamaOwnership != .owned {
                ollamaOwnership = .external
                isOllamaReused = true
            }
            setState(
                .checkingOllama,
                message: "Ollama engine ready",
                detail: "Reusing existing Ollama instance on \(ollamaBaseURL.absoluteString)"
            )
            return
        case .starting:
            // An external Ollama service is actively starting on this port.
            // Bounded wait for readiness to prevent launching a duplicate conflicting process.
            setState(
                .startingOllama,
                message: "Waiting for Ollama engine...",
                detail: "Existing Ollama service is starting on \(ollamaBaseURL.absoluteString)"
            )
            let deadline = Date().addingTimeInterval(timeout)
            while Date() < deadline {
                try Task.checkCancellation()
                if await isOllamaHealthy() {
                    if ollamaOwnership != .owned {
                        ollamaOwnership = .external
                        isOllamaReused = true
                    }
                    return
                }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            throw ProcessSupervisorError.ollamaTimeout
        case .notResponding:
            break // No service listening, proceed to locate binary and spawn
        }

        // Step 2: Locate executable
        guard let ollamaURL = resolveOllamaExecutable(customSearchPaths: customSearchPaths, pathEnvironment: pathEnvironment) else {
            throw ProcessSupervisorError.ollamaExecutableNotFound
        }

        // Invariant: Never spawn a process once shutdown has begun or completed
        guard !isShuttingDown && !hasCompletedShutdown else {
            throw ProcessSupervisorError.startupCancelled
        }

        // Step 3: Launch ollama serve
        setState(
            .startingOllama,
            message: "Starting local Ollama engine...",
            detail: "Launching '\(ollamaURL.path) serve' on 127.0.0.1:11434"
        )
        do {
            let proc = try processLauncher.launchProcess(
                executableURL: ollamaURL,
                arguments: ["serve"],
                currentDirectoryURL: nil,
                environment: ProcessInfo.processInfo.environment
            )
            self._childOllamaProcess = proc
            self.ollamaOwnership = .owned
            self.isOllamaReused = false

            proc.terminationHandler = { [weak self, weak proc] _ in
                Task { @MainActor [weak self, weak proc] in
                    guard let self = self, let proc = proc else { return }
                    if self._childOllamaProcess === proc && self.ollamaOwnership == .owned {
                        self._childOllamaProcess = nil
                        self.ollamaOwnership = .notRunning
                    }
                }
            }
        } catch {
            throw ProcessSupervisorError.ollamaStartupFailed(error.localizedDescription)
        }

        // Step 4: Bounded polling until healthy
        let deadline = Date().addingTimeInterval(timeout)
        do {
            while Date() < deadline {
                try Task.checkCancellation()
                if await isOllamaHealthy() {
                    return
                }
                try await Task.sleep(nanoseconds: 100_000_000) // 100ms
            }
        } catch {
            if let child = _childOllamaProcess {
                await cleanupOwnedChildDuringStartup(isOllama: true, child: child, runId: runId, timeout: 2.0)
            }
            throw error
        }

        // Startup timed out: clean up only the owned child process and await exit
        if let child = _childOllamaProcess {
            await cleanupOwnedChildDuringStartup(isOllama: true, child: child, runId: runId, timeout: 2.0)
        }
        throw ProcessSupervisorError.ollamaTimeout
    }

    // MARK: - Model Verification

    public func verifyModelInstalled() async throws {
        try Task.checkCancellation()
        let tagsURL = ollamaBaseURL.appendingPathComponent("api/tags")
        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await httpClient.data(from: tagsURL)
        } catch {
            try Task.checkCancellation()
            throw ProcessSupervisorError.ollamaStartupFailed("Failed to reach Ollama tags API: \(error.localizedDescription)")
        }

        try Task.checkCancellation()

        guard response.statusCode == 200 else {
            throw ProcessSupervisorError.ollamaStartupFailed("Ollama tags API returned status \(response.statusCode)")
        }

        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let models = json["models"] as? [[String: Any]] else {
            throw ProcessSupervisorError.ollamaStartupFailed("Failed to parse Ollama model list")
        }

        let isModelPresent = models.contains { modelInfo in
            guard let name = modelInfo["name"] as? String else { return false }
            return name == requiredModel || name.hasPrefix("\(requiredModel):")
        }

        if !isModelPresent {
            // Strict Invariant: DO NOT download automatically
            throw ProcessSupervisorError.modelNotFound(requiredModel)
        }
    }

    // MARK: - Python Backend Discovery & Startup

    public enum BackendHealthResult: Equatable {
        case healthy
        case degraded(String)
        case unavailable
    }

    public func verifyBackendHealth() async -> BackendHealthResult {
        let healthURL = backendBaseURL.appendingPathComponent("api/v1/health")
        do {
            let (data, response) = try await httpClient.data(from: healthURL)
            guard response.statusCode == 200 else {
                return .degraded("HTTP status \(response.statusCode)")
            }
            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                return .degraded("Invalid JSON response")
            }

            let status = json["status"] as? String ?? "unknown"
            let coreRunning = json["core_running"] as? Bool ?? false
            let ollamaConnected = json["ollama_connected"] as? Bool ?? false
            let activeModelData = json["active_model"] as? [String: Any]
            let activeModelId = activeModelData?["id"] as? String ?? ""

            guard status == "healthy" else {
                return .degraded("status is '\(status)'")
            }
            guard coreRunning else {
                return .degraded("core_running is false")
            }
            guard ollamaConnected else {
                return .degraded("ollama_connected is false")
            }
            guard activeModelId == requiredModel || activeModelId.hasPrefix(requiredModel) else {
                return .degraded("active model '\(activeModelId)' does not match required '\(requiredModel)'")
            }

            return .healthy
        } catch {
            return .unavailable
        }
    }

    public func resolvePythonExecutable() -> URL? {
        if let envPython = ProcessInfo.processInfo.environment["CARBON_PYTHON_PATH"], !envPython.isEmpty {
            if fileManager.isExecutableFile(atPath: envPython) {
                return URL(fileURLWithPath: envPython)
            }
        }

        let venvPython = projectRoot.appendingPathComponent(".venv/bin/python")
        if fileManager.isExecutableFile(atPath: venvPython.path) {
            return venvPython
        }

        return nil
    }

    public func ensureBackendRunning(
        timeout: TimeInterval = ProcessSupervisor.defaultBackendTimeout
    ) async throws {
        if let existing = backendStartupTask {
            return try await existing.value
        }

        let task = Task { [weak self] () throws in
            guard let self = self else { return }
            try await self.performEnsureBackendRunning(timeout: timeout)
        }
        self.backendStartupTask = task

        do {
            try await task.value
            self.backendStartupTask = nil
        } catch {
            self.backendStartupTask = nil
            throw error
        }
    }

    private func performEnsureBackendRunning(timeout: TimeInterval) async throws {
        try Task.checkCancellation()
        let runId = ensureStartupRunId()
        setState(
            .checkingBackend,
            message: "Checking Carbon Python backend...",
            detail: "Probing \(backendBaseURL.absoluteString)/api/v1/health"
        )

        // Step 0: If we already own an active running child backend that is starting up, await it
        if let staleChild = _childBackendProcess, !staleChild.isRunning {
            _childBackendProcess = nil
            backendOwnership = .notRunning
        }

        if let child = _childBackendProcess, child.isRunning {
            setState(
                .waitingForBackend,
                message: "Waiting for Carbon backend health check...",
                detail: "Awaiting readiness of managed Python backend child"
            )
            let deadline = Date().addingTimeInterval(timeout)
            do {
                while Date() < deadline {
                    try Task.checkCancellation()
                    switch await verifyBackendHealth() {
                    case .healthy:
                        return
                    case .degraded(let reason):
                        await cleanupOwnedChildDuringStartup(isOllama: false, child: child, runId: runId, timeout: 2.0)
                        throw ProcessSupervisorError.backendHealthDegraded(reason)
                    case .unavailable:
                        try await Task.sleep(nanoseconds: 100_000_000)
                    }
                }
            } catch {
                if let activeChild = _childBackendProcess, activeChild === child {
                    await cleanupOwnedChildDuringStartup(isOllama: false, child: activeChild, runId: runId, timeout: 2.0)
                }
                throw error
            }
            await cleanupOwnedChildDuringStartup(isOllama: false, child: child, runId: runId, timeout: 2.0)
            throw ProcessSupervisorError.backendTimeout
        }

        // Step 1: Probe existing backend health
        let initialHealth = await verifyBackendHealth()
        try Task.checkCancellation()

        switch initialHealth {
        case .healthy:
            if backendOwnership != .owned {
                backendOwnership = .external
                isBackendReused = true
            }
            setState(
                .checkingBackend,
                message: "Carbon backend ready",
                detail: "Reusing existing Carbon backend on \(backendBaseURL.absoluteString)"
            )
            return
        case .degraded(let reason):
            throw ProcessSupervisorError.backendHealthDegraded(reason)
        case .unavailable:
            break // Backend is not running, proceed to launch
        }

        // Step 2: Locate Python virtual environment
        guard let pythonURL = resolvePythonExecutable() else {
            let expectedPath = projectRoot.appendingPathComponent(".venv/bin/python").path
            throw ProcessSupervisorError.pythonExecutableNotFound(expectedPath)
        }

        // Invariant: Never spawn a process once shutdown has begun or completed
        guard !isShuttingDown && !hasCompletedShutdown else {
            throw ProcessSupervisorError.startupCancelled
        }

        // Step 3: Launch python -m carbon.main
        setState(
            .startingBackend,
            message: "Starting Carbon Python backend...",
            detail: "Launching '.venv/bin/python -m carbon.main' on 127.0.0.1:8765"
        )
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = projectRoot.appendingPathComponent("src").path

        do {
            let proc = try processLauncher.launchProcess(
                executableURL: pythonURL,
                arguments: ["-m", "carbon.main"],
                currentDirectoryURL: projectRoot,
                environment: env
            )
            self._childBackendProcess = proc
            self.backendOwnership = .owned
            self.isBackendReused = false

            proc.terminationHandler = { [weak self, weak proc] _ in
                Task { @MainActor [weak self, weak proc] in
                    guard let self = self, let proc = proc else { return }
                    if self._childBackendProcess === proc && self.backendOwnership == .owned {
                        self._childBackendProcess = nil
                        self.backendOwnership = .notRunning
                    }
                }
            }
        } catch {
            throw ProcessSupervisorError.backendStartupFailed(error.localizedDescription)
        }

        // Step 4: Bounded polling until backend is healthy
        setState(
            .waitingForBackend,
            message: "Waiting for Carbon backend health check...",
            detail: "Verifying Carbon Core initialization and \(requiredModel) readiness"
        )
        let deadline = Date().addingTimeInterval(timeout)
        do {
            while Date() < deadline {
                try Task.checkCancellation()
                switch await verifyBackendHealth() {
                case .healthy:
                    return
                case .degraded(let reason):
                    // Degraded backend during startup: terminate owned child, await exit and report
                    if let child = _childBackendProcess {
                        await cleanupOwnedChildDuringStartup(isOllama: false, child: child, runId: runId, timeout: 2.0)
                    }
                    throw ProcessSupervisorError.backendHealthDegraded(reason)
                case .unavailable:
                    try await Task.sleep(nanoseconds: 100_000_000) // 100ms
                }
            }
        } catch {
            if let child = _childBackendProcess {
                await cleanupOwnedChildDuringStartup(isOllama: false, child: child, runId: runId, timeout: 2.0)
            }
            throw error
        }

        // Startup timed out: clean up only the owned child process and await exit
        if let child = _childBackendProcess {
            await cleanupOwnedChildDuringStartup(isOllama: false, child: child, runId: runId, timeout: 2.0)
        }
        throw ProcessSupervisorError.backendTimeout
    }

    // MARK: - Safe Concurrent Orchestration & State Machine

    public func startup() async throws {
        try await startup(client: nil)
    }

    public func startup(
        client: (any CarbonClientConnecting)?,
        ollamaTimeout: TimeInterval = 10.0,
        backendTimeout: TimeInterval = ProcessSupervisor.defaultBackendTimeout,
        connectTimeout: TimeInterval = 8.0,
        customOllamaSearchPaths: [String]? = nil
    ) async throws {
        // Prevent startup/retry operations after shutdown has started or finished
        guard !isShuttingDown && !hasCompletedShutdown else {
            throw ProcessSupervisorError.startupCancelled
        }

        if let existingTask = startupTask {
            if !cancellationRequested {
                return try await existingTask.value
            }
            // Wait for any cancelling startup cleanup to finish before retrying
            _ = try? await existingTask.value
        }

        if let cleanupTask = activeStartupCleanupTask {
            await cleanupTask.value
        }

        if startupState == .ready, (client == nil || client?.isConnected == true) {
            return
        }

        cancellationRequested = false
        activeClient = client
        let runId = UUID()
        self.activeStartupRunId = runId
        self.attemptedStartupCleanupChildIds = nil

        let task = Task { [weak self] () throws in
            guard let self = self else { return }
            try await self.performStartupSequence(
                runId: runId,
                client: client,
                ollamaTimeout: ollamaTimeout,
                backendTimeout: backendTimeout,
                connectTimeout: connectTimeout,
                customOllamaSearchPaths: customOllamaSearchPaths
            )
        }

        self.startupTask = task

        do {
            try await task.value
            if self.activeStartupRunId == runId {
                self.startupTask = nil
            }
        } catch {
            if self.activeStartupRunId == runId {
                self.startupTask = nil
            }
            throw error
        }
    }

    public func cancelStartup() {
        cancellationRequested = true
        startupTask?.cancel()
        ollamaStartupTask?.cancel()
        backendStartupTask?.cancel()
        activeClient?.disconnectWebSocket()
    }

    private func performStartupSequence(
        runId: UUID,
        client: (any CarbonClientConnecting)?,
        ollamaTimeout: TimeInterval,
        backendTimeout: TimeInterval,
        connectTimeout: TimeInterval,
        customOllamaSearchPaths: [String]?
    ) async throws {
        startupError = nil
        isOllamaReused = false
        isBackendReused = false
        stateTransitionHistory = []

        do {
            // Steps 2-4: Ensure Ollama engine is available (reuse external or spawn owned)
            try await ensureOllamaRunning(
                timeout: ollamaTimeout,
                customSearchPaths: customOllamaSearchPaths
            )
            try Task.checkCancellation()

            // Steps 5-6: Verify qwen3.5:9b installed (never pull automatically)
            setState(
                .verifyingModel,
                message: "Verifying model \(requiredModel)...",
                detail: "Checking installed models at \(ollamaBaseURL.absoluteString)/api/tags"
            )
            try await verifyModelInstalled()
            try Task.checkCancellation()

            // Steps 7-10: Ensure Carbon Python backend is healthy (reuse external or spawn owned)
            try await ensureBackendRunning(timeout: backendTimeout)
            try Task.checkCancellation()

            // Steps 11-12: Resolve local auth token and connect CarbonClient WebSocket
            if let client = client {
                setState(
                    .connecting,
                    message: "Connecting to Carbon backend...",
                    detail: "Resolving local authentication token and opening WebSocket"
                )

                guard let token = client.resolveAuthToken(), !token.isEmpty else {
                    throw ProcessSupervisorError.authenticationFailed(
                        "No local authentication token found in CARBON_API_TOKEN or data/.carbon_token."
                    )
                }

                client.autoReconnectEnabled = false
                client.connectWebSocket()

                let connectDeadline = Date().addingTimeInterval(connectTimeout)
                while Date() < connectDeadline {
                    try Task.checkCancellation()
                    if client.isConnected {
                        break
                    }
                    if let authErr = client.lastAuthError {
                        throw ProcessSupervisorError.authenticationFailed(authErr)
                    }
                    if let connErr = client.lastConnectionError {
                        throw ProcessSupervisorError.webSocketConnectionFailed(connErr)
                    }
                    try await Task.sleep(nanoseconds: 50_000_000) // 50ms
                }

                guard client.isConnected else {
                    client.disconnectWebSocket()
                    if let authErr = client.lastAuthError {
                        throw ProcessSupervisorError.authenticationFailed(authErr)
                    }
                    if let connErr = client.lastConnectionError {
                        throw ProcessSupervisorError.webSocketConnectionFailed(connErr)
                    }
                    throw ProcessSupervisorError.webSocketConnectionFailed(
                        "Timed out waiting for WebSocket handshake and authentication."
                    )
                }

                client.autoReconnectEnabled = true
                client.startMonitoring()
            }

            // Step 13: Transition to ready ONLY after WebSocket connection succeeds
            let ollamaLabel = isOllamaReused ? "reused" : "managed"
            let backendLabel = isBackendReused ? "reused" : "managed"
            setState(
                .ready,
                message: "Carbon is ready",
                detail: "Ollama (\(ollamaLabel)) • Backend (\(backendLabel)) • Model \(requiredModel)"
            )
        } catch {
            let mappedError: ProcessSupervisorError
            if error is CancellationError || cancellationRequested {
                mappedError = .startupCancelled
            } else if let supError = error as? ProcessSupervisorError {
                mappedError = supError
            } else {
                mappedError = .backendStartupFailed(error.localizedDescription)
            }

            // Clean up WebSocket and only owned child processes on failure/cancellation;
            // externally owned processes are strictly preserved.
            // If shutdown() is actively orchestrating Phase 3/4 teardown, defer process termination to shutdown().
            client?.autoReconnectEnabled = false
            client?.disconnectWebSocket()
            if !isShuttingDown && !hasCompletedShutdown {
                await cleanupAllOwnedChildrenDuringStartup(runId: runId, timeout: 2.0)
            }

            startupError = mappedError
            setState(
                .failed,
                message: mappedError.errorDescription ?? "Carbon startup failed.",
                detail: mappedError.recoverySuggestion
            )
            throw mappedError
        }
    }

    // MARK: - 5-Phase Graceful Application Shutdown & 8.0s Global Watchdog

    public func resolveLocalAuthToken() -> String? {
        if let envToken = ProcessInfo.processInfo.environment["CARBON_API_TOKEN"], !envToken.isEmpty {
            return envToken
        }
        let candidateURLs = [
            projectRoot.appendingPathComponent("data/.carbon_token"),
            URL(fileURLWithPath: fileManager.currentDirectoryPath).appendingPathComponent("data/.carbon_token"),
            fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Carbon/data/.carbon_token")
        ]
        for url in candidateURLs {
            if let token = try? String(contentsOf: url, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
               !token.isEmpty {
                return token
            }
        }
        return nil
    }

    public func dismissShutdownError() {
        if !isShuttingDown && !hasCompletedShutdown && shutdownState == .failed {
            shutdownErrorMessage = nil
            shutdownStatusMessage = ""
            shutdownState = .idle
        }
    }

    @discardableResult
    public func shutdown(
        client: (any CarbonClientConnecting)? = nil,
        budgets: ShutdownTimeoutConfig = .standard,
        lockRelease: (@MainActor @Sendable () -> Void)? = nil
    ) async -> ShutdownReport {
        if hasCompletedShutdown, let previousReport = lastShutdownReport {
            return previousReport
        }

        if let existingShutdown = shutdownTask {
            return await existingShutdown.value
        }

        // Prevent overlapping Retry Quit while an earlier shutdown work task is still executing (e.g. post-watchdog unwind)
        if activeShutdownWorkTask != nil {
            let msg = "Previous shutdown attempt is still finishing. Please wait briefly and retry Quit."
            shutdownState = .failed
            shutdownStatusMessage = "Previous shutdown still finishing"
            shutdownErrorMessage = msg
            let blockedReport = ShutdownReport(
                canTerminate: false,
                inferenceCancelled: false,
                webSocketClosed: false,
                backendHttpRequested: false,
                backendOutcome: backendOwnership == .owned ? .timedOut : (backendOwnership == .external ? .preservedExternal : .notApplicable),
                ollamaOutcome: ollamaOwnership == .owned ? .timedOut : (ollamaOwnership == .external ? .preservedExternal : .notApplicable),
                lockReleased: false,
                timedOutByWatchdog: true,
                failureMessage: msg
            )
            self.lastShutdownReport = blockedReport
            return blockedReport
        }

        isShuttingDown = true
        shutdownErrorMessage = nil
        shutdownState = .shuttingDown
        shutdownStatusMessage = "Shutting down Carbon..."

        let targetClient = client ?? activeClient
        let runId = UUID()
        self.activeShutdownRunId = runId

        let task = Task { @MainActor [weak self] () -> ShutdownReport in
            guard let self = self else {
                return ShutdownReport(
                    canTerminate: false,
                    inferenceCancelled: false,
                    webSocketClosed: false,
                    backendHttpRequested: false,
                    backendOutcome: .notApplicable,
                    ollamaOutcome: .notApplicable,
                    lockReleased: false,
                    timedOutByWatchdog: false,
                    failureMessage: "Supervisor released during shutdown."
                )
            }
            return await self.performCoordinatedShutdown(
                runId: runId,
                targetClient: targetClient,
                budgets: budgets,
                lockRelease: lockRelease
            )
        }

        self.shutdownTask = task
        let report = await task.value
        self.shutdownTask = nil
        self.lastShutdownReport = report
        return report
    }

    private func performCoordinatedShutdown(
        runId: UUID,
        targetClient: (any CarbonClientConnecting)?,
        budgets: ShutdownTimeoutConfig,
        lockRelease: (@MainActor @Sendable () -> Void)?
    ) async -> ShutdownReport {
        // Halt any in-flight startup sequence cleanly so no new processes are launched,
        // and invalidate/cancel any detached startup rollback wait so shutdown + watchdog take over cleanly.
        self.activeStartupRunId = nil
        if startupTask != nil || ollamaStartupTask != nil || backendStartupTask != nil || activeStartupCleanupTask != nil {
            cancelStartup()
            activeStartupCleanupTask?.cancel()
            if let st = startupTask {
                _ = try? await st.value
            }
            if let ct = activeStartupCleanupTask {
                await ct.value
            }
            self.activeStartupCleanupTask = nil
        }

        let globalDeadline = Date().addingTimeInterval(budgets.globalWatchdog)
        func remainingGlobal() -> TimeInterval {
            return max(0.0, globalDeadline.timeIntervalSinceNow)
        }

        var inferenceCancelled = false
        var webSocketClosed = false
        var backendHttpRequested = false
        var backendOutcome: OwnedProcessShutdownOutcome = backendOwnership == .external ? .preservedExternal : .notApplicable
        var ollamaOutcome: OwnedProcessShutdownOutcome = ollamaOwnership == .external ? .preservedExternal : .notApplicable

        return await withCheckedContinuation { (continuation: CheckedContinuation<ShutdownReport, Never>) in
            var settled = false
            var workTask: Task<Void, Never>?
            var watchdogTask: Task<Void, Never>?

            watchdogTask = Task { @MainActor [weak self] in
                let watchdogNanos = UInt64(max(0.0, budgets.globalWatchdog) * 1_000_000_000)
                try? await Task.sleep(nanoseconds: watchdogNanos)
                guard !Task.isCancelled, !settled else { return }
                settled = true
                workTask?.cancel()
                targetClient?.disconnectWebSocket()

                // Re-inspect live ownership invariants: NEVER falsely clear an owned child on watchdog timeout.
                // Note: Intentionally retain activeShutdownWorkTask so overlapping Retry Quit is prevented until it unwinds.
                if let self = self, self.activeShutdownRunId == runId {
                    if self.backendOwnership == .owned {
                        if let child = self._childBackendProcess, !child.isRunning {
                            self._childBackendProcess = nil
                            self.backendOwnership = .notRunning
                            backendOutcome = .exited(terminationStatus: 0)
                        } else {
                            backendOutcome = .timedOut
                        }
                    }
                    if self.ollamaOwnership == .owned {
                        if let child = self._childOllamaProcess, !child.isRunning {
                            self._childOllamaProcess = nil
                            self.ollamaOwnership = .notRunning
                            ollamaOutcome = .exited(terminationStatus: 0)
                        } else {
                            ollamaOutcome = .timedOut
                        }
                    }

                    let stillOwnedBackend = self.backendOwnership == .owned
                    let stillOwnedOllama = self.ollamaOwnership == .owned
                    let canQuit: Bool
                    let failureReason: String?

                    if stillOwnedBackend || stillOwnedOllama {
                        canQuit = false
                        var runningNames: [String] = []
                        if stillOwnedBackend { runningNames.append("Carbon backend") }
                        if stillOwnedOllama { runningNames.append("Ollama engine") }
                        failureReason = "Shutdown watchdog (\(String(format: "%.1f", budgets.globalWatchdog))s) expired while owned process(es) remained running: \(runningNames.joined(separator: ", ")). Application termination cancelled to avoid abandoning running children."
                        self.isShuttingDown = false
                        self.hasCompletedShutdown = false
                        self.shutdownState = .failed
                        self.shutdownStatusMessage = "Shutdown timed out"
                        self.shutdownErrorMessage = failureReason
                        targetClient?.resetShutdownStateAfterCancelledQuit()
                    } else {
                        canQuit = true
                        failureReason = nil
                        self.hasCompletedShutdown = true
                        self.isShuttingDown = false
                        lockRelease?()
                        self.shutdownState = .completed
                        self.shutdownStatusMessage = "Shutdown complete"
                    }

                    let report = ShutdownReport(
                        canTerminate: canQuit,
                        inferenceCancelled: inferenceCancelled,
                        webSocketClosed: webSocketClosed,
                        backendHttpRequested: backendHttpRequested,
                        backendOutcome: backendOutcome,
                        ollamaOutcome: ollamaOutcome,
                        lockReleased: canQuit,
                        timedOutByWatchdog: true,
                        failureMessage: failureReason
                    )
                    continuation.resume(returning: report)
                } else {
                    continuation.resume(
                        returning: ShutdownReport(
                            canTerminate: false,
                            inferenceCancelled: inferenceCancelled,
                            webSocketClosed: webSocketClosed,
                            backendHttpRequested: backendHttpRequested,
                            backendOutcome: backendOutcome,
                            ollamaOutcome: ollamaOutcome,
                            lockReleased: false,
                            timedOutByWatchdog: true,
                            failureMessage: "Shutdown watchdog expired."
                        )
                    )
                }
            }

            self.shutdownWorkLaunchCount += 1
            workTask = Task { @MainActor [weak self] in
                defer {
                    if let self = self, self.activeShutdownRunId == runId {
                        self.activeShutdownWorkTask = nil
                    }
                }

                guard let self = self, self.activeShutdownRunId == runId else {
                    if !settled {
                        settled = true
                        watchdogTask?.cancel()
                        continuation.resume(
                            returning: ShutdownReport(
                                canTerminate: false,
                                inferenceCancelled: false,
                                webSocketClosed: false,
                                backendHttpRequested: false,
                                backendOutcome: .notApplicable,
                                ollamaOutcome: .notApplicable,
                                lockReleased: false,
                                timedOutByWatchdog: false,
                                failureMessage: "Supervisor unavailable."
                            )
                        )
                    }
                    return
                }

                @MainActor func isRunStillActive() -> Bool {
                    guard !settled && !Task.isCancelled else { return false }
                    return self.activeShutdownRunId == runId
                }

                // Phase 1 — Cancel active inference (budget: 1.2s)
                if isRunStillActive() {
                    self.shutdownStatusMessage = "Cancelling active inference..."
                    let p1Budget = min(budgets.inferenceCancel, remainingGlobal())
                    await targetClient?.cancelActiveInferenceAndWait(timeout: p1Budget)
                    if isRunStillActive() {
                        inferenceCancelled = true
                    }
                }

                // Phase 2 — Close WebSocket (budget: 0.3s)
                if isRunStillActive() {
                    self.shutdownStatusMessage = "Closing WebSocket connection..."
                    let p2Budget = min(budgets.webSocketClose, remainingGlobal())
                    await targetClient?.closeWebSocketForShutdown(timeout: p2Budget)
                    if isRunStillActive() {
                        webSocketClosed = true
                    }
                }

                // Phase 3 — Stop Carbon-owned backend (budget: 3.0s)
                if isRunStillActive() {
                    if self.backendOwnership == .owned, let child = self._childBackendProcess {
                        if !child.isRunning {
                            if isRunStillActive() && self._childBackendProcess === child {
                                self._childBackendProcess = nil
                                self.backendOwnership = .notRunning
                                backendOutcome = .exited(terminationStatus: 0)
                            }
                        } else {
                            self.shutdownStatusMessage = "Stopping Carbon Python backend..."
                            let p3Budget = min(budgets.backendShutdown, remainingGlobal())
                            let phaseDeadline = Date().addingTimeInterval(p3Budget)
                            let token = targetClient?.resolveAuthToken() ?? self.resolveLocalAuthToken()

                            if let token = token, !token.isEmpty, Date() < phaseDeadline {
                                let shutdownURL = self.backendBaseURL.appendingPathComponent("api/v1/system/shutdown")
                                var request = URLRequest(url: shutdownURL)
                                request.httpMethod = "POST"
                                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                                request.timeoutInterval = max(0.05, min(1.0, phaseDeadline.timeIntervalSinceNow))

                                do {
                                    let (_, response) = try await self.httpClient.send(request)
                                    if isRunStillActive() {
                                        backendHttpRequested = true
                                        if response.statusCode == 200 {
                                            let rem = phaseDeadline.timeIntervalSinceNow
                                            if rem > 0.02 {
                                                // Leave remaining budget for Process.terminate() fallback if graceful exit stalls
                                                let gracefulWait = rem > 1.2 ? min(2.0, rem - 1.0) : max(0.05, rem * 0.6)
                                                let exitRes = await child.awaitExit(timeout: gracefulWait)
                                                if isRunStillActive(), case .exited(let code) = exitRes {
                                                    backendOutcome = .exited(terminationStatus: code)
                                                }
                                            }
                                        }
                                    }
                                } catch {
                                    // Bounded HTTP failed or timed out; fall through immediately to Process.terminate() if run still active
                                }
                            }

                            if isRunStillActive() && child.isRunning {
                                let remFallback = max(0.05, min(phaseDeadline.timeIntervalSinceNow, remainingGlobal()))
                                let termRes = await child.terminateAndAwaitExit(timeout: remFallback)
                                if isRunStillActive(), case .exited(let code) = termRes {
                                    backendOutcome = .exited(terminationStatus: code)
                                }
                            }

                            // Guard EVERY ownership mutation with active-run & handle-identity verification
                            // (note: proc.terminationHandler may have already cleared _childBackendProcess to nil upon confirmed exit)
                            if isRunStillActive() && (self._childBackendProcess === child || (self._childBackendProcess == nil && !child.isRunning)) {
                                if !child.isRunning {
                                    self._childBackendProcess = nil
                                    self.backendOwnership = .notRunning
                                    if case .exited = backendOutcome {
                                        // already recorded exact code
                                    } else {
                                        backendOutcome = .exited(terminationStatus: 0)
                                    }
                                } else {
                                    // Retain handle & .owned state; never falsely report exited
                                    backendOutcome = .timedOut
                                }
                            }
                        }
                    } else if self.backendOwnership == .external {
                        backendOutcome = .preservedExternal
                    } else {
                        backendOutcome = .notApplicable
                    }
                }

                // Phase 4 — Stop Carbon-owned Ollama (budget: 2.0s)
                if isRunStillActive() {
                    if self.ollamaOwnership == .owned, let child = self._childOllamaProcess {
                        if !child.isRunning {
                            if isRunStillActive() && self._childOllamaProcess === child {
                                self._childOllamaProcess = nil
                                self.ollamaOwnership = .notRunning
                                ollamaOutcome = .exited(terminationStatus: 0)
                            }
                        } else {
                            self.shutdownStatusMessage = "Stopping local Ollama engine..."
                            let p4Budget = max(0.05, min(budgets.ollamaShutdown, remainingGlobal()))
                            let termRes = await child.terminateAndAwaitExit(timeout: p4Budget)
                            if isRunStillActive() && (self._childOllamaProcess === child || (self._childOllamaProcess == nil && !child.isRunning)) {
                                switch termRes {
                                case .exited(let code):
                                    if !child.isRunning {
                                        self._childOllamaProcess = nil
                                        self.ollamaOwnership = .notRunning
                                        ollamaOutcome = .exited(terminationStatus: code)
                                    } else {
                                        ollamaOutcome = .timedOut
                                    }
                                case .timedOut:
                                    // Retain handle & .owned state
                                    ollamaOutcome = .timedOut
                                }
                            }
                        }
                    } else if self.ollamaOwnership == .external {
                        ollamaOutcome = .preservedExternal
                    } else {
                        ollamaOutcome = .notApplicable
                    }
                }

                guard isRunStillActive() else { return }
                settled = true
                watchdogTask?.cancel()

                // Phase 5 — Lock and state cleanup (budget: 0.3s)
                let stillOwnedBackend = self.backendOwnership == .owned
                let stillOwnedOllama = self.ollamaOwnership == .owned
                let allClean = !stillOwnedBackend && !stillOwnedOllama

                if allClean {
                    self.shutdownStatusMessage = "Releasing application lock..."
                    self.hasCompletedShutdown = true
                    self.isShuttingDown = false
                    lockRelease?()
                    self.shutdownState = .completed
                    self.shutdownStatusMessage = "Shutdown complete"
                    self.shutdownErrorMessage = nil

                    let report = ShutdownReport(
                        canTerminate: true,
                        inferenceCancelled: inferenceCancelled,
                        webSocketClosed: webSocketClosed,
                        backendHttpRequested: backendHttpRequested,
                        backendOutcome: backendOutcome,
                        ollamaOutcome: ollamaOutcome,
                        lockReleased: true,
                        timedOutByWatchdog: false,
                        failureMessage: nil
                    )
                    continuation.resume(returning: report)
                } else {
                    var timedOutNames: [String] = []
                    if stillOwnedBackend { timedOutNames.append("Carbon backend") }
                    if stillOwnedOllama { timedOutNames.append("Ollama engine") }
                    let msg = "Timed out stopping Carbon-owned process(es): \(timedOutNames.joined(separator: ", ")). Ownership has been preserved; termination cancelled so you can retry safely."

                    self.isShuttingDown = false
                    self.hasCompletedShutdown = false
                    self.shutdownState = .failed
                    self.shutdownStatusMessage = "Shutdown incomplete"
                    self.shutdownErrorMessage = msg
                    targetClient?.resetShutdownStateAfterCancelledQuit()

                    let report = ShutdownReport(
                        canTerminate: false,
                        inferenceCancelled: inferenceCancelled,
                        webSocketClosed: webSocketClosed,
                        backendHttpRequested: backendHttpRequested,
                        backendOutcome: backendOutcome,
                        ollamaOutcome: ollamaOutcome,
                        lockReleased: false,
                        timedOutByWatchdog: false,
                        failureMessage: msg
                    )
                    continuation.resume(returning: report)
                }
            }
            self.activeShutdownWorkTask = workTask
        }
    }

    // MARK: - Safe Ownership Teardown

    @discardableResult
    public func terminateOwnedChildrenOnly() -> (ollamaTerminated: Bool, backendTerminated: Bool) {
        var ollamaTerminated = false
        var backendTerminated = false

        if ollamaOwnership == .owned, let child = _childOllamaProcess {
            child.terminate()
            if !child.isRunning {
                _childOllamaProcess = nil
                ollamaOwnership = .notRunning
                ollamaTerminated = true
            }
        }
        if backendOwnership == .owned, let child = _childBackendProcess {
            child.terminate()
            if !child.isRunning {
                _childBackendProcess = nil
                backendOwnership = .notRunning
                backendTerminated = true
            }
        }
        return (ollamaTerminated, backendTerminated)
    }

    @discardableResult
    public func terminateOwnedChildrenOnly(awaitExitTimeout: TimeInterval) async -> (ollamaResult: ProcessTerminationResult?, backendResult: ProcessTerminationResult?) {
        var ollamaRes: ProcessTerminationResult?
        var backendRes: ProcessTerminationResult?

        if ollamaOwnership == .owned, let child = _childOllamaProcess {
            let res = await child.terminateAndAwaitExit(timeout: awaitExitTimeout)
            ollamaRes = res
            if case .exited = res {
                _childOllamaProcess = nil
                ollamaOwnership = .notRunning
            }
        }
        if backendOwnership == .owned, let child = _childBackendProcess {
            let res = await child.terminateAndAwaitExit(timeout: awaitExitTimeout)
            backendRes = res
            if case .exited = res {
                _childBackendProcess = nil
                backendOwnership = .notRunning
            }
        }

        return (ollamaRes, backendRes)
    }

    @discardableResult
    public func terminateOwnedChildrenAndAwaitExit(timeout: TimeInterval = 2.0) async -> (ollamaResult: ProcessTerminationResult?, backendResult: ProcessTerminationResult?) {
        await terminateOwnedChildrenOnly(awaitExitTimeout: timeout)
    }
}
