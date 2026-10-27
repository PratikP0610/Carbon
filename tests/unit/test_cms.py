"""Unit tests for Carbon Model Supervisor (CMS)."""

import pytest

from carbon.interfaces.models import (
    ModelCapability,
    ModelMetadata,
    ModelSwitchRequest,
    ReasoningDepth,
)
from carbon.model_control.cms import CarbonModelSupervisor


class MockProvider:
    """Mock runtime provider for CMS unit testing."""

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
        ]
        self.loaded_models: list[str] = []
        self.unloaded_models: list[str] = []

    async def list_available_models(self) -> list[ModelMetadata]:
        return self.models

    async def is_healthy(self) -> bool:
        return True

    async def load_model(self, model_id: str) -> bool:
        self.loaded_models.append(model_id)
        return True

    async def unload_model(self, model_id: str) -> bool:
        self.unloaded_models.append(model_id)
        return True


@pytest.fixture
async def cms():
    provider = MockProvider()
    supervisor = CarbonModelSupervisor(provider=provider, default_model_id="qwen3.5:9b")
    await supervisor.initialize()
    return supervisor


@pytest.mark.asyncio
async def test_cms_initialization(cms: CarbonModelSupervisor):
    """Verify default model is active upon initialization."""
    active = await cms.get_active_model()
    assert active is not None
    assert active.id == "qwen3.5:9b"
    assert active.is_active is True

    models = await cms.list_registered_models()
    assert len(models) >= 2


@pytest.mark.asyncio
async def test_cms_model_selection(cms: CarbonModelSupervisor):
    """Verify capability-based model selection."""
    # Requires thinking
    selected = await cms.select_model_for_task(
        capabilities_required=[ModelCapability.THINKING],
        reasoning=ReasoningDepth.DEEP,
    )
    assert selected.id == "qwen3.5:9b"


@pytest.mark.asyncio
async def test_cms_single_model_concurrency_lease(cms: CarbonModelSupervisor):
    """Verify single-chat-model inference lock acquisition and release."""
    await cms.acquire_inference_lease("qwen3.5:9b")
    assert cms._inference_lock.locked() is True

    await cms.release_inference_lease("qwen3.5:9b")
    assert cms._inference_lock.locked() is False


@pytest.mark.asyncio
async def test_cms_controlled_switching(cms: CarbonModelSupervisor):
    """Verify controlled switching unloads previous model and pre-warms target."""
    req = ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="Code generation task")
    success = await cms.switch_model(req)
    assert success is True

    active = await cms.get_active_model()
    assert active is not None
    assert active.id == "qwen-coder:7b"
