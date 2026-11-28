import Foundation

extension CarbonClient: CarbonClientConnecting {}

// MARK: - Test Doubles

final class ShutdownAtomic<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Value

    init(_ value: Value) {
        self.value = value
    }

    func get() -> Value {
        lock.lock()
        defer { lock.unlock() }
        return value
    }

    func set(_ newValue: Value) {
        lock.lock()
        defer { lock.unlock() }
        value = newValue
    }

    @discardableResult
    func mutate<R>(_ block: (inout Value) -> R) -> R {
        lock.lock()
        defer { lock.unlock() }
        return block(&value)
    }
}

final class ShutdownMockProcess: ProcessRunning, @unchecked Sendable {
    var isRunning: Bool
    var processIdentifier: Int32
    var terminationHandler: (@Sendable (any ProcessRunning) -> Void)?
    var simulatedExitCode: Int32 = 0
    var ignoreTerminate: Bool = false
    var exitAfterDelayOnAwait: TimeInterval? = nil

    private(set) var terminateCallCount: Int = 0
    private(set) var terminateAndAwaitExitCallCount: Int = 0
    private(set) var awaitExitCallCount: Int = 0

    init(processIdentifier: Int32 = 92001, isRunning: Bool = true) {
        self.processIdentifier = processIdentifier
        self.isRunning = isRunning
    }

    func terminate() {
        terminateCallCount += 1
        if !ignoreTerminate {
            isRunning = false
            terminationHandler?(self)
        }
    }

    @discardableResult
    func terminateAndAwaitExit(timeout: TimeInterval = 2.0) async -> ProcessTerminationResult {
        terminateAndAwaitExitCallCount += 1
        terminate()
        if isRunning {
            return .timedOut
        }
        return .exited(terminationStatus: simulatedExitCode)
    }

    @discardableResult
    func awaitExit(timeout: TimeInterval = 2.0) async -> ProcessTerminationResult {
        awaitExitCallCount += 1
        if let delay = exitAfterDelayOnAwait, delay <= timeout {
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            isRunning = false
            terminationHandler?(self)
            return .exited(terminationStatus: simulatedExitCode)
        }
        if isRunning {
            return .timedOut
        }
        return .exited(terminationStatus: simulatedExitCode)
    }
}

final class ShutdownMockLauncher: ProcessSupervisorProcessLauncher, @unchecked Sendable {
    struct LaunchRecord: Sendable {
        let executableURL: URL
        let arguments: [String]
    }

    private let state = ShutdownAtomic<(launches: [LaunchRecord], factory: (@Sendable (URL, [String]) -> any ProcessRunning)?)>(([], nil))

    var launches: [LaunchRecord] {
        state.get().launches
    }

    func setFactory(_ factory: @escaping @Sendable (URL, [String]) -> any ProcessRunning) {
        state.mutate { $0.factory = factory }
    }

    func launchProcess(
        executableURL: URL,
        arguments: [String],
        currentDirectoryURL: URL?,
        environment: [String: String]?
    ) throws -> any ProcessRunning {
        let record = LaunchRecord(executableURL: executableURL, arguments: arguments)
        let current = state.mutate { s -> (@Sendable (URL, [String]) -> any ProcessRunning)? in
            s.launches.append(record)
            return s.factory
        }
        if let factory = current {
            return factory(executableURL, arguments)
        }
        return ShutdownMockProcess()
    }
}

final class ShutdownMockHTTPClient: ProcessSupervisorHTTPClient, @unchecked Sendable {
    struct RecordedRequest: Sendable {
        let method: String
        let path: String
        let authorizationHeader: String?
    }

    private let getHandler = ShutdownAtomic<(@Sendable (URL) async throws -> (Data, Int))?>(nil)
    private let sendHandler = ShutdownAtomic<(@Sendable (URLRequest) async throws -> (Data, Int))?>(nil)
    private let requests = ShutdownAtomic<[RecordedRequest]>([])

    var recordedRequests: [RecordedRequest] {
        requests.get()
    }

    func setGetHandler(_ handler: @escaping @Sendable (URL) async throws -> (Data, Int)) {
        getHandler.set(handler)
    }

    func setSendHandler(_ handler: @escaping @Sendable (URLRequest) async throws -> (Data, Int)) {
        sendHandler.set(handler)
    }

    func data(from url: URL) async throws -> (Data, HTTPURLResponse) {
        requests.mutate { $0.append(RecordedRequest(method: "GET", path: url.path, authorizationHeader: nil)) }
        guard let handler = getHandler.get() else {
            throw URLError(.cannotConnectToHost)
        }
        let (data, code) = try await handler(url)
        return (data, HTTPURLResponse(url: url, statusCode: code, httpVersion: "HTTP/1.1", headerFields: nil)!)
    }

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let url = request.url ?? URL(string: "http://127.0.0.1:8765")!
        let method = request.httpMethod ?? "GET"
        let auth = request.value(forHTTPHeaderField: "Authorization")
        requests.mutate { $0.append(RecordedRequest(method: method, path: url.path, authorizationHeader: auth)) }

        if method == "POST", let handler = sendHandler.get() {
            let (data, code) = try await handler(request)
            return (data, HTTPURLResponse(url: url, statusCode: code, httpVersion: "HTTP/1.1", headerFields: nil)!)
        }
        if let handler = getHandler.get() {
            let (data, code) = try await handler(url)
            return (data, HTTPURLResponse(url: url, statusCode: code, httpVersion: "HTTP/1.1", headerFields: nil)!)
        }
        throw URLError(.cannotConnectToHost)
    }
}

@MainActor
final class ShutdownMockCarbonClient: CarbonClientConnecting {
    var isConnected: Bool = true
    var lastAuthError: String? = nil
    var lastConnectionError: String? = nil
    var autoReconnectEnabled: Bool = true
    var tokenToReturn: String? = "shutdown-secret-token"

    var hasActiveInference: Bool = false
    var inferenceCancelDelay: TimeInterval = 0.0
    var ignoreCancelTimeout: Bool = false

    private(set) var cancelInferenceCallCount: Int = 0
    private(set) var closeWebSocketCallCount: Int = 0
    private(set) var resetAfterCancelCount: Int = 0
    private(set) var connectCallCount: Int = 0
    private(set) var orderLog: [String] = []

    func resolveAuthToken() -> String? {
        return tokenToReturn
    }

    func connectWebSocket() {
        connectCallCount += 1
        isConnected = true
    }

    func disconnectWebSocket() {
        isConnected = false
    }

    func startMonitoring() {}

    func cancelActiveInferenceAndWait(timeout: TimeInterval) async {
        cancelInferenceCallCount += 1
        autoReconnectEnabled = false
        orderLog.append("cancelInference")
        if hasActiveInference && inferenceCancelDelay > 0 {
            let sleepFor = ignoreCancelTimeout ? inferenceCancelDelay : min(inferenceCancelDelay, timeout)
            try? await Task.sleep(nanoseconds: UInt64(sleepFor * 1_000_000_000))
        }
        hasActiveInference = false
    }

    func closeWebSocketForShutdown(timeout: TimeInterval) async {
        closeWebSocketCallCount += 1
        autoReconnectEnabled = false
        isConnected = false
        orderLog.append("closeWebSocket")
    }

    func resetShutdownStateAfterCancelledQuit() {
        resetAfterCancelCount += 1
    }
}

// MARK: - Helpers

func jsonData(_ dict: [String: Any]) -> Data {
    return try! JSONSerialization.data(withJSONObject: dict, options: [])
}

let healthyBackendJSON: [String: Any] = [
    "status": "healthy",
    "core_running": true,
    "ollama_connected": true,
    "active_model": ["id": "qwen3.5:9b", "name": "Qwen 3.5 9B"]
]

let healthyTagsJSON: [String: Any] = [
    "models": [["name": "qwen3.5:9b", "size": 5_000_000_000]]
]

@main
struct ShutdownTestRunner {
    @MainActor
    static func main() async {
        print("=== CARBON STAGE 1.11 INCREMENT 2D: GRACEFUL SHUTDOWN & WATCHDOG TESTS ===")

        let fm = FileManager.default
        let tempDir = fm.temporaryDirectory.appendingPathComponent("carbon_2d_tests_\(UUID().uuidString)")
        try! fm.createDirectory(at: tempDir, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: tempDir) }

        func makeExecutable(at relPath: String) -> URL {
            let url = tempDir.appendingPathComponent(relPath)
            try! fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            fm.createFile(atPath: url.path, contents: "#!/bin/sh\nexit 0\n".data(using: .utf8), attributes: [.posixPermissions: 0o755])
            return url
        }

        _ = makeExecutable(at: ".venv/bin/python")
        let fakeOllama = makeExecutable(at: "bin/ollama")

        // ------------------------------------------------------------------
        // TEST 1: Quit with no active inference
        // ------------------------------------------------------------------
        print("[1/18] Verifying quit with no active inference...")
        do {
            let http = ShutdownMockHTTPClient()
            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") { return (jsonData(healthyBackendJSON), 200) }
                throw URLError(.cannotConnectToHost)
            }
            let client = ShutdownMockCarbonClient()
            client.hasActiveInference = false
            let lockFlag = ShutdownAtomic(false)

            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http)
            try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])

            let report = await supervisor.shutdown(client: client) {
                lockFlag.set(true)
            }

            assert(report.canTerminate == true)
            assert(report.inferenceCancelled == true)
            assert(report.webSocketClosed == true)
            assert(report.backendOutcome == .preservedExternal)
            assert(report.ollamaOutcome == .preservedExternal)
            assert(report.lockReleased == true && lockFlag.get() == true)
            assert(supervisor.shutdownState == .completed)
            assert(supervisor.hasCompletedShutdown == true)
            print("       ✓ Idle quit completed cleanly and released lock")
        } catch {
            assertionFailure("Test 1 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 2: Quit with active inference cancels task then closes WebSocket
        // ------------------------------------------------------------------
        print("[2/18] Verifying quit with active inference cancels task before closing WebSocket...")
        do {
            let http = ShutdownMockHTTPClient()
            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") { return (jsonData(healthyBackendJSON), 200) }
                throw URLError(.cannotConnectToHost)
            }
            let client = ShutdownMockCarbonClient()
            client.hasActiveInference = true
            client.inferenceCancelDelay = 0.05

            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http)
            try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])

            let report = await supervisor.shutdown(client: client)
            assert(report.canTerminate == true)
            assert(client.cancelInferenceCallCount == 1)
            assert(client.closeWebSocketCallCount == 1)
            assert(client.orderLog == ["cancelInference", "closeWebSocket"], "Expected Phase 1 before Phase 2, got \(client.orderLog)")
            assert(!client.hasActiveInference)
            print("       ✓ Active inference cancelled cleanly before WebSocket closure")
        } catch {
            assertionFailure("Test 2 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 3: Real CarbonClient Phase 1/2 lifecycle & blocks new inference/reconnect during shutdown
        // ------------------------------------------------------------------
        print("[3/18] Verifying real CarbonClient shutdown phases block new messages and reconnects...")
        do {
            let client = CarbonClient(
                autoConnect: false,
                projectRoot: tempDir,
                baseURL: URL(string: "http://127.0.0.1:59998")!,
                wsURL: URL(string: "ws://127.0.0.1:59998/ws/chat")!
            )
            client.isGenerating = true
            client.messages.append(ChatDisplayMessage(role: "assistant", text: "Partial", isStreaming: true))

            await client.cancelActiveInferenceAndWait(timeout: 0.2)
            assert(client.isShuttingDown == true)
            assert(client.autoReconnectEnabled == false)
            assert(!client.isGenerating)
            assert(client.messages.last?.isCancelled == true)

            // Sending new message after shutdown starts must be rejected safely
            client.sendMessage("Should be rejected")
            assert(!client.isGenerating)
            assert(client.messages.last?.errorMessage?.contains("shutting down") == true)

            await client.closeWebSocketForShutdown(timeout: 0.2)
            assert(!client.isConnected)
            assert(client.connectionStatus == .disconnected)

            // Attempt reconnect during shutdown must be a safe no-op
            client.connectWebSocket()
            assert(client.connectionStatus == .disconnected)
            print("       ✓ Real CarbonClient cancelled task, closed socket, and blocked post-shutdown prompts/reconnects")
        }

        // ------------------------------------------------------------------
        // TEST 4 & TEST 6: External backend & External Ollama remain untouched
        // ------------------------------------------------------------------
        print("[4/18] Verifying external backend & external Ollama receive ZERO HTTP shutdown or termination signals...")
        do {
            let http = ShutdownMockHTTPClient()
            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") { return (jsonData(healthyBackendJSON), 200) }
                throw URLError(.cannotConnectToHost)
            }
            let client = ShutdownMockCarbonClient()
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http)
            try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])

            assert(supervisor.ollamaOwnership == .external)
            assert(supervisor.backendOwnership == .external)

            let report = await supervisor.shutdown(client: client)
            assert(report.canTerminate == true)
            assert(report.backendHttpRequested == false, "Must NEVER send POST /shutdown to external backend")
            assert(report.backendOutcome == .preservedExternal)
            assert(report.ollamaOutcome == .preservedExternal)
            assert(!http.recordedRequests.contains { $0.method == "POST" }, "No POST requests allowed for external backend")
            assert(supervisor.ollamaOwnership == .external)
            assert(supervisor.backendOwnership == .external)
            print("       ✓ External Ollama and External backend completely untouched")
        } catch {
            assertionFailure("Test 4 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 5 & TEST 7: Carbon-owned backend HTTP shutdown + Carbon-owned Ollama SIGTERM
        // ------------------------------------------------------------------
        print("[5/18] Verifying Carbon-owned backend receives authenticated HTTP shutdown & owned Ollama receives SIGTERM...")
        do {
            let ollamaUp = ShutdownAtomic(false)
            let backendUp = ShutdownAtomic(false)
            let http = ShutdownMockHTTPClient()
            let ollamaProc = ShutdownMockProcess(processIdentifier: 3101)
            let backendProc = ShutdownMockProcess(processIdentifier: 3102)
            backendProc.exitAfterDelayOnAwait = 0.05

            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") {
                    if ollamaUp.get() { return (jsonData(["version": "0.33.0"]), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    if backendUp.get() { return (jsonData(healthyBackendJSON), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }
            http.setSendHandler { req in
                if req.url?.path.hasSuffix("api/v1/system/shutdown") == true {
                    assert(req.value(forHTTPHeaderField: "Authorization") == "Bearer shutdown-secret-token")
                    return (jsonData(["status": "shutting_down"]), 200)
                }
                throw URLError(.badURL)
            }

            let launcher = ShutdownMockLauncher()
            launcher.setFactory { url, _ in
                if url.lastPathComponent == "ollama" {
                    ollamaUp.set(true)
                    return ollamaProc
                } else {
                    backendUp.set(true)
                    return backendProc
                }
            }

            let client = ShutdownMockCarbonClient()
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)
            try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])

            assert(supervisor.ollamaOwnership == .owned)
            assert(supervisor.backendOwnership == .owned)

            let report = await supervisor.shutdown(client: client)
            assert(report.canTerminate == true)
            assert(report.backendHttpRequested == true)
            if case .exited = report.backendOutcome {} else { assertionFailure("Expected backend .exited, got \(report.backendOutcome)") }
            if case .exited = report.ollamaOutcome {} else { assertionFailure("Expected ollama .exited, got \(report.ollamaOutcome)") }
            assert(backendProc.awaitExitCallCount >= 1)
            assert(backendProc.terminateCallCount == 0, "Graceful HTTP shutdown exited cleanly; SIGTERM fallback not needed")
            assert(ollamaProc.terminateAndAwaitExitCallCount == 1)
            assert(supervisor.backendOwnership == .notRunning)
            assert(supervisor.ollamaOwnership == .notRunning)
            assert(supervisor.childBackendProcess == nil)
            assert(supervisor.childOllamaProcess == nil)
            print("       ✓ Owned backend exited via authenticated HTTP shutdown; owned Ollama terminated cleanly")
        } catch {
            assertionFailure("Test 5 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 6: Backend HTTP shutdown unavailable/503 falls back safely to Process.terminate()
        // ------------------------------------------------------------------
        print("[6/18] Verifying backend HTTP shutdown failure falls back cleanly to Process.terminate()...")
        do {
            let backendUp = ShutdownAtomic(false)
            let http = ShutdownMockHTTPClient()
            let backendProc = ShutdownMockProcess(processIdentifier: 3201)

            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    if backendUp.get() { return (jsonData(healthyBackendJSON), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }
            http.setSendHandler { _ in
                return (jsonData(["detail": "unavailable"]), 503)
            }

            let launcher = ShutdownMockLauncher()
            launcher.setFactory { _, _ in
                backendUp.set(true)
                return backendProc
            }

            let client = ShutdownMockCarbonClient()
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)
            try await supervisor.startup(client: client)

            let report = await supervisor.shutdown(client: client)
            assert(report.canTerminate == true)
            assert(report.backendHttpRequested == true)
            assert(backendProc.terminateCallCount == 1, "SIGTERM fallback must fire when HTTP shutdown returns 503")
            assert(supervisor.backendOwnership == .notRunning)
            assert(supervisor.ollamaOwnership == .external)
            print("       ✓ Phase 3 fallback Process.terminate() confirmed backend exit; external Ollama untouched")
        } catch {
            assertionFailure("Test 6 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 7 & TEST 10: Backend shutdown timeout preserves .owned & cancels quit
        // ------------------------------------------------------------------
        print("[7/18] Verifying backend shutdown timeout retains .owned handle and prevents premature quit...")
        do {
            let backendUp = ShutdownAtomic(false)
            let http = ShutdownMockHTTPClient()
            let stuckBackend = ShutdownMockProcess(processIdentifier: 3301)
            stuckBackend.ignoreTerminate = true

            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    if backendUp.get() { return (jsonData(healthyBackendJSON), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }
            http.setSendHandler { _ in throw URLError(.timedOut) }

            let launcher = ShutdownMockLauncher()
            launcher.setFactory { _, _ in
                backendUp.set(true)
                return stuckBackend
            }

            let client = ShutdownMockCarbonClient()
            let lockReleased = ShutdownAtomic(false)
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)
            try await supervisor.startup(client: client)

            let fastBudgets = ShutdownTimeoutConfig(
                inferenceCancel: 0.2, webSocketClose: 0.1, backendShutdown: 0.2,
                ollamaShutdown: 0.2, lockCleanup: 0.1, globalWatchdog: 2.0
            )
            let report = await supervisor.shutdown(client: client, budgets: fastBudgets) {
                lockReleased.set(true)
            }

            assert(report.canTerminate == false, "Must NOT allow quit when owned backend is still running")
            assert(report.backendOutcome == .timedOut)
            assert(report.lockReleased == false && lockReleased.get() == false)
            assert(supervisor.backendOwnership == .owned, "Ownership MUST remain .owned on timeout")
            assert(supervisor.childBackendProcess === stuckBackend, "Handle MUST remain retained")
            assert(supervisor.shutdownState == .failed)
            assert(client.resetAfterCancelCount == 1)
            print("       ✓ Backend timeout preserved .owned handle and cancelled application termination")
        } catch {
            assertionFailure("Test 7 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 8 & TEST 10: Ollama termination timeout preserves .owned & cancels quit
        // ------------------------------------------------------------------
        print("[8/18] Verifying Ollama termination timeout retains .owned handle and cancels quit...")
        do {
            let ollamaUp = ShutdownAtomic(false)
            let http = ShutdownMockHTTPClient()
            let stuckOllama = ShutdownMockProcess(processIdentifier: 3401)
            stuckOllama.ignoreTerminate = true

            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") {
                    if ollamaUp.get() { return (jsonData(["version": "0.33.0"]), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") { return (jsonData(healthyBackendJSON), 200) }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = ShutdownMockLauncher()
            launcher.setFactory { _, _ in
                ollamaUp.set(true)
                return stuckOllama
            }

            let client = ShutdownMockCarbonClient()
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)
            try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])

            let fastBudgets = ShutdownTimeoutConfig(
                inferenceCancel: 0.2, webSocketClose: 0.1, backendShutdown: 0.2,
                ollamaShutdown: 0.2, lockCleanup: 0.1, globalWatchdog: 2.0
            )
            let report = await supervisor.shutdown(client: client, budgets: fastBudgets)

            assert(report.canTerminate == false)
            assert(report.ollamaOutcome == .timedOut)
            assert(report.backendOutcome == .preservedExternal)
            assert(supervisor.ollamaOwnership == .owned, "Ownership MUST remain .owned on timeout")
            assert(supervisor.childOllamaProcess === stuckOllama)
            assert(supervisor.backendOwnership == .external, "External backend untouched")
            print("       ✓ Ollama timeout preserved .owned handle, left external backend untouched, and cancelled quit")
        } catch {
            assertionFailure("Test 8 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 9: Repeated quit requests do NOT create duplicate cleanup operations
        // ------------------------------------------------------------------
        print("[9/18] Verifying repeated concurrent & sequential quit requests deduplicate cleanly...")
        do {
            let backendUp = ShutdownAtomic(false)
            let http = ShutdownMockHTTPClient()
            let backendProc = ShutdownMockProcess(processIdentifier: 3501)
            backendProc.exitAfterDelayOnAwait = 0.08

            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    if backendUp.get() { return (jsonData(healthyBackendJSON), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }
            http.setSendHandler { _ in (jsonData(["status": "shutting_down"]), 200) }

            let launcher = ShutdownMockLauncher()
            launcher.setFactory { _, _ in
                backendUp.set(true)
                return backendProc
            }

            let client = ShutdownMockCarbonClient()
            let lockReleaseCount = ShutdownAtomic(0)
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)
            try await supervisor.startup(client: client)

            async let q1 = supervisor.shutdown(client: client) { lockReleaseCount.mutate { $0 += 1 } }
            async let q2 = supervisor.shutdown(client: client) { lockReleaseCount.mutate { $0 += 1 } }
            let (r1, r2) = await (q1, q2)

            // Subsequent sequential quit request after completion
            let r3 = await supervisor.shutdown(client: client) { lockReleaseCount.mutate { $0 += 1 } }

            assert(r1.canTerminate && r2.canTerminate && r3.canTerminate)
            assert(lockReleaseCount.get() == 1, "Lock must be released exactly once, got \(lockReleaseCount.get())")
            assert(http.recordedRequests.filter { $0.method == "POST" }.count == 1, "Exactly 1 HTTP shutdown request allowed")
            assert(client.closeWebSocketCallCount == 1)
            print("       ✓ Repeated concurrent and sequential quit requests ran cleanup exactly once")
        } catch {
            assertionFailure("Test 9 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 10: Real SingleInstanceLock flock released on successful shutdown, preserved on failed shutdown
        // ------------------------------------------------------------------
        print("[10/18] Verifying real SingleInstanceLock flock release & persistent inode safety...")
        do {
            let lockURL = tempDir.appendingPathComponent("data/.carbon_app.lock")
            let lock1 = SingleInstanceLock(lockURL: lockURL)
            let lock2 = SingleInstanceLock(lockURL: lockURL)
            assert(lock1.acquire() == .acquired)
            if case .alreadyRunning = lock2.acquire() {} else { assertionFailure("Expected lock busy") }

            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: ShutdownMockHTTPClient())
            let report = await supervisor.shutdown(client: nil) {
                lock1.release()
                lock1.release() // verify idempotent release
            }

            assert(report.canTerminate == true && report.lockReleased == true)
            assert(!lock1.isHeld)
            assert(fm.fileExists(atPath: lockURL.path), "Persistent lock file inode must NOT be deleted")
            assert(lock2.acquire() == .acquired, "Second instance must be able to acquire flock after shutdown")
            lock2.release()
            print("       ✓ Real kernel flock released idempotently while preserving lock file inode")
        }

        // ------------------------------------------------------------------
        // TEST 11: Shutdown cancellation/retry behaviour (first fails, retry succeeds)
        // ------------------------------------------------------------------
        print("[11/18] Verifying shutdown failure/cancellation allows clean Retry Quit...")
        do {
            let ollamaUp = ShutdownAtomic(false)
            let http = ShutdownMockHTTPClient()
            let ollamaProc = ShutdownMockProcess(processIdentifier: 3601)
            ollamaProc.ignoreTerminate = true

            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") {
                    if ollamaUp.get() { return (jsonData(["version": "0.33.0"]), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") { return (jsonData(healthyBackendJSON), 200) }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = ShutdownMockLauncher()
            launcher.setFactory { _, _ in
                ollamaUp.set(true)
                return ollamaProc
            }

            let lockURL = tempDir.appendingPathComponent("data/.retry_quit.lock")
            let lock = SingleInstanceLock(lockURL: lockURL)
            assert(lock.acquire() == .acquired)

            let client = ShutdownMockCarbonClient()
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)
            try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])

            let fastBudgets = ShutdownTimeoutConfig(
                inferenceCancel: 0.2, webSocketClose: 0.1, backendShutdown: 0.2,
                ollamaShutdown: 0.15, lockCleanup: 0.1, globalWatchdog: 2.0
            )
            // First Quit: fails on Ollama timeout -> lock stays held, canTerminate == false
            let firstReport = await supervisor.shutdown(client: client, budgets: fastBudgets) {
                lock.release()
            }
            assert(firstReport.canTerminate == false)
            assert(lock.isHeld, "Lock MUST remain held while Carbon stays open after cancelled termination")
            assert(supervisor.ollamaOwnership == .owned)

            // Process is now responsive; user retries Quit
            ollamaProc.ignoreTerminate = false
            let retryReport = await supervisor.shutdown(client: client, budgets: fastBudgets) {
                lock.release()
            }
            assert(retryReport.canTerminate == true)
            assert(supervisor.ollamaOwnership == .notRunning)
            assert(!lock.isHeld, "Lock released cleanly on Retry Quit success")
            print("       ✓ Cancelled quit preserved lock & ownership, and Retry Quit succeeded cleanly")
        } catch {
            assertionFailure("Test 11 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 12 & TEST 13: No process spawned during shutdown & no startup/reconnect allowed once shutdown begins
        // ------------------------------------------------------------------
        print("[12/18] Verifying zero processes spawned during shutdown and post-shutdown startup/reconnect rejected...")
        do {
            let http = ShutdownMockHTTPClient()
            let launcher = ShutdownMockLauncher()
            let client = ShutdownMockCarbonClient()
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            // Start a startup attempt whose Ollama check isn't responding yet, then immediately call shutdown()
            let startTask = Task {
                try await supervisor.startup(client: client, ollamaTimeout: 2.0, customOllamaSearchPaths: [fakeOllama.path])
            }
            // Immediately initiate shutdown concurrently
            let report = await supervisor.shutdown(client: client)

            do {
                try await startTask.value
            } catch let err as ProcessSupervisorError {
                assert(err == .startupCancelled)
            }

            assert(report.canTerminate == true)
            assert(supervisor.hasCompletedShutdown == true)

            // Verify subsequent startup() attempt after shutdown fails cleanly without launching anything
            do {
                try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])
                assertionFailure("startup() must be rejected after shutdown completes")
            } catch let err as ProcessSupervisorError {
                assert(err == .startupCancelled)
            }
            assert(launcher.launches.count <= 1)
            print("       ✓ Shutdown prevented concurrent/subsequent process launches and reconnects")
        } catch {
            assertionFailure("Test 12 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 14: Global 8.0s watchdog behavior bounds hung shutdown & preserves .owned
        // ------------------------------------------------------------------
        print("[13/18] Verifying global watchdog bounds hung HTTP shutdown without abandoning owned processes...")
        do {
            let backendUp = ShutdownAtomic(false)
            let http = ShutdownMockHTTPClient()
            let stuckBackend = ShutdownMockProcess(processIdentifier: 3701)

            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    if backendUp.get() { return (jsonData(healthyBackendJSON), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }
            http.setSendHandler { _ in
                // Simulate hung network socket lasting way longer than globalWatchdog!
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                return (jsonData(["status": "shutting_down"]), 200)
            }

            let launcher = ShutdownMockLauncher()
            launcher.setFactory { _, _ in
                backendUp.set(true)
                return stuckBackend
            }

            let client = ShutdownMockCarbonClient()
            let lockReleased = ShutdownAtomic(false)
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)
            try await supervisor.startup(client: client)

            let watchdogBudgets = ShutdownTimeoutConfig(
                inferenceCancel: 0.2,
                webSocketClose: 0.1,
                backendShutdown: 2.0,
                ollamaShutdown: 1.0,
                lockCleanup: 0.1,
                globalWatchdog: 0.35
            )
            let t0 = Date()
            let report = await supervisor.shutdown(client: client, budgets: watchdogBudgets) {
                lockReleased.set(true)
            }
            let elapsed = Date().timeIntervalSince(t0)

            assert(elapsed < 1.2, "Watchdog MUST abort bounded around 0.35s, took \(elapsed)s")
            assert(report.timedOutByWatchdog == true)
            assert(report.canTerminate == false, "Watchdog must cancel quit rather than abandoning still-running owned backend")
            assert(report.backendOutcome == .timedOut)
            assert(supervisor.backendOwnership == .owned)
            assert(supervisor.childBackendProcess === stuckBackend)
            assert(!lockReleased.get())
            assert(supervisor.shutdownState == .failed)
            print("       ✓ Global watchdog fired cleanly in \(String(format: "%.2f", elapsed))s and preserved .owned state")
        } catch {
            assertionFailure("Test 13 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 15: End-to-End Live Ephemeral Server Shutdown (WS Cancel + HTTP /shutdown + Child Exit + Flock Release)
        // ------------------------------------------------------------------
        print("[14/18] Verifying live ephemeral backend graceful shutdown end-to-end (WS cancel -> WS close -> HTTP /shutdown -> exit -> flock)...")
        do {
            let realPython = URL(fileURLWithPath: fm.currentDirectoryPath).appendingPathComponent(".venv/bin/python")
            if fm.isExecutableFile(atPath: realPython.path) {
                let serverDir = tempDir.appendingPathComponent("e2e_shutdown_server")
                let serverData = serverDir.appendingPathComponent("data")
                try! fm.createDirectory(at: serverData, withIntermediateDirectories: true)
                let liveToken = "e2e-shutdown-token-777"
                try! liveToken.write(to: serverData.appendingPathComponent(".carbon_token"), atomically: true, encoding: .utf8)

                // Pick free ephemeral port
                let portPipe = Pipe()
                let portProc = Process()
                portProc.executableURL = realPython
                portProc.arguments = ["-c", "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()"]
                portProc.standardOutput = portPipe
                try portProc.run()
                portProc.waitUntilExit()
                let portStr = String(data: portPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)!.trimmingCharacters(in: .whitespacesAndNewlines)
                let port = Int(portStr)!

                let serverScript = """
                import asyncio, json, uvicorn
                from fastapi import FastAPI, BackgroundTasks, Header, HTTPException, WebSocket

                app = FastAPI()
                app.state.uvicorn_server = None
                app.state.cancelled_tasks = []

                @app.get("/api/version")
                async def version():
                    return {"version": "0.33.0"}

                @app.get("/api/tags")
                async def tags():
                    return {"models": [{"name": "qwen3.5:9b"}]}

                @app.get("/api/v1/health")
                async def health():
                    return {
                        "status": "healthy",
                        "core_running": True,
                        "ollama_connected": True,
                        "active_model": {"id": "qwen3.5:9b"}
                    }

                @app.post("/api/v1/system/shutdown")
                async def sys_shutdown(background_tasks: BackgroundTasks, authorization: str = Header(default="")):
                    if authorization != "Bearer \(liveToken)":
                        raise HTTPException(status_code=401, detail="Unauthorized")
                    def stop():
                        if app.state.uvicorn_server:
                            app.state.uvicorn_server.should_exit = True
                    background_tasks.add_task(stop)
                    return {"status": "shutting_down"}

                @app.websocket("/ws/chat")
                async def ws_chat(ws: WebSocket):
                    await ws.accept()
                    try:
                        while True:
                            msg = json.loads(await ws.receive_text())
                            act = msg.get("action")
                            if act == "auth" and msg.get("token") == "\(liveToken)":
                                await ws.send_json({"event": "authenticated"})
                            elif act == "submit":
                                await ws.send_json({"event": "task_created", "task_id": "t-live-1", "conversation_id": "c-live"})
                                await ws.send_json({"event": "thinking"})
                            elif act == "cancel":
                                tid = msg.get("task_id", "")
                                app.state.cancelled_tasks.append(tid)
                                await ws.send_json({"event": "cancelled", "task_id": tid})
                    except Exception:
                        pass

                config = uvicorn.Config(app, host="127.0.0.1", port=\(port), log_level="warning")
                server = uvicorn.Server(config)
                app.state.uvicorn_server = server
                server.run()
                """

                // Wrapper script in fake .venv/bin/python? No, use a tiny launcher shell script at serverDir/.venv/bin/python!
                let shimDir = serverDir.appendingPathComponent(".venv/bin")
                let srcDir = serverDir.appendingPathComponent("src")
                try! fm.createDirectory(at: shimDir, withIntermediateDirectories: true)
                try! fm.createDirectory(at: srcDir, withIntermediateDirectories: true)
                let serverPyFile = serverDir.appendingPathComponent("mock_server.py")
                try! serverScript.write(to: serverPyFile, atomically: true, encoding: .utf8)

                let shimPath = shimDir.appendingPathComponent("python")
                let shimBody = "#!/bin/sh\nexec \"\(realPython.path)\" \"\(serverPyFile.path)\"\n"
                fm.createFile(atPath: shimPath.path, contents: shimBody.data(using: .utf8), attributes: [.posixPermissions: 0o755])

                let epURL = URL(string: "http://127.0.0.1:\(port)")!
                let wsURL = URL(string: "ws://127.0.0.1:\(port)/ws/chat")!
                let liveClient = CarbonClient(autoConnect: false, projectRoot: serverDir, baseURL: epURL, wsURL: wsURL)
                let lockURL = serverData.appendingPathComponent(".carbon_app.lock")
                let liveLock = SingleInstanceLock(lockURL: lockURL)
                assert(liveLock.acquire() == .acquired)

                let supervisor = ProcessSupervisor(
                    projectRoot: serverDir,
                    ollamaBaseURL: epURL,
                    backendBaseURL: epURL
                )

                //Wait for ephemeral mock ollama/version isn't up yet until backend starts -> wait: mock server serves both /api/version and /api/v1/health once launched, so startup launches it when we checkOllama?
                // Wait: if ollama port == epURL and ollama is not running yet, startup would try to launch ollama first!
                // Instead, use MockHTTPClient? No, we want DefaultProcessSupervisorHTTPClient to hit the real ephemeral server!
                // So let's verify ensureBackendRunning launches shimPath as owned backend and hits real /api/v1/health + /api/v1/system/shutdown!
                try await supervisor.ensureBackendRunning(timeout: 8.0)
                assert(supervisor.backendOwnership == .owned)
                liveClient.connectWebSocket()
                for _ in 0..<40 {
                    if liveClient.isConnected { break }
                    try? await Task.sleep(nanoseconds: 50_000_000)
                }
                assert(liveClient.isConnected, "Live client must connect to ephemeral server")

                // Start an active generation task so Phase 1 cancels it
                liveClient.sendMessage("Long running query")
                for _ in 0..<40 {
                    if liveClient.currentTaskId == "t-live-1" { break }
                    try? await Task.sleep(nanoseconds: 50_000_000)
                }
                assert(liveClient.currentTaskId == "t-live-1")

                let report = await supervisor.shutdown(client: liveClient, budgets: .standard) {
                    liveLock.release()
                }
                assert(report.canTerminate == true)
                assert(report.inferenceCancelled == true)
                assert(report.webSocketClosed == true)
                assert(report.backendHttpRequested == true)
                if case .exited(let code) = report.backendOutcome {
                    assert(code == 0, "Expected clean exit code 0 from uvicorn server.should_exit=True, got \(code)")
                } else {
                    assertionFailure("Expected .exited for live backend, got \(report.backendOutcome)")
                }
                assert(supervisor.backendOwnership == .notRunning)
                assert(!liveLock.isHeld)
                print("       ✓ Live end-to-end 5-phase shutdown exited Uvicorn cleanly (exit 0) and released flock on port \(port)")
            } else {
                print("       (Skipped live ephemeral E2E shutdown test: .venv/bin/python unavailable)")
            }
        } catch {
            assertionFailure("Test 14 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 15: Verify ShutdownTimeoutConfig standard budgets match approved architecture
        // ------------------------------------------------------------------
        print("[15/18] Verifying approved Increment 2D shutdown timeout budgets...")
        do {
            let std = ShutdownTimeoutConfig.standard
            assert(std.inferenceCancel == 1.2)
            assert(std.webSocketClose == 0.3)
            assert(std.backendShutdown == 3.0)
            assert(std.ollamaShutdown == 2.0)
            assert(std.lockCleanup == 0.3)
            assert(abs((std.inferenceCancel + std.webSocketClose + std.backendShutdown + std.ollamaShutdown + std.lockCleanup) - 6.8) < 0.0001)
            assert(std.globalWatchdog == 8.0)
            print("       ✓ Approved budgets verified (1.2 + 0.3 + 3.0 + 2.0 + 0.3 = 6.8s; Watchdog = 8.0s)")
        }

        // ------------------------------------------------------------------
        // TEST 16: Verify watchdog does not mark already-exited backend as timedOut
        // ------------------------------------------------------------------
        print("[16/18] Verifying watchdog preserves already-completed phase results accurately...")
        do {
            let http = ShutdownMockHTTPClient()
            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") { return (jsonData(healthyBackendJSON), 200) }
                throw URLError(.cannotConnectToHost)
            }
            let client = ShutdownMockCarbonClient()
            client.hasActiveInference = true
            client.inferenceCancelDelay = 0.6 // Delays past 0.15s global watchdog!
            client.ignoreCancelTimeout = true
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http)
            try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])

            let report = await supervisor.shutdown(
                client: client,
                budgets: ShutdownTimeoutConfig(
                    inferenceCancel: 1.0, webSocketClose: 0.1, backendShutdown: 0.1,
                    ollamaShutdown: 0.1, lockCleanup: 0.1, globalWatchdog: 0.15
                )
            )
            // Since both external (zero owned children running), watchdog still confirms no owned children remain
            assert(report.timedOutByWatchdog == true)
            assert(report.canTerminate == true)
            assert(supervisor.ollamaOwnership == .external)
            assert(supervisor.backendOwnership == .external)
            print("       ✓ Watchdog verified safe completion when only external services exist")
        } catch {
            assertionFailure("Test 16 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 17 (D2): Watchdog expiry retains activeShutdownWorkTask, blocks overlapping Retry Quit,
        // prevents old task from mutating ownership, preserves lock, and allows clean subsequent Retry
        // ------------------------------------------------------------------
        print("[17/18] Verifying D2: tracked workTask after watchdog expiry prevents overlapping Retry Quit & stale mutations...")
        do {
            let backendUp = ShutdownAtomic(false)
            let http = ShutdownMockHTTPClient()
            let backendProc = ShutdownMockProcess(processIdentifier: 3801)
            let httpCompletedFlag = ShutdownAtomic(false)

            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    if backendUp.get() { return (jsonData(healthyBackendJSON), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }
            http.setSendHandler { _ in
                // Genuinely cancellation-insensitive suspended HTTP call lasting 0.55s (> 0.15s watchdog)
                await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
                    DispatchQueue.global().asyncAfter(deadline: .now() + 0.55) {
                        cont.resume()
                    }
                }
                httpCompletedFlag.set(true)
                return (jsonData(["status": "shutting_down"]), 200)
            }

            let launcher = ShutdownMockLauncher()
            launcher.setFactory { _, _ in
                backendUp.set(true)
                return backendProc
            }

            let lockURL = tempDir.appendingPathComponent("data/.d2_retry_lock")
            let lock = SingleInstanceLock(lockURL: lockURL)
            assert(lock.acquire() == .acquired)

            let client = ShutdownMockCarbonClient()
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)
            try await supervisor.startup(client: client)

            let budgets = ShutdownTimeoutConfig(
                inferenceCancel: 0.1,
                webSocketClose: 0.05,
                backendShutdown: 2.0,
                ollamaShutdown: 0.2,
                lockCleanup: 0.1,
                globalWatchdog: 0.15
            )

            // 1. Trigger initial quit: watchdog fires at 0.15s while http.send is still suspended!
            let firstReport = await supervisor.shutdown(client: client, budgets: budgets) {
                lock.release()
            }
            assert(firstReport.timedOutByWatchdog == true)
            assert(firstReport.canTerminate == false)
            assert(supervisor.hasActiveShutdownWorkTask == true, "Work task MUST remain tracked after watchdog expiry!")
            assert(supervisor.shutdownWorkLaunchCount == 1)
            assert(supervisor.backendOwnership == .owned)
            assert(supervisor.ollamaOwnership == .external, "External Ollama must remain untouched")
            assert(lock.isHeld, "Lock MUST remain held")

            // 2. Attempt Retry Quit immediately while previous workTask is STILL suspended in HTTP send
            let overlapReport = await supervisor.shutdown(client: client, budgets: budgets) {
                lock.release()
            }
            assert(overlapReport.canTerminate == false, "Overlapping Retry Quit must be rejected safely")
            assert(supervisor.hasActiveShutdownWorkTask == true, "Prior workTask reference must remain kept")
            assert(supervisor.shutdownWorkLaunchCount == 1, "Second shutdown work task MUST NOT be started!")
            assert(lock.isHeld, "Lock MUST still be held")
            assert(supervisor.backendOwnership == .owned)

            // 3. Wait until previous workTask actually finishes its cancellation-insensitive HTTP sleep
            for _ in 0..<40 {
                if !supervisor.hasActiveShutdownWorkTask {
                    break
                }
                try? await Task.sleep(nanoseconds: 25_000_000)
            }
            assert(httpCompletedFlag.get() == true)
            assert(supervisor.hasActiveShutdownWorkTask == false, "Work task tracking clears once task truly finishes")

            // Critical invariant: old cancelled task MUST NOT have called terminate() or cleared ownership after watchdog!
            assert(backendProc.terminateCallCount == 0, "Old cancelled task must not touch process after watchdog expiry")
            assert(supervisor.backendOwnership == .owned, "Ownership must still be .owned until an active retry finishes")
            assert(supervisor.ollamaOwnership == .external)
            assert(lock.isHeld)

            // 4. Now perform Retry Quit with fast/working HTTP shutdown after old task completed
            http.setSendHandler { _ in (jsonData(["status": "shutting_down"]), 200) }
            backendProc.exitAfterDelayOnAwait = 0.05
            let retryBudgets = ShutdownTimeoutConfig(
                inferenceCancel: 0.1, webSocketClose: 0.05, backendShutdown: 1.0,
                ollamaShutdown: 0.2, lockCleanup: 0.1, globalWatchdog: 2.0
            )
            let retryReport = await supervisor.shutdown(client: client, budgets: retryBudgets) {
                lock.release()
            }
            assert(retryReport.canTerminate == true)
            assert(retryReport.lockReleased == true && !lock.isHeld)
            assert(supervisor.shutdownWorkLaunchCount == 2)
            assert(supervisor.backendOwnership == .notRunning)
            assert(supervisor.ollamaOwnership == .external)
            print("       ✓ D2 invariants verified: tracked task, blocked overlap, zero stale mutation, clean subsequent retry")
        } catch {
            assertionFailure("Test 17 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 18 (D2): External processes untouched & prompt watchdog cancel stops ManagedProcess loop quickly
        // ------------------------------------------------------------------
        print("[18/18] Verifying D1+D2 combined: watchdog cancelling real ManagedProcess stops wait promptly without busy-spin...")
        do {
            let fmLauncher = DefaultProcessLauncher()
            let pyExec = URL(fileURLWithPath: "/usr/bin/python3")
            let realChild = try fmLauncher.launchProcess(
                executableURL: pyExec,
                arguments: ["-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(0.8)"],
                currentDirectoryURL: nil,
                environment: nil
            )
            try await Task.sleep(nanoseconds: 40_000_000)

            let http = ShutdownMockHTTPClient()
            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") { return (jsonData(healthyBackendJSON), 200) }
                throw URLError(.cannotConnectToHost)
            }
            let launcher = ShutdownMockLauncher()
            launcher.setFactory { _, _ in realChild }
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            // Inject realChild as owned Ollama by launching when Ollama check fails first
            let ollamaUp = ShutdownAtomic(false)
            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") {
                    if ollamaUp.get() { return (jsonData(["version": "0.33.0"]), 200) }
                    ollamaUp.set(true)
                    throw URLError(.cannotConnectToHost)
                }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                if url.path.hasSuffix("api/v1/health") { return (jsonData(healthyBackendJSON), 200) }
                throw URLError(.cannotConnectToHost)
            }

            let client = ShutdownMockCarbonClient()
            try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])
            assert(supervisor.ollamaOwnership == .owned)
            assert(supervisor.backendOwnership == .external)

            let t0 = Date()
            let report = await supervisor.shutdown(
                client: client,
                budgets: ShutdownTimeoutConfig(
                    inferenceCancel: 0.05, webSocketClose: 0.05, backendShutdown: 0.05,
                    ollamaShutdown: 5.0, lockCleanup: 0.05, globalWatchdog: 0.15
                )
            )
            let elapsed = Date().timeIntervalSince(t0)

            assert(elapsed < 0.5, "Watchdog must cancel real ManagedProcess wait promptly, took \(elapsed)s")
            assert(report.timedOutByWatchdog == true)
            assert(report.canTerminate == false)
            assert(supervisor.ollamaOwnership == .owned)
            assert(supervisor.backendOwnership == .external, "External backend untouched")

            // Once realChild finishes its 0.8s sleep (and cancelled workTask unwinds quickly due to D1 fix)
            try await Task.sleep(nanoseconds: 50_000_000)
            assert(supervisor.hasActiveShutdownWorkTask == false, "D1 fix lets cancelled workTask exit promptly instead of spinning 5.0s!")

            _ = await realChild.awaitExit(timeout: 1.5)
            print("       ✓ Combined D1+D2 verified: cancelled workTask exited in <50ms and preserved .owned/.external")
        } catch {
            assertionFailure("Test 18 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 19 (F1 + 2D): Shutdown interrupting cancelled startup rollback takes control safely without stale mutation
        // ------------------------------------------------------------------
        print("[19/19] Verifying shutdown interrupts active F1 startup rollback cleanly, enforces watchdog, and prevents stale mutation...")
        do {
            let pyExec = URL(fileURLWithPath: "/usr/bin/python3")
            let fmLauncher = DefaultProcessLauncher()
            let realSlowChild = try fmLauncher.launchProcess(
                executableURL: pyExec,
                arguments: ["-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(0.9)"],
                currentDirectoryURL: nil,
                environment: nil
            )
            try await Task.sleep(nanoseconds: 40_000_000)

            let http = ShutdownMockHTTPClient()
            http.setGetHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsJSON), 200) }
                throw URLError(.cannotConnectToHost)
            }
            let launcher = ShutdownMockLauncher()
            launcher.setFactory { _, _ in realSlowChild }

            let lockURL = tempDir.appendingPathComponent("data/.f1_shutdown_lock")
            let lock = SingleInstanceLock(lockURL: lockURL)
            assert(lock.acquire() == .acquired)

            let client = ShutdownMockCarbonClient()
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            let startupTask = Task {
                try await supervisor.startup(client: client, backendTimeout: 5.0)
            }
            var waitingBackend = false
            for _ in 0..<100 {
                if supervisor.startupState == .waitingForBackend {
                    waitingBackend = true
                    break
                }
                try? await Task.sleep(nanoseconds: 20_000_000)
            }
            assert(waitingBackend)

            // Cancel startup (enters 2.0s F1 rollback wait on uncooperative child), then trigger Quit 60ms later!
            supervisor.cancelStartup()
            try? await Task.sleep(nanoseconds: 60_000_000)
            assert(supervisor.hasActiveStartupCleanupTask == true, "F1 rollback wait should be active before shutdown interrupts")

            let t0 = Date()
            let report = await supervisor.shutdown(
                client: client,
                budgets: ShutdownTimeoutConfig(
                    inferenceCancel: 0.05, webSocketClose: 0.05, backendShutdown: 5.0,
                    ollamaShutdown: 0.05, lockCleanup: 0.05, globalWatchdog: 0.25
                ),
                lockRelease: { lock.release() }
            )
            let dt = Date().timeIntervalSince(t0)

            // Startup rollback MUST have been aborted promptly instead of adding 2.0s before watchdog!
            assert(dt < 0.7, "Shutdown must interrupt F1 rollback and enforce watchdog within ~0.25s, took \(dt)s")
            assert(report.timedOutByWatchdog == true)
            assert(report.canTerminate == false)
            assert(!supervisor.hasActiveStartupCleanupTask, "Interrupted startup cleanup tracking must clear")
            assert(supervisor.backendOwnership == .owned, "Living child stays .owned")
            assert(supervisor.childBackendProcess === realSlowChild)
            assert(supervisor.ollamaOwnership == .external, "External Ollama stays untouched")
            assert(lock.isHeld, "Lock MUST remain held after watchdog failure")
            _ = try? await startupTask.value

            // Wait for short 0.9s script to finish naturally, then Retry Quit cleanly
            _ = await realSlowChild.awaitExit(timeout: 1.5)
            let retryReport = await supervisor.shutdown(client: client, budgets: .standard) {
                lock.release()
            }
            assert(retryReport.canTerminate == true && retryReport.lockReleased == true && !lock.isHeld)
            assert(supervisor.backendOwnership == .notRunning)
            assert(supervisor.ollamaOwnership == .external)
            print("       ✓ Shutdown interrupted active F1 rollback in \(String(format: "%.2f", dt))s and completed clean Retry Quit")
        } catch {
            assertionFailure("Test 19 failed: \(error)")
        }

        print("\n=== ALL 19 INCREMENT 2D/2E GRACEFUL SHUTDOWN TESTS PASSED SUCCESSFULLY ===")
    }
}
