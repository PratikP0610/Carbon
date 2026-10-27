"""Unit tests for SQLite tiered memory engine."""

import uuid
from pathlib import Path

import pytest

from carbon.interfaces.models import MemoryRecord, MemoryType
from carbon.memory.sqlite_memory import SQLiteMemoryEngine


@pytest.fixture
async def memory_engine(tmp_path: Path):
    db_file = tmp_path / "test_carbon.db"
    engine = SQLiteMemoryEngine(db_path=db_file)
    await engine.initialize()
    yield engine
    await engine.close()


@pytest.mark.asyncio
async def test_memory_store_and_retrieve(memory_engine: SQLiteMemoryEngine):
    """Store and retrieve memory records across types."""
    rec = MemoryRecord(
        id=str(uuid.uuid4()),
        memory_type=MemoryType.SEMANTIC,
        key="user:preference:theme",
        content="User prefers sleek dark mode with high contrast.",
        metadata={"category": "preferences"},
        source="unit_test",
    )
    rec_id = await memory_engine.store(rec)
    assert rec_id == rec.id

    fetched = await memory_engine.retrieve(
        key="user:preference:theme", memory_type=MemoryType.SEMANTIC
    )
    assert fetched is not None
    assert fetched.key == "user:preference:theme"
    assert "sleek dark mode" in fetched.content
    assert fetched.metadata.get("category") == "preferences"


@pytest.mark.asyncio
async def test_memory_fts_search(memory_engine: SQLiteMemoryEngine):
    """Test full-text keyword retrieval via SQLite FTS5."""
    records = [
        MemoryRecord(
            id=str(uuid.uuid4()),
            memory_type=MemoryType.EPISODIC,
            key="conv:101",
            content="Discussion about migrating the iOS project to Swift 6 and Metal.",
        ),
        MemoryRecord(
            id=str(uuid.uuid4()),
            memory_type=MemoryType.PROCEDURAL,
            key="workflow:deploy",
            content="Deploying FastAPI service to loopback port 8765 on Apple Silicon.",
        ),
    ]
    for r in records:
        await memory_engine.store(r)

    # Search for "Metal"
    results = await memory_engine.search("Metal")
    assert len(results) == 1
    assert results[0].key == "conv:101"

    # Search for "FastAPI"
    results_api = await memory_engine.search("FastAPI")
    assert len(results_api) == 1
    assert results_api[0].key == "workflow:deploy"


@pytest.mark.asyncio
async def test_user_directed_forgetting(memory_engine: SQLiteMemoryEngine):
    """Verify explicit forgetting deletes records permanently."""
    rec = MemoryRecord(
        id=str(uuid.uuid4()),
        memory_type=MemoryType.EPISODIC,
        key="temp:note",
        content="Temporary sensitive note to be forgotten.",
    )
    await memory_engine.store(rec)

    # Verify present
    assert await memory_engine.retrieve("temp:note") is not None

    # User-directed forget by ID
    deleted = await memory_engine.forget(rec.id)
    assert deleted is True

    # Verify gone
    assert await memory_engine.retrieve("temp:note") is None
