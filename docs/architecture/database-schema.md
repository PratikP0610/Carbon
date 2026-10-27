# Carbon SQLite Database Schema Specification

## 1. Storage Topology & Runtime Configuration

Carbon 1.0.0 uses an embedded local **SQLite** database (`data/carbon.db`) accessed asynchronously via `aiosqlite`. In accordance with ADR-0004, no external database server daemons are introduced.

### 1.1 Critical Performance & Safety Pragmas
The connection pool must execute the following pragmas immediately upon opening:

```sql
PRAGMA journal_mode = WAL;         -- Write-Ahead Logging for non-blocking concurrent reads
PRAGMA synchronous = NORMAL;       -- Balances durability with NVMe write performance
PRAGMA foreign_keys = ON;          -- Strict referential integrity enforcement
PRAGMA busy_timeout = 5000;        -- 5000ms retry buffer during high-concurrency writes
PRAGMA temp_store = MEMORY;        -- Store temporary tables and sorting buffers in RAM
PRAGMA cache_size = -64000;        -- 64 MB dedicated page cache
```

---

## 2. Table Definitions

### 2.1 Schema Versioning (`schema_migrations`)
Tracks migration history and prevents schema mismatches across versions.

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
```

### 2.2 Memory Records (`memory_records`)
Partitioned store for the 4 memory tiers: `working`, `episodic`, `semantic`, and `procedural`.

```sql
CREATE TABLE IF NOT EXISTS memory_records (
    id TEXT PRIMARY KEY,                       -- UUID v4
    memory_type TEXT NOT NULL,                 -- 'working', 'episodic', 'semantic', 'procedural'
    key TEXT NOT NULL,                         -- Lookup key or semantic category tag
    content TEXT NOT NULL,                     -- Text payload / memory body
    metadata TEXT NOT NULL DEFAULT '{}',       -- Structured JSON metadata
    source TEXT NOT NULL,                      -- e.g. 'user_interaction', 'system', 'tool_output'
    confidence REAL NOT NULL DEFAULT 1.0,      -- Relevance score (0.0 to 1.0)
    created_at TEXT NOT NULL,                  -- ISO-8601 UTC timestamp
    updated_at TEXT NOT NULL,                  -- ISO-8601 UTC timestamp
    expires_at TEXT                            -- Optional expiration timestamp for TTL pruning
);

CREATE INDEX IF NOT EXISTS idx_memory_type_key ON memory_records(memory_type, key);
CREATE INDEX IF NOT EXISTS idx_memory_created_at ON memory_records(created_at);
CREATE INDEX IF NOT EXISTS idx_memory_updated_at ON memory_records(updated_at);
```

### 2.3 Task State & Checkpointing (`tasks`)
Persists tasks across restarts, supporting state machines and autonomous multi-step execution.

```sql
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,                  -- e.g. 'task-a1b2c3d4'
    conversation_id TEXT NOT NULL,             -- Associated conversation session
    prompt TEXT NOT NULL,                      -- Original user instruction
    state TEXT NOT NULL,                       -- 'pending', 'analyzing', 'awaiting_approval', 'executing', 'streaming', 'completed', 'failed', 'cancelled'
    intent TEXT,                               -- Serialized JSON TaskIntent
    reasoning_depth TEXT NOT NULL,             -- 'instant', 'standard', 'deep'
    model_used TEXT,                           -- Identifier of the model that executed the task
    result TEXT,                               -- Final generated text or summary
    error TEXT,                                -- Error message if failed
    created_at TEXT NOT NULL,                  -- ISO-8601 UTC timestamp
    completed_at TEXT                          -- ISO-8601 UTC timestamp
);

CREATE INDEX IF NOT EXISTS idx_tasks_conversation_id ON tasks(conversation_id);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
```

### 2.4 Conversations (`conversations`)
Groups sequential tasks into conversational context threads.

```sql
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,          -- e.g. 'conv-8f9e0a1b'
    title TEXT NOT NULL DEFAULT 'New Conversation',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at);
```

### 2.5 Model Registry Metadata (`model_registry`)
Local cache of CMS-governed models, capability flags, and health telemetry.

```sql
CREATE TABLE IF NOT EXISTS model_registry (
    model_id TEXT PRIMARY KEY,                 -- e.g. 'qwen3.5:9b'
    name TEXT NOT NULL,                        -- Human-readable name
    provider TEXT NOT NULL DEFAULT 'ollama',   -- Runtime provider
    family TEXT NOT NULL,                      -- Architecture family e.g. 'qwen35'
    parameter_size TEXT NOT NULL,              -- e.g. '9.0B'
    quantization TEXT NOT NULL,                -- e.g. 'Q4_K_M'
    context_length INTEGER NOT NULL,           -- Native context window size
    memory_footprint_mb INTEGER NOT NULL,      -- Disk/RAM footprint estimate
    capabilities TEXT NOT NULL,                -- JSON array of ModelCapability strings
    is_active INTEGER NOT NULL DEFAULT 0,      -- 1 if currently resident in GPU memory
    is_healthy INTEGER NOT NULL DEFAULT 1,     -- 1 if healthy, 0 if degraded/offline
    last_health_check TEXT                     -- ISO-8601 UTC timestamp
);
```

---

## 3. Full-Text Search (FTS5) & Automatic Triggers

Fast semantic/keyword retrieval without running a resource-heavy vector database daemon.

```sql
-- Virtual Table for Fast Text Matching
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    id UNINDEXED,
    key,
    content,
    content='memory_records',
    content_rowid='rowid'
);

-- Automatic Synchronization Triggers
CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory_records BEGIN
    INSERT INTO memory_fts(rowid, id, key, content)
    VALUES (new.rowid, new.id, new.key, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory_records BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, id, key, content)
    VALUES('delete', old.rowid, old.id, old.key, old.content);
END;

CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory_records BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, id, key, content)
    VALUES('delete', old.rowid, old.id, old.key, old.content);
    INSERT INTO memory_fts(rowid, id, key, content)
    VALUES (new.rowid, new.id, new.key, new.content);
END;
```

---

## 4. User-Directed Forgetting Protocol

When a user issues an explicit forgetting instruction (e.g., `"Forget everything about project Apollo"`):

1. **Transaction Isolation:** CMS initiates an immediate database transaction (`BEGIN IMMEDIATE`).
2. **Cascading Deletion:** Records matching the key, conversation, or text query are deleted from `memory_records`.
3. **Triggered FTS Pruning:** SQLite triggers automatically execute deletion from `memory_fts`.
4. **Audit Trail Verification:** An audit log event (`MEMORY_FORGOTTEN`) records the number of purged records and the initiating query, without logging the deleted content.
5. **Commit:** `COMMIT` flushes WAL buffers immediately.
