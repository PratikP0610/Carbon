import Dispatch
import Foundation

@main
struct Stage5DTestRunner {
    @MainActor
    static func main() async {
        print("=== STAGE 5-D SWIFT UNIT VERIFICATION (D1 & D4) ===")

        // 1. D1: Memory pressure event mapping
        print("[1/5] Verifying Mach DispatchSource.MemoryPressureEvent mapping...")
        assert(CarbonMemoryPressureLevel.from(dispatchEvent: .normal) == .normal)
        assert(CarbonMemoryPressureLevel.from(dispatchEvent: .warning) == .warning)
        assert(CarbonMemoryPressureLevel.from(dispatchEvent: .critical) == .critical)
        assert(CarbonMemoryPressureLevel.from(dispatchEvent: [.warning, .critical]) == .critical)
        print("      ✓ Event mapping verified (.normal->1, .warning->2, .critical->4)")

        // 2. D1: Debounce & recovery hysteresis state machine
        print("[2/5] Verifying debounce oscillation suppression & recovery hysteresis...")
        final class EmittedCollector: @unchecked Sendable {
            var items: [CarbonMemoryPressureLevel] = []
        }
        let emitted = EmittedCollector()
        let debouncer = MemoryPressureDebouncer(
            debounceInterval: 0.5,
            recoveryHysteresisInterval: 10.0
        ) { lvl in
            emitted.items.append(lvl)
        }

        // Duplicate normal at baseline ignored
        assert(debouncer.ingest(.normal, scheduleTimer: false) == nil)
        // Rapid oscillation warning -> critical -> warning uses short debounce window
        assert(debouncer.ingest(.warning, scheduleTimer: false) == 0.5)
        assert(debouncer.ingest(.critical, scheduleTimer: false) == 0.5)
        assert(debouncer.ingest(.warning, scheduleTimer: false) == 0.5)
        assert(debouncer.pendingLevel == .warning)
        assert(debouncer.commitPendingLevel() == .warning)
        assert(emitted.items == [.warning])

        // Escalation to critical -> debounce 0.5s -> commit
        assert(debouncer.ingest(.critical, scheduleTimer: false) == 0.5)
        assert(debouncer.commitPendingLevel() == .critical)
        assert(emitted.items == [.warning, .critical])

        // De-escalation critical -> normal uses longer 10.0s recovery hysteresis
        assert(debouncer.ingest(.normal, scheduleTimer: false) == 10.0)
        // Transient rebound back to critical resets/keeps settled state without flapping
        assert(debouncer.ingest(.critical, scheduleTimer: false) == nil)
        assert(emitted.items == [.warning, .critical])

        // Sustained drop waits 10.0s then commits cleanly
        assert(debouncer.ingest(.normal, scheduleTimer: false) == 10.0)
        assert(debouncer.commitPendingLevel() == .normal)
        assert(emitted.items == [.warning, .critical, .normal])
        debouncer.stop()
        print("      ✓ Oscillation debounced and de-escalation hysteresis enforced cleanly")

        // 3. D1: Loopback HTTP request authentication & mock dispatch
        print("[3/5] Verifying authenticated loopback POST /api/v1/system/memory-pressure...")
        let baseURL = URL(string: "http://127.0.0.1:8765")!
        let req = MemoryPressureMonitor.buildRequest(
            baseURL: baseURL,
            level: .critical,
            token: "test-bearer-token"
        )
        assert(req.httpMethod == "POST")
        assert(req.url?.absoluteString == "http://127.0.0.1:8765/api/v1/system/memory-pressure")
        assert(req.value(forHTTPHeaderField: "Authorization") == "Bearer test-bearer-token")
        if let bodyData = req.httpBody,
           let parsed = try? JSONSerialization.jsonObject(with: bodyData) as? [String: Any] {
            assert(parsed["level"] as? Int == 4)
            assert(parsed["source"] as? String == "macos_dispatch_source")
        } else {
            assertionFailure("Missing or malformed JSON body for memory-pressure request")
        }

        let monitor = MemoryPressureMonitor(
            baseURL: baseURL,
            debounceInterval: 0.01,
            recoveryHysteresisInterval: 0.05,
            tokenProvider: { "test-bearer-token" },
            requestSender: { sentReq in
                assert(sentReq.value(forHTTPHeaderField: "Authorization") == "Bearer test-bearer-token")
                let resp = HTTPURLResponse(url: sentReq.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
                return (Data("{\"status\":\"applied\"}".utf8), resp)
            }
        )
        monitor.start()
        monitor.start() // idempotent
        await monitor.postPressureLevel(.warning)
        monitor.stop()
        print("      ✓ Authenticated request builder and monitor lifecycle verified")

        // 4. D4: CarbonClient 40ms token coalescing, ordering, & zero character loss
        print("[4/5] Verifying CarbonClient ~40ms token coalescing and exact fidelity...")
        let client = CarbonClient(tokenCoalesceInterval: 0.04)
        client.messages.append(ChatDisplayMessage(role: "assistant", text: "", isStreaming: true, isProcessing: true))

        // Sleep slightly past 40ms cleanly? Even without sleep: token 0 triggers immediate flush (systemUptime >= 0.04)
        var expectedText = ""
        for idx in 0..<50 {
            let chunk = "w\(idx) "
            expectedText += chunk
            client.handleServerEventForTesting("{\"event\":\"token\",\"text\":\"\(chunk)\"}")
        }
        // All 50 rapid synchronous frames MUST have coalesced aggressively (token 0 immediate, tokens 1..49 buffered)
        assert(client.uiTokenFlushCount == 1, "Expected exactly 1 flush before timer/completion for burst, got \(client.uiTokenFlushCount)")

        //Wait past 40ms interval and flush second burst via timer or completion flush
        try? await Task.sleep(nanoseconds: 55_000_000)
        assert(client.uiTokenFlushCount == 2, "Expected 2 total UI flushes for burst, got \(client.uiTokenFlushCount)")
        assert(client.messages.last?.text == expectedText, "Token order or character fidelity broken after timer flush")

        // Burst right before completion must flush synchronously on completed event with ZERO character loss
        for idx in 50..<65 {
            let chunk = "x\(idx)!"
            expectedText += chunk
            client.handleServerEventForTesting("{\"event\":\"token\",\"text\":\"\(chunk)\"}")
        }
        client.handleServerEventForTesting("{\"event\":\"completed\"}")
        assert(client.messages.last?.isStreaming == false)
        assert(client.messages.last?.text == expectedText, "Completed flush dropped or reordered tokens!")

        // Partial tokens also flush cleanly on cancelled
        client.messages.append(ChatDisplayMessage(role: "assistant", text: "", isStreaming: true, isProcessing: true))
        try? await Task.sleep(nanoseconds: 55_000_000)
        client.handleServerEventForTesting("{\"event\":\"token\",\"text\":\"hello \"}")
        client.handleServerEventForTesting("{\"event\":\"token\",\"text\":\"world\"}")
        client.handleServerEventForTesting("{\"event\":\"cancelled\"}")
        assert(client.messages.last?.text == "hello world")
        assert(client.messages.last?.isCancelled == true)
        print("      ✓ 40ms token coalescing, exact FIFO ordering, and completion/cancel flush verified (flushes=\(client.uiTokenFlushCount) for 65+ tokens)")

        // 5. D4: MarkdownCoordinator 80ms throttle & completion flush fidelity
        print("[5/5] Verifying MarkdownCoordinator 80ms coalescing and final completion parse...")
        let coordinator = MarkdownCoordinator(text: "# Header\n\n")
        var mdText = "# Header\n\n"
        for i in 1...40 {
            mdText += "Item \(i) "
            coordinator.update(text: mdText, isStreaming: true)
        }
        // 80ms throttle prevented 40 redundant synchronous parses during burst
        assert(coordinator.parseCount <= 2, "Expected <= 2 throttled parses during burst, got \(coordinator.parseCount)")
        coordinator.update(text: mdText, isStreaming: false)
        if case .paragraph(let body) = coordinator.blocks.last {
            assert(body.contains("Item 40"), "Final completion parse missing last token")
        } else {
            assertionFailure("Expected final paragraph block containing Item 40")
        }
        print("      ✓ MarkdownCoordinator 80ms throttle + final completion AST parse verified")
        print("\nALL STAGE 5-D SWIFT TESTS PASSED SUCCESSFULLY!")
    }
}
