# Carbon Failure Recovery & Graceful Shutdown Specification

## 1. System Shutdown Architecture

Carbon 1.0.0 enforces clean termination semantics to guarantee zero database corruption, zero orphaned child processes, and zero leaked inference memory allocations.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        SHUTDOWN TRIGGER RECEIVED                       │
│                   (SIGINT, SIGTERM, or API /shutdown)                  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Step 1: Reject Inbound Traffic                                         │
│  - Set core.is_running = False                                         │
│  - Return 503 Service Unavailable to new REST requests                 │
│  - Broadcast 'shutdown_initiated' event to active WebSocket clients    │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Step 2: Cancel Active Tasks & Inference Streams                        │
│  - Trigger cancel_event.set() on all active TaskRecords                │
│  - Abort active HTTP streaming sockets to Ollama daemon                │
│  - Release CMS inference locks and model switch locks                  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Step 3: Flush Persistence & WAL Checkpoint                             │
│  - Complete pending memory writes                                      │
│  - Execute PRAGMA wal_checkpoint(TRUNCATE);                            │
│  - Close SQLite database connections cleanly                           │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Step 4: Audit Trail Finalization & Security Cleanup                    │
│  - Record CARBON_CORE_SHUTDOWN audit log event                         │
│  - Securely wipe ephemeral token file (data/.carbon_token)             │
│  - Exit process cleanly with returncode 0                              │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Startup Recovery & State Reconciliation

When Carbon Core boots up, it executes an integrity verification workflow before accepting user instructions:

1. **Database Integrity Check:**
   - Runs `PRAGMA integrity_check;` on `data/carbon.db`.
   - If corruption is detected, moves the damaged database to `data/carbon.corrupt.<timestamp>.db` and initializes a fresh database from migrations, alerting the user.
2. **Orphaned Task Reconciliation:**
   - Queries `tasks` table for records left in `PENDING`, `ANALYZING`, `EXECUTING`, or `STREAMING` states from a previous ungraceful termination (e.g. system power loss).
   - Updates their state to `FAILED` with error message `"Task interrupted by unexpected host termination."`
3. **Model Runtime Verification:**
   - Issues a ping to Ollama at `http://127.0.0.1:11434/api/version`.
   - Verifies `qwen3.5:9b` presence via `/api/tags`.
   - Pre-warms `qwen3.5:9b` to establish warm inference readiness.

---

## 3. Subsystem Failure Recovery

### 3.1 Ollama Disconnect & Reconnection Watchdog
- If an active inference HTTP request suffers `RemoteDisconnected` or `ConnectError`:
  1. The active task is transitioned to `FAILED` with user-friendly diagnosis: `"Local AI model daemon (Ollama) disconnected."`
  2. The CMS background watchdog transitions model status to `is_healthy = False`.
  3. The watchdog initiates automated reconnection probes every 3.0 seconds.
  4. Once Ollama responds with HTTP 200 on `/api/version`, CMS re-registers models and transitions system state back to `HEALTHY`.

### 3.2 Memory Pressure Recovery (macOS Host Integration)
- On Apple Silicon, unified memory is shared dynamically.
- Carbon monitors host memory status via `sysctl kern.memorystatus_vm_pressure_level`:
  - **Level 1 (Normal):** Standard operation. Primary model kept warm in GPU memory (`keep_alive: 5m`).
  - **Level 2 (Warning):** CMS immediately unloads any idle background specialist models. Forces `keep_alive: 0` on non-essential tasks. Reduces max generation context to 8,192 tokens.
  - **Level 4 (Critical):** CMS unloads the chat model immediately after generation. Disallows deep reasoning steps. Prompts user to close heavy background Mac applications.
