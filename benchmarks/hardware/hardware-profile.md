# Carbon Benchmark Hardware Profile

All Carbon benchmarks and experiments were conducted on this hardware configuration unless otherwise noted.

## Host

| Property | Value |
|---|---|
| Device | Apple MacBook Air (M5) |
| SoC | Apple M5 (10-core: performance + efficiency) |
| Unified Memory | 16 GB |
| Storage | 460 GB NVMe (192 GB free at time of benchmarks) |
| OS | macOS 27.2.1 |
| Python | 3.12 |
| Ollama | 0.33.0 |
| Primary Model | qwen3.5:9b Q4_K_M |

## Thermal State

No thermal throttling warnings were observed during benchmarks. Battery was at 100% charge. Background processes were not explicitly controlled.

## Model

| Property | Value |
|---|---|
| Model | qwen3.5:9b |
| Quantization | Q4_K_M (inferred from manifest) |
| Context length | 4096 (Instant/Standard) / 4096 (Deep) |
| `think` flag | Supported; used for Deep mode |
| Ollama endpoint | `http://127.0.0.1:11434/api/generate` |

## Warm-Residency Budget

CMS static memory budget: **9216 MB** (`DEFAULT_MODEL_MEMORY_BUDGET_MB`).  
Mach memory-pressure context scaling: 32768 / 8192 / 4096 tokens (normal / warning / critical).

## Notes

- All loopback measurements use the same-clock rule (single host, `asyncio` monotonic time).
- Ollama internal durations (`load_duration`, `prompt_eval_duration`, `eval_duration`) use Ollama's own clock.
- Cross-clock comparisons are directionally valid but not precisely additive.
