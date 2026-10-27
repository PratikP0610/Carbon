# ADR-0003: Single Active Chat LLM Concurrency

## Status
Approved

## Context
Carbon 1.0.0 targets Apple Silicon Macs with 16 GB of unified memory. The primary model, `qwen3.5:9b` (Q4_K_M GGUF), consumes ~6.55 GB on disk and ~6.2 GB of unified memory when resident in GPU/Metal memory buffers, alongside context cache.
If multiple chat LLMs run concurrently, memory pressure escalates dramatically, risking macOS memory compression, heavy SSD swap thrashing, or application termination via Out-Of-Memory (OOM) killer.

## Decision
Carbon enforces **Single Active Chat LLM Concurrency**.

1. At any given point in time, exactly one chat LLM may hold an active inference lease.
2. An asynchronous mutual exclusion lease (`asyncio.Lock`) inside CMS gates all chat LLM inference calls.
3. If an incoming task requires a different model, CMS unloads the previous model weights prior to pre-warming and loading the new model.
4. Non-LLM peripheral workloads (lightweight Whisper STT, Kokoro TTS, small embedding models) operate under independent resource caps and do not hold the chat LLM lock, but are tracked in the global resource budget.

## Consequences
### Positive:
- Guaranteed memory safety and system responsiveness on 16 GB unified memory hardware.
- Elimination of OOM crashes caused by concurrent model spawns.

### Negative / Trade-offs:
- Tasks requiring distinct chat models must execute sequentially rather than concurrently. Model switching time (~2–5s cold load) is incurred when switching models.
