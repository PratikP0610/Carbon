"""Stage 6.9-F regression tests for SQLite cancellation safety, WS cleanup, and episodic persistence."""

import asyncio
import json
import logging
import socket
from pathlib import Path
from typing import Any

import pytest
import uvicorn
import websockets

import carbon.main as main_module
from carbon.config.settings import Settings, get_settings
from carbon.core.orchestrator import CarbonCore
from carbon.interfaces.models import (
    MemoryRecord,
    MemoryType,
    PersistedTaskRecord,
    TaskRequest,
    TaskState,
)
from carbon.memory.sqlite_memory import SQLiteMemoryEngine
from carbon.model_control.fake_provider import FakeProvider


def _find_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _assert_db_healthy_and_clean(engine: SQLiteMemoryEngine) -> None:
    conn = engine._require_conn()
    assert conn.in_transaction is False
    ok, messages = await engine.check_integrity()
    assert ok is True, f"SQLite integrity check failed: {messages}"
    async with engine._db_lock:
        async with conn.execute("PRAGMA foreign_key_check") as cur:
            fk_rows = await cur.fetchall()
    assert fk_rows == []


@pytest.mark.asyncio
async def test_cancel_during_begin_immediate_leaves_no_open_tx_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation while BEGIN IMMEDIATE is queued must hold lock until resolved, roll back, and recover."""
    db_path = tmp_path / "begin_cancel.db"
    engine = SQLiteMemoryEngine(db_path)
    await engine.initialize()
    conn = engine._require_conn()

    try:
        begin_started = asyncio.Event()
        release_begin = asyncio.Event()
        orig_inner = conn._execute

        async def gated_inner(fn: Any, *args: Any, **kwargs: Any) -> Any:
            if args and isinstance(args[0], str) and args[0].strip().upper() == "BEGIN IMMEDIATE":
                res = await orig_inner(fn, *args, **kwargs)
                begin_started.set()
                await release_begin.wait()
                return res
            return await orig_inner(fn, *args, **kwargs)

        monkeypatch.setattr(conn, "_execute", gated_inner)

        async def do_store() -> None:
            await engine.store(
                MemoryRecord(
                    id="rec-begin-cancel",
                    memory_type=MemoryType.EPISODIC,
                    key="k:begin-cancel",
                    content="should never commit",
                )
            )

        task = asyncio.create_task(do_store())
        await begin_started.wait()
        task.cancel()
        await asyncio.sleep(0)

        # Lock MUST remain held while BEGIN IMMEDIATE is unresolved
        assert engine._db_lock.locked() is True
        release_begin.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert engine._db_lock.locked() is False
        assert conn.in_transaction is False
        assert await engine.retrieve("rec-begin-cancel") is None

        # Restore _execute and confirm immediate follow-up write + integrity pass
        monkeypatch.setattr(conn, "_execute", orig_inner)
        stored_id = await engine.store(
            MemoryRecord(
                id="rec-after-begin-cancel",
                memory_type=MemoryType.EPISODIC,
                key="k:after-begin-cancel",
                content="persisted cleanly",
            )
        )
        assert stored_id == "rec-after-begin-cancel"
        assert (await engine.retrieve("rec-after-begin-cancel")) is not None
        await _assert_db_healthy_and_clean(engine)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_cancel_during_transaction_body_rolls_back_and_recovers(tmp_path: Path) -> None:
    """Cancellation inside the transaction body must roll back completely and allow subsequent writes."""
    engine = SQLiteMemoryEngine(tmp_path / "body_cancel.db")
    await engine.initialize()
    conn = engine._require_conn()

    try:
        body_entered = asyncio.Event()
        blocker = asyncio.Event()

        async def body_tx() -> None:
            async with engine._db_lock:
                async with engine._write_transaction(conn):
                    await conn.execute(
                        "INSERT INTO conversations (conversation_id, title, created_at, updated_at, metadata) "
                        "VALUES ('conv-body-cancel', 'Cancelled', '2026-01-01', '2026-01-01', '{}')"
                    )
                    body_entered.set()
                    await blocker.wait()

        task = asyncio.create_task(body_tx())
        await body_entered.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert engine._db_lock.locked() is False
        assert conn.in_transaction is False
        assert await engine.get_conversation("conv-body-cancel") is None

        rec = await engine.upsert_conversation("conv-after-body-cancel", title="Clean")
        assert rec.conversation_id == "conv-after-body-cancel"
        await _assert_db_healthy_and_clean(engine)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_cancel_during_commit_resolves_atomically_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during COMMIT must hold the lock until commit finishes without ambiguous state."""
    engine = SQLiteMemoryEngine(tmp_path / "commit_cancel.db")
    await engine.initialize()
    conn = engine._require_conn()

    try:
        commit_started = asyncio.Event()
        release_commit = asyncio.Event()
        orig_commit = conn.commit

        async def gated_commit() -> None:
            commit_started.set()
            await release_commit.wait()
            await orig_commit()

        monkeypatch.setattr(conn, "commit", gated_commit)

        async def do_store() -> None:
            await engine.store(
                MemoryRecord(
                    id="rec-commit-cancel",
                    memory_type=MemoryType.SEMANTIC,
                    key="user:test:commit_cancel",
                    content="atomic full record",
                    metadata={"conversation_id": "conv-commit-cancel"},
                )
            )

        task = asyncio.create_task(do_store())
        await commit_started.wait()
        task.cancel()
        await asyncio.sleep(0)

        # Lock MUST stay held until COMMIT finishes on SQLite
        assert engine._db_lock.locked() is True
        release_commit.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert engine._db_lock.locked() is False
        assert conn.in_transaction is False

        # Transaction committed atomically (either fully present with valid FK, never half-written)
        conv = await engine.get_conversation("conv-commit-cancel")
        mem = await engine.retrieve("rec-commit-cancel")
        assert (conv is None) == (mem is None)

        monkeypatch.setattr(conn, "commit", orig_commit)
        next_id = await engine.store(
            MemoryRecord(
                id="rec-after-commit-cancel",
                memory_type=MemoryType.EPISODIC,
                key="k:after-commit-cancel",
                content="follow-up write ok",
            )
        )
        assert next_id == "rec-after-commit-cancel"
        await _assert_db_healthy_and_clean(engine)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_exception_rollback_defensive_cleanup_and_rollback_failure_visibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify body exception rollback, defensive stale-tx cleanup, and loud rollback-error propagation."""
    engine = SQLiteMemoryEngine(tmp_path / "rollback_errors.db")
    await engine.initialize()
    conn = engine._require_conn()

    try:
        # 1. Body exception rolls back cleanly and preserves exception type
        with pytest.raises(ValueError, match="boom"):
            async with engine._db_lock:
                async with engine._write_transaction(conn):
                    await conn.execute(
                        "INSERT INTO conversations (conversation_id, title, created_at, updated_at, metadata) "
                        "VALUES ('conv-err-rb', 'Err', '2026-01-01', '2026-01-01', '{}')"
                    )
                    raise ValueError("boom")
        assert conn.in_transaction is False
        assert await engine.get_conversation("conv-err-rb") is None

        # 2. Defensive recovery when connection unexpectedly entered a transaction beforehand
        await conn.execute("BEGIN IMMEDIATE")
        assert conn.in_transaction is True
        with caplog.at_level(logging.ERROR):
            await engine.store(
                MemoryRecord(
                    id="rec-after-stale-tx",
                    memory_type=MemoryType.EPISODIC,
                    key="k:stale",
                    content="recovered from stale tx",
                )
            )
        assert conn.in_transaction is False
        assert await engine.retrieve("rec-after-stale-tx") is not None
        assert "already inside an uncommitted transaction" in caplog.text

        # 3. Rollback failure is logged and propagated loudly (never silently ignored)
        orig_rollback = conn.rollback

        async def failing_rollback() -> None:
            raise RuntimeError("simulated rollback I/O failure")

        monkeypatch.setattr(conn, "rollback", failing_rollback)
        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError, match="SQLite rollback failed"):
                async with engine._db_lock:
                    async with engine._write_transaction(conn):
                        await conn.execute(
                            "INSERT INTO conversations (conversation_id, title, created_at, updated_at, metadata) "
                            "VALUES ('conv-rb-fail', 'Fail', '2026-01-01', '2026-01-01', '{}')"
                        )
                        raise ValueError("trigger rollback")
        assert "SQLite rollback failed" in caplog.text

        # Clean up real connection after mocked rollback failure
        monkeypatch.setattr(conn, "rollback", orig_rollback)
        if conn.in_transaction:
            await conn.rollback()
        await _assert_db_healthy_and_clean(engine)
    finally:
        await engine.close()


def _build_fake_core(settings: Settings, fake_provider: FakeProvider) -> CarbonCore:
    core = CarbonCore(settings=settings)
    core.provider = fake_provider
    core.cms.provider = fake_provider
    return core


@pytest.mark.asyncio
async def test_ws_disconnect_immediately_after_completed_response_preserves_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnecting WebSocket right after receiving completed must not cancel final DB persistence."""
    port = _find_free_loopback_port()
    data_dir = tmp_path / "ws_completed_disconnect"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CARBON_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CARBON_DATABASE_PATH", str(data_dir / "carbon.db"))
    monkeypatch.setenv("CARBON_AUDIT_LOG_PATH", str(data_dir / "audit.log"))
    monkeypatch.setenv("CARBON_API_HOST", "127.0.0.1")
    monkeypatch.setenv("CARBON_API_PORT", str(port))
    monkeypatch.setenv("CARBON_API_TOKEN", "ws_test_tok_completed")
    get_settings.cache_clear()
    main_module.core = None

    fake_provider = FakeProvider(response_chunks=["Done", "."])
    core = _build_fake_core(
        Settings(
            data_dir=data_dir,
            database_path=data_dir / "carbon.db",
            audit_log_path=data_dir / "audit.log",
            api_host="127.0.0.1",
            api_port=port,
            api_token="ws_test_tok_completed",
            maintenance_interval_seconds=0.0,
        ),
        fake_provider,
    )
    await core.initialize()
    main_module.core = core

    save_completed_started = asyncio.Event()
    allow_save_completed = asyncio.Event()
    orig_save_task = core.memory.save_task
    completed_task_ids: list[str] = []

    async def gated_save_task(task_rec: PersistedTaskRecord) -> None:
        if task_rec.state == TaskState.COMPLETED:
            completed_task_ids.append(task_rec.task_id)
            save_completed_started.set()
            await allow_save_completed.wait()
        await orig_save_task(task_rec)

    monkeypatch.setattr(core.memory, "save_task", gated_save_task)

    app = main_module.create_app()
    server = uvicorn.Server(
        uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
    )
    srv_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)

    try:
        headers = {"Authorization": "Bearer ws_test_tok_completed"}
        task_id: str | None = None
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/ws/chat", additional_headers=headers
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "action": "submit",
                        "prompt": "Open CarbonUnapprovedTestAppXYZ",
                        "conversation_id": "conv-ws-done",
                    }
                )
            )
            while True:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if ev.get("event") == "task_created":
                    task_id = str(ev["task_id"])
                elif ev.get("event") == "awaiting_approval":
                    await ws.send(
                        json.dumps(
                            {
                                "action": "approve",
                                "approval_id": ev["approval_id"],
                                "task_id": ev["task_id"],
                            }
                        )
                    )
                elif ev.get("event") == "completed":
                    # Client disconnects immediately upon receiving completed frame
                    # BEFORE finally: _persist_if_needed() -> save_task() finishes!
                    break

        await asyncio.wait_for(save_completed_started.wait(), timeout=5.0)
        # Allow WebSocketDisconnect cleanup to run while save_task is suspended
        await asyncio.sleep(0.05)
        allow_save_completed.set()

        # Drain tracked stream task briefly and verify final DB persistence succeeded cleanly
        for _ in range(50):
            if not core._tracked_tasks:
                break
            await asyncio.sleep(0.02)

        assert task_id is not None
        assert task_id in completed_task_ids
        persisted = await core.memory.get_task_record(task_id)
        assert persisted is not None
        assert persisted.state == TaskState.COMPLETED
        assert "not in the approved application whitelist" in (persisted.result or "")
        assert len(core._tracked_tasks) == 0
        await _assert_db_healthy_and_clean(core.memory)
    finally:
        allow_save_completed.set()
        server.should_exit = True
        await srv_task
        main_module.core = None
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_ws_disconnect_during_active_inference_cancels_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnecting WebSocket mid-stream during active inference must still cancel and release leases."""
    port = _find_free_loopback_port()
    data_dir = tmp_path / "ws_active_disconnect"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CARBON_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CARBON_DATABASE_PATH", str(data_dir / "carbon.db"))
    monkeypatch.setenv("CARBON_AUDIT_LOG_PATH", str(data_dir / "audit.log"))
    monkeypatch.setenv("CARBON_API_HOST", "127.0.0.1")
    monkeypatch.setenv("CARBON_API_PORT", str(port))
    monkeypatch.setenv("CARBON_API_TOKEN", "ws_test_tok_active")
    get_settings.cache_clear()
    main_module.core = None

    token_blocker = asyncio.Event()

    async def slow_stream(**_kwargs: Any) -> Any:
        yield {"response": "First", "done": False}
        await token_blocker.wait()
        yield {"response": "Second", "done": True}

    fake_provider = FakeProvider()
    monkeypatch.setattr(fake_provider, "generate_stream_events", slow_stream)
    core = _build_fake_core(
        Settings(
            data_dir=data_dir,
            database_path=data_dir / "carbon.db",
            audit_log_path=data_dir / "audit.log",
            api_host="127.0.0.1",
            api_port=port,
            api_token="ws_test_tok_active",
            maintenance_interval_seconds=0.0,
        ),
        fake_provider,
    )
    await core.initialize()
    main_module.core = core

    app = main_module.create_app()
    server = uvicorn.Server(
        uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
    )
    srv_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)

    try:
        headers = {"Authorization": "Bearer ws_test_tok_active"}
        task_id: str | None = None
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/ws/chat", additional_headers=headers
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "action": "submit",
                        "prompt": "Long active stream",
                        "conversation_id": "conv-ws-act",
                    }
                )
            )
            while True:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if ev.get("event") == "task_created":
                    task_id = str(ev["task_id"])
                elif ev.get("event") == "token":
                    # Disconnect mid-stream while inference is still running
                    break

        for _ in range(100):
            if not core.cms._active_leases and not core._tracked_tasks:
                break
            await asyncio.sleep(0.02)

        assert task_id is not None
        assert len(core.cms._active_leases) == 0
        assert len(core._tracked_tasks) == 0
        persisted = await core.memory.get_task_record(task_id)
        assert persisted is not None
        assert persisted.state == TaskState.CANCELLED
        await _assert_db_healthy_and_clean(core.memory)
    finally:
        token_blocker.set()
        server.should_exit = True
        await srv_task
        main_module.core = None
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_post_response_episodic_memory_failure_does_not_fail_completed_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failure in post-response episodic store() must log diagnostics without failing the completed reply."""
    data_dir = tmp_path / "episodic_post_fail"
    data_dir.mkdir(parents=True, exist_ok=True)
    core = _build_fake_core(
        Settings(
            data_dir=data_dir,
            database_path=data_dir / "carbon.db",
            audit_log_path=data_dir / "audit.log",
            api_token="secret_do_not_log_tok",
            maintenance_interval_seconds=0.0,
        ),
        FakeProvider(response_chunks=["Delivered ", "complete ", "answer."]),
    )
    await core.initialize()

    try:

        async def broken_store(_record: MemoryRecord) -> str:
            raise RuntimeError("simulated episodic persistence error")

        monkeypatch.setattr(core.memory, "store", broken_store)

        task = await core.create_task(
            TaskRequest(prompt="Explain ACID briefly", conversation_id="conv-ep-fail")
        )
        events: list[dict[str, Any]] = []
        with caplog.at_level(logging.ERROR):
            async for ev in core.execute_task_stream(task):
                events.append(ev)

        event_types = [ev.get("event") for ev in events]
        assert "completed" in event_types
        assert "failed" not in event_types
        assert task.state == TaskState.COMPLETED
        assert task.result == "Delivered complete answer."
        assert len(core.cms._active_leases) == 0

        # Error must be visible in logs and audited without secret leakage
        assert "Post-response episodic memory persistence failed" in caplog.text
        assert "secret_do_not_log_tok" not in caplog.text

        audit_lines = [
            json.loads(line)
            for line in (data_dir / "audit.log").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        event_names = [entry["event_type"] for entry in audit_lines]
        assert "MEMORY_PERSISTENCE_FAILED" in event_names
        assert "MODEL_INFERENCE_FAILED" not in event_names

        # Persisted task state remains COMPLETED and SQLite integrity holds
        persisted = await core.memory.get_task_record(task.task_id)
        assert persisted is not None
        assert persisted.state == TaskState.COMPLETED
        assert persisted.result == "Delivered complete answer."
        await _assert_db_healthy_and_clean(core.memory)
    finally:
        await core.shutdown()
