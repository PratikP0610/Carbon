import Foundation
import Combine

public struct ChatDisplayMessage: Identifiable, Equatable {
    public let id: UUID
    public let role: String // "user" or "assistant"
    public var text: String
    public var isStreaming: Bool
    public var isProcessing: Bool // Active generation prior to first user-visible token
    public var isCancelled: Bool
    public var errorMessage: String?
    public var executedReasoningDepth: String?
    public let timestamp: Date

    public init(
        id: UUID = UUID(),
        role: String,
        text: String,
        isStreaming: Bool = false,
        isProcessing: Bool = false,
        isCancelled: Bool = false,
        errorMessage: String? = nil,
        executedReasoningDepth: String? = nil,
        timestamp: Date = Date()
    ) {
        self.id = id
        self.role = role
        self.text = text
        self.isStreaming = isStreaming
        self.isProcessing = isProcessing
        self.isCancelled = isCancelled
        self.errorMessage = errorMessage
        self.executedReasoningDepth = executedReasoningDepth
        self.timestamp = timestamp
    }
}

public enum ConnectionStatus: String, Equatable {
    case disconnected = "OFFLINE"
    case connecting = "CONNECTING"
    case authenticating = "AUTHENTICATING"
    case connected = "ONLINE"
    case failed = "FAILED"
}

private final class WebSocketDelegateHandler: NSObject, URLSessionWebSocketDelegate, @unchecked Sendable {
    weak var client: CarbonClient?

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocol: String?) {
        Task { @MainActor [weak self] in
            self?.client?.webSocketDidOpen(task: webSocketTask)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let wsTask = task as? URLSessionWebSocketTask else { return }
        Task { @MainActor [weak self] in
            self?.client?.webSocketDidFail(task: wsTask, error: error)
        }
    }
}

@MainActor
public final class CarbonClient: ObservableObject {
    @Published public var connectionStatus: ConnectionStatus = .disconnected
    @Published public var isConnected: Bool = false
    @Published public var isHealthOk: Bool = false
    @Published public var activeModel: String = "qwen3.5:9b"
    @Published public var reasoningDepth: String = "auto"
    @Published public var inputText: String = ""
    @Published public var messages: [ChatDisplayMessage] = []
    @Published public var isGenerating: Bool = false
    @Published public var currentTaskId: String? = nil
    @Published public var conversationId: String = "conv-\(UUID().uuidString.prefix(8).lowercased())"

    private let sessionQueue: OperationQueue = {
        let q = OperationQueue()
        q.name = "ai.carbon.client.network"
        q.maxConcurrentOperationCount = 4
        return q
    }()

    private var delegateHandler: WebSocketDelegateHandler!
    private var webSocketTask: URLSessionWebSocketTask?
    private var session: URLSession!
    private let baseURL = URL(string: "http://127.0.0.1:8765")!
    private let wsURL = URL(string: "ws://127.0.0.1:8765/ws/chat")!

    private var reconnectTask: Task<Void, Never>?
    private var reconnectAttempts: Int = 0
    private var submissionTimeoutTask: Task<Void, Never>?
    private var healthCheckTask: Task<Void, Never>?
    private var usedHeaderAuth: Bool = false

    public init() {
        let handler = WebSocketDelegateHandler()
        self.delegateHandler = handler
        self.session = URLSession(configuration: .default, delegate: handler, delegateQueue: sessionQueue)
        handler.client = self

        checkHealth()
        startPeriodicHealthCheck()
        connectWebSocket()
    }

    deinit {
        reconnectTask?.cancel()
        submissionTimeoutTask?.cancel()
        healthCheckTask?.cancel()
        webSocketTask?.cancel(with: .goingAway, reason: nil)
    }

    public func resolveAuthToken() -> String? {
        if let envToken = ProcessInfo.processInfo.environment["CARBON_API_TOKEN"], !envToken.isEmpty {
            return envToken
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        let localTokenURL = home.appendingPathComponent("Carbon/data/.carbon_token")
        if let token = try? String(contentsOf: localTokenURL, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines), !token.isEmpty {
            return token
        }
        return nil
    }

    public func checkHealth() {
        let healthURL = baseURL.appendingPathComponent("/api/v1/health")
        Task {
            do {
                let (data, response) = try await session.data(from: healthURL)
                guard let httpRes = response as? HTTPURLResponse, httpRes.statusCode == 200 else {
                    await MainActor.run {
                        self.isHealthOk = false
                    }
                    return
                }
                guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let status = json["status"] as? String, status == "healthy" else {
                    await MainActor.run {
                        self.isHealthOk = false
                    }
                    return
                }

                await MainActor.run {
                    self.isHealthOk = true
                    if let modelData = json["active_model"] as? [String: Any],
                       let modelId = modelData["id"] as? String {
                        self.activeModel = modelId
                    }
                }
            } catch {
                await MainActor.run {
                    self.isHealthOk = false
                }
            }
        }
    }

    private func startPeriodicHealthCheck() {
        healthCheckTask?.cancel()
        healthCheckTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 10_000_000_000) // 10 seconds
                guard let self = self else { return }
                self.checkHealth()
            }
        }
    }

    public func connectWebSocket() {
        let token = resolveAuthToken()

        // Cancel any pending reconnect attempt
        reconnectTask?.cancel()
        reconnectTask = nil

        // Clean up previous websocket task cleanly without triggering a reconnect loop
        if let oldTask = webSocketTask {
            self.webSocketTask = nil
            oldTask.cancel(with: .goingAway, reason: nil)
        }

        connectionStatus = .connecting
        isConnected = false

        var request = URLRequest(url: wsURL)
        if let token = token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            self.usedHeaderAuth = true
        } else {
            self.usedHeaderAuth = false
        }

        let task = session.webSocketTask(with: request)
        self.webSocketTask = task
        task.resume()

        listenForMessages(for: task)
    }

    fileprivate func webSocketDidOpen(task: URLSessionWebSocketTask) {
        guard self.webSocketTask === task else { return }

        if let token = resolveAuthToken() {
            // Always dispatch in-socket authentication frame to guarantee robust auth
            // across macOS URLSession implementations where HTTP headers are not forwarded.
            connectionStatus = .authenticating
            let authPayload: [String: Any] = ["action": "auth", "token": token]
            if let data = try? JSONSerialization.data(withJSONObject: authPayload),
               let jsonStr = String(data: data, encoding: .utf8) {
                task.send(.string(jsonStr)) { [weak self, weak task] error in
                    if let error = error {
                        Task { @MainActor [weak self] in
                            guard let self = self, let task = task, self.webSocketTask === task else { return }
                            self.webSocketDidFail(task: task, error: error)
                        }
                    }
                }
            }
        } else {
            connectionStatus = .connected
            isConnected = true
            reconnectAttempts = 0
        }
    }

    fileprivate func webSocketDidFail(task: URLSessionWebSocketTask, error: Error?) {
        guard self.webSocketTask === task else { return }
        self.webSocketTask = nil
        isConnected = false
        connectionStatus = .disconnected

        if isGenerating {
            handleSendFailure(error: "Connection lost while communicating with Carbon backend.")
        }

        scheduleReconnect()
    }

    private func scheduleReconnect() {
        guard reconnectTask == nil else { return }

        // Controlled exponential backoff: 1.5s, 2.1s, 2.9s ... up to 10s max
        let delaySeconds = min(10.0, 1.5 * pow(1.4, Double(reconnectAttempts)))
        reconnectAttempts += 1

        reconnectTask = Task { [weak self] in
            let nano = UInt64(delaySeconds * 1_000_000_000)
            try? await Task.sleep(nanoseconds: nano)
            guard let self = self, !Task.isCancelled else { return }
            self.reconnectTask = nil
            self.connectWebSocket()
            self.checkHealth()
        }
    }

    private func listenForMessages(for task: URLSessionWebSocketTask) {
        task.receive { [weak self, weak task] result in
            Task { @MainActor [weak self] in
                guard let self = self, let task = task, self.webSocketTask === task else { return }
                switch result {
                case .failure(let error):
                    let nsErr = error as NSError
                    if nsErr.domain == NSURLErrorDomain && nsErr.code == NSURLErrorCancelled {
                        return
                    }
                    self.webSocketDidFail(task: task, error: error)
                case .success(let message):
                    switch message {
                    case .string(let text):
                        self.handleIncomingJSON(text)
                    case .data(let data):
                        if let text = String(data: data, encoding: .utf8) {
                            self.handleIncomingJSON(text)
                        }
                    @unknown default:
                        break
                    }
                    // Continue listening for next message on this active task
                    self.listenForMessages(for: task)
                }
            }
        }
    }

    private func handleIncomingJSON(_ text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let event = json["event"] as? String else {
            return
        }

        switch event {
        case "authenticated":
            // Explicitly handle server authentication event
            connectionStatus = .connected
            isConnected = true
            reconnectAttempts = 0

        case "task_created":
            // Successful backend acknowledgment: cancel submission timeout
            submissionTimeoutTask?.cancel()
            submissionTimeoutTask = nil

            if let tid = json["task_id"] as? String {
                self.currentTaskId = tid
            }
            if let cid = json["conversation_id"] as? String {
                self.conversationId = cid
            }
            if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                self.messages[lastIdx].isProcessing = true
            }

        case "intent_classified":
            if let depth = json["reasoning_depth"] as? String {
                if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                    self.messages[lastIdx].executedReasoningDepth = depth
                }
            }

        case "thinking":
            // Internal reasoning: preserve processing indicator, do not append raw thinking to text
            if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                self.messages[lastIdx].isProcessing = true
            }

        case "token":
            // User-visible answer streaming
            if let tokenText = json["text"] as? String {
                if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                    self.messages[lastIdx].isProcessing = false
                    self.messages[lastIdx].text += tokenText
                }
            }

        case "completed":
            submissionTimeoutTask?.cancel()
            submissionTimeoutTask = nil
            self.isGenerating = false
            if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                self.messages[lastIdx].isStreaming = false
                self.messages[lastIdx].isProcessing = false
            }
            self.currentTaskId = nil

        case "cancelled":
            submissionTimeoutTask?.cancel()
            submissionTimeoutTask = nil
            self.isGenerating = false
            if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                self.messages[lastIdx].isStreaming = false
                self.messages[lastIdx].isProcessing = false
                self.messages[lastIdx].isCancelled = true
            }
            self.currentTaskId = nil

        case "failed":
            submissionTimeoutTask?.cancel()
            submissionTimeoutTask = nil
            self.isGenerating = false
            if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                self.messages[lastIdx].isStreaming = false
                self.messages[lastIdx].isProcessing = false
                let err = json["error"] as? String ?? "Unknown error"
                self.messages[lastIdx].errorMessage = err
            }
            self.currentTaskId = nil

        case "error":
            submissionTimeoutTask?.cancel()
            submissionTimeoutTask = nil
            let msg = json["message"] as? String ?? "Unknown error"
            if msg.contains("Authentication failed") {
                self.connectionStatus = .failed
                self.isConnected = false
            }
            if self.isGenerating {
                self.handleSendFailure(error: msg)
            }

        default:
            break
        }
    }

    public func sendMessage(_ prompt: String) {
        let cleanPrompt = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanPrompt.isEmpty else { return }

        // Always append user message
        messages.append(ChatDisplayMessage(role: "user", text: cleanPrompt))

        // Check connection state: if offline, immediately show actionable error without locking UI
        guard isConnected, let task = webSocketTask else {
            messages.append(ChatDisplayMessage(
                role: "assistant",
                text: "",
                isStreaming: false,
                isProcessing: false,
                errorMessage: "Cannot send instruction: Carbon backend is currently offline. Please ensure the backend is running at http://127.0.0.1:8765."
            ))
            isGenerating = false
            return
        }

        // Add assistant placeholder with progress state
        messages.append(ChatDisplayMessage(role: "assistant", text: "", isStreaming: true, isProcessing: true))
        isGenerating = true
        currentTaskId = nil

        let payload: [String: Any] = [
            "action": "submit",
            "prompt": cleanPrompt,
            "reasoning": reasoningDepth,
            "conversation_id": conversationId,
            "client_timestamp": Date().timeIntervalSince1970
        ]

        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let jsonStr = String(data: data, encoding: .utf8) else {
            handleSendFailure(error: "Failed to serialize message payload.")
            return
        }

        // Send over WebSocket with error callback
        task.send(.string(jsonStr)) { [weak self, weak task] error in
            if let error = error {
                Task { @MainActor [weak self] in
                    guard let self = self, self.webSocketTask === task else { return }
                    self.handleSendFailure(error: "Failed to send message: \(error.localizedDescription)")
                }
            }
        }

        // Set up 10-second submission acknowledgement timeout to guard against lost tasks
        submissionTimeoutTask?.cancel()
        submissionTimeoutTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 10_000_000_000) // 10s timeout
            guard let self = self, !Task.isCancelled else { return }
            if self.isGenerating && self.currentTaskId == nil {
                self.handleSendFailure(error: "Submission timed out waiting for backend acknowledgement.")
            }
        }
    }

    private func handleSendFailure(error: String) {
        submissionTimeoutTask?.cancel()
        submissionTimeoutTask = nil
        isGenerating = false
        currentTaskId = nil

        if let lastIdx = messages.indices.last, messages[lastIdx].role == "assistant" {
            messages[lastIdx].isStreaming = false
            messages[lastIdx].isProcessing = false
            messages[lastIdx].errorMessage = error
        }
    }

    public func cancelCurrentTask() {
        submissionTimeoutTask?.cancel()
        submissionTimeoutTask = nil

        if let tid = currentTaskId {
            let payload: [String: Any] = [
                "action": "cancel",
                "task_id": tid
            ]
            if let data = try? JSONSerialization.data(withJSONObject: payload),
               let jsonStr = String(data: data, encoding: .utf8) {
                webSocketTask?.send(.string(jsonStr)) { _ in }
            }
        }

        if let lastIdx = messages.indices.last, messages[lastIdx].role == "assistant" {
            messages[lastIdx].isStreaming = false
            messages[lastIdx].isProcessing = false
            messages[lastIdx].isCancelled = true
        }

        isGenerating = false
        currentTaskId = nil
    }
}

