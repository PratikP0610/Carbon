# Carbon — Implementation Status

> **Last updated:** 2026-11-28  
> **Branch:** `showcase/github-research-portfolio`  
> **Evidence standard:** All statuses are derived from source code, test coverage, and verified benchmark results. Planned or PRD-only features are marked **Planned**. Claims that cannot be directly verified are marked **Unverified**.

---

## Status Labels

| Label | Meaning |
|---|---|
| **Implemented** | Exists in source, exercised by tests with measurable coverage. |
| **Partial** | Core logic exists but wiring, production coverage, or integration is incomplete. |
| **In progress** | Active development; some code exists but not stable. |
| **Planned** | Architecturally designed; no production code exists yet. |
| **Deferred** | Explicitly closed for a later phase. |

---

## Backend — Python (`src/carbon/`)

| Component | Status | Evidence |
|---|---|---|
| FastAPI server (`main.py`, 1187 lines) | **Implemented** | 24 REST endpoints + `/ws/chat`; loopback-only `127.0.0.1:8765` |
| Bearer token authentication | **Implemented** | Constant-time `hmac.compare_digest`; atomic `rotate_token`; stale-socket revocation `4401` |
| WebSocket `/ws/chat` streaming | **Implemented** | Progressive token streaming, 10s auth timeout, 5-connection limit |
| `CarbonCore` orchestrator (`orchestrator.py`, 2324 lines) | **Implemented** | 86% test coverage; coordinates all subsystems |
| `IntelligenceEngine` intent classification | **Implemented** | 11-step heuristic/regex classifier (`engine.py`) |
| Adaptive reasoning depth (Instant/Standard/Deep) | **Implemented** | Config at `orchestrator.py:1022–1033`; `think` flag; `num_predict` 512/2560/4096 |
| `CarbonModelSupervisor` (CMS, `cms.py`, 2003 lines) | **Implemented** | 91% coverage; exclusive leases, drain guards, 9216 MB budget |
| `OllamaProvider` streaming inference | **Implemented** | `/api/generate` via `generate_stream_events`; `qwen3.5:9b` |
| CMS model allowlist enforcement | **Implemented** | `allowlist=('qwen3.5:9b',)`; `403` on unapproved switch |
| CMS lease drain guard (`409`) | **Implemented** | `asyncio.Lock` + `_drain_event`; `409` on model refresh under active lease |
| `SQLiteMemoryEngine` (`sqlite_memory.py`, 1641 lines) | **Implemented** | 87% coverage; WAL + FTS5; schema v3; 4-tier memory |
| Working / episodic / semantic / procedural tiers | **Implemented** | Schema verified; TTL expiration logic present |
| Memory retrieval + context injection | **Implemented** | `orchestrator.py:1562–1599` |
| Cancellation-safe SQLite transactions | **Implemented** | Stage 6.9-F; tested in `test_stage69_sqlite_cancellation_safety.py` |
| `GovernanceEngine` (`governance_engine.py`, 560 lines) | **Implemented** | P0–P4 permission ladder; A0–A3 autonomy levels |
| SHA-256 hash-chained audit log | **Implemented** | `prev_hash`/`entry_hash` chain; written to `data/audit.log` |
| `ToolRegistry` + 3 built-in macOS tools | **Partial** | Tools registered (`system_info` P1, `list_applications` P1, `open_application` P2); `get_schemas_for_llm()` not wired into LLM calls |
| `CarbonMetricsRegistry` (telemetry, `metrics.py`, 552 lines) | **Implemented** | NIST-method percentile histograms; latency decomposition spans |
| `DeviceManager` local host info | **Partial** | Local macOS host status via `sysctl hw.memsize` + `platform`; `_connected_nodes` empty |
| `FakeProvider` test stub | **Implemented** | `fake_provider.py`; 87% coverage; full streaming simulation |
| `IEmbeddingEngine` interface | **Planned** | Interface defined; no implementation |
| `IVisionEngine` interface | **Planned** | Interface defined; no implementation |
| Voice pipeline (`src/carbon/voice/`) | **Deferred** | Directory is empty; Stage 6.6 formally closed as deferred |

---

## macOS Client — Swift (`apps/macos/Carbon/Sources/`)

| Component | Status | Evidence |
|---|---|---|
| `CarbonApp.swift` — application lifecycle | **Implemented** | AppDelegate pattern; SwiftUI Scene setup |
| `CarbonClient.swift` — WebSocket transport | **Implemented** | Async/await WebSocket client; Bearer auth; reconnect logic |
| `ContentView.swift` — main chat UI | **Implemented** | SwiftUI message list; streaming update rendering |
| `MarkdownRenderer.swift` — Markdown/math display | **Implemented** | Attributed string rendering; KaTeX-style math |
| `MemoryPressureMonitor.swift` — Mach pressure | **Implemented** | `os_proc_available_memory()`; pressure level broadcast |
| `ProcessSupervisor.swift` — Ollama/backend lifecycle | **Implemented** | Bounded 5-phase shutdown with watchdog |
| `SingleInstanceLock.swift` — `flock` lock | **Implemented** | POSIX `flock` on `CARBON_LOCK_PATH` |
| Signed `.app` bundle | **Planned** | Currently launched as SwiftPM executable (`.build/debug/Carbon`) |
| macOS Keychain integration | **Deferred** | ADR-0007; currently POSIX `0600` file token |

---

## Testing

| Suite | Status | Evidence |
|---|---|---|
| Unit tests (Python, 255 total) | **Implemented** | `make test` → 255 passed, 2 deselected; 88% overall coverage |
| Integration tests (Python) | **Implemented** | API endpoint tests, E2E inference, model routing |
| Performance benchmarks (FakeProvider) | **Implemented** | `tests/performance/`; deterministic latency capture |
| Security tests | **Implemented** | `tests/security/` — auth, governance, memory API |
| Swift tests (`apps/macos/Carbon/Tests/`) | **Implemented** | `swift build` passes; test files present |
| CI/CD (GitHub Actions) | **Implemented** | `.github/workflows/ci.yml` |

---

## Verified Benchmark Results (Deterministic FakeProvider, `make test`)

> These results use `FakeProvider`, not live Ollama. They measure Carbon orchestration overhead, not model inference time. See [EXP-000 plan](research/experiments/EXP-000-latency-decomposition/plan.md) for live decomposition methodology.

| Metric | P50 | P95 | N | Evidence |
|---|---|---|---|---|
| Accept → ProviderStart (warm, orchestrator) | 1.021 ms | 1.183 ms | 50 | `test_orchestration_benchmarks.py` Stage 5-A |
| Accept → Completion (warm, orchestrator) | 1.744 ms | 1.914 ms | 50 | `test_orchestration_benchmarks.py` Stage 5-A |
| WS E2E client → first-answer (FakeProvider) | 1.567 ms | 1.619 ms | 30 | `test_orchestration_benchmarks.py` Stage 5-A |
| SQLite 1k-row point retrieve | 0.113 ms | — | 50 | Stage 5-A SQLite baseline |
| SQLite 1k-row FTS5 search | 0.222 ms | — | 50 | Stage 5-A SQLite baseline |
| Stream teardown + lease release (E3) | — | ≤25 ms | 30 | Stage 5-E `test_stage5e_streaming_fidelity.swift` |

---

## Verified Benchmark Results (Live Ollama, N=100, `qwen3.5:9b`)

> Source: `benchmarks/inference/results_100_runs.json` (Oct 27 2026, pre-Stage-5, default deep reasoning mode).

| Metric | P50 | P95 | Min | Max |
|---|---|---|---|---|
| First reasoning token (TTFT, warm) | 589 ms | 884 ms | 301 ms | 69,448 ms |
| First response token (warm, N=10) | 5,992 ms | 13,911 ms | 4,457 ms | 17,081 ms |
| Generation speed | 15.1 tok/s | 22.1 tok/s | 12.9 tok/s | 22.3 tok/s |
| Total end-to-end latency | 11.65 s | 17.81 s | 5.69 s | 77.51 s |
| Cold start model load | 1.34 s | — | — | — |
| Short prompt E2E P50 | 8.5 s | — | — | — |
| Long prompt E2E P50 | 17.3 s | — | — | — |

> **Note:** Total latency is dominated by model inference (qwen3.5:9b, Q4_K_M, 16GB). Carbon orchestration overhead measured separately via FakeProvider is P95 < 2 ms.

---

## Planned Features (Not Yet Implemented)

| Feature | Status |
|---|---|
| Android companion node pairing | Planned |
| Local embeddings (`IEmbeddingEngine`) | Planned |
| Vision engine (`IVisionEngine`) | Planned |
| Signed `.app` distribution | Planned |
| Voice pipeline (STT/TTS) | Deferred |
| macOS Keychain credential storage | Deferred |
| Multi-model production runtime (> 1 allowlisted model) | Planned |

---

## Security Posture Summary

> See [threat model](security/threat-model.md), [permissions](security/permissions.md), [API security review](security/api-security-code-review.md), and [privacy policy](security/privacy.md) for full detail.

| Property | Implementation | Status |
|---|---|---|
| Loopback-only binding | `HOST = "127.0.0.1"`, `PORT = 8765` | **Implemented** |
| Bearer token auth | Constant-time compare; atomic rotation | **Implemented** |
| Origin validation | `Origin: null` blocked; loopback-only expected | **Implemented** |
| Stale socket revocation | Custom close code `4401` | **Implemented** |
| Hash-chained audit log | SHA-256 `prev_hash`/`entry_hash` | **Implemented** |
| No secrets in source | Verified by security scan | **Implemented** |
| Signed `.app` / Keychain | Not implemented | **Deferred** |

---

*Status document maintained for the `showcase/github-research-portfolio` branch. All claims are traceable to source code, test evidence, or verified benchmark files cited above.*
