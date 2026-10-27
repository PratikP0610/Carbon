"""Unit tests for Dynamic Memory Management, Pressure Ladder, and RWLock Lease Safety."""

import asyncio

import pytest

from carbon.interfaces.models import (
    ModelCapability,
    ModelMetadata,
    ModelSwitchRequest,
)
from carbon.model_control.cms import CarbonModelSupervisor


class ControllableMockProvider:
    """Mock provider with controllable failure injection for unload and load."""

    def __init__(self):
        self.models: list[ModelMetadata] = [
            ModelMetadata(
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
            ),
            ModelMetadata(
                id="qwen-coder:7b",
                name="Qwen Coder 7B",
                provider="ollama",
                family="qwen_coder",
                capabilities=[
                    ModelCapability.COMPLETION,
                    ModelCapability.CHAT,
                    ModelCapability.TOOLS,
                ],
            ),
            ModelMetadata(
                id="failing-model",
                name="Failing Model",
                provider="ollama",
                family="failing",
                capabilities=[ModelCapability.CHAT],
            ),
        ]
        self.loaded_models: list[str] = []
        self.unloaded_models: list[str] = []
        self.fail_unload = False
        self.fail_load_models: set[str] = {"failing-model"}

    async def list_available_models(self) -> list[ModelMetadata]:
        return self.models

    async def is_healthy(self) -> bool:
        return True

    async def load_model(self, model_id: str) -> bool:
        if model_id in self.fail_load_models:
            return False
        self.loaded_models.append(model_id)
        return True

    async def unload_model(self, model_id: str) -> bool:
        if self.fail_unload:
            return False
        self.unloaded_models.append(model_id)
        return True


@pytest.fixture
async def cms_env():
    provider = ControllableMockProvider()
    supervisor = CarbonModelSupervisor(provider=provider, default_model_id="qwen3.5:9b")
    await supervisor.initialize()
    return supervisor, provider


@pytest.mark.asyncio
async def test_memory_pressure_level_1_normal(cms_env):
    """Verify Level 1 (Normal) maintains standard operation and 32K context."""
    supervisor, provider = cms_env
    res = await supervisor.handle_memory_pressure(1)

    assert res["level"] == 1
    assert supervisor.current_pressure_level == 1
    assert supervisor.max_context_tokens == 32768
    active = await supervisor.get_active_model()
    assert active is not None
    assert active.is_active is True


@pytest.mark.asyncio
async def test_memory_pressure_level_2_warning(cms_env):
    """Verify Level 2 (Warning) unloads idle models and caps context to 8K."""
    supervisor, provider = cms_env
    res = await supervisor.handle_memory_pressure(2)

    assert res["level"] == 2
    assert supervisor.current_pressure_level == 2
    assert supervisor.max_context_tokens == 8192
    assert supervisor.idle_specialist_models_unloaded is True


@pytest.mark.asyncio
async def test_memory_pressure_level_4_critical(cms_env):
    """Verify Level 4 (Critical) unloads active chat model and restricts context to 4K."""
    supervisor, provider = cms_env
    res = await supervisor.handle_memory_pressure(4)

    assert res["level"] == 4
    assert supervisor.current_pressure_level == 4
    assert supervisor.max_context_tokens == 4096
    assert "qwen3.5:9b" in provider.unloaded_models
    active = await supervisor.get_active_model()
    assert active.is_active is False


@pytest.mark.asyncio
async def test_no_model_unloading_during_active_inference(cms_env):
    """Verify model switching CANNOT unload the active model while inference lease is held."""
    supervisor, provider = cms_env

    # 1. Acquire inference lease for task-1
    await supervisor.acquire_inference_lease("task-1")
    assert "task-1" in supervisor._active_leases
    assert supervisor._drain_event.is_set() is False

    # 2. Dispatch model switch in a background task
    switch_req = ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="Code generation")
    switch_task = asyncio.create_task(supervisor.switch_model(switch_req))

    # Give switch_task a moment to run and set _switch_pending
    await asyncio.sleep(0.05)
    assert supervisor._switch_pending is True

    # 3. Verify active model has NOT been unloaded yet
    assert "qwen3.5:9b" not in provider.unloaded_models
    active = await supervisor.get_active_model()
    assert active.id == "qwen3.5:9b"
    assert active.is_active is True

    # 4. Now release the inference lease for task-1
    await supervisor.release_inference_lease("task-1")

    # 5. Switch task should now drain and complete successfully
    switch_success = await switch_task
    assert switch_success is True
    assert "qwen3.5:9b" in provider.unloaded_models
    active_after = await supervisor.get_active_model()
    assert active_after.id == "qwen-coder:7b"
    assert active_after.is_active is True


@pytest.mark.asyncio
async def test_no_simultaneous_chat_llm_inference(cms_env):
    """Verify Single Active Chat LLM Concurrency prevents concurrent inference leases."""
    supervisor, provider = cms_env

    # Acquire lease for Task A
    await supervisor.acquire_inference_lease("task-A")
    assert supervisor._inference_lock.locked() is True

    # Attempt to acquire lease for Task B with short timeout -> should raise TimeoutError
    with pytest.raises(TimeoutError):
        await supervisor.acquire_inference_lease("task-B", timeout=0.1)

    # Release Task A
    await supervisor.release_inference_lease("task-A")
    assert supervisor._inference_lock.locked() is False

    # Task B can now acquire cleanly
    await supervisor.acquire_inference_lease("task-B")
    assert "task-B" in supervisor._active_leases
    await supervisor.release_inference_lease("task-B")


@pytest.mark.asyncio
async def test_recovery_after_failed_unload(cms_env):
    """Verify switch aborts safely if model unloading fails, keeping previous model active."""
    supervisor, provider = cms_env
    provider.fail_unload = True

    switch_req = ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="Code generation")
    success = await supervisor.switch_model(switch_req)

    # Switch must be rejected to prevent running two models concurrently
    assert success is False
    active = await supervisor.get_active_model()
    assert active.id == "qwen3.5:9b"
    assert active.is_active is True


@pytest.mark.asyncio
async def test_recovery_after_failed_model_loading(cms_env):
    """Verify automated rollback to primary default model if target model fails to load."""
    supervisor, provider = cms_env

    switch_req = ModelSwitchRequest(target_model_id="failing-model", reason="Test failing load")
    success = await supervisor.switch_model(switch_req)

    assert success is False
    # Verified automated rollback to default model
    assert "qwen3.5:9b" in provider.loaded_models
    active = await supervisor.get_active_model()
    assert active.id == "qwen3.5:9b"
    assert active.is_active is True
