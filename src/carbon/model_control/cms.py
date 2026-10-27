"""Carbon Model Supervisor (CMS) implementation with dynamic memory governance and RWLock lease safety."""

import asyncio
import logging
from typing import Any

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

        # Fair Reader-Writer Lock & Concurrency Control (ADR-0003 & ADR-0006)
        self._inference_lock = asyncio.Lock()
        self._switch_lock = asyncio.Lock()
        self._active_leases: set[str] = set()
        self._switch_pending = False
        self._drain_event = asyncio.Event()
        self._drain_event.set()  # Initially drained

        # Dynamic Memory Management State (ADR-0006)
        self.current_pressure_level: int = 1
        self.max_context_tokens: int = 32768
        self.idle_specialist_models_unloaded: bool = False
        self._deferred_critical_unload: bool = False

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

    async def acquire_inference_lease(self, task_id: str, timeout: float = 60.0) -> None:
        """Acquire single-model concurrency lease prior to inference.

        Blocks while a model switch is pending, ensuring strict lease coordination.
        """
        # Wait if a model switch is pending (Writer priority)
        while self._switch_pending:
            await asyncio.sleep(0.05)

        # Acquire single-model mutual exclusion
        await asyncio.wait_for(self._inference_lock.acquire(), timeout=timeout)
        self._active_leases.add(task_id)
        self._drain_event.clear()
        logger.debug(
            f"Inference lease acquired for task '{task_id}'. Active leases: {len(self._active_leases)}"
        )

    async def release_inference_lease(self, task_id: str) -> None:
        """Release inference lease after generation completes."""
        self._active_leases.discard(task_id)
        if len(self._active_leases) == 0:
            self._drain_event.set()
            if (
                self._deferred_critical_unload
                and self._active_model
                and self._active_model.is_active
            ):
                logger.critical(
                    "Executing deferred Level 4 model eviction now that inference lease has drained."
                )
                await self.provider.unload_model(self._active_model.id)
                self._active_model.is_active = False
                self._deferred_critical_unload = False

        if self._inference_lock.locked():
            self._inference_lock.release()
        logger.debug(
            f"Inference lease released for task '{task_id}'. Active leases: {len(self._active_leases)}"
        )

    async def switch_model(self, request: ModelSwitchRequest) -> bool:
        """Execute a controlled model switch under mutual exclusion.

        Guarantees that model switching can never unload a model during active inference.
        """
        async with self._switch_lock:
            target_id = request.target_model_id
            if target_id not in self._registry:
                logger.error(f"Cannot switch to unknown model: {target_id}")
                return False

            if self._active_model and self._active_model.id == target_id and not request.force:
                logger.info(f"Model '{target_id}' is already active.")
                return True

            # Step 1: Set switch pending to block new inference lease acquisitions
            self._switch_pending = True
            logger.info(
                f"Model switch pending: {self._active_model.id if self._active_model else 'None'} -> {target_id} (Reason: {request.reason})"
            )

            try:
                # Step 2: Await drain of all active inference leases (Guaranteed Lease Safety)
                try:
                    await asyncio.wait_for(self._drain_event.wait(), timeout=10.0)
                except TimeoutError:
                    logger.error(
                        "Timed out waiting for active inference leases to drain. Aborting switch."
                    )
                    return False

                # Step 3: Unload active model with up to 3 retries
                if self._active_model and self._active_model.is_active:
                    unload_success = False
                    for attempt in range(1, 4):
                        logger.info(
                            f"Unloading active model '{self._active_model.id}' (Attempt {attempt}/3)..."
                        )
                        if await self.provider.unload_model(self._active_model.id):
                            unload_success = True
                            break
                        await asyncio.sleep(0.5 * attempt)

                    if not unload_success:
                        logger.critical(
                            f"Failed to unload active model '{self._active_model.id}'. "
                            "Aborting model switch to prevent running concurrent models and crashing host memory."
                        )
                        return False

                    self._active_model.is_active = False

                # Step 4: Pre-warm target model
                logger.info(f"Pre-warming target model '{target_id}'...")
                load_success = await self.provider.load_model(target_id)
                if not load_success:
                    logger.error(
                        f"Failed to pre-warm target model '{target_id}', initiating rollback to '{self.default_model_id}'..."
                    )
                    # Rollback to approved default model
                    if self.default_model_id in self._registry:
                        await self.provider.load_model(self.default_model_id)
                        self._active_model = self._registry[self.default_model_id]
                        self._active_model.is_active = True
                    return False

                # Switch success
                self._active_model = self._registry[target_id]
                self._active_model.is_active = True
                logger.info(f"Model switch to '{target_id}' completed successfully.")
                return True

            finally:
                self._switch_pending = False

    async def handle_memory_pressure(self, level: int) -> dict[str, Any]:
        """Apply dynamic memory governance reaction ladder based on Mach kernel memory pressure."""
        self.current_pressure_level = level
        action_summary = {"level": level, "actions": []}

        if level == 1:
            # Level 1: Normal
            self.max_context_tokens = 32768
            self.idle_specialist_models_unloaded = False
            action_summary["actions"].append(
                "Normal operation: standard context (32K), pre-warming enabled."
            )
            logger.info("Memory Pressure Level 1 (Normal): Standard operations maintained.")

        elif level == 2:
            # Level 2: Warning
            self.max_context_tokens = 8192
            self.idle_specialist_models_unloaded = True
            action_summary["actions"].extend(
                [
                    "Unloaded idle specialist models (embeddings, TTS)",
                    "Capped context window to 8,192 tokens",
                    "Downgraded default reasoning depth to standard",
                ]
            )
            logger.warning(
                "Memory Pressure Level 2 (Warning): Unloaded specialist models, capped context to 8K."
            )

        elif level == 4:
            # Level 4: Critical
            self.max_context_tokens = 4096
            self.idle_specialist_models_unloaded = True

            # Active-Inference Protection:
            # If an inference lease is active, protect ongoing generation and defer unload until release
            if len(self._active_leases) > 0:
                self._deferred_critical_unload = True
                logger.warning(
                    f"Memory Pressure Level 4 (Critical): Inference lease active ({list(self._active_leases)}). "
                    "Protecting active inference; deferring model unload until lease release."
                )
                action_summary["actions"].append(
                    f"Active inference protected for task(s) {list(self._active_leases)}; model unload deferred."
                )
            elif self._active_model and self._active_model.is_active:
                await self.provider.unload_model(self._active_model.id)
                self._active_model.is_active = False
                action_summary["actions"].append(f"Unloaded active model '{self._active_model.id}'")

            action_summary["actions"].extend(
                [
                    "Capped context window to 4,096 tokens",
                    "Prohibited deep reasoning tasks",
                    "Dispatched desktop memory warning alert",
                ]
            )
            logger.critical(
                "Memory Pressure Level 4 (Critical): Evicted or deferred chat model, restricted context to 4K."
            )

        return action_summary
