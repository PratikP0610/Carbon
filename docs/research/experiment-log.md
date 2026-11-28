# Experiment Log

Chronological record of all Carbon experiments. Each entry links to the full plan or report.

---

## Log

| ID | Title | Date | Status | Report |
|---|---|---|---|---|
| [EXP-000](experiments/EXP-000-latency-decomposition/plan.md) | Latency Decomposition: Orchestration vs. Inference | 2026-11-28 | Plan only | [Plan](experiments/EXP-000-latency-decomposition/plan.md) |

---

## Pre-EXP Benchmarks (Accepted Evidence)

The following measurements were taken during Carbon development stages and are accepted as valid evidence, though not formally structured as numbered experiments.

| Benchmark | Date | N | Method | Key Results |
|---|---|---|---|---|
| Direct Ollama baseline (N=100) | Oct 27 2026 | 100 | `benchmarks/inference/statistical_benchmark.py` | TTFT P50=589ms, P95=884ms, gen 15.1 tok/s |
| FakeProvider orchestration (Stage 5-A) | Oct 2026 | 50 | `tests/performance/test_orchestration_benchmarks.py` | Accept→Completion P50=1.744ms, P95=1.914ms |
| WS E2E FakeProvider (Stage 5-A) | Oct 2026 | 30 | `tests/performance/test_orchestration_benchmarks.py` | client→first-answer P50=1.567ms |
| SQLite memory baseline (Stage 5-A) | Oct 2026 | 50 | `tests/performance/` | Retrieve P50=0.113ms, FTS5 search P50=0.222ms |
| Stage 1-10 reasoning depth N=5 | Oct 29 2026 | 5 | `scripts/run_stage1_10_benchmark.py` | Warm instant TTFT=0.113s, cold=4.451s |
| Stage 1-10 E2E optimization (N=30) | Oct 29 2026 | 30 | `scripts/run_stage1_10_optimization_benchmark.py` | See `docs/reports/stage1_10-generation-benchmark-results.json` |

---

## Notes

- All N=20–50 deterministic FakeProvider benchmarks are reproducible with `make test`.
- Live Ollama benchmarks depend on host thermal state and model residency and are not reproducible with identical results.
- EXP-000 will be the first formally structured experiment with pre-registered hypotheses.
