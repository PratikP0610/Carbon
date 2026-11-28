"""Deterministic FakeProvider orchestration performance benchmarks (Stage 5-A).

Safe for CI execution with zero Ollama dependency. Establishes reproducible
baseline distributions on isolated temp databases and FakeProvider.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import carbon.main as main_module
from carbon.config.settings import DEFAULT_PRIMARY_MODEL, Settings, get_settings
from carbon.core.metrics import (
    measure_ws_submission,
    summarize_distribution,
    summarize_e2e_runs,
)
from carbon.core.orchestrator import CarbonCore
from carbon.interfaces.models import (
    ApprovedModelProfile,
    ModelCapability,
    ModelMetadata,
    ReasoningDepth,
    TaskCategory,
    TaskRequest,
)
from carbon.main import create_app
from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.fake_provider import FakeProvider


def _isolated_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "perf_orch"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        database_path=data_dir / "carbon.db",
        audit_log_path=data_dir / "audit.log",
        api_token="perf-bench-token",
        enable_audit_log=True,
    )


@pytest.mark.asyncio
async def test_benchmark_warm_orchestration_overhead_fake_provider(tmp_path: Path) -> None:
    """Measure N=50 warm resident orchestration dispatch latency with FakeProvider."""
    core = CarbonCore(settings=_isolated_settings(tmp_path))
    fake_provider = FakeProvider(
        models=[
            ModelMetadata(
                id=DEFAULT_PRIMARY_MODEL,
                name="Qwen 3.5 9B",
                capabilities=[ModelCapability.CHAT, ModelCapability.THINKING],
                context_length=4096,
                memory_footprint_mb=6200,
                is_active=True,
                is_healthy=True,
            )
        ],
        running_models={DEFAULT_PRIMARY_MODEL},
        response_chunks=["Paris."],
        chunk_delay_seconds=0.0,
    )
    core.provider = fake_provider
    core.cms = CarbonModelSupervisor(
        provider=fake_provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        governance=core.governance,
    )
    await core.initialize()
    try:
        # Warm 1 iteration outside benchmark sample
        warm_task = await core.create_task(
            TaskRequest(prompt="ping", reasoning_override=ReasoningDepth.INSTANT)
        )
        _ = [ev async for ev in core.execute_task_stream(warm_task)]

        provider_start_ms: list[float] = []
        accept_to_answer_ms: list[float] = []
        total_ms: list[float] = []

        for idx in range(50):
            t_accept = time.perf_counter()
            task = await core.create_task(
                TaskRequest(
                    prompt=f"What is 2 + {idx}? Answer in one word.",
                    reasoning_override=ReasoningDepth.INSTANT,
                ),
                accepted_mono=t_accept,
            )
            events = [ev async for ev in core.execute_task_stream(task)]
            assert events[-1]["event"] == "completed"
            timings = events[-1]["backend_timings_ms"]
            provider_start_ms.append(float(timings["backend_acceptance_to_provider_start_ms"]))
            accept_to_answer_ms.append(float(timings["backend_acceptance_to_answer_ms"]))
            total_ms.append(float(timings["backend_acceptance_to_completion_ms"]))

        orch_summary = summarize_distribution(provider_start_ms)
        ans_summary = summarize_distribution(accept_to_answer_ms)
        total_summary = summarize_distribution(total_ms)
        print(
            "\n[Stage 5-A Warm Orchestration N=50 (FakeProvider)] "
            f"Accept->ProviderStart: {json.dumps(orch_summary)} | "
            f"Accept->Answer: {json.dumps(ans_summary)} | "
            f"Total: {json.dumps(total_summary)}"
        )

        assert orch_summary["count"] == 50
        assert orch_summary["p50"] is not None
        assert orch_summary["p95"] is not None
        assert orch_summary["p95"] <= 50.0
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_benchmark_model_switching_overhead_fake_provider(tmp_path: Path) -> None:
    """Measure N=20 alternating category switch overhead using FakeProvider."""
    coder_id = "coder-model:7b"
    core = CarbonCore(settings=_isolated_settings(tmp_path))
    fake_provider = FakeProvider(
        models=[
            ModelMetadata(
                id=DEFAULT_PRIMARY_MODEL,
                name="Qwen 3.5 9B",
                capabilities=[ModelCapability.CHAT, ModelCapability.THINKING],
                context_length=4096,
                memory_footprint_mb=6200,
                is_active=True,
                is_healthy=True,
            ),
            ModelMetadata(
                id=coder_id,
                name="Coder 7B",
                capabilities=[ModelCapability.CHAT, ModelCapability.COMPLETION],
                context_length=8192,
                memory_footprint_mb=4200,
                is_active=False,
                is_healthy=True,
            ),
        ],
        running_models={DEFAULT_PRIMARY_MODEL},
        response_chunks=["Done."],
    )
    core.provider = fake_provider
    core.cms = CarbonModelSupervisor(
        provider=fake_provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        approved_models=(DEFAULT_PRIMARY_MODEL, coder_id),
        model_profiles={
            DEFAULT_PRIMARY_MODEL: ApprovedModelProfile(
                model_id=DEFAULT_PRIMARY_MODEL,
                supported_capabilities=(ModelCapability.CHAT, ModelCapability.THINKING),
                suitable_task_categories=(TaskCategory.GENERAL, TaskCategory.DEEP_REASONING),
                preferred_task_categories=(TaskCategory.GENERAL,),
                estimated_memory_mb=6200,
            ),
            coder_id: ApprovedModelProfile(
                model_id=coder_id,
                supported_capabilities=(ModelCapability.CHAT, ModelCapability.COMPLETION),
                suitable_task_categories=(TaskCategory.CODE,),
                preferred_task_categories=(TaskCategory.CODE,),
                estimated_memory_mb=4200,
            ),
        },
        governance=core.governance,
    )
    await core.initialize()
    try:
        switched_ms: list[float] = []
        for idx in range(20):
            prompt = (
                "Write a python helper function."
                if idx % 2 == 0
                else "What is the capital of Japan?"
            )
            task = await core.create_task(
                TaskRequest(prompt=prompt, reasoning_override=ReasoningDepth.STANDARD)
            )
            events = [ev async for ev in core.execute_task_stream(task)]
            assert events[-1]["event"] == "completed"
            timings = events[-1]["backend_timings_ms"]
            assert timings["requires_switch"] is True
            switched_ms.append(float(timings["backend_acceptance_to_provider_start_ms"]))

        switch_summary = summarize_distribution(switched_ms)
        print(f"\n[Stage 5-A Switched Dispatch N=20 (FakeProvider)] {json.dumps(switch_summary)}")
        assert switch_summary["count"] == 20
        assert switch_summary["p50"] is not None
        assert switch_summary["p95"] is not None
        assert switch_summary["p95"] <= 50.0
    finally:
        await core.shutdown()


def test_benchmark_websocket_e2e_pipeline_fake_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measure N=30 WebSocket turns with separated client and backend monotonic clocks."""
    data_dir = tmp_path / "perf_ws"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CARBON_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CARBON_DATABASE_PATH", str(data_dir / "carbon.db"))
    monkeypatch.setenv("CARBON_AUDIT_LOG_PATH", str(data_dir / "audit.log"))
    monkeypatch.setenv("CARBON_API_TOKEN", "perf-ws-token")
    get_settings.cache_clear()
    main_module.core = None

    fake = FakeProvider(response_chunks=["Paris"], chunk_delay_seconds=0.0)
    fake.running_models = {DEFAULT_PRIMARY_MODEL}
    monkeypatch.setattr("carbon.core.orchestrator.OllamaProvider", lambda **_kw: fake)

    app = create_app()
    runs: list[dict] = []
    try:
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer perf-ws-token"}
            with client.websocket_connect("/ws/chat", headers=headers) as ws:
                # Warm turn
                measure_ws_submission(ws, "ping", reasoning="instant", iteration=0)
                for idx in range(30):
                    runs.append(
                        measure_ws_submission(
                            ws,
                            f"Question {idx}: capital of France?",
                            reasoning="instant",
                            iteration=idx + 1,
                        )
                    )

        summary = summarize_e2e_runs(runs)
        print(f"\n[Stage 5-A WS E2E N=30 (FakeProvider)] {json.dumps(summary)}")
        assert summary["num_runs"] == 30
        assert summary["measurement_rules"]["cross_clock_subtraction"] is False
        assert (
            summary["backend_metrics"]["backend_acceptance_to_provider_start_ms"]["p50"] is not None
        )
        assert summary["backend_metrics"]["backend_acceptance_to_provider_start_ms"]["p95"] <= 50.0
        assert summary["client_metrics"]["client_submit_to_first_answer_ms"]["count"] == 30
    finally:
        main_module.core = None
        get_settings.cache_clear()
