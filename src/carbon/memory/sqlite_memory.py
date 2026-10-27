"""SQLite-backed four-tier persistent memory implementation."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from carbon.interfaces.models import MemoryRecord, MemoryType
from carbon.interfaces.protocols import IMemoryEngine

logger = logging.getLogger(__name__)


class SQLiteMemoryEngine(IMemoryEngine):
    """Local-first tiered memory engine backed by SQLite WAL mode and FTS5."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Initialize database, apply pragmas, and ensure schemas exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row

        # Apply high-performance, crash-safe pragmas
        await self._conn.execute("PRAGMA journal_mode = WAL;")
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        await self._conn.execute("PRAGMA synchronous = NORMAL;")

        # Base memory table
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_records (
                id TEXT PRIMARY KEY,
                memory_type TEXT NOT NULL,
                key TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence REAL NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT
            );
        """)

        # Indexes for fast lookup
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_type_key ON memory_records(memory_type, key);
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_created_at ON memory_records(created_at);
        """)

        # FTS5 full-text search table
        await self._conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                id UNINDEXED,
                key,
                content,
                content='memory_records',
                content_rowid='rowid'
            );
        """)

        # FTS triggers to synchronize inserts, deletes, and updates
        await self._conn.executescript("""
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
        """)

        await self._conn.commit()
        logger.info(f"Initialized SQLite memory database at {self.db_path}")

    async def close(self) -> None:
        """Close SQLite database connection cleanly."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Closed SQLite memory connection.")

    def _ensure_connected(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database connection is not initialized. Call initialize() first.")
        return self._conn

    async def store(self, record: MemoryRecord) -> str:
        """Persist a memory record."""
        conn = self._ensure_connected()
        metadata_json = json.dumps(record.metadata)
        created_str = record.created_at.isoformat()
        updated_str = record.updated_at.isoformat()
        expires_str = record.expires_at.isoformat() if record.expires_at else None

        await conn.execute(
            """
            INSERT OR REPLACE INTO memory_records (
                id, memory_type, key, content, metadata, source,
                confidence, created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.memory_type.value,
                record.key,
                record.content,
                metadata_json,
                record.source,
                record.confidence,
                created_str,
                updated_str,
                expires_str,
            ),
        )
        await conn.commit()
        return record.id

    async def retrieve(
        self, key: str, memory_type: MemoryType | None = None
    ) -> MemoryRecord | None:
        """Retrieve most recent memory record matching key and optional type."""
        conn = self._ensure_connected()
        query = "SELECT * FROM memory_records WHERE key = ?"
        params: list[Any] = [key]
        if memory_type:
            query += " AND memory_type = ?"
            params.append(memory_type.value)
        query += " ORDER BY updated_at DESC LIMIT 1"

        async with conn.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_record(row)

    async def search(
        self, query: str, memory_type: MemoryType | None = None, limit: int = 10
    ) -> list[MemoryRecord]:
        """Perform full-text search across memories."""
        conn = self._ensure_connected()
        # Escape quotes for FTS syntax
        sanitized_query = query.replace('"', '""')
        sql = """
            SELECT m.* FROM memory_records m
            JOIN memory_fts f ON m.rowid = f.rowid
            WHERE memory_fts MATCH ?
        """
        params: list[Any] = [f'"{sanitized_query}"']
        if memory_type:
            sql += " AND m.memory_type = ?"
            params.append(memory_type.value)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        results: list[MemoryRecord] = []
        try:
            async with conn.execute(sql, params) as cursor:
                async for row in cursor:
                    results.append(self._row_to_record(row))
        except Exception as e:
            logger.warning(f"FTS search fallback to LIKE for query '{query}': {e}")
            # Fallback to simple LIKE search if query syntax fails
            like_sql = "SELECT * FROM memory_records WHERE (key LIKE ? OR content LIKE ?)"
            like_params: list[Any] = [f"%{query}%", f"%{query}%"]
            if memory_type:
                like_sql += " AND memory_type = ?"
                like_params.append(memory_type.value)
            like_sql += " ORDER BY updated_at DESC LIMIT ?"
            like_params.append(limit)
            async with conn.execute(like_sql, like_params) as cursor:
                async for row in cursor:
                    results.append(self._row_to_record(row))

        return results

    async def update(
        self, record_id: str, content: str, metadata: dict[str, Any] | None = None
    ) -> bool:
        """Update an existing memory record."""
        conn = self._ensure_connected()
        now_str = datetime.now(UTC).isoformat()
        if metadata is not None:
            meta_json = json.dumps(metadata)
            res = await conn.execute(
                "UPDATE memory_records SET content = ?, metadata = ?, updated_at = ? WHERE id = ?",
                (content, meta_json, now_str, record_id),
            )
        else:
            res = await conn.execute(
                "UPDATE memory_records SET content = ?, updated_at = ? WHERE id = ?",
                (content, now_str, record_id),
            )
        await conn.commit()
        return res.rowcount > 0

    async def forget(self, record_id: str) -> bool:
        """Permanently delete a memory record by ID."""
        conn = self._ensure_connected()
        res = await conn.execute("DELETE FROM memory_records WHERE id = ?", (record_id,))
        await conn.commit()
        logger.info(f"User-directed forgetting: deleted memory record '{record_id}'")
        return res.rowcount > 0

    async def forget_by_query(self, query: str) -> int:
        """Permanently delete memories matching query."""
        records = await self.search(query, limit=100)
        if not records:
            return 0
        conn = self._ensure_connected()
        ids = [r.id for r in records]
        placeholders = ",".join("?" for _ in ids)
        res = await conn.execute(f"DELETE FROM memory_records WHERE id IN ({placeholders})", ids)
        await conn.commit()
        logger.info(
            f"User-directed forgetting: deleted {res.rowcount} memory records matching '{query}'"
        )
        return res.rowcount

    def _row_to_record(self, row: aiosqlite.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            memory_type=MemoryType(row["memory_type"]),
            key=row["key"],
            content=row["content"],
            metadata=json.loads(row["metadata"]),
            source=row["source"],
            confidence=row["confidence"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        )
