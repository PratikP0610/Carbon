"""Carbon Model Supervisor (CMS) implementation."""

import asyncio
import logging

from carbon.interfaces.models import (
    ModelCapability,
    ModelMetadata,
    ModelSwitchRequest,
    ReasoningDepth,
)
from carbon.interfaces.protocols import ICarbonModelSupervisor, IModelProvider

logger = logging.getLogger(__name__)


class CarbonModelSupervisor(ICarbonModelSupervisor):
    """Sole authority governing model registration, selection, switching, and concurrency."""

    def __init__(self, provider: IModelProvider, default_model_id: str = "qwen3.5:9b"):
        self.provider = provider
        self.default_model_id = default_model_id
        self._registry: dict[str, ModelMetadata] = {}
        self._active_model: ModelMetadata | None = None
        # Enforces Single Active Chat LLM Concurrency (ADR-0003)
        self._inference_lock = asyncio.Lock()
        self._switch_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Discover and register available models from the local provider."""
        logger.info("Initializing Carbon Model Supervisor (CMS)...")
        available_models = await self.provider.list_available_models()
        self._registry.clear()

        for model in available_models:
            self._registry[model.id] = model
            logger.info(f"Registered model: {model.id} ({model.family}, {model.parameter_size})")

        # Ensure default model is registered or synthesized if offline
        if self.default_model_id not in self._registry:
            fallback = ModelMetadata(
                id=self.default_model_id,
                name=self.default_model_id,
                provider="ollama",
                family="qwen35",
                parameter_size="9.0B",
                quantization="Q4_K_M",
                context_length=262144,
                memory_footprint_mb=6200,
                capabilities=[
                    ModelCapability.COMPLETION,
                    ModelCapability.CHAT,
                    ModelCapability.TOOLS,
                    ModelCapability.THINKING,
                    ModelCapability.VISION,
                ],
                is_active=False,
                is_healthy=True,
            )
            self._registry[self.default_model_id] = fallback

        # Set default model as active target
        self._active_model = self._registry.get(self.default_model_id)
        if self._active_model:
            self._active_model.is_active = True
        logger.info(f"CMS active model set to: {self.default_model_id}")

    async def get_active_model(self) -> ModelMetadata | None:
        """Return metadata for the currently active model."""
        return self._active_model

    async def list_registered_models(self) -> list[ModelMetadata]:
        """Return all models currently registered with CMS."""
        return list(self._registry.values())

    async def select_model_for_task(
        self,
        capabilities_required: list[ModelCapability],
        reasoning: ReasoningDepth,
    ) -> ModelMetadata:
        """Select eligible model capable of fulfilling requirements."""
        # Check active model first
        if self._active_model and self._active_model.is_healthy:
            has_all_caps = all(
                cap in self._active_model.capabilities for cap in capabilities_required
            )
            if has_all_caps:
                return self._active_model

        # Search registry for suitable model
        for model in self._registry.values():
            if not model.is_healthy:
                continue
            if all(cap in model.capabilities for cap in capabilities_required):
                return model

        # Fallback to active or default model
        if self._active_model:
            logger.warning("No model matched all capabilities perfectly; using active model.")
            return self._active_model

        return self._registry[self.default_model_id]

    async def switch_model(self, request: ModelSwitchRequest) -> bool:
        """Execute a controlled model switch under mutual exclusion."""
        async with self._switch_lock:
            target_id = request.target_model_id
            if target_id not in self._registry:
                logger.error(f"Cannot switch to unknown model: {target_id}")
                return False

            if self._active_model and self._active_model.id == target_id and not request.force:
                logger.info(f"Model '{target_id}' is already active.")
                return True

            logger.info(
                f"Switching model: {self._active_model.id if self._active_model else 'None'} -> {target_id} (Reason: {request.reason})"
            )

            # 1. Unload previous model weights to free unified memory
            if self._active_model:
                await self.provider.unload_model(self._active_model.id)
                self._active_model.is_active = False

            # 2. Pre-warm target model
            success = await self.provider.load_model(target_id)
            if not success:
                logger.error(
                    f"Failed to pre-warm target model '{target_id}', initiating rollback..."
                )
                # Fallback rollback
                if self.default_model_id in self._registry:
                    await self.provider.load_model(self.default_model_id)
                    self._active_model = self._registry[self.default_model_id]
                    self._active_model.is_active = True
                return False

            self._active_model = self._registry[target_id]
            self._active_model.is_active = True
            logger.info(f"Model switch to '{target_id}' completed successfully.")
            return True

    async def acquire_inference_lease(self, model_id: str) -> None:
        """Acquire single-model concurrency lock prior to inference."""
        await self._inference_lock.acquire()

    async def release_inference_lease(self, model_id: str) -> None:
        """Release concurrency lock after inference completes."""
        if self._inference_lock.locked():
            self._inference_lock.release()
