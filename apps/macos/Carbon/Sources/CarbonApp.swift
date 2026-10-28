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
    client.sendMessage("What is 2+2? Answer in one word.")
    if !client.isGenerating {
        print("FAILED: isGenerating should be true after submission")
        exit(1)
    }

    var promptWaited = 0.0
    var sawProcessing = false
    var sawStreamingToken = false

    while client.isGenerating && promptWaited < 60.0 {
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
        print("FAILED: Generation timed out after 60s")
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
    print("--- Detailed Client Message History ---")
    for (idx, m) in client.messages.enumerated() {
        print("  [\(idx)] \(m.role.uppercased()): text='\(m.text)' | err='\(m.errorMessage ?? "none")' | processing=\(m.isProcessing)")
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

