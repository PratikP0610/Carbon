"""Stage 5-E Tier 1 deterministic CI performance and reliability gates (E3 & E4)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from carbon.config.settings import DEFAULT_PRIMARY_MODEL, Settings
from carbon.core.metrics import summarize_distribution
from carbon.core.orchestrator import CarbonCore
from carbon.governance.governance_engine import verify_audit_log_chain
from carbon.interfaces.models import (
    MemoryRecord,
    MemoryType,
    ModelCapability,
    ModelMetadata,
    ModelSwitchRequest,
    ReasoningDepth,
    TaskRequest,
    TaskState,
)
from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.fake_provider import FakeProvider


def _tier1_settings(tmp_path: Path, name: str) -> Settings:
    data_dir = tmp_path / name
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        database_path=data_dir / "carbon.db",
        audit_log_path=data_dir / "audit.log",
        api_token="stage5e-tier1-token",
        enable_audit_log=True,
    )


def _primary_model_metadata() -> ModelMetadata:
    return ModelMetadata(
        id=DEFAULT_PRIMARY_MODEL,
        name="Qwen 3.5 9B",
        capabilities=[ModelCapability.CHAT, ModelCapability.THINKING],
        context_length=4096,
        memory_footprint_mb=6200,
        is_active=True,
        is_healthy=True,
    )


@pytest.mark.asyncio
async def test_stage5e_streaming_teardown_and_lease_release_latency(tmp_path: Path) -> None:
    """E3 Tier 1 Gate: Measure stream teardown + inference lease release latency (Target: <= 25 ms)."""
    settings = _tier1_settings(tmp_path, "e3_teardown")
    core = CarbonCore(settings=settings)
    fake_provider = FakeProvider(
        models=[_primary_model_metadata()],
        running_models={DEFAULT_PRIMARY_MODEL},
        response_chunks=["chunk0 ", "chunk1 ", "chunk2 ", "chunk3 ", "chunk4"],
        chunk_delay_seconds=0.05,
    )
    core.provider = fake_provider
    core.cms = CarbonModelSupervisor(
        provider=fake_provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        governance=core.governance,
    )
    await core.initialize()

    aclosing_ms: list[float] = []
    cancel_ms: list[float] = []

    try:
        # Mode A: Explicit mid-stream early close (aclosing) after first token (N=15)
        for idx in range(15):
            task = await core.create_task(
                TaskRequest(
                    prompt=f"Stream teardown early close test {idx}",
                    reasoning_override=ReasoningDepth.INSTANT,
                )
            )
            t_teardown_start = 0.0
            stream = core.execute_task_stream(task)
            async with contextlib.aclosing(stream) as gen:
                async for ev in gen:
                    if ev["event"] == "token":
                        t_teardown_start = time.perf_counter()
                        break
            assert t_teardown_start > 0.0
            elapsed_ms = (time.perf_counter() - t_teardown_start) * 1000.0
            assert len(core.cms._active_leases) == 0
            assert core.cms._drain_event.is_set()
            aclosing_ms.append(elapsed_ms)

        # Mode B: Mid-stream cancel_task() + stream drain/teardown after first token (N=15)
        for idx in range(15):
            task = await core.create_task(
                TaskRequest(
                    prompt=f"Stream teardown cancel test {idx}",
                    reasoning_override=ReasoningDepth.INSTANT,
                )
            )
            t_cancel_start = 0.0
            stream = core.execute_task_stream(task)
            async with contextlib.aclosing(stream) as gen:
                async for ev in gen:
                    if ev["event"] == "token":
                        t_cancel_start = time.perf_counter()
                        assert core.cancel_task(task.task_id) is True
            assert t_cancel_start > 0.0
            elapsed_ms = (time.perf_counter() - t_cancel_start) * 1000.0
            assert task.state == TaskState.CANCELLED
            assert len(core.cms._active_leases) == 0
            assert core.cms._drain_event.is_set()
            cancel_ms.append(elapsed_ms)

        combined_ms = aclosing_ms + cancel_ms
        aclosing_summary = summarize_distribution(aclosing_ms)
        cancel_summary = summarize_distribution(cancel_ms)
        combined_summary = summarize_distribution(combined_ms)

        print(
            "\n[Stage 5-E E3 Streaming Teardown & Lease Release (N=30, FakeProvider)] "
            + json.dumps(
                {
                    "target_max_ms": 25.0,
                    "combined": combined_summary,
                    "early_aclose_n15": aclosing_summary,
                    "midstream_cancel_n15": cancel_summary,
                }
            )
        )

        assert combined_summary["count"] == 30
        assert combined_summary["p95"] is not None
        assert combined_summary["max"] is not None
        assert combined_summary["p95"] <= 25.0
        assert combined_summary["max"] <= 25.0
    finally:
        shutdown_report = await core.shutdown()
        assert shutdown_report["degraded"] is False
        assert len(core.cms._active_leases) == 0


@pytest.mark.asyncio
async def test_stage5e_concurrency_and_chaos_200_plus_ops(tmp_path: Path) -> None:
    """E4 Tier 1 Gate: 220 concurrent mixed ops with zero deadlocks, zero leaks, valid audit chain."""
    settings = _tier1_settings(tmp_path, "e4_chaos")
    core = CarbonCore(settings=settings, maintenance_interval_seconds=0.0)
    fake_provider = FakeProvider(
        models=[_primary_model_metadata()],
        running_models={DEFAULT_PRIMARY_MODEL},
        response_chunks=["alpha ", "beta ", "gamma"],
        chunk_delay_seconds=0.002,
    )
    core.provider = fake_provider
    core.cms = CarbonModelSupervisor(
        provider=fake_provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        governance=core.governance,
    )
    await core.initialize()

    completed_count = 0
    cancelled_count = 0
    memory_ops_count = 0
    pressure_ops_count = 0
    rejected_switch_count = 0
    sqlite_errors: list[str] = []
    unexpected_errors: list[str] = []

    async def run_completed_stream(idx: int) -> None:
        nonlocal completed_count
        conv_id = f"chaos-conv-{idx % 10}"
        task = await core.create_task(
            TaskRequest(
                prompt=f"Concurrent prompt {idx}",
                conversation_id=conv_id,
                reasoning_override=ReasoningDepth.INSTANT,
            )
        )
        events: list[dict[str, Any]] = []
        async with contextlib.aclosing(core.execute_task_stream(task)) as stream:
            async for ev in stream:
                events.append(ev)
        assert events and events[-1]["event"] in ("completed", "cancelled", "failed")
        completed_count += 1

    async def run_cancelled_stream(idx: int) -> None:
        nonlocal cancelled_count
        task = await core.create_task(
            TaskRequest(
                prompt=f"Cancel midstream prompt {idx}",
                conversation_id=f"chaos-cancel-{idx % 5}",
                reasoning_override=ReasoningDepth.INSTANT,
            )
        )
        async with contextlib.aclosing(core.execute_task_stream(task)) as stream:
            async for ev in stream:
                if ev["event"] == "token":
                    if idx % 2 == 0:
                        core.cancel_task(task.task_id)
                    else:
                        break
        cancelled_count += 1

    async def run_sqlite_op(idx: int) -> None:
        nonlocal memory_ops_count
        conv_id = f"chaos-conv-{idx % 10}"
        key = f"chaos:pref:{idx % 20}"
        try:
            if idx % 3 == 0:
                await core.memory.store(
                    MemoryRecord(
                        id=str(uuid.uuid4()),
                        memory_type=MemoryType.SEMANTIC,
                        key=key,
                        content=f"Chaos semantic fact {idx}",
                        metadata={"conversation_id": conv_id},
                        source="chaos_test",
                    )
                )
            elif idx % 3 == 1:
                await core.memory.search("Chaos semantic fact", limit=5)
            else:
                await core.memory.get_recent_conversation_turns(conversation_id=conv_id, limit=3)
            memory_ops_count += 1
        except (sqlite3.Error, aiosqlite.Error) as db_err:
            sqlite_errors.append(repr(db_err))
        except Exception as exc:
            unexpected_errors.append(repr(exc))

    async def run_pressure_and_chaos_cms(idx: int) -> None:
        nonlocal pressure_ops_count, rejected_switch_count
        if idx % 2 == 0:
            level = (2, 4, 1)[idx % 3]
            await core.cms.handle_memory_pressure(level)
            pressure_ops_count += 1
        else:
            # Unapproved model switches must reject cleanly without mutating state or leaking locks
            ok = await core.cms.switch_model(
                ModelSwitchRequest(
                    target_model_id=f"unapproved:{idx}",
                    reason="stage5e_chaos_unapproved",
                )
            )
            assert ok is False
            rejected_switch_count += 1

    # Total = 70 + 40 + 70 + 40 = 220 concurrent operations
    operations = (
        [run_completed_stream(i) for i in range(70)]
        + [run_cancelled_stream(i) for i in range(40)]
        + [run_sqlite_op(i) for i in range(70)]
        + [run_pressure_and_chaos_cms(i) for i in range(40)]
    )
    assert len(operations) >= 200

    try:
        await asyncio.wait_for(asyncio.gather(*operations), timeout=30.0)
        # Restore pressure level 1 so CMS pressure state resets cleanly before shutdown
        await core.cms.handle_memory_pressure(1)

        assert sqlite_errors == [], f"Unexpected SQLite errors: {sqlite_errors}"
        assert unexpected_errors == [], f"Unexpected operational errors: {unexpected_errors}"
        assert (
            completed_count
            + cancelled_count
            + memory_ops_count
            + pressure_ops_count
            + rejected_switch_count
            == 220
        )
        assert len(core.cms._active_leases) == 0
        assert core.cms._drain_event.is_set()
        assert core.tracked_task_count == 0
    finally:
        shutdown_report = await core.shutdown()

    assert shutdown_report["degraded"] is False
    assert shutdown_report["unresolved_tasks"] == []
    assert shutdown_report["unresolved_leases"] == []
    assert core.tracked_task_count == 0
    assert len(core.cms._active_leases) == 0

    chain_ok, chain_count, chain_err = verify_audit_log_chain(settings.audit_log_path)
    assert chain_ok is True, f"Audit log chain broken: {chain_err}"
    assert chain_err is None
    assert chain_count > 0

    print(
        "\n[Stage 5-E E4 Concurrency & Chaos (220 Concurrent Ops)] "
        + json.dumps(
            {
                "total_concurrent_ops": 220,
                "completed_streams": completed_count,
                "cancelled_or_closed_streams": cancelled_count,
                "sqlite_ops": memory_ops_count,
                "pressure_ops": pressure_ops_count,
                "rejected_switch_ops": rejected_switch_count,
                "leaked_leases": len(core.cms._active_leases),
                "leaked_tracked_tasks": core.tracked_task_count,
                "sqlite_errors": len(sqlite_errors),
                "audit_chain_valid": chain_ok,
                "audit_entries": chain_count,
            }
        )
    )
