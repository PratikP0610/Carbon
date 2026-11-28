"""Dedicated Batch 4.3A tests for Model Lifecycle, /api/ps verification, pre-warming, and recovery."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from carbon.config.settings import DEFAULT_MODEL_LOAD_TIMEOUT_SECONDS, Settings
from carbon.core.orchestrator import CarbonCore
from carbon.interfaces.models import (
    ModelAvailabilityStatus,
    ModelCapability,
    ModelMetadata,
    ModelNotApprovedError,
    ModelSwitchFailureReason,
    ModelSwitchRequest,
    ModelUnavailableError,
    ReasoningDepth,
)
from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.fake_provider import FakeProvider
from carbon.model_control.ollama_provider import OllamaProvider


def _qwen_meta(is_healthy: bool = True) -> ModelMetadata:
    return ModelMetadata(
        id="qwen3.5:9b",
        name="Qwen 3.5 9B",
        provider="ollama",
        family="qwen35",
        parameter_size="9.0B",
        quantization="Q4_K_M",
        capabilities=[
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
            ModelCapability.THINKING,
            ModelCapability.VISION,
        ],
        is_healthy=is_healthy,
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


def _unapproved_meta(model_id: str = "gemma4:12b") -> ModelMetadata:
    return ModelMetadata(
        id=model_id,
        name=model_id,
        provider="ollama",
        family="gemma4",
        capabilities=[ModelCapability.CHAT],
        is_healthy=True,
    )


def test_default_model_load_timeout_setting() -> None:
    """Verify separate configurable model load timeout defaults to 60 seconds."""
    assert DEFAULT_MODEL_LOAD_TIMEOUT_SECONDS == 60.0
    s = Settings()
    assert s.model_load_timeout_seconds == 60.0


@pytest.mark.asyncio
async def test_ollama_provider_ps_and_load_timeout_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify OllamaProvider /api/ps parsing, stream=False load/unload, error body, and timeout."""
    captured: list[dict[str, Any]] = []
    observed_timeouts: list[float | None] = []

    class FakeAsyncClient:
        def __init__(self, handler, timeout_seconds: float | None = None):
            self._handler = handler
            observed_timeouts.append(timeout_seconds)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, path: str) -> httpx.Response:
            return await self._handler("GET", path, None)

        async def post(self, path: str, json: dict[str, Any] | None = None) -> httpx.Response:
            return await self._handler("POST", path, json)

    # 1. /api/ps deduplicates and trims names; load/unload inspect stream=False & error payload
    async def handler_ok(method: str, path: str, body: dict[str, Any] | None) -> httpx.Response:
        captured.append({"method": method, "path": path, "json": body})
        req = httpx.Request(method, f"http://localhost:11434{path}")
        if path == "/api/ps":
            return httpx.Response(
                200,
                request=req,
                json={"models": [{"name": " qwen3.5:9b "}, {"name": "qwen3.5:9b"}, {"name": ""}]},
            )
        if path == "/api/generate" and body and body.get("keep_alive") == "5m":
            return httpx.Response(200, request=req, json={"done": True})
        if path == "/api/generate" and body and body.get("keep_alive") == 0:
            return httpx.Response(200, request=req, json={"done": True})
        return httpx.Response(500, request=req)

    provider = OllamaProvider(load_timeout_seconds=12.5)
    assert provider.load_timeout == 12.5
    monkeypatch.setattr(
        provider,
        "_client",
        lambda timeout_seconds=None: FakeAsyncClient(handler_ok, timeout_seconds),
    )

    assert await provider.list_running_models() == ["qwen3.5:9b"]
    assert await provider.load_model("qwen3.5:9b") is True
    assert await provider.unload_model("qwen3.5:9b") is True
    assert observed_timeouts == [provider.discovery_timeout, 12.5, 12.5]
    assert captured[1]["json"] == {
        "model": "qwen3.5:9b",
        "prompt": "",
        "stream": False,
        "keep_alive": "5m",
    }
    assert captured[2]["json"] == {"model": "qwen3.5:9b", "keep_alive": 0, "stream": False}

    # 2. HTTP 200 with JSON error returns False
    async def handler_error_json(
        method: str, path: str, body: dict[str, Any] | None
    ) -> httpx.Response:
        _ = body
        req = httpx.Request(method, f"http://localhost:11434{path}")
        return httpx.Response(200, request=req, json={"error": "out of memory"})

    monkeypatch.setattr(
        provider,
        "_client",
        lambda timeout_seconds=None: FakeAsyncClient(handler_error_json, timeout_seconds),
    )
    assert await provider.load_model("qwen3.5:9b") is False

    # 3. Timeout returns False instead of raising or leaving a false positive
    async def handler_timeout(
        method: str, path: str, body: dict[str, Any] | None
    ) -> httpx.Response:
        _ = (method, path, body)
        raise httpx.ReadTimeout("load timed out")

    monkeypatch.setattr(
        provider,
        "_client",
        lambda timeout_seconds=None: FakeAsyncClient(handler_timeout, timeout_seconds),
    )
    assert await provider.load_model("qwen3.5:9b") is False


@pytest.mark.asyncio
async def test_startup_prewarm_and_tags_never_alone_activate() -> None:
    """Verify /api/tags alone never activates; pre-warm + /api/ps verification activates cleanly."""
    # Pre-warm path: installed in tags, initially not running in /api/ps
    provider = FakeProvider(models=[_qwen_meta()], running_models=set())
    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()

    assert provider.loaded_models == ["qwen3.5:9b"]
    assert "qwen3.5:9b" in await provider.list_running_models()
    active = await cms.get_active_model()
    assert active is not None
    assert active.id == "qwen3.5:9b"
    assert active.is_installed is True
    assert active.is_running is True
    assert active.is_active is True
    assert active.availability_status == ModelAvailabilityStatus.AVAILABLE

    # Sync without prewarm when model is not in /api/ps stays INSTALLED_NOT_LOADED and non-active
    no_warm_provider = FakeProvider(models=[_qwen_meta()], running_models=set())
    no_warm_cms = CarbonModelSupervisor(provider=no_warm_provider)
    await no_warm_cms._sync_runtime_and_activate(probe_health=True, allow_prewarm=False)
    assert no_warm_provider.loaded_models == []
    assert await no_warm_cms.get_active_model() is None
    approved = await no_warm_cms.list_approved_models()
    assert approved[0].is_installed is True
    assert approved[0].is_running is False
    assert approved[0].is_active is False
    assert approved[0].availability_status == ModelAvailabilityStatus.INSTALLED_NOT_LOADED

    with pytest.raises(ModelUnavailableError):
        no_warm_cms.assert_model_authorized("qwen3.5:9b")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_kwargs",
    [
        {"fail_load": True},
        {"simulate_load_timeout": True},
        {"hide_loaded_in_ps": True},
    ],
)
async def test_prewarm_failure_or_missing_ps_verification_stays_fail_closed(
    tmp_path: Path,
    provider_kwargs: dict[str, Any],
) -> None:
    """Verify load failure, timeout, or missing /api/ps residency never marks model active."""
    provider = FakeProvider(models=[_qwen_meta()], **provider_kwargs)
    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()

    assert await cms.get_active_model() is None
    approved = await cms.list_approved_models()
    assert approved[0].is_installed is True
    assert approved[0].is_running is False
    assert approved[0].is_active is False
    assert approved[0].is_healthy is False
    assert approved[0].availability_status == ModelAvailabilityStatus.UNAVAILABLE

    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "carbon.db",
        audit_log_path=tmp_path / "audit.log",
    )
    core = CarbonCore(settings=settings)
    core.provider = provider
    core.cms = cms
    core.is_running = True
    health = await core.get_system_health()
    assert health["status"] == "degraded"
    assert health["active_model"] is None


@pytest.mark.asyncio
async def test_late_ollama_startup_recovers_on_refresh_and_selection() -> None:
    """Verify offline startup recovers safely when Ollama starts later via refresh or selection."""
    provider = FakeProvider(healthy=False, models=[_qwen_meta(), _unapproved_meta()])
    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()

    assert cms.ollama_available is False
    assert await cms.get_active_model() is None
    assert (await cms.list_approved_models())[0].availability_status == (
        ModelAvailabilityStatus.OLLAMA_UNAVAILABLE
    )

    # Ollama comes online later
    provider.healthy = True
    selected = await cms.select_model_for_task([ModelCapability.CHAT], ReasoningDepth.INSTANT)
    assert selected.id == "qwen3.5:9b"
    assert selected.is_running is True
    assert selected.is_active is True
    assert cms.ollama_available is True

    # Extra installed model remains strictly unapproved
    installed = {m.id: m for m in await cms.list_installed_models()}
    assert installed["gemma4:12b"].is_approved is False
    with pytest.raises(ModelNotApprovedError):
        cms.assert_model_authorized("gemma4:12b")

    # Explicit refresh keeps running verified state intact
    summary = await cms.refresh_models(probe_health=True)
    assert summary["ollama_connected"] is True
    assert summary["active_model_id"] == "qwen3.5:9b"
    assert "qwen3.5:9b" in summary["running_models"]


@pytest.mark.asyncio
async def test_switch_model_verifies_ps_on_unload_failure_and_rollback() -> None:
    """Verify switch_model checks /api/ps before keeping previous model or rolling back."""
    # Case 1: Unload fails AND previous model already disappeared from /api/ps -> clear active
    provider = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms.initialize()

    async def fail_unload_and_drop_residency(_model_id: str) -> bool:
        # Ghost residency vanishes mid-unload while the unload RPC itself fails
        provider.running_models.clear()
        return False

    provider.unload_model = fail_unload_and_drop_residency  # type: ignore[method-assign]

    switched = await cms.switch_model(
        ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="switch test")
    )
    assert switched is False
    assert cms.last_switch_failure == ModelSwitchFailureReason.UNLOAD_FAILED
    assert await cms.get_active_model() is None

    # Case 2: Target load not seen in /api/ps -> fails switch and rolls back cleanly via /api/ps
    provider2 = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms2 = CarbonModelSupervisor(
        provider=provider2,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms2.initialize()
    provider2.hide_loaded_in_ps = True  # Both target load and rollback hide from /api/ps
    switched2 = await cms2.switch_model(
        ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="ps missing test")
    )
    assert switched2 is False
    assert cms2.last_switch_failure == ModelSwitchFailureReason.LOAD_FAILED
    assert await cms2.get_active_model() is None


@pytest.mark.asyncio
async def test_queued_inference_lease_cannot_slip_past_refresh_drain_barrier() -> None:
    """Prove a task already suspended on _inference_lock cannot slip in when refresh drains."""
    provider = FakeProvider(models=[_qwen_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()

    observed_during_refresh: list[dict[str, Any]] = []
    original_discover = provider.discover_runtime_state

    async def spied_discover():
        observed_during_refresh.append(
            {
                "active_leases": set(cms._active_leases),
                "switch_pending": cms._switch_pending,
                "drain_set": cms._drain_event.is_set(),
            }
        )
        return await original_discover()

    provider.discover_runtime_state = spied_discover  # type: ignore[method-assign]

    # 1. Task A holds active inference lease
    await cms.acquire_inference_lease("task-A")
    # 2. Task B checks _switch_pending (False), then suspends inside _inference_lock.acquire()
    task_b = asyncio.create_task(cms.acquire_inference_lease("task-B"))
    await asyncio.sleep(0)
    assert "task-B" not in cms._active_leases

    # 3. Refresh starts, acquires _switch_lock, sets _switch_pending=True, waits on _drain_event
    refresh_task = asyncio.create_task(cms.refresh_models(probe_health=True))
    await asyncio.sleep(0)
    assert cms._switch_pending is True
    assert observed_during_refresh == []

    # 4. Task A releases: wakes BOTH _drain_event waiter and Task B; Task B MUST back out!
    await cms.release_inference_lease("task-A")
    summary = await refresh_task
    assert summary["active_model_id"] == "qwen3.5:9b"
    assert len(observed_during_refresh) == 1
    assert observed_during_refresh[0]["active_leases"] == set()
    assert observed_during_refresh[0]["switch_pending"] is True

    # 5. Now that refresh finished, Task B acquires cleanly
    await asyncio.wait_for(task_b, timeout=1.0)
    assert "task-B" in cms._active_leases
    assert cms._switch_pending is False
    await cms.release_inference_lease("task-B")


@pytest.mark.asyncio
async def test_queued_inference_lease_cannot_slip_past_switch_drain_barrier() -> None:
    """Prove a queued waiter on _inference_lock cannot slip past switch_model() drain."""
    provider = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms.initialize()

    observed_during_switch: list[dict[str, Any]] = []
    original_load = provider.load_model

    async def spied_load(model_id: str) -> bool:
        observed_during_switch.append(
            {
                "model_id": model_id,
                "active_leases": set(cms._active_leases),
                "switch_pending": cms._switch_pending,
            }
        )
        return await original_load(model_id)

    provider.load_model = spied_load  # type: ignore[method-assign]

    await cms.acquire_inference_lease("task-A")
    task_b = asyncio.create_task(cms.acquire_inference_lease("task-B"))
    await asyncio.sleep(0)

    switch_task = asyncio.create_task(
        cms.switch_model(ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="switch race"))
    )
    await asyncio.sleep(0)
    assert cms._switch_pending is True
    assert observed_during_switch == []

    await cms.release_inference_lease("task-A")
    assert await switch_task is True
    assert len(observed_during_switch) == 1
    assert observed_during_switch[0]["active_leases"] == set()
    assert observed_during_switch[0]["switch_pending"] is True

    await asyncio.wait_for(task_b, timeout=1.0)
    assert "task-B" in cms._active_leases
    assert cms._switch_pending is False
    await cms.release_inference_lease("task-B")


@pytest.mark.asyncio
async def test_refresh_drain_timeout_preserves_active_state_and_resets_pending() -> None:
    """Verify drain timeout leaves _registry, _installed_models, and _active_model untouched."""
    provider = FakeProvider(models=[_qwen_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()
    cms.drain_timeout_seconds = 0.05

    prev_active = await cms.get_active_model()
    prev_registry_snapshot = dict(cms._registry)
    prev_installed_snapshot = dict(cms._installed_models)

    await cms.acquire_inference_lease("long-stream")
    with pytest.raises(ModelUnavailableError) as exc_info:
        await cms.refresh_models(probe_health=True)
    assert exc_info.value.reason == ModelSwitchFailureReason.DRAIN_TIMEOUT

    assert cms._switch_pending is False
    assert (await cms.get_active_model()) is prev_active
    assert cms._registry == prev_registry_snapshot
    assert cms._installed_models == prev_installed_snapshot
    await cms.release_inference_lease("long-stream")


@pytest.mark.asyncio
async def test_switch_pending_resets_on_provider_errors_and_cancellation() -> None:
    """Verify _switch_pending always resets on provider errors and CancelledError."""
    provider = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms.initialize()
    prev_active = await cms.get_active_model()

    # 1. Unexpected provider error during refresh leaves live state & resets _switch_pending
    async def broken_discover():
        raise RuntimeError("transient discovery failure")

    provider.discover_runtime_state = broken_discover  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="transient discovery failure"):
        await cms.refresh_models(probe_health=True)
    assert cms._switch_pending is False
    assert (await cms.get_active_model()) is prev_active

    # 2. Cancelling refresh while waiting on drain resets _switch_pending
    await cms.acquire_inference_lease("active-1")
    refresh_task = asyncio.create_task(cms.refresh_models(probe_health=True))
    await asyncio.sleep(0)
    assert cms._switch_pending is True
    refresh_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task
    assert cms._switch_pending is False

    # 3. Cancelling switch_model while waiting on drain resets _switch_pending
    switch_task = asyncio.create_task(
        cms.switch_model(ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="cancel test"))
    )
    await asyncio.sleep(0)
    assert cms._switch_pending is True
    switch_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await switch_task
    assert cms._switch_pending is False
    await cms.release_inference_lease("active-1")


@pytest.mark.asyncio
async def test_release_inference_lease_unloads_outside_inference_lock_with_pending_token() -> None:
    """Verify Stage 5-C3 deferred unload runs outside _inference_lock with an active pending token."""
    provider = FakeProvider(models=[_qwen_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()

    events: list[str] = []
    original_unload = provider.unload_model

    async def tracked_unload(model_id: str) -> bool:
        assert cms._inference_lock.locked() is False
        assert cms._drain_event.is_set() is True
        assert cms._switch_pending is True
        assert any(kind == "deferred_unload" for kind, _ in cms._pending_owners)
        events.append("before_unload")
        ok = await original_unload(model_id)
        events.append("after_unload")
        return ok

    provider.unload_model = tracked_unload  # type: ignore[method-assign]

    await cms.acquire_inference_lease("task-1")
    await cms.handle_memory_pressure(4)
    assert cms._deferred_critical_unload is True

    await cms.release_inference_lease("task-1")
    assert events == ["before_unload", "after_unload"]
    assert cms._inference_lock.locked() is False
    assert cms._drain_event.is_set() is True
    assert cms._switch_pending is False
    assert cms._pending_owners == set()
    assert cms._deferred_critical_unload is False
