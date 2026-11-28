"""Deterministic Stage 5-C reliability engineering tests (C1-C6)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from carbon.config.settings import DEFAULT_PRIMARY_MODEL, Settings
from carbon.core.orchestrator import CarbonCore
from carbon.governance.governance_engine import (
    GENESIS_AUDIT_HASH,
    AuditChainCorruptionError,
    GovernanceEngine,
    verify_audit_log_chain,
)
from carbon.interfaces.models import (
    MemoryRecord,
    MemoryType,
    ModelCapability,
    ModelMetadata,
    ModelSwitchFailureReason,
    ModelSwitchRequest,
    PersistedTaskRecord,
    TaskRequest,
    TaskState,
)
from carbon.memory.sqlite_memory import (
    LATEST_SCHEMA_VERSION,
    DatabaseIntegrityError,
    SQLiteMemoryEngine,
)
from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.fake_provider import FakeProvider
from carbon.model_control.ollama_provider import OllamaProvider


def _make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    defaults: dict[str, Any] = {
        "data_dir": data_dir,
        "database_path": data_dir / "carbon_test.db",
        "audit_log_path": data_dir / "audit.log",
        "api_token": "stage5c-test-token-value",
        "enable_audit_log": True,
        "maintenance_interval_seconds": 0.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _qwen_meta(model_id: str = DEFAULT_PRIMARY_MODEL, **extra: Any) -> ModelMetadata:
    data: dict[str, Any] = {
        "id": model_id,
        "name": model_id,
        "family": "qwen3.5",
        "parameter_size": "9B",
        "capabilities": [
            ModelCapability.CHAT,
            ModelCapability.COMPLETION,
            ModelCapability.TOOLS,
            ModelCapability.THINKING,
        ],
        "memory_footprint_mb": 6200,
        "is_healthy": True,
    }
    data.update(extra)
    return ModelMetadata(**data)


# ==============================================================================
# C1 — Async generator cleanup & lease safety
# ==============================================================================


@pytest.mark.asyncio
async def test_c1_streaming_generator_closed_before_lease_release_on_cancel_and_close(
    tmp_path: Path,
) -> None:
    """Verify aclosing closes provider stream deterministically BEFORE lease release."""
    core = CarbonCore(settings=_make_settings(tmp_path))
    provider = FakeProvider(healthy=True, models=[_qwen_meta()])
    core.provider = provider
    core.cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        approved_models=(DEFAULT_PRIMARY_MODEL,),
        governance=core.governance,
    )
    await core.initialize()

    try:
        events_order: list[str] = []
        first_chunk_emitted = asyncio.Event()

        async def tracked_stream(**_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
            try:
                yield {"response": "chunk1", "thinking": "", "done": False}
                first_chunk_emitted.set()
                await asyncio.Event().wait()  # block until cancelled or closed
            finally:
                events_order.append("generator_closed")

        provider.generate_stream_events = tracked_stream  # type: ignore[method-assign]
        orig_release = core.cms.release_inference_lease

        async def wrapped_release(tid: str) -> None:
            events_order.append("lease_released")
            await orig_release(tid)

        core.cms.release_inference_lease = wrapped_release  # type: ignore[method-assign]

        # Scenario A: early consumer exit via GeneratorExit (aclose)
        task_a = await core.create_task(TaskRequest(prompt="Stream and close early"))
        stream_a = core.execute_task_stream(task_a)
        seen_token = False
        async for evt in stream_a:
            if evt.get("event") == "token":
                seen_token = True
                break
        await stream_a.aclose()
        assert seen_token is True
        assert events_order == ["generator_closed", "lease_released"]
        assert core.cms._active_leases == set()
        assert task_a.state == TaskState.CANCELLED

        # Scenario B: task cancellation via CancelledError while awaiting inside generator
        events_order.clear()
        first_chunk_emitted.clear()
        task_b = await core.create_task(TaskRequest(prompt="Stream and cancel task"))

        async def consume_b() -> None:
            async for _ in core.execute_task_stream(task_b):
                pass

        runner = asyncio.create_task(consume_b())
        await first_chunk_emitted.wait()
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

        assert events_order == ["generator_closed", "lease_released"]
        assert core.cms._active_leases == set()
        assert task_b.state == TaskState.CANCELLED
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_c1_ollama_provider_closes_ndjson_inner_generator_on_early_break() -> None:
    """Verify OllamaProvider.generate_stream_events closes inner _stream_ndjson_dicts via aclosing."""
    provider = OllamaProvider()
    inner_closed = asyncio.Event()

    async def fake_ndjson(
        _ep: str, _payload: dict[str, Any], *, model_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        assert model_id == DEFAULT_PRIMARY_MODEL
        try:
            yield {"response": "part-1", "done": False}
            yield {"response": "part-2", "done": False}
            yield {"response": "", "done": True}
        finally:
            inner_closed.set()

    provider._stream_ndjson_dicts = fake_ndjson  # type: ignore[method-assign]
    gen = provider.generate_stream_events(DEFAULT_PRIMARY_MODEL, "hi")
    first = await anext(gen)
    assert first["response"] == "part-1"
    await gen.aclose()
    assert inner_closed.is_set() is True


# ==============================================================================
# C2 — Task registry & safe degraded shutdown
# ==============================================================================


@pytest.mark.asyncio
async def test_c2_task_registry_cleans_completed_tasks_and_retrieves_exceptions(
    tmp_path: Path,
) -> None:
    """Verify completed/failing tracked tasks are bounded and exceptions are retrieved cleanly."""
    core = CarbonCore(settings=_make_settings(tmp_path))
    await core.initialize()

    done_event = asyncio.Event()
    fail_event = asyncio.Event()

    async def ok_job() -> None:
        done_event.set()

    async def failing_job() -> None:
        fail_event.set()
        raise RuntimeError("boom inside background job")

    try:
        t1 = core.create_tracked_task(ok_job(), name="ok-job")
        t2 = core.create_tracked_task(failing_job(), name="failing-job")
        await asyncio.gather(done_event.wait(), fail_event.wait())
        await asyncio.gather(t1, return_exceptions=True)
        await asyncio.gather(t2, return_exceptions=True)
        # Allow done callbacks to fire on event loop
        await asyncio.sleep(0)
        assert core.tracked_task_count == 0
    finally:
        report = await core.shutdown()
        assert report["degraded"] is False
        assert report["resources_kept_open"] is False
        assert core.memory._conn is None


@pytest.mark.asyncio
async def test_c2_shutdown_degraded_preserves_sqlite_and_http_client_when_task_refuses_cancel(
    tmp_path: Path,
) -> None:
    """Verify degraded shutdown keeps SQLite + shared HTTP client open when tasks survive cancel."""
    core = CarbonCore(
        settings=_make_settings(tmp_path),
        shutdown_grace_seconds=0.01,
        shutdown_cancel_grace_seconds=0.01,
    )
    provider = FakeProvider(healthy=True, models=[_qwen_meta()])
    aclose_calls = 0

    async def tracked_aclose() -> None:
        nonlocal aclose_calls
        aclose_calls += 1

    provider.aclose = tracked_aclose  # type: ignore[method-assign]
    core.provider = provider
    core.cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        approved_models=(DEFAULT_PRIMARY_MODEL,),
        governance=core.governance,
    )
    await core.initialize()

    started = asyncio.Event()
    release_stubborn = asyncio.Event()

    async def stubborn_task() -> None:
        started.set()
        while not release_stubborn.is_set():
            try:
                await release_stubborn.wait()
            except asyncio.CancelledError:
                # Explicitly refuse cancellation cleanly to test degraded shutdown
                if release_stubborn.is_set():
                    raise

    t_stubborn = core.create_tracked_task(stubborn_task(), name="stubborn-worker")
    await started.wait()
    await core.cms.acquire_inference_lease("task-surviving-lease")

    report = await core.shutdown()
    assert report["status"] == "degraded"
    assert report["degraded"] is True
    assert "stubborn-worker" in report["unresolved_tasks"]
    assert "task-surviving-lease" in report["unresolved_leases"]
    assert report["resources_kept_open"] is True
    assert core.shutdown_degraded is True
    assert core.is_accepting_tasks is False

    # Critical safety invariant: neither SQLite nor shared provider transport was closed
    assert core.memory._conn is not None
    assert aclose_calls == 0

    # New tasks rejected once shutting down
    with pytest.raises(RuntimeError, match="shutting down"):
        core.create_tracked_task(asyncio.sleep(0), name="late-task")

    ok_chain, _, err = verify_audit_log_chain(core.settings.audit_log_path)
    assert ok_chain is True, err

    # Clean up test task and resources safely afterwards
    release_stubborn.set()
    await asyncio.gather(t_stubborn, return_exceptions=True)
    await core.cms.release_inference_lease("task-surviving-lease")
    await core.memory.close()


# ==============================================================================
# C3 — CMS ownership tokens & non-deadlocking deferred unload
# ==============================================================================


@pytest.mark.asyncio
async def test_c3_ownership_tokens_and_concurrent_switch_deferred_unload_safety() -> None:
    """Verify ownership tokens isolate callers and deferred unload runs deadlock-free outside _inference_lock."""
    model_a = _qwen_meta(DEFAULT_PRIMARY_MODEL)
    model_b = _qwen_meta("qwen-coder:7b", family="qwen2.5", memory_footprint_mb=4800)
    provider = FakeProvider(
        healthy=True,
        models=[model_a, model_b],
        running_models={DEFAULT_PRIMARY_MODEL},
    )
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        approved_models=(DEFAULT_PRIMARY_MODEL, "qwen-coder:7b"),
    )
    await cms.initialize()

    # 1. Distinct (kind, generation) tokens prevent one operation from clearing another
    tok_1 = cms._acquire_pending_owner("switch")
    tok_2 = cms._acquire_pending_owner("deferred_unload")
    assert tok_1 != tok_2
    assert cms._switch_pending is True
    cms._release_pending_owner(tok_1)
    assert cms._switch_pending is True
    assert not cms._switch_clear_event.is_set()
    cms._release_pending_owner(tok_2)
    assert cms._switch_pending is False
    assert cms._switch_clear_event.is_set()

    # 2. Deferred unload releases _inference_lock before unload I/O, blocks queued lease waiter,
    #    and survives unload cancellation/failure cleanly without leaking tokens or locks
    unload_entered = asyncio.Event()
    allow_unload_finish = asyncio.Event()
    original_unload = provider.unload_model

    async def gated_unload(model_id: str) -> bool:
        assert cms._inference_lock.locked() is False
        assert cms._switch_pending is True
        unload_entered.set()
        await allow_unload_finish.wait()
        return await original_unload(model_id)

    provider.unload_model = gated_unload  # type: ignore[method-assign]

    await cms.acquire_inference_lease("active-1")
    await cms.handle_memory_pressure(4)
    assert cms._deferred_critical_unload is True

    release_task = asyncio.create_task(cms.release_inference_lease("active-1"))
    await unload_entered.wait()

    # A new waiter must stay blocked on _switch_pending during unloaded transition
    lease_waiter_acquired = asyncio.Event()

    async def queued_lease() -> None:
        await cms.acquire_inference_lease("queued-2", timeout=5.0)
        lease_waiter_acquired.set()
        await cms.release_inference_lease("queued-2")

    waiter_task = asyncio.create_task(queued_lease())
    await asyncio.sleep(0)
    assert lease_waiter_acquired.is_set() is False

    allow_unload_finish.set()
    await release_task
    await waiter_task
    assert cms._pending_owners == set()
    assert cms._switch_pending is False
    assert cms._deferred_critical_unload is False

    # 3. Failing / cancelled switch cleans up ONLY its own ownership token
    provider.unload_model = original_unload  # type: ignore[method-assign]
    await cms.handle_memory_pressure(1)
    await cms.refresh_models(probe_health=True)

    cms.drain_timeout_seconds = 0.02
    await cms.acquire_inference_lease("blocker")
    timed_out = await cms.switch_model(
        ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="drain timeout test")
    )
    assert timed_out is False
    assert cms.last_switch_failure == ModelSwitchFailureReason.DRAIN_TIMEOUT
    assert cms._pending_owners == set()
    assert cms._switch_pending is False
    await cms.release_inference_lease("blocker")


# ==============================================================================
# C4 & C5 — SQLite serialization, rollback, integrity, backup, and v3 migration
# ==============================================================================


@pytest.mark.asyncio
async def test_c4_sqlite_read_write_serialization_and_cancellation_rollback(
    tmp_path: Path,
) -> None:
    """Verify _db_lock prevents transaction interleaving and rolls back cleanly on error/cancel."""
    engine = SQLiteMemoryEngine(db_path=tmp_path / "c4_conc.db")
    await engine.initialize()
    try:
        # 1. Concurrent reads and multi-statement writes serialize cleanly without transaction errors
        async def writer(idx: int) -> None:
            rec = MemoryRecord(
                id=f"mem-{idx}",
                memory_type=MemoryType.EPISODIC,
                key=f"k:{idx}",
                content=f"concurrent entry {idx}",
                metadata={"conversation_id": f"conv-{idx % 3}"},
            )
            await engine.store(rec)
            await engine.save_task(
                PersistedTaskRecord(
                    task_id=f"t-{idx}",
                    conversation_id=f"conv-{idx % 3}",
                    prompt=f"prompt {idx}",
                    state=TaskState.COMPLETED,
                )
            )

        async def reader(idx: int) -> None:
            await engine.retrieve(f"k:{idx % 5}")
            await engine.list_memories(conversation_id=f"conv-{idx % 3}")
            await engine.list_tasks(conversation_id=f"conv-{idx % 3}")

        await asyncio.gather(
            *(writer(i) for i in range(15)),
            *(reader(i) for i in range(15)),
        )

        # 2. Rollback on exception and CancelledError leaves conn.in_transaction False
        conn = engine._require_conn()
        with pytest.raises(RuntimeError, match="mid-tx error"):
            async with engine._write_transaction(conn):
                await conn.execute(
                    "INSERT INTO conversations (conversation_id, title, created_at, updated_at, metadata) "
                    "VALUES ('should-rollback-1', 'T', '2026-01-01', '2026-01-01', '{}')"
                )
                raise RuntimeError("mid-tx error")
        assert conn.in_transaction is False
        assert await engine.get_conversation("should-rollback-1") is None

        tx_entered = asyncio.Event()

        async def cancelled_tx() -> None:
            async with engine._write_transaction(conn):
                await conn.execute(
                    "INSERT INTO conversations (conversation_id, title, created_at, updated_at, metadata) "
                    "VALUES ('should-rollback-2', 'T', '2026-01-01', '2026-01-01', '{}')"
                )
                tx_entered.set()
                await asyncio.Event().wait()

        c_task = asyncio.create_task(cancelled_tx())
        await tx_entered.wait()
        c_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await c_task
        assert conn.in_transaction is False
        assert await engine.get_conversation("should-rollback-2") is None
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_c5_corrupt_db_forensic_preservation_and_online_backup(tmp_path: Path) -> None:
    """Verify corrupt DB fails closed preserving untouched originals + forensic copies, and online backup works."""
    # 1. Corrupt existing DB + WAL sibling -> fails closed, original untouched, forensic preserved
    corrupt_db = tmp_path / "corrupt.db"
    corrupt_wal = tmp_path / "corrupt.db-wal"
    orig_bytes = b"not-a-valid-sqlite-database-file-header"
    wal_bytes = b"corrupt-wal-frames"
    corrupt_db.write_bytes(orig_bytes)
    corrupt_wal.write_bytes(wal_bytes)

    failing_engine = SQLiteMemoryEngine(db_path=corrupt_db)
    with pytest.raises(DatabaseIntegrityError, match="forensic copy preserved"):
        await failing_engine.initialize()

    assert corrupt_db.read_bytes() == orig_bytes
    assert corrupt_wal.read_bytes() == wal_bytes
    forensic_dirs = list((tmp_path / "forensics").glob("corrupt_*"))
    assert len(forensic_dirs) == 1
    assert (forensic_dirs[0] / "corrupt.db").read_bytes() == orig_bytes
    assert (forensic_dirs[0] / "corrupt.db-wal").read_bytes() == wal_bytes

    # 2. Healthy DB -> online backup verifies cleanly and holds consistent data
    healthy_db = tmp_path / "healthy.db"
    engine = SQLiteMemoryEngine(db_path=healthy_db)
    await engine.initialize()
    try:
        await engine.store(
            MemoryRecord(
                id="bkp-1",
                memory_type=MemoryType.SEMANTIC,
                key="user:preference:tea",
                content="oolong",
                metadata={"conversation_id": "conv-bkp"},
            )
        )
        ok, msgs = await engine.check_integrity()
        assert ok is True and msgs == ["ok"]

        backup_path = await engine.create_online_backup()
        assert backup_path.exists()
        with sqlite3.connect(backup_path) as bconn:
            assert bconn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            row = bconn.execute(
                "SELECT content, conversation_id FROM memory_records WHERE id='bkp-1'"
            ).fetchone()
            assert row == ("oolong", "conv-bkp")
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_c5_v3_migration_backfill_rollback_and_idempotency(tmp_path: Path) -> None:
    """Verify v3 adds indexed conversation_id cleanly, rolls back on failure, and is idempotent."""
    db_path = tmp_path / "mig_v2_to_v3.db"
    now_iso = datetime.now(UTC).isoformat()

    # Build a valid v2 database (without v3 column/index) with existing rows
    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
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
                expires_at TEXT
            );
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'Conversation',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
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
                payload_expires_at TEXT
            );
            PRAGMA user_version = 2;
            """
        )
        await raw.execute(
            "INSERT INTO schema_migrations VALUES (1, 'initial_memory_schema', ?), "
            "(2, 'conversations_and_tasks_schema', ?)",
            (now_iso, now_iso),
        )
        await raw.execute(
            "INSERT INTO conversations VALUES ('conv-v2', 'Conversation', ?, ?, NULL, '{}')",
            (now_iso, now_iso),
        )
        await raw.execute(
            "INSERT INTO memory_records VALUES ('v2-ep-1', 'episodic', 'task:1', 'hello', ?, "
            "'user_interaction', 1.0, ?, ?, NULL)",
            (json.dumps({"conversation_id": "conv-v2"}), now_iso, now_iso),
        )
        await raw.commit()

    # 1. Injected v3 failure rolls back cleanly without partial schema/user_version change
    failing_v3 = SQLiteMemoryEngine(db_path=db_path, fail_migration_version=3)
    with pytest.raises(RuntimeError, match="Simulated migration failure at version 3"):
        await failing_v3.initialize()

    with sqlite3.connect(db_path) as check_conn:
        assert check_conn.execute("PRAGMA user_version").fetchone()[0] == 2
        cols = [r[1] for r in check_conn.execute("PRAGMA table_info(memory_records)").fetchall()]
        assert "conversation_id" not in cols
        assert check_conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 1

    # 2. Clean v3 upgrade succeeds, backfills conversation_id, creates index, and queries match
    ok_engine = SQLiteMemoryEngine(db_path=db_path)
    await ok_engine.initialize()
    try:
        conn = ok_engine._require_conn()
        async with conn.execute("PRAGMA user_version") as cur:
            assert int((await cur.fetchone())[0]) == LATEST_SCHEMA_VERSION == 3
        async with conn.execute("PRAGMA table_info(memory_records)") as cur:
            cols = [str(r["name"]) for r in await cur.fetchall()]
            assert "conversation_id" in cols
        items, total = await ok_engine.list_memories(conversation_id="conv-v2")
        assert total == 1 and items[0].id == "v2-ep-1"
        turns = await ok_engine.get_recent_conversation_turns("conv-v2")
        assert len(turns) == 1 and turns[0].id == "v2-ep-1"
    finally:
        await ok_engine.close()

    # 3. Re-opening stays idempotent at schema v3
    again = SQLiteMemoryEngine(db_path=db_path)
    await again.initialize()
    await again.close()


# ==============================================================================
# C6 — Audit-chain recovery, WAL checkpointing, & periodic maintenance
# ==============================================================================


@pytest.mark.asyncio
async def test_c6_audit_tail_recovery_fails_loudly_on_corruption_and_handles_large_files(
    tmp_path: Path,
) -> None:
    """Verify corrupted/missing audit tails fail loudly rather than silently resetting to GENESIS."""
    # 1. Valid multi-chunk (>16KB) audit log recovers exact tail hash cleanly
    audit_path = tmp_path / "large_audit.log"
    gov = GovernanceEngine(audit_log_path=audit_path, enable_audit=True)
    await gov.initialize()
    assert gov._last_audit_hash == GENESIS_AUDIT_HASH

    big_payload = "x" * 2048
    for i in range(12):
        await gov.record_audit_entry("BULK_EVENT", {"idx": i, "blob": big_payload})
    expected_tail = gov._last_audit_hash
    assert expected_tail != GENESIS_AUDIT_HASH
    assert audit_path.stat().st_size > 16384

    gov_resumed = GovernanceEngine(audit_log_path=audit_path, enable_audit=True)
    await gov_resumed.initialize()
    assert gov_resumed._last_audit_hash == expected_tail

    # 2. Tampered tail entry fails loudly
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    last_entry = json.loads(lines[-1])
    last_entry["event_type"] = "TAMPERED_EVENT"
    lines[-1] = json.dumps(last_entry)
    tampered_path = tmp_path / "tampered_audit.log"
    tampered_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(AuditChainCorruptionError, match="verification failed"):
        await GovernanceEngine(audit_log_path=tampered_path, enable_audit=True).initialize()

    # 3. Truncated/corrupt JSON line fails loudly
    trunc_path = tmp_path / "truncated_audit.log"
    trunc_path.write_text('{"timestamp": "2026-01-01", "broken": ', encoding="utf-8")
    with pytest.raises(AuditChainCorruptionError, match="Corrupt JSONL"):
        await GovernanceEngine(audit_log_path=trunc_path, enable_audit=True).initialize()

    # 4. Non-empty whitespace-only file fails loudly
    blank_path = tmp_path / "whitespace_audit.log"
    blank_path.write_text("   \n  \n", encoding="utf-8")
    with pytest.raises(AuditChainCorruptionError, match="refusing silent reset"):
        await GovernanceEngine(audit_log_path=blank_path, enable_audit=True).initialize()


@pytest.mark.asyncio
async def test_c6_periodic_maintenance_purges_and_checkpoints_without_interfering_with_shutdown(
    tmp_path: Path,
) -> None:
    """Verify periodic TTL purge + WAL checkpoint serializes cleanly and exits immediately on shutdown."""
    settings = _make_settings(tmp_path, maintenance_interval_seconds=0.01)
    core = CarbonCore(settings=settings)
    await core.initialize()

    loop_ran = asyncio.Event()
    orig_cycle = core.run_maintenance_cycle

    async def spied_cycle() -> dict[str, Any]:
        res = await orig_cycle()
        loop_ran.set()
        return res

    core.run_maintenance_cycle = spied_cycle  # type: ignore[method-assign]

    try:
        # Insert an already-expired memory record
        past = datetime.now(UTC) - timedelta(hours=2)
        await core.memory.store(
            MemoryRecord(
                id="expired-maint-1",
                memory_type=MemoryType.EPISODIC,
                key="k:expired",
                content="stale turn",
                expires_at=past,
            )
        )
        await asyncio.wait_for(loop_ran.wait(), timeout=2.0)
        assert await core.memory.retrieve("expired-maint-1") is None

        # Best-effort WAL checkpoint handles busy/locked cleanly, but surfaces genuine DB corruption
        orig_conn = core.memory._require_conn()
        try:

            class _LockedConn:
                @asynccontextmanager
                async def execute(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
                    raise sqlite3.OperationalError("database is locked")
                    yield

            core.memory._conn = _LockedConn()  # type: ignore[assignment]
            busy_res = await core.memory.checkpoint_wal("PASSIVE")
            assert busy_res["busy"] == 1

            class _CorruptConn:
                @asynccontextmanager
                async def execute(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
                    raise sqlite3.DatabaseError("database disk image is malformed")
                    yield

            core.memory._conn = _CorruptConn()  # type: ignore[assignment]
            with pytest.raises(sqlite3.DatabaseError, match="malformed"):
                await core.memory.checkpoint_wal("PASSIVE")
        finally:
            core.memory._conn = orig_conn
        assert core._maintenance_task is not None
    finally:
        report = await core.shutdown()
        assert report["degraded"] is False
        assert report["resources_kept_open"] is False
        assert core.tracked_task_count == 0
