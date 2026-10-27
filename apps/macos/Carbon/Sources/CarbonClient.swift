import Foundation
import Combine

public struct ChatDisplayMessage: Identifiable, Equatable {
    public let id: UUID
    public let role: String // "user" or "assistant"
    public var text: String
    public var isStreaming: Bool
    public var thinking: String?
    public let timestamp: Date

    public init(id: UUID = UUID(), role: String, text: String, isStreaming: Bool = false, thinking: String? = nil, timestamp: Date = Date()) {
        self.id = id
        self.role = role
        self.text = text
        self.isStreaming = isStreaming
        self.thinking = thinking
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
                // Backend may still be spinning up
            }
        }
    }

    public func connectWebSocket() {
        webSocketTask?.cancel()
        webSocketTask = session.webSocketTask(with: wsURL)
        webSocketTask?.resume()
        self.isConnected = true
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
        case "thinking":
            if let thinkText = json["text"] as? String {
                if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                    if self.messages[lastIdx].thinking == nil {
                        self.messages[lastIdx].thinking = ""
                    }
                    self.messages[lastIdx].thinking! += thinkText
                }
            }
        case "token":
            if let tokenText = json["text"] as? String {
                if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" && self.messages[lastIdx].isStreaming {
                    self.messages[lastIdx].text += tokenText
                }
            }
        case "completed":
            self.isGenerating = false
            if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                self.messages[lastIdx].isStreaming = false
            }
            self.currentTaskId = nil
        case "failed", "cancelled":
            self.isGenerating = false
            if let lastIdx = self.messages.indices.last, self.messages[lastIdx].role == "assistant" {
                self.messages[lastIdx].isStreaming = false
                if let err = json["error"] as? String {
                    self.messages[lastIdx].text += "\n[Error: \(err)]"
                }
            }
            self.currentTaskId = nil
        default:
            break
        }
    }

    public func sendMessage(_ prompt: String) {
        let cleanPrompt = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanPrompt.isEmpty else { return }

        // Add user message to UI
        messages.append(ChatDisplayMessage(role: "user", text: cleanPrompt))

        // Add pending assistant placeholder
        messages.append(ChatDisplayMessage(role: "assistant", text: "", isStreaming: true))
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
