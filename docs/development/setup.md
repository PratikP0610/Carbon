# Carbon Development Setup Guide

This guide covers setting up Carbon's Python backend and native macOS SwiftPM desktop client from source.

---

## 1. Prerequisites

- **Hardware:** Apple Silicon Mac (`arm64`, 16 GB unified memory baseline).
- **Operating System:** macOS 14 Sonoma or newer (Swift package targets `.macOS(.v14)`).
- **Python:** Python `3.12+` (expected by `Makefile` and `pyproject.toml`).
- **Swift / Xcode:** Xcode 16+ or Command Line Tools with Swift 6.0 (`swift-tools-version: 6.0` in [Package.swift](../../apps/macos/Carbon/Package.swift)).
- **Ollama:** Installed locally and reachable on `http://127.0.0.1:11434` with the approved primary model `qwen3.5:9b`.

---

## 2. Ollama & Model Setup

Carbon's approved production model configuration in [settings.py](../../src/carbon/config/settings.py) is strictly:
- **Primary model:** `qwen3.5:9b`
- **Approved allowlist:** `('qwen3.5:9b',)`
- **CMS memory budget:** `9216 MB` (`DEFAULT_MODEL_MEMORY_BUDGET_MB = 9216`)

Verify or pull the model:

```bash
brew install ollama          # if not already installed
brew services start ollama   # or run `ollama serve` manually when needed
ollama pull qwen3.5:9b
ollama list | grep qwen3.5:9b
```

---

## 3. Python Backend Setup

### Verified Workflow (`make setup`)

The repository [Makefile](../../Makefile) installs pinned dependencies from [requirements.lock](../../requirements.lock) and links `src/carbon` in editable mode without drifting transitive packages:

```bash
make setup
```

`make setup` performs:
1. Creates `.venv` using `python3.12` (if absent).
2. Runs `.venv/bin/python -m pip install -r requirements.lock`.
3. Runs `.venv/bin/python -m pip install --no-deps -e .`.

If you need development tools (`pytest`, `ruff`, `mypy`) outside the lockfile or via optional extras, inspect [pyproject.toml](../../pyproject.toml) and `requirements.lock` before installing.

### Optional `.env` Template

An example configuration file exists at [.env.example](../../.env.example). Note that [.env.example](../../.env.example) sets `CARBON_INFERENCE_TIMEOUT_SECONDS=60.0` as an example override, whereas the code default in [settings.py](../../src/carbon/config/settings.py) is `300.0` seconds. Never store real API tokens or secrets in `.env`.

### Backend Entry Points

With the virtual environment populated, the backend can be started directly (using default `data/` unless isolated env vars are set):

```bash
make run                         # runs .venv/bin/python -m carbon.main
# or via console script installed in .venv:
.venv/bin/carbon
```

`carbon.main:main()` starts Uvicorn strictly on `127.0.0.1:8765` (loopback only).

---

## 4. macOS SwiftPM Client Build & Launch

The native desktop application is defined as a Swift Package Manager executable target in [Package.swift](../../apps/macos/Carbon/Package.swift):

```bash
cd apps/macos/Carbon
swift build
```

This compiles the macOS client executable at:

```text
apps/macos/Carbon/.build/debug/Carbon
```

### Runtime Behavior
- **Single-instance lock:** On launch, `CarbonApp` acquires a `flock` via `SingleInstanceLock` (`CARBON_LOCK_PATH` or `data/.carbon_app.lock`). Passing `--skip-instance-lock` skips lock acquisition for specialized test runs.
- **Supervised child processes:** `ProcessSupervisor` checks `http://127.0.0.1:11434` (Ollama) and `http://127.0.0.1:8765/api/v1/health` (Python backend). If they are not running, it spawns `ollama serve` and `carbon.main` (using `CARBON_PYTHON_PATH` if set, otherwise `<projectRoot>/.venv/bin/python`), inheriting the active process environment.
- **No `.app` bundle yet:** Because the binary is built directly with SwiftPM rather than packaged into a signed `.app` bundle, launch it explicitly from Terminal with controlled environment variables rather than through Finder or an uncontrolled `open` command.

---

## 5. Environment Variables & Database Isolation

Carbon configuration is loaded by Pydantic `Settings` (`env_prefix="CARBON_"`) in [settings.py](../../src/carbon/config/settings.py) and read directly by the macOS Swift client in [ProcessSupervisor.swift](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift), [CarbonClient.swift](../../apps/macos/Carbon/Sources/CarbonClient.swift), and [SingleInstanceLock.swift](../../apps/macos/Carbon/Sources/SingleInstanceLock.swift):

| Variable | Consumed By | Purpose & Default |
| :--- | :--- | :--- |
| `CARBON_DATA_DIR` | Python `Settings` | Root directory for runtime data files, token file fallback (`.carbon_token`), and backups. Default: `data`. |
| `CARBON_DATABASE_PATH` | Python `Settings` | Path to the SQLite database file. Default: `data/carbon.db`. |
| `CARBON_AUDIT_LOG_PATH` | Python `Settings` | Path to the append-only SHA-256 hash-chained audit log. Default: `data/audit.log`. |
| `CARBON_LOCK_PATH` | Swift `SingleInstanceLock` | Path to the macOS app single-instance `flock` file. Default: `<projectRoot>/data/.carbon_app.lock` (or `~/Carbon/data/.carbon_app.lock`). |
| `CARBON_API_TOKEN` | Python `Settings` & Swift (`CarbonClient`, `ProcessSupervisor`) | Explicit Bearer authentication token passed over IPC/environment. When unset, Python generates/reads a POSIX `0600` token at `<CARBON_DATA_DIR>/.carbon_token`. Never print, log, or commit token values. |
| `CARBON_PYTHON_PATH` | Swift `ProcessSupervisor` | Optional absolute path to the Python interpreter used when the macOS app spawns `python -m carbon.main`. Falls back to `<projectRoot>/.venv/bin/python`. |

### Isolated Testing vs. Normal Application Use

> [!CAUTION]
> **Never launch Carbon against the existing real database for migration or testing without a verified backup and explicit authorization.**

- **Normal application use:** Running `make run` or `apps/macos/Carbon/.build/debug/Carbon` without overrides uses `data/carbon.db`, `data/audit.log`, and `data/.carbon_app.lock`.
- **Isolated development / testing:** Always set all isolation variables (`CARBON_DATA_DIR`, `CARBON_DATABASE_PATH`, `CARBON_AUDIT_LOG_PATH`, `CARBON_LOCK_PATH`, plus a session-scoped `CARBON_API_TOKEN`) pointing to an isolated directory such as `/tmp/carbon_isolated` before starting **either** the Python backend or `apps/macos/Carbon/.build/debug/Carbon`:

```bash
export CARBON_DATA_DIR=/tmp/carbon_isolated
export CARBON_DATABASE_PATH=/tmp/carbon_isolated/carbon.db
export CARBON_AUDIT_LOG_PATH=/tmp/carbon_isolated/audit.log
export CARBON_LOCK_PATH=/tmp/carbon_isolated/.carbon_app.lock
export CARBON_API_TOKEN="ephemeral_session_token_generated_locally"
```

When starting `Carbon` from this shell, `ProcessSupervisor` forwards these environment variables to any child backend it launches, preventing any access to `data/carbon.db`.

---

## 6. Verification Checklist

Run host diagnostics and the offline checks before developing:

```bash
chmod +x scripts/diagnostics.sh
./scripts/diagnostics.sh
make lint
make test
```
