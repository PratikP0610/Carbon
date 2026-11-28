# Contributing to Carbon

Welcome to Carbon. This guide summarizes the repository layout, daily development checks, and documentation accuracy standards.

---

## 1. Repository Layout

- **`src/carbon/` (Python 3.12+ Backend):**
  - `main.py` — Loopback FastAPI REST endpoints and `/ws/chat` WebSocket handler.
  - `config/` — Pydantic `Settings` (`CARBON_*` env variables) and approved model constants.
  - `core/` — `CarbonCore` orchestrator, structured redacted logging, latency/throughput metrics.
  - `model_control/` — `CarbonModelSupervisor` (CMS), `OllamaProvider`, and offline `FakeProvider`.
  - `memory/` — `SQLiteMemoryEngine` (WAL, FTS5, migrations `v1`–`v3`, cancellation-safe transactions).
  - `governance/` — `GovernanceEngine` (`P0`–`P4` permissions, `A0`–`A3` autonomy evaluation, HITL approvals, SHA-256 hash-chained `audit.log`).
  - `intelligence/` — Heuristic intent classification and reasoning depth selection.
  - `tools/` — Pydantic-validated `ToolRegistry` and built-in tools (`system_info`, `list_applications`, `open_application`).
  - `devices/` — Local macOS `sysctl` telemetry (`_connected_nodes` placeholder for future Android work).
  - `interfaces/` — Protocol interfaces and Pydantic domain/IPC schemas.
  - `voice/` — Currently empty (`0` files; Voice STT/TTS is deferred).
- **`apps/macos/Carbon/` (Swift 6 / SwiftUI Client):**
  - `Sources/` — `CarbonApp`, `CarbonClient`, `ContentView`, `MarkdownRenderer`, `ProcessSupervisor`, `SingleInstanceLock`, `MemoryPressureMonitor`.
  - `Tests/` — Standalone Swift verification harnesses.
- **`tests/`:** Offline unit, integration, security, and performance suites (`conftest.py` enforces isolated temporary directories).
- **`docs/`:** Product (`PRD.md`), architecture specs & ADRs, security specs, development guides, benchmarks, and historical stage reports.

---

## 2. Development & Verification Workflow

1. **Environment Setup:** Follow [Development Setup Guide](setup.md) (`make setup` and `cd apps/macos/Carbon && swift build`).
2. **Code Standards:**
   - Python 3.12+ with full type annotations checked by `mypy src` and formatted/linted by `ruff`.
   - Keep external tool interactions strictly inside schema-validated tools gated by `GovernanceEngine`; never grant LLMs direct shell/filesystem authority or bind network sockets beyond loopback (`127.0.0.1`).
   - Preserve architectural boundaries and record breaking interface or policy decisions via ADRs in [docs/architecture/adr/](../architecture/adr/).
3. **Required Checks Before Submitting Changes:**
   ```bash
   make lint      # ruff check ., ruff format --check, mypy src
   make test      # offline Python tests excluding requires_ollama
   make test-ci   # offline CI subset excluding requires_ollama and requires_macos
   ```
   See [Testing & Quality Assurance](testing.md) for marker details and Swift verification harnesses.
4. **Database Safety:** Always use isolated `CARBON_*` environment paths during manual runs. Never run migrations, tests, or schema changes against `data/carbon.db` without a verified backup and explicit authorization.

---

## 3. Documentation Accuracy Standards

To avoid misleading users or contributors, all documentation updates must classify features using one of five explicit states:

- **IMPLEMENTED:** Present in the current codebase and backed by accepted tests or verification reports.
- **PARTIALLY IMPLEMENTED:** Core building blocks exist, while significant wiring, coverage, or runtime capability remains incomplete (e.g., multi-model runtime switching while the production allowlist is `('qwen3.5:9b',)`; `validate_output` not wired into `CarbonCore`).
- **DEFERRED:** Explicitly postponed for a later phase and absent from active code (e.g., Voice STT/TTS, macOS Keychain secret storage).
- **PLANNED:** Future roadmap scope not yet implemented (e.g., Android companion nodes, `.app` packaging/distribution, broader macOS automation, specialist embedding/vision engines).
- **UNVERIFIED:** Code or documentation exists, but sufficient verification evidence is unavailable or unauthorized (e.g., Stage 5-E Tier 2 and Tier 3 hardware benchmarks).

Additional rules:
- Preserve empirical measurement context for benchmark claims and disclose known limitations honestly.
- Do not present real `data/carbon.db` (currently schema v2) and isolated test databases (schema v3) as identical.
- Never include live tokens, credentials, or secrets in code, logs, examples, or docs.
