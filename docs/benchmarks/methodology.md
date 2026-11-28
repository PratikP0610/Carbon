# Carbon Performance Benchmark Methodology

**Stage 7.5 Alignment Status:** Aligned to verified benchmark scripts, CI performance gates, and [metrics.py](../../src/carbon/core/metrics.py).

---

## 1. Benchmark Tiers & Strict Separation Rules

To avoid mixing synthetic mock timings with live Apple Silicon LLM runs, Carbon separates performance evidence cleanly into three tiers:

| Tier | Environment & Provider | What It Measures | Current Status |
| :--- | :--- | :--- | :--- |
| **Tier 1 (Deterministic CI / Unit Harnesses)** | `FakeProvider`, isolated temp SQLite DBs, SwiftUI/Markdown unit coordinators | Orchestration dispatch, model switch routing overhead, WS E2E framing overhead, SQLite 1k-row queries, stream teardown/cancellation cleanup, concurrency chaos, UI flush & Markdown rates | **IMPLEMENTED** (Stage 5-E Tier 1 baseline accepted) |
| **Tier 2 (Live Local Ollama Inference)** | Local Ollama (`127.0.0.1:11434`, `qwen3.5:9b`) | Cold load, TTFR, TTFT_resp, `eval_count / eval_duration`, turnaround time | **Historical baseline** (Oct 28, `N=100` & clean runs); **UNVERIFIED** for Stage 5+/6.9-F builds |
| **Tier 3 (Sustained Hardware / Stress / Soak)** | Live hardware under multi-hour / real Mach kernel memory pressure | Thermal stability, real kernel OOM/pressure recovery, extended soak | **UNVERIFIED / Unauthorized** |

> [!IMPORTANT]
> Tier 1 results validate regression gates on backend/UI plumbing only. They **do not** constitute live LLM inference measurements or proof of production-scale hardware performance.

---

## 2. Verified Hardware Baseline & Statistical Method

- **Target Hardware Baseline (from Oct 28 benchmark docs/scripts):** Apple Silicon MacBook Air M5, 16 GB Unified Memory, macOS Darwin, local Ollama (`http://127.0.0.1:11434`) running `qwen3.5:9b` (`Q4_K_M`).
- **Percentile Method (`src/carbon/core/metrics.py`):** NIST Linear Interpolation (`nist_percentile` / `summarize_distribution`) using $R = 1 + \frac{P}{100}(N - 1)$, reporting `count`, `min`, `p50`, `p90`, `p95`, and `max`.
- **Single-Clock Integrity (`measure_ws_submission` & `TaskLatencySample`):** Client timings use the client's `time.perf_counter()` and backend timings use backend `time.perf_counter()`; Carbon explicitly prevents cross-clock subtraction (`"cross_clock_subtraction": false`).

---

## 3. Existing vs. Placeholder Harness Locations

Verified against the repository tree:

| Path | Contents in Repository | Status |
| :--- | :--- | :--- |
| `tests/performance/test_stage5e_tier1_gates.py` | Stage 5-E E3 teardown/cancellation gate (`N=30`) & E4 220-op concurrency/chaos gate | **IMPLEMENTED** (Tier 1) |
| `tests/performance/test_orchestration_benchmarks.py` | Stage 5-A warm (`N=50`), switched (`N=20`), and WS E2E (`N=30`) dispatch benchmarks (`FakeProvider`) | **IMPLEMENTED** (Tier 1) |
| `tests/performance/test_memory_benchmarks.py` | Stage 5-A SQLite 1k-row memory & task query benchmarks (`N=50`) | **IMPLEMENTED** (Tier 1) |
| `apps/macos/Carbon/Tests/test_stage5e_streaming_fidelity.swift`, `test_markdown.swift` | SwiftUI update rate & Markdown coordinator parsing benchmarks | **IMPLEMENTED** (Tier 1) |
| `benchmarks/inference/` | `statistical_benchmark.py`, `benchmark_100_runs.py`, `benchmark_e2e_pipeline.py`, `results_100_runs.json`, `results_e2e_clean.json` | **PARTIALLY IMPLEMENTED** *(Historical Oct 28 scripts/datasets + Stage 5-A monotonic E2E harness; `CarbonCore` streams Ollama `POST /api/generate`)* |
| `benchmarks/memory/` | `hardware_memory_verification.py`, `hardware_verification_results.json` | **IMPLEMENTED** *(Historical Oct 28 memory script/artifact)* |
| `benchmarks/orchestration/` | Empty directory (`0` files; orchestration benchmarks live in `tests/performance/`) | **PLANNED** *(Directory unused)* |
| `benchmarks/voice/` | Empty directory (`0` files) | **DEFERRED** *(Voice Stage 6.6 deferred)* |
