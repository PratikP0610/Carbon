# Threats to Validity

This document is an honest assessment of measurement limitations that bound all Carbon benchmark claims. Any interpretation of Carbon's performance data must be read in light of these constraints.

---

## 1. Single-Machine, Single-Run Environment

All benchmark results were collected on one Apple M5 MacBook Air (16 GB unified memory, macOS 27.x). Results on different hardware, OS versions, or memory configurations will differ — potentially substantially. No external validation or cross-machine replication has been performed.

**Impact:** Generalizability is limited. Results are valid for the specific configuration measured, not for Apple Silicon in general.

---

## 2. No Thermal or Background-Load Control

Benchmarks were run without controlling for:
- CPU/GPU thermal throttling
- macOS background processes (Spotlight, iCloud sync, etc.)
- Battery vs. AC power (may affect M5 P-core/E-core scheduling)
- Memory pressure from other applications

The N=100 `results_100_runs.json` benchmark has a max TTFT of 69,448 ms (vs. P95 of 884 ms). This outlier likely reflects a thermal or swap event, but was not investigated. The P95 estimate from 100 samples is more reliable than from smaller N.

**Impact:** P95/P99 estimates may be optimistic. Extreme latency tails are poorly characterized.

---

## 3. Small Sample Sizes

| Benchmark | N | Limitation |
|---|---|---|
| Stage 1-10 Deep reasoning | 5 | P95 from N=5 is essentially the max observation |
| Stage 5-A WS E2E | 30 | P95 estimate spans ±10–20% at this N |
| Stage 5-A FakeProvider | 50 | Reasonable for P50; P95 still has ±5–10% uncertainty |
| Direct Ollama baseline | 100 | Best available; P95 estimate reasonable |

**Impact:** Small-N P95 estimates have wide confidence intervals and should not be treated as precise bounds.

---

## 4. Stochastic LLM Variability

`qwen3.5:9b` is a probabilistic model. Identical prompts produce different token sequences, different lengths, and therefore different generation times. No two runs are independent measurements of the same quantity.

**Impact:** Generation speed and total latency benchmarks conflate prompt-specific effects with system-level effects. A/B comparisons require matched prompts and larger N than currently available.

---

## 5. Ollama Internal Timing vs. Monotonic Clock

Carbon's `CarbonMetricsRegistry` measures elapsed time using Python's `asyncio` event loop clock. Ollama reports `load_duration`, `prompt_eval_duration`, and `eval_duration` from its own internal clock. These two clocks are not synchronized and may have different epoch offsets or resolution.

**Impact:** Direct arithmetic comparison of Carbon span times and Ollama internal times may be misleading. The comparison is directionally valid but not precisely additive.

---

## 6. Same-Clock Rule (Not Always Followed)

The Stage 5-A WS E2E benchmark measures `client→first-answer` latency using the same clock on the same machine. This is correct. The `results_100_runs.json` benchmark also uses same-machine timing. No cross-machine latency measurements exist.

**Impact:** Latency measurements are internally consistent but do not reflect real network transport to a different host.

---

## 7. FakeProvider vs. Live Ollama

FakeProvider benchmarks (`tests/performance/`) measure Carbon orchestration overhead with zero inference cost. These are deterministic and highly reproducible. They do **not** measure real inference performance.

Live Ollama benchmarks measure real inference but cannot isolate Carbon overhead without the FakeProvider comparison.

**Impact:** The two benchmark types answer different questions and should not be directly compared without EXP-000's decomposition methodology.

---

## 8. Schema Version Split

The pre-existing `data/carbon.db` is on schema v2. Fresh test databases use schema v3 (`memory_records.conversation_id` column). Memory performance benchmarks run against v3 (test isolation). Real-world v2 behavior may differ if queries use the new column.

**Impact:** SQLite memory benchmarks reflect v3 schema, not the production v2 database.

---

## 9. No Statistical Independence of Repeated Trials

Repeated inference runs on the same machine against the same resident model are not statistically independent: later runs benefit from OS page cache warmth, Ollama KV cache state, and CPU branch predictor state. The first run after model load is distinct from all subsequent runs.

**Impact:** P50 estimates from warm repeated runs may be optimistically low relative to realistic usage patterns (which mix cold and warm queries).
