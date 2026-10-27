# ADR-0004: SQLite for Structured Persistence & Tiered Memory

## Status
Approved

## Context
Carbon requires robust local persistence across restarts for conversation sessions, tasks, four-tier memory (working, episodic, semantic, procedural), model metadata, and user settings.
Options considered:
1. Flat JSON files on disk.
2. Embedded key-value store (LevelDB, RocksDB, LMDB).
3. SQLite (via `aiosqlite` with WAL mode enabled).
4. External database server (PostgreSQL, Redis).

## Decision
We select **SQLite** with Write-Ahead Logging (`PRAGMA journal_mode=WAL`) and foreign key constraints enabled (`PRAGMA foreign_keys=ON`) as the sole structured database engine for Carbon 1.0.0.

- Database path: `data/carbon.db`.
- Schema migrations: Version-tracked schema migrations executed on startup.
- Asynchronous interface: Accessed via `aiosqlite` to prevent event-loop blocking during I/O.
- Memory tiering: SQLite tables partition records into `working`, `episodic`, `semantic`, and `procedural` partitions, with full-text search (`FTS5`) enabled for keyword retrieval.

## Consequences
### Positive:
- Zero external database services to manage, install, or keep running.
- ACID compliance and crash safety via WAL mode.
- High performance (sub-millisecond indexed reads, easily satisfying the P95 <= 100 ms memory retrieval target).
- Built-in FTS5 full-text indexing provides fast semantic/keyword search without requiring external vector DB daemons in early phases.

### Negative / Trade-offs:
- Future complex vector similarity search will require a pluggable embedding extension (e.g. `sqlite-vec` or local FAISS index) rather than built-in vector primitives.
