"""Dedicated Stage 4.4-A tests for task category separation, unified code detection, and capabilities."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from carbon.config.settings import Settings
from carbon.core.orchestrator import CarbonCore
from carbon.intelligence.engine import (
    IntelligenceEngine,
    derive_required_capabilities,
    is_coding_prompt,
)
from carbon.interfaces.models import (
    ModelCapability,
    ModelMetadata,
    ReasoningDepth,
    TaskCategory,
    TaskRequest,
    TaskState,
)
from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.fake_provider import FakeProvider


def _qwen_meta() -> ModelMetadata:
    return ModelMetadata(
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
        is_healthy=True,
    )


def test_task_category_enum_and_no_image_upload_fields_in_stage_4_4a() -> None:
    """Verify TaskCategory exposes expected values and VISION stays reserved without TaskRequest.images."""
    assert {c.value for c in TaskCategory} == {"general", "code", "vision", "deep_reasoning"}
    assert "images" not in TaskRequest.model_fields


@pytest.mark.asyncio
async def test_general_questions_classify_as_general_with_preserved_categories() -> None:
    """General factual and explanatory questions classify as GENERAL while preserving legacy categories."""
    engine = IntelligenceEngine()

    cases = [
        ("hello", "CONVERSATIONAL", ReasoningDepth.INSTANT),
        ("What is the capital of Japan?", "CONVERSATIONAL", ReasoningDepth.INSTANT),
        ("What is 37 + 58?", "CONVERSATIONAL", ReasoningDepth.INSTANT),
        ("open Safari", "SYSTEM_COMMAND", ReasoningDepth.INSTANT),
        ("Remember that my favorite tea is matcha", "MEMORY_STORE", ReasoningDepth.INSTANT),
        ("What is my favorite tea?", "MEMORY_QUERY", ReasoningDepth.INSTANT),
        ("Forget my favorite tea", "MEMORY_FORGET", ReasoningDepth.INSTANT),
        ("Explain why Python is interpreted.", "GENERAL_QUERY", ReasoningDepth.STANDARD),
        ("Compare RAM and ROM.", "GENERAL_QUERY", ReasoningDepth.STANDARD),
        ("What is Dijkstra's algorithm?", "GENERAL_QUERY", ReasoningDepth.STANDARD),
        (
            "Explain why binary search requires sorted input.",
            "GENERAL_QUERY",
            ReasoningDepth.STANDARD,
        ),
    ]

    for prompt, expected_cat, expected_depth in cases:
        intent = await engine.classify_intent(prompt)
        assert intent.category == expected_cat, prompt
        assert intent.task_category == TaskCategory.GENERAL, prompt
        assert intent.reasoning_depth == expected_depth, prompt
        assert intent.required_capabilities == [ModelCapability.CHAT], prompt


@pytest.mark.asyncio
async def test_coding_requests_classify_consistently_as_code_without_forcing_deep() -> None:
    """Coding requests share one classifier, get CODE category, and keep STANDARD unless escalated."""
    engine = IntelligenceEngine()

    standard_coding_prompts = [
        "Write a Python function to check whether a number is prime.",
        "Write a Java method to find the second-largest distinct element in an integer array.",
        "Implement a rate limiter in Go",
        "Create a SQL query to count monthly active users",
        "Write code to parse CSV rows cleanly",
        "What is the output of this Python program?",
        "Write unit tests for the authentication handler",
    ]
    for prompt in standard_coding_prompts:
        assert is_coding_prompt(prompt) is True
        intent = await engine.classify_intent(prompt)
        assert intent.category == "GENERAL_QUERY", prompt
        assert intent.task_category == TaskCategory.CODE, prompt
        assert intent.reasoning_depth == ReasoningDepth.STANDARD, prompt
        assert intent.required_capabilities == [ModelCapability.CHAT], prompt

    # Deep coding requests classify as CODE AND keep DEEP reasoning + THINKING
    deep_coding_prompts = [
        "Debug this Python function.",
        "Debug this memory leak in our C++ engine.",
        "Refactor this TypeScript class for cleaner async flow.",
        "Perform a comprehensive code review of this PR.",
    ]
    for prompt in deep_coding_prompts:
        assert is_coding_prompt(prompt) is True
        intent = await engine.classify_intent(prompt)
        assert intent.category == "COMPLEX_TASK", prompt
        assert intent.task_category == TaskCategory.CODE, prompt
        assert intent.reasoning_depth == ReasoningDepth.DEEP, prompt
        assert intent.required_capabilities == [ModelCapability.CHAT, ModelCapability.THINKING]


@pytest.mark.asyncio
async def test_general_deep_and_capability_derivation_stay_independent() -> None:
    """Verify general deep reasoning uses DEEP_REASONING and capability rules enforce THINKING properly."""
    engine = IntelligenceEngine()

    deep_non_coding = [
        "Solve this multi-step mathematical puzzle.",
        "Analyze the convergence rate of Newton-Raphson vs gradient descent.",
        "Plan step-by-step to calculate the trajectory of the satellite.",
    ]
    for prompt in deep_non_coding:
        assert is_coding_prompt(prompt) is False
        intent = await engine.classify_intent(prompt)
        assert intent.category == "COMPLEX_TASK", prompt
        assert intent.task_category == TaskCategory.DEEP_REASONING, prompt
        assert intent.reasoning_depth == ReasoningDepth.DEEP, prompt
        assert intent.required_capabilities == [ModelCapability.CHAT, ModelCapability.THINKING]

    # Direct derive_required_capabilities combinations
    assert derive_required_capabilities(
        task_category=TaskCategory.CODE,
        reasoning_depth=ReasoningDepth.INSTANT,
    ) == [ModelCapability.CHAT]
    assert derive_required_capabilities(
        task_category=TaskCategory.CODE,
        reasoning_depth=ReasoningDepth.STANDARD,
    ) == [ModelCapability.CHAT]
    assert derive_required_capabilities(
        task_category=TaskCategory.GENERAL,
        reasoning_depth=ReasoningDepth.DEEP,
    ) == [ModelCapability.CHAT, ModelCapability.THINKING]
    assert derive_required_capabilities(
        task_category=TaskCategory.DEEP_REASONING,
        reasoning_depth=ReasoningDepth.DEEP,
    ) == [ModelCapability.CHAT, ModelCapability.THINKING]
    # VISION remains reserved and is never enabled through text capability derivation
    assert derive_required_capabilities(
        task_category=TaskCategory.VISION,
        reasoning_depth=ReasoningDepth.STANDARD,
        base_capabilities=[ModelCapability.VISION, ModelCapability.CHAT],
    ) == [ModelCapability.CHAT]

    # Direct classify_intent with context reasoning_override applies DEEP before deriving THINKING
    direct_deep = await engine.classify_intent(
        "What is 2+2?", context={"reasoning_override": "deep"}
    )
    assert direct_deep.task_category == TaskCategory.GENERAL
    assert direct_deep.reasoning_depth == ReasoningDepth.DEEP
    assert direct_deep.required_capabilities == [ModelCapability.CHAT, ModelCapability.THINKING]


@pytest.mark.asyncio
async def test_text_questions_about_images_or_vision_never_claim_vision_support() -> None:
    """Text queries mentioning images or computer vision stay GENERAL and never require VISION."""
    engine = IntelligenceEngine()

    prompts = [
        "What is computer vision?",
        "Explain how vision transformers process images.",
        "Describe how PNG image compression works.",
        "Compare JPEG and WebP image formats.",
    ]
    for prompt in prompts:
        intent = await engine.classify_intent(prompt)
        assert intent.task_category == TaskCategory.GENERAL, prompt
        assert intent.task_category != TaskCategory.VISION, prompt
        assert ModelCapability.VISION not in intent.required_capabilities


@pytest.mark.asyncio
async def test_orchestrator_applies_reasoning_override_before_capability_derivation(
    tmp_path: Path,
) -> None:
    """Verify reasoning_override=DEEP adds THINKING to intent and CMS selection calls cleanly."""
    provider = FakeProvider(
        models=[_qwen_meta()],
        running_models={"qwen3.5:9b"},
        default_response="Done.",
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    core.provider = provider
    core.cms = CarbonModelSupervisor(provider=provider)
    await core.initialize()

    select_spy = AsyncMock(wraps=core.cms.select_model_for_task)
    resolve_spy = core.cms.resolve_model_for_leased_task
    captured_resolve_caps: list[list[ModelCapability]] = []

    def _wrapped_resolve(capabilities_required, reasoning, **kwargs):
        captured_resolve_caps.append(list(capabilities_required))
        return resolve_spy(capabilities_required, reasoning, **kwargs)

    core.cms.select_model_for_task = select_spy  # type: ignore[method-assign]
    core.cms.resolve_model_for_leased_task = _wrapped_resolve  # type: ignore[method-assign]
    try:
        # 1. Simple GENERAL prompt with reasoning_override=DEEP must require THINKING (never stale [CHAT])
        t_deep = await core.create_task(
            TaskRequest(prompt="What is 2+2?", reasoning_override=ReasoningDepth.DEEP)
        )
        ev_deep = [e async for e in core.execute_task_stream(t_deep)]

        assert t_deep.state == TaskState.COMPLETED
        assert t_deep.intent is not None
        assert t_deep.intent.task_category == TaskCategory.GENERAL
        assert t_deep.reasoning_depth == ReasoningDepth.DEEP
        assert ModelCapability.THINKING in t_deep.intent.required_capabilities
        assert select_spy.await_args_list[-1].kwargs["capabilities_required"] == [
            ModelCapability.CHAT,
            ModelCapability.THINKING,
        ]
        assert captured_resolve_caps[-1] == [ModelCapability.CHAT, ModelCapability.THINKING]
        classified_deep = [e for e in ev_deep if e.get("event") == "intent_classified"][0]
        assert classified_deep["task_category"] == TaskCategory.GENERAL.value
        assert classified_deep["reasoning_depth"] == ReasoningDepth.DEEP.value

        # 2. CODE task with reasoning_override=INSTANT keeps TaskCategory.CODE without requiring THINKING
        t_code_fast = await core.create_task(
            TaskRequest(
                prompt="Write a Python function to reverse a string",
                reasoning_override=ReasoningDepth.INSTANT,
            )
        )
        _ = [e async for e in core.execute_task_stream(t_code_fast)]

        assert t_code_fast.state == TaskState.COMPLETED
        assert t_code_fast.intent is not None
        assert t_code_fast.intent.task_category == TaskCategory.CODE
        assert t_code_fast.reasoning_depth == ReasoningDepth.INSTANT
        assert t_code_fast.intent.required_capabilities == [ModelCapability.CHAT]
        assert captured_resolve_caps[-1] == [ModelCapability.CHAT]

        # 3. Unified CODE prompt handling in orchestrator matches STANDARD coding prompt template
        t_code_std = await core.create_task(
            TaskRequest(prompt="Write a Java method to compute factorial.")
        )
        _ = [e async for e in core.execute_task_stream(t_code_std)]
        assert t_code_std.intent is not None
        assert t_code_std.intent.task_category == TaskCategory.CODE
        assert t_code_std.reasoning_depth == ReasoningDepth.STANDARD
        gen_calls = [c for c in provider.calls if c["method"] == "generate_stream_events"]
        assert (
            "optimal approach and provide a complete working implementation"
            in gen_calls[-1]["system"]
        )
    finally:
        await core.shutdown()
