"""Dedicated Stage 4.3B-2 regression tests for safe drained model switching and rollback."""

import asyncio
from typing import Any

import pytest

from carbon.interfaces.models import (
    HealthProbeResult,
    HealthProbeStatus,
    ModelAvailabilityStatus,
    ModelCapability,
    ModelMetadata,
    ModelSwitchFailureReason,
    ModelSwitchRequest,
    RunningModelRuntimeInfo,
    RuntimeDiscoveryError,
    RuntimeDiscoverySnapshot,
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


def _failing_meta() -> ModelMetadata:
    return ModelMetadata(
        id="failing-model",
        name="Failing Model",
        provider="ollama",
        family="failing",
        capabilities=[ModelCapability.CHAT],
        is_healthy=True,
    )


@pytest.mark.asyncio
async def test_staged_locked_helper_enforces_all_four_drain_preconditions() -> None:
    """Verify _sync_runtime_staged_locked enforces switch_lock, pending, empty leases, and drain set."""
    provider = FakeProvider(models=[_qwen_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(provider=provider)

    # 1. Not holding _switch_lock -> RuntimeError
    with pytest.raises(RuntimeError, match="_switch_lock"):
        await cms._sync_runtime_staged_locked(probe_health=True, allow_prewarm=False)

    # 2. Holding _switch_lock without _switch_pending -> RuntimeError
    async with cms._switch_lock:
        with pytest.raises(RuntimeError, match="_switch_pending"):
            await cms._sync_runtime_staged_locked(probe_health=True, allow_prewarm=False)

        # 3. _switch_pending=True with non-empty _active_leases -> RuntimeError
        cms._set_switch_pending(True)
        cms._active_leases.add("active-1")
        try:
            with pytest.raises(RuntimeError, match="zero active inference leases"):
                await cms._sync_runtime_staged_locked(probe_health=True, allow_prewarm=False)

            # 4. _active_leases empty BUT _drain_event still cleared (e.g. deferred unload window) -> RuntimeError
            cms._active_leases.clear()
            cms._drain_event.clear()
            with pytest.raises(RuntimeError, match="_drain_event"):
                await cms._sync_runtime_staged_locked(probe_health=True, allow_prewarm=False)
        finally:
            cms._active_leases.clear()
            cms._drain_event.set()
            cms._set_switch_pending(False)


@pytest.mark.asyncio
async def test_switch_drains_active_inference_before_health_and_discovery() -> None:
    """Verify switch_model waits for drain barrier BEFORE health checks or runtime discovery."""
    provider = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms.initialize()

    probe_events: list[dict[str, Any]] = []
    orig_probe = provider.probe_health
    orig_discover = provider.discover_runtime_state

    async def spy_probe() -> HealthProbeResult:
        probe_events.append(
            {
                "fn": "probe_health",
                "leases": set(cms._active_leases),
                "pending": cms._switch_pending,
                "drain_set": cms._drain_event.is_set(),
            }
        )
        return await orig_probe()

    async def spy_discover() -> RuntimeDiscoverySnapshot:
        probe_events.append(
            {
                "fn": "discover_runtime_state",
                "leases": set(cms._active_leases),
                "pending": cms._switch_pending,
                "drain_set": cms._drain_event.is_set(),
            }
        )
        return await orig_discover()

    provider.probe_health = spy_probe  # type: ignore[method-assign]
    provider.discover_runtime_state = spy_discover  # type: ignore[method-assign]

    await cms.acquire_inference_lease("task-1")
    switch_task = asyncio.create_task(
        cms.switch_model(ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="drain first"))
    )
    await asyncio.sleep(0)
    assert cms._switch_pending is True
    assert cms._switch_clear_event.is_set() is False
    # Zero health/discovery probes while inference lease is still active
    assert probe_events == []

    await cms.release_inference_lease("task-1")
    assert await switch_task is True
    assert [e["fn"] for e in probe_events] == ["probe_health", "discover_runtime_state"]
    assert all(
        e["leases"] == set() and e["pending"] is True and e["drain_set"] is True
        for e in probe_events
    )
    assert cms._switch_pending is False
    assert cms._switch_clear_event.is_set() is True


@pytest.mark.asyncio
async def test_queued_inference_cannot_slip_past_switch_barrier_and_uses_new_model() -> None:
    """Verify queued inference cannot slip past switch drain and runs on new active model."""
    provider = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms.initialize()

    observed_during_switch: list[set[str]] = []
    orig_load = provider.load_model

    async def spy_load(model_id: str) -> bool:
        observed_during_switch.append(set(cms._active_leases))
        return await orig_load(model_id)

    provider.load_model = spy_load  # type: ignore[method-assign]

    await cms.acquire_inference_lease("active-1")
    queued = asyncio.create_task(cms.acquire_inference_lease("queued-2", timeout=2.0))
    await asyncio.sleep(0)

    switch_task = asyncio.create_task(
        cms.switch_model(ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="no slip"))
    )
    await asyncio.sleep(0)
    await cms.release_inference_lease("active-1")

    assert await switch_task is True
    assert observed_during_switch == [set()]

    await asyncio.wait_for(queued, timeout=1.0)
    resolved = cms.resolve_model_for_leased_task([], (await cms.get_active_model()).id)  # type: ignore[arg-type]
    assert resolved.id == "qwen-coder:7b"
    await cms.release_inference_lease("queued-2")


@pytest.mark.asyncio
async def test_switch_secondary_model_never_prewarms_default_when_idle() -> None:
    """Verify switching to a secondary approved model when idle never loads default_model_id first."""
    provider = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models=set())
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    # Sync offline/no-prewarm initially so _active_model is None while Ollama is online
    async with cms._switch_lock:
        cms._set_switch_pending(True)
        try:
            await cms._sync_runtime_staged_locked(probe_health=True, allow_prewarm=False)
        finally:
            cms._set_switch_pending(False)

    assert await cms.get_active_model() is None
    assert provider.loaded_models == []

    assert await cms.switch_model(
        ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="direct target load")
    )
    assert provider.loaded_models == ["qwen-coder:7b"]
    active = await cms.get_active_model()
    assert active is not None
    assert active.id == "qwen-coder:7b"
    assert active.is_running is True


@pytest.mark.asyncio
async def test_unapproved_switch_rejects_before_switch_lock() -> None:
    """Verify unapproved target fails fast before touching _switch_lock or waiting on drain."""
    provider = FakeProvider(models=[_qwen_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()

    async with cms._switch_lock:
        res = await asyncio.wait_for(
            cms.switch_model(ModelSwitchRequest(target_model_id="gemma4:12b", reason="unapproved")),
            timeout=0.2,
        )
    assert res is False
    assert cms.last_switch_failure == ModelSwitchFailureReason.UNAPPROVED
    active = await cms.get_active_model()
    assert active is not None and active.id == "qwen3.5:9b"


@pytest.mark.asyncio
async def test_health_probe_transient_inconclusive_vs_confirmed_unavailable_and_exception() -> None:
    """Verify transient blip, inconclusive probe, confirmed outage, and discovery exception paths."""
    # 1. Transient blip [UNREACHABLE, HEALTHY] -> recovers and switches cleanly
    provider1 = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms1 = CarbonModelSupervisor(
        provider=provider1,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms1.initialize()
    health_seq = iter(
        [
            HealthProbeResult(status=HealthProbeStatus.UNREACHABLE),
            HealthProbeResult(status=HealthProbeStatus.HEALTHY, http_status=200),
        ]
    )
    provider1.probe_health = lambda: asyncio.sleep(  # type: ignore[method-assign]
        0,
        result=next(
            health_seq, HealthProbeResult(status=HealthProbeStatus.HEALTHY, http_status=200)
        ),
    )
    assert await cms1.switch_model(
        ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="transient blip")
    )
    assert (await cms1.get_active_model()).id == "qwen-coder:7b"  # type: ignore[union-attr]

    # 2. Inconclusive health (probe=UNREACHABLE, but /api/ps still has active model) -> preserves state
    provider2 = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms2 = CarbonModelSupervisor(
        provider=provider2,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms2.initialize()
    provider2.healthy = False
    provider2.list_running_runtime_info = lambda: asyncio.sleep(  # type: ignore[method-assign]
        0, result=(RunningModelRuntimeInfo(id="qwen3.5:9b"),)
    )
    assert (
        await cms2.switch_model(
            ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="inconclusive")
        )
        is False
    )
    assert cms2.last_switch_failure == ModelSwitchFailureReason.UNAVAILABLE
    preserved = await cms2.get_active_model()
    assert preserved is not None and preserved.id == "qwen3.5:9b" and preserved.is_active is True

    # 3. Confirmed outage (probe=UNREACHABLE AND /api/ps UNREACHABLE) -> fails closed OLLAMA_UNAVAILABLE
    async def unreachable_ps() -> tuple[RunningModelRuntimeInfo, ...]:
        raise RuntimeDiscoveryError(
            "confirmed /api/ps unreachable",
            probe_status=HealthProbeStatus.UNREACHABLE,
            endpoint="/api/ps",
        )

    provider2.list_running_runtime_info = unreachable_ps  # type: ignore[method-assign]
    assert (
        await cms2.switch_model(
            ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="confirmed down")
        )
        is False
    )
    assert cms2.last_switch_failure == ModelSwitchFailureReason.OLLAMA_UNAVAILABLE
    assert await cms2.get_active_model() is None

    # 4. Discovery exception -> aborts switch, preserves prior active state & resets pending
    provider3 = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms3 = CarbonModelSupervisor(
        provider=provider3,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms3.initialize()

    async def failing_tags() -> RuntimeDiscoverySnapshot:
        raise RuntimeError("discovery socket error")

    provider3.discover_runtime_state = failing_tags  # type: ignore[method-assign]
    assert (
        await cms3.switch_model(
            ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="tags error")
        )
        is False
    )
    assert cms3.last_switch_failure == ModelSwitchFailureReason.UNAVAILABLE
    assert cms3._switch_pending is False
    assert cms3._switch_clear_event.is_set() is True
    active3 = await cms3.get_active_model()
    assert active3 is not None and active3.id == "qwen3.5:9b"


@pytest.mark.asyncio
async def test_rollback_prefers_previous_secondary_and_skips_forced_reload_duplicate() -> None:
    """Verify rollback prefers previous secondary model and never retries a forced-reload failure."""
    # Part 1: Active secondary model -> switch to failing-model fails -> rolls back to qwen-coder:7b
    provider = FakeProvider(
        models=[_qwen_meta(), _coder_meta(), _failing_meta()],
        running_models={"qwen-coder:7b"},
        fail_load_models={"failing-model"},
    )
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b", "failing-model"),
    )
    # Switch from default to qwen-coder:7b cleanly first
    await cms.initialize()
    assert await cms.switch_model(
        ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="use coder")
    )
    provider.reset_history()

    assert (
        await cms.switch_model(
            ModelSwitchRequest(target_model_id="failing-model", reason="try failing")
        )
        is False
    )
    assert cms.last_switch_failure == ModelSwitchFailureReason.LOAD_FAILED
    assert provider.loaded_models == ["qwen-coder:7b"]
    active = await cms.get_active_model()
    assert active is not None and active.id == "qwen-coder:7b" and active.is_running is True

    # Part 2: Forced reload of active qwen3.5:9b fails -> NO duplicate rollback load; _active_model is None
    provider2 = FakeProvider(models=[_qwen_meta()], running_models={"qwen3.5:9b"})
    cms2 = CarbonModelSupervisor(provider=provider2)
    await cms2.initialize()
    provider2.reset_history()
    load_attempts: list[str] = []

    async def failing_force_load(model_id: str) -> bool:
        load_attempts.append(model_id)
        return False

    provider2.load_model = failing_force_load  # type: ignore[method-assign]
    assert (
        await cms2.switch_model(
            ModelSwitchRequest(target_model_id="qwen3.5:9b", reason="force reload", force=True)
        )
        is False
    )
    assert cms2.last_switch_failure == ModelSwitchFailureReason.LOAD_FAILED
    assert load_attempts == ["qwen3.5:9b"]
    assert await cms2.get_active_model() is None


@pytest.mark.asyncio
async def test_cancellation_after_target_load_before_ps_verification_stays_fail_closed() -> None:
    """Verify cancellation between load_model() and /api/ps verification leaves no false active state."""
    provider = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms.initialize()

    orig_running_info = provider.list_running_runtime_info
    ps_gate = asyncio.Event()
    in_post_load_ps = asyncio.Event()
    load_completed = asyncio.Event()

    orig_load = provider.load_model

    async def tracked_load(model_id: str) -> bool:
        ok = await orig_load(model_id)
        load_completed.set()
        return ok

    async def gated_ps() -> tuple[RunningModelRuntimeInfo, ...]:
        if load_completed.is_set():
            in_post_load_ps.set()
            await ps_gate.wait()
        return await orig_running_info()

    provider.load_model = tracked_load  # type: ignore[method-assign]
    provider.list_running_runtime_info = gated_ps  # type: ignore[method-assign]

    switch_task = asyncio.create_task(
        cms.switch_model(
            ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="cancel mid ps")
        )
    )
    await asyncio.wait_for(in_post_load_ps.wait(), timeout=1.0)
    # Target load RPC succeeded in Ollama runtime, but /api/ps verification has NOT completed yet
    switch_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await switch_task

    # Must NEVER mark unverified target active or reference unloaded previous model
    assert await cms.get_active_model() is None
    assert cms._registry["qwen-coder:7b"].is_active is False
    assert cms._registry["qwen3.5:9b"].is_active is False
    assert cms._switch_pending is False
    assert cms._switch_clear_event.is_set() is True

    # Discrepancy exists cleanly: Ollama runtime holds qwen-coder:7b while CMS stays fail-closed (None)
    ps_gate.set()
    provider.list_running_runtime_info = orig_running_info  # type: ignore[method-assign]
    assert await provider.list_running_models() == ["qwen-coder:7b"]

    # Discovery without prewarm reconciles exact /api/ps state (only qwen-coder:7b running)
    async with cms._switch_lock:
        cms._set_switch_pending(True)
        try:
            sync_summary = await cms._sync_runtime_staged_locked(
                probe_health=True, allow_prewarm=False
            )
        finally:
            cms._set_switch_pending(False)
    assert sync_summary["running_models"] == ["qwen-coder:7b"]
    assert cms._registry["qwen-coder:7b"].is_running is True
    assert cms._registry["qwen-coder:7b"].availability_status == ModelAvailabilityStatus.AVAILABLE

    # Subsequent refresh_models reconciles active approved model cleanly
    refresh_summary = await cms.refresh_models(probe_health=True)
    assert "qwen-coder:7b" in refresh_summary["running_models"]
    active_after = await cms.get_active_model()
    assert active_after is not None
    assert active_after.id == "qwen3.5:9b"
    assert cms._registry[active_after.id].availability_status == ModelAvailabilityStatus.AVAILABLE


@pytest.mark.asyncio
async def test_switch_model_acquires_switch_lock_exactly_once_without_recursion() -> None:
    """Verify switch_model acquires _switch_lock only once per call (no recursive refresh)."""
    provider = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms.initialize()

    acquire_count = 0
    orig_acquire = cms._switch_lock.acquire

    async def counting_acquire() -> bool:
        nonlocal acquire_count
        acquire_count += 1
        return await orig_acquire()

    cms._switch_lock.acquire = counting_acquire  # type: ignore[method-assign]
    assert await cms.switch_model(
        ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="single lock check")
    )
    assert acquire_count == 1
