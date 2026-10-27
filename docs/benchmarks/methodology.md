# Carbon Performance Benchmark Methodology

## 1. Objectives & Hardware Baseline

Performance is a first-class product requirement for Carbon 1.0.0. The goal of this benchmark methodology is to measure, validate, and prevent regressions in inference latency, orchestration overhead, memory retrieval speed, and system resource pressure on Apple Silicon macOS hardware.

### Hardware Baseline
- **Device:** Apple MacBook Air M5 (Apple Silicon ARM64)
- **Unified Memory:** 16 GB Unified Memory
- **Operating System:** macOS (macOS 14+ / 27+)
- **Model Runtime:** Ollama (v0.33+) with `qwen3.5:9b` (Q4_K_M GGUF, ~6.55 GB)

---

## 2. Key Metrics & Engineering Targets

| Metric | Measurement Scope | Initial Engineering Target |
|---|---|---|
| **Simple Request TTFT** | Time-To-First-Token on warm simple conversational request (`"Hi"`, `"Status"`) | **P95 ≤ 2.0 seconds** |
| **Standard Request TTFT** | Time-To-First-Token on standard explanation / synthesis prompt (approx. 50 tokens input) | **P95 ≤ 4.0 seconds** |
| **Cached Memory Retrieval** | SQLite indexed key retrieval and FTS query from warm cache | **P95 ≤ 100 ms** |
| **Core Orchestration Overhead** | Total latency introduced by Carbon Core (intent, routing, governance, tool parsing) excluding LLM inference | **P95 ≤ 50 ms** |
| **Model Switching Time** | Time required to unload model A, free memory buffers, and pre-warm model B | Tracked baseline (target ≤ 6.0s cold) |
| **Process Memory Footprint** | Resident Set Size (RSS) of Python backend process | **RSS ≤ 250 MB** |
| **Unified Memory Headroom** | Available macOS unified memory during active 9B model inference | **Free/Inactive Memory ≥ 4.0 GB** |
| **Stability Under Load** | Continuous operation without memory leak or crash | **60 minutes continuous test** |

---

## 3. Benchmark Harness Design

Benchmarks are implemented as reproducible Python scripts under `benchmarks/`:

1. `benchmarks/inference/`: Measures cold-load duration, warm TTFT, tokens-per-second generation rate, and prompt evaluation latency.
2. `benchmarks/orchestration/`: Measures microsecond overhead of Core task scheduling, intent classification heuristics, and governance checks.
3. `benchmarks/memory/`: Measures SQLite read/write latency under synthetic scale (1,000 to 10,000 records) and FTS search response times.
4. `benchmarks/voice/`: Measures audio chunk processing, STT transcription latency, and TTS audio synthesis lead time.

---

## 4. Execution Protocol

1. Close non-essential background applications to prevent CPU/memory contention.
2. Verify Ollama is running and `qwen3.5:9b` is present.
3. Run the automated benchmark harness:
   ```bash
   ./scripts/benchmark.sh
   ```
4. Output results in structured JSON and markdown reports for comparison against previous commits.
