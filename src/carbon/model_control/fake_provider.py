"""Deterministic offline FakeProvider implementing IModelProvider for testing."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

from carbon.config.settings import DEFAULT_PRIMARY_MODEL
from carbon.core.metrics import (
    mark_provider_request_execution_start,
    record_provider_eval_metrics,
)
from carbon.interfaces.models import (
    ChatMessage,
    HealthProbeResult,
    HealthProbeStatus,
    InferenceFailureKind,
    ModelCapability,
    ModelMetadata,
    ProviderInferenceError,
    RunningModelRuntimeInfo,
    RuntimeDiscoveryError,
    RuntimeDiscoverySnapshot,
)
from carbon.interfaces.protocols import IModelProvider


def _default_fake_models() -> list[ModelMetadata]:
    """Return a deterministic offline model catalog matching default Carbon capabilities."""
    return [
        ModelMetadata(
            id=DEFAULT_PRIMARY_MODEL,
            name="Qwen 3.5 9B (Fake)",
            provider="fake",
            family="qwen35",
            parameter_size="9.0B",
            quantization="Q4_K_M",
            context_length=4096,
            max_architectural_context=262144,
            memory_footprint_mb=6200,
            capabilities=[
                ModelCapability.COMPLETION,
                ModelCapability.CHAT,
                ModelCapability.TOOLS,
                ModelCapability.THINKING,
                ModelCapability.VISION,
            ],
            is_active=True,
            is_healthy=True,
        )
    ]


@dataclass
class FakeProvider(IModelProvider):
    """Deterministic offline model provider for isolated unit and integration testing.

    Supports deterministic token/event/chat responses as well as controlled simulation
    of provider unavailability, runtime errors, request timeouts, and malformed payloads
    without requiring Ollama or external network connectivity.
    """

    models: list[ModelMetadata] = field(default_factory=_default_fake_models)
    default_response: str = "Deterministic response from FakeProvider."
    response_chunks: list[str] | None = None
    thinking_chunks: list[str] = field(default_factory=list)
    response_factory: Callable[[str, str | None], str] | None = None
    chat_tool_calls: list[dict[str, Any]] | None = None
    done_reason: str | None = "stop"
    healthy: bool = True
    simulate_error: Exception | None = None
    simulate_timeout: bool = False
    timeout_delay_seconds: float = 0.0
    simulate_malformed: bool = False
    malformed_raises: bool = False
    malformed_chunks: list[dict[str, Any]] | None = None
    fail_load: bool = False
    fail_load_models: set[str] = field(default_factory=set)
    fail_unload: bool = False
    simulate_load_timeout: bool = False
    hide_loaded_in_ps: bool = False
    chunk_delay_seconds: float = 0.0
    running_models: set[str] = field(default_factory=set)
    unhealthy_models: set[str] = field(default_factory=set)
    health_probe_status: HealthProbeStatus | None = None
    health_http_status: int | None = None
    discovery_error: Exception | None = None
    ps_error: Exception | None = None
    running_contexts: dict[str, int] = field(default_factory=dict)
    omit_running_context_models: set[str] = field(default_factory=set)
    responses_by_model: dict[str, str] = field(default_factory=dict)
    enforce_installed_on_load: bool = False
    stream_error_after_chunks: int | None = None
    simulate_stream_interruption: bool = False
    interrupt_after_chunks: int | None = None
    simulated_eval_count: int | None = None
    simulated_eval_duration_ns: int | None = None

    calls: list[dict[str, Any]] = field(default_factory=list, init=False)
    loaded_models: list[str] = field(default_factory=list, init=False)
    unloaded_models: list[str] = field(default_factory=list, init=False)

    def reset_history(self) -> None:
        """Clear recorded invocation and lifecycle call logs."""
        self.calls.clear()
        self.loaded_models.clear()
        self.unloaded_models.clear()

    def _resolve_chunks(
        self, prompt: str, system: str | None, *, model_id: str | None = None
    ) -> list[str]:
        if self.response_factory is not None:
            generated = self.response_factory(prompt, system)
            return [generated] if generated else []
        if model_id is not None and model_id in self.responses_by_model:
            override = self.responses_by_model[model_id]
            return [override] if override else []
        if self.response_chunks is not None:
            return list(self.response_chunks)
        if not self.default_response:
            return []
        words = self.default_response.split(" ")
        if len(words) <= 1:
            return [self.default_response]
        return [word if idx == len(words) - 1 else f"{word} " for idx, word in enumerate(words)]

    async def _check_preconditions(self) -> None:
        if not self.healthy:
            raise ConnectionError("FakeProvider is unavailable.")
        if self.simulate_timeout:
            if self.timeout_delay_seconds > 0.0:
                await asyncio.sleep(self.timeout_delay_seconds)
            raise TimeoutError("FakeProvider simulated request timeout.")
        if self.simulate_error is not None:
            raise self.simulate_error

    def _resolve_health_probe(self) -> HealthProbeResult:
        if self.health_probe_status is not None:
            return HealthProbeResult(
                status=self.health_probe_status,
                http_status=self.health_http_status,
                detail=f"FakeProvider simulated health probe status {self.health_probe_status.value}.",
            )
        if not self.healthy:
            return HealthProbeResult(
                status=HealthProbeStatus.UNREACHABLE,
                detail="FakeProvider is unavailable.",
            )
        return HealthProbeResult(status=HealthProbeStatus.HEALTHY, http_status=200)

    def _build_running_infos(self) -> tuple[RunningModelRuntimeInfo, ...]:
        context_fallback: dict[str, int | None] = {m.id: m.context_length for m in self.models}
        infos: list[RunningModelRuntimeInfo] = []
        for model_id in sorted(self.running_models):
            clean_id = model_id.strip()
            if not clean_id:
                continue
            if clean_id in self.omit_running_context_models:
                ctx: int | None = None
            elif clean_id in self.running_contexts:
                raw_ctx = self.running_contexts[clean_id]
                ctx = (
                    raw_ctx
                    if (isinstance(raw_ctx, int) and not isinstance(raw_ctx, bool) and raw_ctx > 0)
                    else None
                )
            else:
                fallback = context_fallback.get(clean_id)
                ctx = (
                    fallback
                    if (
                        isinstance(fallback, int)
                        and not isinstance(fallback, bool)
                        and fallback > 0
                    )
                    else None
                )
            infos.append(RunningModelRuntimeInfo(id=clean_id, context_length=ctx))
        return tuple(infos)

    async def probe_health(self) -> HealthProbeResult:
        """Return structured simulated health probe result."""
        self.calls.append({"method": "probe_health"})
        return self._resolve_health_probe()

    async def list_running_runtime_info(self) -> tuple[RunningModelRuntimeInfo, ...]:
        """Strictly return simulated /api/ps runtime residency or raise on outage/fault."""
        self.calls.append({"method": "list_running_runtime_info"})
        if self.ps_error is not None:
            raise self.ps_error
        if not self.healthy:
            if self.running_models:
                return self._build_running_infos()
            raise RuntimeDiscoveryError(
                "FakeProvider /api/ps is unavailable.",
                probe_status=self._resolve_health_probe().status,
                endpoint="/api/ps",
            )
        if self.simulate_malformed:
            raise RuntimeDiscoveryError(
                "FakeProvider simulated malformed /api/ps payload.",
                probe_status=HealthProbeStatus.INVALID_RESPONSE,
                endpoint="/api/ps",
            )
        return self._build_running_infos()

    async def discover_runtime_state(self) -> RuntimeDiscoverySnapshot:
        """Strictly return atomic snapshot of installed and running fake models."""
        self.calls.append({"method": "discover_runtime_state"})
        if self.discovery_error is not None:
            raise self.discovery_error
        if self.ps_error is not None:
            raise self.ps_error
        if not self.healthy:
            raise RuntimeDiscoveryError(
                "FakeProvider runtime discovery unavailable.",
                probe_status=self._resolve_health_probe().status,
                endpoint="/api/tags",
            )
        if self.simulate_malformed:
            raise RuntimeDiscoveryError(
                "FakeProvider simulated malformed discovery payload.",
                probe_status=HealthProbeStatus.INVALID_RESPONSE,
                endpoint="/api/tags",
            )
        running_infos = self._build_running_infos()
        ctx_by_id = {info.id: info.context_length for info in running_infos}
        running_ids = set(ctx_by_id)
        installed: list[ModelMetadata] = []
        for model in self.models:
            clean_id = model.id.strip()
            runtime_ctx = ctx_by_id.get(clean_id) if clean_id in running_ids else None
            effective_healthy = model.is_healthy and clean_id not in self.unhealthy_models
            installed.append(
                model.model_copy(
                    deep=True,
                    update={
                        "context_length": runtime_ctx,
                        "is_healthy": effective_healthy,
                    },
                )
            )
        return RuntimeDiscoverySnapshot(
            installed_models=tuple(installed),
            running_models=running_infos,
        )

    async def list_available_models(self) -> list[ModelMetadata]:
        """Return configured fake models when healthy, or empty list when unavailable."""
        self.calls.append({"method": "list_available_models"})
        if not self.healthy or self.simulate_malformed or self.discovery_error is not None:
            return []
        return [
            model.model_copy(
                deep=True,
                update={
                    "is_healthy": model.is_healthy and model.id.strip() not in self.unhealthy_models
                },
            )
            for model in self.models
        ]

    async def list_running_models(self) -> list[str]:
        """Return simulated running models (/api/ps) when healthy."""
        self.calls.append({"method": "list_running_models"})
        if not self.healthy or self.simulate_malformed or self.ps_error is not None:
            return []
        return sorted(self.running_models)

    async def is_healthy(self) -> bool:
        """Return configured health availability status."""
        self.calls.append({"method": "is_healthy"})
        return self._resolve_health_probe().is_healthy

    async def generate_stream(
        self,
        model_id: str,
        prompt: str,
        system: str | None = None,
        think: bool | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream deterministic text chunks for a prompt."""
        self.calls.append(
            {
                "method": "generate_stream",
                "model_id": model_id,
                "prompt": prompt,
                "system": system,
                "think": think,
                "options": options,
            }
        )
        await self._check_preconditions() if self.stream_error_after_chunks is None else None

        if self.simulate_malformed:
            if self.malformed_raises:
                raise ValueError("FakeProvider simulated malformed response payload.")
            return

        chunks = self._resolve_chunks(prompt, system, model_id=model_id)
        if self.simulate_stream_interruption:
            limit = (
                len(chunks)
                if self.interrupt_after_chunks is None
                else max(0, self.interrupt_after_chunks)
            )
            if limit == 0:
                raise ProviderInferenceError(
                    InferenceFailureKind.STREAM_INTERRUPTED,
                    "FakeProvider simulated interrupted stream before first token.",
                    model_id=model_id,
                )
            for chunk in chunks[:limit]:
                if self.chunk_delay_seconds > 0.0:
                    await asyncio.sleep(self.chunk_delay_seconds)
                yield chunk
            return

        for idx, chunk in enumerate(chunks):
            if self.stream_error_after_chunks is not None and idx >= self.stream_error_after_chunks:
                await self._check_preconditions()
            if self.chunk_delay_seconds > 0.0:
                await asyncio.sleep(self.chunk_delay_seconds)
            yield chunk
        if self.stream_error_after_chunks is not None:
            await self._check_preconditions()

    async def generate_stream_events(
        self,
        model_id: str,
        prompt: str,
        system: str | None = None,
        think: bool | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream structured event dicts matching OllamaProvider.generate_stream_events."""
        self.calls.append(
            {
                "method": "generate_stream_events",
                "model_id": model_id,
                "prompt": prompt,
                "system": system,
                "think": think,
                "options": options,
            }
        )
        mark_provider_request_execution_start()
        if self.stream_error_after_chunks is None:
            await self._check_preconditions()

        if self.simulate_malformed:
            if self.malformed_raises:
                raise ValueError("FakeProvider simulated malformed JSON event stream.")
            malformed = self.malformed_chunks or [
                {"unexpected_field": True, "response": None, "done": False}
            ]
            for item in malformed:
                yield dict(item)
            return

        emitted_count = 0
        if think is not False:
            for thought in self.thinking_chunks:
                if (
                    self.stream_error_after_chunks is not None
                    and emitted_count >= self.stream_error_after_chunks
                ):
                    await self._check_preconditions()
                if self.chunk_delay_seconds > 0.0:
                    await asyncio.sleep(self.chunk_delay_seconds)
                emitted_count += 1
                yield {
                    "response": "",
                    "thinking": thought,
                    "done": False,
                    "done_reason": None,
                }

        chunks = self._resolve_chunks(prompt, system, model_id=model_id)
        if self.simulate_stream_interruption:
            limit = (
                len(chunks)
                if self.interrupt_after_chunks is None
                else max(0, self.interrupt_after_chunks)
            )
            if limit == 0 and emitted_count == 0:
                raise ProviderInferenceError(
                    InferenceFailureKind.STREAM_INTERRUPTED,
                    "FakeProvider simulated interrupted stream before completion.",
                    model_id=model_id,
                )
            for chunk in chunks[:limit]:
                if self.chunk_delay_seconds > 0.0:
                    await asyncio.sleep(self.chunk_delay_seconds)
                yield {
                    "response": chunk,
                    "thinking": "",
                    "done": False,
                    "done_reason": None,
                }
            return

        for chunk in chunks:
            if (
                self.stream_error_after_chunks is not None
                and emitted_count >= self.stream_error_after_chunks
            ):
                await self._check_preconditions()
            if self.chunk_delay_seconds > 0.0:
                await asyncio.sleep(self.chunk_delay_seconds)
            emitted_count += 1
            yield {
                "response": chunk,
                "thinking": "",
                "done": False,
                "done_reason": None,
            }

        if self.stream_error_after_chunks is not None:
            await self._check_preconditions()

        record_provider_eval_metrics(
            self.simulated_eval_count,
            self.simulated_eval_duration_ns,
        )
        yield {
            "response": "",
            "thinking": "",
            "done": True,
            "done_reason": self.done_reason,
        }

    async def chat_stream(
        self,
        model_id: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream structured chat completion chunks matching Ollama /api/chat shape."""
        self.calls.append(
            {
                "method": "chat_stream",
                "model_id": model_id,
                "messages": [m.model_dump() for m in messages],
                "tools": tools,
            }
        )
        await self._check_preconditions()

        if self.simulate_malformed:
            if self.malformed_raises:
                raise ValueError("FakeProvider simulated malformed chat response.")
            malformed = self.malformed_chunks or [{"corrupt_chat_frame": True}]
            for item in malformed:
                yield dict(item)
            return

        last_prompt = messages[-1].content if messages else ""
        for chunk in self._resolve_chunks(last_prompt, None, model_id=model_id):
            if self.chunk_delay_seconds > 0.0:
                await asyncio.sleep(self.chunk_delay_seconds)
            yield {
                "model": model_id,
                "message": {"role": "assistant", "content": chunk},
                "done": False,
            }

        final_message: dict[str, Any] = {"role": "assistant", "content": ""}
        if self.chat_tool_calls:
            final_message["tool_calls"] = list(self.chat_tool_calls)

        yield {
            "model": model_id,
            "message": final_message,
            "done": True,
            "done_reason": self.done_reason,
        }

    async def load_model(self, model_id: str) -> bool:
        """Record model load request and update simulated /api/ps runtime residency."""
        self.calls.append({"method": "load_model", "model_id": model_id})
        if (
            not self.healthy
            or self.fail_load
            or model_id in self.fail_load_models
            or model_id in self.unhealthy_models
        ):
            return False
        if self.enforce_installed_on_load and not any(
            m.id.strip() == model_id.strip() for m in self.models
        ):
            return False
        if self.simulate_timeout or self.simulate_load_timeout:
            if self.timeout_delay_seconds > 0.0:
                await asyncio.sleep(self.timeout_delay_seconds)
            return False
        self.loaded_models.append(model_id)
        if not self.hide_loaded_in_ps:
            self.running_models.add(model_id)
        for model in self.models:
            model.is_running = model.id in self.running_models
            model.is_active = model.id == model_id and model.id in self.running_models
        return True

    async def unload_model(self, model_id: str) -> bool:
        """Record model unload request and update simulated /api/ps runtime residency."""
        self.calls.append({"method": "unload_model", "model_id": model_id})
        if not self.healthy or self.fail_unload:
            return False
        self.unloaded_models.append(model_id)
        self.running_models.discard(model_id)
        for model in self.models:
            model.is_running = model.id in self.running_models
            if model.id == model_id:
                model.is_active = False
        return True

    async def aclose(self) -> None:
        """Close provider resources cleanly (no-op for deterministic FakeProvider)."""
        return None
