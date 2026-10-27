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
    public let timestamp: Date

    public init(
        id: UUID = UUID(),
        role: String,
        text: String,
        isStreaming: Bool = false,
        isProcessing: Bool = false,
        isCancelled: Bool = false,
        errorMessage: String? = nil,
        timestamp: Date = Date()
    ) {
        self.id = id
        self.role = role
        self.text = text
        self.isStreaming = isStreaming
        self.isProcessing = isProcessing
        self.isCancelled = isCancelled
        self.errorMessage = errorMessage
        self.timestamp = timestamp
    }
}

@MainActor
public final class CarbonClient: ObservableObject {
    @Published public var isConnected: Bool = false
    @Published public var activeModel: String = "qwen3.5:9b"
    @Published public var reasoningDepth: String = "standard"
    @Published public var inputText: String = ""
    @Published public var messages: [ChatDisplayMessage] = []
    @Published public var isGenerating: Bool = false
    @Published public var currentTaskId: String? = nil

    private var webSocketTask: URLSessionWebSocketTask?
    private let session = URLSession(configuration: .default)
    private let baseURL = URL(string: "http://127.0.0.1:8765")!
    private let wsURL = URL(string: "ws://127.0.0.1:8765/ws/chat")!

    public init() {
        connectWebSocket()
        checkHealth()
    }

    private func resolveAuthToken() -> String? {
        if let envToken = ProcessInfo.processInfo.environment["CARBON_API_TOKEN"], !envToken.isEmpty {
            return envToken
        }
        // Local developer fallback file
        let home = FileManager.default.homeDirectoryForCurrentUser
        let localTokenPath = home.appendingPathComponent("Carbon/data/.carbon_token")
        if let token = try? String(contentsOf: localTokenPath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines), !token.isEmpty {
            return token
        }
        return nil
    }

    public func checkHealth() {
        let healthURL = baseURL.appendingPathComponent("/api/v1/health")
        Task {
            do {
                let (data, response) = try await session.data(from: healthURL)
                if let httpRes = response as? HTTPURLResponse, httpRes.statusCode == 200 {
                    if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let modelData = json["active_model"] as? [String: Any],
                       let modelId = modelData["id"] as? String {
                        self.activeModel = modelId
                    }
                }
            } catch {
                // Backend may still be initializing
            }
        }
    }

    public func connectWebSocket() {
        webSocketTask?.cancel()
        var request = URLRequest(url: wsURL)
        let token = resolveAuthToken()
        if let token = token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        webSocketTask = session.webSocketTask(with: request)
        webSocketTask?.resume()
        self.isConnected = true

        // If no handshake header was possible, dispatch in-socket authentication frame as secondary fallback
        if token != nil {
            let authPayload: [String: Any] = ["action": "auth", "token": token!]
            if let data = try? JSONSerialization.data(withJSONObject: authPayload),
               let jsonStr = String(data: data, encoding: .utf8) {
                webSocketTask?.send(.string(jsonStr)) { _ in }
            }
        }

        listenForMessages()
    }

    private func listenForMessages() {
        webSocketTask?.receive { [weak self] result in
            Task { @MainActor [weak self] in
                guard let self = self else { return }
                switch result {
                case .failure(let error):
                    print("WebSocket receive error: \(error)")
                    self.isConnected = false
                    // Auto-reconnect after 3 seconds
                    try? await Task.sleep(nanoseconds: 3_000_000_000)
                    self.connectWebSocket()
                case .success(let message):
                    self.isConnected = true
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
                    self.listenForMessages()
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
        case "task_created":
            if let tid = json["task_id"] as? String {
                self.currentTaskId = tid
            }
            // Immediate acknowledgement: set processing state
            if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                self.messages[lastIdx].isProcessing = true
            }

        case "thinking":
            // Directive 2: Do NOT stream or display raw internal reasoning.
            // Maintain progress indicator without appending reasoning text to user view.
            if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                self.messages[lastIdx].isProcessing = true
            }

        case "token":
            // Incremental user-visible answer streaming
            if let tokenText = json["text"] as? String {
                if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                    self.messages[lastIdx].isProcessing = false
                    self.messages[lastIdx].text += tokenText
                }
            }

        case "completed":
            self.isGenerating = false
            if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                self.messages[lastIdx].isStreaming = false
                self.messages[lastIdx].isProcessing = false
            }
            self.currentTaskId = nil

        case "cancelled":
            self.isGenerating = false
            if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                self.messages[lastIdx].isStreaming = false
                self.messages[lastIdx].isProcessing = false
                self.messages[lastIdx].isCancelled = true
            }
            self.currentTaskId = nil

        case "failed":
            self.isGenerating = false
            if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                self.messages[lastIdx].isStreaming = false
                self.messages[lastIdx].isProcessing = false
                let err = json["error"] as? String ?? "Unknown error"
                self.messages[lastIdx].errorMessage = err
            }
            self.currentTaskId = nil

        default:
            break
        }
    }

    public func sendMessage(_ prompt: String) {
        let cleanPrompt = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanPrompt.isEmpty else { return }

        // 1. Add user message to UI
        messages.append(ChatDisplayMessage(role: "user", text: cleanPrompt))

        // 2. Add pending assistant placeholder with immediate acknowledgement & progress state
        messages.append(ChatDisplayMessage(role: "assistant", text: "", isStreaming: true, isProcessing: true))
        isGenerating = true

        let payload: [String: Any] = [
            "action": "submit",
            "prompt": cleanPrompt,
            "reasoning": reasoningDepth
        ]

        if let data = try? JSONSerialization.data(withJSONObject: payload),
           let jsonStr = String(data: data, encoding: .utf8) {
            webSocketTask?.send(.string(jsonStr)) { error in
                if let error = error {
                    print("Failed to send message: \(error)")
                }
            }
        }
    }

    public func cancelCurrentTask() {
        guard let tid = currentTaskId else { return }
        let payload: [String: Any] = [
            "action": "cancel",
            "task_id": tid
        ]
        if let data = try? JSONSerialization.data(withJSONObject: payload),
           let jsonStr = String(data: data, encoding: .utf8) {
            webSocketTask?.send(.string(jsonStr)) { _ in }
        }
        isGenerating = false
        currentTaskId = nil
    }
}
