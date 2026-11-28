"""Deterministic Stage 5-B unit tests for HTTP pooling, singleflight, caching, and hot-path optimizations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import carbon.devices.manager as devices_module
import carbon.model_control.ollama_provider as ollama_module
from carbon.config.settings import DEFAULT_PRIMARY_MODEL, Settings
from carbon.core.orchestrator import CarbonCore, _extract_memory_fact
from carbon.devices.manager import DeviceManager
from carbon.intelligence.engine import IntelligenceEngine
from carbon.interfaces.models import (
    HealthProbeResult,
    HealthProbeStatus,
    ModelCapability,
    ModelMetadata,
    ModelNotApprovedError,
    ModelUnavailableError,
    ReasoningDepth,
    RuntimeDiscoverySnapshot,
    TaskCategory,
    TaskRequest,
    TaskState,
)
from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.fake_provider import FakeProvider
from carbon.model_control.ollama_provider import OllamaProvider


def _make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "data_dir": data_dir,
        "database_path": data_dir / "carbon.db",
        "audit_log_path": data_dir / "audit.log",
        "api_token": "stage5b-test-token-value",
        "enable_audit_log": True,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


class _MockHTTPResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _MockStreamResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        lines: list[str] | None = None,
        wait_event: asyncio.Event | None = None,
    ) -> None:
        self.status_code = status_code
        self._lines = lines or []
        self._wait_event = wait_event
        self.closed = False

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self) -> AsyncIterator[str]:
        if self._wait_event is not None:
            await self._wait_event.wait()
        for line in self._lines:
            yield line


class _RecordingAsyncHTTPClient:
    """Fake httpx.AsyncClient that records instantiations, calls, timeouts, and close state."""

    def __init__(
        self,
        *,
        instances: list[_RecordingAsyncHTTPClient],
        stream_lines: list[str] | None = None,
        wait_event: asyncio.Event | None = None,
        fail_next_get: Exception | None = None,
        base_url: str = "",
        timeout: Any = None,
        limits: Any = None,
    ) -> None:
        self.base_url = base_url
        self.default_timeout = timeout
        self.limits = limits
        self.is_closed = False
        self.aclose_calls = 0
        self.calls: list[tuple[str, str, float | None]] = []
        self.stream_lines = stream_lines or [
            json.dumps({"response": "hello", "done": False}),
            json.dumps({"response": "", "done": True, "eval_count": 3}),
        ]
        self.wait_event = wait_event
        self.fail_next_get = fail_next_get
        self.last_stream_resp: _MockStreamResponse | None = None
        instances.append(self)

    async def get(self, endpoint: str, *, timeout: float | None = None) -> _MockHTTPResponse:
        if self.is_closed:
            raise RuntimeError("Client used after close")
        self.calls.append(("GET", endpoint, timeout))
        if self.fail_next_get is not None:
            exc = self.fail_next_get
            self.fail_next_get = None
            raise exc
        if endpoint == "/api/version":
            return _MockHTTPResponse(200, {"version": "0.6.0"})
        if endpoint == "/api/tags":
            return _MockHTTPResponse(
                200,
                {
                    "models": [
                        {
                            "name": DEFAULT_PRIMARY_MODEL,
                            "size": 5_000_000_000,
                            "details": {"family": "qwen3.5", "parameter_size": "9B"},
                        }
                    ]
                },
            )
        if endpoint == "/api/ps":
            return _MockHTTPResponse(
                200,
                {"models": [{"name": DEFAULT_PRIMARY_MODEL, "context_length": 32768}]},
            )
        return _MockHTTPResponse(404, {"error": "unknown"})

    async def post(
        self,
        endpoint: str,
        *,
        json: dict[str, Any] | None = None,  # noqa: ARG002
        timeout: float | None = None,
    ) -> _MockHTTPResponse:
        if self.is_closed:
            raise RuntimeError("Client used after close")
        self.calls.append(("POST", endpoint, timeout))
        return _MockHTTPResponse(200, {"status": "ok"})

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        endpoint: str,
        *,
        json: dict[str, Any] | None = None,  # noqa: ARG002
        timeout: float | None = None,
    ) -> AsyncIterator[_MockStreamResponse]:
        if self.is_closed:
            raise RuntimeError("Client used after close")
        self.calls.append((f"STREAM:{method}", endpoint, timeout))
        resp = _MockStreamResponse(
            status_code=200,
            lines=list(self.stream_lines),
            wait_event=self.wait_event,
        )
        self.last_stream_resp = resp
        try:
            yield resp
        finally:
            resp.closed = True

    async def aclose(self) -> None:
        self.aclose_calls += 1
        self.is_closed = True


@pytest.mark.asyncio
async def test_ollama_provider_shares_single_http_client_with_per_call_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify OllamaProvider creates one AsyncClient lazily and preserves per-request timeouts."""
    created: list[_RecordingAsyncHTTPClient] = []

    def _factory(*, base_url: str = "", timeout: Any = None, limits: Any = None) -> Any:
        return _RecordingAsyncHTTPClient(
            instances=created,
            base_url=base_url,
            timeout=timeout,
            limits=limits,
        )

    monkeypatch.setattr(ollama_module.httpx, "AsyncClient", _factory)

    provider = OllamaProvider(
        timeout_seconds=60.0,
        load_timeout_seconds=120.0,
        discovery_timeout_seconds=5.0,
    )
    assert created == []

    health = await provider.probe_health()
    snapshot = await provider.discover_runtime_state()
    loaded = await provider.load_model(DEFAULT_PRIMARY_MODEL)
    events = [
        evt
        async for evt in provider.generate_stream_events(
            model_id=DEFAULT_PRIMARY_MODEL,
            prompt="hi",
        )
    ]
    unloaded = await provider.unload_model(DEFAULT_PRIMARY_MODEL)

    assert health.is_healthy is True
    assert snapshot.installed_models[0].id == DEFAULT_PRIMARY_MODEL
    assert loaded is True
    assert unloaded is True
    assert any(evt.get("response") == "hello" for evt in events)

    assert len(created) == 1
    client = created[0]
    assert client.is_closed is False
    assert isinstance(client.limits, httpx.Limits)
    assert client.calls == [
        ("GET", "/api/version", 5.0),
        ("GET", "/api/tags", 5.0),
        ("GET", "/api/ps", 5.0),
        ("POST", "/api/generate", 120.0),
        ("STREAM:POST", "/api/generate", 60.0),
        ("POST", "/api/generate", 120.0),
    ]

    await provider.aclose()
    assert client.aclose_calls == 1
    assert client.is_closed is True


@pytest.mark.asyncio
async def test_ollama_provider_keeps_shared_client_healthy_after_cancel_or_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify stream cancellation and transport failure keep the pooled client reusable."""
    created: list[_RecordingAsyncHTTPClient] = []
    wait_event = asyncio.Event()

    def _factory(*, base_url: str = "", timeout: Any = None, limits: Any = None) -> Any:
        return _RecordingAsyncHTTPClient(
            instances=created,
            wait_event=wait_event,
            base_url=base_url,
            timeout=timeout,
            limits=limits,
        )

    monkeypatch.setattr(ollama_module.httpx, "AsyncClient", _factory)
    provider = OllamaProvider(discovery_timeout_seconds=5.0)

    gen = provider.generate_stream_events(
        model_id=DEFAULT_PRIMARY_MODEL,
        prompt="cancel me",
    )
    reader_task = asyncio.create_task(anext(gen))
    await asyncio.sleep(0)
    reader_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader_task
    await gen.aclose()

    client = created[0]
    assert client.is_closed is False
    assert client.last_stream_resp is not None and client.last_stream_resp.closed is True

    client.fail_next_get = httpx.ConnectError("transient failure")
    failed_probe = await provider.probe_health()
    assert failed_probe.status == HealthProbeStatus.TRANSPORT_ERROR
    assert client.is_closed is False

    # Subsequent call succeeds on the same pooled client without recreating it
    client.wait_event = None
    recovered_probe = await provider.probe_health()
    assert recovered_probe.is_healthy is True
    assert len(created) == 1

    await provider.aclose()


@pytest.mark.asyncio
async def test_carbon_core_shutdown_closes_provider_client_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify CarbonCore.shutdown() calls provider.aclose() idempotently and supports lazy re-init."""
    created: list[_RecordingAsyncHTTPClient] = []

    def _factory(*, base_url: str = "", timeout: Any = None, limits: Any = None) -> Any:
        return _RecordingAsyncHTTPClient(
            instances=created,
            base_url=base_url,
            timeout=timeout,
            limits=limits,
        )

    monkeypatch.setattr(ollama_module.httpx, "AsyncClient", _factory)
    core = CarbonCore(settings=_make_settings(tmp_path))
    await core.initialize()

    assert len(created) == 1
    first_client = created[0]
    assert first_client.is_closed is False

    await core.shutdown()
    assert first_client.aclose_calls == 1
    assert first_client.is_closed is True

    # Calling aclose() twice is safe and idempotent
    await core.provider.aclose()
    assert first_client.aclose_calls == 1

    # Reusing after close lazily constructs a fresh client instead of raising
    probe = await core.provider.probe_health()
    assert probe.is_healthy is True
    assert len(created) == 2
    assert created[1].is_closed is False
    await core.provider.aclose()


@pytest.mark.asyncio
async def test_cms_singleflight_coalesces_concurrent_readiness_probes() -> None:
    """Verify concurrent ensure_ready_for_inference() calls coalesce into one discovery cycle."""
    provider = FakeProvider(healthy=True)
    probe_gate = asyncio.Event()
    discovery_calls = 0
    original_discover = provider.discover_runtime_state

    async def gated_discover() -> RuntimeDiscoverySnapshot:
        nonlocal discovery_calls
        discovery_calls += 1
        await probe_gate.wait()
        return await original_discover()

    provider.discover_runtime_state = gated_discover  # type: ignore[method-assign]
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        approved_models=(DEFAULT_PRIMARY_MODEL,),
        offline_recovery_cooldown_seconds=2.0,
    )

    waiter_1 = asyncio.create_task(cms.ensure_ready_for_inference())
    waiter_2 = asyncio.create_task(cms.ensure_ready_for_inference())
    waiter_3 = asyncio.create_task(cms.ensure_ready_for_inference())
    await asyncio.sleep(0)

    # Cancelling one caller must not abort the shared flight for peers
    waiter_1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_1

    probe_gate.set()
    await asyncio.gather(waiter_2, waiter_3)

    assert discovery_calls == 1
    assert cms.ollama_available is True


@pytest.mark.asyncio
async def test_cms_offline_cooldown_blocks_burst_probes_and_allows_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify offline cooldown suppresses burst retries, expires safely, and yields to explicit refresh."""
    provider = FakeProvider(healthy=False)
    probe_calls = 0

    async def counting_probe() -> HealthProbeResult:
        nonlocal probe_calls
        probe_calls += 1
        if provider.healthy:
            return HealthProbeResult(status=HealthProbeStatus.HEALTHY, http_status=200)
        return HealthProbeResult(status=HealthProbeStatus.UNREACHABLE, detail="down")

    provider.probe_health = counting_probe  # type: ignore[method-assign]

    now_mono = 100.0
    monkeypatch.setattr("carbon.model_control.cms.time.monotonic", lambda: now_mono)

    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        approved_models=(DEFAULT_PRIMARY_MODEL,),
        offline_recovery_cooldown_seconds=2.0,
    )

    await cms.ensure_ready_for_inference()
    assert probe_calls == 1
    assert cms.ollama_available is False

    # Within cooldown window: rapid ensure_ready calls do not hammer /api/version
    now_mono = 100.5
    await cms.ensure_ready_for_inference()
    await cms.ensure_ready_for_inference()
    assert probe_calls == 1

    # Explicit refresh_models() always bypasses cooldown even while still offline
    await cms.refresh_models(probe_health=True)
    assert probe_calls == 2
    assert cms.ollama_available is False

    # Still within new cooldown -> ensure_ready skips
    now_mono = 101.0
    await cms.ensure_ready_for_inference()
    assert probe_calls == 2

    # Provider comes online, cooldown expires cleanly, recovery succeeds
    provider.healthy = True
    now_mono = 102.6
    await cms.ensure_ready_for_inference()
    assert probe_calls == 3
    assert cms.ollama_available is True


@pytest.mark.asyncio
async def test_device_manager_ttl_cache_and_failure_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify DeviceManager caches healthy sysctl output, refreshes on expiry/force, and skips caching failures."""
    now_mono = 50.0
    monkeypatch.setattr(devices_module.time, "monotonic", lambda: now_mono)

    responses: list[tuple[int, bytes]] = [
        (0, b"17179869184\n"),
        (0, b"34359738368\n"),
        (1, b""),
        (0, b"17179869184\n"),
    ]
    spawn_count = 0

    class _FakeProc:
        def __init__(self, rc: int, out: bytes) -> None:
            self.returncode = rc
            self._out = out

        async def communicate(self) -> tuple[bytes, bytes]:
            return self._out, b""

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> _FakeProc:
        nonlocal spawn_count
        spawn_count += 1
        rc, out = responses.pop(0)
        return _FakeProc(rc, out)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    manager = DeviceManager(cache_ttl_seconds=30.0)
    results = await asyncio.gather(*(manager.get_host_status() for _ in range(4)))
    assert spawn_count == 1
    assert all(r["unified_memory_bytes"] == 17179869184 for r in results)

    # Sequential calls within TTL hit cache
    now_mono = 60.0
    cached = await manager.get_host_status()
    assert spawn_count == 1
    assert cached["unified_memory_gb"] == 16.0

    # force_refresh=True bypasses cache
    refreshed = await manager.get_host_status(force_refresh=True)
    assert spawn_count == 2
    assert refreshed["unified_memory_bytes"] == 34359738368

    # Expiry triggers failed sysctl; failure fallback is NOT cached so next call recovers immediately
    now_mono = 95.0
    degraded = await manager.get_host_status()
    assert spawn_count == 3
    assert degraded["unified_memory_bytes"] == 0
    assert degraded["status"] == "online"

    recovered = await manager.get_host_status()
    assert spawn_count == 4
    assert recovered["unified_memory_bytes"] == 17179869184


@pytest.mark.asyncio
async def test_orchestrator_deduplicates_readiness_and_snapshot_persistence(
    tmp_path: Path,
) -> None:
    """Verify execute_task_stream calls ensure_ready once per route and persists terminal snapshot once."""
    provider = FakeProvider(
        healthy=True,
        models=[
            ModelMetadata(
                id=DEFAULT_PRIMARY_MODEL,
                name=DEFAULT_PRIMARY_MODEL,
                family="qwen3.5",
                parameter_size="9B",
                capabilities=[
                    ModelCapability.CHAT,
                    ModelCapability.COMPLETION,
                    ModelCapability.THINKING,
                    ModelCapability.TOOLS,
                ],
                memory_footprint_mb=6200,
                is_healthy=True,
            )
        ],
    )
    core = CarbonCore(settings=_make_settings(tmp_path))
    core.provider = provider
    core.cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id=core.settings.primary_model,
        approved_models=core.settings.approved_models,
        governance=core.governance,
        offline_recovery_cooldown_seconds=core.settings.offline_recovery_cooldown_seconds,
    )
    await core.initialize()
    try:
        ready_spy = AsyncMock(wraps=core.cms.ensure_ready_for_inference)
        core.cms.ensure_ready_for_inference = ready_spy  # type: ignore[method-assign]

        save_task_spy = AsyncMock(wraps=core.memory.save_task)
        core.memory.save_task = save_task_spy  # type: ignore[method-assign]

        # 1. Normal completion: ensure_ready called 1x, terminal snapshot persisted 1x
        task = await core.create_task(TaskRequest(prompt="Say hello"))
        save_task_spy.reset_mock()
        ready_spy.reset_mock()
        events = [e async for e in core.execute_task_stream(task)]
        assert any(e["event"] == "completed" for e in events)
        assert ready_spy.call_count == 1
        assert save_task_spy.call_count == 1

        # 2. MEMORY_STORE and MEMORY_FORGET fast-paths: terminal snapshot persisted 1x each
        mem_task = await core.create_task(TaskRequest(prompt="Remember that my editor is neovim"))
        save_task_spy.reset_mock()
        mem_events = [e async for e in core.execute_task_stream(mem_task)]
        assert any(e["event"] == "completed" for e in mem_events)
        assert save_task_spy.call_count == 1

        forget_task = await core.create_task(TaskRequest(prompt="Forget my editor"))
        save_task_spy.reset_mock()
        forget_events = [e async for e in core.execute_task_stream(forget_task)]
        assert any(e["event"] == "completed" for e in forget_events)
        assert save_task_spy.call_count == 1

        # 3. Pre-lease cooperative cancellation: terminal CANCELLED persisted 1x
        cancel_task = await core.create_task(TaskRequest(prompt="Will be cancelled"))
        cancel_task.cancel_event.set()
        save_task_spy.reset_mock()
        cancel_events = [e async for e in core.execute_task_stream(cancel_task)]
        assert [e["event"] for e in cancel_events] == ["cancelled"]
        assert save_task_spy.call_count == 1
        assert cancel_task.state == TaskState.CANCELLED

        # 4. Offline rejection: ensure_ready called 1x and failure snapshot persisted 1x
        provider.healthy = False
        provider.running_models.clear()
        await core.cms.refresh_models(probe_health=True)
        fail_task = await core.create_task(TaskRequest(prompt="Explain quantum tunneling"))
        save_task_spy.reset_mock()
        ready_spy.reset_mock()
        fail_events = [e async for e in core.execute_task_stream(fail_task)]
        assert any(e["event"] == "failed" for e in fail_events)
        assert ready_spy.call_count == 1
        assert save_task_spy.call_count == 1
        assert fail_task.state == TaskState.FAILED
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_cms_safety_and_lease_invariants_preserved_after_deduplication() -> None:
    """Verify unapproved rejection, offline rejection, and lease cleanup still enforce CMS policy."""
    provider = FakeProvider(
        healthy=True,
        models=[
            ModelMetadata(
                id="unapproved:latest",
                name="unapproved:latest",
                family="other",
                parameter_size="7B",
                capabilities=[ModelCapability.CHAT],
                memory_footprint_mb=4000,
                is_healthy=True,
            ),
            ModelMetadata(
                id=DEFAULT_PRIMARY_MODEL,
                name=DEFAULT_PRIMARY_MODEL,
                family="qwen3.5",
                parameter_size="9B",
                capabilities=[ModelCapability.CHAT, ModelCapability.COMPLETION],
                memory_footprint_mb=6200,
                is_healthy=True,
            ),
        ],
    )
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        approved_models=(DEFAULT_PRIMARY_MODEL,),
    )
    await cms.initialize()

    with pytest.raises(ModelNotApprovedError):
        cms.assert_model_authorized("unapproved:latest")

    selected = await cms.select_model_for_task(
        capabilities_required=[ModelCapability.CHAT],
        reasoning=ReasoningDepth.INSTANT,
        task_category=TaskCategory.GENERAL,
    )
    assert selected.id == DEFAULT_PRIMARY_MODEL

    await cms.acquire_inference_lease("task-lease-check")
    try:
        leased = cms.resolve_model_for_leased_task(
            capabilities_required=[ModelCapability.CHAT],
            reasoning=ReasoningDepth.INSTANT,
            task_category=TaskCategory.GENERAL,
        )
        assert leased.id == DEFAULT_PRIMARY_MODEL
    finally:
        await cms.release_inference_lease("task-lease-check")
    assert cms._active_leases == set()

    provider.healthy = False
    provider.running_models.clear()
    await cms.refresh_models(probe_health=True)
    with pytest.raises(ModelUnavailableError):
        await cms.select_model_for_task(
            capabilities_required=[ModelCapability.CHAT],
            reasoning=ReasoningDepth.INSTANT,
            task_category=TaskCategory.GENERAL,
        )


@pytest.mark.asyncio
async def test_precompiled_regex_classification_and_memory_extraction_equivalence() -> None:
    """Verify precompiled classification and memory extraction regexes match edge inputs faithfully."""
    engine = IntelligenceEngine()

    instant = await engine.classify_intent("What is 2 + 2?")
    assert instant.reasoning_depth == ReasoningDepth.INSTANT
    assert instant.task_category == TaskCategory.GENERAL

    deep = await engine.classify_intent("Analyze and compare SQLite vs Postgres step-by-step.")
    assert deep.reasoning_depth == ReasoningDepth.DEEP

    code = await engine.classify_intent("Debug this Python Traceback in asyncio")
    assert code.task_category == TaskCategory.CODE

    mem_store = await engine.classify_intent("Remember that my favourite drink is matcha")
    assert mem_store.category == "MEMORY_STORE"
    assert mem_store.task_category == TaskCategory.GENERAL

    mem_forget = await engine.classify_intent("Please forget my favourite drink")
    assert mem_forget.category == "MEMORY_FORGET"
    assert mem_forget.task_category == TaskCategory.GENERAL

    fact, key, norm = _extract_memory_fact("Remember that: My favourite colour is Cobalt Blue!!")
    assert fact == "My favourite colour is Cobalt Blue"
    assert key == "user:preference:favorite_colour"
    assert "(favorite Cobalt Blue)" in norm

    note_fact, note_key, note_norm = _extract_memory_fact("Note that Earth orbits the Sun.")
    assert note_fact == "Earth orbits the Sun"
    assert note_key == "user:fact:earth_orbits_the_sun"
    assert note_norm == "User note: Earth orbits the Sun"
