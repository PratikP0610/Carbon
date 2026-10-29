import SwiftUI
import AppKit

public struct ContentView: View {
    @ObservedObject var client: CarbonClient
    @FocusState private var isInputFocused: Bool

    public init(client: CarbonClient) {
        self.client = client
    }

    public var body: some View {
        VStack(spacing: 0) {
            // Header Bar
            headerView

            Divider()

            // Chat Scroll Area
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if client.messages.isEmpty {
                            emptyStateView
                        } else {
                            ForEach(client.messages) { message in
                                MessageBubbleView(message: message)
                                    .id(message.id)
                            }
                        }
                    }
                    .padding()
                }
                .onChange(of: client.messages.count) { _, _ in
                    if let last = client.messages.last {
                        withAnimation {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
                .onChange(of: client.isGenerating) { _, isGen in
                    if !isGen {
                        isInputFocused = true
                    }
                }
            }

            Divider()

            // Bottom Input Bar
            inputBarView
        }
        .frame(minWidth: 640, minHeight: 480)
        .background(Color(NSColor.windowBackgroundColor))
        .onAppear {
            isInputFocused = true
            NSApp.activate(ignoringOtherApps: true)
            DispatchQueue.main.async {
                (NSApp.keyWindow ?? NSApp.windows.first)?.makeKeyAndOrderFront(nil)
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            isInputFocused = true
        }

    }

    private var headerView: some View {
        HStack(spacing: 12) {
            HStack(spacing: 6) {
                Image(systemName: "cpu")
                    .foregroundColor(.accentColor)
                    .font(.system(size: 16, weight: .bold))
                Text("CARBON")
                    .font(.system(size: 14, weight: .black, design: .monospaced))
            }

            Spacer()

            // Model Badge
            HStack(spacing: 4) {
                Text("MODEL:")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundColor(.secondary)
                Text(client.activeModel)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.secondary.opacity(0.15))
                    .cornerRadius(4)
            }

            // Reasoning Depth Picker
            Picker("", selection: $client.reasoningDepth) {
                Text("Auto").tag("auto")
                Text("Instant").tag("instant")
                Text("Standard").tag("standard")
                Text("Deep").tag("deep")
            }
            .pickerStyle(.segmented)
            .frame(width: 280)

            // Connection Status Pill
            HStack(spacing: 4) {
                Circle()
                    .fill(statusColor)
                    .frame(width: 8, height: 8)
                Text(client.connectionStatus.rawValue)
                    .font(.system(size: 10, weight: .bold))
                    .foregroundColor(statusColor)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(Color.secondary.opacity(0.1))
            .cornerRadius(12)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }

    private var statusColor: Color {
        if client.isConnected {
            return Color.green
        } else if client.isHealthOk {
            return Color.orange
        } else {
            return Color.red
        }
    }

    private var emptyStateView: some View {
        VStack(spacing: 12) {
            Spacer().frame(height: 80)
            Image(systemName: "sparkles")
                .font(.system(size: 44))
                .foregroundColor(.accentColor)
            Text("Carbon 1.0.0")
                .font(.title2.weight(.bold))
            Text("Intelligent, local-first computing assistant powered by Qwen 3.5 9B on Apple Silicon.")
                .font(.body)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
        }
        .frame(maxWidth: .infinity)
    }

    private var inputBarView: some View {
        HStack(spacing: 10) {
            TextField("Ask Carbon anything or give a task...", text: $client.inputText)
                .textFieldStyle(.plain)
                .font(.system(size: 14))
                .focused($isInputFocused)
                .padding(10)
                .background(Color(NSColor.controlBackgroundColor))
                .cornerRadius(8)
                .onSubmit {
                    submitCurrentText()
                }

            if client.isGenerating {
                Button(action: {
                    client.cancelCurrentTask()
                    isInputFocused = true
                }) {
                    Image(systemName: "stop.fill")
                        .foregroundColor(.red)
                        .padding(8)
                }
                .buttonStyle(.plain)
                .help("Cancel task")
            } else {
                Button(action: {
                    submitCurrentText()
                }) {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 24))
                        .foregroundColor(client.inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? .secondary : .accentColor)
                }
                .buttonStyle(.plain)
                .disabled(client.inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .help("Send instruction")
            }
        }
        .padding(12)
    }

    private func submitCurrentText() {
        let text = client.inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        client.inputText = ""
        client.sendMessage(text)
        isInputFocused = true
    }
}

public struct MessageBubbleView: View {
    public let message: ChatDisplayMessage

    public var body: some View {
        HStack(alignment: .top, spacing: 10) {
            if message.role == "user" {
                Spacer()
                Text(message.text)
                    .font(.system(size: 14))
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(Color.accentColor)
                    .foregroundColor(.white)
                    .cornerRadius(12)
                    .frame(maxWidth: 520, alignment: .trailing)
            } else {
                Image(systemName: "cpu")
                    .foregroundColor(.accentColor)
                    .font(.system(size: 16, weight: .bold))
                    .padding(.top, 2)

                VStack(alignment: .leading, spacing: 8) {
                    // 1. Error state
                    if let err = message.errorMessage {
                        HStack(spacing: 6) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundColor(.red)
                            Text("Error: \(err)")
                                .font(.system(size: 13, weight: .semibold))
                                .foregroundColor(.red)
                        }
                        .padding(8)
                        .background(Color.red.opacity(0.1))
                        .cornerRadius(8)
                    }

                    // 2. Processing / Progress indicator
                    if message.text.isEmpty && (message.isProcessing || message.isStreaming) && message.errorMessage == nil && !message.isCancelled {
                        HStack(spacing: 8) {
                            ProgressView()
                                .scaleEffect(0.7)
                            let label: String = {
                                if let depth = message.executedReasoningDepth {
                                    return depth == "instant" ? "Responding..." : "Reasoning (\(depth.capitalized))..."
                                }
                                return "Processing..."
                            }()
                            Text(label)
                                .font(.system(size: 13, weight: .medium))
                                .foregroundColor(.secondary)
                        }
                        .padding(.vertical, 4)
                    }

                    // 3. Incremental user-visible answer text with Markdown & Math formatting
                    if !message.text.isEmpty {
                        MarkdownView(text: message.text, isStreaming: message.isStreaming)
                    }

                    // 4. Executed mode badge
                    if let depth = message.executedReasoningDepth, !message.text.isEmpty {
                        HStack(spacing: 4) {
                            Text(depth.uppercased())
                                .font(.system(size: 9, weight: .bold, design: .monospaced))
                                .foregroundColor(.secondary)
                                .padding(.horizontal, 4)
                                .padding(.vertical, 1)
                                .background(Color.secondary.opacity(0.12))
                                .cornerRadius(3)
                        }
                        .padding(.top, 2)
                    }

                    // 4. Cancellation state
                    if message.isCancelled {
                        HStack(spacing: 4) {
                            Image(systemName: "stop.circle")
                                .font(.system(size: 11))
                            Text("Cancelled by user")
                                .font(.system(size: 11, weight: .medium))
                        }
                        .foregroundColor(.secondary)
                        .padding(.top, 2)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(Color.secondary.opacity(0.1))
                .cornerRadius(12)
                .frame(maxWidth: 680, alignment: .leading)

                Spacer()
            }
        }
    }
}
