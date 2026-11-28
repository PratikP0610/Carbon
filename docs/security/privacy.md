# Carbon Privacy & Data Handling

**Stage 7.5 Alignment Status:** Aligned strictly to [sqlite_memory.py](../../src/carbon/memory/sqlite_memory.py), [orchestrator.py](../../src/carbon/core/orchestrator.py), [main.py](../../src/carbon/main.py), [logging.py](../../src/carbon/core/logging.py), [governance_engine.py](../../src/carbon/governance/governance_engine.py), and [settings.py](../../src/carbon/config/settings.py).

> [!IMPORTANT]
> Carbon is a **local-first** assistant with strong loopback network isolation and explicit forgetting APIs, **but it does not currently provide zero data retention, SQLite encryption at rest, macOS Keychain secret storage, or forensic zero-byte wiping on deletion.**

---

## 1. Control Status Overview

| Privacy Control Area | Current Implementation | Status |
| :--- | :--- | :--- |
| **Local-only inference (Ollama)** | Backend talks only to `CARBON_OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`); no cloud LLM calls exist in backend code. | **IMPLEMENTED** |
| **Loopback-only API** | HTTP binds strictly to `127.0.0.1:8765`, blocks non-loopback HTTP hosts, external origins, and `Origin: null`, and requires bearer auth on sensitive routes. | **IMPLEMENTED** |
| **Log & audit redaction** | `TokenSanitizingFilter` + `sanitize_structured_data` redact tokens, credentials, `prompt`, `raw_prompt`, `content`, `memory_content`, and `query`. | **IMPLEMENTED** |
| **TTL expiration & cleanup** | Configurable retention TTLs + periodic background purge (`working` 24h, `episodic` 7d, `tasks` rows 30d, task payloads 7d). | **IMPLEMENTED** |
| **Confirmation-safe forgetting & wipe APIs** | `forget_by_query_safe()`, `DELETE /api/v1/memory/{id}`, `POST /api/v1/memory/forget`, `DELETE /api/v1/conversations/{id}` remove SQLite rows & FTS entries. | **IMPLEMENTED** *(See leftover-data caveats below)* |
| **Credential protection (`Keychain` vs `0600` file)** | Uses `CARBON_API_TOKEN` or `0600` `data/.carbon_token`. Native Keychain (`ADR-0007`) is not implemented. | **PARTIALLY IMPLEMENTED** *(Keychain **DEFERRED**)* |
| **Voice recording privacy** | `src/carbon/voice/` is empty (0 files); no microphone capture or voice files exist. | **DEFERRED** *(Stage 6.6)* |
| **Database encryption at rest** | `data/carbon.db` is standard unencrypted SQLite (WAL mode). SQLCipher / filesystem key wrapping is not implemented. | **UNVERIFIED / NOT IMPLEMENTED** |
| **Remote / Android node sync** | `DeviceManager._connected_nodes` is an empty placeholder; no remote device sync protocol exists. | **PLANNED** |

---

## 2. What Data Is Stored Locally & Where

1. **SQLite Database (`CARBON_DB_PATH`, default `data/carbon.db`) — NOT Zero-Retention:**
   - **Conversations (`conversations` table):** Created automatically for REST (`stream=false` or created task) and WebSocket sessions.
   - **Tasks (`tasks` table):** Stores `prompt`, `state`, `intent`, `reasoning_depth`, `model_used`, `result`, `error`, `truncated`, and timestamps (`DEFAULT_TASK_TTL_DAYS = 30`). Sensitive payload fields (`prompt`, `result`, `error`, etc.) are scrubbed after `DEFAULT_TASK_PAYLOAD_TTL_DAYS = 7` during periodic maintenance.
   - **Memory Records (`memory_records` + `memory_fts`):**
     - **Completed chat tasks automatically store episodic interaction turns** (`memory_type = "episodic"`, key `"task:<task_id>"`, containing JSON `{"user": task.prompt, "assistant": (task.result or "")[:500]}`) whenever `episodic_ttl_days > 0` (default `7` days).
     - Explicit store commands (`"Remember ..."` or `POST /api/v1/memory`) persist `semantic`, `working` (`DEFAULT_WORKING_TTL_HOURS = 24`), or human-approved `procedural` records.
2. **Audit Log (`CARBON_AUDIT_LOG_PATH`, default `data/audit.log`):**
   - Append-only SHA-256 hash-chained JSONL logging security, model routing, tool-gate, and memory lifecycle events.
   - `TokenSanitizingFilter` and `sanitize_structured_data` redact tokens/credentials and user-text keys (`prompt`, `raw_prompt`, `content`, `memory_content`, `query`); tool audits log sorted `arg_keys` rather than raw values.
3. **Bearer Token (`data/.carbon_token`):**
   - Stored in plaintext with POSIX `0600` permissions when `CARBON_API_TOKEN` is unset. Protected from other OS users, **not** protected against rogue processes running under the same macOS UID.

---

## 3. Forgetting, Deletion & What Data May Remain

- **Memory Deletion (`DELETE /api/v1/memory/{id}`, `POST /api/v1/memory/forget`):**
  - Ambiguous query matches return candidates with `status: "confirmation_required"` (`confirm=false`) rather than deleting silently. Confirmed deletions remove matching `memory_records` rows and update `memory_fts` via `memory_ad` trigger.
  - **What can remain:** Deleting a `memory_records` row does **not** automatically delete the corresponding row in `tasks` (which holds `prompt` / `result` until `payload_expires_at` = 7 days unless wiped via conversation deletion).
- **Conversation Deletion (`DELETE /api/v1/conversations/{conversation_id}`):**
  - Deletes the `conversations` row (cascading to `tasks` via `ON DELETE CASCADE`) and removes linked `memory_records` rows.
- **SQLite & Audit Leftovers (`important limitation`):**
  - Standard SQLite `DELETE` + `WAL` mode marks database pages reusable rather than forensically overwriting disk sectors; residual bytes may persist in `-wal` or unvacuumed freelist pages until checkpoint/`VACUUM`.
  - Append-only `data/audit.log` keeps immutable event metadata (`MEMORY_FORGOTTEN` with `record_id` / `deleted_count`, etc.) for audit chain continuity.

---

## 4. Network & External Communication Boundaries

- **External Network:** Carbon backend contains no cloud LLM clients or external telemetry shippers (**IMPLEMENTED**).
- **Local IPC:** Traffic stays on loopback (`127.0.0.1:8765` HTTP/WS between SwiftUI and Python; `http://127.0.0.1:11434` plain HTTP between Python and local Ollama daemon). Note that Starlette HTTP middleware does not wrap `/ws/chat` upgrades; `/ws/chat` validates `Origin` and bearer tokens directly.
