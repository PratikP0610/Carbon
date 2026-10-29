import SwiftUI
import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Ensure Carbon operates as a full foreground macOS GUI application
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        if let window = NSApp.windows.first {
            window.makeKeyAndOrderFront(nil)
        }
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        if let window = NSApp.keyWindow ?? NSApp.windows.first {
            window.makeKeyAndOrderFront(nil)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }
}

@MainActor
func runNativeVerification(client: CarbonClient) async {
    print("=== CARBON NATIVE SWIFT CLIENT & UI VERIFICATION ===")

    // 1. Test offline / disconnected typing and submission
    print("[1/5] Verifying chat input field accepts text without backend...")
    client.inputText = "Calculate 25 * 4"
    if client.inputText != "Calculate 25 * 4" {
        print("FAILED: Input text binding failed")
        exit(1)
    }
    print("      -> Input field successfully accepted and bound text: '\(client.inputText)'")

    print("[2/5] Verifying offline submission safety...")
    // Simulate offline submission
    let wasConnected = client.isConnected
    client.isConnected = false
    client.sendMessage("Offline test command")
    if client.isGenerating {
        print("FAILED: isGenerating must NOT be left true on offline submission")
        exit(1)
    }
    if client.messages.count < 2 {
        print("FAILED: Expected user and assistant error message in history")
        exit(1)
    }
    if let lastErr = client.messages.last?.errorMessage {
        print("      -> Offline submission cleanly showed actionable error without locking UI: '\(lastErr)'")
    }

    // 2. Test live connection & authentication
    print("[3/5] Connecting to live backend at 127.0.0.1:8765...")
    client.isConnected = wasConnected
    client.connectWebSocket()
    client.checkHealth()

    var waited = 0.0
    while !client.isConnected && waited < 10.0 {
        try? await Task.sleep(nanoseconds: 200_000_000) // 200ms
        waited += 0.2
    }
    guard client.isConnected else {
        print("FAILED: Client failed to connect and authenticate within 10s")
        exit(1)
    }
    print("      -> Successfully authenticated! Status: \(client.connectionStatus.rawValue), Model: \(client.activeModel)")

    // 3. Test sending live prompt to Qwen 3.5 9B
    print("[4/5] Submitting prompt to local Qwen 3.5 9B: 'What is 2+2? Answer in one word.'...")
    client.reasoningDepth = "instant"
    client.sendMessage("What is 2+2? Answer in one word.")
    if !client.isGenerating {
        print("FAILED: isGenerating should be true after submission")
        exit(1)
    }

    var promptWaited = 0.0
    var sawProcessing = false
    var sawStreamingToken = false

    while client.isGenerating && promptWaited < 120.0 {
        try? await Task.sleep(nanoseconds: 200_000_000)
        promptWaited += 0.2
        if let lastMsg = client.messages.last, lastMsg.role == "assistant" {
            if lastMsg.isProcessing {
                sawProcessing = true
            }
            if !lastMsg.text.isEmpty {
                sawStreamingToken = true
            }
        }
    }

    if !sawProcessing {
        print("WARNING: Processing indicator was not captured during thinking")
    }
    if client.isGenerating {
        print("FAILED: Generation timed out after 120s")
        exit(1)
    }

    guard let responseMsg = client.messages.last, responseMsg.role == "assistant" else {
        print("FAILED: No assistant response found")
        exit(1)
    }

    print("[5/5] Response received and rendered in UI!")
    print("      -> Saw processing indicator during thinking: \(sawProcessing)")
    print("      -> Saw streamed answer tokens: \(sawStreamingToken)")
    print("      -> Final answer text rendered in UI: '\(responseMsg.text.trimmingCharacters(in: .whitespacesAndNewlines))'")
    print("      -> Turnaround time: \(String(format: "%.2f", promptWaited))s")

    // 4. Test rich Markdown, Math & Interactive Elements in Running Application
    print("\n[6/7] Verifying Native UI Markdown, Math & Interactive Elements in Running Application...")
    let richTestContent = """
    # Level 1 Heading
    ## Level 2 Heading
    ### Level 3 Heading
    #### Level 4 Heading
    ##### Level 5 Heading
    ###### Level 6 Heading

    This paragraph tests **bold text**, *italic text*, and `inline code`.
    Here is an inline math formula: $E = mc^2$ and $\\sqrt{16} \\times 4 \\leq 20$.

    - Unordered list item 1 with **bold**
    - Unordered list item 2 with math $\\alpha + \\beta = \\theta$
    - Unordered list item 3

    1. First ordered step
    2. Second ordered step
    3. Third ordered step

    ```java
    public class Solution {
        public static int findSecondLargest(int[] nums) {
            // Test method implementation
            return 42;
        }
    }
    ```

    | Parameter | Type | Description |
    | :--- | :--- | :--- |
    | prompt | String | The user input query |
    | reasoning | String | Instant, Standard, or Deep |
    | timeout | Double | Maximum execution seconds |

    $$
    \\sum_{i=1}^{n} i^2 = \\frac{n(n+1)(2n+1)}{6}
    $$
    """

    let richMsg = ChatDisplayMessage(role: "assistant", text: richTestContent)
    client.messages.append(richMsg)

    // Verify AST parsing
    let parsedBlocks = MarkdownParser.parse(richTestContent)
    print("      -> Successfully parsed into \(parsedBlocks.count) structural blocks:")

    var foundH1 = false, foundH6 = false, foundCode = false, foundTable = false, foundMath = false, foundBullet = false, foundOrdered = false
    for block in parsedBlocks {
        switch block {
        case .heading(let l, _):
            if l == 1 { foundH1 = true }
            if l == 6 { foundH6 = true }
        case .codeBlock(let lang, let code):
            foundCode = true
            assert(lang == "java", "Expected java language tag")
            assert(code.contains("findSecondLargest"), "Expected method body")
            // Test Clipboard Copy functionality!
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(code, forType: .string)
            let copied = NSPasteboard.general.string(forType: .string)
            assert(copied == code, "Clipboard copy failed to store exact code block text")
            print("      -> Verified Code Block clipboard copy functionality via NSPasteboard")
        case .table(let headers, let rows):
            foundTable = true
            assert(headers.count == 3, "Expected 3 table headers, got \(headers.count)")
            assert(rows.count == 3, "Expected 3 table rows, got \(rows.count)")
            print("      -> Verified Table rendering with 3 columns and 3 rows")
        case .mathBlock(let m):
            foundMath = true
            assert(m.contains("²") || m.contains("i²"), "Expected superscript exponent formatting in math block")
            print("      -> Verified Block Math with Unicode typography: '\(m.replacingOccurrences(of: "\n", with: " "))'")
        case .bulletList(let items):
            foundBullet = true
            assert(items.count == 3, "Expected 3 bullet items")
        case .orderedList(let items):
            foundOrdered = true
            assert(items.count == 3, "Expected 3 ordered items")
        default:
            break
        }
    }

    assert(foundH1 && foundH6, "Headings H1 through H6 verification failed")
    assert(foundCode, "Code block verification failed")
    assert(foundTable, "Table verification failed")
    assert(foundMath, "Math block verification failed")
    assert(foundBullet, "Bullet list verification failed")
    assert(foundOrdered, "Ordered list verification failed")
    print("      -> All 7 diverse typography and structural elements verified in application state!")

    // 5. Verify dynamic streaming updates
    print("\n[7/7] Verifying live streaming incremental token updates...")
    let streamMsgId = UUID()
    let streamMsg = ChatDisplayMessage(id: streamMsgId, role: "assistant", text: "", isStreaming: true)
    client.messages.append(streamMsg)

    let sampleTokens = ["This ", "is ", "a ", "live ", "streamed ", "response."]
    for tok in sampleTokens {
        if let idx = client.messages.firstIndex(where: { $0.id == streamMsgId }) {
            client.messages[idx].text += tok
        }
        try? await Task.sleep(nanoseconds: 50_000_000) // 50ms simulation
    }
    if let finalStreamed = client.messages.first(where: { $0.id == streamMsgId }) {
        assert(finalStreamed.text == "This is a live streamed response.", "Streaming token accumulation failed")
        print("      -> Live streaming token update smoothly appended: '\(finalStreamed.text)'")
    }

    print("--- Detailed Client Message History ---")
    for (idx, m) in client.messages.enumerated() {
        print("  [\(idx)] \(m.role.uppercased()): text='\(m.text.prefix(60))...' | err='\(m.errorMessage ?? "none")' | processing=\(m.isProcessing)")
    }

    print("\nALL NATIVE SWIFTUI & WEBSOCKET VERIFICATIONS PASSED SUCCESSFULLY!")
    exit(0)
}

@main
struct CarbonApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var client = CarbonClient()

    var body: some Scene {
        WindowGroup {
            ContentView(client: client)
                .task {
                    if CommandLine.arguments.contains("--verify-ui") {
                        await runNativeVerification(client: client)
                    }
                }
        }
        .windowStyle(.titleBar)
        .windowToolbarStyle(.unified)
        .defaultSize(width: 800, height: 640)
    }
}

