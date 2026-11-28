# EXP-000: Latency Decomposition — Carbon Orchestration vs. Ollama Inference

**Status:** Plan only (experiment not yet run)  
**Research question:** [RQ-1](../../questions/latency-decomposition.md)  
**Created:** 2026-11-28  
**Planned runtime:** ~10–15 minutes (live Ollama required)

---

## Hypothesis

Carbon's orchestration overhead (intent classification + CMS lease acquisition + prompt construction) adds **< 5 ms P95** on the warm path. Ollama model inference (prompt evaluation + token generation) dominates total end-to-end latency for typical prompts.

---

## Experimental Design

### Variables

| Variable | Values |
|---|---|
| Provider | Direct Ollama (baseline) vs. Carbon E2E (live Ollama) vs. Carbon FakeProvider |
| Prompt length | Short (< 20 words) vs. Long (> 100 words) |
| Reasoning depth | Instant vs. Standard |
| Model state | Cold (first request after `ollama serve`) vs. Warm (subsequent) |

### Metrics

| Metric | Source | Interpretation |
|---|---|---|
| `load_duration_ms` | Ollama `/api/generate` response | Model cold-load time |
| `prompt_eval_duration_ms` | Ollama `/api/generate` response | Prompt tokenization + first-token prep |
| `eval_duration_ms` | Ollama `/api/generate` response | Token generation time |
| `backend_acceptance_to_provider_start_ms` | CarbonMetricsRegistry | Carbon orchestration overhead |
| `provider_execution_to_first_answer_ms` | CarbonMetricsRegistry | Ollama prompt eval from Carbon's perspective |
| `backend_acceptance_to_completion_ms` | CarbonMetricsRegistry | Full E2E (backend side) |

---

## Protocol

### Phase 1 — FakeProvider Baseline (already measured)

FakeProvider benchmarks from Stage 5-A `make test` provide the Carbon orchestration overhead floor:

- Accept→ProviderStart P50 = 1.021 ms, P95 = 1.183 ms (N=50)
- Accept→Completion P50 = 1.744 ms, P95 = 1.914 ms (N=50)

These numbers require no live Ollama. They represent the irreducible minimum overhead Carbon adds.

### Phase 2 — Direct Ollama Baseline (N=20)

```bash
cd ~/Carbon
ollama serve &
sleep 3
ollama ps  # verify qwen3.5:9b available
.venv/bin/python benchmarks/inference/statistical_benchmark.py
```

This measures Ollama in isolation: `load_duration`, `prompt_eval_duration`, `eval_duration`, TTFT, generation speed. No Carbon involved.

### Phase 3 — Carbon E2E Live (N=20)

```bash
cd ~/Carbon
# Carbon server must be running (requires CARBON_API_TOKEN set)
.venv/bin/python benchmarks/inference/benchmark_e2e_pipeline.py
```

This measures the full Carbon stack: WebSocket connection, Bearer auth, intent classification, CMS lease, Ollama inference, streaming delivery.

### Phase 4 — Analysis

Compute:
- `carbon_overhead_ms = backend_acceptance_to_provider_start_ms` (Phase 3 P50/P95)
- `inference_ms = provider_execution_to_first_answer_ms` (Phase 3 P50/P95)
- `overhead_fraction = carbon_overhead_ms / (carbon_overhead_ms + inference_ms)`

Compare Phase 2 vs. Phase 3 TTFT to verify Carbon's transport overhead.

---

## Statistical Caveats

- N=20 is a **small sample**. P95 from N=20 observations has high uncertainty (the P95 estimate from 20 samples spans roughly [min of top 2, max] in the worst case).
- Results reflect a single Apple M5 machine; thermal state, background load, and memory pressure are not controlled.
- Ollama inference is stochastic; identical prompts produce different token sequences and lengths.
- Clock sources: Carbon metrics use `asyncio` monotonic time; Ollama reports its own internal durations. These are not synchronized.

---

## Expected Outputs

```
benchmarks/results/raw/exp-000-fakeprovider-baseline.json   (already in test results)
benchmarks/results/raw/exp-000-direct-ollama-N20.json
benchmarks/results/raw/exp-000-carbon-e2e-N20.json
benchmarks/results/summary/exp-000-analysis.md
```

---

## Safety Checklist

Before running:
- [ ] `data/carbon.db` SHA-256 verified against backup (must be `433d980d0f868248a744b30e741ae3cf01db239d5aa69ddcfc5c37fa6ac5fe5d`)
- [ ] `CARBON_API_TOKEN` set from `data/.carbon_token` (do NOT hardcode in scripts)
- [ ] No `--force` git operations during run
- [ ] Backup at `~/Carbon_Backups/Carbon_pre_showcase_20261128_0305/` confirmed present

---

## Links

- [Research question RQ-1](../../questions/latency-decomposition.md)
- [Direct Ollama benchmark script](../../../../benchmarks/inference/statistical_benchmark.py)
- [Carbon E2E benchmark script](../../../../benchmarks/inference/benchmark_e2e_pipeline.py)
- [FakeProvider benchmark tests](../../../../tests/performance/)
- [Metrics source](../../../../src/carbon/core/metrics.py)
