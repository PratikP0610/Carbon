"""Comprehensive unit and authority tests for Stage 4.2 Approved Model Registry."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from carbon.config.settings import DEFAULT_APPROVED_MODELS, DEFAULT_PRIMARY_MODEL, Settings
from carbon.core.orchestrator import CarbonCore
from carbon.governance.governance_engine import verify_audit_log_chain
from carbon.interfaces.models import (
    ModelAvailabilityStatus,
    ModelCapability,
    ModelMetadata,
    ModelNotApprovedError,
    ModelSwitchFailureReason,
    ModelSwitchRequest,
    ModelUnavailableError,
    ReasoningDepth,
    TaskRequest,
    TaskState,
)
from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.fake_provider import FakeProvider


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


def _unapproved_meta(model_id: str) -> ModelMetadata:
    return ModelMetadata(
        id=model_id,
        name=model_id,
        provider="ollama",
        family="gemma4" if "gemma" in model_id else "llama",
        capabilities=[
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
            ModelCapability.THINKING,
        ],
        is_healthy=True,
    )


def test_production_settings_hard_pins_qwen_and_blocks_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify approved allowlist is hard-pinned to qwen3.5:9b and unapproved primary is rejected."""
    assert DEFAULT_PRIMARY_MODEL == "qwen3.5:9b"
    assert DEFAULT_APPROVED_MODELS == ("qwen3.5:9b",)

    # CARBON_APPROVED_MODELS cannot expand the production allowlist
    monkeypatch.setenv("CARBON_APPROVED_MODELS", '["qwen3.5:9b", "gemma4:12b"]')
    s = Settings()
    assert s.approved_models == ("qwen3.5:9b",)
    assert s.primary_model == "qwen3.5:9b"

    # Unapproved or blank primary_model configuration must fail closed
    for bad_primary in ("gemma4:12b", "llama3:8b", "", "   "):
        with pytest.raises(ValidationError):
            Settings(primary_model=bad_primary)

    # Duplicate or blank entries rejected cleanly in CMS constructor
    with pytest.raises(ValueError, match="Duplicate"):
        CarbonModelSupervisor(
            FakeProvider(),
            default_model_id="qwen3.5:9b",
            approved_models=("qwen3.5:9b", "qwen3.5:9b"),
        )
    with pytest.raises(ValueError, match="non-empty"):
        CarbonModelSupervisor(
            FakeProvider(),
            default_model_id="qwen3.5:9b",
            approved_models=("qwen3.5:9b", "  "),
        )
    with pytest.raises(ValueError, match="not in approved"):
        CarbonModelSupervisor(FakeProvider(), default_model_id="gemma4:12b")


@pytest.mark.asyncio
async def test_qwen_approved_while_extra_installed_models_stay_unapproved() -> None:
    """Verify additional Ollama models remain unapproved and non-routable."""
    provider = FakeProvider(
        models=[
            _qwen_meta(),
            _unapproved_meta("gemma4:12b"),
            _unapproved_meta("llama3:8b"),
        ]
    )
    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()

    assert cms.is_model_approved("qwen3.5:9b") is True
    assert cms.is_model_approved("gemma4:12b") is False
    assert cms.is_model_approved("llama3:8b") is False

    approved = await cms.list_approved_models()
    registered = await cms.list_registered_models()
    installed = await cms.list_installed_models()

    assert [m.id for m in approved] == ["qwen3.5:9b"]
    assert [m.id for m in registered] == ["qwen3.5:9b"]
    assert approved[0].is_approved is True
    assert approved[0].is_installed is True
    assert approved[0].availability_status == ModelAvailabilityStatus.AVAILABLE

    assert {m.id for m in installed} == {"qwen3.5:9b", "gemma4:12b", "llama3:8b"}
    installed_map = {m.id: m for m in installed}
    assert installed_map["gemma4:12b"].is_approved is False
    assert installed_map["llama3:8b"].is_approved is False

    active = await cms.get_active_model()
    assert active is not None and active.id == "qwen3.5:9b"

    # Attempting to switch to unapproved installed model is rejected BEFORE drain/unload/load
    loads_before = list(provider.loaded_models)
    switched = await cms.switch_model(
        ModelSwitchRequest(target_model_id="gemma4:12b", reason="unauthorized switch")
    )
    assert switched is False
    assert cms.last_switch_failure == ModelSwitchFailureReason.UNAPPROVED
    assert provider.unloaded_models == []
    assert provider.loaded_models == loads_before
    assert not cms._switch_pending
    assert (await cms.get_active_model()) is active


@pytest.mark.asyncio
async def test_missing_qwen_does_not_substitute_installed_unapproved_model(
    tmp_path: Path,
) -> None:
    """Verify absent qwen3.5:9b is marked not_installed and unapproved models never substitute."""
    provider = FakeProvider(models=[_unapproved_meta("gemma4:12b"), _unapproved_meta("llama3:8b")])
    cms = CarbonModelSupervisor(provider=provider)
    await cms.initialize()

    assert await cms.get_active_model() is None
    approved = await cms.list_approved_models()
    assert len(approved) == 1
    assert approved[0].id == "qwen3.5:9b"
    assert approved[0].is_approved is True
    assert approved[0].is_installed is False
    assert approved[0].is_healthy is False
    assert approved[0].is_active is False
    assert approved[0].availability_status == ModelAvailabilityStatus.NOT_INSTALLED

    # Switch & selection fail closed; zero pulls/loads occur
    assert not await cms.switch_model(
        ModelSwitchRequest(target_model_id="qwen3.5:9b", reason="switch missing")
    )
    assert cms.last_switch_failure == ModelSwitchFailureReason.NOT_INSTALLED
    assert provider.loaded_models == []

    with pytest.raises(ModelUnavailableError) as exc_info:
        await cms.select_model_for_task([ModelCapability.CHAT], ReasoningDepth.INSTANT)
    assert exc_info.value.reason == ModelSwitchFailureReason.NOT_INSTALLED

    with pytest.raises(ModelNotApprovedError):
        cms.assert_model_authorized("gemma4:12b")

    # End-to-end CarbonCore fails task without touching provider inference
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "carbon.db",
        audit_log_path=tmp_path / "audit.log",
    )
    core = CarbonCore(settings=settings)
    core.provider = provider
    core.cms = cms
    await core.governance.initialize()
    await core.memory.initialize()
    core.is_running = True
    try:
        health = await core.get_system_health()
        assert health["ollama_connected"] is True
        assert health["active_model"] is None
        assert health["status"] == "degraded"
        assert health["model_availability"] == "not_installed"

        task = await core.create_task(TaskRequest(prompt="Say hello"))
        events = [e async for e in core.execute_task_stream(task)]
        assert task.state == TaskState.FAILED
        assert "not installed" in (task.error or "").lower()
        assert any(e.get("event") == "failed" for e in events)
        # Verify zero inference calls ever reached provider
        assert all(
            c["method"] not in ("generate_stream", "generate_stream_events", "chat_stream")
            for c in provider.calls
        )
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_offline_ollama_and_pre_inference_gate_prevent_unauthorized_inference(
    tmp_path: Path,
) -> None:
    """Verify offline Ollama fails closed accurately and pre-inference gate blocks bypasses."""
    offline_provider = FakeProvider(healthy=False, models=[_qwen_meta()])
    cms = CarbonModelSupervisor(provider=offline_provider)
    await cms.initialize()

    assert await cms.get_active_model() is None
    approved = await cms.list_approved_models()
    assert approved[0].availability_status == ModelAvailabilityStatus.OLLAMA_UNAVAILABLE
    assert approved[0].is_installed is False
    assert approved[0].is_healthy is False

    with pytest.raises(ModelUnavailableError) as exc_info:
        await cms.select_model_for_task([ModelCapability.CHAT], ReasoningDepth.INSTANT)
    assert exc_info.value.reason == ModelSwitchFailureReason.OLLAMA_UNAVAILABLE

    assert not await cms.switch_model(
        ModelSwitchRequest(target_model_id="qwen3.5:9b", reason="offline switch")
    )
    assert cms.last_switch_failure == ModelSwitchFailureReason.OLLAMA_UNAVAILABLE

    # Verify orchestrator pre-inference gate blocks even if select_model_for_task were bypassed
    healthy_provider = FakeProvider(models=[_qwen_meta(), _unapproved_meta("gemma4:12b")])
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "carbon.db",
        audit_log_path=tmp_path / "audit.log",
    )
    core = CarbonCore(settings=settings)
    core.provider = healthy_provider
    core.cms = CarbonModelSupervisor(
        provider=healthy_provider,
        default_model_id=settings.primary_model,
        approved_models=settings.approved_models,
    )
    await core.initialize()
    try:
        # Simulate compromised or buggy selection returning unapproved model ID
        core.cms.select_model_for_task = AsyncMock(  # type: ignore[method-assign]
            return_value=_unapproved_meta("gemma4:12b")
        )
        task = await core.create_task(TaskRequest(prompt="Test secret prompt"))
        events = [e async for e in core.execute_task_stream(task)]
        assert task.state == TaskState.FAILED
        assert "not approved" in (task.error or "").lower()
        assert any(e.get("event") == "failed" for e in events)
        assert all(c["method"] != "generate_stream_events" for c in healthy_provider.calls)

        ok, _, err = verify_audit_log_chain(settings.audit_log_path)
        assert ok, err
        assert "Test secret prompt" not in settings.audit_log_path.read_text(encoding="utf-8")
    finally:
        await core.shutdown()
