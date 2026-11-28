# Carbon Database Schema

**Stage 7.3 Alignment Status:** Aligned strictly to `src/carbon/memory/sqlite_memory.py` and read-only inspection of both the user's real `data/carbon.db` and the isolated Stage 6 database (`/tmp/carbon_stage6_isolated/carbon.db`).

---

## 1. Storage Engine & Connection Configuration

Carbon stores persistent conversations, task history, and tiered memories in a single local SQLite database configured via `CARBON_DB_PATH` (default: `data/carbon.db`).

### Connection Pragmas

Every SQLite connection opens with the following pragmas applied in `SQLiteMemoryEngine`:

| Pragma | Value | Purpose |
| --- | --- | --- |
| `journal_mode` | `WAL` | Write-Ahead Logging for safe concurrent reads and atomic durability |
| `foreign_keys` | `ON` | Enforces `tasks.conversation_id -> conversations.conversation_id` cascading deletes |
| `synchronous` | `NORMAL` | Standard WAL durability/performance balance |
| `busy_timeout` | `5000` | 5,000 ms busy wait before raising `SQLITE_BUSY` |
| `temp_store` | `MEMORY` | Keeps temporary tables/indexes in RAM |
| `cache_size` | `-64000` | ~64 MB page cache (`64000` KiB) |

### Startup Integrity & Forensic Quarantine

Before running migrations, `SQLiteMemoryEngine.initialize()` inspects an existing non-empty database file:
1. Checks the 16-byte SQLite file header (`SQLite format 3\0`).
2. Runs `PRAGMA integrity_check`.
3. If either check fails, Carbon **fails closed** with `DatabaseIntegrityError` and moves the untouched `.db` file plus `-wal` and `-shm` siblings into a timestamped directory under `forensics/` rather than silently deleting or overwriting corrupted data.

---

## 2. Critical Schema Version Comparison (`data/carbon.db` v2 vs. Codebase v3)

> [!IMPORTANT]
> **Real Database (`data/carbon.db`) vs. Current Code & Isolated DB (`v3`)**
> Current Python code in `src/carbon/memory/sqlite_memory.py` defines migrations **v1, v2, and v3**. However, the existing repository user database (`data/carbon.db`, untouched during Stage 6 testing) is currently at **`PRAGMA user_version = 2`**. Any freshly initialized database (such as `/tmp/carbon_stage6_isolated/carbon.db`) automatically runs all three migrations to **`PRAGMA user_version = 3`**.

| Aspect | Real User Database (`data/carbon.db`) | Current Code / Isolated DB (`/tmp/carbon_stage6_isolated/carbon.db`) |
| --- | --- | --- |
| `PRAGMA user_version` | `2` | `3` |
| `schema_migrations` rows | `1`, `2` | `1`, `2`, `3` |
| `memory_records.conversation_id` column | **Absent** (`conversation_id` lives inside `metadata` JSON) | **Present** (`TEXT`, indexed by `idx_memory_conversation_id`, backfilled from `metadata`) |
| `memory_fts` definition | `fts5(id UNINDEXED, key, content, content='memory_records', content_rowid='rowid')` + matching 4-column triggers | `fts5(key, content, content='memory_records', content_rowid='rowid')` + 3-column triggers |
| `model_registry` table | **Does NOT exist** | **Does NOT exist** (models are governed in memory/config by `Settings` + `CMS`, not SQLite) |

When Carbon next initializes against a v2 database with write access, migration v3 will run automatically inside a write transaction (`ALTER TABLE memory_records ADD COLUMN conversation_id TEXT`, `idx_memory_conversation_id`, FTS5 rebuild, and backfill).

---

## 3. Authoritative Schema (Migrations v1–v3)

### 3.1 `schema_migrations` (Version Tracking)

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
```

- Tracks applied migrations cleanly (`1: initial_memory_schema`, `2: conversations_and_tasks_schema`, `3: memory_records_conversation_id`).
- Notice the descriptive column is **`name`**, not `description`.

---

### 3.2 `conversations` (Introduced in Migration v2)

```sql
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'Conversation',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_conversations_updated ON conversations(updated_at DESC);
```

| Column | Type | Null / Default | Description |
| --- | --- | --- | --- |
| `conversation_id` | `TEXT PRIMARY KEY` | NOT NULL | Unique conversation identifier (e.g., UUID or client-supplied session ID) |
| `title` | `TEXT` | NOT NULL DEFAULT `'Conversation'` | Human-readable conversation label |
| `created_at` | `TEXT` | NOT NULL | ISO-8601 UTC timestamp |
| `updated_at` | `TEXT` | NOT NULL | ISO-8601 UTC timestamp updated on new turns/tasks |
| `expires_at` | `TEXT` | NULL | Optional conversation expiration timestamp |
| `metadata` | `TEXT` | NOT NULL DEFAULT `'{}'` | JSON object for conversation metadata |

---

### 3.3 `tasks` (Introduced in Migration v2)

```sql
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    prompt TEXT,
    state TEXT NOT NULL,
    intent TEXT,
    reasoning_depth TEXT NOT NULL DEFAULT 'standard',
    model_used TEXT,
    result TEXT,
    error TEXT,
    truncated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    expires_at TEXT,
    payload_expires_at TEXT,
    FOREIGN KEY (conversation_id)
        REFERENCES conversations(conversation_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_tasks_conversation_id ON tasks(conversation_id);
CREATE INDEX idx_tasks_state ON tasks(state);
CREATE INDEX idx_tasks_created_at ON tasks(created_at DESC);
```

| Column | Type | Null / Default | Description |
| --- | --- | --- | --- |
| `task_id` | `TEXT PRIMARY KEY` | NOT NULL | Unique task identifier |
| `conversation_id` | `TEXT` | NOT NULL | Foreign key to `conversations(conversation_id)` with `ON DELETE CASCADE` |
| `prompt` | `TEXT` | NULL | User prompt text (cleared past `payload_expires_at` during TTL purge) |
| `state` | `TEXT` | NOT NULL | `TaskState` string (`queued`, `planning`, `awaiting_approval`, `executing_tool`, `streaming`, `completed`, `cancelled`, `failed`, `rejected`, etc.) |
| `intent` | `TEXT` | NULL | Serialized intent summary/classification |
| `reasoning_depth` | `TEXT` | NOT NULL DEFAULT `'standard'` | `instant`, `standard`, or `deep` |
| `model_used` | `TEXT` | NULL | Actual approved model ID used (e.g., `qwen3.5:9b`) |
| `result` | `TEXT` | NULL | Final response payload (cleared past `payload_expires_at`) |
| `error` | `TEXT` | NULL | Sanitized error detail on failure/rejection |
| `truncated` | `INTEGER` | NOT NULL DEFAULT `0` | Boolean integer (`0`/`1`) marking output length truncation |
| `created_at` | `TEXT` | NOT NULL | ISO-8601 UTC creation timestamp |
| `completed_at` | `TEXT` | NULL | ISO-8601 UTC completion timestamp |
| `expires_at` | `TEXT` | NULL | Row-level retention deadline (`DEFAULT_TASK_TTL_DAYS = 30`) |
| `payload_expires_at` | `TEXT` | NULL | Prompt/result payload scrub deadline (`DEFAULT_TASK_PAYLOAD_TTL_DAYS = 7`) |

**Lifecycle & Recovery Behavior:** On startup, `SQLiteMemoryEngine` recovers stale non-terminal tasks left behind by an ungraceful process termination. During periodic maintenance, rows past `payload_expires_at` have sensitive payload fields (`prompt`, `result`, etc.) scrubbed while retaining task status metadata until `expires_at`.

---

### 3.4 `memory_records` (Migration v1 + Migration v3 `conversation_id`)

```sql
CREATE TABLE memory_records (
    id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    key TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'user_interaction',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    conversation_id TEXT
);

CREATE INDEX idx_memory_type_key ON memory_records(memory_type, key);
CREATE INDEX idx_memory_created_at ON memory_records(created_at DESC);
CREATE INDEX idx_memory_updated_at ON memory_records(updated_at DESC);
CREATE INDEX idx_memory_expires_at ON memory_records(expires_at);
CREATE INDEX idx_memory_conversation_id
    ON memory_records(conversation_id, memory_type, created_at DESC);
```

| Column | Type | Null / Default | Description |
| --- | --- | --- | --- |
| `id` | `TEXT PRIMARY KEY` | NOT NULL | Deterministic/UUID memory record identifier |
| `memory_type` | `TEXT` | NOT NULL | One of `working`, `episodic`, `semantic`, `procedural` |
| `key` | `TEXT` | NOT NULL | Topical key or conversation turn key |
| `content` | `TEXT` | NOT NULL | Memory body (or serialized JSON `{"user": ..., "assistant": ...}` for episodic turns) |
| `metadata` | `TEXT` | NOT NULL DEFAULT `'{}'` | JSON metadata (tags, conversation link, safety markers) |
| `source` | `TEXT` | NOT NULL DEFAULT `'user_interaction'` | Provenance (`user_interaction`, `chat_turn`, etc.) |
| `confidence` | `REAL` | NOT NULL DEFAULT `1.0` | Confidence weight (`0.0`–`1.0`) |
| `created_at` | `TEXT` | NOT NULL | ISO-8601 UTC timestamp |
| `updated_at` | `TEXT` | NOT NULL | ISO-8601 UTC timestamp |
| `expires_at` | `TEXT` | NULL | Expiration deadline when TTL applies (`working`: `24h`, `episodic`: `7d` defaults) |
| `conversation_id` | `TEXT` | NULL | **Added in v3:** Dedicated indexed column (not a SQL `FOREIGN KEY` constraint in DDL) linking conversation-scoped records |

---

### 3.5 Full-Text Search: `memory_fts` & Triggers (v1 + v3 Rebuild)

In current code (v1/v3):

```sql
CREATE VIRTUAL TABLE memory_fts USING fts5(
    key, content, content='memory_records', content_rowid='rowid'
);

CREATE TRIGGER memory_ai AFTER INSERT ON memory_records BEGIN
    INSERT INTO memory_fts(rowid, key, content)
    VALUES (new.rowid, new.key, new.content);
END;

CREATE TRIGGER memory_ad AFTER DELETE ON memory_records BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, key, content)
    VALUES('delete', old.rowid, old.key, old.content);
END;

CREATE TRIGGER memory_au AFTER UPDATE ON memory_records BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, key, content)
    VALUES('delete', old.rowid, old.key, old.content);
    INSERT INTO memory_fts(rowid, key, content)
    VALUES (new.rowid, new.key, new.content);
END;
```

- SQLite automatically creates FTS5 shadow tables (`memory_fts_config`, `memory_fts_data`, `memory_fts_docsize`, `memory_fts_idx`).
- Migration v3 runs `INSERT INTO memory_fts(memory_fts) VALUES('rebuild');` after altering `memory_records`.
- *(Historical v2 difference on `data/carbon.db`: its existing `memory_fts` schema includes `id UNINDEXED` alongside `key, content` and matching 4-column trigger statements.)*

---

## 4. Concurrency, WAL & Cancellation-Safe Transactions (Stage 6.9-F)

Because `SQLiteMemoryEngine` executes synchronous `sqlite3` calls on an asyncio executor thread against one shared connection:

1. **Application Write Serialization (`_db_lock`)**:
   All engine database methods acquire `self._db_lock` (`asyncio.Lock`) so concurrent coroutines never interleave SQLite transaction statements on the shared connection.
2. **Stage 6.9-F Cancellation Safety (`_write_transaction`)**:
   During Stage 6.9 testing, cancelling an awaiting task while `_write_transaction()` was blocked between `BEGIN IMMEDIATE` and `COMMIT` caused the shared connection to remain inside an open transaction (`OperationalError: cannot start a transaction within a transaction`). The Stage 6.9-F fix adds:
   - `_in_write_tx` re-entrancy guard raising cleanly if nested write transactions are attempted.
   - Defensive pre-begin rollback if `conn.in_transaction` is unexpectedly true.
   - `_await_task_resolving_cancellation()`: wraps `BEGIN IMMEDIATE`, transaction body, `_rollback_uninterruptible()`, and `COMMIT` in shielded tasks that keep holding `_db_lock` until the active executor work finishes and either commits or rolls back cleanly, before re-raising `CancelledError`.
3. **Periodic Maintenance & Checkpointing**:
   `CarbonCore` periodically purges expired TTL records and runs best-effort `WAL` checkpoints (`PRAGMA wal_checkpoint(...)`), plus clean connection closure on shutdown.
