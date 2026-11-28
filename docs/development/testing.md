# Carbon Testing & Quality Assurance

Every subsystem change in Carbon must pass offline automated tests, static analysis, and formatting gates without touching the real `data/carbon.db`.

---

## 1. Python Test Structure & Isolation

The Python suite is configured in [pyproject.toml](../../pyproject.toml) (`asyncio_mode = "auto"`, `pythonpath = ["src"]`) and organized into:

- `tests/unit/` — Subsystem state machines, CMS lifecycle/pressure logic, SQLite cancellation safety & integrity, governance rules, intelligence heuristics, and orchestrator behaviors.
- `tests/integration/` — FastAPI loopback API endpoints, end-to-end model routing, and optional live Ollama inference checks.
- `tests/security/` — REST/WebSocket bearer authentication, Origin/CORS filtering, credential masking, approval gates, and memory API boundaries.
- `tests/performance/` — Performance verification tests.
- `tests/test_desktop_connection_reliability.py` — WebSocket connection lifecycle, authentication, and cleanup reliability tests.
- [tests/conftest.py](../../tests/conftest.py) — Autouse fixture isolating `CARBON_DATA_DIR`, `CARBON_DATABASE_PATH`, and `CARBON_AUDIT_LOG_PATH` into per-test `tmp_path` directories.

### Pytest Markers

Defined in [pyproject.toml](../../pyproject.toml):

| Marker | Meaning |
| :--- | :--- |
| `requires_ollama` | Tests that require a running local Ollama daemon on `127.0.0.1:11434` and installed `qwen3.5:9b` weights. |
| `requires_macos` | Tests that rely on macOS-specific utilities or host behavior (for example, `sysctl`). |

---

## 2. Standard Commands (`Makefile` & CI)

Use the [Makefile](../../Makefile) targets matching [.github/workflows/ci.yml](../../.github/workflows/ci.yml):

```bash
# Lint, formatting check, and static type checking
make lint

# Automatically format Python source, tests, scripts, and benchmarks
make format

# Run offline Python suite excluding live Ollama tests (with coverage)
make test

# Run cross-platform offline CI suite (excludes both requires_ollama and requires_macos)
make test-ci
```

Under the hood, `make lint` executes:
1. `.venv/bin/ruff check .`
2. `.venv/bin/ruff format --check src tests scripts benchmarks`
3. `.venv/bin/mypy src`

Note: `pyright` is listed in `[project.optional-dependencies]` in [pyproject.toml](../../pyproject.toml), but is not currently installed in the default `.venv` from `requirements.lock`; `mypy src` is the active repository type-check gate.

To run targeted test files directly:

```bash
.venv/bin/pytest -v tests/unit/test_stage69_sqlite_cancellation_safety.py
```

---

## 3. Verified Python Baseline (Stage 6.9-F Snapshot)

At Stage 6.9-F acceptance, the offline Python verification gates produced:

- `make test` (`pytest -m "not requires_ollama" --cov=src/carbon --cov-report=term-missing`): **255 passed, 2 deselected**, **88% total coverage**.
- `make lint`: `ruff check .` passed, `ruff format --check` verified **79 files**, and `mypy src` reported **no issues in 27 source files**.

This baseline documents the verified Stage 6.9-F snapshot; it is **not** a permanent guarantee for future code modifications and must be re-run whenever code changes.

---

## 4. macOS Swift Verification Harnesses

Native Swift tests live under `apps/macos/Carbon/Tests/`. Note that [Package.swift](../../apps/macos/Carbon/Package.swift) defines an executable target (`Carbon`) without an `XCTest` test target; each Swift test file is a standalone `@main` harness compiled with `swiftc -parse-as-library` against the relevant source files in `apps/macos/Carbon/Sources/`:

| Harness File | Purpose |
| :--- | :--- |
| `test_single_instance_lock.swift` | Kernel `flock` acquisition, release, PID metadata, and duplicate-launch handling. |
| `test_startup_state_machine.swift` | `ProcessSupervisor` startup state transitions, retries, and startup rollback. |
| `test_process_supervisor.swift` | Process ownership, health/tag checks, timeouts, and process termination safety. |
| `test_shutdown.swift` | Multi-phase graceful shutdown, lock release, Retry Quit, and global watchdog enforcement. |
| `test_markdown.swift` | Markdown/code block/table/math parsing and clipboard behavior. |
| `test_stage5d_ui_and_pressure.swift` | Token/Markdown coalescing throttle and memory-pressure monitor behavior. |
| `test_stage5e_streaming_fidelity.swift` | Streaming UI token accumulation and incremental rendering invariants. |

### Known Issue in `test_shutdown.swift`

In `apps/macos/Carbon/Tests/test_shutdown.swift`, standalone Test 19 (`line 1287`, `assert(report.timedOutByWatchdog == true)`) has a known **intermittent timing-sensitive assertion failure** (SIGTRAP, exit code `133`). Test 19 launches a real Python dummy child sleeping `0.9s` before running startup checks and a `0.25s` shutdown watchdog; when host scheduling delays pre-shutdown steps past ~`0.65s`, the dummy child exits cleanly before the `0.25s` watchdog expires, causing `timedOutByWatchdog` to be `false`. This issue remains open and has not been modified/fixed.

---

## 5. Quality Checklist Before Review

- [ ] `make lint` passes (`ruff check`, `ruff format --check`, `mypy src`).
- [ ] `make test` (and `make test-ci` if touching cross-platform logic) passes.
- [ ] Real `data/carbon.db` was never accessed, migrated, or altered during testing.
- [ ] No API tokens, credentials, or secrets appear in source, logs, or documentation.
