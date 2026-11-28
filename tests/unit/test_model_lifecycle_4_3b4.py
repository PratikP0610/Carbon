"""Dedicated Stage 4.3B-4 regression tests for active inference failure recovery and lease cleanup."""

import asyncio
import errno
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from carbon.config.settings import Settings
from carbon.core.orchestrator import CarbonCore
from carbon.interfaces.models import (
    HealthProbeStatus,
    InferenceFailureKind,
    ModelCapability,
    ModelMetadata,
    ModelSwitchRequest,
    ProviderInferenceError,
    RuntimeDiscoveryError,
    TaskRequest,
    TaskState,
)
from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.fake_provider import FakeProvider
from carbon.model_control.ollama_provider import OllamaProvider


def _qwen_meta() -> ModelMetadata:
    return ModelMetadata(
        id="qwen3.5:9b",
        name="Qwen 3.5 9B",
        provider="ollama",
        family="qwen35",
        context_length=4096,
        max_architectural_context=262144,
        capabilities=[
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
            ModelCapability.THINKING,
        ],
        is_healthy=True,
    )


def _coder_meta() -> ModelMetadata:
    return ModelMetadata(
        id="qwen-coder:7b",
        name="Qwen Coder 7B",
        provider="ollama",
        family="qwen_coder",
        context_length=16384,
        max_architectural_context=131072,
        capabilities=[
            ModelCapability.COMPLETION,
            ModelCapability.CHAT,
            ModelCapability.TOOLS,
        ],
        is_healthy=True,
    )


def _econnrefused_error(url: str = "http://127.0.0.1:11434/api/generate") -> httpx.ConnectError:
    req = httpx.Request("POST", url)
    err = httpx.ConnectError("Connection refused", request=req)
    err.__cause__ = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
    return err


async def _make_core(
    tmp_path: Path,
    provider: FakeProvider,
    *,
    approved_models: tuple[str, ...] = ("qwen3.5:9b",),
) -> CarbonCore:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "carbon.db",
        audit_log_path=tmp_path / "data" / "audit.log",
    )
    core = CarbonCore(settings=settings)
    core.provider = provider
    core.cms = CarbonModelSupervisor(
        provider=provider,
        default_model_id="qwen3.5:9b",
        approved_models=approved_models,
    )
    await core.initialize()
    return core


def _assert_lease_clean(cms: CarbonModelSupervisor) -> None:
    assert cms._active_leases == set()
    assert cms._inference_lock.locked() is False
    assert cms._drain_event.is_set() is True
    assert cms._switch_pending is False
    assert cms._switch_clear_event.is_set() is True


@pytest.mark.asyncio
async def test_connection_failure_before_first_token_releases_lease_and_recovers_later(
    tmp_path: Path,
) -> None:
    """Connection error before token 1 cleans lease, reconciles outage safely, and recovers on next request."""
    provider = FakeProvider(
        models=[_qwen_meta(), _coder_meta()],
        running_models={"qwen3.5:9b"},
        default_response="Recovered answer.",
    )
    core = await _make_core(
        tmp_path,
        provider,
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    try:
        # Simulate confirmed daemon outage during active stream before any token is emitted
        provider.healthy = False
        provider.running_models.clear()

        req1 = TaskRequest(prompt="Explain gravity")
        task1 = await core.create_task(req1)
        events1 = [e async for e in core.execute_task_stream(task1)]

        assert task1.state == TaskState.FAILED
        assert task1.truncated is False
        assert task1.result is None
        _assert_lease_clean(core.cms)

        failed1 = [e for e in events1 if e.get("event") == "failed"]
        assert len(failed1) == 1
        assert failed1[0]["incomplete"] is False
        assert failed1[0]["failure_kind"] == InferenceFailureKind.CONNECTION_ERROR.value
        assert not any(e.get("event") == "completed" for e in events1)

        # Exactly one generation attempt (no automatic retry or model switch)
        gen_calls = [c for c in provider.calls if c["method"] == "generate_stream_events"]
        assert len(gen_calls) == 1
        assert core.cms.ollama_available is False
        assert await core.cms.get_active_model() is None

        # Ollama daemon comes back healthy -> next request recovers cleanly on qwen3.5:9b (never auto-switches)
        provider.healthy = True
        provider.running_models = {"qwen3.5:9b"}
        req2 = TaskRequest(prompt="Explain gravity again")
        task2 = await core.create_task(req2)
        events2 = [e async for e in core.execute_task_stream(task2)]

        assert task2.state == TaskState.COMPLETED
        assert any(e.get("event") == "completed" for e in events2)
        assert not any(e.get("event") == "failed" for e in events2)
        assert task2.model_used == "qwen3.5:9b"
        assert task2.result == "Recovered answer."
        _assert_lease_clean(core.cms)
        active = await core.cms.get_active_model()
        assert active is not None and active.id == "qwen3.5:9b"
        assert not any(c["model_id"] == "qwen-coder:7b" for c in provider.calls if "model_id" in c)
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_partial_stream_failures_mark_incomplete_without_retry_or_duplicate(
    tmp_path: Path,
) -> None:
    """Mid-stream connection drop, timeout, and HTTP error preserve partial output and never complete or retry."""
    for idx, (provider_patch, expected_kind) in enumerate(
        [
            (
                {"simulate_error": ConnectionError("socket drop"), "stream_error_after_chunks": 2},
                InferenceFailureKind.CONNECTION_ERROR,
            ),
            (
                {"simulate_timeout": True, "stream_error_after_chunks": 2},
                InferenceFailureKind.TIMEOUT,
            ),
            (
                {
                    "simulate_error": ProviderInferenceError(
                        InferenceFailureKind.HTTP_ERROR, "Ollama /api/generate returned HTTP 500"
                    ),
                    "stream_error_after_chunks": 2,
                },
                InferenceFailureKind.HTTP_ERROR,
            ),
            (
                {"simulate_stream_interruption": True, "interrupt_after_chunks": 2},
                InferenceFailureKind.STREAM_INTERRUPTED,
            ),
        ]
    ):
        provider = FakeProvider(
            models=[_qwen_meta()],
            running_models={"qwen3.5:9b"},
            response_chunks=["First ", "Second ", "Third"],
            **provider_patch,
        )
        core = await _make_core(tmp_path / f"case_{idx}", provider)
        try:
            task = await core.create_task(TaskRequest(prompt="Write a sentence"))
            events = [e async for e in core.execute_task_stream(task)]

            tokens = [e["text"] for e in events if e.get("event") == "token"]
            assert tokens == ["First ", "Second "]
            assert not any(e.get("event") == "completed" for e in events)

            failed = [e for e in events if e.get("event") == "failed"]
            assert len(failed) == 1
            assert failed[0]["incomplete"] is True
            assert failed[0]["failure_kind"] == expected_kind.value

            assert task.state == TaskState.FAILED
            assert task.truncated is True
            assert task.result == "First Second "
            _assert_lease_clean(core.cms)

            # Strictly one generation call (no silent restart/duplicate content)
            gen_calls = [c for c in provider.calls if c["method"] == "generate_stream_events"]
            assert len(gen_calls) == 1
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_timeout_http_and_malformed_failures_preserve_active_model_and_unblock_next(
    tmp_path: Path,
) -> None:
    """Single timeout, HTTP error, or malformed response never marks active model unavailable or blocks next task."""
    provider = FakeProvider(
        models=[_qwen_meta(), _coder_meta()],
        running_models={"qwen3.5:9b"},
        default_response="Healthy reply.",
    )
    core = await _make_core(
        tmp_path,
        provider,
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    try:
        # 1. Inference timeout before token 1
        provider.simulate_timeout = True
        t1 = await core.create_task(TaskRequest(prompt="Timeout test"))
        ev1 = [e async for e in core.execute_task_stream(t1)]
        assert t1.state == TaskState.FAILED
        assert ev1[-1]["failure_kind"] == InferenceFailureKind.TIMEOUT.value
        assert core.cms.ollama_available is True
        active = await core.cms.get_active_model()
        assert active is not None and active.id == "qwen3.5:9b"
        assert provider.unloaded_models == []
        _assert_lease_clean(core.cms)

        # 2. HTTP 500 error before token 1
        provider.simulate_timeout = False
        provider.simulate_error = ProviderInferenceError(
            InferenceFailureKind.HTTP_ERROR,
            "Ollama /api/generate returned HTTP 500",
            http_status=500,
        )
        t2 = await core.create_task(TaskRequest(prompt="HTTP error test"))
        ev2 = [e async for e in core.execute_task_stream(t2)]
        assert t2.state == TaskState.FAILED
        assert ev2[-1]["failure_kind"] == InferenceFailureKind.HTTP_ERROR.value
        assert core.cms.ollama_available is True
        assert (await core.cms.get_active_model()) is active
        assert provider.unloaded_models == []
        _assert_lease_clean(core.cms)

        # 3. Malformed dict frame (e.g. response=None / unexpected schema)
        provider.simulate_error = None
        provider.simulate_malformed = True
        provider.malformed_chunks = [{"unexpected_field": True, "response": None, "done": False}]
        t3 = await core.create_task(TaskRequest(prompt="Malformed dict test"))
        ev3 = [e async for e in core.execute_task_stream(t3)]
        assert t3.state == TaskState.FAILED
        assert ev3[-1]["failure_kind"] == InferenceFailureKind.INVALID_RESPONSE.value
        assert core.cms.ollama_available is True
        assert (await core.cms.get_active_model()) is active
        _assert_lease_clean(core.cms)

        # 4. Subsequent normal request succeeds cleanly without unblocking issues
        provider.simulate_malformed = False
        t4 = await core.create_task(TaskRequest(prompt="Follow-up healthy request"))
        ev4 = [e async for e in core.execute_task_stream(t4)]
        assert t4.state == TaskState.COMPLETED
        assert t4.result == "Healthy reply."
        assert ev4[-1]["event"] == "completed"
        _assert_lease_clean(core.cms)
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_ollama_provider_streaming_rejects_http_timeout_bad_json_and_truncated_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OllamaProvider rejects HTTP 500, timeout, ECONNREFUSED, corrupt JSON, error chunks, and missing done=true."""

    class _FakeStreamResponse:
        def __init__(self, status_code: int, lines: list[str], body: bytes = b"") -> None:
            self.status_code = status_code
            self._lines = lines
            self._body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aread(self) -> bytes:
            return self._body

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    class _FakeStreamClient:
        def __init__(self, handler) -> None:
            self._handler = handler

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method: str, endpoint: str, json: dict[str, Any] | None = None):
            return self._handler(method, endpoint, json)

    provider = OllamaProvider()

    # 1. HTTP 503 error payload sanitized
    monkeypatch.setattr(
        provider,
        "_client",
        lambda *_a, **_kw: _FakeStreamClient(
            lambda _m, _e, _j: _FakeStreamResponse(503, [], b"overloaded\nretry-later")
        ),
    )
    with pytest.raises(ProviderInferenceError) as http_err:
        async for _ in provider.generate_stream_events("qwen3.5:9b", "hi"):
            pass
    assert http_err.value.kind == InferenceFailureKind.HTTP_ERROR
    assert "\n" not in str(http_err.value)

    # 2. Invalid JSON line raises INVALID_RESPONSE (never silently skipped)
    monkeypatch.setattr(
        provider,
        "_client",
        lambda *_a, **_kw: _FakeStreamClient(
            lambda _m, _e, _j: _FakeStreamResponse(200, ["{corrupt-json"])
        ),
    )
    with pytest.raises(ProviderInferenceError) as bad_json_err:
        async for _ in provider.generate_stream_events("qwen3.5:9b", "hi"):
            pass
    assert bad_json_err.value.kind == InferenceFailureKind.INVALID_RESPONSE

    # 3. Runtime JSON error frame raises HTTP_ERROR
    monkeypatch.setattr(
        provider,
        "_client",
        lambda *_a, **_kw: _FakeStreamClient(
            lambda _m, _e, _j: _FakeStreamResponse(200, ['{"error": "model crashed"}'])
        ),
    )
    with pytest.raises(ProviderInferenceError) as err_frame:
        async for _ in provider.generate_stream_events("qwen3.5:9b", "hi"):
            pass
    assert err_frame.value.kind == InferenceFailureKind.HTTP_ERROR

    # 4. Premature EOF without done=true raises STREAM_INTERRUPTED even after partial tokens
    monkeypatch.setattr(
        provider,
        "_client",
        lambda *_a, **_kw: _FakeStreamClient(
            lambda _m, _e, _j: _FakeStreamResponse(200, ['{"response": "Hello ", "done": false}'])
        ),
    )
    collected: list[dict[str, Any]] = []
    with pytest.raises(ProviderInferenceError) as eof_err:
        async for evt in provider.generate_stream_events("qwen3.5:9b", "hi"):
            collected.append(evt)
    assert len(collected) == 1
    assert eof_err.value.kind == InferenceFailureKind.STREAM_INTERRUPTED

    # 4a. Valid boolean 'done' values (false then true) continue to work
    monkeypatch.setattr(
        provider,
        "_client",
        lambda *_a, **_kw: _FakeStreamClient(
            lambda _m, _e, _j: _FakeStreamResponse(
                200,
                ['{"response": "Hello ", "done": false}', '{"response": "world", "done": true}'],
            )
        ),
    )
    ok_events = [evt async for evt in provider.generate_stream_events("qwen3.5:9b", "hi")]
    assert [(e["response"], e["done"]) for e in ok_events] == [
        ("Hello ", False),
        ("world", True),
    ]

    # 4b. Frame with absent optional 'done' followed by valid 'done': true succeeds
    monkeypatch.setattr(
        provider,
        "_client",
        lambda *_a, **_kw: _FakeStreamClient(
            lambda _m, _e, _j: _FakeStreamResponse(
                200, ['{"response": "Hello "}', '{"response": "world", "done": true}']
            )
        ),
    )
    no_done_events = [evt async for evt in provider.generate_stream_events("qwen3.5:9b", "hi")]
    assert [(e["response"], e["done"]) for e in no_done_events] == [
        ("Hello ", False),
        ("world", True),
    ]

    # 4c. Non-boolean 'done' values (null, string, int) raise INVALID_RESPONSE without coercion
    for bad_done_line in (
        '{"response": "hello", "done": null}',
        '{"response": "hello", "done": "false"}',
        '{"response": "hello", "done": 1}',
    ):
        monkeypatch.setattr(
            provider,
            "_client",
            lambda *_a, _line=bad_done_line, **_kw: _FakeStreamClient(
                lambda _m, _e, _j: _FakeStreamResponse(200, [_line])
            ),
        )
        with pytest.raises(ProviderInferenceError) as bad_done_err:
            async for _ in provider.generate_stream_events("qwen3.5:9b", "hi"):
                pass
        assert bad_done_err.value.kind == InferenceFailureKind.INVALID_RESPONSE

    # 4d. Direct generate_stream_events check also rejects done=None without coercion
    async def _fake_raw_stream(*_args, **_kwargs):
        yield {"response": "hello", "done": None}

    with monkeypatch.context() as mp:
        mp.setattr(provider, "_stream_ndjson_dicts", _fake_raw_stream)
        with pytest.raises(ProviderInferenceError) as direct_done_err:
            async for _ in provider.generate_stream_events("qwen3.5:9b", "hi"):
                pass
        assert direct_done_err.value.kind == InferenceFailureKind.INVALID_RESPONSE

    # 5. Connection error during stream start raises CONNECTION_ERROR
    class _FailingStreamCtx:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc

        async def __aenter__(self):
            raise self._exc

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        provider,
        "_client",
        lambda *_a, **_kw: _FakeStreamClient(
            lambda _m, _e, _j: _FailingStreamCtx(_econnrefused_error())
        ),
    )
    with pytest.raises(ProviderInferenceError) as conn_err:
        async for _ in provider.generate_stream_events("qwen3.5:9b", "hi"):
            pass
    assert conn_err.value.kind == InferenceFailureKind.CONNECTION_ERROR


@pytest.mark.asyncio
async def test_user_cancellation_before_and_during_stream_skips_recovery_and_releases_lease(
    tmp_path: Path,
) -> None:
    """User cancellation (pre-token, mid-stream, and asyncio.CancelledError) never invokes recovery or fails."""
    provider = FakeProvider(
        models=[_qwen_meta()],
        running_models={"qwen3.5:9b"},
        response_chunks=["One ", "Two ", "Three ", "Four"],
        chunk_delay_seconds=0.01,
    )
    core = await _make_core(tmp_path, provider)
    reconcile_spy = AsyncMock(wraps=core.cms.reconcile_after_inference_failure)
    probe_spy = AsyncMock(wraps=provider.probe_health)
    core.cms.reconcile_after_inference_failure = reconcile_spy  # type: ignore[method-assign]
    provider.probe_health = probe_spy  # type: ignore[method-assign]
    try:
        # 1. Pre-token cancellation via cancel_event
        t_pre = await core.create_task(TaskRequest(prompt="Pre cancel"))
        core.cancel_task(t_pre.task_id)
        ev_pre = [e async for e in core.execute_task_stream(t_pre)]
        assert t_pre.state == TaskState.CANCELLED
        assert [e.get("event") for e in ev_pre] == ["cancelled"]
        assert not any(e.get("event") == "failed" for e in ev_pre)
        assert reconcile_spy.await_count == 0
        assert probe_spy.await_count == 0
        _assert_lease_clean(core.cms)

        # 2. Mid-stream cancellation after partial tokens via cancel_event
        t_mid = await core.create_task(TaskRequest(prompt="Mid cancel"))
        ev_mid: list[dict[str, Any]] = []
        async for evt in core.execute_task_stream(t_mid):
            ev_mid.append(evt)
            if evt.get("event") == "token":
                core.cancel_task(t_mid.task_id)

        assert t_mid.state == TaskState.CANCELLED
        assert any(e.get("event") == "cancelled" for e in ev_mid)
        assert not any(e.get("event") in ("failed", "completed") for e in ev_mid)
        assert reconcile_spy.await_count == 0
        assert probe_spy.await_count == 0
        _assert_lease_clean(core.cms)

        # 3. True asyncio.CancelledError propagates without conversion into provider failure
        t_async = await core.create_task(TaskRequest(prompt="Task cancel"))
        provider.chunk_delay_seconds = 0.2
        first_token_seen = asyncio.Event()
        collected_async: list[dict[str, Any]] = []

        async def _consume() -> None:
            async for evt in core.execute_task_stream(t_async):
                collected_async.append(evt)
                if evt.get("event") == "token":
                    first_token_seen.set()

        runner = asyncio.create_task(_consume())
        await asyncio.wait_for(first_token_seen.wait(), timeout=2.0)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

        assert t_async.state == TaskState.CANCELLED
        assert not any(e.get("event") in ("failed", "completed") for e in collected_async)
        assert reconcile_spy.await_count == 0
        assert probe_spy.await_count == 0
        _assert_lease_clean(core.cms)

        # 4. Early generator close (break / aclose) finalizes CANCELLED state, persists partial text, and cleans lease
        provider.chunk_delay_seconds = 0.0
        t_break = await core.create_task(TaskRequest(prompt="Early break"))
        break_events: list[dict[str, Any]] = []
        stream = core.execute_task_stream(t_break)
        async for evt in stream:
            break_events.append(evt)
            if evt.get("event") == "token":
                break
        await stream.aclose()

        _assert_lease_clean(core.cms)
        assert t_break.state == TaskState.CANCELLED
        assert t_break.completed_at is not None
        assert t_break.result == "One "
        assert t_break.truncated is True
        assert not any(e.get("event") in ("failed", "completed") for e in break_events)
        assert reconcile_spy.await_count == 0
        assert probe_spy.await_count == 0
        persisted_break = await core.get_task_persistent(t_break.task_id)
        assert persisted_break is not None
        assert persisted_break["state"] == TaskState.CANCELLED.value
        assert persisted_break["result"] == "One "
        assert persisted_break["truncated"] is True
        assert persisted_break["completed_at"] is not None

        # 5. Early generator close before tokens (on analyzing state_change) also persists CANCELLED safely
        t_early_close = await core.create_task(TaskRequest(prompt="Pre-stream close"))
        early_stream = core.execute_task_stream(t_early_close)
        first_evt = await anext(early_stream)
        assert first_evt["event"] == "state_change"
        await early_stream.aclose()

        _assert_lease_clean(core.cms)
        assert t_early_close.state == TaskState.CANCELLED
        assert t_early_close.completed_at is not None
        assert t_early_close.result is None
        assert t_early_close.truncated is False
        assert reconcile_spy.await_count == 0
        persisted_early = await core.get_task_persistent(t_early_close.task_id)
        assert persisted_early is not None
        assert persisted_early["state"] == TaskState.CANCELLED.value
        assert persisted_early["completed_at"] is not None

        # 6. Follow-up request executes normally after generator closures
        t_followup = await core.create_task(TaskRequest(prompt="Follow-up after close"))
        followup_events = [e async for e in core.execute_task_stream(t_followup)]
        assert t_followup.state == TaskState.COMPLETED
        assert t_followup.result == "One Two Three Four"
        assert followup_events[-1]["event"] == "completed"
        _assert_lease_clean(core.cms)
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_inconclusive_recovery_preserves_state_and_duplicate_lease_release_is_safe(
    tmp_path: Path,
) -> None:
    """Inconclusive probe during connection failure preserves active model; duplicate release never unlocks peer."""
    provider = FakeProvider(
        models=[_qwen_meta()],
        running_models={"qwen3.5:9b"},
        response_chunks=["First ", "Second"],
    )
    core = await _make_core(tmp_path, provider)
    try:
        # 1. Duplicate release_inference_lease must never release a peer task's active lock
        await core.cms.acquire_inference_lease("task-1")
        await core.cms.release_inference_lease("task-1")
        _assert_lease_clean(core.cms)

        await core.cms.acquire_inference_lease("task-2")
        # Stale/duplicate release for task-1 MUST be ignored while task-2 holds the lock
        await core.cms.release_inference_lease("task-1")
        assert "task-2" in core.cms._active_leases
        assert core.cms._inference_lock.locked() is True
        await core.cms.release_inference_lease("task-2")
        _assert_lease_clean(core.cms)

        # 2. Connection error during inference + inconclusive probe preserves last known valid state
        provider.simulate_error = ConnectionError("transient disconnect")
        provider.health_probe_status = HealthProbeStatus.SERVER_ERROR
        provider.health_http_status = 502

        t_fail = await core.create_task(TaskRequest(prompt="Inconclusive recovery test"))
        ev_fail = [e async for e in core.execute_task_stream(t_fail)]

        assert t_fail.state == TaskState.FAILED
        assert ev_fail[-1]["failure_kind"] == InferenceFailureKind.CONNECTION_ERROR.value
        assert core.cms.ollama_available is True
        active = await core.cms.get_active_model()
        assert active is not None and active.id == "qwen3.5:9b"
        assert core.cms.last_health_probe is not None
        assert core.cms.last_health_probe.status == HealthProbeStatus.SERVER_ERROR
        _assert_lease_clean(core.cms)

        # 3. RuntimeDiscoveryError on post-failure probe also preserves state cleanly
        provider.health_probe_status = None
        provider.health_http_status = None
        provider.ps_error = RuntimeDiscoveryError(
            "/api/ps timeout", probe_status=HealthProbeStatus.TIMEOUT, endpoint="/api/ps"
        )
        t_fail2 = await core.create_task(TaskRequest(prompt="Inconclusive ps recovery"))
        await asyncio.sleep(0)
        _ = [e async for e in core.execute_task_stream(t_fail2)]
        assert core.cms.ollama_available is True
        assert (await core.cms.get_active_model()) is not None
        _assert_lease_clean(core.cms)
    finally:
        await core.shutdown()


@pytest.mark.asyncio
async def test_recovery_does_not_race_with_active_switch_or_queued_lease(
    tmp_path: Path,
) -> None:
    """Post-failure recovery releases lease cleanly so pending switch finishes without interference."""
    provider = FakeProvider(
        models=[_qwen_meta(), _coder_meta()],
        running_models={"qwen3.5:9b"},
        simulate_error=ConnectionError("mid-inference disconnect"),
    )
    core = await _make_core(
        tmp_path,
        provider,
        approved_models=("qwen3.5:9b", "qwen-coder:7b"),
    )
    try:
        # 1. Verify reconcile_after_inference_failure skips cleanly if another lease is active
        await core.cms.acquire_inference_lease("other-task")
        res_while_leased = await core.cms.reconcile_after_inference_failure(
            failure_kind=InferenceFailureKind.CONNECTION_ERROR
        )
        assert res_while_leased is None
        assert "other-task" in core.cms._active_leases
        await core.cms.release_inference_lease("other-task")
        _assert_lease_clean(core.cms)

        # 2. Verify failing task releases its lease BEFORE pending switch_model runs, and recovery skips
        in_switch_probe = asyncio.Event()
        release_switch = asyncio.Event()
        orig_probe = provider.probe_health
        reconcile_spy = AsyncMock(wraps=core.cms.reconcile_after_inference_failure)
        core.cms.reconcile_after_inference_failure = reconcile_spy  # type: ignore[method-assign]

        async def gated_switch_probe():
            in_switch_probe.set()
            await release_switch.wait()
            return await orig_probe()

        provider.probe_health = gated_switch_probe  # type: ignore[method-assign]

        await core.cms.acquire_inference_lease("pre-hold")
        switch_task = asyncio.create_task(
            core.cms.switch_model(
                ModelSwitchRequest(target_model_id="qwen-coder:7b", reason="switch")
            )
        )
        await asyncio.sleep(0)
        assert core.cms._switch_lock.locked() is True

        # Releasing pre-hold allows switch to proceed into gated_switch_probe
        await core.cms.release_inference_lease("pre-hold")
        await asyncio.wait_for(in_switch_probe.wait(), timeout=2.0)

        # Calling reconcile_after_inference_failure during active switch must immediately return None
        res_during_switch = await core.cms.reconcile_after_inference_failure(
            failure_kind=InferenceFailureKind.CONNECTION_ERROR
        )
        assert res_during_switch is None

        release_switch.set()
        assert await switch_task is True
        assert (await core.cms.get_active_model()).id == "qwen-coder:7b"  # type: ignore[union-attr]
        _assert_lease_clean(core.cms)
    finally:
        await core.shutdown()
