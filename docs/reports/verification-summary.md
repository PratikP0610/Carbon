# Carbon — Master Verification & Documentation Alignment Summary (Stages 5–7)

**Scope:** Consolidated verification record for **Stage 5 (5-A through 5-E)**, **Stage 6 (6.1 through 6.9-F)**, and **Stage 7 (7.1 through 7.6)** on branch `backup/carbon-tested-phase1-2026-10-29` (`0c7d1d17c55c836715438c7d0c00887cf9aaf19c`).

**Baseline Configuration & Data Invariants:**
- Primary & sole approved model: `qwen3.5:9b` (`DEFAULT_PRIMARY_MODEL = 'qwen3.5:9b'`, `DEFAULT_APPROVED_MODELS = ('qwen3.5:9b',)`)
- Static model memory budget: `9216 MB` (`DEFAULT_MODEL_MEMORY_BUDGET_MB = 9216`)
- Production `data/carbon.db`: Preserved untouched at schema **v2** (`PRAGMA user_version = 2`, SHA-256 `433d980d0f868248a744b30e741ae3cf01db239d5aa69ddcfc5c37fa6ac5fe5d`); isolated Stage 5-C/6 verification databases exercise schema **v3**.

---

## Status Classification Key

- **IMPLEMENTED:** Verified in source code and covered by accepted checks.
- **PARTIALLY IMPLEMENTED:** Implemented in part or constrained by scope/configuration boundaries (e.g. single-model production allowlist).
- **DEFERRED:** Intentionally postponed and not present in the current codebase (e.g. Voice STT/TTS in Stage 6.6).
- **PLANNED:** Design-stage specification not implemented in code (e.g. `asyncio.PriorityQueue`, `CHECKPOINTED` state, macOS Keychain storage).
- **UNVERIFIED:** Not yet executed or proven against post-Stage 6.9-F runtime artifacts (e.g. Tier 2 live M5 latency benchmarks, Tier 3 endurance/real-pressure soak tests).
- **ACCEPTED:** Stage formally completed and accepted by the user.

---

## 1. Stage 5 — Performance, Reliability & Benchmarking (`5-A` to `5-E`)

| Stage | Title | Acceptance Status | Key Test Results | Primary Artifacts & Code Links |
| :--- | :--- | :---: | :--- | :--- |
| **5-A** | Benchmark Foundation & Monotonic Telemetry | **ACCEPTED** | `make test`: `223 passed, 2 deselected` (`87%` cov); `benchmark.sh`: `4 passed`; `make lint`: passed (`74` files, `27` mypy) | [metrics.py](../../src/carbon/core/metrics.py), [test_telemetry_metrics_5_a.py](../../tests/unit/test_telemetry_metrics_5_a.py), [test_orchestration_benchmarks.py](../../tests/performance/test_orchestration_benchmarks.py), [test_memory_benchmarks.py](../../tests/performance/test_memory_benchmarks.py) |
| **5-B** | Transport Pooling & Hot-Path Optimization | **ACCEPTED** | `make test`: `232 passed, 2 deselected` (`89%` cov); `benchmark.sh`: `4 passed`; `make lint`: passed (`75` files, `27` mypy) | [ollama_provider.py](../../src/carbon/model_control/ollama_provider.py), [cms.py](../../src/carbon/model_control/cms.py), [test_stage5b_performance.py](../../tests/unit/test_stage5b_performance.py) |
| **5-C** | Reliability Engineering (C1–C6) | **ACCEPTED** | `test_stage5c_reliability.py`: `10 passed`; `make test`: `242 passed, 2 deselected` (`88%` cov); `benchmark.sh`: `4 passed` | [sqlite_memory.py](../../src/carbon/memory/sqlite_memory.py), [orchestrator.py](../../src/carbon/core/orchestrator.py), [governance_engine.py](../../src/carbon/governance/governance_engine.py), [test_stage5c_reliability.py](../../tests/unit/test_stage5c_reliability.py) |
| **5-D** | Mach Memory Pressure, Ollama Watchdog & SwiftUI Streaming (D1–D4) | **ACCEPTED** | `test_stage5d_pressure_and_watchdog.py`: `4 passed`; Swift `test_stage5d_ui_and_pressure.swift`: passed; `make test`: `246 passed, 2 deselected` (`88%` cov) | [MemoryPressureMonitor.swift](../../apps/macos/Carbon/Sources/MemoryPressureMonitor.swift), [CarbonClient.swift](../../apps/macos/Carbon/Sources/CarbonClient.swift), [MarkdownRenderer.swift](../../apps/macos/Carbon/Sources/MarkdownRenderer.swift), [test_stage5d_pressure_and_watchdog.py](../../tests/unit/test_stage5d_pressure_and_watchdog.py), [test_stage5d_ui_and_pressure.swift](../../apps/macos/Carbon/Tests/test_stage5d_ui_and_pressure.swift) |
| **5-E** | Tier 1 Deterministic CI Benchmarks & Gates (E1–E5) | **ACCEPTED** | Gates `E1`–`E5` passed; `make test`: `248 passed, 2 deselected` (`88%` cov); `benchmark.sh`: `6 passed`; Swift `test_stage5e_streaming_fidelity.swift`, `test_stage5d_ui_and_pressure.swift`, `test_markdown.swift` passed | [test_stage5e_tier1_gates.py](../../tests/performance/test_stage5e_tier1_gates.py), [test_stage5e_streaming_fidelity.swift](../../apps/macos/Carbon/Tests/test_stage5e_streaming_fidelity.swift), [end-to-end-latency-report.md](../benchmarks/end-to-end-latency-report.md) |

### Stage 5 Scope, Limitations & Unverified Work

- **Stage 5-A (`IMPLEMENTED` / `ACCEPTED`):** Replaced cross-clock timestamp comparisons with `time.perf_counter()` backend telemetry (`TaskLatencySample`, `CarbonMetricsRegistry` bounded `maxlen=500`, zero-PII `GET /api/v1/metrics`) and created isolated `FakeProvider`/temp-SQLite performance suites (`scripts/benchmark.sh`). Live Ollama M5 baseline capture remained deferred out of 5-A.
- **Stage 5-B (`IMPLEMENTED` / `ACCEPTED`):** Added pooled `httpx.AsyncClient` keepalive reuse in `OllamaProvider`, singleflight readiness coalescing with `offline_recovery_cooldown_seconds=2.0` in CMS, TTL hardware telemetry caching (`30.0s`) in `DeviceManager`, precompiled `IntelligenceEngine` regexes, and removed redundant snapshot writes/checks.
- **Stage 5-C (`IMPLEMENTED` / `ACCEPTED`):** Added `contextlib.aclosing` async generator guards (`C1`), bounded task registry & degraded-shutdown resource preservation (`C2`), CMS `(kind, generation)` pending ownership tokens so `unload_model` runs outside `_inference_lock` (`C3`), serialized `aiosqlite` transactions (`C4`), SQLite integrity verification + forensic quarantine + online backup + schema **v3** migration (`C5`), and O(1) audit-log tail recovery with periodic TTL purge/WAL checkpoints (`C6`). Production `data/carbon.db` remained intentionally at v2.
- **Stage 5-D (`IMPLEMENTED` / `ACCEPTED`):** Wired `DispatchSource.makeMemoryPressureSource` in `MemoryPressureMonitor.swift` (with `0.5s` escalation debounce and `10.0s` recovery hysteresis), CMS deferred pressure unloading on lease release, single-instance bounded Ollama recovery watchdog (`2.0, 4.0, 8.0, 16.0, 30.0s`), and ~40 ms SwiftUI token coalescing (`MarkdownCoordinator`).
- **Stage 5-E (`IMPLEMENTED` / `ACCEPTED` for Tier 1):** Enforced deterministic Tier 1 CI gates (`E1` orchestration dispatch `P95 <= 50 ms`, `E2` SQLite `recent_turns_ms` `P95 <= 20 ms`, `E3` mid-stream cancel teardown `<= 25 ms` — cut from `52.519 ms` to `3.256 ms` P95, `E4` `220`-op chaos/audit verification with `0` deadlocks/leaks, `E5` SwiftUI `24.25` updates/sec & `10.19` parses/sec). **Tier 2** (post-Stage 6.9-F live Ollama M5 benchmarks) and **Tier 3** (sustained soak & real OS pressure tests) remain **UNVERIFIED**. See [methodology.md](../benchmarks/methodology.md) and [end-to-end-latency-report.md](../benchmarks/end-to-end-latency-report.md).

---

## 2. Stage 6 — Controlled End-to-End Runtime & Regression Verification (`6.1` to `6.9-F`)

All Stage 6 live runs executed against an isolated temporary data directory (`/tmp/carbon_stage6_isolated`, schema v3) using real local `qwen3.5:9b` via Ollama and the debug SwiftPM binary, leaving `data/carbon.db` untouched at v2.

| Stage | Title | Acceptance Status | Summary Result | Key Issues & Deferred / Limited Items |
| :--- | :--- | :---: | :--- | :--- |
| **6.1** | Preflight & Isolation Audit | **ACCEPTED** | **4 / 4 PASS** (`T6.1-1`..`T6.1-4`) | Identified that unconfigured Finder launch defaults to `data/` unless `CARBON_*` env vars isolate the runtime. |
| **6.2** | Application Lifecycle | **ACCEPTED** | **5 / 5 PASS** (`T6.2-1`..`T6.2-5`) | Recorded pre-existing `@MainActor` dispatch warning in `MemoryPressureMonitor.swift:201` and circular import caveat for standalone `governance_engine` import. |
| **6.3** | Chat, Streaming & Targeted UI Verification | **ACCEPTED** | **7 / 7 PASS** (`T6.3-1`..`T6.3-7`) + Targeted UI **Test A & B PASS** | Verified live token streaming (`20.2`–`31.6 tok/s`), LaTeX/Markdown rendering, `34.6 ms` UI Stop cancellation; recorded small `10x10` Stop button hit target (Accessibility label `"Cancel task"` verified in targeted UI pass). |
| **6.4** | Model Management & Pressure Handling | **ACCEPTED** | **6 / 6 PASS** (`T6.4-1`..`T6.4-6`) | Verified `qwen3.5:9b` budget/allowlist (`403` unapproved rejection, `409` active-lease refresh block, Level 2/4 memory pressure deferral & eviction, Ollama watchdog recovery leaving `/api/v1/health` `"degraded"` with `allow_prewarm=False` until next turn). |
| **6.5** | Persistent Memory & Forgetting | **ACCEPTED** | **6 / 6 PASS** (`T6.5-1`..`T6.5-6`) | Verified semantic/episodic v3 persistence across restarts and isolated conversation history; recorded lack of native macOS multi-record forget confirmation button (requires `POST /api/v1/memory/forget` `confirm=true`). |
| **6.6** | Voice Latency & Accuracy | **ACCEPTED (as DEFERRED)** | **0 PASS / 0 FAIL / 6 BLOCKED** (`T6.6-1`..`T6.6-6`) | **DEFERRED:** `src/carbon/voice/` is empty (`0` files), no STT/TTS dependencies exist, and voice protocols remain reserved (`protocols.py`). |
| **6.7** | Failure Handling & Recovery | **ACCEPTED** | **7 / 7 PASS** (`T6.7-1`..`T6.7-7`) | Verified mid-stream `SIGKILL` Ollama recovery (`13.3 ms` WS error notice, `263.9 ms` post-restart recovery), backend disconnect handling, SQLite insert fault trigger, tampered audit chain detection, and `120.8 ms` active-stream shutdown; noted unhandled `500` error formatting on `POST /api/v1/memory` fault. |
| **6.8** | Security & Governance | **ACCEPTED** | **7 / 7 PASS** (`T6.8-1`..`T6.8-7`) | Verified `401`/`4401` auth enforcement, strict `Origin` checks, tool whitelist/schema validation, single-use argument-digest approval binding, prompt-injection resistance, and valid `audit.log` hash chain (`204` entries); noted rejected `401`/pre-auth WS attempts are not logged to `audit.log`. |
| **6.9** | Final Regression Sweep (`R1`–`R8`) | **ACCEPTED** (with `6.9-F` fix) | **7 / 7 Active PASS** (`R1`–`R5`, `R7`, `R8`); `R6` **DEFERRED** | Executed real isolated `qwen3.5:9b` regression (`49` WS token frames, `TTFT 3.262s`, `322`-entry valid audit chain). Earlier intermediate run (`task-9679`) surfaced an intermittent nested SQLite transaction error on mid-write WS cancellation. |
| **6.9-F** | SQLite Cancellation Safety Fix & Follow-up | **ACCEPTED** | **7 / 7** new safety tests PASS; **43 / 43** focused PASS; **`make test`: `255 passed, 2 deselected` (`88%` cov)**; Swift: `test_process_supervisor.swift` `36/36`, `test_stage5d_ui_and_pressure.swift` `5/5`, `test_shutdown.swift` `18/18` (+ intermittent `[19/19]`) | Fixed `_write_transaction()` (`sqlite_memory.py`), `_cleanup_connection_task()` (`main.py`), and post-stream episodic storage isolation (`orchestrator.py`). Confirmed `test_shutdown.swift:1287` Test 19 (`[19/19]`) is a pre-existing timing-sensitive harness race (`0.25s–0.26s` vs `0.9s` dummy sleep; 2/3 pass in repeat runs), unrelated to `6.9-F`. Zero regressions; `pyright` not installed in `.venv`. |

---

## 3. Stage 7 — Documentation Alignment & Quality Assurance (`7.1` to `7.6`)

| Stage | Title | Acceptance Status | Deliverables & Verified Outcomes | Links |
| :--- | :--- | :---: | :--- | :--- |
| **7.1** | Repository Documentation Audit | **ACCEPTED** | Audited `72` files (`README.md`, `docs/`, config/CI) and identified code-to-doc drift across model allowlists, DB schema v2/v3, API/WS contracts, security headers, and Stage 5–6 verification gaps. | *(Audit findings executed in Stages 7.2–7.6)* |
| **7.2** | Core Project Entry & Setup Alignment | **ACCEPTED** | Aligned project overview, model defaults (`qwen3.5:9b`, `9216 MB`), SwiftPM/Python setup, `255 passed / 2 deselected` test baseline, and contribution workflow (`46/46` links verified). | [README.md](../../README.md), [setup.md](../development/setup.md), [testing.md](../development/testing.md), [contributing.md](../development/contributing.md) |
| **7.3** | Architecture, Database & Component Contract Alignment | **ACCEPTED** | Aligned 5 core architecture specs to live source and isolated schema **v3** (separating production v2 DB), replacing legacy protocol/table drift (`21/21` links verified). | [overview.md](../architecture/overview.md), [system-design.md](../architecture/system-design.md), [data-flow.md](../architecture/data-flow.md), [database-schema.md](../architecture/database-schema.md), [component-contracts.md](../architecture/component-contracts.md) |
| **7.4** | API & WebSocket Reference Creation | **ACCEPTED** | Created exhaustive reference verified against `main.py` (**24 REST routes**, `/ws/chat` **15 server events** & **5 client actions**); documented `stream=true` and 3 REST routes lacking direct REST test assertions (`23/23` links verified). | [api-reference.md](../api-reference.md) |
| **7.5** | Security, Privacy & Benchmark Accuracy | **ACCEPTED** | Aligned 6 security/privacy docs (constant-time auth, immediate WS `4401` rotation revocation, `Origin: null` `403`/`1008` rejection, `P0`–`P4`/`A0`–`A3` & SHA-256 chain, TTLs/WAL/audit retention) and 3 benchmark docs (Tier 1 gates vs historical Oct 28 live baselines vs `UNVERIFIED` Tier 2/3). Corrected Markdown parsing rate link to `test_stage5e_streaming_fidelity.swift`. | [threat-model.md](../security/threat-model.md), [permissions.md](../security/permissions.md), [permission-matrix.md](../security/permission-matrix.md), [privacy.md](../security/privacy.md), [api-security-code-review.md](../security/api-security-code-review.md), [api-security-verification-report.md](../security/api-security-verification-report.md), [methodology.md](../benchmarks/methodology.md), [inference-benchmark-report.md](../benchmarks/inference-benchmark-report.md), [end-to-end-latency-report.md](../benchmarks/end-to-end-latency-report.md) |
| **7.6** | Verification Summaries & Cross-Link QA | **SUBMITTED FOR APPROVAL** | Created this master summary, converted local `file://` links in active docs to portable repo-relative links, added alignment notices to legacy Oct 28 architecture docs, aligned `README.md` navigation & CMS lock terminology, and verified all links & diffs cleanly. | [verification-summary.md](verification-summary.md) |

---

## 4. Known Consolidated Limitations & Historical Documentation Scope

1. **Production DB vs. Schema v3:** `data/carbon.db` remains intentionally at schema **v2**; schema **v3** (`tasks`, `conversations`, `schema_migrations`, indexes) has only been verified against isolated test/Stage 6 databases.
2. **Deferred & Planned Scope Not Present in Code:** Voice STT/TTS ([protocols.py](../../src/carbon/interfaces/protocols.py)), macOS Keychain secret storage ([keychain-implementation-status.md](../architecture/keychain-implementation-status.md) — `PLANNED`), multi-step autonomous execution (`A3` task planner), signed `.app` bundling, and production multi-model routing beyond `('qwen3.5:9b',)` remain unimplemented/deferred.
3. **UI & Test Harness Limitations:** Pre-existing intermittent Test 19 timing race in [test_shutdown.swift](../../apps/macos/Carbon/Tests/test_shutdown.swift#L1287); `MemoryPressureMonitor.swift:201` `@MainActor` dispatch warning under Stage 6 manual tests; lack of UI interactive confirmation card for multi-record chat `"Forget ..."` commands (API confirmation works); `pyright` not installed in `.venv`; 3 REST routes lacking direct REST test assertions (documented in [api-reference.md](../api-reference.md)); unverified live Tier 2 / sustained Tier 3 benchmarks.
4. **Historical Pre-Stage-5 Reports Preserved:** Earlier baseline documents under [docs/reports/](.) (including Oct 28 / Nov 1–26 reports such as `carbon-1.0.0-master-completion-audit.md`, `stage1_*`, `phase1*`, and `stage1_6-desktop-freeze-investigation.md`) reflect pre-Stage-5 codebase snapshots and may reference local machine diagnostic paths (`file:///Library/Logs/...` or scratch logs); refer to Stages 7.2–7.6 documents and this summary for the authoritative current state.
