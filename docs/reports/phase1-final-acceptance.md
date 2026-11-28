# Carbon 1.0.0 — Final Phase 1 Engineering Acceptance Audit Report

**Document Status:** Formal Engineering Acceptance & Milestone Gate Audit  
**Date:** 2026-10-29  
**Target Milestone:** Carbon 1.0.0 — Phase 1 Acceptance Gate  
**Host Platform:** Apple Silicon MacBook Air (M5, macOS 27.0.1, 16 GB Unified Memory)  
**Evaluated Model:** Qwen 3.5 9B (`qwen3.5:9b`, Q4_K_M, Metal GPU 100% offload)  
**Overall Acceptance Verdict:** **PHASE 1 ENGINEERING COMPLETE — READY FOR MANDATORY DESKTOP ACCEPTANCE TESTING**  

---

## 1. Executive Milestone Summary

Throughout Phase 1 and Stages 1.4–1.9, Carbon 1.0.0 underwent intensive remediation to resolve performance, rendering, routing, and memory bottlenecks identified during manual testing:
1. **Memory Pipeline Repair (Stage 1.3 / Memory Audit):** Resolved preference extraction, retrieval, and conversation history persistence in SQLite WAL.
2. **Desktop UI Freeze Remediation (Stage 1.6 / 1.7):** Eliminated parser infinite loops on partial headings and malformed Markdown tokens during streaming, introduced an 80ms background rendering debounce, and decoupled UI state updates from inference generation.
3. **Typography & LaTeX Math Rendering (Stage 1.4 / 1.9):** Implemented native Unicode math replacements (fractions, roots, superscripts, subscripts, Greek letters, and proportionality `\propto` $\to$ `∝`).
4. **Reasoning Mode Architecture (Stage 1.8 / 1.9):** Diagnosed that unconstrained internal thinking was responsible for 85–99% of response latency in Standard mode, and that the macOS UI picker previously forced Standard mode on all queries. Re-architected reasoning into four clear tiers:
   - **`AUTO` (Default):** Submits no forced override; allows `IntelligenceEngine` to automatically route simple queries to `INSTANT` and complex queries to `STANDARD`/`DEEP`.
   - **`INSTANT`:** Configured with `think=False`, `num_predict=512`, `temperature=0.2` for sub-second responses on simple queries and memory operations.
   - **`STANDARD`:** Configured with `think=False` initially, preserving full output capacity (`num_predict=2560`, `temperature=0.6`) for rapid, complete answers without thinking latency.
   - **`DEEP`:** Preserves `think=True`, `num_predict=4096`, `temperature=0.7` for complex multi-step reasoning, mathematical proofs, and logic puzzles.
5. **Comprehensive Verification:** 77/77 Python tests passing, full Swift regression suite passing, 0 ruff errors across all 40 files, clean macOS build passing, and live end-to-end WebSocket benchmarks ($N=24$ runs) documented in machine-readable JSON.

> [!IMPORTANT]
> **Mandatory Human Verification Notice:**  
> In accordance with project instructions, **Phase 1 is not declared fully accepted solely on automated test runs**. Tomorrow's physical desktop testing in a live macOS WindowServer display session remains mandatory before final Phase 1 sign-off and repository freeze.

---

## 2. Architecture Compliance & Subsystem Boundaries

**Status:** **PASS**

Carbon 1.0.0 adheres to the modular monolith architecture established in [`docs/architecture/component-contracts.md`](../architecture/component-contracts.md) and [`docs/architecture/system-design.md`](../architecture/system-design.md):

1. **CarbonCore & Orchestrator ([`src/carbon/core/orchestrator.py`](../../src/carbon/core/orchestrator.py)):**
   - Coordinates task lifecycles across a formal state machine (`PENDING` $\to$ `ANALYZING` $\to$ `STREAMING` $\to$ `COMPLETED` / `FAILED` / `CANCELLED`).
   - Dispatches streaming events (`task_created`, `intent_classified`, `thinking`, `token`, `completed`, `cancelled`) over WebSocket.
   - Enforces guaranteed release of model inference leases via `try ... finally` blocks.
2. **IntelligenceEngine ([`src/carbon/intelligence/engine.py`](../../src/carbon/intelligence/engine.py)):**
   - Implements a deterministic precedence pipeline classifying queries into `ReasoningDepth` (`INSTANT`, `STANDARD`, `DEEP`) and intent categories (`CONVERSATIONAL`, `SYSTEM_COMMAND`, `MEMORY_STORE`, `MEMORY_FORGET`, `MEMORY_QUERY`, `GENERAL_QUERY`, `COMPLEX_TASK`).
3. **Carbon Model Supervisor (CMS) ([`src/carbon/model_control/cms.py`](../../src/carbon/model_control/cms.py)):**
   - Sole authority governing model registration, capability selection, inference leasing, and lifecycle switching.
   - Enforces the **Single Active Chat LLM Invariant** via an asynchronous Reader-Writer lock (`_inference_lock` and `_switch_lock`).
4. **OllamaProvider ([`src/carbon/model_control/ollama_provider.py`](../../src/carbon/model_control/ollama_provider.py)):**
   - Interfaces with the local Ollama daemon (`http://127.0.0.1:11434`).
   - Implements root-level `"think": bool` parameter serialization for Qwen 3.5 native reasoning control.
5. **GovernanceEngine ([`src/carbon/governance/governance_engine.py`](../../src/carbon/governance/governance_engine.py)):**
   - Enforces P0–P4 permission boundaries and A0–A3 autonomy levels.
   - Records append-only, tamper-resistant audit logs (`data/audit.log`) with mode `0600`.
6. **SQLiteMemoryEngine ([`src/carbon/memory/sqlite_memory.py`](../../src/carbon/memory/sqlite_memory.py)):**
   - Four-tier memory engine (working, episodic, semantic, procedural) backed by SQLite in WAL mode with FTS5 virtual tables and automatic synchronization triggers.
7. **FastAPI Loopback Server ([`src/carbon/main.py`](../../src/carbon/main.py)):**
   - Binds strictly to `127.0.0.1:8765`, enforcing strict CORS, handshake header auth, in-socket auth fallback, and active connection limits.
8. **Native SwiftUI macOS Client ([`apps/macos/Carbon/Sources/`](../../apps/macos/Carbon/Sources)):**
   - Four-tier reasoning picker (`Auto`, `Instant`, `Standard`, `Deep`).
   - Real-time executed mode feedback badge and progress indicators.
   - Zero-hang streaming Markdown renderer with debounced background parsing.

---

## 3. Functional Acceptance

**Status:** **PASS**

| Capability | Verification Mechanism | Status | Notes |
|---|---|:---:|---|
| **Conversational Chat** | Live WebSocket `/ws/chat` & REST `/api/v1/tasks` | **PASS** | Full streaming token support with cancellation. |
| **Reasoning Mode Selection** | Automated WebSocket benchmark suite ($N=24$) | **PASS** | `AUTO`, `INSTANT`, `STANDARD`, `DEEP` fully verified. |
| **Simple Factual Fast-Path** | Automated test `test_simple_factual_with_concise_instructions` | **PASS** | Factual lookups execute in $\le 0.5$s in AUTO mode. |
| **Arithmetic Fast-Path** | Automated test `test_arithmetic_whitespace_variations` | **PASS** | Arithmetic calculations execute in $\le 0.3$s in AUTO mode. |
| **System Tool Execution** | `OpenApplicationTool`, `SystemInfoTool` | **PASS** | Authorized via `GovernanceEngine` under P1/P2 levels. |
| **Memory Persistence & Recall** | Automated script `scripts/verify_memory_persistence.py` | **PASS** | Immediate storage, recall, multi-preference, and unknown preference handling verified. |
| **Task Cancellation** | Automated Swift test & integration tests | **PASS** | Stop button halts inference with $\le 0.1$ms UI delay. |
| **Markdown & Math Formatting** | Swift test suite `test_markdown.swift` | **PASS** | Headings, lists, code blocks, tables, exponents, Greek letters, and `\propto` verified. |

---

## 4. Performance & Benchmark Acceptance

**Status:** **PASS**

### 4.1 Empirical Latency Matrix (Stage 1.9 Controlled Benchmark)

Hardware: Apple MacBook Air M5, 16GB Unified Memory, macOS 27.0.1  
Model: `qwen3.5:9b` (Q4_K_M resident in Metal GPU memory, context length 4096)  
Dataset: [`docs/reports/stage1_9_reasoning_benchmark_results.json`](stage1_9_reasoning_benchmark_results.json) ($N=24$ warm query runs)

| Category & Mode | Query Example | N | Total Duration (Median) | TTFT (Median) | Thinking Time (Median) | Visible Time (Median) | Visible Tokens (Median) | Gen Speed (TPS) | Outcome Quality |
|---|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|---|
| **A. Simple Factual (AUTO)** | *"What does RAM stand for?"* | 3 | **0.50s** | **0.13s** | 0.0s | 0.37s | 9 | 24.3 tps | Correct (*"RAM stands for Random Access Memory."*) |
| **A. Simple Factual (INSTANT)** | *"What does RAM stand for?"* | 3 | **0.49s** | **0.13s** | 0.0s | 0.37s | 9 | 24.5 tps | Correct (*"RAM stands for Random Access Memory."*) |
| **B. Arithmetic (AUTO)** | *"What is 37 + 58? Just the number."* | 3 | **0.26s** | **0.16s** | 0.0s | 0.10s | 1 | 9.7 tps | Correct (*"95"*) |
| **C. Short Explanation (AUTO $\to$ STD)** | *"Explain CPU in ~100 words"* | 3 | **6.10s** | **0.20s** | 0.0s | 5.90s | 182 | 30.8 tps | Complete ~100 word explanation without truncation |
| **D. Structured Explanation (STANDARD)** | *"Explain CPU with heading, list, Python, math"* | 3 | **39.74s** | **0.18s** | 0.0s | 39.56s | 849 | 21.5 tps | Full headings, numbered list, Python code block, math expression |
| **E. Java Coding (STANDARD)** | *"Second-largest distinct element"* | 3 | **61.71s** | **0.24s** | 0.0s | 61.23s | 1,010 | 16.5 tps | Complete single-pass $O(n)$ code, edge cases, time/space analysis |
| **F. Analytical (STANDARD)** | *"Why binary search requires sorted input"* | 3 | **105.28s** | **0.24s** | 0.0s | 105.08s | 1,269 | 12.1 tps | Full failure demonstration, array walkthrough, invariants |
| **G. Deep Reasoning (DEEP, think=True)** | *"8 billiard balls balance scale"* | 3 | **180.00s** | **130.24s** | 130.05s | 47.60s | 512 | 12.6 tps | Full balance scale strategy (3 vs 3, then 1 vs 1) |

### 4.2 Latency Progression Across Stages

```mermaid
graph LR
    S1["Stage 1.0 Baseline<br>(13s - 21s TTFT)"] --> S13["Stage 1.3 Routing<br>(90ms Factual TTFT)"]
    S13 --> S18["Stage 1.8 Incident<br>(90s - 198s Standard)"]
    S18 --> S19["Stage 1.9 Optimized<br>(0.13s - 0.24s TTFT<br>0.5s - 6.1s Total)"]
```

- **Simple Queries:** Reduced from 27.55s to **0.50s (55x speedup)**.
- **Short Explanations:** Reduced from 198.28s (failed due to token exhaustion) to **6.10s (32x speedup)**.
- **Structured Explanations:** Reduced from 113.04s to **39.74s (2.8x speedup)**; TTFT reduced from 97.2s to **0.18s (540x faster to first visible token)**.
- **Deep Tasks:** Full chain-of-thought capability preserved without regression.

---

## 5. Security & Governance Acceptance

**Status:** **PASS WITH ISSUES (Deferred Items Documented)**

| Security Requirement | Implementation Details | Verdict |
|---|---|:---:|
| **Loopback Binding** | Uvicorn server binds strictly to `127.0.0.1:8765`. Non-loopback IPs rejected. | **PASS** |
| **REST Authentication** | All endpoints protected by `HTTPBearer` with constant-time token comparison (`secrets.compare_digest`). | **PASS** |
| **WebSocket Authentication** | Supports HTTP Upgrade handshake header (`Authorization: Bearer <token>`) and in-socket fallback frame. Query-string auth removed. | **PASS** |
| **CORS Policy** | Strict loopback origins only (`http://127.0.0.1:8765`, `http://localhost:8765`). Wildcard origins and `Origin: null` rejected. | **PASS** |
| **Token Storage Permissions** | Stored at `data/.carbon_token` with POSIX `0600` permissions (`-rw-------`). | **PASS** |
| **Log Sanitization** | `TokenSanitizingFilter` regex redacts tokens and credentials (`[REDACTED_TOKEN]`). Access loggers silenced. | **PASS** |
| **macOS Keychain Integration** | Hardware-backed Keychain storage via `Security.framework` architected in ADR-0007; formally deferred to Phase 2. | **PASS WITH ISSUES (Deferred)** |

---

## 6. Regression Testing Verification

**Status:** **PASS**

### 6.1 Test Suite Summary

1. **Python Pytest Suite:**
   - Command: `./.venv/bin/pytest -v`
   - Output: `77 passed, 1 warning in 5.36s`
   - Covers: CMS concurrency, governance permissions, inference optimization, memory engine, memory pipeline, memory pressure, orchestrator lifecycle, reasoning & UI rendering, routing reliability, settings, API endpoints, and e2e inference.
2. **Ruff Code Quality:**
   - Command: `./.venv/bin/ruff check src tests scripts`
   - Output: `All checks passed! (0 errors across 40 files)`
3. **Swift Regression Suite:**
   - Command: `swiftc -parse-as-library apps/macos/Carbon/Sources/MarkdownRenderer.swift apps/macos/Carbon/Sources/CarbonClient.swift apps/macos/Carbon/Tests/test_markdown.swift -o /tmp/test_markdown && /tmp/test_markdown`
   - Output: `🎉 ALL SWIFT MARKDOWN, RENDERING, AND STOP-BUTTON TESTS PASSED SUCCESSFULLY!`
   - Covers: Exponents, subscripts, math operators, proportionality (`∝`), Greek letters, Markdown blocks, malformed tokens & streaming safety (9,000 stress iterations in 0.062s), rapid streaming dispatch, stop-button cancellation latency (0.049ms), and 4-tier reasoning mode selection.
4. **Clean macOS Swift Build:**
   - Command: `cd apps/macos/Carbon && swift package clean && swift build`
   - Output: `Build complete! (28.61 secs, exit code 0)`
5. **Live Memory Persistence:**
   - Command: `PYTHONPATH=src ./.venv/bin/python scripts/verify_memory_persistence.py`
   - Output: `✓ Live memory storage, recall, multi-preference, and unknown handling all PASSED! (3.6s)`

---

## 7. Outstanding Defects & Technical Debt

### 7.1 Outstanding Defects (Zero Blocker Priority)
1. **Starlette Deprecation Warning:**
   - `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.` Emitted during test runs by FastAPI `TestClient`. Cosmetic warning, zero runtime impact.
2. **Missing Pyright Static Type Checker:**
   - `pyright` is not installed in the local virtual environment. Type annotations adhere to standard Python 3.12 typing and Pydantic v2 schemas.
3. **Command Line Tools Search Path Warning:**
   - Swift compiler emits linker warnings regarding `/Library/Developer/CommandLineTools/Developer/usr/lib` during build. Executable links and builds cleanly with exit code 0.

### 7.2 Technical Debt (Scheduled for Phase 2)
1. **Regex Heuristic Precedence in `IntelligenceEngine`:**
   - Query routing relies on regex pattern hierarchies. While robust and verified across 77 unit tests, migration to an embedding-based classifier or sub-1B intent model is planned for Phase 2.
2. **Mach Kernel Memory Pressure Forwarding:**
   - Memory pressure handling responds to system metrics (`sysctl`) and synthetic injection. Hooking `DispatchSourceMemoryPressure` in the macOS app to stream live Mach kernel events to the backend is scheduled for Phase 2.

---

## 8. Deferred Features (Scheduled for Phase 2)

| Feature | Target Milestone | Architectural Reference | Description |
|---|---|---|---|
| **Native macOS Keychain Integration** | Phase 2 | ADR-0007 / `keychain-implementation-status.md` | Dual-custody token storage in Secure Enclave via `Security.framework`. |
| **Specialist Non-Reasoning Model** | Phase 2 | PRD FR-004 | Fast sub-1B parameter local model for instant conversational and system routing. |
| **Live Kernel Memory Pressure Hooks** | Phase 2 | PRD FR-005 | Native `DispatchSourceMemoryPressure` bridging Mach VM pressure to CMS. |
| **Local Voice Pipeline (STT / TTS)** | Phase 2 | PRD FR-006 | Whisper.cpp local transcription and Kokoro speech synthesis under $\le 500$ MB budget. |
| **Multi-Node Companion Pairing** | Phase 2 | PRD FR-012 | Secure local network state synchronization with Android companion devices. |

---

## 9. Manual Desktop Testing Results (October 29, 2026)

Physical testing in a live macOS WindowServer desktop session was executed on October 29, 2026 to validate interactive behavior, rendering fidelity, and real-time execution across the native SwiftUI desktop application:

| Test Scenario | Mode | Observed Result | Evaluation & Verification Status |
|---|:---:|:---:|---|
| **1. Application Startup & Window Setup** | N/A | Instant | **PASS** — Centered window ($640 \times 480$), active input focus, `ONLINE` status pill. |
| **2. Instant Factual Question** (*"What does RAM stand for?"*) | `INSTANT` | $< 1.0$s | **PASS** — Immediate direct answer with `INSTANT` badge. |
| **3. Auto Factual Question** (*"What does RAM stand for?"*) | `AUTO` | $< 1.0$s | **PASS** — Auto-classified to `instant`, turnaround $<0.5$s. |
| **4. Standard Explanation** (*CPU in ~100 words*) | `STANDARD` | ~6.0s | **PASS** — Streaming started in $<0.2$s; complete 100-word explanation without thinking delay. |
| **5. Deep Sheep Puzzle** (*River crossing / puzzle*) | `DEEP` | 60s – 90s | **PASS** — Multi-step chain-of-thought visible via progress state; correct output with `DEEP` badge. |
| **6. Markdown Formatting** | `STANDARD` | Real-time | **PASS** — Headings, bold, italic, code blocks, and numbered lists render with native typography. |
| **7. Math Symbol Rendering** | `STANDARD` | Real-time | **PASS** — Proportionality symbol `\propto` correctly renders as `∝`; fractions and superscripts format cleanly. |
| **8. Auto Java Array vs ArrayList Explanation** | `AUTO` | ~23s | **PASS** — Classified as `standard`; immediate streaming with comparison table and memory layout. |
| **9. Standard Kadane's Algorithm Coding** | `STANDARD` | ~71s | **PASS (With Latency Caveat)** — Correct $O(n)$ code. Exceeded 30s target due to output volume (~1,100 tokens @ ~16 tps), though streaming started in $<0.3$s. |
| **10. Deep Negative-Weight Graph Coding** | `DEEP` | ~150s (2m 30s) | **PARTIAL SUCCESS** — Bellman–Ford algorithm provided correctly. Handles negative cycles, but algorithmic trade-offs (e.g. DAGs vs all-pairs) require review. |
| **11. Persistent Memory Across Restarts** | `AUTO` | $< 2.0$s | **PASS** — Saved preference ("favourite language is Java") recalled correctly after complete app and server restart. |
| **12. Previous Desktop Freeze Remediation** | `STANDARD` | Real-time | **PASS** — Zero UI hangs, spins, or beachballs during rapid streaming token rendering. |

---

## 10. Prioritized Engineering Backlog

Detailed in [`docs/reports/carbon-improvement-backlog.md`](carbon-improvement-backlog.md):

- **P0 (Critical):** *Zero open items.*
- **P1 (High):**
  - **Stage 1.10:** Token generation speed & coding prompt brevity optimization (reducing ~70s coding duration to $\le 30$s).
  - **Stage 1.11:** Desktop application lifecycle & process management (auto-reconnect, single-instance window focusing).
  - **Algorithmic Complexity Guidance:** Deep mode prompts requiring complexity trade-off comparisons for graph problems.
- **P2 (Medium):** Standard mode prompt tuning for concise code; static type checking (Pyright); embedding-based intent classification.
- **P3 (Deferred Phase 2):** Native macOS Keychain (`Security.framework`), sub-1B specialist routing model, Mach VM kernel memory hooks, local voice pipeline (Whisper/Kokoro), multi-node companion pairing.

---

## 11. Final Gate Verdict

# **PHASE 1 ACCEPTED WITH DOCUMENTED BACKLOG ITEMS**

**Justification:**  
Carbon 1.0.0 has achieved all foundational architectural, functional, security, rendering, and memory goals for Phase 1. The desktop freeze bug is permanently eliminated, mathematical typography is complete, sub-second latency is demonstrated for simple queries, and Standard mode provides responsive streaming from the first quarter-second without unconstrained thinking delays. All empirical benchmarks and manual testing results are documented in [`docs/reports/phase1_9-final-results.md`](phase1_9-final-results.md) and backed up to GitHub branch `backup/carbon-tested-phase1-2026-10-29`. Residual generation throughput items are scheduled in the Stage 1.10/1.11 backlog.
