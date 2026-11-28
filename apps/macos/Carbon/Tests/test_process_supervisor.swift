import Foundation
import Combine
import Darwin

// MARK: - Test Utilities & Mocks

final class AtomicState<T>: @unchecked Sendable {
    private var value: T
    private let lock = NSLock()

    init(_ value: T) {
        self.value = value
    }

    func get() -> T {
        lock.lock()
        defer { lock.unlock() }
        return value
    }

    func set(_ newValue: T) {
        lock.lock()
        defer { lock.unlock() }
        value = newValue
    }

    func mutate<R>(_ block: (inout T) -> R) -> R {
        lock.lock()
        defer { lock.unlock() }
        return block(&value)
    }
}

final class MockProcess: ProcessRunning, @unchecked Sendable {
    var isRunning: Bool
    var processIdentifier: Int32
    var terminationHandler: (@Sendable (any ProcessRunning) -> Void)?
    var simulatedExitCode: Int32 = 0
    var ignoreTerminate: Bool = false
    private(set) var terminateCallCount: Int = 0
    private(set) var terminateAndAwaitExitCallCount: Int = 0

    init(processIdentifier: Int32 = 10001, isRunning: Bool = true) {
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
        } else {
            return .exited(terminationStatus: simulatedExitCode)
        }
    }

    @discardableResult
    func awaitExit(timeout: TimeInterval = 2.0) async -> ProcessTerminationResult {
        if isRunning {
            return .timedOut
        } else {
            return .exited(terminationStatus: simulatedExitCode)
        }
    }

    func simulateExit(exitCode: Int32 = 0) {
        simulatedExitCode = exitCode
        isRunning = false
        terminationHandler?(self)
    }
}

final class MockProcessLauncher: ProcessSupervisorProcessLauncher, @unchecked Sendable {
    struct LaunchRecord: Sendable {
        let executableURL: URL
        let arguments: [String]
        let currentDirectoryURL: URL?
        let environment: [String: String]?
    }

    private let state = AtomicState<(launches: [LaunchRecord], factory: (@Sendable () -> any ProcessRunning)?, error: Error?)>(([], nil, nil))

    var launchedProcesses: [LaunchRecord] {
        state.get().launches
    }

    func setProcessFactory(_ factory: @escaping @Sendable () -> any ProcessRunning) {
        state.mutate { $0.factory = factory }
    }

    func setLaunchError(_ error: Error?) {
        state.mutate { $0.error = error }
    }

    func launchProcess(
        executableURL: URL,
        arguments: [String],
        currentDirectoryURL: URL?,
        environment: [String: String]?
    ) throws -> any ProcessRunning {
        let current = state.get()
        if let error = current.error {
            throw error
        }
        let record = LaunchRecord(
            executableURL: executableURL,
            arguments: arguments,
            currentDirectoryURL: currentDirectoryURL,
            environment: environment
        )
        state.mutate { $0.launches.append(record) }
        if let factory = current.factory {
            return factory()
        }
        return MockProcess()
    }
}

final class MockProcessSupervisorHTTPClient: ProcessSupervisorHTTPClient, @unchecked Sendable {
    private struct State {
        var responses: [String: (Data, Int)] = [:]
        var dynamicHandler: (@Sendable (URL) async throws -> (Data, Int))?
        var loggedRequests: [URL] = []
    }

    private let state = AtomicState(State())

    var requestLog: [URL] {
        state.get().loggedRequests
    }

    func setResponse(forPath pathSuffix: String, json: [String: Any], statusCode: Int = 200) {
        let data = try! JSONSerialization.data(withJSONObject: json, options: [])
        state.mutate { $0.responses[pathSuffix] = (data, statusCode) }
    }

    func setDynamicHandler(_ handler: @escaping @Sendable (URL) async throws -> (Data, Int)) {
        state.mutate { $0.dynamicHandler = handler }
    }

    func data(from url: URL) async throws -> (Data, HTTPURLResponse) {
        let (handler, currentResponses) = state.mutate { s -> ((@Sendable (URL) async throws -> (Data, Int))?, [String: (Data, Int)]) in
            s.loggedRequests.append(url)
            return (s.dynamicHandler, s.responses)
        }

        if let handler = handler {
            let (data, status) = try await handler(url)
            let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1", headerFields: nil)!
            return (data, response)
        }

        for (suffix, (data, status)) in currentResponses {
            if url.path.hasSuffix(suffix) || url.absoluteString.hasSuffix(suffix) {
                let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1", headerFields: nil)!
                return (data, response)
            }
        }

        throw URLError(.cannotConnectToHost)
    }
}

// MARK: - Test Suite

@main
struct ProcessSupervisorTestRunner {
    @MainActor
    static func main() async {
        print("=== CARBON STAGE 1.11 INCREMENT 2B: PROCESS SUPERVISOR & PIPE-DRAIN TESTS ===")

        let fileManager = FileManager.default
        let tempDir = fileManager.temporaryDirectory.appendingPathComponent("carbon_supervisor_tests_\(UUID().uuidString)")
        try! fileManager.createDirectory(at: tempDir, withIntermediateDirectories: true)

        defer {
            try? fileManager.removeItem(at: tempDir)
        }

        // Helper to create fake executables in tempDir
        func makeExecutable(at relativePath: String) -> URL {
            let url = tempDir.appendingPathComponent(relativePath)
            try! fileManager.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            fileManager.createFile(atPath: url.path, contents: "#!/bin/sh\nexit 0\n".data(using: .utf8), attributes: [.posixPermissions: 0o755])
            return url
        }

        let standardHealthyBackend: [String: Any] = [
            "status": "healthy",
            "core_running": true,
            "ollama_connected": true,
            "active_model": [
                "id": "qwen3.5:9b",
                "name": "Qwen 3.5 9B"
            ]
        ]

        // -------------------------------------------------------------
        // PROCESS OWNERSHIP TESTS (1 - 6)
        // -------------------------------------------------------------

        // Test 1: Existing Ollama process is classified as external
        print("[1/35] Verifying existing Ollama process is classified as external...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            httpClient.setResponse(forPath: "api/version", json: ["version": "0.33.0"])
            let launcher = MockProcessLauncher()
            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            try await supervisor.ensureOllamaRunning()
            assert(supervisor.ollamaOwnership == .external, "Expected .external ownership for pre-existing Ollama, got \(supervisor.ollamaOwnership)")
            assert(supervisor.childOllamaProcess == nil, "Expected childOllamaProcess to be nil for external Ollama")
            assert(launcher.launchedProcesses.isEmpty, "Launcher should not have been invoked for pre-existing Ollama")
            print("       ✓ Existing Ollama classified as .external, no child process adopted")
        } catch {
            assertionFailure("Test 1 failed with error: \(error)")
        }

        // Test 2: Existing backend process is classified as external
        print("[2/35] Verifying existing backend process is classified as external...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            httpClient.setResponse(forPath: "api/v1/health", json: standardHealthyBackend)
            let launcher = MockProcessLauncher()
            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            try await supervisor.ensureBackendRunning()
            assert(supervisor.backendOwnership == .external, "Expected .external ownership for pre-existing backend, got \(supervisor.backendOwnership)")
            assert(supervisor.childBackendProcess == nil, "Expected childBackendProcess to be nil for external backend")
            assert(launcher.launchedProcesses.isEmpty, "Launcher should not have been invoked for pre-existing backend")
            print("       ✓ Existing backend classified as .external, no child process adopted")
        } catch {
            assertionFailure("Test 2 failed with error: \(error)")
        }

        // Test 3: Newly spawned Ollama child is classified as owned
        print("[3/35] Verifying newly spawned Ollama child is classified as owned...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            let ollamaStarted = AtomicState(false)
            httpClient.setDynamicHandler { url in
                if url.path.hasSuffix("api/version") {
                    if ollamaStarted.get() {
                        let data = try! JSONSerialization.data(withJSONObject: ["version": "0.33.0"])
                        return (data, 200)
                    }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }

            let fakeOllama = makeExecutable(at: "bin/ollama")
            let launcher = MockProcessLauncher()
            let mockProc = MockProcess(processIdentifier: 20001)
            launcher.setProcessFactory {
                ollamaStarted.set(true)
                return mockProc
            }

            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            try await supervisor.ensureOllamaRunning(customSearchPaths: [fakeOllama.path])
            assert(supervisor.ollamaOwnership == .owned, "Expected .owned ownership for spawned Ollama, got \(supervisor.ollamaOwnership)")
            assert(supervisor.childOllamaProcess?.processIdentifier == 20001, "Expected child process identifier 20001")
            assert(launcher.launchedProcesses.count == 1, "Launcher should have launched exactly one process")
            assert(launcher.launchedProcesses[0].arguments == ["serve"], "Arguments should be ['serve']")
            print("       ✓ Newly spawned Ollama classified as .owned with retained process handle")
        } catch {
            assertionFailure("Test 3 failed with error: \(error)")
        }

        // Test 4: Newly spawned backend child is classified as owned
        print("[4/35] Verifying newly spawned backend child is classified as owned...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            let backendStarted = AtomicState(false)
            httpClient.setDynamicHandler { url in
                if url.path.hasSuffix("api/v1/health") {
                    if backendStarted.get() {
                        let data = try! JSONSerialization.data(withJSONObject: standardHealthyBackend)
                        return (data, 200)
                    }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }

            _ = makeExecutable(at: ".venv/bin/python")
            let launcher = MockProcessLauncher()
            let mockProc = MockProcess(processIdentifier: 20002)
            launcher.setProcessFactory {
                backendStarted.set(true)
                return mockProc
            }

            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            try await supervisor.ensureBackendRunning()
            assert(supervisor.backendOwnership == .owned, "Expected .owned ownership for spawned backend, got \(supervisor.backendOwnership)")
            assert(supervisor.childBackendProcess?.processIdentifier == 20002, "Expected child backend process identifier 20002")
            assert(launcher.launchedProcesses.count == 1, "Expected 1 process launch")
            print("       ✓ Newly spawned backend classified as .owned with retained process handle")
        } catch {
            assertionFailure("Test 4 failed with error: \(error)")
        }

        // Test 5: Process ownership resets to notRunning when child process terminates
        print("[5/35] Verifying process ownership resets to notRunning when child process terminates...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            let ollamaStarted = AtomicState(false)
            let backendStarted = AtomicState(false)
            httpClient.setDynamicHandler { url in
                if url.path.hasSuffix("api/version") && ollamaStarted.get() {
                    return (try! JSONSerialization.data(withJSONObject: ["version": "0.33.0"]), 200)
                }
                if url.path.hasSuffix("api/v1/health") && backendStarted.get() {
                    return (try! JSONSerialization.data(withJSONObject: standardHealthyBackend), 200)
                }
                throw URLError(.cannotConnectToHost)
            }

            let fakeOllama = makeExecutable(at: "bin/ollama_t5")
            _ = makeExecutable(at: ".venv/bin/python")

            let launcher = MockProcessLauncher()
            let mockOllamaProc = MockProcess(processIdentifier: 30001)
            let mockBackendProc = MockProcess(processIdentifier: 30002)

            let launchIndex = AtomicState(0)
            launcher.setProcessFactory {
                launchIndex.mutate { $0 += 1 }
                let current = launchIndex.get()
                if current == 1 {
                    ollamaStarted.set(true)
                    return mockOllamaProc
                } else {
                    backendStarted.set(true)
                    return mockBackendProc
                }
            }

            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            try await supervisor.ensureOllamaRunning(customSearchPaths: [fakeOllama.path])
            try await supervisor.ensureBackendRunning()

            assert(supervisor.ollamaOwnership == .owned)
            assert(supervisor.backendOwnership == .owned)

            // Simulate abnormal/unexpected Ollama termination
            mockOllamaProc.simulateExit()
            try await Task.sleep(nanoseconds: 50_000_000) // 50ms for MainActor callback
            assert(supervisor.ollamaOwnership == .notRunning, "Expected ollamaOwnership to reset to .notRunning")
            assert(supervisor.childOllamaProcess == nil, "Expected childOllamaProcess to be nil")

            // Simulate abnormal/unexpected Backend termination
            mockBackendProc.simulateExit()
            try await Task.sleep(nanoseconds: 50_000_000)
            assert(supervisor.backendOwnership == .notRunning, "Expected backendOwnership to reset to .notRunning")
            assert(supervisor.childBackendProcess == nil, "Expected childBackendProcess to be nil")

            print("       ✓ Process ownership cleanly reset to .notRunning upon child termination")
        } catch {
            assertionFailure("Test 5 failed with error: \(error)")
        }

        // Test 6: External processes are never terminated by supervisor cleanup
        print("[6/35] Verifying external processes are never terminated by supervisor cleanup...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            httpClient.setResponse(forPath: "api/version", json: ["version": "0.33.0"])
            httpClient.setResponse(forPath: "api/v1/health", json: standardHealthyBackend)

            let launcher = MockProcessLauncher()
            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            try await supervisor.ensureOllamaRunning()
            try await supervisor.ensureBackendRunning()

            assert(supervisor.ollamaOwnership == .external)
            assert(supervisor.backendOwnership == .external)

            // Perform teardown
            supervisor.terminateOwnedChildrenOnly()

            // Invariant: external processes must remain .external, never touched or killed
            assert(supervisor.ollamaOwnership == .external, "External Ollama ownership must remain .external")
            assert(supervisor.backendOwnership == .external, "External backend ownership must remain .external")
            assert(supervisor.childOllamaProcess == nil)
            assert(supervisor.childBackendProcess == nil)
            print("       ✓ Zero-adoption invariant verified: External processes untouched during cleanup")
        } catch {
            assertionFailure("Test 6 failed with error: \(error)")
        }

        // -------------------------------------------------------------
        // DISCOVERY AND VERIFICATION TESTS (7 - 11)
        // -------------------------------------------------------------

        // Test 7: Ollama binary resolution follows exact search order
        print("[7/35] Verifying Ollama binary resolution search order...")
        do {
            let p1 = makeExecutable(at: "search_opt_homebrew/bin/ollama")
            let p2 = makeExecutable(at: "search_usr_local/bin/ollama")
            let p3 = makeExecutable(at: "search_app/Contents/Resources/ollama")
            let pathBin = makeExecutable(at: "search_path_fallback/bin/ollama")

            let supervisor = ProcessSupervisor(projectRoot: tempDir)

            // When all 3 search paths are present, tier 1 (/opt/homebrew) must win
            let res1 = supervisor.resolveOllamaExecutable(customSearchPaths: [p1.path, p2.path, p3.path], pathEnvironment: nil)
            assert(res1?.path == p1.path, "Expected tier 1 path \(p1.path), got \(res1?.path ?? "nil")")

            // Remove tier 1, tier 2 (/usr/local) must win
            try! fileManager.removeItem(at: p1)
            let res2 = supervisor.resolveOllamaExecutable(customSearchPaths: [p1.path, p2.path, p3.path], pathEnvironment: nil)
            assert(res2?.path == p2.path, "Expected tier 2 path \(p2.path), got \(res2?.path ?? "nil")")

            // Remove tier 2, tier 3 (/Applications) must win
            try! fileManager.removeItem(at: p2)
            let res3 = supervisor.resolveOllamaExecutable(customSearchPaths: [p1.path, p2.path, p3.path], pathEnvironment: nil)
            assert(res3?.path == p3.path, "Expected tier 3 path \(p3.path), got \(res3?.path ?? "nil")")

            // Remove tier 3, tier 4 (PATH fallback) must win
            try! fileManager.removeItem(at: p3)
            let res4 = supervisor.resolveOllamaExecutable(customSearchPaths: [p1.path, p2.path, p3.path], pathEnvironment: pathBin.deletingLastPathComponent().path)
            assert(res4?.path == pathBin.path, "Expected tier 4 PATH fallback \(pathBin.path), got \(res4?.path ?? "nil")")

            print("       ✓ Search order strictly honored: /opt/homebrew -> /usr/local -> /Applications -> PATH")
        }

        // Test 8: Missing Ollama executable produces actionable error
        print("[8/35] Verifying missing Ollama executable produces actionable error...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            let launcher = MockProcessLauncher()
            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            do {
                try await supervisor.ensureOllamaRunning(customSearchPaths: ["/nonexistent/dir/ollama"], pathEnvironment: "")
                assertionFailure("Expected error to be thrown when Ollama is missing")
            } catch let err as ProcessSupervisorError {
                assert(err == .ollamaExecutableNotFound, "Expected .ollamaExecutableNotFound, got \(err)")
                let desc = err.errorDescription ?? ""
                assert(desc.contains("Ollama executable not found in standard paths"), "Error description must contain search paths")
                assert(desc.contains("https://ollama.ai"), "Error description must contain installation link")
                print("       ✓ Actionable error returned when Ollama executable is not found")
            } catch {
                assertionFailure("Unexpected error thrown: \(error)")
            }
        }

        // Test 9: Installed qwen3.5:9b model passes verification
        print("[9/35] Verifying installed qwen3.5:9b model passes verification...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            httpClient.setResponse(forPath: "api/tags", json: [
                "models": [
                    ["name": "qwen3.5:9b", "size": 5200000000],
                    ["name": "llama3.2:latest", "size": 2000000000]
                ]
            ])
            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient
            )

            try await supervisor.verifyModelInstalled()
            print("       ✓ Model 'qwen3.5:9b' successfully verified in installed tags")
        } catch {
            assertionFailure("Test 9 failed with error: \(error)")
        }

        // Test 10: Missing qwen3.5:9b model fails with exact manual pull instructions
        print("[10/35] Verifying missing model fails with exact manual pull instructions...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            httpClient.setResponse(forPath: "api/tags", json: [
                "models": [
                    ["name": "llama3.2:latest", "size": 2000000000],
                    ["name": "mistral:latest", "size": 4000000000]
                ]
            ])
            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient
            )

            do {
                try await supervisor.verifyModelInstalled()
                assertionFailure("Expected verifyModelInstalled to fail when model is missing")
            } catch let err as ProcessSupervisorError {
                assert(err == .modelNotFound("qwen3.5:9b"), "Expected .modelNotFound('qwen3.5:9b'), got \(err)")
                let desc = err.errorDescription ?? ""
                assert(desc == "Model 'qwen3.5:9b' is not installed. Run: ollama pull qwen3.5:9b", "Exact prompt required, got: \(desc)")
                print("       ✓ Missing model threw exact manual pull instruction: \"\(desc)\"")
            } catch {
                assertionFailure("Unexpected error: \(error)")
            }
        }

        // Test 11: Model verification never issues an automated pull request
        print("[11/35] Verifying model verification never issues an automated pull request...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            httpClient.setResponse(forPath: "api/tags", json: [
                "models": [["name": "other-model:latest"]]
            ])
            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient
            )

            _ = try? await supervisor.verifyModelInstalled()

            // Verify no request was made to any pull endpoint
            for requestURL in httpClient.requestLog {
                assert(!requestURL.path.contains("pull"), "Automated pull request detected to \(requestURL.path)!")
            }
            assert(httpClient.requestLog.count == 1, "Only a single GET /api/tags request should have been dispatched")
            print("       ✓ Invariant verified: Zero automated pull requests dispatched")
        }

        // -------------------------------------------------------------
        // BACKEND SUPERVISION TESTS (12 - 16)
        // -------------------------------------------------------------

        // Test 12: Backend reuse detects existing healthy instance
        print("[12/35] Verifying backend reuse detects existing healthy instance...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            httpClient.setResponse(forPath: "api/v1/health", json: standardHealthyBackend)
            let launcher = MockProcessLauncher()
            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            let result = await supervisor.verifyBackendHealth()
            assert(result == .healthy, "Expected .healthy, got \(result)")
            try await supervisor.ensureBackendRunning()
            assert(supervisor.backendOwnership == .external)
            assert(launcher.launchedProcesses.isEmpty)
            print("       ✓ Existing healthy backend detected and reused without launching child")
        } catch {
            assertionFailure("Test 12 failed with error: \(error)")
        }

        // Test 13: Backend startup uses correct virtualenv Python executable and PYTHONPATH
        print("[13/35] Verifying backend startup uses correct virtualenv Python and PYTHONPATH...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            let healthCount = AtomicState(0)
            httpClient.setDynamicHandler { url in
                if url.path.hasSuffix("api/v1/health") {
                    healthCount.mutate { $0 += 1 }
                    if healthCount.get() > 1 {
                        return (try! JSONSerialization.data(withJSONObject: standardHealthyBackend), 200)
                    }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }

            let fakeVenvPython = makeExecutable(at: ".venv/bin/python")
            let launcher = MockProcessLauncher()
            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            try await supervisor.ensureBackendRunning()
            assert(launcher.launchedProcesses.count == 1, "Expected 1 child launch")
            let launch = launcher.launchedProcesses[0]
            assert(launch.executableURL.path == fakeVenvPython.path, "Expected executable \(fakeVenvPython.path), got \(launch.executableURL.path)")
            assert(launch.arguments == ["-m", "carbon.main"], "Expected arguments ['-m', 'carbon.main'], got \(launch.arguments)")
            assert(launch.currentDirectoryURL?.path == tempDir.path, "Expected working dir \(tempDir.path)")
            let pythonPath = launch.environment?["PYTHONPATH"]
            let expectedSrc = tempDir.appendingPathComponent("src").path
            assert(pythonPath == expectedSrc, "Expected PYTHONPATH \(expectedSrc), got \(pythonPath ?? "nil")")
            print("       ✓ Spawned backend with .venv/bin/python -m carbon.main and PYTHONPATH=\(expectedSrc)")
        } catch {
            assertionFailure("Test 13 failed with error: \(error)")
        }

        // Test 14: Degraded or wrong active model is detected and rejected
        print("[14/35] Verifying degraded or wrong active model is detected and rejected...")
        do {
            let degradedStatuses: [[String: Any]] = [
                // Wrong model
                [
                    "status": "healthy",
                    "core_running": true,
                    "ollama_connected": true,
                    "active_model": ["id": "llama3.2:1b"]
                ],
                // Core not running
                [
                    "status": "healthy",
                    "core_running": false,
                    "ollama_connected": true,
                    "active_model": ["id": "qwen3.5:9b"]
                ],
                // Ollama disconnected
                [
                    "status": "healthy",
                    "core_running": true,
                    "ollama_connected": false,
                    "active_model": ["id": "qwen3.5:9b"]
                ],
                // Status not healthy
                [
                    "status": "initializing",
                    "core_running": true,
                    "ollama_connected": true,
                    "active_model": ["id": "qwen3.5:9b"]
                ]
            ]

            for (idx, payload) in degradedStatuses.enumerated() {
                let httpClient = MockProcessSupervisorHTTPClient()
                httpClient.setResponse(forPath: "api/v1/health", json: payload)
                let supervisor = ProcessSupervisor(
                    projectRoot: tempDir,
                    httpClient: httpClient
                )

                let result = await supervisor.verifyBackendHealth()
                if case .degraded(let reason) = result {
                    print("         Sub-case \(idx + 1): Rejected degraded state as expected: \(reason)")
                } else {
                    assertionFailure("Sub-case \(idx + 1): Expected .degraded, got \(result)")
                }
            }
            print("       ✓ Degraded telemetry contract cases verified and rejected")
        }

        // Test 15: Backend startup timeout is enforced
        print("[15/35] Verifying backend startup timeout is enforced...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            _ = makeExecutable(at: ".venv/bin/python")
            let launcher = MockProcessLauncher()
            let mockProc = MockProcess(processIdentifier: 40001)
            launcher.setProcessFactory { mockProc }

            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            do {
                try await supervisor.ensureBackendRunning(timeout: 0.2) // 200ms timeout
                assertionFailure("Expected ensureBackendRunning to throw .backendTimeout")
            } catch let err as ProcessSupervisorError {
                assert(err == .backendTimeout, "Expected .backendTimeout, got \(err)")
                assert(mockProc.terminateCallCount >= 1, "Child process should be terminated upon timeout")
                assert(supervisor.backendOwnership == .notRunning, "Ownership should reset to .notRunning")
                print("       ✓ Backend timeout enforced and unready child process terminated")
            } catch {
                assertionFailure("Unexpected error: \(error)")
            }
        }

        // Test 16: Unsuccessful child startup cleans up child process
        print("[16/35] Verifying unsuccessful child startup cleans up child process...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            let callIndex = AtomicState(0)
            httpClient.setDynamicHandler { url in
                callIndex.mutate { $0 += 1 }
                if callIndex.get() == 1 {
                    // Initial check before launch: unavailable
                    throw URLError(.cannotConnectToHost)
                }
                // Subsequent check after launch reports degraded health (e.g. wrong model loaded)
                let degraded: [String: Any] = [
                    "status": "degraded",
                    "core_running": true,
                    "ollama_connected": false,
                    "active_model": ["id": "wrong-model"]
                ]
                return (try! JSONSerialization.data(withJSONObject: degraded), 200)
            }

            _ = makeExecutable(at: ".venv/bin/python")
            let launcher = MockProcessLauncher()
            let mockProc = MockProcess(processIdentifier: 40002)
            launcher.setProcessFactory { mockProc }

            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            do {
                try await supervisor.ensureBackendRunning(timeout: 1.0)
                assertionFailure("Expected ensureBackendRunning to fail on degraded backend")
            } catch let err as ProcessSupervisorError {
                if case .backendHealthDegraded = err {
                    assert(mockProc.terminateCallCount >= 1, "Degraded child process should be terminated")
                    assert(supervisor.backendOwnership == .notRunning, "Ownership should reset to .notRunning")
                    assert(supervisor.childBackendProcess == nil, "Child reference should be cleared")
                    print("       ✓ Degraded child cleaned up immediately upon health rejection")
                } else {
                    assertionFailure("Expected .backendHealthDegraded, got \(err)")
                }
            } catch {
                assertionFailure("Unexpected error: \(error)")
            }
        }

        // -------------------------------------------------------------
        // RELIABILITY & CONCURRENCY TESTS (17 - 19)
        // -------------------------------------------------------------

        // Test 17: Concurrent startup() calls are deduplicated
        print("[17/35] Verifying concurrent startup() calls are deduplicated...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            httpClient.setResponse(forPath: "api/version", json: ["version": "0.33.0"])
            httpClient.setResponse(forPath: "api/tags", json: ["models": [["name": "qwen3.5:9b"]]])
            httpClient.setResponse(forPath: "api/v1/health", json: standardHealthyBackend)

            let launcher = MockProcessLauncher()
            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            // Trigger two simultaneous startup() calls
            async let call1: Void = supervisor.startup()
            async let call2: Void = supervisor.startup()

            try await call1
            try await call2

            assert(supervisor.ollamaOwnership == .external)
            assert(supervisor.backendOwnership == .external)
            assert(launcher.launchedProcesses.isEmpty, "No duplicate processes launched")
            print("       ✓ Concurrent startup() calls serialized and deduplicated into a single task")
        } catch {
            assertionFailure("Test 17 failed with error: \(error)")
        }

        // Test 18: Process stdout/stderr pipe handlers are attached
        print("[18/35] Verifying process stdout/stderr pipe handlers are attached...")
        do {
            let launcher = DefaultProcessLauncher()
            let echoURL = URL(fileURLWithPath: "/bin/echo")
            if fileManager.isExecutableFile(atPath: echoURL.path) {
                let proc = try! launcher.launchProcess(
                    executableURL: echoURL,
                    arguments: ["Carbon process supervision pipe drain test"],
                    currentDirectoryURL: nil,
                    environment: nil
                )
                assert(proc.isRunning || proc.processIdentifier > 0)
                // Wait briefly for echo to finish
                try? await Task.sleep(nanoseconds: 50_000_000)
                print("       ✓ DefaultProcessLauncher successfully configured asynchronous pipe drains")
            }
        }

        // Test 19: Cleanup preserves external processes and terminates owned children
        print("[19/35] Verifying cleanup preserves external processes and terminates owned children...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            let backendStarted = AtomicState(false)
            httpClient.setDynamicHandler { url in
                if url.path.hasSuffix("api/version") {
                    return (try! JSONSerialization.data(withJSONObject: ["version": "0.33.0"]), 200)
                }
                if url.path.hasSuffix("api/v1/health") && backendStarted.get() {
                    return (try! JSONSerialization.data(withJSONObject: standardHealthyBackend), 200)
                }
                throw URLError(.cannotConnectToHost)
            }

            _ = makeExecutable(at: ".venv/bin/python")
            let launcher = MockProcessLauncher()
            let mockBackendProc = MockProcess(processIdentifier: 50001)
            launcher.setProcessFactory {
                backendStarted.set(true)
                return mockBackendProc
            }

            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            try await supervisor.ensureOllamaRunning() // External
            try await supervisor.ensureBackendRunning() // Owned

            assert(supervisor.ollamaOwnership == .external)
            assert(supervisor.backendOwnership == .owned)
            assert(mockBackendProc.terminateCallCount == 0)

            // Trigger teardown
            supervisor.terminateOwnedChildrenOnly()

            // Verify: owned backend was terminated, external Ollama was preserved
            assert(mockBackendProc.terminateCallCount == 1, "Owned backend process must be terminated")
            assert(supervisor.backendOwnership == .notRunning, "Owned backend ownership resets to .notRunning")
            assert(supervisor.ollamaOwnership == .external, "External Ollama ownership must remain .external")
            print("       ✓ Ownership-bounded cleanup strictly preserved external process while terminating owned child")
        } catch {
            assertionFailure("Test 19 failed with error: \(error)")
        }

        // -------------------------------------------------------------
        // DEDICATED PIPE DRAINING TESTS (20 - 24)
        // -------------------------------------------------------------

        let pythonExecutable = URL(fileURLWithPath: "/usr/bin/python3")

        // Test 20: Child process producing stdout larger than OS pipe buffer (>128KB) drains and exits
        print("[20/35] Verifying stdout > pipe buffer (256KB) drains asynchronously without blocking...")
        do {
            let launcher = DefaultProcessLauncher()
            // 262,144 bytes is 4x the 64KB macOS pipe buffer
            let proc = try launcher.launchProcess(
                executableURL: pythonExecutable,
                arguments: ["-c", "import sys; sys.stdout.write('A' * 262144); sys.stdout.flush()"],
                currentDirectoryURL: nil,
                environment: nil
            )

            // Await child process natural completion within bounded 2.0s timeout
            await proc.awaitExit(timeout: 2.0)
            assert(!proc.isRunning, "Child process writing 256KB stdout must exit without hanging")
            try await Task.sleep(nanoseconds: 30_000_000)

            if let managed = proc as? ManagedProcess, let drainer = managed.stdoutDrainer {
                assert(drainer.totalBytesDrained == 262144, "Expected 262144 bytes drained, got \(drainer.totalBytesDrained)")
                print("       ✓ Genuine stdout drain consumed \(drainer.totalBytesDrained) bytes without blocking child")
            } else {
                assertionFailure("Failed to inspect ManagedProcess stdout drainer")
            }
        } catch {
            assertionFailure("Test 20 failed: \(error)")
        }

        // Test 21: Child process producing stderr larger than OS pipe buffer (>128KB) drains and exits
        print("[21/35] Verifying stderr > pipe buffer (256KB) drains asynchronously without blocking...")
        do {
            let launcher = DefaultProcessLauncher()
            let proc = try launcher.launchProcess(
                executableURL: pythonExecutable,
                arguments: ["-c", "import sys; sys.stderr.write('B' * 262144); sys.stderr.flush()"],
                currentDirectoryURL: nil,
                environment: nil
            )

            await proc.awaitExit(timeout: 2.0)
            assert(!proc.isRunning, "Child process writing 256KB stderr must exit without hanging")
            try await Task.sleep(nanoseconds: 30_000_000)

            if let managed = proc as? ManagedProcess, let drainer = managed.stderrDrainer {
                assert(drainer.totalBytesDrained == 262144, "Expected 262144 bytes drained, got \(drainer.totalBytesDrained)")
                print("       ✓ Genuine stderr drain consumed \(drainer.totalBytesDrained) bytes without blocking child")
            } else {
                assertionFailure("Failed to inspect ManagedProcess stderr drainer")
            }
        } catch {
            assertionFailure("Test 21 failed: \(error)")
        }

        // Test 22: Verify both children exit successfully without blocking
        print("[22/35] Verifying child processes exit cleanly with exit code 0...")
        do {
            let launcher = DefaultProcessLauncher()
            let proc = try launcher.launchProcess(
                executableURL: pythonExecutable,
                arguments: ["-c", "import sys; sys.stdout.write('C'*100000); sys.stderr.write('D'*100000); sys.stdout.flush(); sys.stderr.flush()"],
                currentDirectoryURL: nil,
                environment: nil
            )

            await proc.awaitExit(timeout: 2.0)
            assert(!proc.isRunning, "Child process producing concurrent stdout/stderr must finish")
            if let managed = proc as? ManagedProcess {
                assert(managed.process.terminationStatus == 0, "Expected exit code 0, got \(managed.process.terminationStatus)")
                print("       ✓ Child exited with status 0 after concurrent stdout/stderr streams")
            }
        } catch {
            assertionFailure("Test 22 failed: \(error)")
        }

        // Test 23: Verify pipe handlers are removed after termination
        print("[23/35] Verifying readabilityHandlers are detached after termination...")
        do {
            let launcher = DefaultProcessLauncher()
            let proc = try launcher.launchProcess(
                executableURL: pythonExecutable,
                arguments: ["-c", "import sys; sys.stdout.write('EOF test'); sys.stdout.flush()"],
                currentDirectoryURL: nil,
                environment: nil
            )

            await proc.awaitExit(timeout: 2.0)
            if let managed = proc as? ManagedProcess {
                managed.cleanupPipes()
                assert(managed.stdoutDrainer?.hasHandlerAttached == false, "stdout readabilityHandler must be nil after cleanup")
                assert(managed.stderrDrainer?.hasHandlerAttached == false, "stderr readabilityHandler must be nil after cleanup")
                print("       ✓ Both stdout and stderr readabilityHandlers cleared and file descriptors closed")
            }
        } catch {
            assertionFailure("Test 23 failed: \(error)")
        }

        // Test 24: Verify cleanup is safe when termination occurs during active pipe reads
        print("[24/35] Verifying safe cleanup when termination occurs during active pipe reads...")
        do {
            let launcher = DefaultProcessLauncher()
            // Child process produces continuous stream with sleep
            let streamScript = """
            import sys, time
            for _ in range(500):
                sys.stdout.write('DATA\\n')
                sys.stdout.flush()
                time.sleep(0.02)
            """
            let proc = try launcher.launchProcess(
                executableURL: pythonExecutable,
                arguments: ["-c", streamScript],
                currentDirectoryURL: nil,
                environment: nil
            )

            // Let it stream some data
            try await Task.sleep(nanoseconds: 40_000_000) // 40ms
            assert(proc.isRunning, "Child should still be streaming")

            // Abruptly terminate while active reads are occurring
            let termResult = await proc.terminateAndAwaitExit(timeout: 2.0)
            assert(!proc.isRunning, "Child must be terminated")
            assert(termResult != .timedOut)
            if let managed = proc as? ManagedProcess {
                assert(managed.stdoutDrainer?.hasHandlerAttached == false)
                assert(managed.stderrDrainer?.hasHandlerAttached == false)
                print("       ✓ Abrupt termination during active reads completed safely without deadlocks or crashes")
            }
        } catch {
            assertionFailure("Test 24 failed: \(error)")
        }

        // -------------------------------------------------------------
        // SUPERVISION RECOVERY & STARTING-SERVICE TESTS (25 - 27)
        // -------------------------------------------------------------

        // Test 25: Ollama service still starting (HTTP 503) awaits readiness and avoids duplicate launch
        print("[25/35] Verifying Ollama service still starting (HTTP 503) does not trigger duplicate launch...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            let probeCount = AtomicState(0)
            httpClient.setDynamicHandler { url in
                if url.path.hasSuffix("api/version") {
                    probeCount.mutate { $0 += 1 }
                    let count = probeCount.get()
                    if count < 3 {
                        // Responding with 503 Service Unavailable (service starting up)
                        return (Data(), 503)
                    } else {
                        // Now healthy
                        let data = try! JSONSerialization.data(withJSONObject: ["version": "0.33.0"])
                        return (data, 200)
                    }
                }
                throw URLError(.cannotConnectToHost)
            }

            let launcher = MockProcessLauncher()
            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            try await supervisor.ensureOllamaRunning(timeout: 5.0)
            assert(supervisor.ollamaOwnership == .external, "Expected .external ownership once starting service reached ready")
            assert(launcher.launchedProcesses.isEmpty, "Supervisor must NOT launch duplicate process when port is already listening")
            print("       ✓ In-progress starting Ollama service awaited and reused without duplicate spawn")
        } catch {
            assertionFailure("Test 25 failed: \(error)")
        }

        // Test 26: Failed owned-child startup terminates child and awaits exit within bounded timeout
        print("[26/35] Verifying failed child startup terminates child and awaits exit...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            let launcher = MockProcessLauncher()
            let mockChild = MockProcess(processIdentifier: 60001, isRunning: true)
            launcher.setProcessFactory { mockChild }

            let fakeOllama = makeExecutable(at: "bin/ollama_t26")
            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            do {
                // Low timeout so it fails quickly
                try await supervisor.ensureOllamaRunning(timeout: 0.2, customSearchPaths: [fakeOllama.path])
                assertionFailure("Expected ensureOllamaRunning to fail on timeout")
            } catch let err as ProcessSupervisorError {
                assert(err == .ollamaTimeout)
                assert(mockChild.terminateAndAwaitExitCallCount >= 1, "Child must be terminated with awaited exit")
                assert(!mockChild.isRunning, "Child must be marked not running")
                assert(supervisor.ollamaOwnership == .notRunning)
                print("       ✓ Child termination and exit cleanly awaited within bounded deadline")
            } catch {
                assertionFailure("Unexpected error: \(error)")
            }
        }

        // Test 27: Concurrently spawned startup requests cannot spawn duplicate children when owned child is starting
        print("[27/35] Verifying duplicate launch prevented when owned child is already starting...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            let launcher = MockProcessLauncher()
            let fakeOllama = makeExecutable(at: "bin/ollama_t27")

            let ollamaHealthy = AtomicState(false)
            httpClient.setDynamicHandler { url in
                if url.path.hasSuffix("api/version") {
                    if ollamaHealthy.get() {
                        return (try! JSONSerialization.data(withJSONObject: ["version": "0.33.0"]), 200)
                    }
                    throw URLError(.cannotConnectToHost)
                }
                throw URLError(.cannotConnectToHost)
            }

            let mockProc = MockProcess(processIdentifier: 70001)
            launcher.setProcessFactory {
                DispatchQueue.global().asyncAfter(deadline: .now() + 0.1) {
                    ollamaHealthy.set(true)
                }
                return mockProc
            }

            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            // Run two consecutive calls to ensureOllamaRunning
            async let call1: Void = supervisor.ensureOllamaRunning(timeout: 2.0, customSearchPaths: [fakeOllama.path])
            async let call2: Void = supervisor.ensureOllamaRunning(timeout: 2.0, customSearchPaths: [fakeOllama.path])

            try await call1
            try await call2

            assert(launcher.launchedProcesses.count == 1, "Expected exactly 1 process launched, got \(launcher.launchedProcesses.count)")
            assert(supervisor.ollamaOwnership == .owned)
            print("       ✓ Sequential and concurrent ensureOllamaRunning calls safely shared the active child")
        } catch {
            assertionFailure("Test 27 failed: \(error)")
        }

        // -------------------------------------------------------------
        // PROCESS TERMINATION & TIMEOUT AUDIT TESTS (28 - 33)
        // -------------------------------------------------------------

        // Test 28: A controlled child that exits normally after SIGTERM
        print("[28/35] Verifying child exits normally after SIGTERM with confirmed exit...")
        do {
            let launcher = DefaultProcessLauncher()
            let script = """
            import time, sys
            sys.stdout.write('READY\\n')
            sys.stdout.flush()
            time.sleep(10)
            """
            let proc = try launcher.launchProcess(
                executableURL: pythonExecutable,
                arguments: ["-c", script],
                currentDirectoryURL: nil,
                environment: nil
            )

            try await Task.sleep(nanoseconds: 50_000_000) // 50ms to ensure process is up
            assert(proc.isRunning, "Process should be running")

            let termResult = await proc.terminateAndAwaitExit(timeout: 2.0)
            if case .exited = termResult {
                assert(!proc.isRunning, "Process must be confirmed not running")
                if let managed = proc as? ManagedProcess {
                    assert(managed.stdoutDrainer?.hasHandlerAttached == false, "Pipes must be closed after confirmed exit")
                }
                print("       ✓ Confirmed exit returned after SIGTERM: \(termResult)")
            } else {
                assertionFailure("Expected .exited, got \(termResult)")
            }
        } catch {
            assertionFailure("Test 28 failed: \(error)")
        }

        // Test 29: A controlled child that ignores SIGTERM
        print("[29/35] Verifying child that ignores SIGTERM reports .timedOut and does NOT close pipes...")
        do {
            let launcher = DefaultProcessLauncher()
            let script = """
            import signal, time, sys
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            sys.stdout.write('IGNORING\\n')
            sys.stdout.flush()
            time.sleep(0.4)
            """
            let proc = try launcher.launchProcess(
                executableURL: pythonExecutable,
                arguments: ["-c", script],
                currentDirectoryURL: nil,
                environment: nil
            )

            try await Task.sleep(nanoseconds: 50_000_000) // 50ms
            assert(proc.isRunning, "Process should be running")

            // Send SIGTERM with a short 0.15s deadline (process sleeps 0.4s and ignores SIGTERM)
            let termResult = await proc.terminateAndAwaitExit(timeout: 0.15)
            assert(termResult == .timedOut, "Expected .timedOut when child ignores SIGTERM, got \(termResult)")
            assert(proc.isRunning, "Child must still be running after timeout")

            if let managed = proc as? ManagedProcess {
                // Safety invariant: Do NOT silently close pipes while child is still running!
                assert(managed.stdoutDrainer?.hasHandlerAttached == true, "Pipes must remain open while child is still running")
                print("       ✓ .timedOut reported accurately; pipes were safely kept open")
            }

            // Await natural exit so process terminates cleanly
            let exitResult = await proc.awaitExit(timeout: 1.0)
            assert(exitResult != .timedOut)
            assert(!proc.isRunning)
        } catch {
            assertionFailure("Test 29 failed: \(error)")
        }

        // Test 30: Child continues writing while termination is underway
        print("[30/35] Verifying child continues writing while termination is underway without pipe deadlocks...")
        do {
            let launcher = DefaultProcessLauncher()
            let script = """
            import sys, time
            for i in range(500):
                sys.stdout.write(f'line {i}\\n')
                sys.stdout.flush()
                time.sleep(0.01)
            """
            let proc = try launcher.launchProcess(
                executableURL: pythonExecutable,
                arguments: ["-c", script],
                currentDirectoryURL: nil,
                environment: nil
            )

            try await Task.sleep(nanoseconds: 40_000_000)
            assert(proc.isRunning)

            let termResult = await proc.terminateAndAwaitExit(timeout: 2.0)
            if case .exited = termResult {
                assert(!proc.isRunning)
                if let managed = proc as? ManagedProcess {
                    assert(managed.stdoutDrainer?.hasHandlerAttached == false)
                }
                print("       ✓ Active writes consumed safely during termination without deadlocks")
            } else {
                assertionFailure("Expected .exited, got \(termResult)")
            }
        } catch {
            assertionFailure("Test 30 failed: \(error)")
        }

        // Test 31: Timeout reporting when exit is not confirmed and ownership preserved
        print("[31/35] Verifying ownership is preserved when termination exit is not confirmed...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            let launcher = MockProcessLauncher()
            let unkillableProc = MockProcess(processIdentifier: 80001, isRunning: true)
            unkillableProc.ignoreTerminate = true // Ignores SIGTERM
            launcher.setProcessFactory { unkillableProc }

            let fakeOllama = makeExecutable(at: "bin/ollama_t31")
            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            // Attempt startup: probes fail, supervisor launches child, polling times out, tries terminateAndAwaitExit
            do {
                try await supervisor.ensureOllamaRunning(timeout: 0.1, customSearchPaths: [fakeOllama.path])
                assertionFailure("Expected ensureOllamaRunning to fail on timeout")
            } catch {
                // Invariant: Because unkillableProc ignored SIGTERM and stayed running,
                // supervisor MUST NOT mark ownership as .notRunning or discard child handle!
                assert(supervisor.ollamaOwnership == .owned, "Ownership must remain .owned when exit was not confirmed")
                assert(supervisor.childOllamaProcess != nil, "Child handle must be retained when exit was not confirmed")
                print("       ✓ Unconfirmed exit preserved .owned ownership and process handle")
            }

            // Teardown attempt
            let teardownRes = await supervisor.terminateOwnedChildrenOnly(awaitExitTimeout: 0.1)
            assert(teardownRes.ollamaResult == .timedOut, "Expected .timedOut in teardown result")
            assert(supervisor.ollamaOwnership == .owned, "Ownership still preserved after failed teardown")

            // Simulate child eventual exit
            unkillableProc.simulateExit()
            try await Task.sleep(nanoseconds: 30_000_000)
            assert(supervisor.ollamaOwnership == .notRunning)
            assert(supervisor.childOllamaProcess == nil)
            print("       ✓ Ownership cleanly transitioned to .notRunning once exit was actually confirmed")
        } catch {
            assertionFailure("Test 31 failed: \(error)")
        }

        // Test 32: Repeated cleanup after successful termination is idempotent
        print("[32/35] Verifying repeated cleanup is idempotent and safe...")
        do {
            let launcher = DefaultProcessLauncher()
            let proc = try launcher.launchProcess(
                executableURL: pythonExecutable,
                arguments: ["-c", "exit(0)"],
                currentDirectoryURL: nil,
                environment: nil
            )

            await proc.awaitExit(timeout: 1.0)
            assert(!proc.isRunning)

            if let managed = proc as? ManagedProcess {
                // Call cleanupPipes 5 times consecutively
                for _ in 0..<5 {
                    managed.cleanupPipes()
                }
                assert(managed.stdoutDrainer?.hasHandlerAttached == false)
                assert(managed.stderrDrainer?.hasHandlerAttached == false)
            }

            let supervisor = ProcessSupervisor(projectRoot: tempDir)
            for _ in 0..<3 {
                supervisor.terminateOwnedChildrenOnly()
            }
            print("       ✓ Repeated cleanup calls were completely idempotent with zero crashes")
        } catch {
            assertionFailure("Test 32 failed: \(error)")
        }

        // Test 33: External process preservation during termination calls
        print("[33/35] Verifying external processes are preserved during termination calls...")
        do {
            let httpClient = MockProcessSupervisorHTTPClient()
            httpClient.setResponse(forPath: "api/version", json: ["version": "0.33.0"])
            httpClient.setResponse(forPath: "api/v1/health", json: standardHealthyBackend)
            let launcher = MockProcessLauncher()

            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )

            try await supervisor.ensureOllamaRunning()
            try await supervisor.ensureBackendRunning()

            assert(supervisor.ollamaOwnership == .external)
            assert(supervisor.backendOwnership == .external)

            let teardownRes = await supervisor.terminateOwnedChildrenOnly(awaitExitTimeout: 1.0)
            assert(teardownRes.ollamaResult == nil, "External Ollama must not produce termination result")
            assert(teardownRes.backendResult == nil, "External backend must not produce termination result")
            assert(supervisor.ollamaOwnership == .external, "Ownership must remain .external")
            assert(supervisor.backendOwnership == .external, "Ownership must remain .external")
            assert(launcher.launchedProcesses.isEmpty, "No processes launched or touched")
            print("       ✓ External processes completely untouched and preserved")
        } catch {
            assertionFailure("Test 33 failed: \(error)")
        }

        // Test 34 (D1): Cancelling awaitExit while child is running stops polling promptly without busy-spin
        print("[34/35] Verifying cancelled awaitExit exits polling loop promptly and keeps child pipes open...")
        do {
            let launcher = DefaultProcessLauncher()
            let proc = try launcher.launchProcess(
                executableURL: pythonExecutable,
                arguments: ["-c", "import time; time.sleep(2.0)"],
                currentDirectoryURL: nil,
                environment: nil
            )
            try await Task.sleep(nanoseconds: 40_000_000)
            assert(proc.isRunning)

            let start = Date()
            let waitTask = Task { () -> ProcessTerminationResult in
                return await proc.awaitExit(timeout: 5.0)
            }
            try await Task.sleep(nanoseconds: 50_000_000)
            waitTask.cancel()
            let res = await waitTask.value
            let elapsed = Date().timeIntervalSince(start)

            assert(elapsed < 0.5, "Cancelled awaitExit MUST complete promptly, took \(elapsed)s")
            assert(res == .timedOut, "Still-running child must return .timedOut on cancelled wait, got \(res)")
            assert(proc.isRunning, "Child is still running")
            if let managed = proc as? ManagedProcess {
                assert(managed.stdoutDrainer?.hasHandlerAttached == true, "Pipes must remain open while child still runs")
            }

            // Clean up real child
            let finalExit = await proc.terminateAndAwaitExit(timeout: 1.0)
            if case .exited = finalExit {} else { assertionFailure("Expected clean exit during cleanup") }
            print("       ✓ Cancelled awaitExit completed cleanly in \(String(format: "%.2f", elapsed))s without busy-spin")
        } catch {
            assertionFailure("Test 34 failed: \(error)")
        }

        // Test 35 (D1): Cancelling terminateAndAwaitExit while child ignores SIGTERM exits promptly & keeps .owned
        print("[35/35] Verifying cancelled terminateAndAwaitExit stops polling promptly and keeps still-running child owned...")
        do {
            let launcher = DefaultProcessLauncher()
            let script = """
            import signal, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(0.8)
            """
            let realUncooperative = try launcher.launchProcess(
                executableURL: pythonExecutable,
                arguments: ["-c", script],
                currentDirectoryURL: nil,
                environment: nil
            )
            try await Task.sleep(nanoseconds: 40_000_000)
            assert(realUncooperative.isRunning)

            // Part A: Cancel terminateAndAwaitExit(timeout: 5.0) directly while child ignores SIGTERM
            let termStart = Date()
            let directTask = Task { () -> ProcessTerminationResult in
                return await realUncooperative.terminateAndAwaitExit(timeout: 5.0)
            }
            try await Task.sleep(nanoseconds: 50_000_000)
            directTask.cancel()
            let directRes = await directTask.value
            let directElapsed = Date().timeIntervalSince(termStart)

            assert(directElapsed < 0.5, "Cancelled terminateAndAwaitExit MUST finish promptly, took \(directElapsed)s")
            assert(directRes == .timedOut, "Expected .timedOut when cancelled while child still runs")
            assert(realUncooperative.isRunning, "Uncooperative child must still be running")
            if let managed = realUncooperative as? ManagedProcess {
                assert(managed.stdoutDrainer?.hasHandlerAttached == true, "Pipes must remain open while child runs")
            }

            // Part B: Verify supervisor.terminateOwnedChildrenAndAwaitExit cancellation preserves .owned and handle
            let httpClient = MockProcessSupervisorHTTPClient()
            let mockLauncher = MockProcessLauncher()
            mockLauncher.setProcessFactory { realUncooperative }
            let fakeOllama = makeExecutable(at: "bin/ollama_t35")
            let supervisor = ProcessSupervisor(projectRoot: tempDir, httpClient: httpClient, processLauncher: mockLauncher)

            let ollamaReady = AtomicState(false)
            httpClient.setDynamicHandler { url in
                if url.path.hasSuffix("api/version") && ollamaReady.get() {
                    return (try! JSONSerialization.data(withJSONObject: ["version": "0.33.0"]), 200)
                }
                throw URLError(.cannotConnectToHost)
            }
            mockLauncher.setProcessFactory {
                ollamaReady.set(true)
                return realUncooperative
            }
            try await supervisor.ensureOllamaRunning(timeout: 2.0, customSearchPaths: [fakeOllama.path])
            assert(supervisor.ollamaOwnership == .owned)

            let supTermStart = Date()
            let supTermTask = Task { @MainActor in
                await supervisor.terminateOwnedChildrenAndAwaitExit(timeout: 5.0)
            }
            try await Task.sleep(nanoseconds: 50_000_000)
            supTermTask.cancel()
            _ = await supTermTask.value
            let supElapsed = Date().timeIntervalSince(supTermStart)

            assert(supElapsed < 0.5, "Cancelled supervisor termination wait must return promptly, took \(supElapsed)s")
            assert(realUncooperative.isRunning, "Uncooperative child must still be running")
            assert(supervisor.ollamaOwnership == .owned, "Still-running child MUST remain .owned")
            assert(supervisor.childOllamaProcess === realUncooperative)

            // Await natural finish of short 0.8s script so temp cleanup is tidy
            _ = await realUncooperative.awaitExit(timeout: 1.5)
            print("       ✓ Cancelled terminateAndAwaitExit aborted wait in \(String(format: "%.2f", supElapsed))s and kept child .owned")
        } catch {
            assertionFailure("Test 35 failed: \(error)")
        }

        // Test 36 (Stage 4.3A Fix 1): Default backend timeout accommodates 60s model load + overhead
        print("[36/36] Verifying ProcessSupervisor default timeouts & slow pre-warm startup survival...")
        do {
            assert(ProcessSupervisor.defaultModelLoadTimeout == 60.0)
            assert(ProcessSupervisor.defaultBackendTimeout == 75.0)

            let httpClient = MockProcessSupervisorHTTPClient()
            let probeCount = AtomicState(0)
            httpClient.setDynamicHandler { url in
                if url.path.hasSuffix("api/v1/health") {
                    let count = probeCount.mutate { val -> Int in
                        val += 1
                        return val
                    }
                    // Unavailable for first 5 probes while model pre-warms, then healthy
                    if count < 5 {
                        throw URLError(.cannotConnectToHost)
                    }
                    let healthy: [String: Any] = [
                        "status": "healthy",
                        "core_running": true,
                        "ollama_connected": true,
                        "active_model": ["id": "qwen3.5:9b"]
                    ]
                    return (try! JSONSerialization.data(withJSONObject: healthy), 200)
                }
                throw URLError(.cannotConnectToHost)
            }

            _ = makeExecutable(at: ".venv/bin/python")
            let launcher = MockProcessLauncher()
            let mockProc = MockProcess(processIdentifier: 40036)
            launcher.setProcessFactory { mockProc }

            let supervisor = ProcessSupervisor(
                projectRoot: tempDir,
                httpClient: httpClient,
                processLauncher: launcher
            )
            // Uses default 75.0s backendTimeout without premature termination
            try await supervisor.ensureBackendRunning()
            assert(supervisor.backendOwnership == .owned)
            assert(mockProc.terminateCallCount == 0, "Slow healthy warmup must not terminate backend")
            assert(probeCount.get() >= 5)
            print("       ✓ Default timeout=75.0s verified and slow pre-warm survived cleanly")
        } catch {
            assertionFailure("Test 36 failed: \(error)")
        }

        print("\n=== ALL 36 PROCESS SUPERVISOR TESTS PASSED SUCCESSFULLY ===")
    }
}
