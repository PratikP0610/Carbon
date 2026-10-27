# Carbon Performance Benchmark Methodology

## 1. Objectives & Hardware Baseline

Performance is a first-class product requirement for Carbon 1.0.0. The goal of this benchmark methodology is to measure, validate, and prevent regressions in inference latency, orchestration overhead, memory retrieval speed, and system resource pressure on Apple Silicon macOS hardware.

### Hardware Baseline
- **Device:** Apple MacBook Air M5 (Apple Silicon ARM64)
- **Unified Memory:** 16 GB Unified Memory
- **Operating System:** macOS (macOS 14+ / 27+)
- **Model Runtime:** Ollama (v0.33+) with `qwen3.5:9b` (Q4_K_M GGUF, ~6.55 GB)

---

## 2. Key Metrics & Statistical Distributions

In accordance with Phase 0 revisions, all benchmark observations must be derived from repeated runs (N ≥ 5 for inference, N ≥ 50 for micro-benchmarks) and reported as a statistical distribution:
- **Minimum (Min)**
- **Median (P50)**
- **95th Percentile (P95)**
- **Maximum (Max)**

| Metric | Measurement Scope | PRD Target | Statistical Reporting |
|---|---|---|---|
| **Cold Model Load Time** | Disk APFS → Metal Unified Memory allocation | Baseline tracked | Single cold observation post `keep_alive: 0` |
| **Simple Request TTFT** | Time-To-First-Token on warm simple prompt (e.g., status, basic math) | **P95 ≤ 2.0s** | Min, Median, P95, Max |
| **Standard Request TTFT** | Time-To-First-Token on 50-token input prompt | **P95 ≤ 4.0s** | Min, Median, P95, Max |
| **Generation Throughput** | Output tokens generated per second (`eval_count / eval_duration`) | ≥ 15 tok/s | Min, Median, P95, Max |
| **Total Turnaround Latency**| Total request execution time from dispatch to final token | Task-dependent | Min, Median, P95, Max |
| **Model Switching Time** | Explicit unload of active model + load of target model | ≤ 6.0s cold | Repeated switch iterations |
| **Core Orchestration Overhead**| Core pipeline delay (intent, governance, validation) excluding LLM | **P95 ≤ 50ms** | Min, Median, P95, Max |
| **Host Memory Pressure** | Mach kernel pressure level during peak inference | Level 1 (Normal) | Sampled before, during, and after inference |

---

## 3. Real-World Testing Conditions

To ensure benchmarks reflect actual user environments rather than synthetic isolation, tests must be conducted under **Normal macOS Operational Workloads**:
1. **Background User Applications Active:** macOS desktop session running standard developer applications (browser with multiple active tabs, terminal windows, code editor, background sync daemons).
2. **Unified Memory Baseline:** Document initial memory state (`vm_stat`, `sysctl kern.memorystatus_vm_pressure_level`, swap allocation).
3. **Thermal State:** Monitor thermal throttling indicators (`pmset -g therm`) to verify Apple Silicon is operating without passive thermal throttling penalties.
4. **Distinguish Metrics:**
   - *Disk Footprint:* APFS file size on disk.
   - *Resident VRAM Allocation:* Physical Metal GPU buffer allocation.
   - *Host Memory Pressure:* Mach virtual memory pressure level (Normal = 1, Warning = 2, Critical = 4).

---

## 4. Benchmark Harness Design & Execution Protocol

Benchmarks are organized into reproducible scripts under `benchmarks/`:
1. `benchmarks/inference/statistical_benchmark.py`: Measures cold load, warm TTFT, generation speed, total turnaround latency, and computes statistical distributions.
2. `benchmarks/orchestration/`: Measures microsecond overhead of Core task scheduling and governance checks.
3. `benchmarks/memory/`: Measures SQLite read/write latency under scale and FTS5 search times.
4. `benchmarks/voice/`: Measures audio chunk processing and speech pipeline latency.

Execution command:
```bash
.venv/bin/python benchmarks/inference/statistical_benchmark.py
```

