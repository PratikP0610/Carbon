"""Deterministic Stage 5-D tests for memory pressure endpoint, lease safety, and Ollama watchdog."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

import carbon.main as main_module
from carbon.config.settings import (
    DEFAULT_APPROVED_MODELS,
    DEFAULT_PRIMARY_MODEL,
    Settings,
    get_settings,
)
from carbon.core.orchestrator import CarbonCore
from carbon.governance.governance_engine import verify_audit_log_chain
from carbon.interfaces.models import (
    ModelCapability,
    ModelMetadata,
)
from carbon.main import create_app
from carbon.model_control.cms import (
    DEFAULT_MODEL_MEMORY_BUDGET_MB,
    DEFAULT_WATCHDOG_BACKOFF_SCHEDULE,
    CarbonModelSupervisor,
)
from carbon.model_control.fake_provider import FakeProvider


def _qwen_meta(model_id: str = DEFAULT_PRIMARY_MODEL) -> ModelMetadata:
    return ModelMetadata(
        id=model_id,
        name=model_id,
        family="qwen3.5",
        parameter_size="9B",
        capabilities=[
            ModelCapability.CHAT,
            ModelCapability.COMPLETION,
            ModelCapability.TOOLS,
            ModelCapability.THINKING,
        ],
        memory_footprint_mb=6200,
        is_healthy=True,
    )


def _setup_api_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: FakeProvider
) -> tuple[Any, CarbonCore, str]:
    data_dir = tmp_path / "api_d2"
    data_dir.mkdir(parents=True, exist_ok=True)
    token = "stage5d-loopback-secret-token"
    monkeypatch.setenv("CARBON_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CARBON_DATABASE_PATH", str(data_dir / "carbon_test.db"))
    monkeypatch.setenv("CARBON_AUDIT_LOG_PATH", str(data_dir / "audit.log"))
    monkeypatch.setenv("CARBON_API_TOKEN", token)
    monkeypatch.setenv("CARBON_MAINTENANCE_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()

    settings = get_settings()
    test_core = CarbonCore(settings=settings, maintenance_interval_seconds=0.0)
    test_core.provider = provider
    test_core.cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        approved_models=DEFAULT_APPROVED_MODELS,
        governance=test_core.governance,
        enable_recovery_watchdog=False,
    )
    main_module.core = test_core
    app = create_app()
    app.state.api_token = token
    return app, test_core, token


# ==============================================================================
# D2 — Authenticated Loopback Memory-Pressure Endpoint & Lease Protection
# ==============================================================================


@pytest.mark.asyncio
async def test_d2_memory_pressure_endpoint_auth_validation_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify loopback auth, non-loopback 403, strict validation, budget preservation, and audit trail."""
    provider = FakeProvider(
        healthy=True, models=[_qwen_meta()], running_models={DEFAULT_PRIMARY_MODEL}
    )
    app, test_core, token = _setup_api_core(tmp_path, monkeypatch, provider)
    await test_core.initialize()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 55000)),
            base_url="http://127.0.0.1:8765",
        ) as client:
            # 1. Missing / wrong token rejected with 401
            unauth = await client.post("/api/v1/system/memory-pressure", json={"level": 2})
            assert unauth.status_code == 401

            bad_auth = await client.post(
                "/api/v1/system/memory-pressure",
                headers={"Authorization": "Bearer wrong-token"},
                json={"level": 2},
            )
            assert bad_auth.status_code == 401

            # 2. Invalid levels rejected (including bool and out-of-set ints)
            for bad_level in (0, 3, 5, True, "critical"):
                invalid = await client.post(
                    "/api/v1/system/memory-pressure",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"level": bad_level},
                )
                assert invalid.status_code == 422

            # 3. Valid Level 2 transition applies warning limits and preserves 9216 MB budget
            ok_lvl2 = await client.post(
                "/api/v1/system/memory-pressure",
                headers={"Authorization": f"Bearer {token}"},
                json={"level": 2, "source": "macos_dispatch_source"},
            )
            assert ok_lvl2.status_code == 200
            payload = ok_lvl2.json()
            assert payload["status"] == "applied"
            assert payload["level"] == 2
            assert payload["max_context_tokens"] == 8192
            assert test_core.cms.memory_budget_mb == DEFAULT_MODEL_MEMORY_BUDGET_MB == 9216

            # 4. Valid Level 4 transition with zero leases unloads active model safely
            ok_lvl4 = await client.post(
                "/api/v1/system/memory-pressure",
                headers={"Authorization": f"Bearer {token}"},
                json={"level": 4, "source": "macos_dispatch_source"},
            )
            assert ok_lvl4.status_code == 200
            assert ok_lvl4.json()["max_context_tokens"] == 4096
            assert DEFAULT_PRIMARY_MODEL in provider.unloaded_models

            # 5. Shutdown guard returns 503 cleanly
            test_core._accepting_tasks = False
            during_shutdown = await client.post(
                "/api/v1/system/memory-pressure",
                headers={"Authorization": f"Bearer {token}"},
                json={"level": 1},
            )
            assert during_shutdown.status_code == 503
            test_core._accepting_tasks = True

        # 6. Non-loopback client host rejected with 403
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("10.0.0.99", 55000)),
            base_url="http://127.0.0.1:8765",
        ) as remote_client:
            remote_res = await remote_client.post(
                "/api/v1/system/memory-pressure",
                headers={"Authorization": f"Bearer {token}"},
                json={"level": 1},
            )
            assert remote_res.status_code == 403

        await test_core.shutdown()
        ok_chain, _, err = verify_audit_log_chain(test_core.settings.audit_log_path)
        assert ok_chain is True, err
        log_text = test_core.settings.audit_log_path.read_text(encoding="utf-8")
        assert "MEMORY_PRESSURE_UPDATED" in log_text
    finally:
        if test_core.is_running:
            await test_core.shutdown()
        main_module.core = None
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_d2_memory_pressure_critical_protects_active_lease_over_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Level 4 via API defers eviction while an inference lease is active, then evicts on release."""
    provider = FakeProvider(
        healthy=True,
        models=[_qwen_meta()],
        running_models={DEFAULT_PRIMARY_MODEL},
    )
    app, test_core, token = _setup_api_core(tmp_path, monkeypatch, provider)
    try:
        await test_core.initialize()
        await test_core.cms.acquire_inference_lease("active-stream-1")
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 55001)),
                base_url="http://127.0.0.1:8765",
            ) as client:
                res = await client.post(
                    "/api/v1/system/memory-pressure",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"level": 4},
                )
            assert res.status_code == 200
            body = res.json()
            assert body["deferred_critical_unload"] is True
            assert test_core.cms._deferred_critical_unload is True
            assert DEFAULT_PRIMARY_MODEL not in provider.unloaded_models
        finally:
            await test_core.cms.release_inference_lease("active-stream-1")

        # Deferred unload ran immediately on lease release outside _inference_lock
        assert DEFAULT_PRIMARY_MODEL in provider.unloaded_models
        assert test_core.cms._deferred_critical_unload is False
        assert test_core.cms._pending_owners == set()
        await test_core.shutdown()
    finally:
        main_module.core = None
        get_settings.cache_clear()


# ==============================================================================
# D3 — Controlled Ollama Recovery Watchdog
# ==============================================================================


@pytest.mark.asyncio
async def test_d3_watchdog_backoff_schedule_allow_prewarm_false_and_bounded_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify exact (2,4,8,16,30)s backoff delays, allow_prewarm=False, no overlaps, and clean exhaustion."""
    assert DEFAULT_WATCHDOG_BACKOFF_SCHEDULE == (2.0, 4.0, 8.0, 16.0, 30.0)

    provider = FakeProvider(healthy=False, models=[_qwen_meta()])
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        approved_models=DEFAULT_APPROVED_MODELS,
    )
    await cms.initialize()
    assert cms.ollama_available is False

    observed_timeouts: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(aw: Any, timeout: float | None = None) -> Any:
        if timeout == cms.drain_timeout_seconds:
            return await real_wait_for(aw, timeout=timeout)
        observed_timeouts.append(timeout)
        if asyncio.iscoroutine(aw):
            aw.close()
        raise TimeoutError

    monkeypatch.setattr("carbon.model_control.cms.asyncio.wait_for", fake_wait_for)

    t1 = cms.trigger_recovery_watchdog(reason="unit_test_offline")
    t2 = cms.trigger_recovery_watchdog(reason="duplicate_trigger")
    assert t1 is not None and t1 is t2  # overlapping triggers coalesce to same task
    await t1

    assert observed_timeouts == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert cms.watchdog_attempts == 5
    assert cms._watchdog_exhausted is True
    assert cms.is_watchdog_running is False
    # Never pre-warms during watchdog probes (allow_prewarm=False)
    assert provider.loaded_models == []


@pytest.mark.asyncio
async def test_d3_watchdog_recovers_without_prewarm_and_stops_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify watchdog restores ollama_available with allow_prewarm=False and halts cleanly on shutdown."""
    provider = FakeProvider(healthy=False, models=[_qwen_meta()])
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        approved_models=DEFAULT_APPROVED_MODELS,
    )
    await cms.initialize()

    observed_timeouts: list[float | None] = []
    attempt_num = 0
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(aw: Any, timeout: float | None = None) -> Any:
        nonlocal attempt_num
        if timeout == cms.drain_timeout_seconds:
            return await real_wait_for(aw, timeout=timeout)
        observed_timeouts.append(timeout)
        if asyncio.iscoroutine(aw):
            aw.close()
        attempt_num += 1
        if attempt_num == 3:
            provider.healthy = True
        raise TimeoutError

    monkeypatch.setattr("carbon.model_control.cms.asyncio.wait_for", fake_wait_for)

    t = cms.trigger_recovery_watchdog(reason="transient_outage")
    assert t is not None
    await t

    assert observed_timeouts == [2.0, 4.0, 8.0]
    assert cms.ollama_available is True
    assert cms._watchdog_exhausted is False
    assert provider.loaded_models == []  # allow_prewarm=False respected!

    # Test active lease deferral + clean stop on CarbonCore shutdown
    settings = Settings(
        data_dir=tmp_path / "wd_core",
        database_path=tmp_path / "wd_core" / "carbon_test.db",
        audit_log_path=tmp_path / "wd_core" / "audit.log",
        enable_audit_log=True,
        maintenance_interval_seconds=0.0,
    )
    monkeypatch.undo()
    core = CarbonCore(settings=settings)
    offline_provider = FakeProvider(healthy=False, models=[_qwen_meta()])
    core.provider = offline_provider
    core.cms = CarbonModelSupervisor(
        provider=offline_provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        approved_models=DEFAULT_APPROVED_MODELS,
        governance=core.governance,
    )
    await core.initialize()
    assert core.cms.is_watchdog_running is True

    # While an inference lease or _switch_lock is held, single probe skips safely
    await core.cms.acquire_inference_lease("lease-hold")
    assert await core.cms.attempt_watchdog_recovery_once() is False
    await core.cms.release_inference_lease("lease-hold")

    report = await core.shutdown()
    assert core.cms.is_watchdog_running is False
    assert report["degraded"] is False
