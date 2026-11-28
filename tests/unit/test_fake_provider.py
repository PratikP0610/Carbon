"""Dedicated unit and offline integration tests for FakeProvider."""

import asyncio
from pathlib import Path

import pytest

from carbon.config.settings import Settings
from carbon.core.orchestrator import CarbonCore
from carbon.interfaces.models import (
    ChatMessage,
    ModelCapability,
    ModelMetadata,
    ModelSwitchRequest,
    ReasoningDepth,
    TaskRequest,
    TaskState,
)
from carbon.interfaces.protocols import IModelProvider
from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.fake_provider import FakeProvider


def test_fake_provider_implements_protocol():
    """FakeProvider must satisfy IModelProvider runtime protocol."""
    provider = FakeProvider()
    assert isinstance(provider, IModelProvider)


@pytest.mark.asyncio
async def test_fake_provider_deterministic_generation():
    """Verify deterministic text, structured events, and chat streaming."""
    provider = FakeProvider(
        response_chunks=["Hello ", "Carbon!"],
        thinking_chunks=["Step 1..."],
        done_reason="stop",
    )

    assert await provider.is_healthy() is True
    models = await provider.list_available_models()
    assert len(models) == 1
    assert models[0].id == "qwen3.5:9b"

    text_tokens = [
        tok
        async for tok in provider.generate_stream(
            model_id="qwen3.5:9b",
            prompt="Say hi",
            think=False,
            options={"num_predict": 64},
        )
    ]
    assert "".join(text_tokens) == "Hello Carbon!"

    events = [
        evt
        async for evt in provider.generate_stream_events(
            model_id="qwen3.5:9b",
            prompt="Reason deeply",
            think=True,
        )
    ]
    assert events[0]["thinking"] == "Step 1..."
    assert "".join(e["response"] for e in events) == "Hello Carbon!"
    assert events[-1]["done"] is True
    assert events[-1]["done_reason"] == "stop"

    # When think=False, thinking chunks are suppressed deterministically
    events_no_think = [
        evt
        async for evt in provider.generate_stream_events(
            model_id="qwen3.5:9b",
            prompt="Instant",
            think=False,
        )
    ]
    assert all(e["thinking"] == "" for e in events_no_think)

    chat_events = [
        evt
        async for evt in provider.chat_stream(
            model_id="qwen3.5:9b",
            messages=[ChatMessage(role="user", content="Ping")],
        )
    ]
    assert "".join(e["message"]["content"] for e in chat_events) == "Hello Carbon!"
    assert chat_events[-1]["done"] is True
    assert len(provider.calls) == 6


@pytest.mark.asyncio
async def test_fake_provider_response_factory_and_lifecycle():
    """Verify prompt-aware response factory and load/unload tracking."""
    models = [
        ModelMetadata(
            id="qwen3.5:9b",
            name="Primary",
            capabilities=[ModelCapability.CHAT, ModelCapability.THINKING],
        ),
        ModelMetadata(
            id="qwen-fast:1.5b",
            name="Fast",
            capabilities=[ModelCapability.CHAT],
        ),
    ]
    provider = FakeProvider(
        models=models,
        response_factory=lambda prompt, _sys: f"echo:{prompt}",
    )

    chunks = [c async for c in provider.generate_stream("qwen3.5:9b", "abc")]
    assert chunks == ["echo:abc"]

    assert await provider.unload_model("qwen3.5:9b") is True
    assert await provider.load_model("qwen-fast:1.5b") is True
    assert provider.unloaded_models == ["qwen3.5:9b"]
    assert provider.loaded_models == ["qwen-fast:1.5b"]


@pytest.mark.asyncio
async def test_fake_provider_unavailable_simulation():
    """Verify unavailable provider reports offline and raises ConnectionError on stream calls."""
    provider = FakeProvider(healthy=False)

    assert await provider.is_healthy() is False
    assert await provider.list_available_models() == []
    assert await provider.load_model("qwen3.5:9b") is False
    assert await provider.unload_model("qwen3.5:9b") is False

    with pytest.raises(ConnectionError, match="unavailable"):
        async for _ in provider.generate_stream("qwen3.5:9b", "hi"):
            pass

    with pytest.raises(ConnectionError, match="unavailable"):
        async for _ in provider.generate_stream_events("qwen3.5:9b", "hi"):
            pass

    with pytest.raises(ConnectionError, match="unavailable"):
        async for _ in provider.chat_stream("qwen3.5:9b", [ChatMessage(role="user", content="hi")]):
            pass


@pytest.mark.asyncio
async def test_fake_provider_error_and_timeout_simulation():
    """Verify simulated exceptions and request timeouts behave deterministically."""
    error_provider = FakeProvider(simulate_error=RuntimeError("Upstream crashed"))
    with pytest.raises(RuntimeError, match="Upstream crashed"):
        async for _ in error_provider.generate_stream_events("qwen3.5:9b", "hi"):
            pass

    timeout_provider = FakeProvider(simulate_timeout=True)
    with pytest.raises(TimeoutError):
        async for _ in timeout_provider.generate_stream("qwen3.5:9b", "hi"):
            pass

    delayed_timeout_provider = FakeProvider(simulate_timeout=True, timeout_delay_seconds=0.2)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            async for _ in delayed_timeout_provider.chat_stream(
                "qwen3.5:9b", [ChatMessage(role="user", content="hi")]
            ):
                pass


@pytest.mark.asyncio
async def test_fake_provider_malformed_response_simulation():
    """Verify both corrupt chunk payloads and decode-error malformed mode."""
    corrupt_provider = FakeProvider(
        simulate_malformed=True,
        malformed_chunks=[{"unexpected": 42}],
    )
    chunks = [evt async for evt in corrupt_provider.generate_stream_events("qwen3.5:9b", "hi")]
    assert chunks == [{"unexpected": 42}]

    raising_provider = FakeProvider(simulate_malformed=True, malformed_raises=True)
    with pytest.raises(ValueError, match="malformed"):
        async for _ in raising_provider.generate_stream_events("qwen3.5:9b", "hi"):
            pass


@pytest.mark.asyncio
async def test_fake_provider_integrates_with_core_and_cms_offline(tmp_path: Path):
    """Verify CarbonCore + CMS execute end-to-end tasks offline using FakeProvider."""
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    fake_provider = FakeProvider(
        models=[
            ModelMetadata(
                id="qwen3.5:9b",
                name="Primary",
                capabilities=[ModelCapability.CHAT, ModelCapability.THINKING],
            ),
            ModelMetadata(
                id="qwen-fast:1.5b",
                name="Fast",
                capabilities=[ModelCapability.CHAT],
            ),
        ],
        default_response="Offline execution succeeded.",
    )
    core.provider = fake_provider
    core.cms = CarbonModelSupervisor(
        provider=fake_provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-fast:1.5b"),
    )
    await core.initialize()

    try:
        health = await core.get_system_health()
        assert health["ollama_connected"] is True
        assert await core.cms.switch_model(
            ModelSwitchRequest(target_model_id="qwen-fast:1.5b", reason="Test switch")
        )

        task = await core.create_task(
            TaskRequest(prompt="What is 2 + 2?", reasoning_override=ReasoningDepth.INSTANT)
        )
        events = [evt async for evt in core.execute_task_stream(task)]

        assert task.state == TaskState.COMPLETED
        assert task.result == "Offline execution succeeded."
        assert any(e.get("event") == "completed" for e in events)
        assert not core.cms._inference_lock.locked()
    finally:
        await core.shutdown()
