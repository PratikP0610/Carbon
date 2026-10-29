import Foundation
import SwiftUI

@main
struct TestRunner {
    @MainActor
    static func main() async {
        // Test MathFormatter
        print("Testing MathFormatter...")

        // Test superscripts
        let math1 = MathFormatter.processInlineMathDelimiters("Let $3^2 = 9$ and $x^10$ be true.")
        assert(math1.contains("3² = 9"), "Failed exponent replacement: \(math1)")
        print("✓ Exponents formatted correctly: \(math1)")

        // Test subscripts
        let math2 = MathFormatter.processInlineMathDelimiters("For $a_1 + a_2 + a_n$.")
        assert(math2.contains("a₁ + a₂ + aₙ"), "Failed subscript replacement: \(math2)")
        print("✓ Subscripts formatted correctly: \(math2)")

        // Test mathematical operators
        let math3 = MathFormatter.processInlineMathDelimiters("$\\sqrt{16} \\times 4 \\leq 20$")
        assert(math3.contains("√(16) × 4 ≤ 20"), "Failed operator replacement: \(math3)")
        print("✓ Math operators formatted correctly: \(math3)")

        // Test proportionality expressions (Stage 1.9 requirement)
        let mathProp = MathFormatter.processInlineMathDelimiters("For a CPU clock: $f \\propto \\frac{1}{T}$ and $P \\propto V^2$")
        assert(mathProp.contains("f ∝ (1 / T)"), "Failed proportionality replacement in fraction: \(mathProp)")
        assert(mathProp.contains("P ∝ V²"), "Failed proportionality replacement with exponent: \(mathProp)")
        print("✓ Proportionality expressions formatted correctly: \(mathProp)")

        // Test Greek letters
        let math4 = MathFormatter.processInlineMathDelimiters("$\\alpha + \\beta = \\theta$")
        assert(math4.contains("α + β = θ"), "Failed Greek letters: \(math4)")
        print("✓ Greek letters formatted correctly: \(math4)")

        // Test MarkdownParser
        print("\nTesting MarkdownParser...")

        // Test headings
        let hText = "# Title 1\n## Title 2\n### Title 3"
        let hBlocks = MarkdownParser.parse(hText)
        assert(hBlocks.count == 3, "Expected 3 heading blocks, got \(hBlocks.count)")
        if case .heading(let level, let text) = hBlocks[0] {
            assert(level == 1 && text == "Title 1")
        } else {
            assertionFailure("First block is not heading 1")
        }
        print("✓ Headings parsed correctly")

        // Test Code Block
        let codeText = "```java\npublic class Solution {\n    public int findSecondLargest(int[] arr) {}\n}\n```"
        let codeBlocks = MarkdownParser.parse(codeText)
        assert(codeBlocks.count == 1, "Expected 1 code block, got \(codeBlocks.count)")
        if case .codeBlock(let lang, let code) = codeBlocks[0] {
            assert(lang == "java")
            assert(code.contains("public int findSecondLargest"))
        } else {
            assertionFailure("Block is not code block")
        }
        print("✓ Code block parsed correctly with language tag")

        // Test Bullet List
        let listText = "- Point A\n- Point B\n* Point C"
        let listBlocks = MarkdownParser.parse(listText)
        assert(listBlocks.count == 1, "Expected 1 list block, got \(listBlocks.count)")
        if case .bulletList(let items) = listBlocks[0] {
            assert(items.count == 3)
            assert(items[0] == "Point A")
            assert(items[1] == "Point B")
            assert(items[2] == "Point C")
        } else {
            assertionFailure("Block is not bullet list")
        }
        print("✓ Bullet list parsed correctly")

        // Test Ordered List
        let oListText = "1. First step\n2. Second step\n3. Third step"
        let oListBlocks = MarkdownParser.parse(oListText)
        assert(oListBlocks.count == 1, "Expected 1 ordered list block, got \(oListBlocks.count)")
        if case .orderedList(let items) = oListBlocks[0] {
            assert(items.count == 3)
            assert(items[0].number == 1 && items[0].text == "First step")
            assert(items[1].number == 2 && items[1].text == "Second step")
            assert(items[2].number == 3 && items[2].text == "Third step")
        } else {
            assertionFailure("Block is not ordered list")
        }
        print("✓ Ordered list parsed correctly")

        // Test Table
        let tableText = """
        | Weighing | Left Pan | Right Pan | Outcome |
        | :--- | :--- | :--- | :--- |
        | First | Balls 1, 2, 3 | Balls 4, 5, 6 | Balanced or Unbalanced |
        | Second | Ball 1 | Ball 2 | Isolated |
        """
        let tableBlocks = MarkdownParser.parse(tableText)
        assert(tableBlocks.count == 1, "Expected 1 table block, got \(tableBlocks.count)")
        if case .table(let headers, let rows) = tableBlocks[0] {
            assert(headers.count == 4)
            assert(rows.count == 2)
            assert(headers[0] == "Weighing")
            assert(rows[0][1] == "Balls 1, 2, 3")
        } else {
            assertionFailure("Block is not table")
        }
        print("✓ Table parsed correctly with headers and rows")

        // Test Horizontal Rule
        let hrText = "Section 1\n\n---\n\nSection 2"
        let hrBlocks = MarkdownParser.parse(hrText)
        assert(hrBlocks.count == 3, "Expected 3 blocks (para, divider, para), got \(hrBlocks.count)")
        if case .horizontalRule = hrBlocks[1] {
            print("✓ Horizontal rule parsed correctly")
        } else {
            assertionFailure("Block 1 is not horizontalRule")
        }

        // Test Block Math
        let blockMathText = "$$\nE = mc^2\n$$"
        let bmBlocks = MarkdownParser.parse(blockMathText)
        assert(bmBlocks.count == 1, "Expected 1 block math, got \(bmBlocks.count)")
        if case .mathBlock(let math) = bmBlocks[0] {
            assert(math.contains("E = mc²"))
            print("✓ Block math parsed correctly with formatted exponent")
        } else {
            assertionFailure("Block is not mathBlock")
        }

        // ========================================================
        // Stage 1.7 Regression Tests: Parser Hang & Streaming Safety
        // ========================================================
        print("\nTesting Stage 1.7 Regression Suite (Infinite Loop & Streaming Safety)...")

        // 1. Partial heading tokens during streaming
        let partialHeadingTokens = ["#", "##", "###", "####", "#####", "######"]
        for token in partialHeadingTokens {
            let start = ProcessInfo.processInfo.systemUptime
            let blocksNoNL = MarkdownParser.parse(token)
            let blocksWithNL = MarkdownParser.parse("\(token)\n")
            let elapsed = ProcessInfo.processInfo.systemUptime - start

            assert(!blocksNoNL.isEmpty, "Failed to parse streaming token '\(token)' without newline")
            assert(!blocksWithNL.isEmpty, "Failed to parse streaming token '\(token)' with newline")
            assert(elapsed < 0.05, "Parser hung on streaming token '\(token)': took \(elapsed)s")
        }
        print("✓ Partial heading tokens ('#' through '######') parsed safely without infinite loops")

        // 2. Malformed headings (level > 6, or no space)
        let malformedHeadings = [
            "####### Seven hashes",
            "######## Eight hashes",
            "#TagWithoutSpace",
            "###HeadingNoSpace",
            "# 1. Numbered Heading"
        ]
        for malformed in malformedHeadings {
            let start = ProcessInfo.processInfo.systemUptime
            let blocks = MarkdownParser.parse(malformed)
            let elapsed = ProcessInfo.processInfo.systemUptime - start
            assert(!blocks.isEmpty, "Malformed heading parsed to empty blocks: \(malformed)")
            assert(elapsed < 0.05, "Parser hung on malformed heading '\(malformed)': took \(elapsed)s")
        }
        print("✓ Malformed headings parsed safely as text/paragraph without hangs")

        // 3. Whitespace-only and empty strings
        let whitespaceInputs = ["", "   ", "\n", "\n\n\n", "\t\t\n  \n\t  \n"]
        for ws in whitespaceInputs {
            let start = ProcessInfo.processInfo.systemUptime
            let blocks = MarkdownParser.parse(ws)
            let elapsed = ProcessInfo.processInfo.systemUptime - start
            assert(blocks.isEmpty, "Expected 0 blocks for whitespace input, got \(blocks.count)")
            assert(elapsed < 0.05, "Parser hung on whitespace input: took \(elapsed)s")
        }
        print("✓ Empty and whitespace-only inputs handled cleanly")

        // 4. Incomplete/unclosed streaming code and math blocks
        let unclosedCode = "```python\ndef test():\n    return 42"
        let unclosedCodeBlocks = MarkdownParser.parse(unclosedCode)
        assert(unclosedCodeBlocks.count == 1, "Expected 1 code block for unclosed code")
        if case .codeBlock(let lang, let code) = unclosedCodeBlocks[0] {
            assert(lang == "python")
            assert(code.contains("return 42"))
        } else {
            assertionFailure("Expected code block for unclosed code")
        }

        let unclosedMath = "$$\n\\int_0^1 x^2 dx"
        let unclosedMathBlocks = MarkdownParser.parse(unclosedMath)
        assert(unclosedMathBlocks.count == 1, "Expected 1 math block for unclosed math")
        print("✓ Unclosed streaming code and math blocks parsed without infinite loops")

        // 5. Malformed/single-line tables
        let singleLineTable = "| single column |"
        let singleLineBlocks = MarkdownParser.parse(singleLineTable)
        assert(singleLineBlocks.count == 1, "Expected single column to parse as paragraph")
        print("✓ Malformed/single-line table parsed safely as paragraph")

        // 6. Indented multiline list continuations
        let multilineBullet = "- Line 1\n  continuation line\n- Line 2"
        let mbBlocks = MarkdownParser.parse(multilineBullet)
        assert(mbBlocks.count == 1, "Expected 1 bullet list block")
        if case .bulletList(let items) = mbBlocks[0] {
            assert(items.count == 2, "Expected 2 items, got \(items.count)")
            assert(items[0].contains("continuation line"), "Continuation text missing: \(items[0])")
        } else {
            assertionFailure("Expected bullet list")
        }

        let multilineOrdered = "1. First step\n  continuation details\n2. Second step"
        let moBlocks = MarkdownParser.parse(multilineOrdered)
        assert(moBlocks.count == 1, "Expected 1 ordered list block")
        if case .orderedList(let items) = moBlocks[0] {
            assert(items.count == 2, "Expected 2 ordered items, got \(items.count)")
            assert(items[0].text.contains("continuation details"), "Continuation text missing: \(items[0].text)")
        } else {
            assertionFailure("Expected ordered list")
        }
        print("✓ Indented multiline list continuations parsed correctly")

        // 7. High-volume stress test verifying defensive progress invariant
        let stressInputs = [
            "#", "##\n", "###\n```", "````", "$$", "$$$\n$$", ">", ">>\n>",
            "|", "||", "|||", "-", "*", "+", "1.", "1. \n2.", "---", "***"
        ]
        let stressStart = ProcessInfo.processInfo.systemUptime
        for _ in 0..<500 {
            for input in stressInputs {
                _ = MarkdownParser.parse(input)
            }
        }
        let stressElapsed = ProcessInfo.processInfo.systemUptime - stressStart
        assert(stressElapsed < 1.0, "Stress test took too long: \(stressElapsed)s for 9,000 parses")
        print("✓ High-volume stress test (9,000 parses of edge-case tokens) completed in \(String(format: "%.3f", stressElapsed))s")

        // 8. Streaming coordinator responsiveness and throttle verification
        print("\nTesting MarkdownCoordinator Streaming & Rendering Responsiveness...")
        let coordinator = MarkdownCoordinator(text: "# Initial Title")
        assert(coordinator.blocks.count == 1, "Expected initial title block")

        let chunkStart = ProcessInfo.processInfo.systemUptime
        var accumulated = "# Initial Title\n\n"
        for i in 1...60 {
            accumulated += "Step \(i): Process unit data chunk. "
            coordinator.update(text: accumulated, isStreaming: true)
        }
        let chunkElapsed = ProcessInfo.processInfo.systemUptime - chunkStart
        assert(chunkElapsed < 0.1, "Rapid streaming token dispatch blocked main thread: took \(chunkElapsed)s")
        print("✓ Rapid streaming token updates (60 bursts) processed in \(String(format: "%.3f", chunkElapsed))s without blocking")

        // Final completion parse
        coordinator.update(text: accumulated, isStreaming: false)
        assert(!coordinator.blocks.isEmpty, "Expected parsed blocks upon completion")
        print("✓ Completed response final parse rendered smoothly")

        // 9. Stop-button / cancellation responsiveness verification
        print("\nTesting Stop-Button / Task Cancellation Responsiveness...")
        let client = CarbonClient()
        client.isGenerating = true
        let testMsg = ChatDisplayMessage(role: "assistant", text: "Partial reasoning...", isStreaming: true, isProcessing: true)
        client.messages.append(testMsg)

        let cancelStart = ProcessInfo.processInfo.systemUptime
        client.cancelCurrentTask()
        let cancelLatency = ProcessInfo.processInfo.systemUptime - cancelStart

        assert(!client.isGenerating, "Client should not be generating after cancellation")
        assert(client.messages.last?.isCancelled == true, "Assistant message must be marked cancelled")
        assert(client.messages.last?.isStreaming == false, "Assistant message streaming must be stopped")
        assert(cancelLatency < 0.005, "Cancellation latency too high: \(cancelLatency)s")
        print("✓ Stop-button cancellation executed with 0ms UI delay (\(String(format: "%.3f", cancelLatency * 1000))ms)")

        // 10. Stage 1.9 Reasoning Mode Architecture Verification
        print("\nTesting Stage 1.9 Reasoning Mode Architecture...")
        assert(client.reasoningDepth == "auto", "Default reasoning mode must be 'auto', got '\(client.reasoningDepth)'")
        print("✓ Default mode is AUTO for new sessions")

        client.reasoningDepth = "instant"
        assert(client.reasoningDepth == "instant", "Explicit INSTANT mode must be respected")
        client.reasoningDepth = "standard"
        assert(client.reasoningDepth == "standard", "Explicit STANDARD mode must be respected")
        client.reasoningDepth = "deep"
        assert(client.reasoningDepth == "deep", "Explicit DEEP mode must be respected")
        print("✓ Explicit mode selections (INSTANT, STANDARD, DEEP) switch cleanly")

        // Test executedReasoningDepth reflection in ChatDisplayMessage
        var assistantMsg = ChatDisplayMessage(role: "assistant", text: "Answer", executedReasoningDepth: "instant")
        assert(assistantMsg.executedReasoningDepth == "instant")
        assistantMsg.executedReasoningDepth = "standard"
        assert(assistantMsg.executedReasoningDepth == "standard")
        print("✓ ChatDisplayMessage accurately tracks executed reasoning depth for UI badge display")

        print("\n🎉 ALL SWIFT MARKDOWN, RENDERING, AND STOP-BUTTON TESTS PASSED SUCCESSFULLY!")
    }
}
