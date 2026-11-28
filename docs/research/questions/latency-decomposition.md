# RQ-1: Latency Decomposition — Orchestration vs. Inference

**Status:** Active (EXP-000 planned)  
**Priority:** High  
**Experiment:** [EXP-000](../experiments/EXP-000-latency-decomposition/plan.md)

---

## Research Question

> **How much of end-to-end latency is Carbon orchestration overhead vs. model inference?**

Specifically: when a user submits a chat message over WebSocket, what fraction of elapsed time is spent in:

1. Carbon's intent classification and reasoning depth routing?
2. CMS lease acquisition and prompt construction?
3. Ollama model inference (prompt evaluation + token generation)?
4. Network/WebSocket transport overhead?

---

## Motivation

Carbon's architecture imposes a non-trivial orchestration stack before Ollama generates the first token. If this overhead is negligible relative to inference, it validates the design. If it is significant, it identifies optimization targets.

The FakeProvider benchmarks (Stage 5-A) show Carbon orchestration adds P95 < 2 ms on the warm path with zero inference cost. The question is whether this holds end-to-end against a live Ollama process.

---

## Measured Spans

Carbon's `CarbonMetricsRegistry` captures:

| Span | Metric key | Covers |
|---|---|---|
| Accept → ProviderStart | `backend_acceptance_to_provider_start_ms` | Intent classify + CMS lease + prompt build |
| ProviderStart → FirstAnswer | `provider_execution_to_first_answer_ms` | Ollama prompt eval + first token |
| Accept → Completion | `backend_acceptance_to_completion_ms` | Full end-to-end (backend side) |

---

## Existing Evidence

| Source | Method | Key Result |
|---|---|---|
| Stage 5-A FakeProvider (N=50) | Deterministic test stub | Accept→ProviderStart P50=1.021ms, P95=1.183ms |
| Stage 5-A FakeProvider (N=50) | Deterministic test stub | Accept→Completion P50=1.744ms, P95=1.914ms |
| `results_100_runs.json` (N=100, Oct 2026) | Live Ollama `/api/generate` | TTFT P50=589ms, P95=884ms |

The FakeProvider data isolates Carbon overhead. The `results_100_runs.json` data provides the inference baseline. EXP-000 will close the gap by measuring both simultaneously through the Carbon stack.

---

## Hypotheses

- **H1:** Carbon orchestration overhead (Accept→ProviderStart) is <5 ms P95 on the warm path with live Ollama.
- **H2:** Ollama prompt evaluation (not token generation) is the dominant latency component for short prompts.
- **H3:** Cold model load (first request after Ollama start) adds 1–4 s to TTFT, while subsequent requests (warm) show TTFT < 1 s.

---

## Open Sub-questions

1. Does `think=True` (Deep reasoning) change the TTFT distribution, or only the total generation length?
2. Is there measurable jitter in `backend_acceptance_to_provider_start_ms` under concurrent WS connections?
3. How does prompt length (short vs. long) affect Ollama prompt evaluation time?

---

## Links

- [EXP-000 Plan](../experiments/EXP-000-latency-decomposition/plan.md)
- [Experiment Log](../experiment-log.md)
- [Benchmarks methodology](../../benchmarks/methodology.md)
- [FakeProvider source](../../../src/carbon/model_control/fake_provider.py)
- [Metrics source](../../../src/carbon/core/metrics.py)
