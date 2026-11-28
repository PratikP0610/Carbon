"""Unit tests for Stage 1.10 Controlled Generation Optimization:
1. Task-aware Standard prompt efficiency (coding vs explanation)
2. Truncation detection and graceful fallback handling
3. Code block fence preservation upon generation limit
"""

from unittest.mock import AsyncMock, patch

import pytest

from carbon.config import Settings
from carbon.core.orchestrator import CarbonCore
from carbon.interfaces.models import (
    HealthProbeResult,
    HealthProbeStatus,
    ModelCapability,
    ModelMetadata,
    ReasoningDepth,
    RunningModelRuntimeInfo,
    RuntimeDiscoverySnapshot,
    TaskRequest,
    TaskState,
)
from carbon.model_control.ollama_provider import OllamaProvider


@pytest.fixture(autouse=True)
def _mock_installed_qwen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly mock Ollama health & qwen3.5:9b discovery for offline stream unit tests."""
    qwen = ModelMetadata(
        id="qwen3.5:9b",
        name="Qwen 3.5 9B",
        capabilities=[
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
            ModelCapability.THINKING,
            ModelCapability.VISION,
        ],
    )
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
                installed_models=(qwen,),
                running_models=(RunningModelRuntimeInfo(id="qwen3.5:9b", context_length=4096),),
            )
        ),
    )
    monkeypatch.setattr(
        OllamaProvider,
        "list_running_runtime_info",
        AsyncMock(return_value=(RunningModelRuntimeInfo(id="qwen3.5:9b", context_length=4096),)),
    )
    monkeypatch.setattr(OllamaProvider, "is_healthy", AsyncMock(return_value=True))
    monkeypatch.setattr(
        OllamaProvider, "list_running_models", AsyncMock(return_value=["qwen3.5:9b"])
    )
    monkeypatch.setattr(OllamaProvider, "load_model", AsyncMock(return_value=True))
    monkeypatch.setattr(
        OllamaProvider,
        "list_available_models",
        AsyncMock(return_value=[qwen]),
    )


@pytest.mark.asyncio
async def test_standard_coding_task_prompt_efficiency(tmp_path):
    """Verify that a coding task in STANDARD mode receives concise, task-aware coding directives."""
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    await core.initialize()

    captured_system = None
    captured_options = None

    async def mock_generate_stream_events(model_id, prompt, system, think=None, options=None):
        _ = (model_id, prompt, think)
        nonlocal captured_system, captured_options
        captured_system = system
        captured_options = options
        yield {"response": "```java\nint x = 1;\n```", "done": True, "done_reason": "stop"}

    with patch.object(
        core.provider, "generate_stream_events", side_effect=mock_generate_stream_events
    ):
        req = TaskRequest(
            prompt="Write a Java method to find the second-largest distinct element in an integer array.",
            reasoning_override=ReasoningDepth.STANDARD,
        )
        task = await core.create_task(req)

        tokens = []
        async for evt in core.execute_task_stream(task):
            if evt.get("event") == "token":
                tokens.append(evt.get("text"))

        assert task.state == TaskState.COMPLETED
        assert task.truncated is False
        assert captured_system is not None
        assert "optimal approach and provide a complete working implementation" in captured_system
        assert "Include essential edge-case handling" in captured_system
        assert "Clearly state the time and space complexity" in captured_system
        assert "Avoid unnecessary conversational introductions" in captured_system
        assert captured_options["num_predict"] == 2560
        assert captured_options["temperature"] == 0.6

    await core.shutdown()


@pytest.mark.asyncio
async def test_standard_explanation_task_prompt_efficiency(tmp_path):
    """Verify that an explanation task in STANDARD mode receives direct, structured explanation directives."""
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    await core.initialize()

    captured_system = None

    async def mock_generate_stream_events(model_id, prompt, system, think=None, options=None):
        _ = (model_id, prompt, think, options)
        nonlocal captured_system
        captured_system = system
        yield {
            "response": "Binary search divides the search space in half.",
            "done": True,
            "done_reason": "stop",
        }

    with patch.object(
        core.provider, "generate_stream_events", side_effect=mock_generate_stream_events
    ):
        req = TaskRequest(
            prompt="Explain why binary search requires sorted input.",
            reasoning_override=ReasoningDepth.STANDARD,
        )
        task = await core.create_task(req)

        async for _ in core.execute_task_stream(task):
            pass

        assert task.state == TaskState.COMPLETED
        assert task.truncated is False
        assert captured_system is not None
        assert (
            "Answer the question directly and explain essential concepts clearly" in captured_system
        )
        assert "Use examples when helpful" in captured_system
        assert "Avoid conversational filler, repetitive conclusions" in captured_system

    await core.shutdown()


@pytest.mark.asyncio
async def test_truncation_detection_and_code_fence_closure(tmp_path):
    """Verify that hitting the token limit (done_reason='length') flags truncation, closes unclosed fences, and adds warning."""
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    await core.initialize()

    async def mock_truncated_stream(model_id, prompt, system, think=None, options=None):
        _ = (model_id, prompt, system, think, options)
        # Yield incomplete output with an open code block
        yield {"response": "Here is the code:\n```python\ndef solve():\n    return 42"}
        yield {"done": True, "done_reason": "length"}

    with patch.object(core.provider, "generate_stream_events", side_effect=mock_truncated_stream):
        req = TaskRequest(
            prompt="Write a function to solve this.",
            reasoning_override=ReasoningDepth.STANDARD,
        )
        task = await core.create_task(req)

        events = []
        async for evt in core.execute_task_stream(task):
            events.append(evt)

        assert task.state == TaskState.COMPLETED
        assert task.truncated is True
        # Verify code block was safely closed so markdown renderer never hangs
        assert task.result.count("```") % 2 == 0
        assert "> [!WARNING]" in task.result
        assert "Generation limit reached" in task.result
        assert "Deep" in task.result

        # Verify task dictionary contains truncated status
        d = task.to_dict()
        assert d["truncated"] is True

    await core.shutdown()
