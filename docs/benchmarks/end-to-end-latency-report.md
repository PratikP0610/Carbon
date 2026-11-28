# Carbon — Latency, Orchestration & Tier 1 Verification Report

**Stage 7.5 Alignment Status:** Separates **accepted Stage 5-E Tier 1 deterministic benchmarks** (`FakeProvider` / isolated SQLite / Swift unit coordinators) cleanly from **historical Oct 28 live M5 hardware baselines** and **unverified Tier 2 / Tier 3 benchmarks**.

---

## 1. Accepted Stage 5-E Tier 1 Benchmark Baseline (`IMPLEMENTED` — Deterministic CI Only)

All metrics below are from the accepted Stage 5-E Tier 1 verification suite (`FakeProvider`, isolated temp SQLite databases, and Swift test runners):

| Benchmark | Accepted Result | Scope & Conditions | Source Evidence |
| :--- | ---: | :--- | :--- |
| **Warm dispatch P95** | **0.993 ms** | `N=50` warm resident dispatch (`backend_acceptance_to_provider_start_ms`), `FakeProvider`, isolated DB | [test_orchestration_benchmarks.py](../../tests/performance/test_orchestration_benchmarks.py) |
| **Switched dispatch P95** | **1.094 ms** | `N=20` alternating category model switch dispatch, `FakeProvider` | [test_orchestration_benchmarks.py](../../tests/performance/test_orchestration_benchmarks.py) |
| **WebSocket E2E P95** | **1.282 ms** | `N=30` `/ws/chat` backend acceptance to provider start (`FakeProvider`, `TestClient`) | [test_orchestration_benchmarks.py](../../tests/performance/test_orchestration_benchmarks.py) |
| **SQLite recent turns, 1k rows P95** | **0.096 ms** | `N=50` `get_recent_conversation_turns()` over seeded 1,000-row temp SQLite DB | [test_memory_benchmarks.py](../../tests/performance/test_memory_benchmarks.py) |
| **Stream teardown P95** | **3.066 ms** | `N=30` combined (`15` early `aclosing` + `15` mid-stream `cancel_task`), `FakeProvider` (`25.0 ms` gate) | [test_stage5e_tier1_gates.py](../../tests/performance/test_stage5e_tier1_gates.py) |
| **Cancellation P95** | **3.256 ms** | `N=15` mid-stream `cancel_task` (`FakeProvider`, `chunk_delay_seconds=0.05`); improved from `52.519 ms` to `3.256 ms` (~`93.8%` reduction) | [test_stage5e_tier1_gates.py](../../tests/performance/test_stage5e_tier1_gates.py) |
| **SwiftUI update rate** | **24.25 updates/sec** | `240` tokens (~220 tok/s arrival over `1.320s`), `33` periodic UI flushes (`<= 25/s` gate), 100% FIFO fidelity | [test_stage5e_streaming_fidelity.swift](../../apps/macos/Carbon/Tests/test_stage5e_streaming_fidelity.swift) |
| **Markdown parsing rate** | **10.19 parses/sec** | `6` `MarkdownCoordinator` parses in `0.491s` across `80` streamed tokens + final AST verification | [test_stage5e_streaming_fidelity.swift](../../apps/macos/Carbon/Tests/test_stage5e_streaming_fidelity.swift#L148-L176) |
| **Concurrency & chaos gate** | **220 concurrent ops** | `70` completed streams + `40` cancelled streams + `70` SQLite ops + `40` CMS pressure/rejected-switch ops; `0` deadlocks, `0` leaks, `0` SQLite errors, valid SHA-256 `audit.log` chain | [test_stage5e_tier1_gates.py](../../tests/performance/test_stage5e_tier1_gates.py) |

### Mandatory Tier 1 Qualifications & Limitations

1. **Tier 1 only — not production scale:** These timings validate in-process plumbing, lock release, and SQLite/UI responsiveness in bounded CI tests; they are **not** proof of sustained production-scale performance.
2. **Zero live LLM or hardware GPU measurement:** Python orchestration benchmarks used `FakeProvider`, not Ollama Metal inference.
3. **Tier 2 and Tier 3 remain UNVERIFIED:** Current post-Stage 5/6 live hardware inference (`Tier 2`) and multi-hour / real-pressure soak testing (`Tier 3`) remain unauthorized / unverified.

---

## 2. Architecture Path & Single-Clock Telemetry Model (`IMPLEMENTED`)

In current code ([main.py](../../src/carbon/main.py), [orchestrator.py](../../src/carbon/core/orchestrator.py), [ollama_provider.py](../../src/carbon/model_control/ollama_provider.py), [metrics.py](../../src/carbon/core/metrics.py)):

1. SwiftUI `CarbonClient` submits `{"action": "submit", ...}` over `/ws/chat` using its own client `time.perf_counter()` clock.
2. Backend measures `backend_acceptance_to_provider_start_ms`, `backend_acceptance_to_answer_ms`, `provider_execution_to_first_answer_ms`, reasoning equivalents, and `backend_acceptance_to_completion_ms` strictly on backend `time.perf_counter()` (`"cross_clock_subtraction": false`).
3. `CarbonCore` acquires CMS's single-model concurrency lease (`acquire_inference_lease` / `asyncio.Lock`), then streams `OllamaProvider.generate_stream_events()` via **`POST /api/generate`** (with `think` and `options` per `ReasoningDepth`; `chat_stream()` `/api/chat` is unused) and releases the lease in `finally:`.

---

## 3. Historical Live M5 Hardware Reference (Oct 28, Pre-Stage 5/6)

> [!NOTE]
> The values below come from the **historical Oct 28, 2026** baseline (`benchmarks/inference/results_e2e_clean.json` & `results_100_runs.json`) measured on an Apple M5 (16 GB) before adaptive reasoning and Stage 5 optimizations. They are **historical reference data**, not current Tier 2 verification.

### Historical Clean Normal-Operation Breakdown (Oct 28, Pre-Optimization)

| Stage | Path Component | Min | P50 | P90 | P95 | Max |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Stage 1** | Submit $\to$ API Receipt ($T_1 - T_0$) | 0.20 ms | 0.30 ms | 0.30 ms | 0.30 ms | 0.30 ms |
| **Stage 2** | API Receipt $\to$ Lease/Infer ($T_2 - T_1$) | 0.40 ms | 0.50 ms | 0.66 ms | 0.78 ms | 0.90 ms |
| **Stage 3** | First Reasoning Token TTFR ($T_3 - T_0$) | 403.90 ms | 499.70 ms | 576.52 ms | 587.86 ms | 599.20 ms |
| **Stage 4** | Backend First Answer ($T_4 - T_0$) | 10,743.20 ms | 16,710.90 ms | 97,596.36 ms | 125,878.08 ms | 154,159.80 ms |
| **Stage 5** | SwiftUI Rendered TTFT ($T_5 - T_0$) | 10,743.40 ms | 16,711.10 ms | 97,596.56 ms | 125,878.28 ms | 154,160.00 ms |
| **Gap** | Transit & Rendering ($T_5 - T_4$) | 0.10 ms | 0.20 ms | 0.20 ms | 0.20 ms | 0.20 ms |
| **Stage 6** | Total Completion ($T_6 - T_0$) | 10.80 s | 17.46 s | 110.76 s | 141.54 s | 172.32 s |

- **Historical PRD Evaluation (Pre-Stage 1.9 unconditional thinking):** Under Oct 28 defaults, simple/complex prompts met `< 1.0s` / `< 3.0s` for reasoning start (`TTFR`), **did not meet** them for visible response start (`UI TTFT`), and met `>= 15.0 tok/s` generation speed (`15.07 P50` / `22.09 P95`).
- **Historical Contention Subset (`N=71` from `results_100_runs.json`):** Contention TTFR `597.35 ms P50` / `885.87 ms P95` (max `69,448.20 ms` outlier from unmediated external `pytest` queueing Ollama); contention speed `14.98 tok/s P50` / `15.82 tok/s P95`.

---

## 4. Unverified Benchmarks Remaining for Later Stages (`UNVERIFIED` / `PLANNED` / `DEFERRED`)

- **Tier 2 Hardware Inference Re-Verification:** Re-measuring live M5 latency across `INSTANT` / `STANDARD` / `DEEP` on the current post-Stage 6.9-F backend (**UNVERIFIED**).
- **Tier 3 Sustained / Pressure Soak Tests:** Kernel-triggered real memory-pressure & multi-hour endurance runs (**UNVERIFIED**).
- **Voice Latency Benchmarks (`benchmarks/voice/`):** **DEFERRED** (Stage 6.6 voice subsystem deferred).
