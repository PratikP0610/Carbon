# ADR-0002: Model Control Plane (CMS) as Sole Model Authority

## Status
Approved

## Context
In agentic AI systems, LLM workers frequently attempt to invoke tools, change execution parameters, or alter model configurations directly from generation output. If models possess self-sovereignty or direct system control, security boundaries collapse, leading to prompt injection exploits or runaway resource consumption.

## Decision
We designate the **Carbon Model Supervisor (CMS)** as the single, unbypassable authority over model selection, model loading/unloading, model health monitoring, and inference lease allocation.

1. **Worker Role Only:** All LLMs (including the primary model Qwen 3.5 9B) operate strictly as ephemeral workers.
2. **Zero Inherent Authority:** A model cannot change its own allocation, load alternative models, register new models, or trigger tool execution directly.
3. **Structured Request Routing:** An LLM can only suggest tool calls or structured plans. Carbon Core routes these through Governance, which grants or denies authorization independently of model desire.

## Consequences
### Positive:
- **Bulletproof security perimeter:** Prompt injections cannot hijack system permissions or reconfigure model routing.
- **Predictable lifecycle:** CMS maintains ground truth on what models are active, healthy, and consuming unified memory.

### Negative / Trade-offs:
- Additional validation and routing layer introduces minor orchestration latency (< 10 ms), well within the PRD Section 7 target of <= 50 ms.
