# Carbon 1.0.0 — Stage 1.1 Routing Reliability Correction Report

**Document Status:** Formal Implementation & Validation Report  
**Date:** 2026-10-28  
**Mode:** Controlled Implementation (IntelligenceEngine routing only)  
**Milestone:** Carbon 1.0.0 — Phase 1  
**Target File:** [`src/carbon/intelligence/engine.py`](../../src/carbon/intelligence/engine.py)  
**Test Suite:** [`tests/unit/test_routing_reliability.py`](../../tests/unit/test_routing_reliability.py)  

---

## 1. Executive Summary

During the Stage 1 Post-Implementation Code Review ([`docs/reports/stage1-post-implementation-review.md`](stage1-post-implementation-review.md)), two non-fatal classifier edge cases were identified in `IntelligenceEngine`:
1. **Precedence Inversion:** `_instant_patterns` was evaluated before `_deep_patterns`, causing complex questions with concise-answer clauses (e.g. *"Solve this multi-step mathematical puzzle. Answer in one word."*) to be downgraded to `INSTANT` (`think: false`).
2. **Arithmetic Word-Boundary Artifact:** The trailing `\b` in `^(calculate|compute|what is \d+[\s\+\-\*\/])\b` failed to match natural spaced arithmetic expressions (e.g. `"What is 37 + 58?"`) because space-to-operator is not a word boundary.

Stage 1.1 resolves both issues cleanly with zero architectural churn, full backward compatibility, 100% lint compliance on production code, and a dedicated 55-test regression pass across the test suite.

---

## 2. Technical Modifications in `IntelligenceEngine`

### 2.1 Heuristic Precedence Correction
The intent classification hierarchy in `IntelligenceEngine.classify_intent()` was restructured into a strict prioritized pipeline:

```mermaid
flowchart TD
    Prompt[User Prompt Ingestion] --> Stage1{1. Matches _deep_patterns?\n(solve, multi-step, puzzle, debug, architect...)}
    Stage1 -- Yes --> Deep[ReasoningDepth.DEEP\nCategory: COMPLEX_TASK\nthink: true, full reasoning preserved]
    Stage1 -- No --> Stage2{2. Matches fast conversational / system?\n(greetings, ping, open app, time...)}
    Stage2 -- Yes --> Instant1[ReasoningDepth.INSTANT\nCategory: CONVERSATIONAL / SYSTEM_COMMAND\nthink: false, num_predict: 120]
    Stage2 -- No --> Stage3{3. Matches _arithmetic_pattern?\n(37 + 58, calculate 125 - 45...)}
    Stage3 -- Yes --> Instant2[ReasoningDepth.INSTANT\nCategory: CONVERSATIONAL\nthink: false, num_predict: 120]
    Stage3 -- No --> Stage4{4. Matches _standard_indicators?\n(explain, why, how, write python, algorithm...)}
    Stage4 -- Yes --> Standard1[ReasoningDepth.STANDARD\nCategory: GENERAL_QUERY\nthink: true, concise clause ignored]
    Stage4 -- No --> Stage5{5. Factual prefix AND concise clause?\n(what is... + answer in one word)}
    Stage5 -- Yes --> Instant3[ReasoningDepth.INSTANT\nCategory: CONVERSATIONAL\nthink: false, num_predict: 120]
    Stage5 -- No --> Default[6. Default Fallback\nReasoningDepth.STANDARD\nCategory: GENERAL_QUERY]
```

**Key Invariants Established:**
- **Invariance 1 (Deep Immunity):** Any prompt containing deep keywords (`solve`, `multi-step`, `puzzle`, `debug`, `refactor`, `architect`, `analyze`, `plan step`, `compare deeply`, `code review`) is routed immediately to `DEEP`. Concise instructions (`answer in one word`, `just the number`) can never override deep complexity.
- **Invariance 2 (Standard Analytical Immunity):** Analytical and generative requests (`explain`, `why`, `how`, `write python`, `write function`, `algorithm`, `complexity`, `dijkstra`, `compare`) are protected in Stage 4. Concise instructions like *"Be concise"* or *"Answer in one word"* will not downgrade them to `INSTANT`.
- **Invariance 3 (Safe Factual Fast-Path):** Simple factual lookups only route to `INSTANT` if they have a factual interrogative prefix (`what is`, `who is`, `where is`, `when was`, `name the`, `state the`) **and** an explicit concise instruction (`answer in one word`, `just the number`), and contain zero analytical markers.

### 2.2 Arithmetic Matching Logic Correction
The arithmetic matching pattern was replaced with a robust regular expression supporting arbitrary whitespace around operators while strictly rejecting embedded or complex mathematical expressions:

```python
self._arithmetic_pattern = (
    r"^(?:what\s+is\s+|calculate\s+|compute\s+)?"
    r"[-+]?\d+(?:\.\d+)?"
    r"(?:\s*[-+*/×÷]\s*[-+]?\d+(?:\.\d+)?)+"
    r"(?:\s*\??\s*(?:answer\s+in\s+one\s+word|just\s+the\s+number|in\s+one\s+word|please))?"
    r"[\s.?!]*$"
)
```

**Capabilities:**
- Arbitrary operator spacing: `"37 + 58"`, `"37+58"`, `"what is 37   +   58 ?"`.
- Natural query prefixes: `"What is..."`, `"Calculate..."`, `"Compute..."`, or bare expressions (`"125 - 45"`).
- Standard operators: `+`, `-`, `*`, `/`, `×`, `÷`.
- Optional trailing punctuation and concise clauses (`?`, `.`, `Just the number.`, `Answer in one word.`).
- Strict anchoring (`^...$`): Embedded math in complex questions like *"Solve this multi-step mathematical problem: 37 + 58"* will not match the arithmetic fast path.

---

## 3. Modified-File Inventory

| File Path | Component | Nature of Changes |
| :--- | :--- | :--- |
| [`src/carbon/intelligence/engine.py`](../../src/carbon/intelligence/engine.py) | Production Classifier | Replaced heuristic pattern list with prioritized 6-stage classification pipeline. Added robust arithmetic regex and standard reasoning protection. |
| [`tests/unit/test_routing_reliability.py`](../../tests/unit/test_routing_reliability.py) | New Test Suite | Added 6 comprehensive test functions validating 55+ distinct routing scenarios across whitespace, complexity, factual lookup, and orchestrator integration. |
| [`tests/unit/test_inference_optimization.py`](../../tests/unit/test_inference_optimization.py) | Unit Test Maintenance | Cleaned mock callback signatures (`prompt`, `system`) and removed unused imports for 100% ruff compliance. |

---

## 4. Regression Test Results

### 4.1 New Routing Reliability Tests (`test_routing_reliability.py`)

All 6 test suites passed in **0.13 seconds**:

| Test Function | Scenarios Covered | Classified Category | Classified Depth | Status |
| :--- | :--- | :--- | :--- | :--- |
| `test_arithmetic_whitespace_variations` | 19 spacing variants (`"What is 37 + 58?"`, `"37+58"`, `"Calculate 125 - 45"`, `"Compute 12 * 8"`, `"What is 144 / 12?"`, `"10 + 20 - 5"`) | `CONVERSATIONAL` | `INSTANT` (`think: false`) | **PASSED** |
| `test_complex_math_with_concise_instructions` | 6 complex/multi-step/puzzle tasks with concise instructions (`"Solve this multi-step mathematical puzzle. Answer in one word."`, `"Solve for x: 2x + 5 = 15. Just the number."`) | `COMPLEX_TASK` | `DEEP` (`think: true`) | **PASSED** |
| `test_simple_factual_with_concise_instructions` | 7 factual queries (`"What is the capital of Japan? Answer in one word."`, `"State boiling point of water in Celsius. Answer in one word."`) | `CONVERSATIONAL` | `INSTANT` (`think: false`) | **PASSED** |
| `test_complex_coding_and_algorithm_questions` | 10 analytical/coding prompts (`"Explain the time complexity of Dijkstra's algorithm. Be concise."`, `"Write a Python function to check whether a number is prime."`, `"is_even"`) | `GENERAL_QUERY` | `STANDARD` (`think: true`) | **PASSED** |
| `test_existing_routing_behavior` | 11 cases verifying greetings (`"hello"`, `"ping"`), system commands (`"open Safari"`), and deep tasks (`"Debug this memory leak..."`) | Varied | Varied | **PASSED** |
| `test_orchestrator_integration_mode_selection` | Full pipeline integration with `CarbonCore`: validates that classified depth sets `think_flag` and `options` (`num_predict: 120`, `temp: 0.2` on INSTANT; `think: True`, `options: None` on STANDARD/DEEP). | Varied | Varied | **PASSED** |

### 4.2 Full Repository Test Suite
Ran `./.venv/bin/pytest tests/`:
- **Result:** **`55 passed, 1 warning in 23.22s`** (Exit Code 0).
- Zero regressions across unit, security, integration, and desktop reliability suites.

### 4.3 Code Quality and Lint Verification
- **Command:** `./.venv/bin/ruff check src tests/unit`
- **Result:** **`All checks passed!`** (0 errors, 0 warnings across all production source code and unit tests).

### 4.4 Desktop App Build Verification
- **Command:** `cd apps/macos/Carbon && swift build`
- **Result:** **`Build complete! (0.16 secs)`** (Exit Code 0).

---

## 5. Remaining Risks and Phase 1 Boundary

1. **Heuristic Keyword Boundaries:**
   - While the prioritized regex pipeline correctly resolves all tested ambiguity between concise instructions and deep tasks, regex pattern matching operates purely on lexical syntax rather than deep semantic embeddings.
   - Any query containing unfamiliar technical vocabulary not in `_deep_patterns` or `_standard_indicators` falls back to `ReasoningDepth.STANDARD`. This is a **fail-safe default** because `STANDARD` preserves full native model reasoning and does not truncate output.
2. **Phase 1 Boundary:**
   - No Phase 2 work has been initiated.
   - Model parameters, CMS reader-writer concurrency, and REST/WebSocket security remain unmodified.
   - Pre-existing uncommitted changes in `apps/macos/Carbon/Sources/` remain intact.
   - No git commits or pushes have been performed.

