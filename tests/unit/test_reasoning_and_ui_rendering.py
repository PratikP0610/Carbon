"""Unit tests for Stage 1.4: Reasoning Performance & UI Rendering Reliability."""

import asyncio
from unittest.mock import patch

import pytest

from carbon.config.settings import Settings
from carbon.core.orchestrator import CarbonCore
from carbon.interfaces.models import ReasoningDepth, TaskRequest, TaskState


@pytest.mark.asyncio
async def test_standard_coding_completion_without_token_exhaustion(tmp_path):
    """Verify that a standard coding request receives sufficient token budget (2560 tokens)

    and completes without falling back to token limit error.
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    await core.initialize()

    captured_options = None

    async def mock_generate_stream_events(model_id, prompt, system, think=None, options=None):
        _ = (model_id, prompt, system, think)
        nonlocal captured_options
        captured_options = options
        # Simulate reasoning followed by complete Java code
        yield {"thinking": "Need to track first and second largest distinctly...\n"}
        yield {"response": "```java\npublic class Solution {\n"}
        yield {"response": "    public static Integer findSecondLargest(int[] arr) {\n"}
        yield {"response": "        if (arr == null || arr.length < 2) return null;\n"}
        yield {"response": "        Integer first = null, second = null;\n"}
        yield {"response": "        for (int n : arr) {\n"}
        yield {"response": "            if (first == null || n > first) {\n"}
        yield {"response": "                second = first;\n"}
        yield {"response": "                first = n;\n"}
        yield {"response": "            } else if (n != first && (second == null || n > second)) {\n"}
        yield {"response": "                second = n;\n"}
        yield {"response": "            }\n        }\n        return second;\n    }\n}\n```"}
        yield {"done": True}

    with patch.object(core.provider, "generate_stream_events", side_effect=mock_generate_stream_events):
        req = TaskRequest(
            prompt="Write a Java method to find the second largest distinct element in an integer array."
        )
        task = await core.create_task(req)

        tokens = []
        async for evt in core.execute_task_stream(task):
            if evt.get("event") == "token":
                tokens.append(evt.get("text"))

        full_output = "".join(tokens)
        assert task.state == TaskState.COMPLETED
        assert task.reasoning_depth == ReasoningDepth.STANDARD
        assert captured_options is not None
        assert captured_options["num_predict"] == 2560
        assert captured_options["temperature"] == 0.6
        assert "public static Integer findSecondLargest" in full_output
        assert "No response was generated within the token limit" not in full_output

    await core.shutdown()


@pytest.mark.asyncio
async def test_deep_reasoning_completion_and_budget(tmp_path):
    """Verify that a deep reasoning task receives a 4096 token budget and guided system prompt."""
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    await core.initialize()

    captured_options = None
    captured_system = None

    async def mock_generate_stream_events(model_id, prompt, system, think=None, options=None):
        _ = (model_id, prompt, think)
        nonlocal captured_options, captured_system
        captured_options = options
        captured_system = system
        yield {"thinking": "Divide the 8 balls into groups: 3, 3, 2...\n"}
        yield {"response": "### Solution to the Eight-Ball Scale Problem\n\n"}
        yield {"response": "1. **Weigh 3 balls against 3 balls** (leave 2 aside).\n"}
        yield {"response": "2. If balanced, weigh the remaining 2 balls to find the heavier one.\n"}
        yield {"response": "3. If unbalanced, weigh 1 against 1 from the heavier group.\n"}
        yield {"done": True}

    with patch.object(core.provider, "generate_stream_events", side_effect=mock_generate_stream_events):
        req = TaskRequest(
            prompt="Solve this eight-ball balance-scale problem: You have 8 identical-looking balls, "
            "one of which is slightly heavier than the others. Using a balance scale only twice, "
            "how do you find the heavier ball?"
        )
        task = await core.create_task(req)

        tokens = []
        async for evt in core.execute_task_stream(task):
            if evt.get("event") == "token":
                tokens.append(evt.get("text"))

        full_output = "".join(tokens)
        assert task.state == TaskState.COMPLETED
        assert task.reasoning_depth == ReasoningDepth.DEEP
        assert captured_options["num_predict"] == 4096
        assert captured_options["temperature"] == 0.7
        assert "Conduct thorough, focused multi-step reasoning" in captured_system
        assert "### Solution" in full_output

    await core.shutdown()


@pytest.mark.asyncio
async def test_budget_exhaustion_fallback(tmp_path):
    """Verify that when Ollama exhausts its token budget during thinking without emitting visible tokens,

    Carbon cleanly falls back to the user-facing retry prompt instead of an empty or hung response.
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    await core.initialize()

    async def mock_exhausted_generate(model_id, prompt, system, think=None, options=None):
        _ = (model_id, prompt, system, think, options)
        # Yield only thinking chunks until done_reason = length
        yield {"thinking": "Still thinking... running out of tokens..."}
        yield {"done": True, "done_reason": "length"}

    with patch.object(core.provider, "generate_stream_events", side_effect=mock_exhausted_generate):
        req = TaskRequest(prompt="Write a long solution", reasoning_override=ReasoningDepth.STANDARD)
        task = await core.create_task(req)

        tokens = []
        async for evt in core.execute_task_stream(task):
            if evt.get("event") == "token":
                tokens.append(evt.get("text"))

        full_output = "".join(tokens)
        assert task.state == TaskState.COMPLETED
        assert "No response was generated within the token limit" in full_output
        assert "adjust reasoning depth" in full_output

    await core.shutdown()


@pytest.mark.asyncio
async def test_mid_stream_cancellation(tmp_path):
    """Verify that setting task.cancel_event mid-stream terminates inference gracefully."""
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    await core.initialize()

    async def mock_infinite_stream(model_id, prompt, system, think=None, options=None):
        _ = (model_id, prompt, system, think, options)
        for i in range(100):
            yield {"response": f"token_{i} "}
            await asyncio.sleep(0.01)
        yield {"done": True}

    with patch.object(core.provider, "generate_stream_events", side_effect=mock_infinite_stream):
        req = TaskRequest(prompt="Generate 100 tokens")
        task = await core.create_task(req)

        events_received = []
        token_count = 0
        async for evt in core.execute_task_stream(task):
            events_received.append(evt)
            if evt.get("event") == "token":
                token_count += 1
                if token_count >= 3:
                    # Trigger mid-stream cancellation
                    task.cancel_event.set()

        assert task.state == TaskState.CANCELLED
        cancelled_events = [e for e in events_received if e.get("event") == "cancelled"]
        assert len(cancelled_events) == 1
        # Inference lock must be released
        assert not core.cms._inference_lock.locked()

    await core.shutdown()

