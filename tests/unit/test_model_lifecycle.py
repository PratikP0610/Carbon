"""Stage 4.3C lifecycle integration, API status mapping, audit, memory, and live guarded tests."""

import json
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import carbon.main as main_module
from carbon.config.settings import DEFAULT_APPROVED_MODELS, Settings, get_settings
from carbon.core.logging import TokenSanitizingFilter
from carbon.core.orchestrator import CarbonCore
from carbon.governance.governance_engine import verify_audit_log_chain
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
    TaskRequest,
    TaskState,
)
from carbon.main import create_app
from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.fake_provider import FakeProvider
from carbon.model_control.ollama_provider import OllamaProvider


def _qwen_meta() -> ModelMetadata:
    return ModelMetadata(
        id="qwen3.5:9b",
        name="Qwen 3.5 9B",
        provider="ollama",
        family="qwen35",
        context_length=4096,
        max_architectural_context=262144,
        capabilities=[
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
            ModelCapability.THINKING,
        ],
        is_healthy=True,
    )


def _unapproved_meta(model_id: str) -> ModelMetadata:
    return ModelMetadata(
        id=model_id,
        name=model_id,
        provider="ollama",
        family="external",
        capabilities=[ModelCapability.CHAT],
        is_healthy=True,
    )


def _make_settings(tmp_path: Path, *, api_token: str | None = None) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir,
        database_path=data_dir / "carbon.db",
        audit_log_path=data_dir / "audit.log",
        api_token=api_token,
    )


def _create_isolated_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: str = "stage43c-secret-bearer-token-999",
) -> tuple[Any, Settings, dict[str, str]]:
    data_dir = tmp_path / "api_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    token_file = data_dir / ".carbon_token"
    token_file.write_text(token, encoding="utf-8")
    os.chmod(token_file, 0o600)

    monkeypatch.setenv("CARBON_API_TOKEN", token)
    monkeypatch.setenv("CARBON_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CARBON_DATABASE_PATH", str(data_dir / "carbon.db"))
    monkeypatch.setenv("CARBON_AUDIT_LOG_PATH", str(data_dir / "audit.log"))
    get_settings.cache_clear()
    main_module.core = None

    settings = get_settings()
    app = create_app()
    app.state.api_token = token
    headers = {"Authorization": f"Bearer {token}"}
    return app, settings, headers


@pytest.mark.asyncio
async def test_runtime_availability_transitions_offline_to_online_outage_and_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify cold offline -> online activation -> confirmed outage -> recovery across API and CMS."""
    state: dict[str, Any] = {
        "healthy": False,
        "installed": (),
        "running": (),
        "loaded_calls": 0,
    }

    async def _probe_health() -> HealthProbeResult:
        if state["healthy"]:
            return HealthProbeResult(status=HealthProbeStatus.HEALTHY, http_status=200)
        return HealthProbeResult(status=HealthProbeStatus.UNREACHABLE)

    async def _discover() -> RuntimeDiscoverySnapshot:
        if not state["healthy"]:
            raise RuntimeDiscoveryError(
                "Connection refused",
                probe_status=HealthProbeStatus.UNREACHABLE,
                endpoint="/api/tags",
            )
        return RuntimeDiscoverySnapshot(
            installed_models=state["installed"],
            running_models=state["running"],
        )

    async def _list_running_runtime_info() -> tuple[RunningModelRuntimeInfo, ...]:
        if not state["healthy"]:
            raise RuntimeDiscoveryError(
                "Connection refused",
                probe_status=HealthProbeStatus.UNREACHABLE,
                endpoint="/api/ps",
            )
        return state["running"]

    async def _load_model(model_id: str) -> bool:
        if not state["healthy"] or model_id != "qwen3.5:9b":
            return False
        state["loaded_calls"] += 1
        state["running"] = (RunningModelRuntimeInfo(id="qwen3.5:9b", context_length=4096),)
        return True

    monkeypatch.setattr(OllamaProvider, "probe_health", staticmethod(_probe_health))
    monkeypatch.setattr(OllamaProvider, "discover_runtime_state", staticmethod(_discover))
    monkeypatch.setattr(
        OllamaProvider, "list_running_runtime_info", staticmethod(_list_running_runtime_info)
    )
    monkeypatch.setattr(OllamaProvider, "load_model", staticmethod(_load_model))
    monkeypatch.setattr(
        OllamaProvider, "is_healthy", staticmethod(AsyncMock(side_effect=lambda: state["healthy"]))
    )

    app, _, headers = _create_isolated_app(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            # 1. Cold start offline -> active_model is None, approved model marks OLLAMA_UNAVAILABLE
            r1 = client.get("/api/v1/models", headers=headers)
            assert r1.status_code == 200
            b1 = r1.json()
            assert b1["active_model"] is None
            assert (
                b1["approved_models"][0]["availability_status"]
                == ModelAvailabilityStatus.OLLAMA_UNAVAILABLE.value
            )

            # Fail-closed inference rejection while offline
            assert main_module.core is not None
            task_offline = await main_module.core.create_task(TaskRequest(prompt="Say hello"))
            ev_offline = [e async for e in main_module.core.execute_task_stream(task_offline)]
            assert task_offline.state == TaskState.FAILED
            assert ev_offline[-1]["event"] == "failed"

            # 2. Ollama comes online with qwen3.5:9b installed -> POST /models/refresh prewarms and activates
            state["healthy"] = True
            state["installed"] = (_qwen_meta(),)
            state["running"] = ()

            r2 = client.post("/api/v1/models/refresh", headers=headers)
            assert r2.status_code == 200
            b2 = r2.json()
            assert b2["summary"]["ollama_connected"] is True
            assert b2["active_model"]["id"] == "qwen3.5:9b"
            assert b2["active_model"]["is_running"] is True
            assert (
                b2["active_model"]["availability_status"] == ModelAvailabilityStatus.AVAILABLE.value
            )
            assert b2["active_model"]["context_length"] == 4096
            assert state["loaded_calls"] == 1

            # 3. Runtime outage -> confirmed UNREACHABLE transitions cleanly back to unavailable
            state["healthy"] = False
            state["installed"] = ()
            state["running"] = ()

            r3 = client.post("/api/v1/models/refresh", headers=headers)
            assert r3.status_code == 200
            b3 = r3.json()
            assert b3["summary"]["ollama_connected"] is False
            assert b3["active_model"] is None
            assert (
                b3["approved_models"][0]["availability_status"]
                == ModelAvailabilityStatus.OLLAMA_UNAVAILABLE.value
            )

            # 4. Ollama recovers -> next refresh restores qwen3.5:9b cleanly
            state["healthy"] = True
            state["installed"] = (_qwen_meta(),)
            r4 = client.post("/api/v1/models/refresh", headers=headers)
            assert r4.status_code == 200
            b4 = r4.json()
            assert b4["active_model"]["id"] == "qwen3.5:9b"
            assert (
                b4["active_model"]["availability_status"] == ModelAvailabilityStatus.AVAILABLE.value
            )
    finally:
        main_module.core = None
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_unapproved_models_never_become_approved_through_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify unapproved installed/running models never enter approved allowlist via refresh."""
    qwen = _qwen_meta()
    gemma = _unapproved_meta("gemma4:12b")
    mistral = _unapproved_meta("mistral:7b")

    provider = FakeProvider(
        models=[gemma, mistral],
        running_models={"gemma4:12b"},
    )
    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()

    # Even when default approved model is missing and unapproved gemma4:12b IS running:
    # 1. Approved allowlist remains strictly DEFAULT_APPROVED_MODELS (qwen3.5:9b)
    # 2. Unapproved running model is never promoted to active_model
    approved = await cms.list_approved_models()
    assert tuple(m.id for m in approved) == DEFAULT_APPROVED_MODELS
    assert cms.is_model_approved("gemma4:12b") is False
    assert cms.is_model_approved("mistral:7b") is False
    assert await cms.get_active_model() is None

    installed_map = {m.id: m for m in await cms.list_installed_models()}
    assert set(installed_map) == {"gemma4:12b", "mistral:7b"}
    assert installed_map["gemma4:12b"].is_approved is False
    assert installed_map["gemma4:12b"].is_running is True

    # Subsequent refresh when qwen3.5:9b is also installed activates only qwen3.5:9b
    provider.models = [qwen, gemma, mistral]
    await cms.refresh_models(probe_health=True)
    active = await cms.get_active_model()
    assert active is not None and active.id == "qwen3.5:9b"
    assert [m.id for m in await cms.list_approved_models()] == ["qwen3.5:9b"]

    # Switch to discovered/running unapproved model stays forbidden
    assert (
        await cms.switch_model(ModelSwitchRequest(target_model_id="gemma4:12b", reason="promote"))
        is False
    )
    assert cms.last_switch_failure == ModelSwitchFailureReason.UNAPPROVED

    # HTTP API contract consistency check across POST /api/v1/models/refresh + /switch
    monkeypatch.setattr(
        OllamaProvider,
        "probe_health",
        AsyncMock(
            return_value=HealthProbeResult(status=HealthProbeStatus.HEALTHY, http_status=200)
        ),
    )
    monkeypatch.setattr(
        OllamaProvider,
        "discover_runtime_state",
        AsyncMock(
            return_value=RuntimeDiscoverySnapshot(
                installed_models=(qwen, gemma, mistral),
                running_models=(
                    RunningModelRuntimeInfo(id="qwen3.5:9b", context_length=4096),
                    RunningModelRuntimeInfo(id="gemma4:12b", context_length=8192),
                ),
            )
        ),
    )
    monkeypatch.setattr(
        OllamaProvider,
        "list_running_runtime_info",
        AsyncMock(return_value=(RunningModelRuntimeInfo(id="qwen3.5:9b", context_length=4096),)),
    )
    monkeypatch.setattr(OllamaProvider, "is_healthy", AsyncMock(return_value=True))
    app, _, headers = _create_isolated_app(tmp_path / "http", monkeypatch)
    try:
        with TestClient(app) as client:
            ref = client.post("/api/v1/models/refresh", headers=headers)
            assert ref.status_code == 200
            payload = ref.json()
            assert [m["id"] for m in payload["approved_models"]] == ["qwen3.5:9b"]
            assert [m["id"] for m in payload["registered_models"]] == ["qwen3.5:9b"]
            assert payload["active_model"]["id"] == "qwen3.5:9b"
            assert (
                client.post(
                    "/api/v1/models/switch",
                    headers=headers,
                    json={"target_model_id": "gemma4:12b", "reason": "unapproved"},
                ).status_code
                == 403
            )
    finally:
        main_module.core = None
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_audit_hash_chain_integrity_tamper_detection_and_secret_redaction(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify hash-chain audit integrity, tamper detection, and credential redaction on failures."""
    leaked_bearer = "sk-live-secret-bearer-token-987654"
    leaked_query_tok = "tokval-should-not-leak-abcdef1234"
    leaked_password = "super-secret-password-999"

    provider = FakeProvider(
        models=[_qwen_meta()],
        running_models={"qwen3.5:9b"},
        simulate_error=ConnectionError(
            f"Upstream failed with Bearer {leaked_bearer} token={leaked_query_tok} 'password': '{leaked_password}'"
        ),
    )
    settings = _make_settings(tmp_path)
    core = CarbonCore(settings=settings)
    core.provider = provider
    core.cms = CarbonModelSupervisor(provider=provider)
    await core.initialize()

    core_logger = logging.getLogger("carbon.core.orchestrator")
    token_filter = TokenSanitizingFilter()
    core_logger.addFilter(token_filter)
    caplog.handler.addFilter(token_filter)
    try:
        # 1. Fail closed on unapproved model switch rejection in audit log
        await core.governance.record_audit_entry(
            "MODEL_SWITCH_REJECTED",
            {
                "target_model_id": "gemma4:12b",
                "reason": ModelSwitchFailureReason.UNAPPROVED.value,
                "token": leaked_query_tok,
                "authorization": f"Bearer {leaked_bearer}",
            },
            level="WARNING",
        )

        # 2. Failed active inference writes sanitized MODEL_INFERENCE_FAILED into audit chain
        with caplog.at_level(logging.ERROR, logger="carbon.core.orchestrator"):
            task = await core.create_task(TaskRequest(prompt="Explain gravity"))
            events = [e async for e in core.execute_task_stream(task)]
            assert task.state == TaskState.FAILED
            assert events[-1]["event"] == "failed"

        audit_text = settings.audit_log_path.read_text(encoding="utf-8")
        assert "MODEL_SWITCH_REJECTED" in audit_text
        assert "MODEL_INFERENCE_FAILED" in audit_text
        for secret in (leaked_bearer, leaked_query_tok, leaked_password):
            assert secret not in audit_text
            assert secret not in caplog.text

        ok, entry_count, err = verify_audit_log_chain(settings.audit_log_path)
        assert ok is True and entry_count >= 3, err

        # 3. Any post-hoc modification to audit.log breaks hash chain verification
        tampered_path = tmp_path / "tampered_audit.log"
        lines = settings.audit_log_path.read_text(encoding="utf-8").splitlines()
        last = json.loads(lines[-1])
        last["level"] = "INFO"
        lines[-1] = json.dumps(last)
        tampered_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tamper_ok, _, tamper_err = verify_audit_log_chain(tampered_path)
        assert tamper_ok is False
        assert tamper_err is not None and "entry_hash mismatch" in tamper_err
    finally:
        caplog.handler.removeFilter(token_filter)
        core_logger.removeFilter(token_filter)
        await core.shutdown()


@pytest.mark.asyncio
async def test_multiturn_conversation_preserves_sqlite_memory_across_outage_and_reload(
    tmp_path: Path,
) -> None:
    """Verify episodic and semantic SQLite memory context remains intact across outage and reload."""
    conv_id = "conv-stage-4-3c-memory"
    db_dir = tmp_path / "persistent_state"
    settings = Settings(
        data_dir=db_dir,
        database_path=db_dir / "carbon.db",
        audit_log_path=db_dir / "audit.log",
    )

    provider1 = FakeProvider(
        models=[_qwen_meta()],
        running_models={"qwen3.5:9b"},
        default_response="Neovim starts fast because it uses async RPC and LuaJIT.",
    )
    core1 = CarbonCore(settings=settings)
    core1.provider = provider1
    core1.cms = CarbonModelSupervisor(provider=provider1)
    await core1.initialize()
    try:
        # Turn 1: Explicit semantic memory storage + episodic turn in SQLite
        t1 = await core1.create_task(
            TaskRequest(
                prompt="Remember that my favorite editor is Neovim",
                conversation_id=conv_id,
            )
        )
        _ = [e async for e in core1.execute_task_stream(t1)]
        assert t1.state == TaskState.COMPLETED
        assert "Neovim" in (t1.result or "")

        sem_rec = await core1.memory.retrieve("user:preference:favorite_editor")
        assert sem_rec is not None and "Neovim" in sem_rec.content

        # Turn 2: Conversational inference writes episodic turn under same conversation_id
        t2 = await core1.create_task(
            TaskRequest(
                prompt="Explain why Neovim starts quickly",
                conversation_id=conv_id,
            )
        )
        _ = [e async for e in core1.execute_task_stream(t2)]
        assert t2.state == TaskState.COMPLETED
        assert t2.model_used == "qwen3.5:9b"

        # Simulated Ollama outage -> Turn 3 fails cleanly without corrupting SQLite memories
        provider1.healthy = False
        provider1.running_models.clear()
        t3 = await core1.create_task(
            TaskRequest(
                prompt="How does my favorite editor load plugins?",
                conversation_id=conv_id,
            )
        )
        _ = [e async for e in core1.execute_task_stream(t3)]
        assert t3.state == TaskState.FAILED
        assert core1.cms.ollama_available is False
        assert await core1.cms.get_active_model() is None

        # Ollama returns & force model reload executes cleanly
        provider1.healthy = True
        provider1.running_models = {"qwen3.5:9b"}
        provider1.default_response = "Your favorite editor is Neovim."
        await core1.cms.refresh_models(probe_health=True)
        reloaded = await core1.cms.switch_model(
            ModelSwitchRequest(
                target_model_id="qwen3.5:9b",
                reason="post_outage_reload",
                force=True,
            )
        )
        assert reloaded is True
        assert "qwen3.5:9b" in provider1.unloaded_models
        assert "qwen3.5:9b" in provider1.loaded_models

        # Turn 4: Semantic + episodic context both reach the reloaded model prompt
        t4 = await core1.create_task(
            TaskRequest(
                prompt="What is my favorite editor?",
                conversation_id=conv_id,
            )
        )
        _ = [e async for e in core1.execute_task_stream(t4)]
        assert t4.state == TaskState.COMPLETED

        gen_calls = [c for c in provider1.calls if c["method"] == "generate_stream_events"]
        last_system = gen_calls[-1]["system"]
        assert "Neovim" in last_system
        assert "Explain why Neovim starts quickly" in last_system
        assert "async RPC and LuaJIT" in last_system
    finally:
        await core1.shutdown()

    # Re-open a brand new CarbonCore against the same SQLite DB (full process reload simulation)
    provider2 = FakeProvider(
        models=[_qwen_meta()],
        running_models={"qwen3.5:9b"},
        default_response="Confirmed after full restart: Neovim.",
    )
    core2 = CarbonCore(settings=settings)
    core2.provider = provider2
    core2.cms = CarbonModelSupervisor(provider=provider2)
    await core2.initialize()
    try:
        t5 = await core2.create_task(
            TaskRequest(
                prompt="What is my favorite editor?",
                conversation_id=conv_id,
            )
        )
        _ = [e async for e in core2.execute_task_stream(t5)]
        assert t5.state == TaskState.COMPLETED
        assert t5.result == "Confirmed after full restart: Neovim."

        post_restart_calls = [c for c in provider2.calls if c["method"] == "generate_stream_events"]
        restarted_system = post_restart_calls[-1]["system"]
        assert "Neovim" in restarted_system
        assert "Explain why Neovim starts quickly" in restarted_system

        turns = await core2.memory.get_recent_conversation_turns(conversation_id=conv_id, limit=10)
        assert len(turns) >= 3
        persisted_task = await core2.get_task_persistent(t5.task_id)
        assert persisted_task is not None and persisted_task["state"] == TaskState.COMPLETED.value
    finally:
        await core2.shutdown()


@pytest.mark.requires_ollama
@pytest.mark.asyncio
async def test_live_ollama_guarded_readonly_lifecycle_and_approval_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guarded live Ollama check: read-only discovery, CMS refresh, API endpoints, no mutation."""
    provider = OllamaProvider()
    probe = await provider.probe_health()
    if not probe.is_healthy:
        pytest.skip(f"Ollama daemon is not running locally ({probe.status.value}).")

    # 1. Strictly read-only OllamaProvider discovery against live daemon
    snapshot = await provider.discover_runtime_state()
    available = await provider.list_available_models()
    running = await provider.list_running_runtime_info()

    installed_ids = {m.id.strip() for m in snapshot.installed_models if m.id.strip()}
    running_ids = {rt.id.strip() for rt in snapshot.running_models if rt.id.strip()}
    assert installed_ids == {m.id.strip() for m in available if m.id.strip()}
    assert running_ids == {rt.id.strip() for rt in running if rt.id.strip()}
    for model in snapshot.installed_models:
        assert not isinstance(model.context_length, bool)
        if model.max_architectural_context is not None:
            assert not isinstance(model.max_architectural_context, bool)
    for rt in running:
        assert not isinstance(rt.context_length, bool)

    async def _forbid_live_mutation(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "Live guarded Stage 4.3C test must never mutate models or run inference."
        )

    # Never unload models or execute live inference during lifecycle check; if default model
    # is installed on the host but not currently loaded in /api/ps, keep load_model off-wire.
    default_installed = "qwen3.5:9b" in installed_ids
    default_running = "qwen3.5:9b" in running_ids
    load_guard = (
        AsyncMock(return_value=False)
        if (default_installed and not default_running)
        else AsyncMock(side_effect=_forbid_live_mutation)
    )
    monkeypatch.setattr(OllamaProvider, "load_model", load_guard)
    monkeypatch.setattr(
        OllamaProvider, "unload_model", AsyncMock(side_effect=_forbid_live_mutation)
    )
    for infer_method in (
        "generate",
        "generate_stream",
        "generate_stream_events",
        "chat",
        "chat_stream",
    ):
        monkeypatch.setattr(OllamaProvider, infer_method, _forbid_live_mutation)

    # 2. CarbonModelSupervisor.refresh_models() + approval boundary against live daemon
    cms = CarbonModelSupervisor(provider=provider)
    summary = await cms.refresh_models(probe_health=True)
    assert summary["ollama_connected"] is True
    assert summary["approved_count"] == len(DEFAULT_APPROVED_MODELS)
    assert summary["installed_count"] == len(snapshot.installed_models)
    assert cms.is_model_approved("unapproved-stage43c-probe:latest") is False
    switched = await cms.switch_model(
        ModelSwitchRequest(
            target_model_id="unapproved-stage43c-probe:latest",
            reason="live_readonly_guard",
        )
    )
    assert switched is False
    assert cms.last_switch_failure == ModelSwitchFailureReason.UNAPPROVED

    # 3. Authenticated GET /api/v1/models and POST /api/v1/models/refresh against live daemon
    app, _, headers = _create_isolated_app(tmp_path / "live_api", monkeypatch)
    try:
        with TestClient(app) as client:
            assert client.get("/api/v1/models").status_code == 401
            assert client.post("/api/v1/models/refresh").status_code == 401

            get_res = client.get("/api/v1/models", headers=headers)
            assert get_res.status_code == 200
            get_body = get_res.json()
            assert [m["id"] for m in get_body["approved_models"]] == list(DEFAULT_APPROVED_MODELS)
            assert [m["id"] for m in get_body["registered_models"]] == list(DEFAULT_APPROVED_MODELS)
            assert {m["id"] for m in get_body["installed_models"]} == installed_ids

            ref_res = client.post("/api/v1/models/refresh", headers=headers)
            assert ref_res.status_code == 200
            ref_body = ref_res.json()
            assert ref_body["status"] == "refreshed"
            assert ref_body["summary"]["ollama_connected"] is True
            assert ref_body["summary"]["installed_count"] == len(snapshot.installed_models)
            assert [m["id"] for m in ref_body["approved_models"]] == list(DEFAULT_APPROVED_MODELS)
            assert {m["id"] for m in ref_body["installed_models"]} == installed_ids
            if default_running:
                assert ref_body["active_model"] is not None
                assert ref_body["active_model"]["id"] == "qwen3.5:9b"
                assert ref_body["active_model"]["is_running"] is True
            else:
                assert ref_body["active_model"] is None
    finally:
        main_module.core = None
        get_settings.cache_clear()
