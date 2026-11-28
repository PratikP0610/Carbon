# RQ-4: Governance Tradeoffs — Latency Cost of Authorization + Audit Logging

**Status:** Partial data (FakeProvider benchmarks)  
**Priority:** Medium

---

## Research Question

> **What is the latency cost of governance authorization + audit logging on Carbon's hot inference path?**

---

## Motivation

Every inference request passes through `GovernanceEngine` for permission evaluation and appends an entry to the hash-chained `audit.log`. The question is whether this adds measurable overhead relative to orchestration and inference.

---

## Architecture

The governance path on each request:

1. `GovernanceEngine.evaluate_request()` — permission level check, autonomy policy evaluation.
2. `GovernanceEngine.log_event()` — SHA-256 hash computation, `audit.log` append (synchronous file I/O).

The hash chain requires the previous entry's hash to be read on each write, making `log_event` a sequential operation that cannot be parallelized.

---

## Existing Evidence

From Stage 5-A FakeProvider benchmarks (N=50):

- `backend_acceptance_to_provider_start_ms` P50 = 1.021 ms, P95 = 1.183 ms
- This span includes governance evaluation AND the full CMS lease path.

No isolated governance-only measurement exists yet. The contribution of governance to the 1.021 ms budget is currently unquantified.

---

## Planned Experiment Design

1. Add a `governance_evaluation_ms` span to `CarbonMetricsRegistry`.
2. Run N=50 FakeProvider requests.
3. Compare `governance_evaluation_ms` vs. `backend_acceptance_to_provider_start_ms`.
4. Estimate audit log I/O latency by measuring `audit.log` file size growth and disk flush timing.

---

## Hypotheses

- **H1:** Governance evaluation (in-memory permission check) adds < 0.1 ms per request.
- **H2:** Audit log SHA-256 computation adds < 0.5 ms per request.
- **H3:** Total governance overhead < 1 ms P95 on the warm path.

---

## Links

- [GovernanceEngine source](../../../src/carbon/governance/governance_engine.py)
- [Metrics source](../../../src/carbon/core/metrics.py)
- [FakeProvider benchmarks](../../../tests/performance/)
- [Experiment Log](../experiment-log.md)
