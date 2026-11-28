"""Deterministic SQLite memory and task persistence benchmarks (Stage 5-A)."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from carbon.core.metrics import summarize_distribution
from carbon.interfaces.models import (
    MemoryRecord,
    MemoryType,
    PersistedTaskRecord,
    ReasoningDepth,
    TaskState,
)
from carbon.memory.sqlite_memory import SQLiteMemoryEngine


@pytest.mark.asyncio
async def test_benchmark_sqlite_memory_and_task_operations(tmp_path: Path) -> None:
    """Seed 1,000 memory rows + 200 tasks in temp DB and measure NIST read/write distributions."""
    db_path = tmp_path / "perf_memory.db"
    engine = SQLiteMemoryEngine(db_path=db_path)
    await engine.initialize()
    try:
        # Seed 1,000 mixed semantic/episodic records across 20 conversations
        for idx in range(1000):
            conv_id = f"conv-{idx % 20:03d}"
            mem_type = MemoryType.SEMANTIC if idx % 3 == 0 else MemoryType.EPISODIC
            key = (
                f"user:preference:item_{idx}" if mem_type == MemoryType.SEMANTIC else f"task:{idx}"
            )
            content = (
                f"User preference item {idx}: favorite language python topic_{idx % 25}"
                if mem_type == MemoryType.SEMANTIC
                else json.dumps(
                    {
                        "user": f"Explain topic {idx % 25}",
                        "assistant": f"Detailed answer about python topic_{idx % 25}",
                    }
                )
            )
            await engine.store(
                MemoryRecord(
                    id=str(uuid.uuid4()),
                    memory_type=mem_type,
                    key=key,
                    content=content,
                    metadata={"conversation_id": conv_id},
                    source="benchmark_seed",
                )
            )

        for idx in range(200):
            now = datetime.now(UTC)
            await engine.save_task(
                PersistedTaskRecord(
                    task_id=f"task-seed-{idx:04d}",
                    conversation_id=f"conv-{idx % 20:03d}",
                    prompt=f"Prompt {idx}",
                    state=TaskState.COMPLETED,
                    reasoning_depth=ReasoningDepth.INSTANT,
                    model_used="qwen3.5:9b",
                    result="ok",
                    created_at=now,
                    completed_at=now,
                )
            )

        retrieve_ms: list[float] = []
        search_ms: list[float] = []
        turns_ms: list[float] = []
        save_task_ms: list[float] = []

        for idx in range(50):
            t0 = time.perf_counter()
            rec = await engine.retrieve(
                f"user:preference:item_{(idx * 3) % 999}",
                memory_type=MemoryType.SEMANTIC,
            )
            retrieve_ms.append((time.perf_counter() - t0) * 1000.0)
            assert rec is not None

            t1 = time.perf_counter()
            found = await engine.search(
                f"python topic_{idx % 25}",
                memory_type=MemoryType.SEMANTIC,
                limit=5,
            )
            search_ms.append((time.perf_counter() - t1) * 1000.0)
            assert len(found) > 0

            t2 = time.perf_counter()
            turns = await engine.get_recent_conversation_turns(
                conversation_id=f"conv-{idx % 20:03d}",
                limit=3,
            )
            turns_ms.append((time.perf_counter() - t2) * 1000.0)
            assert len(turns) > 0

            t3 = time.perf_counter()
            now = datetime.now(UTC)
            await engine.save_task(
                PersistedTaskRecord(
                    task_id=f"task-bench-{idx:04d}",
                    conversation_id=f"conv-{idx % 20:03d}",
                    prompt="Benchmark save",
                    state=TaskState.COMPLETED,
                    reasoning_depth=ReasoningDepth.INSTANT,
                    model_used="qwen3.5:9b",
                    result="ok",
                    created_at=now,
                    completed_at=now,
                )
            )
            save_task_ms.append((time.perf_counter() - t3) * 1000.0)

        summary = {
            "retrieve_ms": summarize_distribution(retrieve_ms),
            "search_ms": summarize_distribution(search_ms),
            "recent_turns_ms": summarize_distribution(turns_ms),
            "save_task_ms": summarize_distribution(save_task_ms),
        }
        print(f"\n[Stage 5-A SQLite 1k-Row Baseline (N=50)] {json.dumps(summary)}")

        for key_name, stats in summary.items():
            assert stats["count"] == 50, key_name
            assert stats["p50"] is not None
            assert stats["p95"] is not None

        # Stage 5-E Tier 1 hard gate (E2): recent-turn retrieval P95 <= 20 ms at 1,000 rows
        # (retrieve_ms, search_ms, and save_task_ms remain informational baseline metrics).
        assert summary["recent_turns_ms"]["p95"] <= 20.0
    finally:
        await engine.close()
