"""Regression tests for Stage 1.1 IntelligenceEngine routing reliability.

Validates:
1. Arithmetic whitespace variations (INSTANT).
2. Complex mathematical & algorithmic tasks with concise-answer instructions (DEEP / STANDARD).
3. Simple factual questions with concise-answer instructions (INSTANT).
4. Complex coding, algorithmic, and explanatory queries (STANDARD).
5. Existing conversational greetings, system commands, and deep debug tasks.
6. Integration with CarbonCore orchestrator mode selection.
"""

from unittest.mock import patch

import pytest

from carbon.config.settings import Settings
from carbon.core.orchestrator import CarbonCore
from carbon.intelligence.engine import IntelligenceEngine
from carbon.interfaces.models import ReasoningDepth, TaskRequest


@pytest.mark.asyncio
async def test_arithmetic_whitespace_variations():
    """Verify simple arithmetic with any spacing routes to INSTANT without reasoning."""
    engine = IntelligenceEngine()

    arithmetic_prompts = [
        "What is 37 + 58?",
        "What is 37+58?",
        "What is 37 + 58",
        "what is 37   +   58 ?",
        "Calculate 125 - 45",
        "Calculate 125-45",
        "Compute 12 * 8",
        "Compute 12*8",
        "What is 144 / 12?",
        "What is 144/12?",
        "37 + 58",
        "37+58",
        "125 - 45",
        "12 * 8",
        "144 / 12",
        "What is 37 + 58? Just the number.",
        "What is 37 + 58? Answer in one word.",
        "Calculate 10 + 20 - 5",
        "compute 25 * 4 / 2",
    ]

    for prompt in arithmetic_prompts:
        intent = await engine.classify_intent(prompt)
        assert intent.reasoning_depth == ReasoningDepth.INSTANT, (
            f"Failed on '{prompt}': got {intent.reasoning_depth.value}, expected instant"
        )
        assert intent.category == "CONVERSATIONAL", (
            f"Failed category on '{prompt}': got {intent.category}"
        )


@pytest.mark.asyncio
async def test_complex_math_with_concise_instructions():
    """Verify complex mathematical & algorithmic tasks are NEVER downgraded to INSTANT by concise instructions."""
    engine = IntelligenceEngine()

    deep_prompts = [
        "Solve this multi-step mathematical puzzle. Answer in one word.",
        "Solve this multi-step mathematical problem: 37 + 58. Answer in one word.",
        "Solve for x: 2x + 5 = 15. Answer with just the number.",
        "Plan step-by-step to calculate the trajectory of the satellite. Answer in one word.",
        "Debug this complex numerical instability in our matrix solver. Be concise.",
        "Analyze the convergence rate of Newton-Raphson vs gradient descent. Answer in one word.",
    ]

    for prompt in deep_prompts:
        intent = await engine.classify_intent(prompt)
        assert intent.reasoning_depth == ReasoningDepth.DEEP, (
            f"Failed on '{prompt}': got {intent.reasoning_depth.value}, expected deep"
        )
        assert intent.category == "COMPLEX_TASK"


@pytest.mark.asyncio
async def test_simple_factual_with_concise_instructions():
    """Verify simple factual lookup questions with explicit concise instructions route to INSTANT."""
    engine = IntelligenceEngine()

    factual_prompts = [
        "What is the capital of Japan? Answer in one word.",
        "State the boiling point of water in Celsius. Answer in one word.",
        "What is the largest planet in our solar system? Answer in one word.",
        "Who is the primary author of Romeo and Juliet? Answer in one word.",
        "Name the primary author of Hamlet. Answer in one word.",
        "What is the capital of France? Just the number.",
        "State the speed of light in vacuum. In one word.",
    ]

    for prompt in factual_prompts:
        intent = await engine.classify_intent(prompt)
        assert intent.reasoning_depth == ReasoningDepth.INSTANT, (
            f"Failed on '{prompt}': got {intent.reasoning_depth.value}, expected instant"
        )
        assert intent.category == "CONVERSATIONAL"


@pytest.mark.asyncio
async def test_ordinary_factual_queries_without_concise_instructions():
    """Verify ordinary factual questions enter INSTANT mode without requiring concise instructions."""
    engine = IntelligenceEngine()

    ordinary_factual_prompts = [
        "What is the capital of Japan?",
        "Who developed Python?",
        "Where is Mount Everest?",
        "When was the first iPhone released?",
        "What does CPU stand for?",
        "Define photosynthesis.",
        "What is the boiling point of water?",
        "Name three programming languages.",
        "What is the capital of France?",
        "Who wrote Romeo and Juliet?",
        "Where is the Great Barrier Reef?",
        "When was the Declaration of Independence signed?",
        "Define entropy.",
        "List three continents.",
        "Capital of Italy",
        "Speed of light in vacuum",
    ]

    for prompt in ordinary_factual_prompts:
        intent = await engine.classify_intent(prompt)
        assert intent.reasoning_depth == ReasoningDepth.INSTANT, (
            f"Failed on '{prompt}': got {intent.reasoning_depth.value}, expected instant"
        )
        assert intent.category == "CONVERSATIONAL"


@pytest.mark.asyncio
async def test_analytical_questions_with_factual_prefixes_preserved():
    """Verify queries with factual prefixes but analytical/explanatory intent preserve reasoning (STANDARD / DEEP)."""
    engine = IntelligenceEngine()

    analytical_cases = [
        ("Explain why Python is interpreted.", ReasoningDepth.STANDARD, "GENERAL_QUERY"),
        ("Compare RAM and ROM.", ReasoningDepth.STANDARD, "GENERAL_QUERY"),
        ("Explain how a CPU executes instructions.", ReasoningDepth.STANDARD, "GENERAL_QUERY"),
        ("Analyse the time complexity of binary search.", ReasoningDepth.DEEP, "COMPLEX_TASK"),
        ("Analyze the time complexity of binary search.", ReasoningDepth.DEEP, "COMPLEX_TASK"),
        ("Explain why this algorithm fails.", ReasoningDepth.STANDARD, "GENERAL_QUERY"),
        ("Debug this Python function.", ReasoningDepth.DEEP, "COMPLEX_TASK"),
        ("Solve this multi-step mathematical problem.", ReasoningDepth.DEEP, "COMPLEX_TASK"),
        ("What is Dijkstra's algorithm?", ReasoningDepth.STANDARD, "GENERAL_QUERY"),
        ("What is the time complexity of Dijkstra's algorithm?", ReasoningDepth.STANDARD, "GENERAL_QUERY"),
        ("What is the difference between RAM and ROM?", ReasoningDepth.STANDARD, "GENERAL_QUERY"),
        ("What is the output of this Python program?", ReasoningDepth.STANDARD, "GENERAL_QUERY"),
        ("What is the output of this Python program? Explain why.", ReasoningDepth.STANDARD, "GENERAL_QUERY"),
        ("What is 37 + 58? Explain how you calculated it.", ReasoningDepth.STANDARD, "GENERAL_QUERY"),
        ("What is Dijkstra's algorithm? Answer in one sentence.", ReasoningDepth.STANDARD, "GENERAL_QUERY"),
        ("Solve 37 + 58. Answer in one word.", ReasoningDepth.DEEP, "COMPLEX_TASK"),
    ]

    for prompt, expected_depth, expected_cat in analytical_cases:
        intent = await engine.classify_intent(prompt)
        assert intent.reasoning_depth == expected_depth, (
            f"Failed on '{prompt}': got {intent.reasoning_depth.value}, expected {expected_depth.value}"
        )
        assert intent.category == expected_cat


@pytest.mark.asyncio
async def test_complex_coding_and_algorithm_questions():
    """Verify coding, algorithmic, and explanatory queries preserve reasoning (STANDARD)."""
    engine = IntelligenceEngine()

    standard_prompts = [
        "Explain the time complexity of Dijkstra's algorithm. Be concise.",
        "Write a Python function to check whether a number is prime.",
        "Write a Python function named 'is_even' that returns True if an integer is even and False otherwise. Output only the code.",
        "Explain why the answer is 95.",
        "Explain why 37 + 58 = 95.",
        "Compare two engineering design approaches.",
        "Compare two engineering design approaches. Answer in one word.",
        "Explain the difference between a process and a thread. Answer in one word.",
        "A farmer has 17 sheep. All but 9 run away. How many sheep does the farmer have left? Explain your reasoning in two sentences.",
        "How does a binary search tree maintain its balance?",
    ]

    for prompt in standard_prompts:
        intent = await engine.classify_intent(prompt)
        assert intent.reasoning_depth == ReasoningDepth.STANDARD, (
            f"Failed on '{prompt}': got {intent.reasoning_depth.value}, expected standard"
        )
        assert intent.category == "GENERAL_QUERY"


@pytest.mark.asyncio
async def test_existing_routing_behavior():
    """Verify existing conversational greetings, system commands, and general queries remain intact."""
    engine = IntelligenceEngine()

    # Greetings & system commands -> INSTANT
    instant_cases = [
        ("hello", "CONVERSATIONAL"),
        ("hi Carbon", "CONVERSATIONAL"),
        ("ping", "CONVERSATIONAL"),
        ("status", "CONVERSATIONAL"),
        ("open Safari", "SYSTEM_COMMAND"),
        ("what time is it", "CONVERSATIONAL"),
        ("who are you", "CONVERSATIONAL"),
        (
            "Hello Carbon! How can you assist me today as a macOS AI assistant? Respond in two concise sentences.",
            "CONVERSATIONAL",
        ),
    ]
    for prompt, expected_cat in instant_cases:
        intent = await engine.classify_intent(prompt)
        assert intent.reasoning_depth == ReasoningDepth.INSTANT, (
            f"Failed on '{prompt}': expected instant, got {intent.reasoning_depth.value}"
        )
        assert intent.category == expected_cat

    # Deep debug / architecture -> DEEP
    deep_cases = [
        "Debug this memory leak in our C++ engine.",
        "Architect a resilient event-driven microservice system.",
        "Perform a comprehensive code review of this PR.",
    ]
    for prompt in deep_cases:
        intent = await engine.classify_intent(prompt)
        assert intent.reasoning_depth == ReasoningDepth.DEEP, (
            f"Failed on '{prompt}': expected deep, got {intent.reasoning_depth.value}"
        )
        assert intent.category == "COMPLEX_TASK"


@pytest.mark.asyncio
async def test_orchestrator_integration_mode_selection(tmp_path):
    """Verify CarbonCore selects correct inference mode (think_flag and options) based on classified depth."""
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
        yield {"response": "Done", "done": True}

    with patch.object(
        core.provider, "generate_stream_events", side_effect=mock_generate_stream_events
    ):
        # 1. Simple arithmetic: automatically classified as INSTANT -> think=False, num_predict=512
        req_arithmetic = TaskRequest(prompt="What is 37 + 58?")
        task_arithmetic = await core.create_task(req_arithmetic)

        async for _ in core.execute_task_stream(task_arithmetic):
            pass

        assert task_arithmetic.reasoning_depth == ReasoningDepth.INSTANT
        assert captured_calls[-1]["think"] is False
        assert captured_calls[-1]["options"] == {"num_predict": 512, "temperature": 0.2}

        # 2. Complex task with concise instruction: classified as DEEP -> think=True, num_predict=4096
        req_complex = TaskRequest(
            prompt="Solve this multi-step mathematical puzzle. Answer in one word."
        )
        task_complex = await core.create_task(req_complex)

        async for _ in core.execute_task_stream(task_complex):
            pass

        assert task_complex.reasoning_depth == ReasoningDepth.DEEP
        assert captured_calls[-1]["think"] is True
        assert captured_calls[-1]["options"] == {"num_predict": 4096, "temperature": 0.7}

        # 3. Explanatory task with concise instruction: classified as STANDARD -> think=False, num_predict=2560
        req_standard = TaskRequest(
            prompt="Explain the time complexity of Dijkstra's algorithm. Be concise."
        )
        task_standard = await core.create_task(req_standard)

        async for _ in core.execute_task_stream(task_standard):
            pass

        assert task_standard.reasoning_depth == ReasoningDepth.STANDARD
        assert captured_calls[-1]["think"] is False
        assert captured_calls[-1]["options"] == {"num_predict": 2560, "temperature": 0.6}

    await core.shutdown()


@pytest.mark.asyncio
async def test_stage1_9_task_request_normalization():
    """Verify TaskRequest correctly normalizes reasoning overrides including 'auto'."""
    # 1. 'auto' string normalizes to None (no override)
    req_auto = TaskRequest(prompt="Hello", reasoning_override="auto")  # type: ignore[arg-type]
    assert req_auto.reasoning_override is None

    req_auto_upper = TaskRequest(prompt="Hello", reasoning_override="AUTO")  # type: ignore[arg-type]
    assert req_auto_upper.reasoning_override is None

    req_none = TaskRequest(prompt="Hello", reasoning_override=None)
    assert req_none.reasoning_override is None

    # 2. Valid enum strings normalize to ReasoningDepth
    req_instant = TaskRequest(prompt="Hello", reasoning_override="instant")  # type: ignore[arg-type]
    assert req_instant.reasoning_override == ReasoningDepth.INSTANT

    req_standard = TaskRequest(prompt="Hello", reasoning_override="STANDARD")  # type: ignore[arg-type]
    assert req_standard.reasoning_override == ReasoningDepth.STANDARD

    req_deep = TaskRequest(prompt="Hello", reasoning_override="deep")  # type: ignore[arg-type]
    assert req_deep.reasoning_override == ReasoningDepth.DEEP


@pytest.mark.asyncio
async def test_stage1_9_orchestrator_auto_and_explicit_routing(tmp_path):
    """Verify Orchestrator behavior for AUTO mode vs explicit overrides."""
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
        yield {"response": "Response token", "done": True}

    with patch.object(
        core.provider, "generate_stream_events", side_effect=mock_generate_stream_events
    ):
        # 1. AUTO mode on factual question -> classified as INSTANT, think=False
        req_auto_fact = TaskRequest(prompt="What does RAM stand for?", reasoning_override=None)
        task_auto_fact = await core.create_task(req_auto_fact)
        events = []
        async for evt in core.execute_task_stream(task_auto_fact):
            events.append(evt)

        assert task_auto_fact.reasoning_depth == ReasoningDepth.INSTANT
        assert captured_calls[-1]["think"] is False
        assert captured_calls[-1]["options"]["num_predict"] == 512
        classified_evts = [e for e in events if e.get("event") == "intent_classified"]
        assert len(classified_evts) == 1
        assert classified_evts[0]["reasoning_depth"] == "instant"

        # 2. AUTO mode on explanation -> classified as STANDARD, think=False, num_predict=2560
        req_auto_exp = TaskRequest(prompt="Explain what a CPU does.", reasoning_override=None)
        task_auto_exp = await core.create_task(req_auto_exp)
        events.clear()
        async for evt in core.execute_task_stream(task_auto_exp):
            events.append(evt)

        assert task_auto_exp.reasoning_depth == ReasoningDepth.STANDARD
        assert captured_calls[-1]["think"] is False
        assert captured_calls[-1]["options"]["num_predict"] == 2560

        # 3. Explicit DEEP override on simple query -> forced DEEP, think=True, num_predict=4096
        req_deep_override = TaskRequest(
            prompt="What is 2+2?", reasoning_override=ReasoningDepth.DEEP
        )
        task_deep = await core.create_task(req_deep_override)
        events.clear()
        async for evt in core.execute_task_stream(task_deep):
            events.append(evt)

        assert task_deep.reasoning_depth == ReasoningDepth.DEEP
        assert captured_calls[-1]["think"] is True
        assert captured_calls[-1]["options"]["num_predict"] == 4096

        # 4. Explicit INSTANT override on explanation -> forced INSTANT, think=False, num_predict=512
        req_instant_override = TaskRequest(
            prompt="Explain quantum entanglement.", reasoning_override=ReasoningDepth.INSTANT
        )
        task_instant = await core.create_task(req_instant_override)
        events.clear()
        async for evt in core.execute_task_stream(task_instant):
            events.append(evt)

        assert task_instant.reasoning_depth == ReasoningDepth.INSTANT
        assert captured_calls[-1]["think"] is False
        assert captured_calls[-1]["options"]["num_predict"] == 512

    await core.shutdown()
