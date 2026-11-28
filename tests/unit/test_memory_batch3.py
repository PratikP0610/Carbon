"""Unit tests for Batch 3 Persistent Memory, Migrations, TTLs, Safety, and Task Persistence."""

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from carbon.config.settings import Settings
from carbon.core.orchestrator import CarbonCore
from carbon.governance.governance_engine import DecisionSource, verify_audit_log_chain
from carbon.interfaces.models import (
    MemoryRecord,
    MemoryType,
    PersistedTaskRecord,
    TaskRequest,
    TaskState,
)
from carbon.memory.sqlite_memory import LATEST_SCHEMA_VERSION, SQLiteMemoryEngine


def _make_settings(tmp_path: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "data_dir": tmp_path,
        "database_path": tmp_path / "carbon_test.db",
        "audit_log_path": tmp_path / "audit.log",
        "enable_audit_log": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_sqlite_pragmas_and_fresh_migrations(tmp_path: Path) -> None:
    """Verify WAL/safety pragmas, schema_migrations, indexes, FTS5, and idempotency."""
    db_path = tmp_path / "fresh.db"
    engine = SQLiteMemoryEngine(db_path=db_path)
    await engine.initialize()

    try:
        conn = engine._require_conn()
        async with conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
            assert row is not None and str(row[0]).lower() == "wal"

        async with conn.execute("PRAGMA foreign_keys") as cur:
            row = await cur.fetchone()
            assert row is not None and int(row[0]) == 1

        async with conn.execute("PRAGMA synchronous") as cur:
            row = await cur.fetchone()
            assert row is not None and int(row[0]) == 1  # NORMAL == 1

        async with conn.execute("PRAGMA busy_timeout") as cur:
            row = await cur.fetchone()
            assert row is not None and int(row[0]) == 5000

        async with conn.execute("PRAGMA temp_store") as cur:
            row = await cur.fetchone()
            assert row is not None and int(row[0]) == 2  # MEMORY == 2

        async with conn.execute("PRAGMA cache_size") as cur:
            row = await cur.fetchone()
            assert row is not None and int(row[0]) == -64000

        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
            assert row is not None and int(row[0]) == LATEST_SCHEMA_VERSION

        async with conn.execute("SELECT version FROM schema_migrations ORDER BY version") as cur:
            versions = [int(r[0]) for r in await cur.fetchall()]
            assert versions == [1, 2, 3]

        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memory_records'"
        ) as cur:
            index_names = {str(r[0]) for r in await cur.fetchall()}
            assert "idx_memory_updated_at" in index_names
            assert "idx_memory_expires_at" in index_names
            assert "idx_memory_conversation_id" in index_names
    finally:
        await engine.close()

    # Re-opening must be idempotent
    engine2 = SQLiteMemoryEngine(db_path=db_path)
    await engine2.initialize()
    await engine2.close()


@pytest.mark.asyncio
async def test_migration_rollback_on_failure(tmp_path: Path) -> None:
    """Verify failed migration v2 rolls back cleanly and leaves schema at v1."""
    db_path = tmp_path / "rollback.db"
    failing_engine = SQLiteMemoryEngine(db_path=db_path, fail_migration_version=2)

    with pytest.raises(RuntimeError, match="Simulated migration failure"):
        await failing_engine.initialize()
    await failing_engine.close()

    with sqlite3.connect(db_path) as conn:
        user_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert user_ver == 1
        migrations = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
        assert migrations == [1]
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "memory_records" in tables
        assert "conversations" not in tables
        assert "tasks" not in tables

    # Recovery without injected failure completes migration cleanly
    ok_engine = SQLiteMemoryEngine(db_path=db_path)
    await ok_engine.initialize()
    await ok_engine.close()


@pytest.mark.asyncio
async def test_legacy_database_upgrade_preserves_rows_and_backfills_conversations(
    tmp_path: Path,
) -> None:
    """Verify legacy rows keep NULL expires_at while conversations table is backfilled."""
    db_path = tmp_path / "legacy.db"
    now_iso = datetime.now(UTC).isoformat()

    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.executescript(
            """
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
            CREATE VIRTUAL TABLE memory_fts USING fts5(
                key, content, content='memory_records', content_rowid='rowid'
            );
            """
        )
        await raw.execute(
            """
            INSERT INTO memory_records (
                id, memory_type, key, content, metadata, source, confidence, created_at, updated_at, expires_at
            ) VALUES (?, 'episodic', 'task:old1', 'User: Hi\nCarbon: Hello', ?, 'user_interaction', 1.0, ?, ?, NULL)
            """,
            ("leg-ep-1", json.dumps({"conversation_id": "conv-legacy-1"}), now_iso, now_iso),
        )
        await raw.execute(
            """
            INSERT INTO memory_records (
                id, memory_type, key, content, metadata, source, confidence, created_at, updated_at, expires_at
            ) VALUES (?, 'semantic', 'user:preference:tea', 'green tea', '{}', 'user_explicit', 1.0, ?, ?, NULL)
            """,
            ("leg-sem-1", now_iso, now_iso),
        )
        await raw.commit()

    engine = SQLiteMemoryEngine(db_path=db_path)
    await engine.initialize()
    try:
        ep = await engine.retrieve("leg-ep-1")
        sem = await engine.retrieve("leg-sem-1")
        assert ep is not None and ep.expires_at is None
        assert sem is not None and sem.expires_at is None
        assert ep.content == "User: Hi\nCarbon: Hello"

        conv = await engine.get_conversation("conv-legacy-1")
        assert conv is not None
        assert conv.metadata.get("backfilled") is True
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_tier_ttls_expiration_filtering_and_purge(tmp_path: Path) -> None:
    """Verify D1/D2 default TTLs, expired invisibility across paths, and purge_expired."""
    engine = SQLiteMemoryEngine(
        db_path=tmp_path / "ttl.db",
        episodic_ttl_days=7,
        working_ttl_hours=24,
    )
    await engine.initialize()

    try:
        ep = MemoryRecord(
            id="ep-new",
            memory_type=MemoryType.EPISODIC,
            key="turn:1",
            content=json.dumps({"user": "hello", "assistant": "world"}),
            metadata={"conversation_id": "conv-ttl"},
        )
        wk = MemoryRecord(
            id="wk-new",
            memory_type=MemoryType.WORKING,
            key="scratch:1",
            content="active plan note",
            metadata={"conversation_id": "conv-ttl"},
        )
        sem = MemoryRecord(
            id="sem-new",
            memory_type=MemoryType.SEMANTIC,
            key="user:preference:os",
            content="User preference for os: macOS",
        )
        await engine.store(ep)
        await engine.store(wk)
        await engine.store(sem)

        stored_ep = await engine.retrieve("ep-new")
        stored_wk = await engine.retrieve("wk-new")
        stored_sem = await engine.retrieve("sem-new")
        assert stored_ep is not None and stored_ep.expires_at is not None
        assert stored_wk is not None and stored_wk.expires_at is not None
        assert stored_sem is not None and stored_sem.expires_at is None

        ep_delta = (stored_ep.expires_at - datetime.now(UTC)).total_seconds()
        wk_delta = (stored_wk.expires_at - datetime.now(UTC)).total_seconds()
        assert 6 * 86400 < ep_delta <= 7 * 86400 + 60
        assert 23 * 3600 < wk_delta <= 24 * 3600 + 60

        # Store an already-expired episodic turn and ensure ZERO retrieval paths return it
        expired_ep = MemoryRecord(
            id="ep-expired",
            memory_type=MemoryType.EPISODIC,
            key="turn:expired",
            content=json.dumps({"user": "secret expired turn", "assistant": "hidden"}),
            metadata={"conversation_id": "conv-ttl"},
            expires_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        await engine.store(expired_ep)

        assert await engine.retrieve("ep-expired") is None
        assert all(r.id != "ep-expired" for r in await engine.search("secret expired"))
        listed, total = await engine.list_memories()
        assert total == 3
        assert all(r.id != "ep-expired" for r in listed)
        turns = await engine.get_recent_conversation_turns("conv-ttl")
        assert len(turns) == 1 and "hello" in turns[0].content

        res = await engine.forget_by_query_safe("secret expired", confirm=True)
        assert res.deleted_count == 0

        purged = await engine.purge_expired()
        assert purged["memories_deleted"] == 1
        with sqlite3.connect(engine.db_path) as sconn:
            total_rows = sconn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0]
        assert total_rows == 3
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_working_memory_deleted_on_clean_shutdown(tmp_path: Path) -> None:
    """Verify Decision D2: working memory is cleared on clean CarbonCore.shutdown."""
    settings = _make_settings(tmp_path)
    core = CarbonCore(settings=settings)
    await core.initialize()

    await core.memory.store(
        MemoryRecord(
            id="wk-session",
            memory_type=MemoryType.WORKING,
            key="active:goal",
            content="temporary goal",
        )
    )
    await core.memory.store(
        MemoryRecord(
            id="sem-keep",
            memory_type=MemoryType.SEMANTIC,
            key="user:preference:editor",
            content="neovim",
        )
    )
    await core.shutdown()

    verify_engine = SQLiteMemoryEngine(db_path=settings.database_path)
    await verify_engine.initialize()
    try:
        assert await verify_engine.retrieve("wk-session") is None
        assert await verify_engine.retrieve("sem-keep") is not None
    finally:
        await verify_engine.close()

    ok, _, err = verify_audit_log_chain(settings.audit_log_path)
    assert ok, err


@pytest.mark.asyncio
async def test_procedural_memory_requires_explicit_user_approval(tmp_path: Path) -> None:
    """Verify Decision D3: real GovernanceEngine approval boundary, no auto-promotion or replay."""
    settings = _make_settings(tmp_path)
    core = CarbonCore(settings=settings)
    await core.initialize()

    try:
        proc_record = MemoryRecord(
            id="proc-1",
            memory_type=MemoryType.PROCEDURAL,
            key="workflow:deploy",
            content="Run pytest before deploying.",
            source="user_explicit",
        )

        # 1. Missing or fabricated approval_id MUST fail closed
        with pytest.raises(PermissionError):
            await core.save_explicit_memory(proc_record, decision_source=DecisionSource.USER_UI)

        with pytest.raises(PermissionError):
            await core.save_explicit_memory(
                proc_record,
                approval_id="apr-does-not-exist",
                decision_source=DecisionSource.USER_API,
            )

        # 2. Server-side approval challenge created; untrusted origins CANNOT approve it
        approval = await core.request_procedural_memory_approval(
            key=proc_record.key,
            content=proc_record.content,
        )
        ok_model, _ = await core.governance.resolve_approval(
            approval.approval_id, approved=True, source=DecisionSource.MODEL_OUTPUT
        )
        assert not ok_model
        ok_mem, _ = await core.governance.resolve_approval(
            approval.approval_id, approved=True, source=DecisionSource.MEMORY_RECORD
        )
        assert not ok_mem

        with pytest.raises(PermissionError):
            await core.save_explicit_memory(
                proc_record,
                approval_id=approval.approval_id,
                decision_source=DecisionSource.USER_API,
            )

        # 3. Trusted human decision succeeds, but tampering with content fails digest check
        ok_user, _ = await core.governance.resolve_approval(
            approval.approval_id, approved=True, source=DecisionSource.USER_API
        )
        assert ok_user

        tampered = MemoryRecord(
            id="proc-tamper",
            memory_type=MemoryType.PROCEDURAL,
            key=proc_record.key,
            content="Run rm -rf instead.",
            source="user_explicit",
        )
        with pytest.raises(PermissionError):
            await core.save_explicit_memory(
                tampered,
                approval_id=approval.approval_id,
                decision_source=DecisionSource.USER_API,
            )

        # 4. Model-originated / auto_promoted payloads MUST fail even with valid approval
        model_originated = MemoryRecord(
            id="proc-model",
            memory_type=MemoryType.PROCEDURAL,
            key=proc_record.key,
            content=proc_record.content,
            source="model_output",
        )
        with pytest.raises(PermissionError):
            await core.save_explicit_memory(
                model_originated,
                approval_id=approval.approval_id,
                decision_source=DecisionSource.USER_API,
            )

        # 5. Matching payload with trusted human approval succeeds once
        saved_id = await core.save_explicit_memory(
            proc_record,
            approval_id=approval.approval_id,
            decision_source=DecisionSource.USER_API,
        )
        assert saved_id == "proc-1"
        retrieved = await core.memory.retrieve("proc-1")
        assert retrieved is not None and retrieved.memory_type == MemoryType.PROCEDURAL

        # 6. Replay with already-consumed approval_id MUST fail
        replay = MemoryRecord(
            id="proc-replay",
            memory_type=MemoryType.PROCEDURAL,
            key=proc_record.key,
            content=proc_record.content,
            source="user_explicit",
        )
        with pytest.raises(PermissionError):
            await core.save_explicit_memory(
                replay,
                approval_id=approval.approval_id,
                decision_source=DecisionSource.USER_API,
            )
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_ambiguous_forget_safety_and_over_100_records_deletion(tmp_path: Path) -> None:
    """Verify ambiguous multi-match prevents blind deletion and >100 bulk confirm succeeds."""
    engine = SQLiteMemoryEngine(db_path=tmp_path / "forget.db")
    await engine.initialize()

    try:
        for idx in range(250):
            await engine.store(
                MemoryRecord(
                    id=f"rec-{idx}",
                    memory_type=MemoryType.SEMANTIC,
                    key=f"project:note:{idx}",
                    content=f"Project Apollo milestone detail #{idx}",
                )
            )
        await engine.store(
            MemoryRecord(
                id="rec-unique",
                memory_type=MemoryType.SEMANTIC,
                key="user:preference:tea",
                content="Unique jasmine tea preference",
            )
        )

        # Multi-match without confirm returns confirmation_required and deletes 0 rows
        ambiguous = await engine.forget_by_query_safe("Apollo milestone", confirm=False)
        assert ambiguous.status == "confirmation_required"
        assert ambiguous.deleted_count == 0
        assert len(ambiguous.candidates) == 250
        assert await engine.forget_by_query("Apollo milestone") == 0

        # Confirm=True deletes ALL 250 records cleanly without the old 100-row cap
        confirmed = await engine.forget_by_query_safe("Apollo milestone", confirm=True)
        assert confirmed.status == "deleted"
        assert confirmed.deleted_count == 250

        # Unique match deletes without prompt ambiguity
        assert await engine.forget_by_query("jasmine tea") == 1
        assert await engine.retrieve("rec-unique") is None
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_conversation_cascade_delete_and_task_restart_reconciliation(
    tmp_path: Path,
) -> None:
    """Verify conversation cascade deletes tasks + memories, and restarts cancel pending tasks."""
    settings = _make_settings(tmp_path)
    core1 = CarbonCore(settings=settings)
    await core1.initialize()

    await core1.memory.upsert_conversation("conv-A", title="Thread A")
    await core1.memory.upsert_conversation("conv-B", title="Thread B")

    await core1.memory.save_task(
        PersistedTaskRecord(
            task_id="task-done",
            conversation_id="conv-A",
            prompt="hello",
            state=TaskState.COMPLETED,
            result="hi there",
        )
    )
    await core1.memory.save_task(
        PersistedTaskRecord(
            task_id="task-stale-approval",
            conversation_id="conv-A",
            prompt="open terminal",
            state=TaskState.AWAITING_APPROVAL,
        )
    )
    await core1.memory.save_task(
        PersistedTaskRecord(
            task_id="task-conv-b",
            conversation_id="conv-B",
            prompt="stay",
            state=TaskState.COMPLETED,
            result="kept",
        )
    )
    await core1.memory.store(
        MemoryRecord(
            id="mem-conv-b",
            memory_type=MemoryType.EPISODIC,
            key="task:task-conv-b",
            content=json.dumps({"user": "stay", "assistant": "kept"}),
            metadata={"conversation_id": "conv-B"},
        )
    )
    await core1.shutdown()

    # Restart CarbonCore on the same DB: stale AWAITING_APPROVAL must become CANCELLED
    core2 = CarbonCore(settings=settings)
    await core2.initialize()
    try:
        stale = await core2.get_task_persistent("task-stale-approval")
        done = await core2.get_task_persistent("task-done")
        assert stale is not None and stale["state"] == TaskState.CANCELLED.value
        assert done is not None and done["state"] == TaskState.COMPLETED.value
        assert done["result"] == "hi there"

        # Deleting conv-B removes its conversation, cascaded task, and linked memory, leaving conv-A
        counts = await core2.delete_conversation("conv-B")
        assert counts == {
            "memories_deleted": 1,
            "tasks_deleted": 1,
            "conversations_deleted": 1,
        }
        assert await core2.get_task_persistent("task-conv-b") is None
        assert await core2.memory.retrieve("mem-conv-b") is None
        assert await core2.get_task_persistent("task-done") is not None
    finally:
        await core2.shutdown()


@pytest.mark.asyncio
async def test_episodic_zero_ttl_disables_cross_session_chat_turns(tmp_path: Path) -> None:
    """Verify episodic_ttl_days=0 disables cross-session chat turn persistence in CarbonCore."""
    from carbon.model_control.cms import CarbonModelSupervisor
    from carbon.model_control.fake_provider import FakeProvider

    settings = _make_settings(tmp_path, episodic_ttl_days=0)
    core = CarbonCore(settings=settings)
    fake = FakeProvider(default_response="Zero episodic reply")
    core.provider = fake
    core.cms = CarbonModelSupervisor(
        provider=fake,
        default_model_id=settings.primary_model,
        approved_models=settings.approved_models,
    )
    await core.initialize()

    try:
        task = await core.create_task(TaskRequest(prompt="Tell me a short fact", stream=False))
        events = [ev async for ev in core.execute_task_stream(task)]
        assert any(ev["event"] == "completed" for ev in events)

        turns = await core.memory.get_recent_conversation_turns(task.conversation_id)
        assert turns == []
        _, total = await core.memory.list_memories(memory_type=MemoryType.EPISODIC)
        assert total == 0

        persisted = await core.get_task_persistent(str(uuid.UUID(int=0)) if False else task.task_id)
        assert persisted is not None and persisted["state"] == TaskState.COMPLETED.value
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_task_payload_7d_metadata_30d_and_conversation_retention(tmp_path: Path) -> None:
    """Verify 7d payload stripping, 30d metadata retention, and conversation retention rules."""
    settings = _make_settings(tmp_path, task_payload_ttl_days=7, task_ttl_days=30)
    core = CarbonCore(settings=settings)
    await core.initialize()

    now = datetime.now(UTC)
    try:
        # 1. Task past 7-day payload TTL, still within 30-day metadata TTL
        await core.memory.save_task(
            PersistedTaskRecord(
                task_id="task-payload-aged",
                conversation_id="conv-ephemeral",
                prompt="Sensitive user prompt text",
                result="Sensitive assistant answer",
                error="Sensitive error trace",
                intent={"category": "GENERAL_QA", "summary": "Sensitive intent summary"},
                state=TaskState.COMPLETED,
                model_used="qwen3.5:9b",
                created_at=now - timedelta(days=8),
                completed_at=now - timedelta(days=8),
                payload_expires_at=now - timedelta(hours=1),
                expires_at=now + timedelta(days=22),
            )
        )

        # Synchronous retrieval masks ALL payload fields while keeping structural metadata
        fetched = await core.get_task_persistent("task-payload-aged")
        assert fetched is not None
        assert fetched["prompt"] is None
        assert fetched["result"] is None
        assert fetched["error"] is None
        assert fetched["intent"] is None
        assert fetched["state"] == "completed"
        assert fetched["model_used"] == "qwen3.5:9b"

        listed = await core.memory.list_tasks(conversation_id="conv-ephemeral")
        assert len(listed) == 1
        assert listed[0].prompt is None and listed[0].result is None and listed[0].intent is None

        # purge_expired nullifies payload columns in SQLite while preserving metadata row
        purged = await core.memory.purge_expired()
        assert purged["task_payloads_cleared"] == 1
        assert purged["tasks_deleted"] == 0
        with sqlite3.connect(settings.database_path) as sconn:
            row = sconn.execute(
                "SELECT prompt, result, error, intent, state FROM tasks WHERE task_id = ?",
                ("task-payload-aged",),
            ).fetchone()
            assert row == (None, None, None, None, "completed")

        # Ephemeral task conversation carries bounded 30-day default retention
        ephemeral_conv = await core.memory.get_conversation("conv-ephemeral")
        assert ephemeral_conv is not None and ephemeral_conv.expires_at is not None

        # Linking a SEMANTIC long-term memory retains conversation header without expiry
        await core.memory.store(
            MemoryRecord(
                id="sem-conv-pin",
                memory_type=MemoryType.SEMANTIC,
                key="user:preference:lang",
                content="Python",
                metadata={"conversation_id": "conv-longterm"},
            )
        )
        longterm_conv = await core.memory.get_conversation("conv-longterm")
        assert longterm_conv is not None and longterm_conv.expires_at is None

        # 2. Task & conversation past 30-day metadata TTL become completely invisible and purged
        await core.memory.save_task(
            PersistedTaskRecord(
                task_id="task-meta-aged",
                conversation_id="conv-expired-30d",
                prompt="Old prompt",
                state=TaskState.COMPLETED,
                created_at=now - timedelta(days=31),
                payload_expires_at=now - timedelta(days=24),
                expires_at=now - timedelta(hours=1),
            )
        )
        await core.memory.upsert_conversation(
            "conv-expired-30d", expires_at=now - timedelta(minutes=5)
        )
        assert await core.get_task_persistent("task-meta-aged") is None
        assert await core.memory.get_conversation("conv-expired-30d") is None
        purged2 = await core.memory.purge_expired()
        assert purged2["tasks_deleted"] == 1
        assert purged2["conversations_deleted"] == 1
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_memory_update_auditing_and_all_tier_fts_cascade_delete(tmp_path: Path) -> None:
    """Verify sanitized MEMORY_UPDATED audits and complete 4-tier + FTS + task deletion."""
    settings = _make_settings(tmp_path)
    core = CarbonCore(settings=settings)
    await core.initialize()

    cid = "conv-full-wipe"
    unique_tokens = [
        "WipeTokenWorking911",
        "WipeTokenEpisodic912",
        "WipeTokenSemantic913",
        "WipeTokenProcedural914",
    ]

    try:
        # 1. Semantic create + explicit update -> MEMORY_UPDATED audit without private content
        sem_id = await core.save_explicit_memory(
            MemoryRecord(
                id="sem-upd-1",
                memory_type=MemoryType.SEMANTIC,
                key="user:preference:color",
                content=f"Initial color {unique_tokens[2]}",
                metadata={"conversation_id": cid},
            ),
            decision_source=DecisionSource.USER_API,
        )
        secret_update = f"Updated sapphire color {unique_tokens[2]}"
        assert await core.update_memory(
            sem_id,
            secret_update,
            {"conversation_id": cid, "confidence_note": "confirmed"},
            decision_source=DecisionSource.USER_API,
        )

        # 2. Store WORKING, EPISODIC, and governance-approved PROCEDURAL in the same conversation
        await core.memory.store(
            MemoryRecord(
                id="wk-wipe",
                memory_type=MemoryType.WORKING,
                key="active:step",
                content=unique_tokens[0],
                metadata={"conversation_id": cid},
            )
        )
        await core.memory.store(
            MemoryRecord(
                id="ep-wipe",
                memory_type=MemoryType.EPISODIC,
                key="turn:wipe",
                content=json.dumps({"user": unique_tokens[1], "assistant": "noted"}),
                metadata={"conversation_id": cid},
            )
        )
        apr = await core.request_procedural_memory_approval(
            key="workflow:wipe", content=unique_tokens[3]
        )
        await core.governance.resolve_approval(
            apr.approval_id, approved=True, source=DecisionSource.USER_API
        )
        await core.save_explicit_memory(
            MemoryRecord(
                id="proc-wipe",
                memory_type=MemoryType.PROCEDURAL,
                key="workflow:wipe",
                content=unique_tokens[3],
                metadata={"conversation_id": cid},
                source="user_api",
            ),
            approval_id=apr.approval_id,
            decision_source=DecisionSource.USER_API,
        )
        await core.memory.save_task(
            PersistedTaskRecord(
                task_id="task-wipe-1",
                conversation_id=cid,
                prompt=unique_tokens[1],
                result="Persisted reply",
                state=TaskState.COMPLETED,
            )
        )

        # Delete complete conversation and verify ALL tiers, tasks, history, and FTS are gone
        counts = await core.delete_conversation(cid)
        assert counts == {"memories_deleted": 4, "tasks_deleted": 1, "conversations_deleted": 1}

        for tok in unique_tokens:
            assert await core.memory.search(tok) == []
            with sqlite3.connect(settings.database_path) as sconn:
                fts_hits = sconn.execute(
                    "SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?",
                    (tok,),
                ).fetchall()
                assert fts_hits == []

        assert await core.memory.get_recent_conversation_turns(cid) == []
        assert await core.memory.get_conversation(cid) is None
        assert await core.get_task_persistent("task-wipe-1") is None
        assert await core.memory.list_tasks(conversation_id=cid) == []

        # Verify audit log integrity & MEMORY_UPDATED privacy
        ok, entry_count, err = verify_audit_log_chain(settings.audit_log_path)
        assert ok and entry_count > 0, err
        audit_text = settings.audit_log_path.read_text(encoding="utf-8")
        event_types = [json.loads(line)["event_type"] for line in audit_text.splitlines() if line]
        assert "MEMORY_UPDATED" in event_types
        assert secret_update not in audit_text
        assert unique_tokens[2] not in audit_text
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_memory_expired_purged_audit_event_and_chain(tmp_path: Path) -> None:
    """Verify MEMORY_EXPIRED_PURGED audit emission, zero secret leakage, and hash chain."""
    settings = _make_settings(tmp_path)
    secret_mem = "ExpiredPrivateMemorySecret777"
    secret_prompt = "ExpiredTaskPromptSecret666"
    now = datetime.now(UTC)

    # 1. Seed expired rows across episodic, working, task payload, and task/conversation metadata
    seed_engine = SQLiteMemoryEngine(db_path=settings.database_path)
    await seed_engine.initialize()
    try:
        await seed_engine.store(
            MemoryRecord(
                id="exp-mem-1",
                memory_type=MemoryType.EPISODIC,
                key="turn:old",
                content=json.dumps({"user": secret_mem, "assistant": "old reply"}),
                expires_at=now - timedelta(minutes=10),
            )
        )
        await seed_engine.store(
            MemoryRecord(
                id="exp-wk-1",
                memory_type=MemoryType.WORKING,
                key="scratch:old",
                content=f"working {secret_mem}",
                expires_at=now - timedelta(minutes=5),
            )
        )
        await seed_engine.save_task(
            PersistedTaskRecord(
                task_id="exp-task-payload",
                conversation_id="conv-purge-keep",
                prompt=secret_prompt,
                result="old answer",
                state=TaskState.COMPLETED,
                created_at=now - timedelta(days=8),
                payload_expires_at=now - timedelta(minutes=2),
                expires_at=now + timedelta(days=20),
            )
        )
        await seed_engine.save_task(
            PersistedTaskRecord(
                task_id="exp-task-meta",
                conversation_id="conv-purge-gone",
                prompt="old prompt",
                state=TaskState.COMPLETED,
                created_at=now - timedelta(days=31),
                payload_expires_at=now - timedelta(days=24),
                expires_at=now - timedelta(minutes=1),
            )
        )
        await seed_engine.upsert_conversation(
            "conv-purge-gone", expires_at=now - timedelta(minutes=1)
        )
    finally:
        await seed_engine.close()

    # 2. Startup initialize() triggers purge_expired_records() and logs MEMORY_EXPIRED_PURGED
    core = CarbonCore(settings=settings)
    await core.initialize()
    try:
        # Also test explicit purge_expired_records() on newly expired rows
        await core.memory.store(
            MemoryRecord(
                id="exp-mem-2",
                memory_type=MemoryType.EPISODIC,
                key="turn:old2",
                content=json.dumps({"user": secret_mem, "assistant": "reply2"}),
                expires_at=now - timedelta(seconds=1),
            )
        )
        second_purge = await core.purge_expired_records()
        assert second_purge["memories_deleted"] == 1
    finally:
        await core.shutdown()

    # 3. Inspect audit log entries and verify hash chain integrity + privacy
    ok, count, err = verify_audit_log_chain(settings.audit_log_path)
    assert ok and count > 0, err

    audit_text = settings.audit_log_path.read_text(encoding="utf-8")
    assert secret_mem not in audit_text
    assert secret_prompt not in audit_text

    purged_entries = [
        json.loads(line)
        for line in audit_text.splitlines()
        if line and json.loads(line).get("event_type") == "MEMORY_EXPIRED_PURGED"
    ]
    assert len(purged_entries) == 2
    assert purged_entries[0]["details"] == {
        "memories_deleted": 2,
        "task_payloads_cleared": 1,
        "tasks_deleted": 1,
        "conversations_deleted": 1,
    }
    assert purged_entries[1]["details"] == {
        "memories_deleted": 1,
        "task_payloads_cleared": 0,
        "tasks_deleted": 0,
        "conversations_deleted": 0,
    }
