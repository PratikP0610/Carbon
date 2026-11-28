"""Dedicated Stage 4.3B-1 tests for event-based switch/lease coordination and post-lease resolution."""

import asyncio
from pathlib import Path

import pytest

from carbon.config.settings import Settings
from carbon.core.orchestrator import CarbonCore
from carbon.governance.governance_engine import verify_audit_log_chain
from carbon.interfaces.models import (
    ModelAvailabilityStatus,
    ModelCapability,
    ModelMetadata,
    ModelSwitchRequest,
    ModelUnavailableError,
    ReasoningDepth,
    TaskRequest,
    TaskState,
)
from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.fake_provider import FakeProvider


def _qwen_meta() -> ModelMetadata:
    return ModelMetadata(
        id="qwen3.5:9b",
        name="Qwen 3.5 9B",
        provider="ollama",
        family="qwen35",
        capabilities=[
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
            ModelCapability.THINKING,
        ],
        is_healthy=True,
    )


def _coder_meta() -> ModelMetadata:
    return ModelMetadata(
        id="qwen-coder:7b",
        name="Qwen Coder 7B",
        provider="ollama",
        family="qwen_coder",
        capabilities=[
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
        ],
        is_healthy=True,
    )


@pytest.mark.asyncio
async def test_event_based_wakeup_replaces_polling_and_enforces_deadline() -> None:
    """Verify acquire_inference_lease wakes via _switch_clear_event and enforces timeout deadline."""
    provider = FakeProvider(models=[_qwen_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()

    assert cms._switch_clear_event.is_set() is True
    cms._set_switch_pending(True)
    assert cms._switch_pending is True
    assert cms._switch_clear_event.is_set() is False

    # 1. Timeout expires promptly when pending stays set
    with pytest.raises(TimeoutError):
        await cms.acquire_inference_lease("timeout-task", timeout=0.04)

    # 2. Clearing _switch_pending wakes waiting task via _switch_clear_event immediately
    waiter = asyncio.create_task(cms.acquire_inference_lease("event-task", timeout=1.0))
    await asyncio.sleep(0)
    assert waiter.done() is False

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    cms._set_switch_pending(False)
    await waiter
    elapsed = loop.time() - t0
    assert elapsed < 0.04
    assert "event-task" in cms._active_leases
    await cms.release_inference_lease("event-task")


@pytest.mark.asyncio
async def test_sequential_switches_cannot_let_woken_waiter_slip_in_between() -> None:
    """Verify woken _switch_clear_event waiters re-check _switch_pending and cannot slip in."""
    provider = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms.initialize()

    # Part 1: Deterministic wakeup race: event set -> cleared again in same tick before task_w resumes
    cms._set_switch_pending(True)
    task_w = asyncio.create_task(cms.acquire_inference_lease("task-W", timeout=2.0))
    await asyncio.sleep(0)
    assert task_w.done() is False

    # Simulate Switch 1 exiting (_set_switch_pending(False) schedules task_w) and Switch 2
    # immediately re-entering pending (_set_switch_pending(True)) before task_w resumes.
    cms._set_switch_pending(False)
    cms._set_switch_pending(True)
    await asyncio.sleep(0)

    # task_w woke on _switch_clear_event, looped via continue, saw _switch_pending=True, and stayed blocked!
    assert task_w.done() is False
    assert cms._active_leases == set()
    assert cms._inference_lock.locked() is False

    # Part 2: Once Switch 2 clears pending, task_w acquires cleanly; a subsequent switch waits for task_w
    cms._set_switch_pending(False)
    await asyncio.wait_for(task_w, timeout=1.0)
    assert "task-W" in cms._active_leases

    switch_observed_leases: list[set[str]] = []
    original_load = provider.load_model

    async def spied_load(model_id: str) -> bool:
        switch_observed_leases.append(set(cms._active_leases))
        return await original_load(model_id)

    provider.load_model = spied_load  # type: ignore[method-assign]
    switch_task = asyncio.create_task(
        cms.switch_model(ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="sequential"))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert cms._switch_pending is True
    assert switch_observed_leases == []

    await cms.release_inference_lease("task-W")
    assert await switch_task is True
    assert switch_observed_leases == [set()]
    assert cms._switch_pending is False
    assert cms._switch_clear_event.is_set() is True


@pytest.mark.asyncio
async def test_stale_pre_lease_selection_resolves_to_switched_active_model(tmp_path: Path) -> None:
    """Prove execute_task_stream resolves the newly switched active model post-lease cleanly."""
    provider = FakeProvider(
        models=[_qwen_meta(), _coder_meta()],
        running_models={"qwen3.5:9b"},
        default_response="Switched response OK.",
    )
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "carbon.db",
        audit_log_path=tmp_path / "audit.log",
    )
    core = CarbonCore(settings=settings)
    core.provider = provider
    core.cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await core.initialize()

    try:
        # Hook acquire_inference_lease so right before lease acquisition, an approved switch completes
        original_acquire = core.cms.acquire_inference_lease
        switched_once = False

        async def acquire_with_concurrent_switch(task_id: str, timeout: float = 60.0) -> None:
            nonlocal switched_once
            if not switched_once:
                switched_once = True
                assert await core.cms.switch_model(
                    ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="mid-queue switch")
                )
            await original_acquire(task_id, timeout=timeout)

        core.cms.acquire_inference_lease = acquire_with_concurrent_switch  # type: ignore[method-assign]

        task = await core.create_task(TaskRequest(prompt="Write a hello world helper."))
        events = [e async for e in core.execute_task_stream(task)]

        assert task.state == TaskState.COMPLETED
        assert task.model_used == "qwen-coder:7b"
        assert any(e.get("event") == "completed" for e in events)
        gen_calls = [c for c in provider.calls if c["method"] == "generate_stream_events"]
        assert len(gen_calls) == 1
        assert gen_calls[0]["model_id"] == "qwen-coder:7b"
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_level4_pressure_eviction_stays_fail_closed_across_post_lease_and_recovery(
    tmp_path: Path,
) -> None:
    """Verify Level 4 eviction fails closed pre/post-lease and recovers only after Level 1."""
    provider = FakeProvider(
        models=[_qwen_meta()],
        running_models={"qwen3.5:9b"},
        default_response="Recovered after pressure cleared.",
    )
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "carbon.db",
        audit_log_path=tmp_path / "audit.log",
    )
    core = CarbonCore(settings=settings)
    core.provider = provider
    core.cms = CarbonModelSupervisor(provider=provider)
    await core.initialize()

    try:
        # Case A: Level 4 eviction triggers between pre-lease check and acquire_inference_lease
        original_acquire = core.cms.acquire_inference_lease
        evicted_once = False

        async def acquire_with_pressure_eviction(task_id: str, timeout: float = 60.0) -> None:
            nonlocal evicted_once
            if not evicted_once:
                evicted_once = True
                await core.cms.handle_memory_pressure(4)
            await original_acquire(task_id, timeout=timeout)

        core.cms.acquire_inference_lease = acquire_with_pressure_eviction  # type: ignore[method-assign]

        task1 = await core.create_task(TaskRequest(prompt="Explain memory pressure."))
        events1 = [e async for e in core.execute_task_stream(task1)]
        assert task1.state == TaskState.FAILED
        assert task1.model_used is None
        assert any(e.get("event") == "failed" for e in events1)
        assert all(c["method"] != "generate_stream_events" for c in provider.calls)

        # Case B: While still at Level 4, subsequent tasks stay fail-closed (ensure_ready does NOT reload)
        core.cms.acquire_inference_lease = original_acquire  # type: ignore[method-assign]
        loads_before = len(provider.loaded_models)
        await core.cms.ensure_ready_for_inference()
        assert len(provider.loaded_models) == loads_before
        with pytest.raises(ModelUnavailableError):
            await core.cms.select_model_for_task([ModelCapability.CHAT], ReasoningDepth.INSTANT)

        task2 = await core.create_task(TaskRequest(prompt="Try while still Level 4."))
        events2 = [e async for e in core.execute_task_stream(task2)]
        assert task2.state == TaskState.FAILED
        assert any(e.get("event") == "failed" for e in events2)
        assert all(c["method"] != "generate_stream_events" for c in provider.calls)

        # Case C: Pressure returns to Level 1 -> pre-lease recovery pre-warms cleanly and completes
        await core.cms.handle_memory_pressure(1)
        task3 = await core.create_task(TaskRequest(prompt="Hello after Level 1 recovery."))
        events3 = [e async for e in core.execute_task_stream(task3)]
        assert task3.state == TaskState.COMPLETED
        assert task3.model_used == "qwen3.5:9b"
        assert any(e.get("event") == "completed" for e in events3)
        active = await core.cms.get_active_model()
        assert active is not None
        assert active.is_running is True
        assert active.availability_status == ModelAvailabilityStatus.AVAILABLE

        ok_chain, count, err = verify_audit_log_chain(settings.audit_log_path)
        assert ok_chain and count > 0, err
        assert "MODEL_INFERENCE_REJECTED" in settings.audit_log_path.read_text(encoding="utf-8")
    finally:
        await core.shutdown()
