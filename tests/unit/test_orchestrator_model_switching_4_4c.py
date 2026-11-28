"""Dedicated Stage 4.4-C tests for orchestrator routing, pre-lease switching, concurrency, num_ctx, and safety."""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from carbon.config.settings import DEFAULT_APPROVED_MODELS, Settings
from carbon.core.orchestrator import MAX_MODEL_ROUTING_ATTEMPTS, CarbonCore
from carbon.governance.governance_engine import verify_audit_log_chain
from carbon.interfaces.models import (
    ApprovedModelProfile,
    ModelCapability,
    ModelMetadata,
    ModelSwitchFailureReason,
    ModelSwitchRequest,
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
        ],
        is_healthy=True,
    )


def _coder_meta(model_id: str = "qwen-coder:7b") -> ModelMetadata:
    return ModelMetadata(
        id=model_id,
        name="Qwen Coder 7B",
        provider="ollama",
        family="qwen_coder",
        parameter_size="7.0B",
        quantization="Q4_K_M",
        context_length=16384,
        max_architectural_context=131072,
        memory_footprint_mb=4700,
        capabilities=[
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
        ],
        is_healthy=True,
    )


def _coder_profile(model_id: str = "qwen-coder:7b") -> ApprovedModelProfile:
    return ApprovedModelProfile(
        model_id=model_id,
        supported_capabilities=(
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
        ),
        suitable_task_categories=(TaskCategory.CODE,),
        preferred_task_categories=(TaskCategory.CODE,),
        estimated_memory_mb=4700,
        allow_under_memory_pressure=True,
        max_pressure_level=2,
        max_recommended_context_tokens=32768,
    )


def _general_only_meta(model_id: str = "general-only:8b") -> ModelMetadata:
    return ModelMetadata(
        id=model_id,
        name=model_id,
        provider="ollama",
        family="general",
        memory_footprint_mb=5100,
        capabilities=[ModelCapability.COMPLETION, ModelCapability.CHAT],
        is_healthy=True,
    )


def _general_only_profile(
    model_id: str = "general-only:8b",
    *,
    allow_thinking: bool = False,
    max_pressure_level: int = 2,
    memory_mb: int = 5100,
) -> ApprovedModelProfile:
    caps: list[ModelCapability] = [ModelCapability.COMPLETION, ModelCapability.CHAT]
    suitable: list[TaskCategory] = [TaskCategory.GENERAL]
    if allow_thinking:
        caps.append(ModelCapability.THINKING)
        suitable.append(TaskCategory.DEEP_REASONING)
    return ApprovedModelProfile(
        model_id=model_id,
        supported_capabilities=tuple(caps),
        suitable_task_categories=tuple(suitable),
        preferred_task_categories=(TaskCategory.GENERAL,),
        estimated_memory_mb=memory_mb,
        allow_under_memory_pressure=True,
        max_pressure_level=max_pressure_level,
    )


async def _make_core(
    tmp_path: Path,
    provider: FakeProvider,
    *,
    default_model_id: str = "qwen3.5:9b",
    approved_models: tuple[str, ...] = ("qwen3.5:9b",),
    model_profiles: dict[str, ApprovedModelProfile] | None = None,
) -> tuple[CarbonCore, Settings]:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    core.provider = provider
    core.cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id=default_model_id,
        approved_models=approved_models,
        model_profiles=model_profiles,
        governance=core.governance,
    )
    await core.initialize()
    return core, settings


def _assert_lease_clean(cms: CarbonModelSupervisor) -> None:
    assert cms._active_leases == set()
    assert cms._inference_lock.locked() is False
    assert cms._drain_event.is_set() is True
    assert cms._switch_pending is False
    assert cms._switch_clear_event.is_set() is True


@pytest.mark.asyncio
async def test_suitable_active_model_performs_no_unnecessary_switch(tmp_path: Path) -> None:
    """When active approved model already satisfies GENERAL/CODE/DEEP, zero switches occur."""
    provider = FakeProvider(
        models=[_qwen_meta(), _coder_meta()],
        running_models={"qwen3.5:9b"},
        default_response="Qwen sticky reply.",
    )
    core, settings = await _make_core(
        tmp_path,
        provider,
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
        model_profiles={"qwen-coder:7b": _coder_profile()},
    )
    switch_spy = AsyncMock(wraps=core.cms.switch_model)
    core.cms.switch_model = switch_spy  # type: ignore[method-assign]
    provider.reset_history()
    try:
        for prompt in (
            "What is the capital of France?",
            "Write a Python function to check palindromes.",
            "Solve this multi-step logic puzzle.",
        ):
            task = await core.create_task(TaskRequest(prompt=prompt))
            events = [e async for e in core.execute_task_stream(task)]
            assert task.state == TaskState.COMPLETED
            assert task.model_used == "qwen3.5:9b"
            assert events[-1]["event"] == "completed"
            assert not any(e.get("event") == "model_switching" for e in events)

        assert switch_spy.await_count == 0
        assert provider.loaded_models == []
        assert provider.unloaded_models == []
        _assert_lease_clean(core.cms)

        audit_text = settings.audit_log_path.read_text(encoding="utf-8")
        assert "MODEL_ROUTING_DECIDED" in audit_text
        assert "MODEL_POST_LEASE_VERIFIED" in audit_text
        assert "MODEL_SWITCH_REQUESTED" not in audit_text
        ok_chain, count, err = verify_audit_log_chain(settings.audit_log_path)
        assert ok_chain and count > 0, err
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_unsuitable_active_model_switches_before_lease_and_preserves_memory(
    tmp_path: Path,
) -> None:
    """Unsuitable active model triggers pre-lease switch, single residency, verified inference, and memory continuity."""
    provider = FakeProvider(
        models=[_general_only_meta("general-only:8b"), _coder_meta()],
        running_models={"general-only:8b"},
        responses_by_model={
            "general-only:8b": "General response.",
            "qwen-coder:7b": "def fib(n: int) -> int: return n",
        },
    )
    core, settings = await _make_core(
        tmp_path,
        provider,
        default_model_id="general-only:8b",
        approved_models=("general-only:8b", "qwen-coder:7b"),
        model_profiles={
            "general-only:8b": _general_only_profile("general-only:8b"),
            "qwen-coder:7b": _coder_profile(),
        },
    )

    lease_state_during_switch: list[tuple[str, set[str], bool, set[str]]] = []
    orig_switch = core.cms.switch_model

    async def spied_switch(req: ModelSwitchRequest) -> bool:
        lease_state_during_switch.append(
            (
                req.target_model_id,
                set(core.cms._active_leases),
                core.cms._inference_lock.locked(),
                set(provider.running_models),
            )
        )
        ok = await orig_switch(req)
        assert len(provider.running_models) <= 1
        return ok

    core.cms.switch_model = spied_switch  # type: ignore[method-assign]
    conv_id = "conv-stage-4-4c-continuity"
    try:
        # Turn 1: Store preference & run GENERAL on general-only:8b
        t_mem = await core.create_task(
            TaskRequest(
                prompt="Remember that my favorite language is Rust", conversation_id=conv_id
            )
        )
        _ = [e async for e in core.execute_task_stream(t_mem)]
        assert t_mem.state == TaskState.COMPLETED

        # Turn 2: Standard GENERAL keeps general-only:8b (0 switches)
        t_gen = await core.create_task(
            TaskRequest(
                prompt="Explain what my favorite language is known for.",
                conversation_id=conv_id,
            )
        )
        ev_gen = [e async for e in core.execute_task_stream(t_gen)]
        assert t_gen.state == TaskState.COMPLETED
        assert t_gen.model_used == "general-only:8b"
        assert len(lease_state_during_switch) == 0
        assert ev_gen[-1]["event"] == "completed"

        # Turn 3: CODE request sees general-only:8b unsuitable -> switches to qwen-coder:7b BEFORE lease!
        secret_code_prompt = "Write a Python FFI function for the systems language"
        t_code = await core.create_task(
            TaskRequest(prompt=secret_code_prompt, conversation_id=conv_id)
        )
        ev_code = [e async for e in core.execute_task_stream(t_code)]
        assert t_code.state == TaskState.COMPLETED
        assert t_code.model_used == "qwen-coder:7b"
        assert t_code.result == "def fib(n: int) -> int: return n"
        assert any(
            e.get("event") == "model_switching" and e.get("target_model") == "qwen-coder:7b"
            for e in ev_code
        )
        assert ev_code[-1]["event"] == "completed"

        # Verify switch happened strictly with zero leases held and zero lock held
        assert len(lease_state_during_switch) == 1
        assert lease_state_during_switch[0][0] == "qwen-coder:7b"
        assert lease_state_during_switch[0][1] == set()
        assert lease_state_during_switch[0][2] is False

        # Verify single-resident-model invariant (general-only:8b unloaded, qwen-coder:7b running)
        assert provider.running_models == {"qwen-coder:7b"}
        assert "general-only:8b" in provider.unloaded_models
        _assert_lease_clean(core.cms)

        # Verify conversation memory context remained intact across model switch & was not truncated
        gen_calls = [c for c in provider.calls if c["method"] == "generate_stream_events"]
        last_system = gen_calls[-1]["system"] or ""
        assert "Rust" in last_system
        assert "Explain what my favorite language is known for." in last_system

        # Verify next GENERAL prompt safely switches back from qwen-coder:7b to general-only:8b
        t_gen2 = await core.create_task(
            TaskRequest(prompt="Explain how garbage collection differs.", conversation_id=conv_id)
        )
        _ = [e async for e in core.execute_task_stream(t_gen2)]
        assert t_gen2.state == TaskState.COMPLETED
        assert t_gen2.model_used == "general-only:8b"
        assert provider.running_models == {"general-only:8b"}
        _assert_lease_clean(core.cms)

        # Check complete audit trail and zero prompt leakage
        audit_text = settings.audit_log_path.read_text(encoding="utf-8")
        for evt_name in (
            "MODEL_ROUTING_DECIDED",
            "MODEL_SWITCH_REQUESTED",
            "MODEL_SWITCH_SUCCEEDED",
            "MODEL_POST_LEASE_VERIFIED",
        ):
            assert evt_name in audit_text
        assert secret_code_prompt not in audit_text
        ok_chain, count, err = verify_audit_log_chain(settings.audit_log_path)
        assert ok_chain and count > 0, err
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_activation_failure_and_load_timeout_rollback_prevent_inference(
    tmp_path: Path,
) -> None:
    """Failed load or load timeout rolls back safely, records audit events, and runs zero inference."""
    # Case 1: Target load failure (fail_load_models)
    provider1 = FakeProvider(
        models=[_general_only_meta("general-only:8b"), _coder_meta()],
        running_models={"general-only:8b"},
        fail_load_models={"qwen-coder:7b"},
    )
    core1, settings1 = await _make_core(
        tmp_path / "c1",
        provider1,
        default_model_id="general-only:8b",
        approved_models=("general-only:8b", "qwen-coder:7b"),
        model_profiles={
            "general-only:8b": _general_only_profile("general-only:8b"),
            "qwen-coder:7b": _coder_profile(),
        },
    )
    try:
        provider1.reset_history()
        t_fail = await core1.create_task(
            TaskRequest(prompt="Write a Python binary search function")
        )
        ev_fail = [e async for e in core1.execute_task_stream(t_fail)]

        assert t_fail.state == TaskState.FAILED
        assert t_fail.model_used is None
        assert ev_fail[-1]["event"] == "failed"
        assert all(c["method"] != "generate_stream_events" for c in provider1.calls)
        _assert_lease_clean(core1.cms)

        # Rollback restored previous general-only:8b active cleanly, never silently falling back for inference
        active = await core1.cms.get_active_model()
        assert active is not None and active.id == "general-only:8b"
        assert provider1.running_models == {"general-only:8b"}

        audit_text1 = settings1.audit_log_path.read_text(encoding="utf-8")
        assert "MODEL_SWITCH_FAILED" in audit_text1
        assert "MODEL_INFERENCE_REJECTED" in audit_text1
        ok_chain, _, err = verify_audit_log_chain(settings1.audit_log_path)
        assert ok_chain, err
    finally:
        await core1.shutdown()

    # Case 2: Load timeout on switch target -> fails closed cleanly and rolls back active state
    provider2 = FakeProvider(
        models=[_general_only_meta("general-only:8b"), _coder_meta()],
        running_models={"general-only:8b"},
    )
    core2, settings2 = await _make_core(
        tmp_path / "c2",
        provider2,
        default_model_id="general-only:8b",
        approved_models=("general-only:8b", "qwen-coder:7b"),
        model_profiles={
            "general-only:8b": _general_only_profile("general-only:8b"),
            "qwen-coder:7b": _coder_profile(),
        },
    )
    try:
        orig_load = provider2.load_model

        async def slow_coder_load(model_id: str) -> bool:
            if model_id == "qwen-coder:7b":
                # Simulate target load timing out cleanly inside provider
                provider2.calls.append({"method": "load_model", "model_id": model_id})
                return False
            return await orig_load(model_id)

        provider2.load_model = slow_coder_load  # type: ignore[method-assign]
        t_to = await core2.create_task(TaskRequest(prompt="Write a SQL query to group users"))
        ev_to = [e async for e in core2.execute_task_stream(t_to)]

        assert t_to.state == TaskState.FAILED
        assert ev_to[-1]["event"] == "failed"
        assert all(c["method"] != "generate_stream_events" for c in provider2.calls)
        active2 = await core2.cms.get_active_model()
        assert active2 is not None and active2.id == "general-only:8b"
        _assert_lease_clean(core2.cms)
        assert "MODEL_SWITCH_FAILED" in settings2.audit_log_path.read_text(encoding="utf-8")
    finally:
        await core2.shutdown()


@pytest.mark.asyncio
async def test_drain_timeout_blocks_unsafe_switch_and_preserves_active_task(tmp_path: Path) -> None:
    """When an active lease fails to drain within deadline, switching fails with DRAIN_TIMEOUT."""
    provider = FakeProvider(
        models=[_general_only_meta("general-only:8b"), _coder_meta()],
        running_models={"general-only:8b"},
        default_response="Safe response.",
    )
    core, settings = await _make_core(
        tmp_path,
        provider,
        default_model_id="general-only:8b",
        approved_models=("general-only:8b", "qwen-coder:7b"),
        model_profiles={
            "general-only:8b": _general_only_profile("general-only:8b"),
            "qwen-coder:7b": _coder_profile(),
        },
    )
    core.cms.drain_timeout_seconds = 0.05
    try:
        await core.cms.acquire_inference_lease("long-running-task")
        provider.reset_history()

        t_blocked = await core.create_task(TaskRequest(prompt="Write a Rust sort function"))
        ev_blocked = [e async for e in core.execute_task_stream(t_blocked)]

        assert t_blocked.state == TaskState.FAILED
        assert "drain" in (t_blocked.error or "").lower()
        assert ev_blocked[-1]["event"] == "failed"
        assert all(c["method"] != "generate_stream_events" for c in provider.calls)
        assert provider.unloaded_models == []
        assert provider.loaded_models == []
        assert "long-running-task" in core.cms._active_leases
        assert (await core.cms.get_active_model()).id == "general-only:8b"  # type: ignore[union-attr]

        audit_text = settings.audit_log_path.read_text(encoding="utf-8")
        assert "MODEL_SWITCH_FAILED" in audit_text
        assert ModelSwitchFailureReason.DRAIN_TIMEOUT.value in audit_text
        assert "MODEL_INFERENCE_REJECTED" in audit_text

        await core.cms.release_inference_lease("long-running-task")
        _assert_lease_clean(core.cms)
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_active_model_change_between_planning_and_lease_re_plans_and_bounds_retries(
    tmp_path: Path,
) -> None:
    """Concurrent switch to incompatible model triggers bounded lease-free retry (max 2 attempts, no loop)."""
    # Part 1: One concurrent interference -> Attempt 1 detects mismatch post-lease, releases lease,
    # re-plans on Attempt 2, switches cleanly, verifies, and completes inference.
    provider1 = FakeProvider(
        models=[_general_only_meta("general-only:8b"), _coder_meta()],
        running_models={"general-only:8b"},
        default_response="Recovered on coder after single re-plan.",
    )
    core1, settings1 = await _make_core(
        tmp_path / "once",
        provider1,
        default_model_id="general-only:8b",
        approved_models=("general-only:8b", "qwen-coder:7b"),
        model_profiles={
            "general-only:8b": _general_only_profile("general-only:8b"),
            "qwen-coder:7b": _coder_profile(),
        },
    )
    try:
        orig_acquire = core1.cms.acquire_inference_lease
        orig_select = core1.cms.select_model_for_task
        select_lease_state: list[set[str]] = []
        acquire_count = 0

        async def spied_select(*args: Any, **kwargs: Any) -> ModelMetadata:
            select_lease_state.append(set(core1.cms._active_leases))
            return await orig_select(*args, **kwargs)

        async def steal_once_before_lease(task_id: str, timeout: float = 60.0) -> None:
            nonlocal acquire_count
            acquire_count += 1
            if acquire_count == 1:
                assert await core1.cms.switch_model(
                    ModelSwitchRequest(
                        target_model_id="general-only:8b",
                        reason="concurrent task preempted active model",
                    )
                )
            await orig_acquire(task_id, timeout=timeout)

        core1.cms.select_model_for_task = spied_select  # type: ignore[method-assign]
        core1.cms.acquire_inference_lease = steal_once_before_lease  # type: ignore[method-assign]

        t_ok = await core1.create_task(TaskRequest(prompt="Write a Python function to sort tuples"))
        ev_ok = [e async for e in core1.execute_task_stream(t_ok)]

        assert t_ok.state == TaskState.COMPLETED
        assert t_ok.model_used == "qwen-coder:7b"
        assert acquire_count == 2
        assert select_lease_state == [set(), set()]
        assert ev_ok[-1]["event"] == "completed"
        _assert_lease_clean(core1.cms)

        audit1 = settings1.audit_log_path.read_text(encoding="utf-8")
        assert "MODEL_POST_LEASE_VERIFICATION_FAILED" in audit1
        assert "MODEL_POST_LEASE_VERIFIED" in audit1
        ok_chain, _, err = verify_audit_log_chain(settings1.audit_log_path)
        assert ok_chain, err
    finally:
        await core1.shutdown()

    # Part 2: Continuous concurrent preemption on EVERY lease acquisition stops at bounded limit (2)
    provider2 = FakeProvider(
        models=[_general_only_meta("general-only:8b"), _coder_meta()],
        running_models={"general-only:8b"},
    )
    core2, settings2 = await _make_core(
        tmp_path / "loop",
        provider2,
        default_model_id="general-only:8b",
        approved_models=("general-only:8b", "qwen-coder:7b"),
        model_profiles={
            "general-only:8b": _general_only_profile("general-only:8b"),
            "qwen-coder:7b": _coder_profile(),
        },
    )
    try:
        orig_acquire2 = core2.cms.acquire_inference_lease
        always_steal_calls = 0

        async def steal_every_time(task_id: str, timeout: float = 60.0) -> None:
            nonlocal always_steal_calls
            always_steal_calls += 1
            assert await core2.cms.switch_model(
                ModelSwitchRequest(
                    target_model_id="general-only:8b", reason="persistent contention"
                )
            )
            await orig_acquire2(task_id, timeout=timeout)

        core2.cms.acquire_inference_lease = steal_every_time  # type: ignore[method-assign]
        t_bounded = await core2.create_task(TaskRequest(prompt="Implement a stack class in Python"))
        ev_bounded = [e async for e in core2.execute_task_stream(t_bounded)]

        assert t_bounded.state == TaskState.FAILED
        assert always_steal_calls == MAX_MODEL_ROUTING_ATTEMPTS == 2
        assert ev_bounded[-1]["event"] == "failed"
        assert all(c["method"] != "generate_stream_events" for c in provider2.calls)
        _assert_lease_clean(core2.cms)

        audit2 = settings2.audit_log_path.read_text(encoding="utf-8")
        assert "MODEL_POST_LEASE_VERIFICATION_FAILED" in audit2
        assert "MODEL_INFERENCE_REJECTED" in audit2
    finally:
        await core2.shutdown()


@pytest.mark.asyncio
async def test_concurrent_requests_enforce_single_lease_and_safe_switching(
    tmp_path: Path,
) -> None:
    """Concurrent GENERAL and CODE tasks serialize cleanly under single lease without unsafe unloading."""
    provider = FakeProvider(
        models=[_general_only_meta("general-only:8b"), _coder_meta()],
        running_models={"general-only:8b"},
        response_chunks=["Chunk1 ", "Chunk2"],
        chunk_delay_seconds=0.02,
    )
    core, _ = await _make_core(
        tmp_path,
        provider,
        default_model_id="general-only:8b",
        approved_models=("general-only:8b", "qwen-coder:7b"),
        model_profiles={
            "general-only:8b": _general_only_profile("general-only:8b"),
            "qwen-coder:7b": _coder_profile(),
        },
    )

    max_leases_seen = 0
    max_running_seen = 0
    orig_gen = provider.generate_stream_events

    async def tracked_gen(*args: Any, **kwargs: Any):
        nonlocal max_leases_seen, max_running_seen
        max_leases_seen = max(max_leases_seen, len(core.cms._active_leases))
        max_running_seen = max(max_running_seen, len(provider.running_models))
        async for evt in orig_gen(*args, **kwargs):
            max_leases_seen = max(max_leases_seen, len(core.cms._active_leases))
            max_running_seen = max(max_running_seen, len(provider.running_models))
            yield evt

    provider.generate_stream_events = tracked_gen  # type: ignore[method-assign]
    try:
        t_gen = await core.create_task(TaskRequest(prompt="What is the capital of Japan?"))
        t_code = await core.create_task(
            TaskRequest(prompt="Write a Python function to reverse words")
        )

        # Kick off t_gen first so it enters lease on general-only:8b, then launch t_code concurrently
        async def _collect(t: Any) -> list[dict[str, Any]]:
            return [e async for e in core.execute_task_stream(t)]

        task_a = asyncio.create_task(_collect(t_gen))
        await asyncio.sleep(0.01)
        task_b = asyncio.create_task(_collect(t_code))
        ev_a, ev_b = await asyncio.gather(task_a, task_b)

        assert t_gen.state == TaskState.COMPLETED
        assert t_gen.model_used == "general-only:8b"
        assert t_code.state == TaskState.COMPLETED
        assert t_code.model_used == "qwen-coder:7b"
        assert ev_a[-1]["event"] == "completed"
        assert ev_b[-1]["event"] == "completed"
        assert max_leases_seen == 1
        assert max_running_seen == 1
        _assert_lease_clean(core.cms)
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_cancellation_generator_exit_and_errors_release_leases_cleanly(
    tmp_path: Path,
) -> None:
    """Cancellation, asyncio.CancelledError, aclose GeneratorExit, and provider/verification errors always release leases."""
    provider = FakeProvider(
        models=[_general_only_meta("general-only:8b"), _coder_meta()],
        running_models={"general-only:8b"},
        response_chunks=["PartA ", "PartB ", "PartC"],
        chunk_delay_seconds=0.02,
    )
    core, _ = await _make_core(
        tmp_path,
        provider,
        default_model_id="general-only:8b",
        approved_models=("general-only:8b", "qwen-coder:7b"),
        model_profiles={
            "general-only:8b": _general_only_profile("general-only:8b"),
            "qwen-coder:7b": _coder_profile(),
        },
    )
    try:
        # 1. Pre-switch / mid-stream GeneratorExit via early aclose() after switching to qwen-coder:7b
        t_close = await core.create_task(
            TaskRequest(prompt="Write a Python function to merge dicts")
        )
        stream = core.execute_task_stream(t_close)
        async for evt in stream:
            if evt.get("event") == "token":
                break
        await stream.aclose()
        assert t_close.state == TaskState.CANCELLED
        assert t_close.result == "PartA "
        assert t_close.truncated is True
        persisted = await core.get_task_persistent(t_close.task_id)
        assert persisted is not None and persisted["state"] == TaskState.CANCELLED.value
        _assert_lease_clean(core.cms)

        # 2. asyncio.CancelledError mid-stream releases lease cleanly
        t_cancel = await core.create_task(TaskRequest(prompt="Write a Python class for a queue"))
        first_tok = asyncio.Event()

        async def _consume_until_token() -> None:
            async for evt in core.execute_task_stream(t_cancel):
                if evt.get("event") == "token":
                    first_tok.set()

        runner = asyncio.create_task(_consume_until_token())
        await asyncio.wait_for(first_tok.wait(), timeout=2.0)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert t_cancel.state == TaskState.CANCELLED
        _assert_lease_clean(core.cms)

        # 3. Cancel during pending pre-lease switch_model releases barrier and lease stays unacquired
        switch_started = asyncio.Event()
        release_switch = asyncio.Event()
        orig_switch = core.cms.switch_model

        async def slow_switch(req: ModelSwitchRequest) -> bool:
            switch_started.set()
            await release_switch.wait()
            return await orig_switch(req)

        core.cms.switch_model = slow_switch  # type: ignore[method-assign]
        t_pre = await core.create_task(TaskRequest(prompt="Explain DNS caching"))
        pre_events: list[dict[str, Any]] = []

        async def _consume_pre() -> None:
            async for evt in core.execute_task_stream(t_pre):
                pre_events.append(evt)

        pre_task = asyncio.create_task(_consume_pre())
        await asyncio.wait_for(switch_started.wait(), timeout=2.0)
        core.cancel_task(t_pre.task_id)
        release_switch.set()
        await asyncio.wait_for(pre_task, timeout=2.0)
        assert any(evt.get("event") == "cancelled" for evt in pre_events)
        assert t_pre.state == TaskState.CANCELLED
        _assert_lease_clean(core.cms)
        core.cms.switch_model = orig_switch  # type: ignore[method-assign]

        # 4. Provider stream exception releases lease cleanly & allows next request
        provider.simulate_error = TimeoutError("upstream hung")
        t_err = await core.create_task(TaskRequest(prompt="What is 2 + 2?"))
        ev_err = [e async for e in core.execute_task_stream(t_err)]
        assert t_err.state == TaskState.FAILED
        assert ev_err[-1]["event"] == "failed"
        _assert_lease_clean(core.cms)

        provider.simulate_error = None
        t_ok = await core.create_task(TaskRequest(prompt="What is 3 + 3?"))
        ev_ok = [e async for e in core.execute_task_stream(t_ok)]
        assert t_ok.state == TaskState.COMPLETED
        assert ev_ok[-1]["event"] == "completed"
        _assert_lease_clean(core.cms)
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_num_ctx_passed_to_ollama_and_updated_with_memory_pressure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cms.max_context_tokens is passed cleanly as Ollama options.num_ctx and stays consistent per request."""
    wire_payloads: list[dict[str, Any]] = []

    class _StreamRes:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aread(self) -> bytes:
            return b""

        async def aiter_lines(self):
            yield json.dumps({"response": "Wire OK", "done": True})

    class _FakeHttpClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _ = (args, kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method: str, endpoint: str, json: dict[str, Any]):
            _ = (method, endpoint)
            wire_payloads.append(json)
            return _StreamRes()

        async def get(self, endpoint: str) -> httpx.Response:
            req = httpx.Request("GET", f"http://127.0.0.1:11434{endpoint}")
            if endpoint == "/api/version":
                return httpx.Response(200, request=req, json={"version": "0.6.0"})
            if endpoint == "/api/tags":
                return httpx.Response(
                    200,
                    request=req,
                    json={
                        "models": [{"name": "qwen3.5:9b", "details": {"context_length": 262144}}]
                    },
                )
            if endpoint == "/api/ps":
                return httpx.Response(
                    200,
                    request=req,
                    json={"models": [{"name": "qwen3.5:9b", "context_length": 4096}]},
                )
            return httpx.Response(404, request=req)

    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    monkeypatch.setattr(core.provider, "_client", lambda *_a, **_kw: _FakeHttpClient())
    await core.initialize()
    try:
        # 1. Level 1 normal -> num_ctx = 32768 serialized into /api/generate JSON options
        assert core.cms.max_context_tokens == 32768
        t1 = await core.create_task(TaskRequest(prompt="What is 2 + 2?"))
        _ = [e async for e in core.execute_task_stream(t1)]
        assert t1.state == TaskState.COMPLETED
        assert wire_payloads[-1]["options"]["num_ctx"] == 32768
        assert wire_payloads[-1]["options"]["num_predict"] == 512

        # 2. Level 2 warning pressure -> num_ctx = 8192 passed cleanly to Ollama
        await core.cms.handle_memory_pressure(2)
        assert core.cms.max_context_tokens == 8192
        t2 = await core.create_task(TaskRequest(prompt="Explain binary search"))
        _ = [e async for e in core.execute_task_stream(t2)]
        assert t2.state == TaskState.COMPLETED
        assert wire_payloads[-1]["options"]["num_ctx"] == 8192
        assert wire_payloads[-1]["options"]["num_predict"] == 2560
        _assert_lease_clean(core.cms)
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_level4_deep_reasoning_and_allowlist_protections_remain_enforced(
    tmp_path: Path,
) -> None:
    """Level 4 critical memory pressure rejects DEEP reasoning even with a tiny active model and preserves allowlist."""
    assert DEFAULT_APPROVED_MODELS == ("qwen3.5:9b",)

    tiny_thinker = _general_only_meta("tiny-thinker:1.5b")
    provider = FakeProvider(
        models=[tiny_thinker],
        running_models={"tiny-thinker:1.5b"},
        default_response="Instant under pressure OK.",
    )
    core, settings = await _make_core(
        tmp_path,
        provider,
        default_model_id="tiny-thinker:1.5b",
        approved_models=("tiny-thinker:1.5b",),
        model_profiles={
            "tiny-thinker:1.5b": _general_only_profile(
                "tiny-thinker:1.5b",
                allow_thinking=True,
                max_pressure_level=4,
                memory_mb=1500,
            )
        },
    )
    try:
        # Put system at Level 4 critical memory pressure with tiny model still kept running in test state
        core.cms.current_pressure_level = 4
        core.cms.max_context_tokens = 4096

        # DEEP reasoning MUST reject fail-closed under Level 4 with zero inference
        t_deep = await core.create_task(
            TaskRequest(prompt="Solve this multi-step mathematical puzzle.")
        )
        ev_deep = [e async for e in core.execute_task_stream(t_deep)]
        assert t_deep.state == TaskState.FAILED
        assert ev_deep[-1]["event"] == "failed"
        assert all(c["method"] != "generate_stream_events" for c in provider.calls)
        _assert_lease_clean(core.cms)

        # INSTANT on explicitly Level-4-permitted running tiny model uses num_ctx=4096 cleanly
        t_inst = await core.create_task(TaskRequest(prompt="What is 5 + 7?"))
        ev_inst = [e async for e in core.execute_task_stream(t_inst)]
        assert t_inst.state == TaskState.COMPLETED
        assert ev_inst[-1]["event"] == "completed"
        gen_calls = [c for c in provider.calls if c["method"] == "generate_stream_events"]
        assert gen_calls[-1]["options"]["num_ctx"] == 4096
        _assert_lease_clean(core.cms)

        assert "MODEL_SELECTION_REJECTED" in settings.audit_log_path.read_text(encoding="utf-8")
        ok_chain, _, err = verify_audit_log_chain(settings.audit_log_path)
        assert ok_chain, err
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_post_lease_identity_mismatch_with_suitable_model_rejects_original_plan(
    tmp_path: Path,
) -> None:
    """Different but independently suitable active model never runs under stale Model A plan."""
    # Subcase 1: Concurrent switch from Model A to independently suitable Model B before lease.
    # Attempt 1 rejects Model B under Model A's plan, records failure, releases lease, and re-plans
    # on Attempt 2 to execute cleanly under Model B's own plan.
    provider1 = FakeProvider(
        models=[_general_only_meta("model-a:8b"), _general_only_meta("model-b:8b")],
        running_models={"model-a:8b"},
        responses_by_model={
            "model-a:8b": "Model A reply.",
            "model-b:8b": "Model B reply.",
        },
    )
    core1, settings1 = await _make_core(
        tmp_path / "replan",
        provider1,
        default_model_id="model-a:8b",
        approved_models=("model-a:8b", "model-b:8b"),
        model_profiles={
            "model-a:8b": _general_only_profile("model-a:8b"),
            "model-b:8b": _general_only_profile("model-b:8b"),
        },
    )
    try:
        orig_acquire1 = core1.cms.acquire_inference_lease
        orig_release1 = core1.cms.release_inference_lease
        orig_select1 = core1.cms.select_model_for_task
        orig_gen1 = provider1.generate_stream_events

        planned_by_attempt: list[tuple[str, set[str]]] = []
        leases_seen_at_inference: list[set[str]] = []
        acquire_calls1 = 0
        release_calls1 = 0

        async def spied_select1(*args: Any, **kwargs: Any) -> ModelMetadata:
            selected = await orig_select1(*args, **kwargs)
            planned_by_attempt.append((selected.id, set(core1.cms._active_leases)))
            return selected

        async def switch_to_suitable_b_before_lease(task_id: str, timeout: float = 60.0) -> None:
            nonlocal acquire_calls1
            acquire_calls1 += 1
            if acquire_calls1 == 1:
                assert await core1.cms.switch_model(
                    ModelSwitchRequest(
                        target_model_id="model-b:8b",
                        reason="concurrent task activated another suitable model",
                    )
                )
            await orig_acquire1(task_id, timeout=timeout)

        async def spied_release1(task_id: str) -> None:
            nonlocal release_calls1
            release_calls1 += 1
            await orig_release1(task_id)

        async def spied_gen1(*args: Any, **kwargs: Any):
            leases_seen_at_inference.append(set(core1.cms._active_leases))
            async for evt in orig_gen1(*args, **kwargs):
                yield evt

        core1.cms.select_model_for_task = spied_select1  # type: ignore[method-assign]
        core1.cms.acquire_inference_lease = switch_to_suitable_b_before_lease  # type: ignore[method-assign]
        core1.cms.release_inference_lease = spied_release1  # type: ignore[method-assign]
        provider1.generate_stream_events = spied_gen1  # type: ignore[method-assign]

        t1 = await core1.create_task(TaskRequest(prompt="What is the capital of France?"))
        ev1 = [e async for e in core1.execute_task_stream(t1)]

        assert t1.state == TaskState.COMPLETED
        assert t1.model_used == "model-b:8b"
        assert t1.result == "Model B reply."
        assert ev1[-1]["event"] == "completed"
        assert acquire_calls1 == 2
        assert release_calls1 == 2
        assert planned_by_attempt == [("model-a:8b", set()), ("model-b:8b", set())]
        assert leases_seen_at_inference == [{t1.task_id}]

        gen_calls1 = [c for c in provider1.calls if c["method"] == "generate_stream_events"]
        assert len(gen_calls1) == 1
        assert gen_calls1[0]["model_id"] == "model-b:8b"
        _assert_lease_clean(core1.cms)

        audit_entries1 = [
            json.loads(line)
            for line in settings1.audit_log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        fail_idx = next(
            i
            for i, e in enumerate(audit_entries1)
            if e["event_type"] == "MODEL_POST_LEASE_VERIFICATION_FAILED"
        )
        ver_idx = next(
            i
            for i, e in enumerate(audit_entries1)
            if e["event_type"] == "MODEL_POST_LEASE_VERIFIED"
        )
        assert fail_idx < ver_idx
        fail_details = audit_entries1[fail_idx]["details"]
        assert fail_details["planned_model_id"] == "model-a:8b"
        assert fail_details["resolved_model_id"] == "model-b:8b"
        assert fail_details["active_model_id"] == "model-b:8b"
        assert fail_details["attempt"] == 1
        assert fail_details["will_retry"] is True
        assert audit_entries1[ver_idx]["details"]["planned_model_id"] == "model-b:8b"
        assert audit_entries1[ver_idx]["details"]["model_id"] == "model-b:8b"
        assert audit_entries1[ver_idx]["details"]["matched_planned_model"] is True
        ok_chain, _, err = verify_audit_log_chain(settings1.audit_log_path)
        assert ok_chain, err
    finally:
        await core1.shutdown()

    # Subcase 2: Persistent concurrent swap between two suitable models hits retry bound cleanly
    provider2 = FakeProvider(
        models=[_general_only_meta("model-a:8b"), _general_only_meta("model-b:8b")],
        running_models={"model-a:8b"},
        default_response="Should never run.",
    )
    core2, settings2 = await _make_core(
        tmp_path / "bounded",
        provider2,
        default_model_id="model-a:8b",
        approved_models=("model-a:8b", "model-b:8b"),
        model_profiles={
            "model-a:8b": _general_only_profile("model-a:8b"),
            "model-b:8b": _general_only_profile("model-b:8b"),
        },
    )
    try:
        orig_acquire2 = core2.cms.acquire_inference_lease
        swap_count2 = 0

        async def always_swap_suitable_before_lease(task_id: str, timeout: float = 60.0) -> None:
            nonlocal swap_count2
            swap_count2 += 1
            active_now = await core2.cms.get_active_model()
            target = (
                "model-b:8b" if (active_now and active_now.id == "model-a:8b") else "model-a:8b"
            )
            assert await core2.cms.switch_model(
                ModelSwitchRequest(target_model_id=target, reason="persistent suitable contention")
            )
            await orig_acquire2(task_id, timeout=timeout)

        core2.cms.acquire_inference_lease = always_swap_suitable_before_lease  # type: ignore[method-assign]
        t2 = await core2.create_task(TaskRequest(prompt="What is the capital of Italy?"))
        ev2 = [e async for e in core2.execute_task_stream(t2)]

        assert t2.state == TaskState.FAILED
        assert t2.model_used is None
        assert ev2[-1]["event"] == "failed"
        assert swap_count2 == MAX_MODEL_ROUTING_ATTEMPTS == 2
        assert all(c["method"] != "generate_stream_events" for c in provider2.calls)
        _assert_lease_clean(core2.cms)

        audit2 = settings2.audit_log_path.read_text(encoding="utf-8")
        assert "MODEL_POST_LEASE_VERIFICATION_FAILED" in audit2
        assert "MODEL_INFERENCE_REJECTED" in audit2
        assert "MODEL_POST_LEASE_VERIFIED" not in audit2
        ok_chain, _, err = verify_audit_log_chain(settings2.audit_log_path)
        assert ok_chain, err
    finally:
        await core2.shutdown()

    # Subcase 3: Non-concurrent identity mismatch (CMS active model stays Model A while resolver returns Model B)
    # must fail closed immediately on Attempt 1 with zero retries and zero inference.
    provider3 = FakeProvider(
        models=[_general_only_meta("model-a:8b"), _general_only_meta("model-b:8b")],
        running_models={"model-a:8b", "model-b:8b"},
        default_response="Should never run.",
    )
    core3, settings3 = await _make_core(
        tmp_path / "fail_closed",
        provider3,
        default_model_id="model-a:8b",
        approved_models=("model-a:8b", "model-b:8b"),
        model_profiles={
            "model-a:8b": _general_only_profile("model-a:8b"),
            "model-b:8b": _general_only_profile("model-b:8b"),
        },
    )
    try:
        select_calls3 = 0
        orig_select3 = core3.cms.select_model_for_task

        async def spied_select3(*args: Any, **kwargs: Any) -> ModelMetadata:
            nonlocal select_calls3
            select_calls3 += 1
            return await orig_select3(*args, **kwargs)

        core3.cms.select_model_for_task = spied_select3  # type: ignore[method-assign]
        core3.cms.resolve_model_for_leased_task = (  # type: ignore[method-assign]
            lambda *_a, **_kw: core3.cms._registry["model-b:8b"]
        )

        t3 = await core3.create_task(TaskRequest(prompt="What is the capital of Spain?"))
        ev3 = [e async for e in core3.execute_task_stream(t3)]

        assert t3.state == TaskState.FAILED
        assert t3.model_used is None
        assert ev3[-1]["event"] == "failed"
        assert select_calls3 == 1
        assert all(c["method"] != "generate_stream_events" for c in provider3.calls)
        _assert_lease_clean(core3.cms)

        audit3 = settings3.audit_log_path.read_text(encoding="utf-8")
        assert "MODEL_POST_LEASE_VERIFICATION_FAILED" in audit3
        assert "MODEL_INFERENCE_REJECTED" in audit3
    finally:
        await core3.shutdown()


@pytest.mark.asyncio
async def test_actual_asyncio_task_cancellation_during_model_switch(tmp_path: Path) -> None:
    """Cancelling the asyncio task mid-switch propagates CancelledError, clears barriers/leases, and recovers."""
    provider = FakeProvider(
        models=[_general_only_meta("general-only:8b"), _coder_meta()],
        running_models={"general-only:8b"},
        default_response="Safe recovery response.",
    )
    core, settings = await _make_core(
        tmp_path,
        provider,
        default_model_id="general-only:8b",
        approved_models=("general-only:8b", "qwen-coder:7b"),
        model_profiles={
            "general-only:8b": _general_only_profile("general-only:8b"),
            "qwen-coder:7b": _coder_profile(),
        },
    )
    try:
        load_blocked = asyncio.Event()
        release_load = asyncio.Event()
        orig_load = provider.load_model

        async def blocking_coder_load(model_id: str) -> bool:
            if model_id == "qwen-coder:7b":
                load_blocked.set()
                await release_load.wait()
            return await orig_load(model_id)

        provider.load_model = blocking_coder_load  # type: ignore[method-assign]

        # 1. Start a CODE task that requires switching from general-only:8b -> qwen-coder:7b
        t_switch = await core.create_task(
            TaskRequest(prompt="Write a Python function to invert a dict")
        )

        async def _run_task_stream() -> list[dict[str, Any]]:
            return [e async for e in core.execute_task_stream(t_switch)]

        runner = asyncio.create_task(_run_task_stream())
        await asyncio.wait_for(load_blocked.wait(), timeout=2.0)

        # Verify switch barrier is actively held before cancellation
        assert core.cms._switch_pending is True
        assert core.cms._switch_clear_event.is_set() is False
        assert core.cms._switch_lock.locked() is True
        assert core.cms._active_leases == set()

        # 2. Cancel the actual asyncio task (not core.cancel_task()) and verify CancelledError propagates
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(runner, timeout=2.0)

        # 3. Verify switch barrier is cleared, no leases remain held, and zero inference ran
        assert t_switch.state == TaskState.CANCELLED
        assert core.cms._switch_lock.locked() is False
        _assert_lease_clean(core.cms)
        assert all(c["method"] != "generate_stream_events" for c in provider.calls)

        # 4. Subsequent safe request recovers cleanly, acquires lease, and runs
        provider.load_model = orig_load  # type: ignore[method-assign]
        t_next = await core.create_task(TaskRequest(prompt="What is 2 + 2?"))
        ev_next = [e async for e in core.execute_task_stream(t_next)]
        assert t_next.state == TaskState.COMPLETED
        assert t_next.model_used == "general-only:8b"
        assert ev_next[-1]["event"] == "completed"
        _assert_lease_clean(core.cms)

        ok_chain, count, err = verify_audit_log_chain(settings.audit_log_path)
        assert ok_chain and count > 0, err
    finally:
        await core.shutdown()
