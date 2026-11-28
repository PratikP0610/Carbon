# Stage 1.9: Reasoning Mode Optimization & Performance Audit Report

**Date:** 2026-10-29  
**Audit Type:** Implementation, Benchmark & Verification Audit  
**Host Hardware:** Apple MacBook Air M5, 16GB Unified Memory, macOS 27.0.1  
**Backend:** FastAPI `127.0.0.1:8765`, Ollama `127.0.0.1:11434` (`qwen3.5:9b Q4_K_M`)  
**Context Window:** 4096 tokens  
**Benchmark Scope:** 24 warm queries across 8 suites (AUTO, INSTANT, STANDARD, DEEP)  
**Machine-Readable Dataset:** [`docs/reports/stage1_9_reasoning_benchmark_results.json`](stage1_9_reasoning_benchmark_results.json)  

---

## 1. Executive Summary

Stage 1.9 completes the final Phase 1 engineering remediation for Carbon 1.0.0 prior to physical desktop acceptance testing.

In Stage 1.8, an empirical audit revealed that `ReasoningDepth.STANDARD` spent **85% to 99% of total response time** inside internal `<think>` tags on `qwen3.5:9b` (taking 90–198 seconds for ordinary queries). Furthermore, the native macOS desktop UI defaulted to `"standard"` in its picker and forcibly submitted `"reasoning": "standard"` on every message, overriding `IntelligenceEngine`'s deterministic classification and causing simple factual lookups to take 27.6 seconds instead of ~1 second.

In Stage 1.9:
1. **Four-Tier Reasoning Architecture Implemented:**
   - **`AUTO` (Default):** Submits no forced override (`reasoning_override=None`). Allows `IntelligenceEngine` to automatically classify the query to `INSTANT`, `STANDARD`, or `DEEP`.
   - **`INSTANT`:** Configured with `think=False`, `num_predict=512`, `temperature=0.2`. Instant responses for facts, arithmetic, greetings, and memory storage/recall.
   - **`STANDARD`:** Configured with `think=False` initially, preserving full output token capacity (`num_predict=2560`, `temperature=0.6`). Direct, well-structured answers without internal thinking token overhead.
   - **`DEEP`:** Preserves `think=True`, `num_predict=4096`, `temperature=0.7` for complex multi-step reasoning, mathematical proofs, and logic puzzles.
2. **Math Formatting Correction:** Added `("\\propto", "∝")` to `MathFormatter.symbolReplacements` in `MarkdownRenderer.swift` and verified with regression tests.
3. **Execution Mode Feedback in Native UI:** The desktop client now accurately displays the executed reasoning depth (`Instant`, `Standard`, `Deep`) via an execution badge and contextual progress indicators.
4. **Controlled Validation Across 24 Warm Queries:** Verified that simple factual queries drop from **27.55s to 0.50s (55x faster)**, short explanations drop from **198.28s to 6.10s (32x faster)**, structured explanations drop from **113.04s to 39.74s (2.8x faster)** with TTFT dropping from **97.2s to 0.18s (540x faster)**, and Java coding solutions execute completely and robustly without token exhaustion.

---

## 2. Reasoning Mode Architecture & Implementation

### 2.1 Backend Contract & Routing (`src/carbon/`)

- **Model Normalization ([`src/carbon/interfaces/models.py`](../../src/carbon/interfaces/models.py)):**  
  Added a Pydantic `field_validator` on `TaskRequest.reasoning_override` that normalizes `"auto"`, `"AUTO"`, `""`, and `None` into `None`. Case-insensitive string values (`"instant"`, `"standard"`, `"deep"`) normalize to their respective `ReasoningDepth` enum instances.
- **Orchestration Execution ([`src/carbon/core/orchestrator.py`](../../src/carbon/core/orchestrator.py)):**
  - When `task.reasoning_override is None` (`AUTO` mode), `task.reasoning_depth` is assigned directly from `intent.reasoning_depth` determined by `IntelligenceEngine`.
  - When an explicit user override is provided (`INSTANT`, `STANDARD`, `DEEP`), it is strictly respected.
  - **`STANDARD` Configuration:**
    ```python
    elif task.reasoning_depth == ReasoningDepth.STANDARD:
        think_flag = False
        options = {
            "num_predict": 2560,
            "temperature": 0.6,
        }
    ```
    System prompt directive: `"Provide a complete, well-structured, and clear answer.\n"`
  - **`DEEP` Configuration:**
    ```python
    elif task.reasoning_depth == ReasoningDepth.DEEP:
        think_flag = True
        options = {
            "num_predict": 4096,
            "temperature": 0.7,
        }
    ```
  - Emits `intent_classified` event with `category` and executed `reasoning_depth` over WebSocket.
- **WebSocket Protocol ([`src/carbon/main.py`](../../src/carbon/main.py)):**
  - The WebSocket endpoint passes payload `reasoning` through `TaskRequest`, sending `reasoning_mode` in `task_created` and notifying the UI immediately.

### 2.2 Native macOS Desktop Client (`apps/macos/Carbon/Sources/`)

- **Default Selection ([`CarbonClient.swift`](../../apps/macos/Carbon/Sources/CarbonClient.swift)):**  
  `@Published public var reasoningDepth: String = "auto"`. All new sessions default to `AUTO`.
- **Picker UI ([`ContentView.swift`](../../apps/macos/Carbon/Sources/ContentView.swift)):**  
  Segmented picker offers four choices: `Auto`, `Instant`, `Standard`, and `Deep` with a width of 280pt.
- **State Feedback ([`ContentView.swift`](../../apps/macos/Carbon/Sources/ContentView.swift)):**  
  Assistant messages capture `executedReasoningDepth` from `intent_classified` events. In `MessageBubbleView`, a small monospaced tag (`INSTANT`, `STANDARD`, or `DEEP`) is displayed on completed responses, and the progress text dynamically reflects the mode (`"Responding..."` for Instant, `"Reasoning (Deep)..."` for Deep).
- **Proportionality Formatting ([`MarkdownRenderer.swift`](../../apps/macos/Carbon/Sources/MarkdownRenderer.swift)):**  
  Added `("\\propto", "∝")` to `MathFormatter.symbolReplacements`. Expressions such as `f \propto \frac{1}{T}` format cleanly as `f ∝ (1 / T)`.

---

## 3. Empirical Benchmark Results (Stage 1.9)

A controlled benchmark was executed against the resident `qwen3.5:9b` model over the live loopback WebSocket (`ws://127.0.0.1:8765/ws/chat`), testing 8 test suites with 3 warm repetitions each ($N=24$ warm runs).

| Suite ID | Category & Mode | N | Total Duration (Median) | TTFT (Median) | Thinking Time (Median) | Visible Time (Median) | Visible Tokens (Median) | Gen Speed (TPS) | Outcome Quality |
|---|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|---|
| `A_auto` | **A. Simple factual (AUTO)** | 3 | **0.50s** | 0.13s | 0.0s | 0.37s | 9 | 24.3 tps | Correct (*"RAM stands for Random Access Memory."*) |
| `A_inst` | **A. Simple factual (INSTANT)** | 3 | **0.49s** | 0.13s | 0.0s | 0.37s | 9 | 24.5 tps | Correct (*"RAM stands for Random Access Memory."*) |
| `B_auto` | **B. Arithmetic (AUTO)** | 3 | **0.26s** | 0.16s | 0.0s | 0.10s | 1 | 9.7 tps | Correct (*"95"*) |
| `C_auto` | **C. Short explanation (AUTO $\to$ STD)** | 3 | **6.10s** | 0.20s | 0.0s | 5.90s | 182 | 30.8 tps | Complete ~100 word explanation; zero token exhaustion |
| `D_std` | **D. Structured explanation (STANDARD)** | 3 | **39.74s** | 0.18s | 0.0s | 39.56s | 849 | 21.5 tps | Full headings, numbered list, Python code block, math expression |
| `E_std` | **E. Java coding (STANDARD)** | 3 | **61.71s** | 0.24s | 0.0s | 61.23s | 1,010 | 16.5 tps | Full single-pass $O(n)$ code, edge cases, time/space analysis |
| `F_std` | **F. Analytical (STANDARD)** | 3 | **105.28s** | 0.24s | 0.0s | 105.08s | 1,269 | 12.1 tps | Full failure demonstration, array examples, invariants |
| `G_deep` | **G. Deep reasoning (DEEP, think=True)** | 3 | **180.00s** | 130.24s | 130.05s | 47.60s | 512 | 12.6 tps | Full balance scale strategy (3 vs 3, then 1 vs 1) |

---

## 4. Side-by-Side Comparison: Stage 1.8 (Before) vs. Stage 1.9 (After)

| Benchmark Category | Prompt | Stage 1.8 Metric (Forced Standard, think=True) | Stage 1.9 Metric (Optimized) | Performance Delta |
|---|---|---|---|---|
| **Simple Factual** | *"What does RAM stand for?"* | Median Total: **27.55s**<br>Median TTFT: **24.93s**<br>Thinking: 24.45s (438 tokens) | **AUTO Mode:**<br>Median Total: **0.50s**<br>Median TTFT: **0.13s**<br>Thinking: 0.0s (0 tokens) | **55x faster total**<br>**192x faster TTFT** |
| **Arithmetic** | *"What is 37 + 58? Just the number."* | N/A (tested factual) | **AUTO Mode:**<br>Median Total: **0.26s**<br>Median TTFT: **0.16s**<br>Thinking: 0.0s | **Near-instant (< 0.3s)** |
| **Short Explanation** | *"Explain CPU in ~100 words"* | Median Total: **198.28s**<br>Median TTFT: **198.27s**<br>VisTokens: 0 (Failed: token budget exhausted in thinking) | **AUTO $\to$ STANDARD:**<br>Median Total: **6.10s**<br>Median TTFT: **0.20s**<br>VisTokens: 182 (Complete answer) | **32x faster total**<br>**Eliminated token starvation** |
| **Structured Explanation** (Incident Prompt) | *"Explain CPU with heading, list, Python code, math"* | Median Total: **113.04s**<br>Median TTFT: **97.18s**<br>Thinking: 96.61s (1240 tokens)<br>Visible: 15.77s | **STANDARD Mode:**<br>Median Total: **39.74s**<br>Median TTFT: **0.18s**<br>Thinking: 0.0s (0 tokens)<br>Visible: 39.56s (849 tokens) | **2.8x faster total**<br>**540x faster TTFT**<br>**Immediate streaming feedback** |
| **Java Coding** | *"Write Java method to find second-largest distinct element"* | Median Total: **89.64s**<br>Median TTFT: **32.59s**<br>Thinking: 32.08s (Run 3 failed: token exhaustion) | **STANDARD Mode:**<br>Median Total: **61.71s**<br>Median TTFT: **0.24s**<br>Thinking: 0.0s<br>Visible: 61.23s (1,010 tokens) | **1.5x faster total**<br>**135x faster TTFT**<br>**100% completion rate (0 failures)** |
| **Analytical Query** | *"Why binary search requires sorted input"* | Median Total: **180.74s**<br>Median TTFT: **130.60s**<br>Thinking: 130.13s (1538 tokens) | **STANDARD Mode:**<br>Median Total: **105.28s**<br>Median TTFT: **0.24s**<br>Thinking: 0.0s<br>Visible: 105.08s (1,269 tokens) | **1.7x faster total**<br>**544x faster TTFT**<br>**Full detailed breakdown** |
| **Deep Reasoning** | *"8 billiard balls balance scale"* | N/A (Stage 1.4 baseline: ~165s) | **DEEP Mode (think=True):**<br>Median Total: **180.00s**<br>Median TTFT: **130.24s**<br>Thinking: 130.05s (1,511 tokens) | **Preserved full deep reasoning capability** |

---

## 5. Answer Quality & Completeness Validation

1. **Java Coding Completeness:**
   - Evaluated the generated Java solution across all 3 repetitions.
   - Every run generated a complete class (`SecondLargestFinder`) with a robust single-pass method tracking `max1` and `max2`.
   - Edge cases handled cleanly: arrays with $<2$ elements throw `IllegalArgumentException`, duplicates are ignored, negative integers are handled.
   - Comprehensive complexity analysis included: $O(n)$ time complexity, $O(1)$ auxiliary space complexity, and comparisons against sorting ($O(n \log n)$) and `HashSet`.
   - Length: 3,506 to 3,838 characters; zero truncated code or syntax errors.
2. **Structured Explanation Formatting:**
   - Contains Markdown `# The Central Processing Unit` heading.
   - Contains numbered lists of CPU stages (Fetch, Decode, Execute, Writeback).
   - Contains Python code snippet demonstrating a toy cycle.
   - Contains mathematical expressions with clock rate and cycle time, utilizing `\propto` rendered as `∝`.
3. **Analytical Completeness:**
   - Generates complete walkthroughs of binary search failure modes on unsorted arrays, citing search intervals $[L, R]$, midpoints, and incorrect pruning.

---

## 6. Comprehensive Regression Testing

All regression test suites were run and passed without regressions:

1. **Python Pytest Suite:**
   - Command: `./.venv/bin/pytest -v`
   - Result: **77 passed, 0 failed, 1 warning (deprecation) in 5.36s**
2. **Ruff Linter:**
   - Command: `./.venv/bin/ruff check src tests scripts`
   - Result: **All checks passed! (0 errors across 40 files)**
3. **Swift Unit & Regression Suite:**
   - Command: `swiftc -parse-as-library apps/macos/Carbon/Sources/MarkdownRenderer.swift apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Tests/test_markdown.swift -o /tmp/test_markdown && /tmp/test_markdown`
   - Result: **All checks passed!**
     - Exponents, subscripts, math operators, proportionality (`∝`), Greek letters.
     - Markdown parser, code blocks, tables, ordered and bullet lists.
     - Malformed markdown & infinite-loop safety tests (9,000 stress iterations in 0.062s).
     - Rapid streaming token updates (60 bursts without main thread blocking).
     - Stop-button cancellation latency: **0.049 ms**.
     - Reasoning mode state reflection & `ChatDisplayMessage.executedReasoningDepth`.
4. **Clean macOS Build:**
   - Command: `cd apps/macos/Carbon && swift package clean && swift build`
   - Result: **Build complete! (28.61 secs, exit code 0)**
5. **Live Memory Persistence & Recall:**
   - Command: `PYTHONPATH=src ./.venv/bin/python scripts/verify_memory_persistence.py`
   - Result: **All checks passed (3.6s)** — Store Java preference $\to$ query Java $\to$ store SwiftUI preference $\to$ query SwiftUI $\to$ query unknown car $\to$ correct negative response.
6. **API Security & Loopback Binding:**
   - Verified strict loopback binding `127.0.0.1:8765`, loopback-only CORS, handshake header auth, in-socket auth fallback.

