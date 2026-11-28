"""Dedicated Stage 4.4-B tests for CMS curated profiles, routing policy, memory integration, and post-lease checks."""

import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from carbon.config.settings import DEFAULT_APPROVED_MODELS, DEFAULT_PRIMARY_MODEL, Settings
from carbon.core.orchestrator import CarbonCore
from carbon.governance.governance_engine import GovernanceEngine, verify_audit_log_chain
from carbon.interfaces.models import (
    ApprovedModelProfile,
    ModelCapability,
    ModelIncompatibleError,
    ModelMetadata,
    ModelNotApprovedError,
    ModelSwitchFailureReason,
    ModelSwitchRequest,
    ModelUnavailableError,
    ReasoningDepth,
    ResourcePolicyError,
    TaskCategory,
    TaskRequest,
    TaskState,
)
from carbon.interfaces.protocols import ICarbonModelSupervisor
from carbon.model_control.cms import DEFAULT_MODEL_MEMORY_BUDGET_MB, CarbonModelSupervisor
from carbon.model_control.fake_provider import FakeProvider


def _qwen_tags_meta(*, include_vision: bool = True, include_thinking: bool = True) -> ModelMetadata:
    caps = [ModelCapability.COMPLETION, ModelCapability.CHAT, ModelCapability.TOOLS]
    if include_thinking:
        caps.append(ModelCapability.THINKING)
    if include_vision:
        caps.append(ModelCapability.VISION)
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
        capabilities=caps,
        is_healthy=True,
    )


def _coder_meta(model_id: str = "qwen-coder:7b", memory_mb: int = 4700) -> ModelMetadata:
    return ModelMetadata(
        id=model_id,
        name="Qwen Coder 7B",
        provider="ollama",
        family="qwen_coder",
        parameter_size="7.0B",
        quantization="Q4_K_M",
        context_length=16384,
        max_architectural_context=131072,
        memory_footprint_mb=memory_mb,
        capabilities=[
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
        ],
        is_healthy=True,
    )


def _coder_profile(model_id: str = "qwen-coder:7b", memory_mb: int = 4700) -> ApprovedModelProfile:
    return ApprovedModelProfile(
        model_id=model_id,
        supported_capabilities=(
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
        ),
        suitable_task_categories=(TaskCategory.CODE,),
        preferred_task_categories=(TaskCategory.CODE,),
        estimated_memory_mb=memory_mb,
        allow_under_memory_pressure=True,
        max_pressure_level=2,
        max_recommended_context_tokens=32768,
    )


def _general_only_meta(
    model_id: str = "general-only:8b",
    *,
    tags_capabilities: list[ModelCapability] | None = None,
    memory_mb: int = 5100,
) -> ModelMetadata:
    return ModelMetadata(
        id=model_id,
        name=model_id,
        provider="ollama",
        family="general",
        memory_footprint_mb=memory_mb,
        capabilities=tags_capabilities or [ModelCapability.CHAT, ModelCapability.COMPLETION],
        is_healthy=True,
    )


def _general_only_profile(
    model_id: str = "general-only:8b",
    *,
    allow_thinking: bool = False,
    allow_pressure: bool = True,
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
        allow_under_memory_pressure=allow_pressure,
        max_pressure_level=max_pressure_level,
    )


@pytest.mark.asyncio
async def test_curated_profile_validation_and_authoritative_tags_override() -> None:
    """CMS curated profiles validate inputs strictly and override untrusted /api/tags capabilities."""
    assert DEFAULT_PRIMARY_MODEL == "qwen3.5:9b"
    assert DEFAULT_APPROVED_MODELS == ("qwen3.5:9b",)
    assert DEFAULT_MODEL_MEMORY_BUDGET_MB == 9216

    for bad_int in (True, False, -1):
        with pytest.raises(ValidationError):
            ApprovedModelProfile(model_id="m", estimated_memory_mb=bad_int)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            ApprovedModelProfile(model_id="m", max_pressure_level=bad_int)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ApprovedModelProfile(model_id="m", max_recommended_context_tokens=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ApprovedModelProfile(
            model_id="m",
            preferred_task_categories=(TaskCategory.CODE,),
            suitable_task_categories=(TaskCategory.GENERAL,),
        )
    with pytest.raises(ValidationError):
        ApprovedModelProfile(
            model_id="m",
            supported_capabilities=(ModelCapability.CHAT,),
            suitable_task_categories=(TaskCategory.VISION,),
        )
    with pytest.raises(ValidationError):
        ApprovedModelProfile(
            model_id="m",
            supported_capabilities=(ModelCapability.CHAT,),
            suitable_task_categories=(TaskCategory.DEEP_REASONING,),
        )

    # Even if /api/tags claims VISION and omits THINKING on qwen3.5:9b, CMS policy enforces curated capabilities
    provider = FakeProvider(
        models=[_qwen_tags_meta(include_vision=True, include_thinking=False)],
        running_models={"qwen3.5:9b"},
    )
    cms = CarbonModelSupervisor(
        provider=provider,
        # Extra unapproved profile entry must never approve gemma4:12b
        model_profiles={
            "gemma4:12b": _general_only_profile("gemma4:12b", allow_thinking=True),
        },
    )
    assert isinstance(cms, ICarbonModelSupervisor)
    await cms.initialize()

    assert cms.is_model_approved("gemma4:12b") is False
    with pytest.raises(ModelNotApprovedError):
        cms.get_model_profile("gemma4:12b")

    qwen_profile = cms.get_model_profile("qwen3.5:9b")
    assert ModelCapability.THINKING in qwen_profile.supported_capabilities
    assert ModelCapability.VISION not in qwen_profile.supported_capabilities
    assert TaskCategory.CODE in qwen_profile.suitable_task_categories

    approved = await cms.list_approved_models()
    assert len(approved) == 1
    assert approved[0].capabilities == list(qwen_profile.supported_capabilities)
    assert ModelCapability.VISION not in approved[0].capabilities
    assert ModelCapability.THINKING in approved[0].capabilities


@pytest.mark.asyncio
async def test_active_stickiness_for_general_and_code_when_explicitly_supported() -> None:
    """Active approved model stays selected without switch for GENERAL and explicitly suitable CODE."""
    # Test 1: GENERAL sticks to active approved model rather than switching to idle peer
    fast_meta = _general_only_meta("qwen-fast:3b", memory_mb=2200)
    fast_profile = _general_only_profile("qwen-fast:3b", memory_mb=2200)
    provider = FakeProvider(
        models=[_qwen_tags_meta(), fast_meta],
        running_models={"qwen-fast:3b"},
    )
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=("qwen3.5:9b", "qwen-fast:3b"),
        model_profiles={"qwen-fast:3b": fast_profile},
    )
    await cms.initialize()
    assert await cms.switch_model(
        ModelSwitchRequest(target_model_id="qwen-fast:3b", reason="activate fast")
    )

    decision_gen = await cms.plan_model_for_task(
        [ModelCapability.CHAT],
        ReasoningDepth.INSTANT,
        task_category=TaskCategory.GENERAL,
    )
    assert decision_gen.selected_model_id == "qwen-fast:3b"
    assert decision_gen.requires_switch is False

    # Test 2: Single-model production qwen3.5:9b explicitly allows CODE -> selected cleanly
    single_provider = FakeProvider(models=[_qwen_tags_meta()], running_models={"qwen3.5:9b"})
    single_cms = CarbonModelSupervisor(provider=single_provider)
    await single_cms.initialize()
    decision_qwen_code = await single_cms.plan_model_for_task(
        [ModelCapability.CHAT],
        ReasoningDepth.STANDARD,
        task_category=TaskCategory.CODE,
    )
    assert decision_qwen_code.selected_model_id == "qwen3.5:9b"
    assert decision_qwen_code.requires_switch is False

    # Active dedicated coder also stays selected cleanly without switch
    coder_provider = FakeProvider(
        models=[_qwen_tags_meta(), _coder_meta()],
        running_models={"qwen3.5:9b"},
    )
    coder_cms = CarbonModelSupervisor(
        provider=coder_provider,
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
        model_profiles={"qwen-coder:7b": _coder_profile()},
    )
    await coder_cms.initialize()
    assert await coder_cms.switch_model(
        ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="warm coder")
    )
    decision_coder_active = await coder_cms.plan_model_for_task(
        [ModelCapability.CHAT],
        ReasoningDepth.STANDARD,
        task_category=TaskCategory.CODE,
    )
    assert decision_coder_active.selected_model_id == "qwen-coder:7b"
    assert decision_coder_active.requires_switch is False


@pytest.mark.asyncio
async def test_active_code_suitable_model_stays_selected_over_preferred_coder() -> None:
    """Active model explicitly suitable for CODE stays selected even when another model prefers CODE."""
    provider = FakeProvider(
        models=[_qwen_tags_meta(), _coder_meta()],
        running_models={"qwen3.5:9b"},
    )
    cms = CarbonModelSupervisor(
        provider=provider,
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
        model_profiles={"qwen-coder:7b": _coder_profile()},
    )
    await cms.initialize()
    assert (await cms.get_active_model()).id == "qwen3.5:9b"  # type: ignore[union-attr]

    # 1. STANDARD CODE: active qwen3.5:9b already explicitly supports CODE -> retained (no switch)
    std_code = await cms.plan_model_for_task(
        [ModelCapability.CHAT],
        ReasoningDepth.STANDARD,
        task_category=TaskCategory.CODE,
    )
    assert std_code.selected_model_id == "qwen3.5:9b"
    assert std_code.requires_switch is False
    assert std_code.reason == "active_compatible_model_retained"
    selected_meta = await cms.select_model_for_task(
        [ModelCapability.CHAT],
        ReasoningDepth.STANDARD,
        task_category=TaskCategory.CODE,
    )
    assert selected_meta.id == "qwen3.5:9b"

    # 2. DEEP CODE: active qwen3.5:9b supports THINKING + CODE -> retained (no switch)
    deep_code = await cms.plan_model_for_task(
        [ModelCapability.CHAT, ModelCapability.THINKING],
        ReasoningDepth.DEEP,
        task_category=TaskCategory.CODE,
    )
    assert deep_code.selected_model_id == "qwen3.5:9b"
    assert deep_code.requires_switch is False

    # 3. GENERAL remains sticky on active qwen3.5:9b
    gen_decision = await cms.plan_model_for_task(
        [ModelCapability.CHAT],
        ReasoningDepth.STANDARD,
        task_category=TaskCategory.GENERAL,
    )
    assert gen_decision.selected_model_id == "qwen3.5:9b"
    assert gen_decision.requires_switch is False


@pytest.mark.asyncio
async def test_unsuitable_active_model_selects_preferred_alternative_specialist() -> None:
    """Only when active model is unsuitable does CMS rank alternatives and prefer category specialists."""
    general_only = _general_only_meta("general-only:8b", memory_mb=5000)
    provider = FakeProvider(
        models=[general_only, _qwen_tags_meta(), _coder_meta()],
        running_models={"general-only:8b"},
    )
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="general-only:8b",
        approved_models=("general-only:8b", "qwen3.5:9b", "qwen-coder:7b"),
        model_profiles={
            "general-only:8b": _general_only_profile("general-only:8b", allow_thinking=False),
            # qwen3.5:9b appears BEFORE qwen-coder:7b and is suitable (not preferred) for CODE
            "qwen-coder:7b": _coder_profile(),
        },
    )
    await cms.initialize()
    assert (await cms.get_active_model()).id == "general-only:8b"  # type: ignore[union-attr]

    # 1. GENERAL sticks with active general-only:8b (no switch)
    gen = await cms.plan_model_for_task(
        [ModelCapability.CHAT],
        ReasoningDepth.STANDARD,
        task_category=TaskCategory.GENERAL,
    )
    assert gen.selected_model_id == "general-only:8b"
    assert gen.requires_switch is False

    # 2. STANDARD CODE: active general-only:8b lacks CODE suitability -> ranks alternatives and
    # picks dedicated specialist qwen-coder:7b over suitable-only qwen3.5:9b!
    std_code = await cms.plan_model_for_task(
        [ModelCapability.CHAT],
        ReasoningDepth.STANDARD,
        task_category=TaskCategory.CODE,
    )
    assert std_code.selected_model_id == "qwen-coder:7b"
    assert std_code.requires_switch is True
    assert std_code.reason == "preferred_specialist_selected"
    assert "code" in std_code.rejected_candidates["general-only:8b"].lower()

    # 3. DEEP CODE: active general-only:8b lacks CODE & THINKING, qwen-coder:7b lacks THINKING ->
    # picks eligible alternative qwen3.5:9b!
    deep_code = await cms.plan_model_for_task(
        [ModelCapability.CHAT, ModelCapability.THINKING],
        ReasoningDepth.DEEP,
        task_category=TaskCategory.CODE,
    )
    assert deep_code.selected_model_id == "qwen3.5:9b"
    assert deep_code.requires_switch is True
    assert deep_code.reason == "eligible_alternative_selected"
    assert "thinking" in deep_code.rejected_candidates["qwen-coder:7b"].lower()

    # 4. When qwen-coder:7b is active, GENERAL sees it lacks GENERAL suitability and selects
    # a GENERAL-preferred alternative deterministically (general-only:8b precedes qwen3.5:9b).
    assert await cms.switch_model(
        ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="activate coder")
    )
    fallback_gen = await cms.plan_model_for_task(
        [ModelCapability.CHAT],
        ReasoningDepth.STANDARD,
        task_category=TaskCategory.GENERAL,
    )
    assert fallback_gen.selected_model_id == "general-only:8b"
    assert fallback_gen.requires_switch is True
    assert fallback_gen.reason == "preferred_specialist_selected"

    # 5. With active coder lacking THINKING, DEEP CODE switches away to qwen3.5:9b cleanly
    switch_for_deep_code = await cms.plan_model_for_task(
        [ModelCapability.CHAT, ModelCapability.THINKING],
        ReasoningDepth.DEEP,
        task_category=TaskCategory.CODE,
    )
    assert switch_for_deep_code.selected_model_id == "qwen3.5:9b"
    assert switch_for_deep_code.requires_switch is True


@pytest.mark.asyncio
async def test_general_model_blocked_for_code_unless_profile_allows_and_audits(
    tmp_path: Path,
) -> None:
    """General model without CODE in curated profile cannot run CODE pre- or post-lease."""
    audit_path = tmp_path / "audit.log"
    gov = GovernanceEngine(audit_log_path=audit_path, enable_audit=True)
    await gov.initialize()

    provider = FakeProvider(
        models=[_general_only_meta("general-only:8b")],
        running_models={"general-only:8b"},
    )
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="general-only:8b",
        approved_models=("general-only:8b",),
        model_profiles={"general-only:8b": _general_only_profile("general-only:8b")},
        governance=gov,
    )
    await cms.initialize()

    # Allowed for GENERAL
    ok_gen = await cms.select_model_for_task(
        [ModelCapability.CHAT],
        ReasoningDepth.STANDARD,
        task_category=TaskCategory.GENERAL,
    )
    assert ok_gen.id == "general-only:8b"

    # Rejected for CODE pre-lease
    with pytest.raises(ModelIncompatibleError) as exc_info:
        await cms.plan_model_for_task(
            [ModelCapability.CHAT],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.CODE,
        )
    assert exc_info.value.reason == ModelSwitchFailureReason.INCOMPATIBLE_CAPABILITY
    assert cms.last_selection_failure == ModelSwitchFailureReason.INCOMPATIBLE_CAPABILITY

    # Rejected post-lease too (never silent fallback)
    with pytest.raises(ModelIncompatibleError):
        cms.resolve_model_for_leased_task(
            [ModelCapability.CHAT],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.CODE,
        )

    ok_chain, count, err = verify_audit_log_chain(audit_path)
    assert ok_chain and count >= 1, err
    assert "MODEL_SELECTION_REJECTED" in audit_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_deep_without_thinking_and_vision_without_vision_reject(tmp_path: Path) -> None:
    """DEEP rejects non-THINKING models (even if /api/tags lies) and VISION rejects without VISION."""
    audit_path = tmp_path / "audit.log"
    gov = GovernanceEngine(audit_log_path=audit_path, enable_audit=True)
    await gov.initialize()

    # /api/tags falsely advertises THINKING and VISION on chat-basic:3b, while CMS profile only grants CHAT
    lying_meta = _general_only_meta(
        "chat-basic:3b",
        tags_capabilities=[
            ModelCapability.CHAT,
            ModelCapability.THINKING,
            ModelCapability.VISION,
        ],
    )
    provider = FakeProvider(models=[lying_meta], running_models={"chat-basic:3b"})
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="chat-basic:3b",
        approved_models=("chat-basic:3b",),
        model_profiles={
            "chat-basic:3b": _general_only_profile("chat-basic:3b", allow_thinking=False)
        },
        governance=gov,
    )
    await cms.initialize()

    # Test 5: DEEP rejected both pre-lease and post-lease even when capabilities_required=[CHAT]
    with pytest.raises(ModelIncompatibleError):
        await cms.select_model_for_task(
            [ModelCapability.CHAT],
            ReasoningDepth.DEEP,
            task_category=TaskCategory.GENERAL,
        )
    with pytest.raises(ModelIncompatibleError):
        await cms.plan_model_for_task(
            [ModelCapability.CHAT],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.DEEP_REASONING,
        )
    with pytest.raises(ModelIncompatibleError):
        cms.resolve_model_for_leased_task(
            [ModelCapability.CHAT],
            ReasoningDepth.DEEP,
            task_category=TaskCategory.GENERAL,
        )

    # Test 6: VISION rejected in both minimal and default production qwen3.5:9b setups
    with pytest.raises(ModelIncompatibleError):
        await cms.plan_model_for_task(
            [ModelCapability.CHAT, ModelCapability.VISION],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.VISION,
        )
    with pytest.raises(ModelIncompatibleError):
        cms.resolve_model_for_leased_task(
            [ModelCapability.CHAT],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.VISION,
        )

    qwen_provider = FakeProvider(
        models=[_qwen_tags_meta(include_vision=True)],
        running_models={"qwen3.5:9b"},
    )
    qwen_cms = CarbonModelSupervisor(provider=qwen_provider, governance=gov)
    await qwen_cms.initialize()
    with pytest.raises(ModelIncompatibleError):
        await qwen_cms.select_model_for_task(
            [ModelCapability.CHAT, ModelCapability.VISION],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.VISION,
        )
    with pytest.raises(ModelIncompatibleError):
        qwen_cms.resolve_model_for_leased_task(
            [ModelCapability.CHAT, ModelCapability.VISION],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.VISION,
        )

    ok_chain, count, err = verify_audit_log_chain(audit_path)
    assert ok_chain and count >= 4, err


@pytest.mark.asyncio
async def test_unapproved_installed_or_running_and_missing_unhealthy_specialists() -> None:
    """Unapproved installed/running models and missing/unhealthy specialists never select or fall back."""
    unapproved_coder = ModelMetadata(
        id="deepseek-coder:6.7b",
        name="DeepSeek Coder",
        capabilities=[
            ModelCapability.CHAT,
            ModelCapability.THINKING,
            ModelCapability.VISION,
        ],
    )
    missing_coder_id = "missing-coder:7b"
    unhealthy_coder = _coder_meta("unhealthy-coder:7b")

    provider = FakeProvider(
        models=[_general_only_meta("general-only:8b"), unhealthy_coder, unapproved_coder],
        running_models={"general-only:8b", "deepseek-coder:6.7b"},
        unhealthy_models={"unhealthy-coder:7b"},
    )
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="general-only:8b",
        approved_models=("general-only:8b", missing_coder_id, "unhealthy-coder:7b"),
        model_profiles={
            "general-only:8b": _general_only_profile("general-only:8b", allow_thinking=False),
            missing_coder_id: _coder_profile(missing_coder_id),
            "unhealthy-coder:7b": _coder_profile("unhealthy-coder:7b"),
        },
    )
    await cms.initialize()

    # Installed list visibly tracks deepseek-coder:6.7b as running but NOT approved
    installed_map = {m.id: m for m in await cms.list_installed_models()}
    assert installed_map["deepseek-coder:6.7b"].is_approved is False
    assert installed_map["deepseek-coder:6.7b"].is_running is True

    # Test 7, 8, 9, 10: Neither installed nor running unapproved coder can satisfy CODE/DEEP/VISION;
    # missing/unhealthy approved specialists do NOT fall back to general-only:8b!
    with pytest.raises(ModelIncompatibleError) as code_err:
        await cms.plan_model_for_task(
            [ModelCapability.CHAT],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.CODE,
        )
    msg = str(code_err.value).lower()
    assert missing_coder_id in msg and "unhealthy-coder:7b" in msg and "general-only:8b" in msg

    with pytest.raises(ModelIncompatibleError):
        await cms.select_model_for_task(
            [ModelCapability.CHAT],
            ReasoningDepth.DEEP,
            task_category=TaskCategory.GENERAL,
        )
    with pytest.raises(ModelIncompatibleError):
        await cms.select_model_for_task(
            [ModelCapability.CHAT],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.VISION,
        )

    # Even if only approved specialist is unhealthy, selection fails clearly with UNAVAILABLE
    only_bad_provider = FakeProvider(
        models=[unhealthy_coder, unapproved_coder],
        unhealthy_models={"unhealthy-coder:7b"},
        running_models={"deepseek-coder:6.7b"},
    )
    only_bad_cms = CarbonModelSupervisor(
        provider=only_bad_provider,
        default_model_id="unhealthy-coder:7b",
        approved_models=("unhealthy-coder:7b",),
        model_profiles={"unhealthy-coder:7b": _coder_profile("unhealthy-coder:7b")},
    )
    await only_bad_cms.initialize()
    with pytest.raises(ModelUnavailableError) as unavail_err:
        await only_bad_cms.select_model_for_task(
            [ModelCapability.CHAT],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.CODE,
        )
    assert unavail_err.value.reason == ModelSwitchFailureReason.UNAVAILABLE

    # If corrupted/spoofed active pointer ever held unapproved model, post-lease fails with ModelNotApprovedError
    cms._active_model = unapproved_coder.model_copy(update={"is_active": True, "is_running": True})
    with pytest.raises(ModelNotApprovedError):
        cms.resolve_model_for_leased_task(
            [ModelCapability.CHAT],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.GENERAL,
        )


@pytest.mark.asyncio
async def test_memory_budget_and_level4_critical_memory_pressure_policies(
    tmp_path: Path,
) -> None:
    """Memory budget and pressure Levels 2/4 govern selection and block DEEP at Level 4."""
    audit_path = tmp_path / "audit.log"
    gov = GovernanceEngine(audit_log_path=audit_path, enable_audit=True)
    await gov.initialize()

    tiny_thinker = _general_only_meta("tiny-thinker:1.5b", memory_mb=1500)
    heavy_thinker = _general_only_meta("heavy-thinker:32b", memory_mb=18500)
    pressure_shy = _general_only_meta("pressure-shy:9b", memory_mb=7000)

    provider = FakeProvider(
        models=[tiny_thinker, heavy_thinker, pressure_shy],
        running_models={"tiny-thinker:1.5b"},
    )
    cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="tiny-thinker:1.5b",
        approved_models=("tiny-thinker:1.5b", "heavy-thinker:32b", "pressure-shy:9b"),
        model_profiles={
            "tiny-thinker:1.5b": ApprovedModelProfile(
                model_id="tiny-thinker:1.5b",
                supported_capabilities=(ModelCapability.CHAT, ModelCapability.THINKING),
                suitable_task_categories=(TaskCategory.GENERAL, TaskCategory.DEEP_REASONING),
                preferred_task_categories=(TaskCategory.GENERAL,),
                estimated_memory_mb=1500,
                allow_under_memory_pressure=True,
                max_pressure_level=4,
            ),
            "heavy-thinker:32b": ApprovedModelProfile(
                model_id="heavy-thinker:32b",
                supported_capabilities=(ModelCapability.CHAT, ModelCapability.THINKING),
                suitable_task_categories=(TaskCategory.GENERAL, TaskCategory.DEEP_REASONING),
                preferred_task_categories=(TaskCategory.DEEP_REASONING,),
                estimated_memory_mb=18500,
                allow_under_memory_pressure=True,
                max_pressure_level=2,
            ),
            "pressure-shy:9b": _general_only_profile(
                "pressure-shy:9b",
                allow_thinking=True,
                allow_pressure=False,
                max_pressure_level=1,
                memory_mb=7000,
            ),
        },
        memory_budget_mb=DEFAULT_MODEL_MEMORY_BUDGET_MB,
        governance=gov,
    )
    await cms.initialize()

    # Budget blocks heavy-thinker:32b even if tiny-thinker wasn't installed
    cms_heavy_only = CarbonModelSupervisor(
        provider=FakeProvider(models=[heavy_thinker], running_models={"heavy-thinker:32b"}),
        default_model_id="heavy-thinker:32b",
        approved_models=("heavy-thinker:32b",),
        model_profiles={"heavy-thinker:32b": cms.get_model_profile("heavy-thinker:32b")},
        governance=gov,
    )
    await cms_heavy_only.initialize()
    with pytest.raises(ResourcePolicyError) as budget_err:
        await cms_heavy_only.plan_model_for_task(
            [ModelCapability.CHAT],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.GENERAL,
        )
    assert budget_err.value.reason == ModelSwitchFailureReason.RESOURCE_BUDGET_EXCEEDED
    with pytest.raises(ResourcePolicyError):
        cms_heavy_only.resolve_model_for_leased_task(
            [ModelCapability.CHAT],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.GENERAL,
        )

    # Activate pressure-shy:9b at Level 1, then trigger Level 2 warning pressure:
    # active pressure-shy:9b becomes ineligible under pressure, forcing fallback to tiny-thinker:1.5b
    assert await cms.switch_model(
        ModelSwitchRequest(target_model_id="pressure-shy:9b", reason="activate pressure-shy")
    )
    await cms.handle_memory_pressure(2)
    dec_l2 = await cms.plan_model_for_task(
        [ModelCapability.CHAT],
        ReasoningDepth.STANDARD,
        task_category=TaskCategory.GENERAL,
    )
    assert dec_l2.selected_model_id == "tiny-thinker:1.5b"
    assert dec_l2.requires_switch is True
    assert "pressure" in dec_l2.rejected_candidates["pressure-shy:9b"].lower()
    assert await cms.switch_model(
        ModelSwitchRequest(target_model_id="tiny-thinker:1.5b", reason="ensure tiny active")
    )

    # Critical Level 4 blocks DEEP reasoning EVEN when tiny-thinker:1.5b is running with max_pressure_level=4
    cms.current_pressure_level = 4
    with pytest.raises(ResourcePolicyError) as l4_deep_err:
        await cms.plan_model_for_task(
            [ModelCapability.CHAT, ModelCapability.THINKING],
            ReasoningDepth.DEEP,
            task_category=TaskCategory.GENERAL,
        )
    assert l4_deep_err.value.reason == ModelSwitchFailureReason.MEMORY_PRESSURE

    with pytest.raises(ResourcePolicyError) as l4_cat_err:
        await cms.select_model_for_task(
            [ModelCapability.CHAT],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.DEEP_REASONING,
        )
    assert l4_cat_err.value.reason == ModelSwitchFailureReason.MEMORY_PRESSURE

    with pytest.raises(ResourcePolicyError):
        cms.resolve_model_for_leased_task(
            [ModelCapability.CHAT],
            ReasoningDepth.DEEP,
            task_category=TaskCategory.GENERAL,
        )

    # Instant/General remains allowed ONLY if running active model explicitly allows Level 4
    assert (
        cms.resolve_model_for_leased_task(
            [ModelCapability.CHAT],
            ReasoningDepth.INSTANT,
            task_category=TaskCategory.GENERAL,
        ).id
        == "tiny-thinker:1.5b"
    )

    # Production qwen3.5:9b is disallowed at Level 4 (evicted and blocked even for INSTANT)
    qwen_provider = FakeProvider(models=[_qwen_tags_meta()], running_models={"qwen3.5:9b"})
    qwen_cms = CarbonModelSupervisor(provider=qwen_provider, governance=gov)
    await qwen_cms.initialize()
    await qwen_cms.handle_memory_pressure(4)
    with pytest.raises(ResourcePolicyError) as qwen_l4_err:
        await qwen_cms.select_model_for_task([ModelCapability.CHAT], ReasoningDepth.INSTANT)
    assert qwen_l4_err.value.reason == ModelSwitchFailureReason.MEMORY_PRESSURE
    with pytest.raises(ResourcePolicyError) as qwen_post_err:
        qwen_cms.resolve_model_for_leased_task([ModelCapability.CHAT], ReasoningDepth.INSTANT)
    assert qwen_post_err.value.reason == ModelSwitchFailureReason.MEMORY_PRESSURE

    ok_chain, count, err = verify_audit_log_chain(audit_path)
    assert ok_chain and count >= 4, err


@pytest.mark.asyncio
async def test_deterministic_selection_and_tie_breaking() -> None:
    """Selection returns identical decisions across repeated calls and reversed tag ordering."""
    general_only = _general_only_meta("general-only:8b")
    coder_a = _coder_meta("coder-a:7b")
    coder_b = _coder_meta("coder-b:7b")
    models_fwd = [general_only, coder_a, coder_b]
    models_rev = list(reversed(models_fwd))

    # 1. Deterministic alternative tie-breaking when active general-only:8b is unsuitable for CODE
    alt_results: list[tuple[str, bool, str]] = []
    for catalog in (models_fwd, models_rev):
        provider = FakeProvider(models=catalog, running_models={"general-only:8b"})
        cms = CarbonModelSupervisor(
            provider=provider,
            default_model_id="general-only:8b",
            approved_models=("general-only:8b", "coder-a:7b", "coder-b:7b"),
            model_profiles={
                "general-only:8b": _general_only_profile("general-only:8b"),
                "coder-a:7b": _coder_profile("coder-a:7b"),
                "coder-b:7b": _coder_profile("coder-b:7b"),
            },
        )
        await cms.initialize()
        for _ in range(3):
            dec = await cms.plan_model_for_task(
                [ModelCapability.CHAT],
                ReasoningDepth.STANDARD,
                task_category=TaskCategory.CODE,
            )
            alt_results.append((dec.selected_model_id, dec.requires_switch, dec.reason))

    assert len(set(alt_results)) == 1
    assert alt_results[0] == ("coder-a:7b", True, "preferred_specialist_selected")

    # 2. Deterministic active-model stickiness regardless of tag discovery order
    sticky_results: list[tuple[str, bool, str]] = []
    for catalog in ([_qwen_tags_meta(), coder_a], [coder_a, _qwen_tags_meta()]):
        provider = FakeProvider(models=catalog, running_models={"qwen3.5:9b"})
        cms = CarbonModelSupervisor(
            provider=provider,
            default_model_id="qwen3.5:9b",
            approved_models=("qwen3.5:9b", "coder-a:7b"),
            model_profiles={"coder-a:7b": _coder_profile("coder-a:7b")},
        )
        await cms.initialize()
        for _ in range(3):
            dec = await cms.plan_model_for_task(
                [ModelCapability.CHAT],
                ReasoningDepth.STANDARD,
                task_category=TaskCategory.CODE,
            )
            sticky_results.append((dec.selected_model_id, dec.requires_switch, dec.reason))

    assert len(set(sticky_results)) == 1
    assert sticky_results[0] == ("qwen3.5:9b", False, "active_compatible_model_retained")


@pytest.mark.asyncio
async def test_strict_synchronous_post_lease_verification_performs_no_switches_or_awaits() -> None:
    """resolve_model_for_leased_task is strictly synchronous and never mutates residency or awaits RPCs."""
    assert inspect.iscoroutinefunction(CarbonModelSupervisor.resolve_model_for_leased_task) is False

    provider = FakeProvider(
        models=[_qwen_tags_meta(), _coder_meta()],
        running_models={"qwen3.5:9b"},
    )
    cms = CarbonModelSupervisor(
        provider=provider,
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
        model_profiles={"qwen-coder:7b": _coder_profile()},
    )
    await cms.initialize()

    # Switch to qwen-coder:7b OUTSIDE lease, then test strict post-lease checks inside held lease
    assert await cms.switch_model(
        ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="activate coder")
    )
    provider.reset_history()

    await cms.acquire_inference_lease("lease-check-1")
    try:
        # Compatible request on active coder succeeds synchronously with zero provider calls
        resolved = cms.resolve_model_for_leased_task(
            [ModelCapability.CHAT],
            ReasoningDepth.STANDARD,
            task_category=TaskCategory.CODE,
        )
        assert resolved.id == "qwen-coder:7b"

        # Requesting GENERAL or DEEP while non-thinking CODE-only coder holds residency MUST fail
        with pytest.raises(ModelIncompatibleError):
            cms.resolve_model_for_leased_task(
                [ModelCapability.CHAT],
                ReasoningDepth.STANDARD,
                task_category=TaskCategory.GENERAL,
            )
        with pytest.raises(ModelIncompatibleError):
            cms.resolve_model_for_leased_task(
                [ModelCapability.CHAT, ModelCapability.THINKING],
                ReasoningDepth.DEEP,
                task_category=TaskCategory.CODE,
            )

        # Confirm zero load/unload/discovery RPCs were executed while holding lease
        assert provider.calls == []
        assert provider.loaded_models == []
        assert provider.unloaded_models == []
    finally:
        await cms.release_inference_lease("lease-check-1")


@pytest.mark.asyncio
async def test_production_single_model_qwen_works_and_fake_provider_helpers(
    tmp_path: Path,
) -> None:
    """Existing single-model qwen3.5:9b production config completes tasks and FakeProvider extensions work."""
    # Verify FakeProvider responses_by_model & enforce_installed_on_load helpers directly
    helper_provider = FakeProvider(
        models=[_qwen_tags_meta()],
        responses_by_model={"qwen3.5:9b": "Qwen specific output"},
        enforce_installed_on_load=True,
    )
    assert await helper_provider.load_model("not-installed:1b") is False
    assert await helper_provider.load_model("qwen3.5:9b") is True

    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    core.provider = helper_provider
    core.cms = CarbonModelSupervisor(
        provider=helper_provider,
        default_model_id=settings.primary_model,
        approved_models=settings.approved_models,
        governance=core.governance,
    )
    await core.initialize()
    try:
        for prompt in (
            "What is the capital of France?",
            "Write a Python function to reverse a list.",
            "Solve this multi-step logic puzzle.",
        ):
            task = await core.create_task(TaskRequest(prompt=prompt))
            events = [e async for e in core.execute_task_stream(task)]
            assert task.state == TaskState.COMPLETED
            assert task.model_used == "qwen3.5:9b"
            assert task.result == "Qwen specific output"
            assert events[-1]["event"] == "completed"

        # Even in single-model production, CMS selection rejects VISION and audits safely
        with pytest.raises(ModelIncompatibleError):
            await core.cms.select_model_for_task(
                [ModelCapability.CHAT, ModelCapability.VISION],
                ReasoningDepth.STANDARD,
                task_category=TaskCategory.VISION,
            )
        audit_text = settings.audit_log_path.read_text(encoding="utf-8")
        assert "MODEL_SELECTION_REJECTED" in audit_text
        ok_chain, count, err = verify_audit_log_chain(settings.audit_log_path)
        assert ok_chain and count > 0, err
    finally:
        await core.shutdown()
