"""Deterministic Stage 5-A unit tests for monotonic telemetry and benchmark helpers."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

import carbon.main as main_module
from carbon.config.settings import DEFAULT_PRIMARY_MODEL, Settings, get_settings
from carbon.core.metrics import (
    CarbonMetricsRegistry,
    TaskLatencySample,
    capture_provider_telemetry,
    measure_ws_submission,
    nist_percentile,
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
from carbon.model_control.ollama_provider import OllamaProvider


def _make_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        database_path=data_dir / "carbon.db",
        audit_log_path=data_dir / "audit.log",
        api_token="stage5a-test-token-value",
        enable_audit_log=True,
    )


def test_nist_percentile_and_summary() -> None:
    """Verify NIST linear interpolation math and empty/singleton edge behavior."""
    assert nist_percentile([], 50.0) == 0.0
    assert nist_percentile([42.0], 95.0) == 42.0
    # N=5 sorted [10, 20, 30, 40, 50]: P50 rank=3.0 -> 30.0; P95 rank=4.8 -> 48.0
    vals = [50.0, 10.0, 40.0, 20.0, 30.0]
    assert nist_percentile(vals, 50.0) == pytest.approx(30.0)
    assert nist_percentile(vals, 95.0) == pytest.approx(48.0)

    empty_summary = summarize_distribution([])
    assert empty_summary == {
        "count": 0,
        "min": None,
        "p50": None,
        "p90": None,
        "p95": None,
        "max": None,
    }


def test_registry_bounded_window_and_eval_validation() -> None:
    """Verify bounded ring buffer eviction and strict eval throughput filtering."""
    registry = CarbonMetricsRegistry(max_samples=3)
    for idx in range(5):
        eval_val: Any = 20 if idx == 4 else (True if idx == 3 else None)
        sample = TaskLatencySample.from_monotonic_points(
            task_category="general",
            reasoning_depth="instant",
            terminal_state="completed",
            requires_switch=False,
            is_fast_path=False,
            model_id=DEFAULT_PRIMARY_MODEL,
            failure_kind=None,
            t_accept_mono=10.0 + idx,
            t_infer_start_mono=10.005 + idx,
            t_reasoning_mono=None,
            t_answer_mono=10.015 + idx,
            t_complete_mono=10.025 + idx,
            eval_count=eval_val,
            eval_duration_ns=1_000_000_000,
        )
        registry.record_task_sample(sample)

    snap = registry.snapshot(active_model_id=DEFAULT_PRIMARY_MODEL, ollama_available=True)
    assert snap["window_sample_count"] == 3
    assert snap["counters"]["total_tasks"] == 5
    assert snap["counters"]["completed_tasks"] == 5
    # Only idx=4 in window had valid non-bool eval_count=20 -> 20.0 tok/s
    assert snap["throughput"]["provider_eval_tokens_per_sec"]["count"] == 1
    assert snap["throughput"]["provider_eval_tokens_per_sec"]["p50"] == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_provider_execution_timestamp_fires_at_http_send_not_generator_init() -> None:
    """Creating an async generator does not mark execution start; sending HTTP POST does."""
    events_log: list[tuple[str, float]] = []

    class FakeHttpResponse:
        status_code = 200

        async def aiter_lines(self) -> AsyncIterator[str]:
            yield json.dumps({"response": "ok", "thinking": "", "done": False})
            yield json.dumps(
                {
                    "response": "",
                    "thinking": "",
                    "done": True,
                    "done_reason": "stop",
                    "eval_count": 10,
                    "eval_duration": 500_000_000,
                }
            )

    class FakeHttpClient:
        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        @asynccontextmanager
        async def stream(
            self, method: str, endpoint: str, *, json: dict[str, Any]
        ) -> AsyncIterator[FakeHttpResponse]:
            assert method == "POST"
            assert endpoint == "/api/generate"
            assert json["model"] == DEFAULT_PRIMARY_MODEL
            events_log.append(("http_stream_enter", time.perf_counter()))
            yield FakeHttpResponse()

    provider = OllamaProvider(base_url="http://127.0.0.1:11434")
    provider._client = lambda *_a, **_kw: FakeHttpClient()  # type: ignore[method-assign, assignment]

    with capture_provider_telemetry() as telemetry:
        gen = provider.generate_stream_events(
            model_id=DEFAULT_PRIMARY_MODEL,
            prompt="hello",
            think=False,
        )
        # Generator object was created, NOT iterated yet -> must remain None!
        await asyncio.sleep(0.025)
        before_iter = time.perf_counter()
        assert telemetry.provider_request_start_mono is None

        chunks = [chunk async for chunk in gen]

    assert len(chunks) == 2
    assert telemetry.provider_request_start_mono is not None
    assert telemetry.provider_request_start_mono >= before_iter
    assert events_log[0][0] == "http_stream_enter"
    assert telemetry.provider_request_start_mono <= events_log[0][1]
    assert telemetry.eval_count == 10
    assert telemetry.eval_duration_ns == 500_000_000


@pytest.mark.asyncio
async def test_orchestrator_separates_backend_and_provider_monotonic_latencies(
    tmp_path: Path,
) -> None:
    """Orchestrator captures T_accept -> T_infer_start -> T_answer cleanly on backend clock."""
    core = CarbonCore(settings=_make_settings(tmp_path))
    fake_provider = FakeProvider(
        models=[
            ModelMetadata(
                id=DEFAULT_PRIMARY_MODEL,
                name="Qwen 3.5 9B",
                capabilities=[
                    ModelCapability.CHAT,
                    ModelCapability.TOOLS,
                    ModelCapability.THINKING,
                ],
                context_length=4096,
                memory_footprint_mb=6200,
                is_active=True,
                is_healthy=True,
            )
        ],
        running_models={DEFAULT_PRIMARY_MODEL},
        thinking_chunks=["reasoning..."],
        response_chunks=["42"],
        chunk_delay_seconds=0.025,
        simulated_eval_count=30,
        simulated_eval_duration_ns=1_000_000_000,
    )
    core.provider = fake_provider
    core.cms = CarbonModelSupervisor(
        provider=fake_provider,
        default_model_id=DEFAULT_PRIMARY_MODEL,
        governance=core.governance,
    )
    await core.initialize()
    try:
        task_deep = await core.create_task(
            TaskRequest(
                prompt="Solve deeply",
                reasoning_override=ReasoningDepth.DEEP,
            )
        )
        events_deep = [ev async for ev in core.execute_task_stream(task_deep)]
        assert events_deep[-1]["event"] == "completed"
        timings_deep = events_deep[-1]["backend_timings_ms"]

        assert timings_deep["clock_basis"] == "backend_monotonic_perf_counter"
        assert timings_deep["backend_acceptance_to_provider_start_ms"] >= 0.0
        assert timings_deep["provider_execution_to_first_reasoning_ms"] >= 20.0
        assert timings_deep["provider_execution_to_first_answer_ms"] >= 40.0
        assert (
            timings_deep["backend_acceptance_to_answer_ms"]
            >= timings_deep["provider_execution_to_first_answer_ms"]
        )
        assert (
            timings_deep["backend_acceptance_to_completion_ms"]
            >= timings_deep["backend_acceptance_to_answer_ms"]
        )
        assert timings_deep["provider_eval_tokens_per_sec"] == pytest.approx(30.0)

        # INSTANT mode (think=False): reasoning metrics must remain None
        task_inst = await core.create_task(
            TaskRequest(
                prompt="ping",
                reasoning_override=ReasoningDepth.INSTANT,
            )
        )
        events_inst = [ev async for ev in core.execute_task_stream(task_inst)]
        assert events_inst[-1]["event"] == "completed"
        timings_inst = events_inst[-1]["backend_timings_ms"]
        assert timings_inst["backend_acceptance_to_first_reasoning_ms"] is None
        assert timings_inst["provider_execution_to_first_reasoning_ms"] is None
        assert timings_inst["provider_execution_to_first_answer_ms"] >= 20.0

        snap = core.metrics.snapshot(
            active_model_id=DEFAULT_PRIMARY_MODEL,
            ollama_available=True,
            current_pressure_level=1,
            max_context_tokens=32768,
        )
        assert snap["counters"]["total_tasks"] == 2
        assert snap["latencies_ms"]["by_reasoning_depth"]["deep"]["sample_count"] == 1
        assert snap["latencies_ms"]["by_reasoning_depth"]["instant"]["sample_count"] == 1
        assert snap["throughput"]["provider_eval_tokens_per_sec"]["p50"] == pytest.approx(30.0)
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_metrics_records_switched_failures_and_cancellations(tmp_path: Path) -> None:
    """Switch counters, rejections, and cancellations record accurately without leaking prompts."""
    coder_id = "coder-model:7b"
    core = CarbonCore(settings=_make_settings(tmp_path))
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
        # 1. Switched task (GENERAL active -> CODE task)
        code_task = await core.create_task(TaskRequest(prompt="Write a python function to sort."))
        ev_code = [ev async for ev in core.execute_task_stream(code_task)]
        assert ev_code[-1]["event"] == "completed"
        assert ev_code[-1]["backend_timings_ms"]["requires_switch"] is True

        # 2. Pre-cancelled task
        cancel_task = await core.create_task(TaskRequest(prompt="Cancel me"))
        cancel_task.cancel_event.set()
        ev_cancel = [ev async for ev in core.execute_task_stream(cancel_task)]
        assert ev_cancel[-1]["event"] == "cancelled"

        # 3. Provider failure while running on already-active coder model (no second switch)
        fake_provider.simulate_error = RuntimeError("simulated error with secret=topsecret")
        fail_task = await core.create_task(TaskRequest(prompt="Write a python script."))
        ev_fail = [ev async for ev in core.execute_task_stream(fail_task)]
        assert ev_fail[-1]["event"] == "failed"

        snap = core.metrics.snapshot()
        assert snap["counters"]["total_tasks"] == 3
        assert snap["counters"]["completed_tasks"] == 1
        assert snap["counters"]["cancelled_tasks"] == 1
        assert snap["counters"]["failed_tasks"] == 1
        assert snap["counters"]["switched_tasks"] == 1
        assert snap["counters"]["switch_attempts"] == 1
        assert snap["counters"]["failure_kind_counts"].get("provider_error") == 1
        assert snap["latencies_ms"]["by_routing_path"]["switched"]["sample_count"] == 1
        assert "topsecret" not in json.dumps(snap)
        assert "Write a python function" not in json.dumps(snap)
    finally:
        await core.shutdown()


def test_ws_benchmark_helper_uses_separate_clocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Benchmark helper records independent client monotonic and backend monotonic metrics."""
    data_dir = tmp_path / "bench_ws"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CARBON_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CARBON_DATABASE_PATH", str(data_dir / "carbon.db"))
    monkeypatch.setenv("CARBON_AUDIT_LOG_PATH", str(data_dir / "audit.log"))
    monkeypatch.setenv("CARBON_API_TOKEN", "bench-helper-token")
    get_settings.cache_clear()
    main_module.core = None

    fake = FakeProvider(response_chunks=["Paris"], chunk_delay_seconds=0.01)
    monkeypatch.setattr("carbon.core.orchestrator.OllamaProvider", lambda **_kw: fake)
    fake.running_models = {DEFAULT_PRIMARY_MODEL}

    app = create_app()
    try:
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer bench-helper-token"}
            with client.websocket_connect("/ws/chat", headers=headers) as ws:
                rec = measure_ws_submission(
                    ws,
                    "What is the capital of France?",
                    reasoning="instant",
                    iteration=1,
                )
            assert rec["terminal_state"] == "completed"
            assert rec["client_clock_basis"] == "client_monotonic_perf_counter"
            assert rec["backend_clock_basis"] == "backend_monotonic_perf_counter"
            assert rec["client_submit_to_first_answer_ms"] >= 5.0
            assert rec["backend_acceptance_to_answer_ms"] >= 5.0
            assert rec["provider_execution_to_first_answer_ms"] >= 5.0

            summary = summarize_e2e_runs([rec])
            assert summary["measurement_rules"]["cross_clock_subtraction"] is False
            assert (
                summary["measurement_rules"]["client_answer_metric_scope"]
                == "ws_json_receipt_only_not_screen_display"
            )
            assert summary["backend_metrics"]["backend_acceptance_to_answer_ms"]["count"] == 1
            assert summary["client_metrics"]["client_submit_to_first_answer_ms"]["count"] == 1

            metrics_res = client.get("/api/v1/metrics", headers=headers)
            assert metrics_res.status_code == 200
            body = metrics_res.json()
            assert body["counters"]["completed_tasks"] == 1
            assert body["cms_state"]["active_model_id"] == DEFAULT_PRIMARY_MODEL
    finally:
        main_module.core = None
        get_settings.cache_clear()
