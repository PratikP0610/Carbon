"""Unit tests for Stage 1 inference optimisation implementation."""

from typing import Any
from unittest.mock import patch

import pytest
from httpx import Response

from carbon.config.settings import Settings
from carbon.core.orchestrator import CarbonCore
from carbon.interfaces.models import (
    ReasoningDepth,
    TaskRequest,
    TaskState,
)
from carbon.model_control.ollama_provider import OllamaProvider


class MockStreamResponse:
    """Mock for httpx streaming response."""

    def __init__(self, lines: list[str], status_code: int = 200):
        self.lines = lines
        self.status_code = status_code

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    async def aread(self):
        return b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.mark.asyncio
async def test_root_level_think_false_and_options_serialization():
    """Verify think: false is serialized at the ROOT level of the payload, not inside options."""
    provider = OllamaProvider()
    captured_payloads: list[dict[str, Any]] = []

    class MockAsyncClient:
        def stream(self, method: str, url: str, json: dict[str, Any]):
            captured_payloads.append(json)
            return MockStreamResponse(['{"response": "Paris", "done": true}'])

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch.object(provider, "_client", return_value=MockAsyncClient()):
        # 1. Test think=False and options provided
        async for _ in provider.generate_stream_events(
            model_id="qwen3.5:9b",
            prompt="What is 2+2?",
            system="You are Carbon.",
            think=False,
            options={"num_predict": 120, "temperature": 0.2},
        ):
            pass

        assert len(captured_payloads) == 1
        p1 = captured_payloads[0]
        # Must be root-level
        assert "think" in p1
        assert p1["think"] is False
        assert "options" in p1
        assert p1["options"] == {"num_predict": 120, "temperature": 0.2}
        assert "think" not in p1["options"]  # Strictly not inside options

        # 2. Test backward compatibility: think and options omitted
        captured_payloads.clear()
        async for _ in provider.generate_stream_events(
            model_id="qwen3.5:9b",
            prompt="Hello",
        ):
            pass

        assert len(captured_payloads) == 1
        p2 = captured_payloads[0]
        assert "think" not in p2
        assert "options" not in p2
        assert p2["model"] == "qwen3.5:9b"
        assert p2["prompt"] == "Hello"


@pytest.mark.asyncio
async def test_instant_routing_policy(tmp_path):
    """Verify INSTANT tasks set think=False, num_predict=512, temperature=0.2."""
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    await core.initialize()

    captured_calls = []

    async def mock_generate_stream_events(model_id, prompt, system, think=None, options=None):
        _ = (prompt, system)
        captured_calls.append({"model_id": model_id, "think": think, "options": options})
        yield {"response": "4", "done": True}

    with patch.object(core.provider, "generate_stream_events", side_effect=mock_generate_stream_events):
        req = TaskRequest(prompt="What is 2+2?", reasoning_override=ReasoningDepth.INSTANT)
        task = await core.create_task(req)

        events = []
        async for evt in core.execute_task_stream(task):
            events.append(evt)

        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["think"] is False
        assert call["options"] == {"num_predict": 512, "temperature": 0.2}
        assert task.state == TaskState.COMPLETED
        assert task.result == "4"

    await core.shutdown()


@pytest.mark.asyncio
async def test_standard_and_deep_backward_compatibility(tmp_path):
    """Verify STANDARD and DEEP tasks configure appropriate reasoning budgets and temperatures."""
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    await core.initialize()

    captured_calls = []

    async def mock_generate_stream_events(model_id, prompt, system, think=None, options=None):
        _ = (prompt, system)
        captured_calls.append({"model_id": model_id, "think": think, "options": options})
        yield {"thinking": "Analyzing...", "response": ""}
        yield {"thinking": "", "response": "Completed answer."}
        yield {"done": True}

    with patch.object(core.provider, "generate_stream_events", side_effect=mock_generate_stream_events):
        # 1. Standard (Stage 1.9: think=False with full 2560 token capacity)
        req_std = TaskRequest(prompt="Explain concurrency", reasoning_override=ReasoningDepth.STANDARD)
        task_std = await core.create_task(req_std)
        async for _ in core.execute_task_stream(task_std):
            pass

        assert captured_calls[0]["think"] is False
        assert captured_calls[0]["options"] == {"num_predict": 2560, "temperature": 0.6}

        # 2. Deep
        captured_calls.clear()
        req_deep = TaskRequest(prompt="Multi-step architectural plan", reasoning_override=ReasoningDepth.DEEP)
        task_deep = await core.create_task(req_deep)
        async for _ in core.execute_task_stream(task_deep):
            pass

        assert captured_calls[0]["think"] is True
        assert captured_calls[0]["options"] == {"num_predict": 4096, "temperature": 0.7}

    await core.shutdown()


@pytest.mark.asyncio
async def test_user_reasoning_override_preservation(tmp_path):
    """Verify explicit user reasoning_override is preserved and not clobbered by intent classifier."""
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    await core.initialize()

    captured_calls = []

    async def mock_generate_stream_events(model_id, prompt, system, think=None, options=None):
        _ = (prompt, system)
        captured_calls.append({"model_id": model_id, "think": think, "options": options})
        yield {"response": "Tokyo", "done": True}

    with patch.object(core.provider, "generate_stream_events", side_effect=mock_generate_stream_events):
        # User explicitly requested STANDARD on a prompt that would otherwise classify as INSTANT
        req = TaskRequest(
            prompt="What is the capital of Japan?",
            reasoning_override=ReasoningDepth.STANDARD,
        )
        task = await core.create_task(req)
        assert task.reasoning_override == ReasoningDepth.STANDARD

        async for _ in core.execute_task_stream(task):
            pass

        # Must retain STANDARD (Stage 1.9: think=False, num_predict=2560)
        assert task.reasoning_depth == ReasoningDepth.STANDARD
        assert captured_calls[0]["think"] is False
        assert captured_calls[0]["options"]["num_predict"] == 2560

        # User explicitly requested INSTANT on a prompt that would otherwise classify as DEEP
        captured_calls.clear()
        req2 = TaskRequest(
            prompt="Solve this complex multi-step puzzle.",
            reasoning_override=ReasoningDepth.INSTANT,
        )
        task2 = await core.create_task(req2)
        assert task2.reasoning_override == ReasoningDepth.INSTANT

        async for _ in core.execute_task_stream(task2):
            pass

        # Must retain INSTANT
        assert task2.reasoning_depth == ReasoningDepth.INSTANT
        assert captured_calls[0]["think"] is False
        assert captured_calls[0]["options"]["num_predict"] == 512

    await core.shutdown()


@pytest.mark.asyncio
async def test_token_budget_exhaustion_empty_answer_fallback(tmp_path):
    """Verify that when token budget is exhausted without emitting visible answer tokens,

    a fallback message is cleanly returned and streamed to the UI.
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    await core.initialize()

    async def mock_empty_generate(*_args, **_kwargs):
        # Yield only thinking, no response tokens (budget exhausted)
        yield {"thinking": "Thinking deeply...", "response": ""}
        yield {"done": True}

    with patch.object(core.provider, "generate_stream_events", side_effect=mock_empty_generate):
        req = TaskRequest(prompt="Complex problem", reasoning_override=ReasoningDepth.INSTANT)
        task = await core.create_task(req)

        tokens_received = []
        async for evt in core.execute_task_stream(task):
            if evt.get("event") == "token":
                tokens_received.append(evt.get("text"))

        assert len(tokens_received) > 0
        assert "No response was generated within the token limit" in "".join(tokens_received)
        assert task.state == TaskState.COMPLETED

    await core.shutdown()


@pytest.mark.asyncio
async def test_context_reporting_distinction():
    """Verify distinction between active runtime context and maximum architectural context."""
    provider = OllamaProvider()

    # Mock /api/tags returning architectural context (262144)
    # and /api/ps returning active runtime context (4096)
    tags_data = {
        "models": [
            {
                "name": "qwen3.5:9b",
                "details": {
                    "family": "qwen35",
                    "parameter_size": "9.0B",
                    "quantization_level": "Q4_K_M",
                    "context_length": 262144,
                },
                "capabilities": ["completion", "chat", "thinking"],
                "size": 6550825550,
            }
        ]
    }
    ps_data = {
        "models": [
            {
                "name": "qwen3.5:9b",
                "context_length": 4096,
                "size_vram": 6107546253,
            }
        ]
    }

    async def mock_get(url: str):
        if url == "/api/tags":
            return Response(200, json=tags_data)
        elif url == "/api/ps":
            return Response(200, json=ps_data)
        return Response(404)

    with patch.object(provider, "_client") as mock_client:
        mock_client.return_value.__aenter__.return_value.get.side_effect = mock_get

        models = await provider.list_available_models()
        assert len(models) == 1
        m = models[0]
        # Active runtime context must be 4096
        assert m.context_length == 4096
        # Architectural limit must be 262144
        assert m.max_architectural_context == 262144


@pytest.mark.asyncio
async def test_cms_lease_safety_with_optimisation(tmp_path):
    """Verify CMS fair inference lock is held and cleanly released during optimized generation."""
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    await core.initialize()

    lock_was_held = False

    async def mock_generate(*_args, **_kwargs):
        nonlocal lock_was_held
        lock_was_held = core.cms._inference_lock.locked()
        yield {"response": "OK", "done": True}

    with patch.object(core.provider, "generate_stream_events", side_effect=mock_generate):
        req = TaskRequest(prompt="test", reasoning_override=ReasoningDepth.INSTANT)
        task = await core.create_task(req)
        async for _ in core.execute_task_stream(task):
            pass

        assert lock_was_held is True
        # Lock must be released upon completion
        assert core.cms._inference_lock.locked() is False

    await core.shutdown()
