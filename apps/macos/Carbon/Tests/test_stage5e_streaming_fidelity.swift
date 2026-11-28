import Dispatch
import Foundation

@main
struct Stage5EStreamingFidelityRunner {
    @MainActor
    static func main() async {
        print("=== STAGE 5-E TIER 1: SWIFTUI STREAMING FIDELITY & RATE GATE (E5) ===")

        // 1. Sustained streaming rate <= 25 updates/sec & exact 240-token FIFO fidelity
        print("[1/3] Verifying <= 25 UI updates/sec and exact 240-token ordering during sustained streaming...")
        let client = CarbonClient(tokenCoalesceInterval: 0.04)
        client.messages.append(
            ChatDisplayMessage(role: "assistant", text: "", isStreaming: true, isProcessing: true)
        )
        client.isGenerating = true
        client.currentTaskId = "stage5e-stream-rate"

        var expectedParts: [String] = []
        let totalTokens = 240
        // Settle past initial interval so token 0 renders immediately, then stream smoothly over ~1.0s
        try? await Task.sleep(nanoseconds: 50_000_000)

        let streamStart = ProcessInfo.processInfo.systemUptime
        for idx in 0..<totalTokens {
            let chunk = "[\(idx):αβ🚀] "
            expectedParts.append(chunk)
            let json = "{\"event\":\"token\",\"text\":\"\(chunk)\"}"
            client.handleServerEventForTesting(json)
            // ~4.5ms spacing across 240 tokens ~= 1.08s sustained arrival (~220 tokens/sec input)
            try? await Task.sleep(nanoseconds: 4_500_000)
        }
        let elapsedSeconds = ProcessInfo.processInfo.systemUptime - streamStart
        let flushesDuringStream = client.uiTokenFlushCount
        let periodicFlushesDuringStream = max(0, flushesDuringStream - 1)
        let observedPeriodicRate = elapsedSeconds > 0
            ? Double(periodicFlushesDuringStream) / elapsedSeconds
            : 0.0

        client.handleServerEventForTesting("{\"event\":\"completed\"}")

        let expectedFullText = expectedParts.joined()
        let renderedText = client.messages.last?.text ?? ""
        assert(renderedText == expectedFullText, "Token ordering or character fidelity mismatch after 240-token completion")
        assert(client.messages.last?.isStreaming == false)
        assert(observedPeriodicRate <= 25.0, "Streaming UI flush rate exceeded 25 updates/sec: \(observedPeriodicRate)")

        // Verify exact sequential index ordering in final text
        var cursor = renderedText.startIndex
        for idx in 0..<totalTokens {
            let expectedToken = "[\(idx):αβ🚀] "
            let slice = renderedText[cursor...].prefix(expectedToken.count)
            assert(slice == expectedToken, "Out-of-order token at index \(idx)")
            cursor = renderedText.index(cursor, offsetBy: expectedToken.count)
        }
        print(
            String(
                format: "      ✓ Rate gate passed: periodic=%.2f updates/sec (streamFlushes=%d in %.3fs, postCompleteTotal=%d for %d tokens), 100%% FIFO fidelity",
                observedPeriodicRate,
                flushesDuringStream,
                elapsedSeconds,
                client.uiTokenFlushCount,
                totalTokens
            )
        )

        // 2. Zero character loss across all terminal stream events & local cancellation
        print("[2/3] Verifying zero character loss across completed, cancelled, failed, error, and local cancel...")
        let trickyTokens = [
            "```swift\n",
            "let msg = \"hello\\n",
            "world 🚀 αβγ\"\n",
            "```\n",
            "Tail-char-preservation!"
        ]

        // 2a. Server 'cancelled' event
        client.messages.append(ChatDisplayMessage(role: "assistant", text: "", isStreaming: true, isProcessing: true))
        client.isGenerating = true
        client.currentTaskId = "stage5e-cancel-server"
        try? await Task.sleep(nanoseconds: 50_000_000)
        var expectedPartial = ""
        for tok in trickyTokens {
            expectedPartial += tok
            let escaped = tok
                .replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: "\"", with: "\\\"")
                .replacingOccurrences(of: "\n", with: "\\n")
            client.handleServerEventForTesting("{\"event\":\"token\",\"text\":\"\(escaped)\"}")
        }
        client.handleServerEventForTesting("{\"event\":\"cancelled\"}")
        assert(client.messages.last?.text == expectedPartial, "Server cancelled dropped buffered characters")
        assert(client.messages.last?.isCancelled == true)
        assert(client.messages.last?.isStreaming == false)

        // 2b. Local cancelCurrentTask()
        client.messages.append(ChatDisplayMessage(role: "assistant", text: "", isStreaming: true, isProcessing: true))
        client.isGenerating = true
        client.currentTaskId = "stage5e-cancel-local"
        try? await Task.sleep(nanoseconds: 50_000_000)
        for tok in trickyTokens {
            let escaped = tok
                .replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: "\"", with: "\\\"")
                .replacingOccurrences(of: "\n", with: "\\n")
            client.handleServerEventForTesting("{\"event\":\"token\",\"text\":\"\(escaped)\"}")
        }
        client.cancelCurrentTask()
        assert(client.messages.last?.text == expectedPartial, "Local cancelCurrentTask dropped buffered characters")
        assert(client.messages.last?.isCancelled == true)
        assert(client.messages.last?.isStreaming == false)

        // 2c. Server 'failed' event
        client.messages.append(ChatDisplayMessage(role: "assistant", text: "", isStreaming: true, isProcessing: true))
        client.isGenerating = true
        client.currentTaskId = "stage5e-failed"
        try? await Task.sleep(nanoseconds: 50_000_000)
        for tok in trickyTokens {
            let escaped = tok
                .replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: "\"", with: "\\\"")
                .replacingOccurrences(of: "\n", with: "\\n")
            client.handleServerEventForTesting("{\"event\":\"token\",\"text\":\"\(escaped)\"}")
        }
        client.handleServerEventForTesting("{\"event\":\"failed\",\"error\":\"simulated failure\"}")
        assert(client.messages.last?.text == expectedPartial, "Server failed dropped buffered characters")
        assert(client.messages.last?.errorMessage == "simulated failure")
        assert(client.messages.last?.isStreaming == false)

        // 2d. Server 'error' event (while generating)
        client.messages.append(ChatDisplayMessage(role: "assistant", text: "", isStreaming: true, isProcessing: true))
        client.isGenerating = true
        client.currentTaskId = "stage5e-error"
        try? await Task.sleep(nanoseconds: 50_000_000)
        for tok in trickyTokens {
            let escaped = tok
                .replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: "\"", with: "\\\"")
                .replacingOccurrences(of: "\n", with: "\\n")
            client.handleServerEventForTesting("{\"event\":\"token\",\"text\":\"\(escaped)\"}")
        }
        client.handleServerEventForTesting("{\"event\":\"error\",\"message\":\"simulated stream error\"}")
        assert(client.messages.last?.text == expectedPartial, "Server error dropped buffered characters")
        assert(client.messages.last?.errorMessage == "simulated stream error")
        assert(client.messages.last?.isStreaming == false)
        print("      ✓ Zero character loss verified across completed, cancelled, local cancel, failed, and error")

        // 3. MarkdownCoordinator 80ms coalescing ceiling during sustained streaming + final AST completeness
        print("[3/3] Verifying MarkdownCoordinator 80ms coalescing and final completion parse fidelity...")
        let coordinator = MarkdownCoordinator(text: "# Stage 5-E\n\n")
        var mdText = "# Stage 5-E\n\n"
        let mdStart = ProcessInfo.processInfo.systemUptime
        for idx in 1...80 {
            mdText += "tok\(idx) "
            coordinator.update(text: mdText, isStreaming: true)
            try? await Task.sleep(nanoseconds: 5_000_000) // ~0.40s sustained
        }
        let mdElapsed = ProcessInfo.processInfo.systemUptime - mdStart
        let streamingParses = coordinator.parseCount
        let periodicParseRate = mdElapsed > 0 ? Double(max(0, streamingParses - 1)) / mdElapsed : 0.0
        assert(periodicParseRate <= 13.5, "Markdown streaming parse rate too high: \(periodicParseRate)")

        coordinator.update(text: mdText, isStreaming: false)
        if case .paragraph(let body) = coordinator.blocks.last {
            assert(body.contains("tok80"), "Final Markdown parse missing trailing token")
        } else {
            assertionFailure("Expected trailing paragraph AST block")
        }
        print(
            String(
                format: "      ✓ MarkdownCoordinator rate=%.2f parses/sec (%d parses in %.3fs for 80 tokens) + final AST verified",
                periodicParseRate,
                streamingParses,
                mdElapsed
            )
        )
        print("\nALL STAGE 5-E TIER 1 SWIFT STREAMING FIDELITY TESTS PASSED SUCCESSFULLY!")
    }
}
