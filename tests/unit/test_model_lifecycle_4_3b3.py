"""Dedicated Stage 4.3B-3 regression tests for bounded discovery, conservative health, and isolation."""

import asyncio
import errno
import socket
import ssl
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from carbon.config.settings import DEFAULT_DISCOVERY_TIMEOUT_SECONDS, Settings
from carbon.core.orchestrator import CarbonCore
from carbon.interfaces.models import (
    HealthProbeResult,
    HealthProbeStatus,
    ModelAvailabilityStatus,
    ModelCapability,
    ModelMetadata,
    ModelSwitchFailureReason,
    ModelSwitchRequest,
    ModelUnavailableError,
    RuntimeDiscoveryError,
    RuntimeDiscoverySnapshot,
)
from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.fake_provider import FakeProvider
from carbon.model_control.ollama_provider import OllamaProvider


def _qwen_meta(
    context_length: int | None = 4096,
    max_arch: int | None = 262144,
) -> ModelMetadata:
    return ModelMetadata(
        id="qwen3.5:9b",
        name="Qwen 3.5 9B",
        provider="ollama",
        family="qwen35",
        context_length=context_length,
        max_architectural_context=max_arch,
        capabilities=[
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
            ModelCapability.THINKING,
        ],
        is_healthy=True,
    )


def _coder_meta(
    context_length: int | None = 16384,
    max_arch: int | None = 131072,
) -> ModelMetadata:
    return ModelMetadata(
        id="qwen-coder:7b",
        name="Qwen Coder 7B",
        provider="ollama",
        family="qwen_coder",
        context_length=context_length,
        max_architectural_context=max_arch,
        capabilities=[
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
        ],
        is_healthy=True,
    )


def _econnrefused_error(url: str = "http://127.0.0.1:11434/api/version") -> httpx.ConnectError:
    req = httpx.Request("GET", url)
    err = httpx.ConnectError("All connection attempts failed", request=req)
    err.__cause__ = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
    return err


def _dns_connect_error(url: str = "http://127.0.0.1:11434/api/version") -> httpx.ConnectError:
    req = httpx.Request("GET", url)
    err = httpx.ConnectError("[Errno 8] nodename nor servname provided", request=req)
    err.__cause__ = socket.gaierror(8, "nodename nor servname provided")
    return err


class _FakeAsyncClient:
    def __init__(self, handler, observed_timeouts: list[float | None], timeout: float | None):
        self._handler = handler
        observed_timeouts.append(timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, path: str) -> httpx.Response:
        return await self._handler("GET", path, None)

    async def post(self, path: str, json: dict[str, Any] | None = None) -> httpx.Response:
        return await self._handler("POST", path, json)


@pytest.mark.asyncio
async def test_discovery_timeout_settings_validation_and_routing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify CARBON_DISCOVERY_TIMEOUT_SECONDS, validation, and strict endpoint timeout separation."""
    assert DEFAULT_DISCOVERY_TIMEOUT_SECONDS == 5.0
    assert Settings().discovery_timeout_seconds == 5.0

    monkeypatch.setenv("CARBON_DISCOVERY_TIMEOUT_SECONDS", "2.5")
    configured = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "carbon.db",
        audit_log_path=tmp_path / "audit.log",
    )
    assert configured.discovery_timeout_seconds == 2.5

    core = CarbonCore(settings=configured)
    assert core.provider.discovery_timeout == 2.5
    assert core.provider.load_timeout == 60.0
    assert core.provider.timeout == 300.0

    # Invalid timeout values fail directly with ValueError (no silent clamping)
    for bad in (0, 0.0, -1.0, float("nan"), float("inf"), True):
        with pytest.raises((ValueError, ValidationError)):
            Settings(discovery_timeout_seconds=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            OllamaProvider(discovery_timeout_seconds=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            OllamaProvider(load_timeout_seconds=bad)  # type: ignore[arg-type]

    observed_timeouts: list[float | None] = []
    tags_json = {"models": [{"name": "qwen3.5:9b", "details": {"context_length": 262144}}]}
    ps_json = {"models": [{"name": "qwen3.5:9b", "context_length": 8192}]}

    async def handler(method: str, path: str, body: dict[str, Any] | None) -> httpx.Response:
        _ = body
        req = httpx.Request(method, f"http://127.0.0.1:11434{path}")
        if path == "/api/version":
            return httpx.Response(200, request=req, json={"version": "0.6.0"})
        if path == "/api/tags":
            return httpx.Response(200, request=req, json=tags_json)
        if path == "/api/ps":
            return httpx.Response(200, request=req, json=ps_json)
        return httpx.Response(200, request=req, json={"done": True})

    provider = OllamaProvider(
        discovery_timeout_seconds=3.25,
        load_timeout_seconds=45.0,
        timeout_seconds=180.0,
    )
    monkeypatch.setattr(
        provider,
        "_client",
        lambda timeout_seconds=None: _FakeAsyncClient(handler, observed_timeouts, timeout_seconds),
    )

    assert (await provider.probe_health()).is_healthy is True
    snapshot = await provider.discover_runtime_state()
    assert snapshot.installed_models[0].id == "qwen3.5:9b"
    assert await provider.list_running_models() == ["qwen3.5:9b"]
    assert await provider.load_model("qwen3.5:9b") is True
    assert await provider.unload_model("qwen3.5:9b") is True
    assert observed_timeouts == [3.25, 3.25, 3.25, 45.0, 45.0]


@pytest.mark.asyncio
async def test_initialize_and_refresh_single_pass_non_redundant_ps_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify initialize() and refresh_models() perform 1x /api/tags + 1x /api/ps for discovery."""
    endpoints_hit: list[str] = []
    running_in_ps = True

    async def handler(method: str, path: str, body: dict[str, Any] | None) -> httpx.Response:
        nonlocal running_in_ps
        _ = method
        endpoints_hit.append(path)
        req = httpx.Request("GET", f"http://127.0.0.1:11434{path}")
        if path == "/api/version":
            return httpx.Response(200, request=req, json={"version": "0.6.0"})
        if path == "/api/tags":
            return httpx.Response(
                200,
                request=req,
                json={"models": [{"name": "qwen3.5:9b", "details": {"context_length": 262144}}]},
            )
        if path == "/api/ps":
            models = [{"name": "qwen3.5:9b", "context_length": 4096}] if running_in_ps else []
            return httpx.Response(200, request=req, json={"models": models})
        if path == "/api/generate" and body and body.get("keep_alive") == "5m":
            running_in_ps = True
            return httpx.Response(200, request=req, json={"done": True})
        return httpx.Response(404, request=req)

    provider = OllamaProvider()
    monkeypatch.setattr(
        provider,
        "_client",
        lambda timeout_seconds=None: _FakeAsyncClient(handler, [], timeout_seconds),
    )

    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()
    assert endpoints_hit == ["/api/version", "/api/tags", "/api/ps"]

    endpoints_hit.clear()
    await cms.refresh_models(probe_health=True)
    assert endpoints_hit == ["/api/version", "/api/tags", "/api/ps"]

    # When cold start requires pre-warming, discovery still emits 1x /api/ps before load + 1x after
    endpoints_hit.clear()
    running_in_ps = False
    cms2 = CarbonModelSupervisor(provider=provider)
    await cms2.initialize()
    assert endpoints_hit == [
        "/api/version",
        "/api/tags",
        "/api/ps",
        "/api/generate",
        "/api/ps",
    ]


@pytest.mark.asyncio
async def test_econnrefused_vs_dns_and_other_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only verified ECONNREFUSED yields UNREACHABLE; DNS and other connect errors stay TRANSPORT_ERROR."""
    req = httpx.Request("GET", "http://127.0.0.1:11434/api/version")

    mixed_group_err = httpx.ConnectError("mixed", request=req)
    mixed_group_err.__cause__ = ExceptionGroup(
        "happy-eyeballs",
        [socket.gaierror(8, "dns failed"), ConnectionRefusedError(errno.ECONNREFUSED, "refused")],
    )

    refused_group_err = httpx.ConnectError("all refused", request=req)
    refused_group_err.__cause__ = ExceptionGroup(
        "happy-eyeballs",
        [
            ConnectionRefusedError(errno.ECONNREFUSED, "::1 refused"),
            OSError(errno.ECONNREFUSED, "127.0.0.1 refused"),
        ],
    )

    host_unreach_err = httpx.ConnectError("no route", request=req)
    host_unreach_err.__cause__ = OSError(errno.EHOSTUNREACH, "No route to host")

    ssl_err = httpx.ConnectError("tls fail", request=req)
    ssl_err.__cause__ = ssl.SSLError("handshake failed")

    bare_connect_err = httpx.ConnectError("generic connect error", request=req)

    cases: list[tuple[Exception, HealthProbeStatus]] = [
        (_econnrefused_error(), HealthProbeStatus.UNREACHABLE),
        (refused_group_err, HealthProbeStatus.UNREACHABLE),
        (_dns_connect_error(), HealthProbeStatus.TRANSPORT_ERROR),
        (mixed_group_err, HealthProbeStatus.TRANSPORT_ERROR),
        (host_unreach_err, HealthProbeStatus.TRANSPORT_ERROR),
        (ssl_err, HealthProbeStatus.TRANSPORT_ERROR),
        (bare_connect_err, HealthProbeStatus.TRANSPORT_ERROR),
        (httpx.ReadTimeout("timed out", request=req), HealthProbeStatus.TIMEOUT),
        (
            httpx.RemoteProtocolError("protocol error", request=req),
            HealthProbeStatus.TRANSPORT_ERROR,
        ),
    ]

    provider = OllamaProvider()
    for injected_exc, expected_status in cases:

        async def fail_get(
            method: str, path: str, body: Any, exc: Exception = injected_exc
        ) -> httpx.Response:
            _ = (method, path, body)
            raise exc

        monkeypatch.setattr(
            provider,
            "_client",
            lambda timeout_seconds=None, h=fail_get: _FakeAsyncClient(h, [], timeout_seconds),
        )

        probe = await provider.probe_health()
        assert probe.status == expected_status

        with pytest.raises(RuntimeDiscoveryError) as tags_err:
            await provider.discover_runtime_state()
        assert tags_err.value.probe_status == expected_status

        with pytest.raises(RuntimeDiscoveryError) as ps_err:
            await provider.list_running_runtime_info()
        assert ps_err.value.probe_status == expected_status

    # DNS failure while an active model is committed must preserve active state (never outage)
    fake = FakeProvider(
        models=[_qwen_meta(), _coder_meta()],
        running_models={"qwen3.5:9b"},
    )
    cms = CarbonModelSupervisor(
        provider=fake,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms.initialize()

    fake.health_probe_status = HealthProbeStatus.TRANSPORT_ERROR
    assert (
        await cms.switch_model(
            ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="dns outage check")
        )
        is False
    )
    assert cms.last_switch_failure == ModelSwitchFailureReason.UNAVAILABLE
    assert cms.last_health_probe is not None
    assert cms.last_health_probe.status == HealthProbeStatus.TRANSPORT_ERROR
    active = await cms.get_active_model()
    assert active is not None and active.id == "qwen3.5:9b"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "http_status"),
    [
        (HealthProbeStatus.TIMEOUT, None),
        (HealthProbeStatus.TRANSPORT_ERROR, None),
        (HealthProbeStatus.SERVER_ERROR, 503),
        (HealthProbeStatus.CLIENT_ERROR, 404),
        (HealthProbeStatus.CLIENT_ERROR, 401),
        (HealthProbeStatus.INVALID_RESPONSE, 200),
    ],
)
async def test_cold_start_runtime_unknown_vs_unavailable_and_probe_freshness(
    tmp_path: Path,
    status: HealthProbeStatus,
    http_status: int | None,
) -> None:
    """Cold-start inconclusive health commits RUNTIME_UNKNOWN (never NOT_INSTALLED) and updates last_health_probe."""
    provider = FakeProvider(
        models=[_qwen_meta(), _coder_meta()],
        running_models={"qwen3.5:9b"},
        health_probe_status=status,
        health_http_status=http_status,
    )
    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()

    assert cms.ollama_available is False
    assert await cms.get_active_model() is None
    assert cms.last_health_probe is not None
    assert cms.last_health_probe.status == status
    assert cms.last_health_probe.http_status == http_status

    approved = await cms.list_approved_models()
    assert len(approved) == 1
    assert approved[0].availability_status == ModelAvailabilityStatus.RUNTIME_UNKNOWN
    assert approved[0].availability_status != ModelAvailabilityStatus.NOT_INSTALLED
    assert approved[0].availability_status != ModelAvailabilityStatus.OLLAMA_UNAVAILABLE
    assert approved[0].is_installed is False
    assert approved[0].is_active is False
    assert approved[0].context_length is None

    with pytest.raises(ModelUnavailableError) as auth_err:
        cms.assert_model_authorized("qwen3.5:9b")
    assert auth_err.value.reason == ModelSwitchFailureReason.UNAVAILABLE

    assert (
        await cms.switch_model(ModelSwitchRequest(target_model_id="qwen3.5:9b", reason="switch"))
        is False
    )
    assert cms.last_switch_failure == ModelSwitchFailureReason.UNAVAILABLE

    # Check CarbonCore.get_system_health surfaces runtime_unknown accurately
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
    assert health["ollama_connected"] is False
    assert health["model_availability"] == "runtime_unknown"

    # Recovery refreshes last_health_probe to HEALTHY and activates cleanly
    provider.health_probe_status = None
    provider.health_http_status = None
    provider.healthy = True
    summary = await cms.refresh_models(probe_health=True)
    assert summary["ollama_connected"] is True
    assert summary["health_probe_status"] == "healthy"
    assert cms.last_health_probe is not None
    assert cms.last_health_probe.status == HealthProbeStatus.HEALTHY
    active = await cms.get_active_model()
    assert active is not None and active.id == "qwen3.5:9b"


@pytest.mark.asyncio
async def test_health_probe_freshness_prevents_stale_unreachable_influence() -> None:
    """Verify recovery or later healthy probe clears prior UNREACHABLE from affecting authorization."""
    provider = FakeProvider(models=[_qwen_meta()], healthy=False)
    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()
    assert cms.last_health_probe is not None
    assert cms.last_health_probe.status == HealthProbeStatus.UNREACHABLE

    # Later attempt: /api/version succeeds (HEALTHY), but /api/tags fails (TIMEOUT)
    provider.healthy = True
    provider.discovery_error = RuntimeDiscoveryError(
        "/api/tags timed out",
        probe_status=HealthProbeStatus.TIMEOUT,
        endpoint="/api/tags",
    )
    with pytest.raises(RuntimeDiscoveryError):
        await cms.refresh_models(probe_health=True)

    # Probe freshness updated to HEALTHY (stale UNREACHABLE cleared)
    assert cms.last_health_probe.status == HealthProbeStatus.HEALTHY
    with pytest.raises(ModelUnavailableError) as err:
        cms.assert_model_authorized("qwen3.5:9b")
    assert err.value.reason == ModelSwitchFailureReason.UNAVAILABLE

    # Subsequent healthy discovery promotes state cleanly
    provider.discovery_error = None
    provider.running_models = {"qwen3.5:9b"}
    await cms.ensure_ready_for_inference()
    assert cms.ollama_available is True
    assert cms.last_health_probe.status == HealthProbeStatus.HEALTHY
    assert (await cms.get_active_model()) is not None


@pytest.mark.asyncio
async def test_partial_endpoint_failures_tags_vs_ps_and_legacy_independence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify /api/tags vs /api/ps directional failures fail atomic discovery while legacy methods stay independent."""
    tags_payload: dict[str, Any] = {
        "models": [{"name": "qwen3.5:9b", "details": {"context_length": 262144}}]
    }
    ps_payload: dict[str, Any] = {"models": [{"name": "qwen3.5:9b", "context_length": 8192}]}

    # Direction 1: /api/tags succeeds, /api/ps fails
    for ps_failure in ("timeout", "http500", "http404", "econnrefused", "dns", "bad_json"):
        calls_seen: list[str] = []

        async def handler_tags_ok_ps_fail(
            method: str,
            path: str,
            body: Any,
            mode: str = ps_failure,
            seen: list[str] = calls_seen,
        ) -> httpx.Response:
            _ = (method, body)
            seen.append(path)
            req = httpx.Request("GET", f"http://127.0.0.1:11434{path}")
            if path == "/api/tags":
                return httpx.Response(200, request=req, json=tags_payload)
            if path == "/api/ps":
                if mode == "timeout":
                    raise httpx.ReadTimeout("ps timeout", request=req)
                if mode == "http500":
                    return httpx.Response(500, request=req)
                if mode == "http404":
                    return httpx.Response(404, request=req)
                if mode == "econnrefused":
                    raise _econnrefused_error("http://127.0.0.1:11434/api/ps")
                if mode == "dns":
                    raise _dns_connect_error("http://127.0.0.1:11434/api/ps")
                return httpx.Response(200, request=req, content=b"not-json")
            return httpx.Response(200, request=req, json={"version": "0.6.0"})

        provider = OllamaProvider()
        monkeypatch.setattr(
            provider,
            "_client",
            lambda timeout_seconds=None, h=handler_tags_ok_ps_fail: _FakeAsyncClient(
                h, [], timeout_seconds
            ),
        )

        with pytest.raises(RuntimeDiscoveryError) as exc_info:
            await provider.discover_runtime_state()
        assert exc_info.value.endpoint == "/api/ps"

        # Legacy list_available_models() depends ONLY on /api/tags and must still succeed
        calls_seen.clear()
        legacy_available = await provider.list_available_models()
        assert len(legacy_available) == 1
        assert legacy_available[0].id == "qwen3.5:9b"
        assert legacy_available[0].context_length is None
        assert legacy_available[0].max_architectural_context == 262144
        assert calls_seen == ["/api/tags"]
        assert await provider.list_running_models() == []

    # Direction 2: /api/ps succeeds, /api/tags fails
    for tags_failure in ("timeout", "http500", "http404", "econnrefused", "dns", "bad_json"):
        calls_seen_2: list[str] = []

        async def handler_ps_ok_tags_fail(
            method: str,
            path: str,
            body: Any,
            mode: str = tags_failure,
            seen: list[str] = calls_seen_2,
        ) -> httpx.Response:
            _ = (method, body)
            seen.append(path)
            req = httpx.Request("GET", f"http://127.0.0.1:11434{path}")
            if path == "/api/ps":
                return httpx.Response(200, request=req, json=ps_payload)
            if path == "/api/tags":
                if mode == "timeout":
                    raise httpx.ReadTimeout("tags timeout", request=req)
                if mode == "http500":
                    return httpx.Response(500, request=req)
                if mode == "http404":
                    return httpx.Response(404, request=req)
                if mode == "econnrefused":
                    raise _econnrefused_error("http://127.0.0.1:11434/api/tags")
                if mode == "dns":
                    raise _dns_connect_error("http://127.0.0.1:11434/api/tags")
                return httpx.Response(200, request=req, json={"models": "invalid-not-list"})
            return httpx.Response(200, request=req, json={"version": "0.6.0"})

        provider2 = OllamaProvider()
        monkeypatch.setattr(
            provider2,
            "_client",
            lambda timeout_seconds=None, h=handler_ps_ok_tags_fail: _FakeAsyncClient(
                h, [], timeout_seconds
            ),
        )

        with pytest.raises(RuntimeDiscoveryError) as exc_info_2:
            await provider2.discover_runtime_state()
        assert exc_info_2.value.endpoint == "/api/tags"

        # Legacy list_running_models() depends ONLY on /api/ps and must still succeed
        calls_seen_2.clear()
        legacy_running = await provider2.list_running_models()
        assert legacy_running == ["qwen3.5:9b"]
        assert calls_seen_2 == ["/api/ps"]
        assert await provider2.list_available_models() == []

    # Verify CMS preserves live state across both failure directions during refresh/switch
    fake = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(
        provider=fake,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms.initialize()
    prior_active = await cms.get_active_model()

    fake.ps_error = RuntimeDiscoveryError(
        "/api/ps failed",
        probe_status=HealthProbeStatus.SERVER_ERROR,
        endpoint="/api/ps",
        http_status=500,
    )
    assert (
        await cms.switch_model(
            ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="ps fail")
        )
        is False
    )
    assert (await cms.get_active_model()) is prior_active
    assert cms._switch_pending is False
    assert cms._switch_clear_event.is_set() is True

    fake.ps_error = None
    fake.discovery_error = RuntimeDiscoveryError(
        "/api/tags failed", probe_status=HealthProbeStatus.TIMEOUT, endpoint="/api/tags"
    )
    with pytest.raises(RuntimeDiscoveryError):
        await cms.refresh_models(probe_health=True)
    assert (await cms.get_active_model()) is prior_active
    assert cms._switch_pending is False
    assert cms._switch_clear_event.is_set() is True


@pytest.mark.asyncio
async def test_confirmed_outage_vs_inconclusive_health_preserves_active_state() -> None:
    """Confirm only dual ECONNREFUSED + /api/ps ECONNREFUSED clears active state; ambiguous failures preserve it."""
    provider = FakeProvider(models=[_qwen_meta(), _coder_meta()], running_models={"qwen3.5:9b"})
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms.initialize()
    prior_active = await cms.get_active_model()
    assert prior_active is not None

    # All inconclusive statuses must preserve active model cleanly
    for bad_status, http_code in (
        (HealthProbeStatus.SERVER_ERROR, 500),
        (HealthProbeStatus.SERVER_ERROR, 502),
        (HealthProbeStatus.CLIENT_ERROR, 404),
        (HealthProbeStatus.CLIENT_ERROR, 429),
        (HealthProbeStatus.TIMEOUT, None),
        (HealthProbeStatus.TRANSPORT_ERROR, None),
        (HealthProbeStatus.INVALID_RESPONSE, 200),
    ):
        provider.health_probe_status = bad_status
        provider.health_http_status = http_code
        assert (
            await cms.switch_model(
                ModelSwitchRequest(target_model_id="qwen-coder:7b", reason=bad_status.value)
            )
            is False
        )
        assert cms.last_switch_failure == ModelSwitchFailureReason.UNAVAILABLE
        assert (await cms.get_active_model()) is prior_active
        assert cms.ollama_available is True
        assert cms._switch_pending is False
        assert cms._switch_clear_event.is_set() is True

    # Probe 1 UNREACHABLE, Probe 2 TIMEOUT -> inconclusive, preserves active state
    provider.health_probe_status = None
    provider.health_http_status = None
    seq = iter(
        [
            HealthProbeResult(status=HealthProbeStatus.UNREACHABLE),
            HealthProbeResult(status=HealthProbeStatus.TIMEOUT),
        ]
    )
    orig_probe = provider.probe_health
    provider.probe_health = lambda: asyncio.sleep(  # type: ignore[method-assign]
        0, result=next(seq, HealthProbeResult(status=HealthProbeStatus.UNREACHABLE))
    )
    assert (
        await cms.switch_model(
            ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="probe2 timeout")
        )
        is False
    )
    assert (await cms.get_active_model()) is prior_active

    # Probe 1 UNREACHABLE, Probe 2 UNREACHABLE, /api/ps fails with 500 or DNS -> preserves active state
    provider.probe_health = orig_probe  # type: ignore[method-assign]
    provider.healthy = False
    for ps_exc in (
        RuntimeDiscoveryError(
            "/api/ps 500",
            probe_status=HealthProbeStatus.SERVER_ERROR,
            endpoint="/api/ps",
            http_status=500,
        ),
        RuntimeDiscoveryError(
            "/api/ps dns", probe_status=HealthProbeStatus.TRANSPORT_ERROR, endpoint="/api/ps"
        ),
        RuntimeDiscoveryError(
            "/api/ps timeout", probe_status=HealthProbeStatus.TIMEOUT, endpoint="/api/ps"
        ),
    ):
        provider.ps_error = ps_exc
        assert (
            await cms.switch_model(
                ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="ps inconclusive")
            )
            is False
        )
        assert cms.last_switch_failure == ModelSwitchFailureReason.UNAVAILABLE
        assert (await cms.get_active_model()) is prior_active

    # Now Probe 1 & Probe 2 UNREACHABLE AND /api/ps UNREACHABLE -> confirmed outage
    provider.ps_error = None
    provider.running_models.clear()
    assert (
        await cms.switch_model(
            ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="confirmed outage")
        )
        is False
    )
    assert cms.last_switch_failure == ModelSwitchFailureReason.OLLAMA_UNAVAILABLE
    assert cms.ollama_available is False
    assert await cms.get_active_model() is None


@pytest.mark.asyncio
async def test_context_length_bool_rejection_and_lifecycle_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject bool context lengths in Pydantic & HTTP parsing; sync runtime context across load/unload."""
    for bad_val in (True, False, 0, -512, "4096"):
        with pytest.raises(ValidationError):
            ModelMetadata(id="bad", name="Bad", context_length=bad_val)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            ModelMetadata(id="bad", name="Bad", max_architectural_context=bad_val)  # type: ignore[arg-type]

    # Ollama HTTP JSON returning booleans / missing context produces None (no fabrication)
    async def handler(method: str, path: str, body: Any) -> httpx.Response:
        _ = (method, body)
        req = httpx.Request("GET", f"http://127.0.0.1:11434{path}")
        if path == "/api/tags":
            return httpx.Response(
                200,
                request=req,
                json={
                    "models": [
                        {"name": "bool-ctx", "details": {"context_length": True}},
                        {"name": "zero-ctx", "details": {"context_length": 0}},
                        {"name": "qwen3.5:9b", "details": {}},
                    ]
                },
            )
        if path == "/api/ps":
            return httpx.Response(
                200,
                request=req,
                json={
                    "models": [
                        {"name": "bool-ctx", "context_length": False},
                        {"name": "qwen3.5:9b", "context_length": 12288},
                    ]
                },
            )
        return httpx.Response(200, request=req, json={"version": "0.6.0"})

    provider = OllamaProvider()
    monkeypatch.setattr(
        provider,
        "_client",
        lambda timeout_seconds=None: _FakeAsyncClient(handler, [], timeout_seconds),
    )

    snapshot = await provider.discover_runtime_state()
    by_id = {m.id: m for m in snapshot.installed_models}
    assert by_id["bool-ctx"].context_length is None
    assert by_id["bool-ctx"].max_architectural_context is None
    assert by_id["zero-ctx"].context_length is None
    assert by_id["zero-ctx"].max_architectural_context is None
    assert by_id["qwen3.5:9b"].context_length == 12288
    assert by_id["qwen3.5:9b"].max_architectural_context is None

    # Verify load/switch/Level-4 eviction update and clear runtime context_length accurately
    fake = FakeProvider(
        models=[_qwen_meta(context_length=4096), _coder_meta(context_length=16384)],
        running_models={"qwen3.5:9b"},
        running_contexts={"qwen3.5:9b": 8192, "qwen-coder:7b": 16384},
    )
    cms = CarbonModelSupervisor(
        provider=fake,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms.initialize()
    assert cms._registry["qwen3.5:9b"].context_length == 8192
    assert cms._registry["qwen-coder:7b"].context_length is None

    assert await cms.switch_model(
        ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="ctx sync")
    )
    assert cms._registry["qwen-coder:7b"].context_length == 16384
    assert cms._registry["qwen3.5:9b"].context_length is None

    await cms.handle_memory_pressure(4)
    assert cms._registry["qwen-coder:7b"].context_length is None
    assert cms._installed_models["qwen-coder:7b"].context_length is None


@pytest.mark.asyncio
async def test_snapshot_isolation_deep_copy_and_cancellation() -> None:
    """Verify frozen snapshots, deep-copied CMS models, and cancellation state preservation."""
    fake = FakeProvider(
        models=[_qwen_meta(), _coder_meta()],
        running_models={"qwen3.5:9b"},
    )
    snapshot = await fake.discover_runtime_state()
    with pytest.raises(ValidationError):
        snapshot.installed_models = ()  # type: ignore[misc]
    with pytest.raises(ValidationError):
        snapshot.running_models[0].context_length = 9999  # type: ignore[misc]

    # Mutating nested mutable fields on returned snapshot cannot affect provider or CMS
    snapshot.installed_models[0].capabilities.clear()
    snapshot.installed_models[0].context_length = 999999
    assert len(fake.models[0].capabilities) > 0

    cms = CarbonModelSupervisor(
        provider=fake,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    await cms.initialize()
    prior_active = await cms.get_active_model()
    assert prior_active is not None
    assert len(prior_active.capabilities) > 0
    assert cms._installed_models["qwen3.5:9b"] is not cms._registry["qwen3.5:9b"]
    cms._registry["qwen3.5:9b"].capabilities.append(ModelCapability.EMBEDDING)
    assert ModelCapability.EMBEDDING not in cms._installed_models["qwen3.5:9b"].capabilities

    # Cancellation mid-probe and mid-discovery leaves committed state intact and resets pending
    in_probe = asyncio.Event()
    gate = asyncio.Event()

    async def slow_probe() -> HealthProbeResult:
        in_probe.set()
        await gate.wait()
        return HealthProbeResult(status=HealthProbeStatus.HEALTHY, http_status=200)

    fake.probe_health = slow_probe  # type: ignore[method-assign]
    task = asyncio.create_task(cms.refresh_models(probe_health=True))
    await asyncio.wait_for(in_probe.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await cms.get_active_model()) is prior_active
    assert cms._switch_pending is False
    assert cms._switch_clear_event.is_set() is True

    # Restore probe and test cancellation mid-discover_runtime_state
    fake.probe_health = lambda: asyncio.sleep(  # type: ignore[method-assign]
        0, result=HealthProbeResult(status=HealthProbeStatus.HEALTHY, http_status=200)
    )
    in_discover = asyncio.Event()

    async def slow_discover() -> RuntimeDiscoverySnapshot:
        in_discover.set()
        await gate.wait()
        return RuntimeDiscoverySnapshot()

    fake.discover_runtime_state = slow_discover  # type: ignore[method-assign]
    task2 = asyncio.create_task(
        cms.switch_model(ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="cancel"))
    )
    await asyncio.wait_for(in_discover.wait(), timeout=1.0)
    task2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task2

    assert (await cms.get_active_model()) is prior_active
    assert cms._switch_pending is False
    assert cms._switch_clear_event.is_set() is True
