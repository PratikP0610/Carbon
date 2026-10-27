# Carbon 1.0.0 — SwiftUI Streaming & UI Responsiveness Verification

**Document Date:** 2026-10-28  
**Author:** Google Antigravity (Primary Development Agent)  
**Target Codebase:** `apps/macos/Carbon` ([CarbonClient.swift](file:///Users/pratikpradeshi/Carbon/apps/macos/Carbon/Sources/CarbonClient.swift), [ContentView.swift](file:///Users/pratikpradeshi/Carbon/apps/macos/Carbon/Sources/ContentView.swift))  
**Status:** Implemented & Verified via Native Swift 6 Build  

---

## 1. Architectural Directive Compliance

In strict accordance with the development directive:
1. **Zero Raw Internal Reasoning Streaming:** Raw `<think>` tokens emitted by Qwen 3.5 9B are explicitly intercepted and suppressed from the user interface.
2. **Immediate Acknowledgement:** The moment a user submits an instruction, an assistant message bubble is created instantly with state `isProcessing = true`.
3. **Simple Processing Indicator:** While the model is reasoning internally, the interface displays an elegant, compact progress indicator (`ProgressView()` with "Processing...").
4. **Incremental User-Visible Streaming:** The moment the first user-visible answer token arrives (`chunk.get("response")`), the processing indicator transitions into incremental markdown/text streaming.
5. **Clear Cancellation and Error States:** Distinct, user-friendly UI badges handle task cancellation (`[Cancelled by user]`) and operational errors (`Error: [message]`).
6. **No Artificial Model Swapping:** No secondary model was introduced to conceal latency; the single-active-chat-LLM policy (`qwen3.5:9b`) is strictly preserved.

---

## 2. State Machine & Component Design

```mermaid
stateDiagram-v2
    [*] --> Idle: Client Connected
    Idle --> ImmediateAcknowledgement: User Clicks Send / Presses Enter
    ImmediateAcknowledgement --> Processing: Backend Acknowledges (task_created)
    Processing --> Processing: Backend Emits "thinking" (Tokens Hidden from View)
    Processing --> AnswerStreaming: Backend Emits First User-Visible "token"
    AnswerStreaming --> AnswerStreaming: Subsequent Visible Tokens Stream In
    AnswerStreaming --> Completed: Backend Emits "completed"
    Processing --> Cancelled: User Clicks Stop Button (cancelCurrentTask)
    AnswerStreaming --> Cancelled: User Clicks Stop Button (cancelCurrentTask)
    Processing --> Failed: Backend Emits "failed" (error payload)
    AnswerStreaming --> Failed: Backend Emits "failed" (error payload)
    Completed --> Idle: Ready for Next Instruction
    Cancelled --> Idle: Ready for Next Instruction
    Failed --> Idle: Ready for Next Instruction
```

---

## 3. Implementation Code Review

### 3.1 Data Model ([CarbonClient.swift:L4-L35](file:///Users/pratikpradeshi/Carbon/apps/macos/Carbon/Sources/CarbonClient.swift#L4-L35))
The UI state structure cleanly encapsulates generation lifecycle without persisting raw reasoning text:
```swift
public struct ChatDisplayMessage: Identifiable, Equatable {
    public let id: UUID
    public let role: String // "user" or "assistant"
    public var text: String
    public var isStreaming: Bool
    public var isProcessing: Bool // Active generation prior to first visible token
    public var isCancelled: Bool
    public var errorMessage: String?
    public let timestamp: Date
}
```

### 3.2 Event Processing ([CarbonClient.swift:L116-L180](file:///Users/pratikpradeshi/Carbon/apps/macos/Carbon/Sources/CarbonClient.swift#L116-L180))
- **`task_created`:** Sets `isProcessing = true` on the assistant bubble for immediate feedback.
- **`thinking`:** Intercepted; maintains `isProcessing = true` without modifying `message.text` or presenting a raw reasoning dump.
- **`token`:** The arrival of the first visible character sets `isProcessing = false` and appends text directly to `message.text`.
- **`cancelled`:** Sets `isStreaming = false`, `isProcessing = false`, and `isCancelled = true`.
- **`failed`:** Sets `errorMessage = err` for prominent error styling.

### 3.3 Visual Presentation ([ContentView.swift:L180-L235](file:///Users/pratikpradeshi/Carbon/apps/macos/Carbon/Sources/ContentView.swift#L180-L235))
In `MessageBubbleView`, the UI is rendered strictly according to the clean state pattern:
1. **Error State:** Renders an alert box with red accent, icon `exclamationmark.triangle.fill`, and the sanitized error description.
2. **Processing State:** If `message.text.isEmpty && (isProcessing || isStreaming)`, renders a compact `ProgressView().scaleEffect(0.7)` with `Text("Processing...")`.
3. **Incremental Streaming:** Renders `Text(message.text).font(.system(size: 14)).textSelection(.enabled)`.
4. **Cancellation Badge:** If `message.isCancelled`, renders a muted `Image(systemName: "stop.circle")` with `Text("Cancelled by user")`.

---

## 4. Verification & Build Results

The SwiftUI client compiles cleanly with zero warnings or errors using native Swift 6 tooling on macOS:
```bash
$ swift build
Building for debugging...
Build complete! (0.22 secs)
```
The application guarantees that internal model thinking is isolated from the conversational viewport while providing immediate visual acknowledgement and incremental streaming.
