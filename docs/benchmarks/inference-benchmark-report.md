# Carbon — Inference Benchmark Report (Historical Phase 0/1 Baseline + Tier 1 Distinction)

**Stage 7.5 Alignment Status:** Historical Oct 28 empirical results preserved strictly under their original conditions, separated from the accepted Stage 5-E Tier 1 deterministic baseline and updated to match current architecture ([ollama_provider.py](../../src/carbon/model_control/ollama_provider.py), [cms.py](../../src/carbon/model_control/cms.py)).

> [!IMPORTANT]
> **Validity Scope:**
> - **Section 1–6 (Oct 28, `N=100` live Ollama runs):** Historical **pre-Stage 5/6** baseline recorded on `2026-10-28` (`benchmarks/inference/results_100_runs.json`). Note that `OllamaProvider.generate_stream_events()` streams Ollama `/api/generate` (`chat_stream()` `/api/chat` exists unused) under `CarbonModelSupervisor` `asyncio.Lock` inference leasing (not a multi-reader "RWLock").
> - **Current Post-Stage 5/6 Live Hardware Benchmarks (Tier 2 / Tier 3):** **UNVERIFIED / Unauthorized** under Stage 7.5. No new live inference runs were executed in Stage 7.5.
> - **Current Orchestration, SQLite & UI Plumbing (Tier 1):** Accepted in **Stage 5-E** using `FakeProvider` and isolated harnesses (see [end-to-end-latency-report.md](./end-to-end-latency-report.md)).

---

## 1. Historical Live Inference Baseline Summary (2026-10-28, `N=100`, `qwen3.5:9b`)

- **Target Hardware (historical run):** Apple Silicon MacBook Air M5, 16 GB Unified Memory, local Ollama (`127.0.0.1:11434`), `qwen3.5:9b` (`Q4_K_M`, 9.0B parameters).
- **Percentile Method:** NIST Linear Interpolation ($R = 1 + \frac{P}{100}(N - 1)$).
- **Workload:** 50 short prompts (~10–30 tokens) alternating with 50 long prompts (~80–180 tokens).

### Historical PRD Target Evaluation (Pre-Stage 1.9 Reasoning Optimization)

| Metric | Original PRD Target | Historical P50 | Historical P95 | Historical Verdict | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **TTFR (First Reasoning)** | P95 < 1.0s (Simple) / < 3.0s (Complex) | **589.15 ms** | **883.74 ms** | **MET** | Measured up to `<think>` start |
| **TTFT_resp (First Visible Text)** | P95 < 1.0s (Simple) / < 3.0s (Complex) | **5,991.93 ms** | **13,911.23 ms** | **NOT MET** | Pre-Stage 1.9 unconditional thinking consumed ~5–14s before answer output |
| **Generation Speed** | $\ge 15$ tok/s | **15.07 tok/s** | **22.09 tok/s** | **MET** | `12.86` min – `22.26` max tok/s |
| **Single-Model Concurrency** | 1 Chat LLM max | 1 active lease | 1 active lease | **MET** | Exclusive inference lease enforced |

---

## 2. Historical Overall Summary (Oct 28, `N=100` Retained Dataset)

| Metric | Minimum | P50 (Median) | P90 | P95 | Maximum |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **TTFR (First Reasoning)** | 301.48 ms | **589.15 ms** | **871.59 ms** | **883.74 ms** | 69,448.20 ms* |
| **TTFT_resp (First Visible Response)** | 4,457.21 ms | **5,991.93 ms** | **10,741.55 ms** | **13,911.23 ms** | 17,080.90 ms |
| **Generation Speed** | 12.86 tok/s | **15.07 tok/s** | **18.53 tok/s** | **22.09 tok/s** | 22.26 tok/s |
| **Total Turnaround Time** | 5.69 s | **11.65 s** | **17.71 s** | **17.81 s** | 77.51 s* |

*\*Outlier Iteration 25 (69.45s TTFR, 77.51s total): occurred because an external `pytest` run sent 3 unmediated requests directly to Ollama concurrently (`T=00:27:14`), queuing Iteration 25 in Ollama's HTTP backlog.*

---

## 3. Historical Condition Split: Normal (`N=29`) vs. Contention (`N=71`)

### Normal Operation (Mach Pressure Level 1, `N=29`, historical)

| Metric | Min | P50 | P90 | P95 | Max |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **TTFR** | 301.48 ms | **405.94 ms** | **565.66 ms** | **587.80 ms** | 608.54 ms |
| **Generation Speed** | 15.10 tok/s | **21.50 tok/s** | **22.18 tok/s** | **22.25 tok/s** | 22.26 tok/s |

### Resource Contention (Mach Level 2 Warning & Background Workload, `N=71`, historical)

| Metric | Min | P50 | P90 | P95 | Max |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **TTFR** | 536.30 ms | **597.35 ms** | **873.53 ms** | **885.87 ms** | 69,448.20 ms* |
| **Generation Speed** | 12.86 tok/s | **14.98 tok/s** | **15.42 tok/s** | **15.82 tok/s** | 16.70 tok/s |

### Historical Cold Start Observation (after `keep_alive: 0`)

- Model Load Duration: **1.342s** | Cold TTFR: **1.675s** | Cold TTFT_resp: **16.154s** | Cold Total: **21.147s** | Cold Speed: **22.14 tok/s**.

---

## 4. Architectural Changes & Current Unverified Tier 2/3 Gaps

1. **Current Code Changes Since Oct 28 (`IMPLEMENTED`):**
   - `CarbonCore` streams `OllamaProvider.generate_stream_events()` (`POST /api/generate`) with single-model `asyncio.Lock` inference leasing (`CarbonModelSupervisor.acquire_inference_lease`); `OllamaProvider.chat_stream()` (`/api/chat`) exists unused.
   - Adaptive reasoning (`ReasoningDepth.INSTANT` / `STANDARD` / `DEEP`) and Stage 1.10 generation optimizations were added later in the branch (their standalone reports reside in `docs/reports/`).
2. **Unverified Current Live Inference Benchmarks (`UNVERIFIED`):**
   - Formal Tier 2 / Tier 3 live hardware benchmark re-verification on the current post-Stage 6.9-F codebase has **not** been authorized or executed; Stage 5-E accepted Tier 1 deterministic gates only (detailed in [end-to-end-latency-report.md](./end-to-end-latency-report.md)).
