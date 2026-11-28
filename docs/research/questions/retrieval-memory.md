# RQ-5: Retrieval Memory — Context Injection Quality and Latency

**Status:** Planned  
**Priority:** Medium  
**Dependency:** Live Ollama required; populated `data/carbon.db` with realistic conversations.

---

## Research Question

> **Does SQLite memory context injection improve response relevance, and at what latency cost?**

---

## Motivation

Carbon's `SQLiteMemoryEngine` retrieves relevant past conversations and injects them into the prompt before each inference call. This adds:
1. A SQLite query latency cost.
2. Additional prompt tokens (affecting Ollama prompt evaluation time).
3. Potential relevance improvement if retrieved context is semantically useful.

Whether the quality benefit justifies the latency cost is an open empirical question.

---

## Memory Architecture

- **Working memory:** Most recent conversation turns (always injected).
- **Episodic memory:** Past conversations retrieved by FTS5 keyword search.
- **Semantic memory:** Summary-level concepts (retrieved by relevance).
- **Procedural memory:** User-approved behavioral patterns.

The retrieval call at `orchestrator.py:1562–1599` runs before each inference.

---

## Planned Experiment Design

1. Seed `data/carbon.db` with N=50 synthetic conversations on 5 topics.
2. Run 20 test prompts (4 per topic) with memory retrieval enabled vs. disabled.
3. Record: SQLite retrieval latency, prompt token count delta, TTFT delta, total latency delta.
4. Rate response relevance manually (3-point scale: on-topic / neutral / off-topic).

---

## Threats to Validity

- Synthetic seed conversations may not reflect realistic memory content.
- Manual relevance scoring is subjective and not reproducible.
- N=20 is insufficient for statistical inference.
- FTS5 keyword search may retrieve irrelevant passages that add noise rather than signal.

---

## Limitation

`IEmbeddingEngine` (semantic vector search) is not yet implemented. This experiment is limited to FTS5 keyword retrieval, which is a weaker signal than embedding-based similarity.

---

## Links

- [SQLiteMemoryEngine source](../../../src/carbon/memory/sqlite_memory.py)
- [Memory retrieval in orchestrator](../../../src/carbon/core/orchestrator.py#L1562-L1599)
- [Experiment Log](../experiment-log.md)
