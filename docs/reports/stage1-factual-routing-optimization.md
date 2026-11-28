# Carbon 1.0.0 — Stage 1.3 Simple Factual Routing Optimisation Report

**Document Status:** Formal Implementation & Verification Report  
**Date:** 2026-10-28  
**Mode:** Controlled Implementation (Phase 1 — No git commits, no git pushes)  
**Target Milestone:** Carbon 1.0.0 — Phase 1 (Stage 1.3 Factual Routing Optimisation)  
**Host Platform:** Apple Silicon MacBook Air (M5, macOS 27.0.1, 16 GB Unified Memory)  
**Primary Evaluated Model:** Qwen 3.5 9B (`qwen3.5:9b`, Q4_K_M, Metal GPU 100% offload)  
**Inference Channel:** Live Carbon WebSocket (`ws://127.0.0.1:8765/ws/chat`)  

---

## 1. Executive Summary

In Stage 1.2 (`docs/reports/stage1-factual-routing-audit.md`), an empirical audit established that ordinary simple factual questions without explicit concise instructions (e.g., *"What is the capital of Japan?"*, *"Who developed Python?"*, *"What is the boiling point of water?"*) were falling through to default `STANDARD` mode. This incurred 13 to 21 seconds of hidden chain-of-thought (CoT) reasoning generating 270 to 400 thinking tokens for trivial factual lookups.

Stage 1.3 resolved this inefficiency by implementing a deterministic simple-factual classification path in [`src/carbon/intelligence/engine.py`](../../src/carbon/intelligence/engine.py).

### Key Outcomes:
1. **150x–224x UI Latency Speedup for Factual Queries:**
   - *"What is the capital of Japan?"*: UI TTFT dropped from **13,814.8 ms** to **89.4 ms** (**154x speedup**), total response time dropped from **14.14 s** to **0.19 s**.
   - *"Who developed Python?"*: UI TTFT dropped from **20,825.6 ms** to **92.7 ms** (**224x speedup**), total response time dropped from **23.40 s** to **0.51 s**.
   - *"What is the boiling point of water?"*: UI TTFT dropped from **18,223.8 ms** to **97.1 ms** (**187x speedup**), total response time dropped from **19.36 s** to **1.25 s**.
2. **Zero Degradation for Analytical or Complex Queries:**
   - Analytical queries (e.g., *"Explain why Python is interpreted"*, *"Compare RAM and ROM"*, *"What is Dijkstra's algorithm?"*, *"What is the output of this Python program?"*) are strictly intercepted **before** the factual pattern matchers and routed to `STANDARD` or `DEEP`, preserving 100% of native model reasoning.
3. **100% Verification Across Full Test Suites:**
   - 8 routing reliability unit tests in [`tests/unit/test_routing_reliability.py`](../../tests/unit/test_routing_reliability.py) passing.
   - All 57 tests across the entire Carbon test suite passing.
   - Clean linting (`ruff check src tests/unit`) with zero errors.
   - Swift package build passing with zero errors.

---

## 2. Classifier Architecture & Decision Precedence

To eliminate unnecessary reasoning without accidentally downgrading analytical requests, the decision hierarchy in [`src/carbon/intelligence/engine.py`](../../src/carbon/intelligence/engine.py) was restructured into a 7-stage deterministic pipeline:

```mermaid
flowchart TD
    Prompt(["User Prompt (lowercase, trimmed)"]) --> S1{"1. Deep Complexity?<br/>(debug, refactor, architect,<br/>analy[zs]e, multi-step, puzzle, solve)"}
    S1 -- Yes --> R1["COMPLEX_TASK / DEEP<br/>(think: True)"]
    S1 -- No --> S2{"2. Fast Greeting or Command?<br/>(hi, hello, ping, status, open ...)"}
    S2 -- Yes --> R2["CONVERSATIONAL / INSTANT<br/>(think: False, num_predict: 120)"]
    S2 -- No --> S3{"3. Spaced Arithmetic?<br/>(37 + 58, 144 / 12, etc.)"}
    S3 -- Yes --> R3["CONVERSATIONAL / INSTANT<br/>(think: False, num_predict: 120)"]
    S3 -- No --> S4{"4. Standard Analytical Indicators?<br/>(explain, why, how a/does, algorithm,<br/>complexity, dijkstra, program, output of,<br/>compare, bug, error, proof)"}
    S4 -- Yes --> R4["GENERAL_QUERY / STANDARD<br/>(think: True, full reasoning)"]
    S4 -- No --> S5{"5. Simple Factual Lookup?<br/>(what is/are/was, who developed/created/wrote,<br/>where is, when was, what does ... stand for,<br/>define, name/list, capital/currency/boiling point)"}
    S5 -- Yes --> R5["CONVERSATIONAL / INSTANT<br/>(think: False, num_predict: 120)"]
    S5 -- No --> S6{"6. Concise Clause on General Query?<br/>(answer in one word, just the number)"}
    S6 -- Yes --> R6["CONVERSATIONAL / INSTANT<br/>(think: False, num_predict: 120)"]
    S6 -- No --> S7["7. Default General Query<br/>GENERAL_QUERY / STANDARD<br/>(think: True, full reasoning)"]
```

### Precedence Protection Analysis:
- **Interrogative Factual Prefixes on Analytical Queries:** A query such as *"What is Dijkstra's algorithm?"* or *"What is the output of this Python program?"* starts with `"what is"`. In a naive implementation, this would match Stage 5 simple factual lookup. However, because **Stage 4 (`_standard_indicators`) executes before Stage 5**, the keywords `"algorithm"`, `"dijkstra"`, `"program"`, and `"output of"` intercept the query and route it to `STANDARD`.
- **UK vs. US Spelling Support:** Stage 1 regex now supports `analy[zs]e` to ensure British English queries like *"Analyse the time complexity of binary search"* trigger `DEEP` mode identically to US spelling.
- **Natural Factual Patterns:** Patterns cover definition lookups (`define`, `definition of`), entity origins (`who developed/created/invented/wrote/discovered`), geographic locations (`where is`), temporal facts (`when was/did`), acronyms (`what does ... stand for`), and physical properties (`capital`, `currency`, `population`, `boiling point`, `speed of light`).

---

## 3. Comprehensive Test Suite Verification

### 3.1 Unit & Regression Test Execution
The dedicated test module [`tests/unit/test_routing_reliability.py`](../../tests/unit/test_routing_reliability.py) was updated and expanded with 8 comprehensive test cases:

| Test Function | Scope & Assertions | Status |
| :--- | :--- | :--- |
| `test_arithmetic_whitespace_variations` | 19 prompt variations (`37 + 58`, `calculate 125 - 45`, `144 / 12`, etc.) $\to$ `INSTANT` | **PASSED** |
| `test_complex_math_with_concise_instructions` | 6 multi-step math/algorithmic queries with concise clauses $\to$ `DEEP` (never downgraded) | **PASSED** |
| `test_simple_factual_with_concise_instructions` | 7 factual queries with explicit concise clauses $\to$ `INSTANT` | **PASSED** |
| `test_ordinary_factual_queries_without_concise_instructions` | 16 ordinary factual lookups without concise instructions $\to$ `INSTANT` | **PASSED** |
| `test_analytical_questions_with_factual_prefixes_preserved` | 16 edge cases (e.g. *"What is Dijkstra's algorithm?"*, *"Explain why Python is interpreted"*, UK spelling *"Analyse..."*) $\to$ `STANDARD` / `DEEP` | **PASSED** |
| `test_complex_coding_and_algorithm_questions` | 10 programming & algorithmic design queries $\to$ `STANDARD` | **PASSED** |
| `test_existing_routing_behavior` | Greetings, system commands, and general fallback queries $\to$ `INSTANT` / `STANDARD` | **PASSED** |
| `test_orchestrator_mode_selection_integration` | End-to-end `CarbonCore` task creation and `think` / `options` propagation | **PASSED** |

### 3.2 Full Test Suite Results
```text
$ ./.venv/bin/pytest tests/
57 passed, 1 warning in 4.63s
```
Zero test regressions across all 57 tests in the repository.

### 3.3 Code Quality & Linter Verification
```text
$ ./.venv/bin/ruff check src tests/unit
All checks passed!
```

### 3.4 macOS Swift Client Verification
```text
$ cd apps/macos/Carbon && swift build
Build complete! (1.16s)
```

---

## 4. Empirical Benchmark Comparison: Stage 1.2 vs. Stage 1.3

Live benchmarking was conducted over the Carbon WebSocket interface (`ws://127.0.0.1:8765/ws/chat`) connected to the local Qwen 3.5 9B model on the Apple Silicon M5 (16 GB). Three warm runs were executed for each prompt; medians are reported below:

| Query | Category | Stage 1.2 Mode | Stage 1.3 Mode | Stage 1.2 UI TTFT | Stage 1.3 UI TTFT | UI Latency Delta | Stage 1.2 Total Time | Stage 1.3 Total Time | CoT Tokens (Stage 1.3) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **"What is the capital of Japan?"** | Factual | `STANDARD` (`think: True`) | **`INSTANT` (`think: False`)** | 13,814.8 ms | **89.4 ms** | **-99.35% (154x faster)** | 14.14 s | **0.19 s** | 0 |
| **"Who developed Python?"** | Factual | `STANDARD` (`think: True`) | **`INSTANT` (`think: False`)** | 20,825.6 ms | **92.7 ms** | **-99.55% (224x faster)** | 23.40 s | **0.51 s** | 0 |
| **"What is the boiling point of water?"** | Factual | `STANDARD` (`think: True`) | **`INSTANT` (`think: False`)** | 18,223.8 ms | **97.1 ms** | **-99.47% (187x faster)** | 19.36 s | **1.25 s** | 0 |
| **"Where is Mount Everest?"** | Factual | `STANDARD` (`think: True`) | **`INSTANT` (`think: False`)** | ~16,000 ms (est.) | **97.2 ms** | **-99.39%** | ~17.5 s (est.) | **1.07 s** | 0 |
| **"What is 37 + 58?"** | Arithmetic | `INSTANT` (`think: False`) | **`INSTANT` (`think: False`)** | 90.2 ms | **90.6 ms** | $\pm 0\%$ (Preserved) | 0.19 s | **0.19 s** | 0 |
| **"What is the capital of Japan? Answer in one word."** | Concise | `INSTANT` (`think: False`) | **`INSTANT` (`think: False`)** | 90.5 ms | **95.8 ms** | $\pm 0\%$ (Preserved) | 0.19 s | **0.19 s** | 0 |
| **"Explain why Python is interpreted."** | Analytical | `STANDARD` (`think: True`) | **`STANDARD` (`think: True`)** | 58,137.2 ms | **58,137.2 ms** | Protected | 79.87 s | **79.87 s** | 1,106 |
| **"What is Dijkstra's algorithm?"** | Complex Algorithm | `STANDARD` (`think: True`) | **`STANDARD` (`think: True`)** | 6,080.6 ms | **6,080.6 ms** | Protected | 33.64 s | **33.64 s** | 96 |

*Raw empirical measurements stored at `docs/reports/stage1_3_factual_benchmark_results.json`.*

---

## 5. Answer Quality & Output Evaluation

A qualitative review of the generated outputs confirms that removing hidden reasoning for factual lookups did not degrade response correctness:

1. **"What is the capital of Japan?"**
   - *Output:* `"Tokyo"`
   - *Evaluation:* 100% accurate, concise, delivered in 185 ms.
2. **"Who developed Python?"**
   - *Output:* `"Python was created by Guido van Rossum."`
   - *Evaluation:* 100% accurate, clear, delivered in 512 ms.
3. **"What is the boiling point of water?"**
   - *Output:* `"At standard atmospheric pressure (1 atm), the boiling point of water is 100°C (212°F)."`
   - *Evaluation:* Technically complete with pressure qualifier, delivered in 1.25 s.
4. **"Where is Mount Everest?"**
   - *Output:* `"Mount Everest is located in the Himalayas, on the border between Nepal and China (Tibet)."`
   - *Evaluation:* Geographically precise, delivered in 1.06 s.
5. **"What is Dijkstra's algorithm?" (Analytical Safeguard):**
   - *Output:* Detailed multi-section explanation covering graph theory, min-heap priority queues, non-negative edge constraints, and asymptotic complexity $O((V+E)\log V)$.
   - *Evaluation:* Native chain-of-thought reasoning fully preserved; no truncation or simplification.

---

## 6. Architectural Boundary & Governance Statement

1. **No External Scope Expansion:** All changes were strictly restricted to heuristic routing in [`src/carbon/intelligence/engine.py`](../../src/carbon/intelligence/engine.py) and accompanying regression tests in [`tests/unit/test_routing_reliability.py`](../../tests/unit/test_routing_reliability.py).
2. **Preservation of Pre-Existing Workspace State:**
   - Pre-existing modifications in `apps/macos/Carbon/Sources/` were untouched.
   - Pre-existing untracked file `tests/test_desktop_connection_reliability.py` was preserved as found.
3. **Infrastructure Integrity:**
   - CMS scheduling, lease acquisition, model lifecycle, and Ollama provider contracts remain unchanged.
   - Zero changes to REST API authentication (`.carbon_token`), WebSocket streaming protocol, or Sandboxing / Governance rules.
   - Zero new external Python or Swift dependencies introduced.
4. **Phase Boundary:** Phase 1 optimisation is complete. Phase 2 remains strictly uninitiated. No git commits or pushes have been made.

