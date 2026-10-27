# ADR-0006: Dynamic Unified Memory Management and Mach Pressure Governance

## Status
Approved

## Context
Initial design iterations proposed a static "9.0 GB Model Memory Budget" for the 16 GB Apple Silicon MacBook Air M5.
However, empirical analysis and host operating system mechanics demonstrate that a fixed 9 GB model budget is flawed for local macOS environments:
1. **Unified Memory Dynamic Sharing:** Apple Silicon shares the 16 GB unified memory pool dynamically across CPU, Metal GPU, and Apple Neural Engine (ANE).
2. **Variable Background Workloads:** User applications (Safari with multiple tabs, IDEs, Slack, Spotify, system processes) fluctuate in their resident memory demands (often consuming 6 to 10 GB). A static 9 GB allocation could trigger swap compression or OOM crashes if user apps demand more RAM.
3. **Disambiguation of Memory Metrics:**
   - *Disk Footprint:* Raw model weight file size on the APFS volume (e.g., `qwen3.5:9b` Q4_K_M is 6.55 GB).
   - *Resident VRAM Allocation:* Physical memory pages mapped into Metal GPU buffers for model tensors and KV cache (e.g., ~6.10 GB base + ~0.5–1.5 GB context).
   - *Host Memory Pressure:* Mach kernel virtual memory pressure level (`kern.memorystatus_vm_pressure_level`), representing true memory health including compression and swap activity.

## Decision
We discard the rigid static 9 GB budget in favor of **Dynamic Unified Memory Management with Mach Kernel Memory Pressure Governance**:

1. **Configurable Thresholds:** Memory limits are defined dynamically via configurable settings rather than hardcoded constants:
   - `MEMORY_PRESSURE_CHECK_INTERVAL_SEC` (default: 5.0s).
   - `MODEL_CONTEXT_MAX_TOKENS_NORMAL` (default: 32,768).
   - `MODEL_CONTEXT_MAX_TOKENS_CONSTRAINED` (default: 8,192).
2. **Mach Kernel Pressure Levels Reaction Ladder:**
   - **Level 1 (Normal):** Primary chat model (`qwen3.5:9b`) is kept warm in GPU unified memory (`keep_alive: 5m` or persistent). Deep reasoning is permitted up to 32K context.
   - **Level 2 (Warning):** CMS immediately unloads all idle specialist models (embeddings, TTS). Forces `keep_alive: 0` for non-interactive tasks. Temporarily caps context window to 8,192 tokens. Drops generation reasoning depth from `deep` to `standard`.
   - **Level 4 (Critical):** CMS unloads the chat model immediately upon task completion (`keep_alive: 0`). Rejects background memory-intensive tasks. Emits a notification to the macOS UI informing the user of host memory saturation.
3. **Specialist Workload Isolation:** Non-LLM specialist models (Whisper.cpp STT, Kokoro TTS, BGE Embeddings) operate under strictly bounded budgets (≤ 500 MB each) and run on Apple Neural Engine (ANE) or CPU cores to avoid GPU Metal buffer contention with the primary Chat LLM.

## Consequences
### Positive:
- **Rock-solid system stability:** Carbon dynamically adapts to whatever normal macOS applications the user has running without causing macOS UI stutter, swap thrashing, or OOM crashes.
- **Maximized performance when idle:** When the host has ample free RAM, Carbon utilizes larger context windows and pre-warmed weights for sub-second TTFT.
- **Accurate telemetry:** Disambiguates disk footprint vs. Metal resident allocation vs. Mach kernel pressure in all logs and UI metrics.

### Negative / Trade-offs:
- Requires a background memory watchdog task polling `kern.memorystatus_vm_pressure_level` every 5 seconds. Polling overhead is negligible (< 0.01% CPU).
