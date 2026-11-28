# RQ-2: Reasoning Depth — Does Routing Change Measurable Model Behavior?

**Status:** Planned  
**Priority:** Medium  
**Dependency:** Live Ollama required; `qwen3.5:9b` with `think` flag support.

---

## Research Question

> **Does routing to Instant/Standard/Deep reasoning depth actually change measurable model behavior?**

Carbon classifies every user intent and routes to one of three reasoning modes:

| Mode | `think` flag | `num_predict` | `temperature` |
|---|---|---|---|
| Instant | `False` | 512 | 0.2 |
| Standard | `False` | 2560 | 0.6 |
| Deep | `True` | 4096 | 0.7 |

The question is whether these differences produce measurably different output quality and latency characteristics, or whether the model behaves similarly regardless.

---

## Motivation

If routing makes no measurable difference, the complexity of the 11-step intent classifier is not justified. If routing does produce measurable differences, it validates Carbon's adaptive reasoning architecture as a principled design choice.

---

## Variables

- **Independent:** Reasoning depth (Instant / Standard / Deep)
- **Dependent (latency):** TTFT, total generation time, token count
- **Dependent (quality):** Factual accuracy on known-answer prompts; presence of reasoning trace tokens in Deep mode

---

## Existing Evidence

Stage 1-10 benchmark (`docs/reports/stage1_10-performance-benchmark-results.json`):
- Instant cold start: TTFT 4.451 s (cold), 0.113 s (warm), total 0.485 s (warm)
- Deep mode implies `think=True` → reasoning trace tokens precede visible answer

---

## Planned Experiment Design

1. Select 10 prompts per category (factual, analytical, planning).
2. Run each prompt 5 times under each reasoning depth (N=150 total).
3. Record: TTFT, total tokens, thinking tokens, visible tokens, generation speed.
4. Score factual prompts against expected answers.
5. Report whether Deep mode produces longer reasoning traces and whether this correlates with answer quality.

---

## Threats to Validity

- N=5 per cell is very small; variance will be high.
- qwen3.5:9b's `think` flag behavior depends on prompt phrasing.
- Quality scoring is subjective without a ground-truth dataset.

---

## Links

- [Reasoning depth config](../../../src/carbon/core/orchestrator.py#L1022-L1033)
- [Intelligence engine](../../../src/carbon/intelligence/engine.py)
- [Experiment Log](../experiment-log.md)
