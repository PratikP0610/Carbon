# Carbon Stage 1.11 — Increment 2C Final Delivery & Review Report
## Startup State Machine & `CarbonClient` / SwiftUI Integration

**Task ID:** `C-1.11-P2-2C` (Final Review)  
**Phase:** Startup State Machine Integration  
**Mode:** Controlled implementation, verification, and acceptance review  
**Date:** 2026-11-25  
**Status:** VERIFIED & COMPLETE (Awaiting Project-Owner Acceptance before Increment 2D)

---

## 1. Executive Summary

Increment 2C integrates the accepted [`ProcessSupervisor`](../../apps/macos/Carbon/Sources/ProcessSupervisor.swift) directly into Carbon's existing macOS SwiftUI application (`CarbonApp.swift`, `CarbonClient.swift`, `ContentView.swift`), verified with full Swift and Python regression suites and hardened for every edge case raised during final acceptance review.

---

## 2. Files Changed

| File | Action | Purpose |
|---|---|---|
| `apps/macos/Carbon/Sources/CarbonClient.swift` | Modified | Decoupled `init(autoConnect:projectRoot:baseURL:wsURL:)` (`autoConnect` defaults to `false`), added multi-path `resolveAuthToken()`, added duplicate `connectWebSocket()` guard, `disconnectWebSocket()`, `startMonitoring()`, `lastAuthError` / `lastConnectionError`, `autoReconnectEnabled`, WebSocket close-code `4401`/`1008` + `"Invalid token"` detection, and fail-closed `pendingAuthToken` validation inside `webSocketDidOpen` so missing tokens never report `.connected`. |
| `apps/macos/Carbon/Sources/ProcessSupervisor.swift` | Modified | Added `StartupState` enum, `CarbonClientConnecting` protocol, auth/WS/cancellation cases + `recoverySuggestion` on `ProcessSupervisorError`, observable UI status properties, deduplicated `startup(client:...)`, `cancelStartup()`, owned-child failure/cancellation cleanup preserving `.external` processes, dead-handle reset prior to probe, and strict `=== proc` identity checks inside child `terminationHandler` closures. |
| `apps/macos/Carbon/Sources/ContentView.swift` | Modified | Extended existing UI with `startupBannerView` (progress indicator, active phase & dependency detail, `Ollama`/`Backend` `Reused` vs `Managed` badges, `Cancel` button, selectable recovery instructions + `Retry` button on `.failed`) and header pill status mapping. Existing chat UI preserved untouched. |
| `apps/macos/Carbon/Sources/CarbonApp.swift` | Modified | Declared `CarbonClient: CarbonClientConnecting`, initialized `CarbonClient(autoConnect: false, projectRoot: root)` and `ProcessSupervisor(projectRoot: root)`, and invoked `supervisor.startup(client: client)` in `.task` while preserving `--verify-ui` and Increment 2A `SingleInstanceLock`. |
| `apps/macos/Carbon/Tests/test_startup_state_machine.swift` | Created | 16 automated scenarios covering startup state transitions, error handling, authentication lifecycle, ownership/termination-timeout Retry safety, deduplication, cancellation, real `CarbonClient`, and ephemeral loopback WebSocket chat. |
| `tests/test_desktop_connection_reliability.py` | Modified | Isolated `test_app` fixture from live Ollama daemon availability by mocking `OllamaProvider.is_healthy` and `OllamaProvider.list_available_models` via `monkeypatch` without modifying any test assertions. |

---

## 3. Architecture & Startup State Transitions

```
idle
  └──> checkingOllama        (Probe 127.0.0.1:11434/api/version)
         ├── [ready] ────────────────────────────┐ (ollamaOwnership = .external, isOllamaReused = true)
         └── [starting / notResponding]          │
               └──> startingOllama               │ (Launch 'ollama serve' if needed; ollamaOwnership = .owned)
                      │                          │
                      ▼                          ▼
                  verifyingModel  (GET 127.0.0.1:11434/api/tags for 'qwen3.5:9b'; NEVER pulls)
                      │
                      ▼
                  checkingBackend (Probe 127.0.0.1:8765/api/v1/health)
                      ├── [healthy] ───────────────────────────┐ (backendOwnership = .external, isBackendReused = true)
                      └── [unavailable]                        │
                            └──> startingBackend               │ (Launch .venv/bin/python -m carbon.main)
                                   │                           │
                                   ▼                           │
                             waitingForBackend                 │ (Poll /api/v1/health; backendOwnership = .owned)
                                   │                           │
                                   ▼                           ▼
                               connecting  (Resolve auth token safely; CarbonClient.connectWebSocket())
                                   │
                                   ▼ [WebSocket 'authenticated' event received]
                                 ready     (Enable autoReconnectEnabled & startMonitoring())

Any phase error / missing dependency / timeout / auth failure / WS error / cancelStartup():
  └──> disconnectWebSocket() + terminateOwnedChildrenAndAwaitExit(timeout: 2.0) [external untouched]
  └──> failed (startupError populated + recoverySuggestion + Retry button; .owned kept if .timedOut)
```

---

## 4. Python Failure Investigation (`tests/test_desktop_connection_reliability.py`)

### 4.1 Findings
The three prior failures (`test_health_endpoint_contract`, `test_websocket_disconnect_completed_task_safe`, `test_websocket_disconnect_already_cancelled_task_safe`) were confirmed to be **100% environmental**, not implementation regressions:
- `CarbonCore.get_system_health()` (`src/carbon/core/orchestrator.py:191-205`) invokes `await self.provider.is_healthy()`, issuing a real HTTP `GET http://127.0.0.1:11434/api/version`.
- `tests/test_desktop_connection_reliability.py`'s `test_app` fixture did not isolate `OllamaProvider`, so whenever Ollama was not running on `127.0.0.1:11434`, `ollama_healthy` evaluated to `False` and `/api/v1/health` returned `{"status": "degraded", "ollama_connected": false}`.

### 4.2 Fix & Verification
- Isolated `OllamaProvider.is_healthy` (`AsyncMock(return_value=True)`) and `OllamaProvider.list_available_models` (`qwen3.5:9b` metadata) inside `test_app(monkeypatch)` in `tests/test_desktop_connection_reliability.py`.
- All original test assertions (`status == "healthy"`, `ollama_connected is True`, `active_model["id"] == "qwen3.5:9b"`) remain completely intact and unmodified.
- **Re-run Result:**
  - `.venv/bin/pytest tests/test_desktop_connection_reliability.py` -> **`11 passed, 1 warning in 0.26s`** (Exit `0`)
  - `.venv/bin/pytest tests/test_desktop_connection_reliability.py tests/integration/ tests/security/ tests/unit/` -> **`85 passed, 1 skipped, 1 warning in 3.73s`** (Exit `0`; `test_e2e_inference.py` cleanly skipped via `Ollama daemon is not running locally.`)

---

## 5. Authentication Lifecycle Verification (`CarbonClient` + `ProcessSupervisor`)

Verified by code audit and automated Tests 8, 9, 10, 12, 13, and 14:
1. **No Premature Ready State:** `CarbonClient.connectWebSocket()` sets `connectionStatus = .connecting` (`isConnected = false`). `webSocketDidOpen` sets `connectionStatus = .authenticating` (`isConnected = false`) and sends `{"action": "auth", "token": ...}`. `ProcessSupervisor` remains in `.connecting` and **only** transitions to `.ready` after `CarbonClient` receives `{"event": "authenticated"}` (`isConnected == true`).
2. **Fail-Closed Token Handling:** `ProcessSupervisor` rejects missing tokens in Step 11 (`.authenticationFailed`) before calling `connectWebSocket()`. Inside `CarbonClient.webSocketDidOpen`, if `pendingAuthToken ?? resolveAuthToken()` is nil/empty, it sets `lastAuthError = "Missing local authentication token."`, `connectionStatus = .failed`, `isConnected = false`, and cancels the socket—never marking `.connected`.
3. **Authentication Rejection Reporting:** Backend error messages containing `"Authentication failed"` or `"Invalid token"`, as well as WebSocket close codes `4401` and `1008`, set `lastAuthError`, transition `connectionStatus = .failed`, halt auto-reconnect loops, and bubble up as `ProcessSupervisorError.authenticationFailed` -> `StartupState.failed`.
4. **No False Ready & No Retry Duplicates:** On failure/timeout, `ProcessSupervisor` disconnects `CarbonClient`, keeps `autoReconnectEnabled = false`, transitions to `.failed`, and never records `.ready` in `stateTransitionHistory`. Concurrent `startup` / `connectWebSocket()` calls are deduplicated (`connectWebSocket` returns immediately when already `.connecting`/`.authenticating`/`.connected` with an active `webSocketTask`).

---

## 6. Process Ownership, Failure Cleanup & Retry Verification

Verified by code audit and automated Tests 5, 7, 9, 11, 15, and 16:
1. **Strict Ownership Boundary:** `terminateOwnedChildrenOnly` / `terminateOwnedChildrenAndAwaitExit` only signal children whose ownership is `.owned` and with a retained `ProcessRunning` handle. Pre-existing `.external` Ollama and backend instances remain completely untouched across failures, timeouts, and cancellations.
2. **Stale Handler Race Protection:** Spawned process `terminationHandler` closures capture `[weak self, weak proc]` and verify `self._childOllamaProcess === proc` (or `_childBackendProcess === proc`) before resetting ownership, preventing an earlier exited process from clearing a newly spawned process handle.
3. **Termination Timeout Ownership Retention:** If a child ignores SIGTERM (`terminateAndAwaitExit` returns `.timedOut`), ownership stays `.owned` and the process handle remains retained.
4. **Retry Behavior After Timeout:** When Retry runs while an owned child from a prior timed-out attempt is still running (`isRunning == true`):
   - If the retained owned child subsequently becomes healthy, Step 0 detects readiness and reuses it cleanly while preserving `.owned`—spawning **0 duplicate processes**.
   - If the retained owned child remains stuck/unhealthy, Step 0 awaits timeout, re-attempts termination, preserves `.owned` if `.timedOut`, and strictly **blocks spawning a second conflicting child**.

---

## 7. Automated Test, Build & Lint Results

| Check | Result | Exit Code |
|---|---|---|
| `swiftc -parse-as-library ... test_startup_state_machine.swift && /tmp/test_startup_state_machine` | **16 / 16 PASS** | `0` |
| `swiftc -parse-as-library ... test_process_supervisor.swift && /tmp/test_process_supervisor` | **33 / 33 PASS** | `0` |
| `swiftc -parse-as-library ... test_single_instance_lock.swift && /tmp/test_single_instance_lock` | **8 / 8 PASS** | `0` |
| `swiftc -parse-as-library ... test_markdown.swift && /tmp/test_markdown` | **ALL PASS** (high-volume 9,000 parses in 0.070s, stop latency 0.030ms) | `0` |
| `swift build --package-path apps/macos/Carbon` | **Build complete! (0.27 secs incremental / 16.96 secs full)** | `0` |
| `.venv/bin/pytest tests/test_desktop_connection_reliability.py tests/integration/ tests/security/ tests/unit/` | **85 passed, 1 skipped, 1 warning in 3.73s** | `0` |
| `.venv/bin/ruff check src/ tests/` | **All checks passed!** | `0` |
| `git diff --check` | **Clean (no output)** | `0` |

---

## 8. Manual macOS GUI Testing Status

- **Live Service Availability Probe:** Read-only probes (`curl --max-time 1`) to `http://127.0.0.1:11434/api/version` (`ollama:000`) and `http://127.0.0.1:8765/api/v1/health` (`backend:000`) confirmed neither production service is currently running locally.
- **Status:** Manual macOS GUI launch testing was **not** executed in this increment because starting/stopping production services is prohibited under Increment 2C restrictions and Increment 2D quit/termination integration is not yet wired into `AppDelegate`. Manual interactive desktop launch QA should be executed under separate authorization once Increment 2D is implemented or production services are started.

---

## 9. Remaining Limitations & Git Backup Verification

1. **Increment 2D Deferred:** `AppDelegate.applicationShouldTerminate(_:)` and coordinated shutdown of owned backend/Ollama children on app quit remain unimplemented until Increment 2D authorization.
2. **Manual GUI & Live E2E Inference QA:** Interactive GUI testing and `tests/integration/test_e2e_inference.py` require a live Ollama daemon with `qwen3.5:9b`.
3. **Git State & Backup Verification:**
   - Branch: `backup/carbon-tested-phase1-2026-10-29`
   - `HEAD`: `0c7d1d17c55c836715438c7d0c00887cf9aaf19c`
   - Backup ref `backup/carbon-tested-phase1-2026-10-29`: `0c7d1d17c55c836715438c7d0c00887cf9aaf19c` (preserved untouched; zero commits/pushes/branch switches).

---

**Stop Condition:** All Increment 2C acceptance checks are complete and verified. Awaiting project-owner acceptance before proceeding to Increment 2D.
