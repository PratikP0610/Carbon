import Foundation

extension CarbonClient: CarbonClientConnecting {}

// MARK: - Test Doubles

final class AtomicValue<Value: Sendable>: @unchecked Sendable {
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

final class TestMockProcess: ProcessRunning, @unchecked Sendable {
    var isRunning: Bool
    var processIdentifier: Int32
    var terminationHandler: (@Sendable (any ProcessRunning) -> Void)?
    var simulatedExitCode: Int32 = 0
    var ignoreTerminate: Bool = false
    private(set) var terminateCallCount: Int = 0
    private(set) var terminateAndAwaitExitCallCount: Int = 0

    init(processIdentifier: Int32 = 91001, isRunning: Bool = true) {
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
        if isRunning {
            return .timedOut
        }
        return .exited(terminationStatus: simulatedExitCode)
    }
}

final class TestMockLauncher: ProcessSupervisorProcessLauncher, @unchecked Sendable {
    struct LaunchRecord: Sendable {
        let executableURL: URL
        let arguments: [String]
    }

    private let state = AtomicValue<(launches: [LaunchRecord], factory: (@Sendable (URL, [String]) -> any ProcessRunning)?)>(([], nil))

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
        return TestMockProcess()
    }
}

final class TestMockHTTPClient: ProcessSupervisorHTTPClient, @unchecked Sendable {
    private let handlerState = AtomicValue<(@Sendable (URL) async throws -> (Data, Int))?>(nil)
    private let requestsState = AtomicValue<[String]>([])

    var requestedPaths: [String] {
        requestsState.get()
    }

    func setHandler(_ handler: @escaping @Sendable (URL) async throws -> (Data, Int)) {
        handlerState.set(handler)
    }

    func data(from url: URL) async throws -> (Data, HTTPURLResponse) {
        requestsState.mutate { $0.append(url.path) }
        guard let handler = handlerState.get() else {
            throw URLError(.cannotConnectToHost)
        }
        let (data, code) = try await handler(url)
        let res = HTTPURLResponse(url: url, statusCode: code, httpVersion: "HTTP/1.1", headerFields: nil)!
        return (data, res)
    }
}

@MainActor
final class TestMockCarbonClient: CarbonClientConnecting {
    var isConnected: Bool = false
    var lastAuthError: String? = nil
    var lastConnectionError: String? = nil
    var autoReconnectEnabled: Bool = true

    var tokenToReturn: String? = "test-local-token"
    var onConnect: (@MainActor (TestMockCarbonClient) -> Void)?

    private(set) var connectCallCount: Int = 0
    private(set) var disconnectCallCount: Int = 0
    private(set) var startMonitoringCallCount: Int = 0

    func resolveAuthToken() -> String? {
        return tokenToReturn
    }

    func connectWebSocket() {
        connectCallCount += 1
        onConnect?(self)
    }

    func disconnectWebSocket() {
        disconnectCallCount += 1
        isConnected = false
    }

    func startMonitoring() {
        startMonitoringCallCount += 1
    }

    func cancelActiveInferenceAndWait(timeout: TimeInterval) async {
        // Not used in 2C startup tests
    }

    func closeWebSocketForShutdown(timeout: TimeInterval) async {
        disconnectWebSocket()
    }

    func resetShutdownStateAfterCancelledQuit() {
        // Not used in 2C startup tests
    }
}

// MARK: - Helper Utilities

func jsonData(_ dict: [String: Any]) -> Data {
    return try! JSONSerialization.data(withJSONObject: dict, options: [])
}

let healthyBackendPayload: [String: Any] = [
    "status": "healthy",
    "core_running": true,
    "ollama_connected": true,
    "active_model": ["id": "qwen3.5:9b", "name": "Qwen 3.5 9B"]
]

let healthyTagsPayload: [String: Any] = [
    "models": [["name": "qwen3.5:9b", "size": 5_000_000_000]]
]

let missingModelTagsPayload: [String: Any] = [
    "models": [["name": "llama3.2:latest", "size": 2_000_000_000]]
]

@main
struct StartupStateMachineTestRunner {
    @MainActor
    static func main() async {
        print("=== CARBON STAGE 1.11 INCREMENT 2C: STARTUP STATE MACHINE & CLIENT INTEGRATION TESTS ===")

        let fileManager = FileManager.default
        let tempDir = fileManager.temporaryDirectory.appendingPathComponent("carbon_2c_tests_\(UUID().uuidString)")
        try! fileManager.createDirectory(at: tempDir, withIntermediateDirectories: true)
        defer { try? fileManager.removeItem(at: tempDir) }

        func makeExecutable(at relativePath: String) -> URL {
            let url = tempDir.appendingPathComponent(relativePath)
            try! fileManager.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            fileManager.createFile(atPath: url.path, contents: "#!/bin/sh\nexit 0\n".data(using: .utf8), attributes: [.posixPermissions: 0o755])
            return url
        }

        _ = makeExecutable(at: ".venv/bin/python")
        let fakeOllama = makeExecutable(at: "bin/ollama")

        // ------------------------------------------------------------------
        // TEST 1: Both Ollama and Backend already running
        // ------------------------------------------------------------------
        print("[1/13] Verifying startup when Ollama and backend are both already running...")
        do {
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                if url.path.hasSuffix("api/v1/health") { return (jsonData(healthyBackendPayload), 200) }
                throw URLError(.cannotConnectToHost)
            }
            let launcher = TestMockLauncher()
            let client = TestMockCarbonClient()
            client.onConnect = { c in c.isConnected = true }

            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)
            assert(supervisor.startupState == .idle)

            try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])

            assert(supervisor.startupState == .ready)
            assert(supervisor.ollamaOwnership == .external)
            assert(supervisor.backendOwnership == .external)
            assert(supervisor.isOllamaReused == true)
            assert(supervisor.isBackendReused == true)
            assert(launcher.launches.isEmpty, "Must not spawn any child when both services are healthy")
            assert(client.connectCallCount == 1)
            assert(client.startMonitoringCallCount == 1)
            assert(supervisor.stateTransitionHistory == [.checkingOllama, .verifyingModel, .checkingBackend, .connecting, .ready],
                   "Unexpected state history: \(supervisor.stateTransitionHistory)")
            print("       ✓ Both external services reused; deterministic transition to .ready")
        } catch {
            assertionFailure("Test 1 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 2: Neither Ollama nor Backend running
        // ------------------------------------------------------------------
        print("[2/13] Verifying startup when neither service is running...")
        do {
            let ollamaUp = AtomicValue(false)
            let backendUp = AtomicValue(false)
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") {
                    if ollamaUp.get() { return (jsonData(["version": "0.33.0"]), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                if url.path.hasSuffix("api/tags") {
                    return (jsonData(healthyTagsPayload), 200)
                }
                if url.path.hasSuffix("api/v1/health") {
                    if backendUp.get() { return (jsonData(healthyBackendPayload), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            let ollamaProc = TestMockProcess(processIdentifier: 1101)
            let backendProc = TestMockProcess(processIdentifier: 1102)
            launcher.setFactory { url, _ in
                if url.lastPathComponent == "ollama" {
                    ollamaUp.set(true)
                    return ollamaProc
                } else {
                    backendUp.set(true)
                    return backendProc
                }
            }

            let client = TestMockCarbonClient()
            client.onConnect = { c in c.isConnected = true }

            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)
            try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])

            assert(supervisor.startupState == .ready)
            assert(supervisor.ollamaOwnership == .owned)
            assert(supervisor.backendOwnership == .owned)
            assert(supervisor.isOllamaReused == false)
            assert(supervisor.isBackendReused == false)
            assert(supervisor.childOllamaProcess === ollamaProc)
            assert(supervisor.childBackendProcess === backendProc)
            assert(launcher.launches.count == 2)
            assert(supervisor.stateTransitionHistory == [
                .checkingOllama, .startingOllama, .verifyingModel,
                .checkingBackend, .startingBackend, .waitingForBackend,
                .connecting, .ready
            ], "Unexpected history: \(supervisor.stateTransitionHistory)")
            print("       ✓ Spawned both owned processes in order and transitioned through full state machine to .ready")
        } catch {
            assertionFailure("Test 2 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 3: Only Ollama running
        // ------------------------------------------------------------------
        print("[3/13] Verifying startup when only Ollama is running...")
        do {
            let backendUp = AtomicValue(false)
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    if backendUp.get() { return (jsonData(healthyBackendPayload), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            let backendProc = TestMockProcess(processIdentifier: 1201)
            launcher.setFactory { _, _ in
                backendUp.set(true)
                return backendProc
            }

            let client = TestMockCarbonClient()
            client.onConnect = { c in c.isConnected = true }

            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)
            try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])

            assert(supervisor.startupState == .ready)
            assert(supervisor.ollamaOwnership == .external)
            assert(supervisor.backendOwnership == .owned)
            assert(supervisor.isOllamaReused == true)
            assert(supervisor.isBackendReused == false)
            assert(launcher.launches.count == 1)
            assert(supervisor.stateTransitionHistory == [
                .checkingOllama, .verifyingModel,
                .checkingBackend, .startingBackend, .waitingForBackend,
                .connecting, .ready
            ])
            print("       ✓ Reused external Ollama and spawned only owned backend")
        } catch {
            assertionFailure("Test 3 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 4: Only Backend running
        // ------------------------------------------------------------------
        print("[4/13] Verifying startup when only the backend is running...")
        do {
            let ollamaUp = AtomicValue(false)
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") {
                    if ollamaUp.get() { return (jsonData(["version": "0.33.0"]), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                if url.path.hasSuffix("api/v1/health") { return (jsonData(healthyBackendPayload), 200) }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            let ollamaProc = TestMockProcess(processIdentifier: 1301)
            launcher.setFactory { _, _ in
                ollamaUp.set(true)
                return ollamaProc
            }

            let client = TestMockCarbonClient()
            client.onConnect = { c in c.isConnected = true }

            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)
            try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])

            assert(supervisor.startupState == .ready)
            assert(supervisor.ollamaOwnership == .owned)
            assert(supervisor.backendOwnership == .external)
            assert(supervisor.isOllamaReused == false)
            assert(supervisor.isBackendReused == true)
            assert(launcher.launches.count == 1)
            assert(supervisor.stateTransitionHistory == [
                .checkingOllama, .startingOllama, .verifyingModel,
                .checkingBackend, .connecting, .ready
            ])
            print("       ✓ Spawned owned Ollama and reused external backend")
        } catch {
            assertionFailure("Test 4 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 5: Missing model cleans up any owned child and preserves external
        // ------------------------------------------------------------------
        print("[5/13] Verifying missing model fails cleanly with pull prompt and zero auto-downloads...")
        do {
            let ollamaUp = AtomicValue(false)
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") {
                    if ollamaUp.get() { return (jsonData(["version": "0.33.0"]), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                if url.path.hasSuffix("api/tags") { return (jsonData(missingModelTagsPayload), 200) }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            let ollamaProc = TestMockProcess(processIdentifier: 1401)
            launcher.setFactory { _, _ in
                ollamaUp.set(true)
                return ollamaProc
            }

            let client = TestMockCarbonClient()
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            do {
                try await supervisor.startup(client: client, customOllamaSearchPaths: [fakeOllama.path])
                assertionFailure("Expected missing model error")
            } catch let err as ProcessSupervisorError {
                assert(err == .modelNotFound("qwen3.5:9b"))
                assert(supervisor.startupState == .failed)
                assert(supervisor.startupError == .modelNotFound("qwen3.5:9b"))
                assert(err.recoverySuggestion?.contains("ollama pull qwen3.5:9b") == true)
                assert(!http.requestedPaths.contains { $0.contains("pull") }, "Must never trigger ollama pull")
                assert(ollamaProc.terminateCallCount == 1, "Owned child started during failed launch must be cleaned up")
                assert(supervisor.ollamaOwnership == .notRunning)
                assert(client.connectCallCount == 0)
                print("       ✓ Missing model stopped startup cleanly, showed manual pull action, cleaned up owned child")
            }
        } catch {
            assertionFailure("Test 5 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 6: Ollama executable missing
        // ------------------------------------------------------------------
        print("[6/13] Verifying missing Ollama executable fails cleanly...")
        do {
            let http = TestMockHTTPClient()
            let launcher = TestMockLauncher()
            let client = TestMockCarbonClient()
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            do {
                try await supervisor.startup(client: client, customOllamaSearchPaths: ["/nonexistent/bin/ollama"])
                assertionFailure("Expected ollamaExecutableNotFound")
            } catch let err as ProcessSupervisorError {
                assert(err == .ollamaExecutableNotFound)
                assert(supervisor.startupState == .failed)
                assert(supervisor.startupError == .ollamaExecutableNotFound)
                assert(err.recoverySuggestion?.contains("https://ollama.ai") == true)
                print("       ✓ Missing Ollama binary transitioned to .failed with install instructions")
            }
        } catch {
            assertionFailure("Test 6 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 7: Backend health timeout cleans up owned backend, preserves external Ollama
        // ------------------------------------------------------------------
        print("[7/13] Verifying backend health timeout cleans up owned backend while preserving external Ollama...")
        do {
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            let backendProc = TestMockProcess(processIdentifier: 1501)
            launcher.setFactory { _, _ in backendProc }
            let client = TestMockCarbonClient()

            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            do {
                try await supervisor.startup(client: client, backendTimeout: 0.25, customOllamaSearchPaths: [fakeOllama.path])
                assertionFailure("Expected backendTimeout")
            } catch let err as ProcessSupervisorError {
                assert(err == .backendTimeout)
                assert(supervisor.startupState == .failed)
                assert(supervisor.startupError == .backendTimeout)
                assert(backendProc.terminateAndAwaitExitCallCount >= 1)
                assert(supervisor.backendOwnership == .notRunning)
                assert(supervisor.ollamaOwnership == .external, "External Ollama must be preserved!")
                assert(client.connectCallCount == 0)
                print("       ✓ Backend timeout terminated owned backend and preserved external Ollama")
            }
        } catch {
            assertionFailure("Test 7 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 8: Authentication failures (missing token & rejected WebSocket auth)
        // ------------------------------------------------------------------
        print("[8/13] Verifying authentication failure states (missing token & server rejection)...")
        do {
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                if url.path.hasSuffix("api/v1/health") { return (jsonData(healthyBackendPayload), 200) }
                throw URLError(.cannotConnectToHost)
            }

            // Sub-case A: Missing local token
            let clientNoToken = TestMockCarbonClient()
            clientNoToken.tokenToReturn = nil
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http)

            do {
                try await supervisor.startup(client: clientNoToken)
                assertionFailure("Expected authenticationFailed when token is missing")
            } catch let err as ProcessSupervisorError {
                if case .authenticationFailed = err {
                    assert(supervisor.startupState == .failed)
                    assert(clientNoToken.connectCallCount == 0, "Must not open WebSocket if token missing")
                    print("         Sub-case A: Missing token rejected before WebSocket open")
                } else {
                    assertionFailure("Expected .authenticationFailed, got \(err)")
                }
            }

            // Sub-case B: Rejected auth error on WebSocket
            let clientBadToken = TestMockCarbonClient()
            clientBadToken.tokenToReturn = "wrong-token"
            clientBadToken.onConnect = { c in
                c.lastAuthError = "Authentication failed. In-socket auth frame required."
            }

            do {
                try await supervisor.startup(client: clientBadToken, connectTimeout: 1.0)
                assertionFailure("Expected authenticationFailed on rejected auth")
            } catch let err as ProcessSupervisorError {
                if case .authenticationFailed(let msg) = err {
                    assert(msg.contains("Authentication failed"))
                    assert(supervisor.startupState == .failed)
                    assert(!clientBadToken.isConnected)
                    print("         Sub-case B: WebSocket auth error transitioned to .failed(.authenticationFailed)")
                } else {
                    assertionFailure("Expected .authenticationFailed, got \(err)")
                }
            }
        } catch {
            assertionFailure("Test 8 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 9: WebSocket connection failure
        // ------------------------------------------------------------------
        print("[9/13] Verifying WebSocket connection failure prevents .ready and cleans up owned child...")
        do {
            let backendUp = AtomicValue(false)
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    if backendUp.get() { return (jsonData(healthyBackendPayload), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            let backendProc = TestMockProcess(processIdentifier: 1601)
            launcher.setFactory { _, _ in
                backendUp.set(true)
                return backendProc
            }

            let client = TestMockCarbonClient()
            client.onConnect = { c in
                c.lastConnectionError = "Could not connect to WebSocket"
            }

            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            do {
                try await supervisor.startup(client: client, connectTimeout: 1.0)
                assertionFailure("Expected webSocketConnectionFailed")
            } catch let err as ProcessSupervisorError {
                if case .webSocketConnectionFailed = err {
                    assert(supervisor.startupState == .failed)
                    assert(backendProc.terminateCallCount == 1, "Owned backend cleaned up on WS launch failure")
                    assert(supervisor.ollamaOwnership == .external, "External Ollama preserved")
                    assert(client.disconnectCallCount >= 1)
                    print("       ✓ WebSocket connection failure reported accurately and cleaned up owned child")
                } else {
                    assertionFailure("Expected .webSocketConnectionFailed, got \(err)")
                }
            }
        } catch {
            assertionFailure("Test 9 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 10: Repeated startup requests (concurrent & sequential idempotency)
        // ------------------------------------------------------------------
        print("[10/13] Verifying repeated concurrent & sequential startup requests do not duplicate work...")
        do {
            let backendUp = AtomicValue(false)
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    if backendUp.get() { return (jsonData(healthyBackendPayload), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            launcher.setFactory { _, _ in
                backendUp.set(true)
                return TestMockProcess(processIdentifier: 1701)
            }

            let client = TestMockCarbonClient()
            client.onConnect = { c in
                Task { @MainActor in
                    try? await Task.sleep(nanoseconds: 50_000_000)
                    c.isConnected = true
                }
            }

            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            // Concurrent calls
            async let s1: Void = supervisor.startup(client: client)
            async let s2: Void = supervisor.startup(client: client)
            try await s1
            try await s2

            assert(launcher.launches.count == 1, "Expected 1 launch, got \(launcher.launches.count)")
            assert(client.connectCallCount == 1, "Expected 1 WebSocket connect call, got \(client.connectCallCount)")
            assert(supervisor.startupState == .ready)

            // Subsequent sequential call while already ready & connected
            try await supervisor.startup(client: client)
            assert(launcher.launches.count == 1)
            assert(client.connectCallCount == 1)
            print("       ✓ Repeated concurrent and sequential startup requests safely deduplicated")
        } catch {
            assertionFailure("Test 10 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 11: Cancellation mid-startup and Retry recovery
        // ------------------------------------------------------------------
        print("[11/13] Verifying cancellation mid-startup and clean Retry recovery...")
        do {
            let backendUp = AtomicValue(false)
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    if backendUp.get() { return (jsonData(healthyBackendPayload), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            let cancelledBackendProc = TestMockProcess(processIdentifier: 1801)
            let recoveredBackendProc = TestMockProcess(processIdentifier: 1802)
            let launchCount = AtomicValue(0)
            launcher.setFactory { _, _ in
                let idx = launchCount.mutate { val -> Int in
                    val += 1
                    return val
                }
                if idx == 1 {
                    // First attempt: stay unavailable so we can cancel during .waitingForBackend
                    return cancelledBackendProc
                } else {
                    backendUp.set(true)
                    return recoveredBackendProc
                }
            }

            let client = TestMockCarbonClient()
            client.onConnect = { c in c.isConnected = true }
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            // Start first attempt and cancel after 150ms
            let startTask = Task {
                try await supervisor.startup(client: client, backendTimeout: 5.0)
            }
            try? await Task.sleep(nanoseconds: 150_000_000)
            supervisor.cancelStartup()

            do {
                try await startTask.value
                assertionFailure("Expected startupCancelled error")
            } catch let err as ProcessSupervisorError {
                assert(err == .startupCancelled, "Expected .startupCancelled, got \(err)")
                assert(supervisor.startupState == .failed)
                assert(cancelledBackendProc.terminateCallCount >= 1, "Owned backend must be terminated upon cancellation")
                assert(supervisor.backendOwnership == .notRunning)
                assert(supervisor.ollamaOwnership == .external, "External Ollama must never be terminated upon cancellation")
            }

            // Now recover with Retry
            try await supervisor.startup(client: client, backendTimeout: 2.0)
            assert(supervisor.startupState == .ready)
            assert(supervisor.startupError == nil)
            assert(client.isConnected)
            assert(supervisor.backendOwnership == .owned)
            assert(supervisor.childBackendProcess === recoveredBackendProc)
            print("       ✓ Cancelled startup cleanly terminated owned child, preserved external Ollama, and recovered on Retry")
        } catch {
            assertionFailure("Test 11 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 12: Real CarbonClient unit behavior (autoConnect: false, token resolution, duplicate connect guard)
        // ------------------------------------------------------------------
        print("[12/13] Verifying real CarbonClient lifecycle hooks and token resolution...")
        do {
            let clientTempDir = tempDir.appendingPathComponent("real_client_dir")
            let dataDir = clientTempDir.appendingPathComponent("data")
            try! fileManager.createDirectory(at: dataDir, withIntermediateDirectories: true)
            let tokenFile = dataDir.appendingPathComponent(".carbon_token")
            try! "secret-unit-token-123".write(to: tokenFile, atomically: true, encoding: .utf8)

            let realClient = CarbonClient(
                autoConnect: false,
                projectRoot: clientTempDir,
                baseURL: URL(string: "http://127.0.0.1:59999")!,
                wsURL: URL(string: "ws://127.0.0.1:59999/ws/chat")!
            )
            assert(realClient.connectionStatus == .disconnected, "Must NOT auto-connect when autoConnect=false")
            assert(!realClient.isConnected)

            let resolved = realClient.resolveAuthToken()
            assert(resolved == "secret-unit-token-123", "Expected token from projectRoot/data/.carbon_token")

            realClient.disconnectWebSocket()
            assert(realClient.connectionStatus == .disconnected)
            print("       ✓ Real CarbonClient initializes offline cleanly and resolves token from projectRoot")
        }

        // ------------------------------------------------------------------
        // TEST 13: Real CarbonClient + ProcessSupervisor loopback WebSocket auth & chat integration
        // ------------------------------------------------------------------
        print("[13/13] Verifying ProcessSupervisor + real CarbonClient loopback WebSocket auth and chat flow...")
        do {
            // Find an unused port and run a tiny Python asyncio WebSocket + HTTP mock server
            // strictly on that ephemeral loopback port (never touching production ports 8765 or 11434)
            let realPython = URL(fileURLWithPath: fileManager.currentDirectoryPath).appendingPathComponent(".venv/bin/python")
            let hasFastAPI = fileManager.isExecutableFile(atPath: realPython.path)
            if hasFastAPI {
                let serverDir = tempDir.appendingPathComponent("live_ws_server")
                let serverData = serverDir.appendingPathComponent("data")
                try! fileManager.createDirectory(at: serverData, withIntermediateDirectories: true)
                try! "ephemeral-ws-token-999".write(
                    to: serverData.appendingPathComponent(".carbon_token"),
                    atomically: true,
                    encoding: .utf8
                )

                // Pick a free ephemeral port via Python socket
                let portPipe = Pipe()
                let portProc = Process()
                portProc.executableURL = realPython
                portProc.arguments = ["-c", "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()"]
                portProc.standardOutput = portPipe
                try portProc.run()
                portProc.waitUntilExit()
                let portData = portPipe.fileHandleForReading.readDataToEndOfFile()
                let portStr = String(data: portData, encoding: .utf8)!.trimmingCharacters(in: .whitespacesAndNewlines)
                let port = Int(portStr)!

                let serverScript = """
                import asyncio, json, uvicorn
                from fastapi import FastAPI, WebSocket

                app = FastAPI()

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
                        "active_model": {"id": "qwen3.5:9b", "name": "Qwen 3.5 9B"}
                    }

                @app.websocket("/ws/chat")
                async def ws_chat(ws: WebSocket):
                    await ws.accept()
                    while True:
                        raw = await ws.receive_text()
                        msg = json.loads(raw)
                        action = msg.get("action")
                        if action == "auth":
                            if msg.get("token") == "ephemeral-ws-token-999":
                                await ws.send_json({"event": "authenticated"})
                            else:
                                await ws.send_json({"event": "error", "message": "Authentication failed"})
                                await ws.close(code=4401)
                                return
                        elif action == "submit":
                            await ws.send_json({"event": "task_created", "task_id": "t-2c-test", "conversation_id": "c-2c"})
                            await ws.send_json({"event": "token", "text": "Hello from Carbon!"})
                            await ws.send_json({"event": "completed"})

                uvicorn.run(app, host="127.0.0.1", port=\(port), log_level="warning")
                """

                let serverLauncher = DefaultProcessLauncher()
                let serverProc = try serverLauncher.launchProcess(
                    executableURL: realPython,
                    arguments: ["-c", serverScript],
                    currentDirectoryURL: serverDir,
                    environment: nil
                )
                defer {
                    serverProc.terminate()
                }

                // Wait briefly for ephemeral FastAPI/uvicorn test server to bind
                let epBase = URL(string: "http://127.0.0.1:\(port)")!
                let epWS = URL(string: "ws://127.0.0.1:\(port)/ws/chat")!
                let client = CarbonClient(autoConnect: false, projectRoot: serverDir, baseURL: epBase, wsURL: epWS)
                let supervisor = ProcessSupervisor(
                    projectRoot: serverDir,
                    ollamaBaseURL: epBase,
                    backendBaseURL: epBase
                )

                // Wait until ephemeral test server accepts TCP before running startup
                for _ in 0..<40 {
                    if await supervisor.verifyBackendHealth() == .healthy {
                        break
                    }
                    try? await Task.sleep(nanoseconds: 100_000_000)
                }

                try await supervisor.startup(client: client, connectTimeout: 5.0)
                assert(supervisor.startupState == .ready)
                assert(client.isConnected)
                assert(client.connectionStatus == .connected)

                // Test existing chat functionality after startup
                client.sendMessage("Ping")
                for _ in 0..<40 {
                    if !client.isGenerating {
                        break
                    }
                    try? await Task.sleep(nanoseconds: 50_000_000)
                }
                assert(!client.isGenerating, "Generation should complete")
                assert(client.messages.last?.text == "Hello from Carbon!", "Expected streamed chat reply, got: \(client.messages.last?.text ?? "nil")")

                client.disconnectWebSocket()
                _ = await serverProc.terminateAndAwaitExit(timeout: 2.0)
                print("       ✓ End-to-end startup -> WebSocket auth -> ready -> live chat verified on ephemeral loopback port \(port)")
            } else {
                print("       (Skipped live ephemeral WS server test: .venv/bin/python not present at cwd)")
            }
        } catch {
            assertionFailure("Test 13 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 14: Never transitions to .ready while WebSocket auth is pending; failed retry never duplicates WS/process
        // ------------------------------------------------------------------
        print("[14/16] Verifying Carbon never enters .ready while WebSocket auth is pending, and Retry avoids duplicates...")
        do {
            let latestBackend = AtomicValue<TestMockProcess?>(nil)
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    if let p = latestBackend.get(), p.isRunning {
                        return (jsonData(healthyBackendPayload), 200)
                    }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            let launchIdx = AtomicValue(0)
            launcher.setFactory { _, _ in
                let idx = launchIdx.mutate { v -> Int in v += 1; return v }
                let proc = TestMockProcess(processIdentifier: Int32(1900 + idx))
                latestBackend.set(proc)
                return proc
            }

            let client = TestMockCarbonClient()
            // First attempt: WebSocket opens/starts connecting, never receives "authenticated"
            client.onConnect = { c in
                c.isConnected = false
            }

            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            let firstAttempt = Task {
                try await supervisor.startup(client: client, connectTimeout: 0.25)
            }
            try? await Task.sleep(nanoseconds: 120_000_000)
            // While connectWebSocket() has been called but auth has NOT finished, state MUST be .connecting (never .ready!)
            if client.connectCallCount == 1 && !client.isConnected {
                assert(supervisor.startupState == .connecting, "Must stay .connecting while auth pending, got \(supervisor.startupState)")
                assert(!supervisor.stateTransitionHistory.contains(.ready), "Must NEVER enter .ready before authentication!")
            }

            do {
                try await firstAttempt.value
                assertionFailure("Expected WebSocket timeout failure")
            } catch let err as ProcessSupervisorError {
                if case .webSocketConnectionFailed = err {
                    assert(supervisor.startupState == .failed)
                    assert(!supervisor.stateTransitionHistory.contains(.ready))
                } else {
                    assertionFailure("Expected webSocketConnectionFailed, got \(err)")
                }
            }

            // Now Retry cleanly succeeds without leaving stale duplicate connections
            client.onConnect = { c in c.isConnected = true }
            try await supervisor.startup(client: client, connectTimeout: 1.0)
            assert(supervisor.startupState == .ready)
            assert(client.connectCallCount == 2, "Expected exactly 1 connect attempt per startup run (2 total)")
            assert(launcher.launches.count == 2, "First owned child exited on cleanup; second Retry launched 1 clean backend")
            print("       ✓ Confirmed .connecting held until auth, failed timeout never reported .ready, and Retry succeeded cleanly")
        } catch {
            assertionFailure("Test 14 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 15: Owned child ignoring SIGTERM preserves .owned and Retry reuses without duplicate launch
        // ------------------------------------------------------------------
        print("[15/16] Verifying Retry reuses still-running owned child after termination timeout without duplicate launch...")
        do {
            let backendHealthy = AtomicValue(false)
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    if backendHealthy.get() { return (jsonData(healthyBackendPayload), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            let slowUncooperativeBackend = TestMockProcess(processIdentifier: 2001, isRunning: true)
            slowUncooperativeBackend.ignoreTerminate = true // Ignores SIGTERM on first timeout!
            launcher.setFactory { _, _ in slowUncooperativeBackend }

            let client = TestMockCarbonClient()
            client.onConnect = { c in c.isConnected = true }

            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            // Attempt 1: times out; terminateAndAwaitExit returns .timedOut; child stays alive and .owned!
            do {
                try await supervisor.startup(client: client, backendTimeout: 0.2)
                assertionFailure("Expected backendTimeout")
            } catch let err as ProcessSupervisorError {
                assert(err == .backendTimeout)
                assert(supervisor.startupState == .failed)
                assert(supervisor.backendOwnership == .owned, "Termination timeout MUST preserve .owned ownership!")
                assert(supervisor.childBackendProcess === slowUncooperativeBackend, "Child handle MUST remain retained!")
                assert(supervisor.ollamaOwnership == .external, "External Ollama MUST remain untouched!")
                assert(launcher.launches.count == 1)
            }

            // Now the still-running owned child finishes warming up before user clicks Retry
            backendHealthy.set(true)

            // Attempt 2 (Retry): Step 0 sees _childBackendProcess still running, probes it, finds it healthy, reuses WITHOUT spawning duplicate!
            try await supervisor.startup(client: client, backendTimeout: 1.0)
            assert(supervisor.startupState == .ready)
            assert(supervisor.backendOwnership == .owned)
            assert(supervisor.childBackendProcess === slowUncooperativeBackend)
            assert(launcher.launches.count == 1, "Must NOT spawn duplicate child when owned child is still running!")
            assert(client.connectCallCount == 1)
            print("       ✓ Still-running owned child preserved on timeout and safely reused on Retry with 0 duplicate spawns")
        } catch {
            assertionFailure("Test 15 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 16: Retry when uncooperative owned child remains unhealthy does not spawn duplicate
        // ------------------------------------------------------------------
        print("[16/16] Verifying Retry against still-stuck owned child never spawns duplicate and preserves .owned...")
        do {
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            let stuckBackend = TestMockProcess(processIdentifier: 2101, isRunning: true)
            stuckBackend.ignoreTerminate = true
            launcher.setFactory { _, _ in stuckBackend }

            let client = TestMockCarbonClient()
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            // Attempt 1
            do {
                try await supervisor.startup(client: client, backendTimeout: 0.15)
                assertionFailure("Expected backendTimeout")
            } catch {}

            // Attempt 2 (Retry while stuckBackend is STILL running and unhealthy)
            do {
                try await supervisor.startup(client: client, backendTimeout: 0.15)
                assertionFailure("Expected backendTimeout on second attempt")
            } catch let err as ProcessSupervisorError {
                assert(err == .backendTimeout)
                assert(supervisor.startupState == .failed)
                assert(supervisor.backendOwnership == .owned)
                assert(supervisor.childBackendProcess === stuckBackend)
                assert(launcher.launches.count == 1, "Still-stuck owned child must block spawning conflicting duplicate")
                assert(supervisor.ollamaOwnership == .external, "External Ollama untouched across multiple failures")
                print("       ✓ Repeated failure with uncooperative owned child prevented duplicate spawn and kept .owned")
            }
        } catch {
            assertionFailure("Test 16 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 17 (F1): Cancelling startup awaits real subprocess SIGTERM exit before returning & preserves external Ollama
        // ------------------------------------------------------------------
        print("[17/19] Verifying F1: startup cancellation awaits real child SIGTERM exit cleanly while preserving external Ollama...")
        do {
            let pythonExec = URL(fileURLWithPath: "/usr/bin/python3")
            let delayedExitScript = """
            import signal, sys, time
            def handler(signum, frame):
                time.sleep(0.22)
                sys.exit(0)
            signal.signal(signal.SIGTERM, handler)
            time.sleep(5.0)
            """
            let realLauncher = DefaultProcessLauncher()
            let childHolder = AtomicValue<(any ProcessRunning)?>(nil)

            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            launcher.setFactory { _, _ in
                let proc = try! realLauncher.launchProcess(
                    executableURL: pythonExec,
                    arguments: ["-c", delayedExitScript],
                    currentDirectoryURL: nil,
                    environment: nil
                )
                childHolder.set(proc)
                return proc
            }

            let client = TestMockCarbonClient()
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            let startTask = Task {
                try await supervisor.startup(client: client, backendTimeout: 5.0)
            }

            var reachedWait = false
            for _ in 0..<100 {
                if supervisor.startupState == .waitingForBackend, let proc = childHolder.get(), proc.isRunning {
                    reachedWait = true
                    break
                }
                try? await Task.sleep(nanoseconds: 20_000_000)
            }
            assert(reachedWait, "Must reach .waitingForBackend with real child running")
            // Give python process a moment to install SIGTERM handler
            try? await Task.sleep(nanoseconds: 80_000_000)

            let cancelStart = Date()
            supervisor.cancelStartup()

            do {
                try await startTask.value
                assertionFailure("Expected startupCancelled error")
            } catch let err as ProcessSupervisorError {
                let elapsed = Date().timeIntervalSince(cancelStart)
                let proc = childHolder.get()!
                assert(err == .startupCancelled, "Expected .startupCancelled, got \(err)")
                assert(elapsed >= 0.15, "Rollback MUST wait for ~0.22s SIGTERM exit instead of returning prematurely (\(elapsed)s)")
                assert(!proc.isRunning, "Real child process MUST already be dead before startup() returns")
                assert(supervisor.backendOwnership == .notRunning, "Ownership must be .notRunning immediately on return, got \(supervisor.backendOwnership)")
                assert(supervisor.childBackendProcess == nil, "Child handle must be cleared after confirmed exit")
                assert(!supervisor.hasActiveStartupCleanupTask, "Startup cleanup task tracking must be cleared")
                assert(supervisor.ollamaOwnership == .external, "External Ollama MUST remain untouched")
                assert(!client.isConnected && !client.autoReconnectEnabled)
                if let managed = proc as? ManagedProcess {
                    assert(managed.stdoutDrainer?.hasHandlerAttached == false, "Pipes should close after confirmed exit")
                }
                print("       ✓ F1 verified: rollback waited \(String(format: "%.2f", elapsed))s for confirmed exit and cleared .owned immediately")
            }
        } catch {
            assertionFailure("Test 17 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 18 (F1): Uncooperative SIGTERM-ignoring real child retains .owned, handle, pipes & blocks duplicate retry
        // ------------------------------------------------------------------
        print("[18/19] Verifying F1 timeout: SIGTERM-ignoring child retains .owned/handle/pipes on rollback & blocks duplicate retry...")
        do {
            let pythonExec = URL(fileURLWithPath: "/usr/bin/python3")
            let ignoreSigtermScript = """
            import signal, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(3.5)
            """
            let realLauncher = DefaultProcessLauncher()
            let childHolder = AtomicValue<(any ProcessRunning)?>(nil)

            let backendHealthy = AtomicValue(false)
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    if backendHealthy.get() { return (jsonData(healthyBackendPayload), 200) }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            launcher.setFactory { _, _ in
                let proc = try! realLauncher.launchProcess(
                    executableURL: pythonExec,
                    arguments: ["-c", ignoreSigtermScript],
                    currentDirectoryURL: nil,
                    environment: nil
                )
                childHolder.set(proc)
                return proc
            }

            let client = TestMockCarbonClient()
            client.onConnect = { c in c.isConnected = true }
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)

            let startTask = Task {
                try await supervisor.startup(client: client, backendTimeout: 5.0)
            }

            var launched = false
            for _ in 0..<100 {
                if supervisor.startupState == .waitingForBackend, childHolder.get() != nil {
                    launched = true
                    break
                }
                try? await Task.sleep(nanoseconds: 20_000_000)
            }
            assert(launched)
            try? await Task.sleep(nanoseconds: 80_000_000)

            let cancelStart = Date()
            supervisor.cancelStartup()

            do {
                try await startTask.value
                assertionFailure("Expected startupCancelled error")
            } catch let err as ProcessSupervisorError {
                let elapsed = Date().timeIntervalSince(cancelStart)
                let proc = childHolder.get()!
                assert(err == .startupCancelled)
                assert(elapsed >= 1.8, "Expected ~2.0s bounded rollback wait on uncooperative child, got \(elapsed)s")
                assert(proc.isRunning, "Uncooperative child must still be running")
                assert(supervisor.backendOwnership == .owned, "Still-running child MUST retain .owned")
                assert(supervisor.childBackendProcess === proc, "Handle MUST remain retained")
                assert(supervisor.ollamaOwnership == .external, "External Ollama untouched")
                if let managed = proc as? ManagedProcess {
                    assert(managed.stdoutDrainer?.hasHandlerAttached == true, "Pipes must remain open while child still runs")
                }
            }

            // Simulate backend warming up on the retained owned child, then Retry startup
            backendHealthy.set(true)
            try await supervisor.startup(client: client, backendTimeout: 2.0)
            assert(launcher.launches.count == 1, "Retry MUST reuse existing owned child and NEVER spawn duplicate!")
            assert(supervisor.startupState == .ready && client.isConnected)
            assert(supervisor.backendOwnership == .owned)
            assert(supervisor.childBackendProcess === childHolder.get()!)

            // Let child finish naturally and clean up cleanly (no SIGKILL)
            _ = await childHolder.get()!.awaitExit(timeout: 2.5)
            print("       ✓ F1 timeout & no-duplicate Retry verified cleanly with retained .owned/handle/pipes")
        } catch {
            assertionFailure("Test 18 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 19 (G1): ProcessSupervisorError.recoverySuggestion & UI startupDetailMessage parity
        // ------------------------------------------------------------------
        print("[19/19] Verifying G1: LocalizedError.recoverySuggestion optional conformance & UI startupDetailMessage...")
        do {
            let cases: [ProcessSupervisorError] = [
                .ollamaExecutableNotFound,
                .ollamaStartupFailed("boom"),
                .ollamaTimeout,
                .modelNotFound("qwen3.5:9b"),
                .pythonExecutableNotFound("/missing/.venv/bin/python"),
                .backendStartupFailed("boom"),
                .backendHealthDegraded("HTTP status 503"),
                .backendTimeout,
                .authenticationFailed("bad token"),
                .webSocketConnectionFailed("refused"),
                .startupCancelled
            ]
            for errCase in cases {
                let optErr: ProcessSupervisorError? = errCase
                let asLocalized: any LocalizedError = errCase
                assert(errCase.recoverySuggestion != nil && !(errCase.recoverySuggestion ?? "").isEmpty)
                assert(optErr?.recoverySuggestion == errCase.recoverySuggestion, "Optional chaining must return recovery text")
                assert(asLocalized.recoverySuggestion == errCase.recoverySuggestion, "LocalizedError witness must return recovery text")
                assert(errCase.errorDescription != nil && !(errCase.errorDescription ?? "").isEmpty)
            }

            // Verify live supervisor failure populates both startupError?.recoverySuggestion and startupDetailMessage
            let http = TestMockHTTPClient()
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                if url.path.hasSuffix("api/v1/health") { return (Data("{\"status\":\"degraded\"}".utf8), 503) }
                throw URLError(.cannotConnectToHost)
            }
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: TestMockLauncher())
            let client = TestMockCarbonClient()
            do {
                try await supervisor.startup(client: client)
                assertionFailure("Expected degraded health error")
            } catch let err as ProcessSupervisorError {
                assert(err == .backendHealthDegraded("HTTP status 503"))
            }

            assert(supervisor.startupState == .failed)
            assert(supervisor.startupError?.recoverySuggestion != nil, "startupError?.recoverySuggestion MUST NOT be nil")
            assert(supervisor.startupDetailMessage == supervisor.startupError?.recoverySuggestion, "UI startupDetailMessage must match recoverySuggestion")
            assert(supervisor.startupDetailMessage?.contains("8765") == true)
            print("       ✓ G1 verified: optional recoverySuggestion and UI startupDetailMessage populated cleanly")
        } catch {
            assertionFailure("Test 19 failed: \(error)")
        }

        // ------------------------------------------------------------------
        // TEST 20 (Stage 4.3A Fix 1): startup() default backendTimeout == 75.0s & slow warmup
        // ------------------------------------------------------------------
        print("[20/20] Verifying startup() uses 75s defaultBackendTimeout and succeeds after delayed pre-warm...")
        do {
            assert(ProcessSupervisor.defaultModelLoadTimeout == 60.0)
            assert(ProcessSupervisor.defaultBackendTimeout == 75.0)

            let http = TestMockHTTPClient()
            let healthAttempt = AtomicValue(0)
            http.setHandler { url in
                if url.path.hasSuffix("api/version") { return (jsonData(["version": "0.33.0"]), 200) }
                if url.path.hasSuffix("api/tags") { return (jsonData(healthyTagsPayload), 200) }
                if url.path.hasSuffix("api/v1/health") {
                    let n = healthAttempt.mutate { c -> Int in
                        c += 1
                        return c
                    }
                    if n >= 6 {
                        return (jsonData(healthyBackendPayload), 200)
                    }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = TestMockLauncher()
            let proc = TestMockProcess(processIdentifier: 2201, isRunning: true)
            launcher.setFactory { _, _ in proc }
            let client = TestMockCarbonClient()
            client.onConnect = { c in c.isConnected = true }

            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: http, processLauncher: launcher)
            // Call startup with default backendTimeout
            try await supervisor.startup(client: client)
            assert(supervisor.startupState == .ready)
            assert(launcher.launches.count == 1)
            assert(proc.terminateCallCount == 0)
            assert(healthAttempt.get() >= 6)
            print("       ✓ startup() default 75.0s backendTimeout succeeded cleanly across delayed warmup")
        } catch {
            assertionFailure("Test 20 failed: \(error)")
        }

        print("\n=== ALL 20 INCREMENT 2C/2E STARTUP STATE MACHINE TESTS PASSED SUCCESSFULLY ===")
    }
}
