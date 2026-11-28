# RQ-3: Model Residency — Cold Load vs. Warm Inference

**Status:** Data available  
**Priority:** Medium  
**Existing data:** `benchmarks/inference/results_100_runs.json`

---

## Research Question

> **What is the actual overhead of cold model loading vs. warm resident inference for `qwen3.5:9b` on Apple Silicon?**

---

## Motivation

Carbon's CMS is designed around model warm-residency as a core strategy: once loaded, `qwen3.5:9b` stays resident in unified memory. The 9216 MB static budget is sized to prevent eviction under normal macOS memory pressure. This design only pays off if the cold-load penalty is significant.

---

## Existing Evidence

From `benchmarks/inference/results_100_runs.json` (N=100, Oct 2026, Apple M5, 16 GB):

| Metric | Value |
|---|---|
| Cold start model load (`load_duration`) | **1.34 s** |
| Cold start total latency | **21.1 s** |
| Cold start TTFT | **16.2 s** |
| Warm TTFT P50 | **589 ms** |
| Warm TTFT P95 | **884 ms** |

**Warm/cold TTFT ratio:** ≈27× improvement from residency.

---

## Interpretation

The 1.34 s cold load is the minimum model-load time measured by Ollama's internal clock. The much larger cold TTFT (16.2 s) includes initial prompt evaluation after model load. After warm-up, TTFT drops to ~589 ms median.

This validates CMS warm-residency as a worthwhile strategy: eliminating the cold-load path reduces TTFT by >25×.

---

## Open Questions

1. Does thermal state affect warm TTFT? (The N=100 max TTFT of 69,448 ms is a suspected thermal or swap event.)
2. What is the warm TTFT when macOS memory pressure is elevated?
3. How does `MemoryPressureMonitor`'s context scaling (32768/8192/4096 tokens) affect TTFT?

---

## Links

- [CMS source](../../../src/carbon/model_control/cms.py)
- [OllamaProvider source](../../../src/carbon/model_control/ollama_provider.py)
- [Raw benchmark data](../../../benchmarks/inference/results_100_runs.json)
- [Experiment Log](../experiment-log.md)
