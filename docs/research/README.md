# Carbon Research Portfolio

Carbon is a **local-first AI assistant and orchestration platform** designed for Apple Silicon macOS. It is both a usable personal assistant and an engineering research platform for studying the design tradeoffs of running private, governed AI locally.

This research portfolio documents the open questions Carbon is designed to answer, the experiments conducted so far, and the threats to validity that bound the claims.

---

## Research Focus

Carbon sits at the intersection of three research areas:

1. **Local inference systems engineering** — Can a single-machine AI assistant achieve usable latency with strong privacy and governance guarantees?
2. **Orchestration overhead decomposition** — What fraction of end-to-end latency is Carbon logic vs. model inference, and where are the dominant costs?
3. **Governed AI affordances** — What does a practical permission/autonomy ladder look like for a local personal assistant, and what are the latency and UX tradeoffs?

---

## Research Questions

| ID | Question | Status |
|---|---|---|
| [RQ-1](questions/latency-decomposition.md) | How much of end-to-end latency is Carbon orchestration overhead vs. model inference? | **Active** (EXP-000 planned) |
| [RQ-2](questions/reasoning-depth.md) | Does routing to Instant/Standard/Deep actually change measurable model behavior? | **Planned** |
| [RQ-3](questions/model-residency.md) | What is the actual overhead of cold model loading vs. warm resident inference? | **Data available** (results_100_runs.json) |
| [RQ-4](questions/governance-tradeoffs.md) | What is the latency cost of governance authorization + audit logging? | **Partial data** (FakeProvider benchmarks) |
| [RQ-5](questions/retrieval-memory.md) | Does memory context injection improve response relevance, and at what latency cost? | **Planned** (requires live Ollama) |

---

## Experiments

| ID | Title | Status | Report |
|---|---|---|---|
| [EXP-000](experiments/EXP-000-latency-decomposition/plan.md) | Latency Decomposition: Orchestration vs. Inference | **Planned** | Plan only |

---

## Supporting Documentation

- [Experiment Log](experiment-log.md) — chronological record of all experiments
- [Threats to Validity](threats-to-validity.md) — honest assessment of measurement limitations
- [EXP-000 Plan](experiments/EXP-000-latency-decomposition/plan.md) — full methodology for latency decomposition

---

## Relationship to Carbon Development

The research questions directly shaped Carbon's architecture:

- **RQ-1** drove the `CarbonMetricsRegistry` latency span design (`backend_acceptance_to_provider_start_ms`, `provider_execution_to_first_answer_ms`, `backend_acceptance_to_completion_ms`).
- **RQ-3** drove the CMS warm-residency model and the 9216 MB static memory budget.
- **RQ-4** drove the governance engine's `asyncio`-native design (no blocking I/O on the hot path).
- **RQ-5** will drive the `IEmbeddingEngine` interface when implemented.
