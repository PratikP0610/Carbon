"""Stage 5-A server-side monotonic telemetry and NIST percentile utilities."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

DEFAULT_METRICS_WINDOW_SIZE: int = 500


def nist_percentile(values: Sequence[float], percentile: float) -> float:
    """Compute percentile via NIST linear interpolation: R = 1 + (P / 100) * (N - 1)."""
    if isinstance(percentile, bool) or not (0.0 <= percentile <= 100.0):
        raise ValueError(f"Percentile must be between 0 and 100, got {percentile!r}.")
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])

    rank = 1.0 + (percentile / 100.0) * (n - 1)
    k = int(rank)
    d = rank - k
    if k >= n:
        return float(sorted_vals[-1])
    if k <= 1:
        return float(sorted_vals[0]) + d * (float(sorted_vals[1]) - float(sorted_vals[0]))
    return float(sorted_vals[k - 1]) + d * (float(sorted_vals[k]) - float(sorted_vals[k - 1]))


def summarize_distribution(values: Sequence[float], *, decimals: int = 3) -> dict[str, Any]:
    """Return NIST statistical summary (min, p50, p90, p95, max) for a sample sequence."""
    if not values:
        return {
            "count": 0,
            "min": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "max": None,
        }
    return {
        "count": len(values),
        "min": round(float(min(values)), decimals),
        "p50": round(nist_percentile(values, 50.0), decimals),
        "p90": round(nist_percentile(values, 90.0), decimals),
        "p95": round(nist_percentile(values, 95.0), decimals),
        "max": round(float(max(values)), decimals),
    }


@dataclass
class ProviderExecutionTelemetry:
    """Per-request provider execution timestamps and token evaluation counters."""

    provider_request_start_mono: float | None = None
    eval_count: int | None = None
    eval_duration_ns: int | None = None


_active_provider_telemetry: ContextVar[ProviderExecutionTelemetry | None] = ContextVar(
    "carbon_provider_telemetry",
    default=None,
)


@contextmanager
def capture_provider_telemetry() -> Iterator[ProviderExecutionTelemetry]:
    """Bind a per-request ProviderExecutionTelemetry receiver to the current async context."""
    telemetry = ProviderExecutionTelemetry()
    token = _active_provider_telemetry.set(telemetry)
    try:
        yield telemetry
    finally:
        _active_provider_telemetry.reset(token)


def mark_provider_request_execution_start() -> None:
    """Record monotonic timestamp immediately at provider request execution boundary."""
    telemetry = _active_provider_telemetry.get()
    if telemetry is not None and telemetry.provider_request_start_mono is None:
        telemetry.provider_request_start_mono = time.perf_counter()


def record_provider_eval_metrics(eval_count: Any, eval_duration_ns: Any) -> None:
    """Record provider-reported eval token count and duration (ns) when valid."""
    telemetry = _active_provider_telemetry.get()
    if telemetry is None:
        return
    if (
        not isinstance(eval_count, bool)
        and isinstance(eval_count, int)
        and eval_count >= 0
        and not isinstance(eval_duration_ns, bool)
        and isinstance(eval_duration_ns, int)
        and eval_duration_ns > 0
    ):
        telemetry.eval_count = eval_count
        telemetry.eval_duration_ns = eval_duration_ns


def _safe_delta_ms(start_mono: float | None, end_mono: float | None) -> float | None:
    """Compute non-negative millisecond delta between two same-clock monotonic readings."""
    if start_mono is None or end_mono is None:
        return None
    return max(0.0, (end_mono - start_mono) * 1000.0)


def _sanitize_label(raw_value: Any, *, max_len: int = 64) -> str | None:
    """Sanitize short identifier/enum strings for telemetry without exposing user data."""
    if not isinstance(raw_value, str):
        return None
    cleaned = " ".join(raw_value.replace("\r", " ").replace("\n", " ").split())
    return cleaned[:max_len] if cleaned else None


@dataclass(frozen=True)
class TaskLatencySample:
    """Sanitized zero-PII latency sample measured strictly on backend time.perf_counter()."""

    task_category: str
    reasoning_depth: str
    terminal_state: str
    requires_switch: bool
    is_fast_path: bool = False
    model_id: str | None = None
    failure_kind: str | None = None
    backend_acceptance_to_provider_start_ms: float | None = None
    backend_acceptance_to_answer_ms: float | None = None
    provider_execution_to_first_answer_ms: float | None = None
    backend_acceptance_to_first_reasoning_ms: float | None = None
    provider_execution_to_first_reasoning_ms: float | None = None
    backend_acceptance_to_completion_ms: float = 0.0
    provider_eval_tokens_per_sec: float | None = None
    visible_chunk_count: int = 0
    visible_output_chars: int = 0

    @classmethod
    def from_monotonic_points(
        cls,
        *,
        task_category: str,
        reasoning_depth: str,
        terminal_state: str,
        requires_switch: bool,
        is_fast_path: bool,
        model_id: str | None,
        failure_kind: str | None,
        t_accept_mono: float,
        t_infer_start_mono: float | None,
        t_reasoning_mono: float | None,
        t_answer_mono: float | None,
        t_complete_mono: float,
        eval_count: int | None = None,
        eval_duration_ns: int | None = None,
        visible_chunk_count: int = 0,
        visible_output_chars: int = 0,
    ) -> TaskLatencySample:
        """Build sanitized latency sample strictly from same-clock backend monotonic points."""
        eval_tok_s: float | None = None
        if (
            eval_count is not None
            and eval_duration_ns is not None
            and not isinstance(eval_count, bool)
            and not isinstance(eval_duration_ns, bool)
            and eval_count >= 0
            and eval_duration_ns > 0
        ):
            eval_tok_s = eval_count / (eval_duration_ns / 1_000_000_000.0)

        return cls(
            task_category=_sanitize_label(task_category) or "unknown",
            reasoning_depth=_sanitize_label(reasoning_depth) or "standard",
            terminal_state=_sanitize_label(terminal_state) or "unknown",
            requires_switch=bool(requires_switch),
            is_fast_path=bool(is_fast_path),
            model_id=_sanitize_label(model_id),
            failure_kind=_sanitize_label(failure_kind),
            backend_acceptance_to_provider_start_ms=_safe_delta_ms(
                t_accept_mono, t_infer_start_mono
            ),
            backend_acceptance_to_answer_ms=_safe_delta_ms(t_accept_mono, t_answer_mono),
            provider_execution_to_first_answer_ms=_safe_delta_ms(t_infer_start_mono, t_answer_mono),
            backend_acceptance_to_first_reasoning_ms=_safe_delta_ms(
                t_accept_mono, t_reasoning_mono
            ),
            provider_execution_to_first_reasoning_ms=_safe_delta_ms(
                t_infer_start_mono, t_reasoning_mono
            ),
            backend_acceptance_to_completion_ms=_safe_delta_ms(t_accept_mono, t_complete_mono)
            or 0.0,
            provider_eval_tokens_per_sec=eval_tok_s,
            visible_chunk_count=max(0, int(visible_chunk_count)),
            visible_output_chars=max(0, int(visible_output_chars)),
        )

    def to_timings_dict(self) -> dict[str, Any]:
        """Serialize strictly non-PII backend monotonic timings for telemetry consumers."""

        def _round_opt(val: float | None) -> float | None:
            return round(val, 3) if val is not None else None

        return {
            "clock_basis": "backend_monotonic_perf_counter",
            "requires_switch": self.requires_switch,
            "is_fast_path": self.is_fast_path,
            "backend_acceptance_to_provider_start_ms": _round_opt(
                self.backend_acceptance_to_provider_start_ms
            ),
            "backend_acceptance_to_answer_ms": _round_opt(self.backend_acceptance_to_answer_ms),
            "provider_execution_to_first_answer_ms": _round_opt(
                self.provider_execution_to_first_answer_ms
            ),
            "backend_acceptance_to_first_reasoning_ms": _round_opt(
                self.backend_acceptance_to_first_reasoning_ms
            ),
            "provider_execution_to_first_reasoning_ms": _round_opt(
                self.provider_execution_to_first_reasoning_ms
            ),
            "backend_acceptance_to_completion_ms": round(
                self.backend_acceptance_to_completion_ms, 3
            ),
            "provider_eval_tokens_per_sec": _round_opt(self.provider_eval_tokens_per_sec),
        }


def _summarize_sample_group(samples: Sequence[TaskLatencySample]) -> dict[str, Any]:
    """Summarize all named backend latency metrics across a list of TaskLatencySample."""
    return {
        "sample_count": len(samples),
        "backend_acceptance_to_provider_start_ms": summarize_distribution(
            [
                s.backend_acceptance_to_provider_start_ms
                for s in samples
                if s.backend_acceptance_to_provider_start_ms is not None
            ]
        ),
        "backend_acceptance_to_answer_ms": summarize_distribution(
            [
                s.backend_acceptance_to_answer_ms
                for s in samples
                if s.backend_acceptance_to_answer_ms is not None
            ]
        ),
        "provider_execution_to_first_answer_ms": summarize_distribution(
            [
                s.provider_execution_to_first_answer_ms
                for s in samples
                if s.provider_execution_to_first_answer_ms is not None
            ]
        ),
        "backend_acceptance_to_first_reasoning_ms": summarize_distribution(
            [
                s.backend_acceptance_to_first_reasoning_ms
                for s in samples
                if s.backend_acceptance_to_first_reasoning_ms is not None
            ]
        ),
        "provider_execution_to_first_reasoning_ms": summarize_distribution(
            [
                s.provider_execution_to_first_reasoning_ms
                for s in samples
                if s.provider_execution_to_first_reasoning_ms is not None
            ]
        ),
        "backend_acceptance_to_completion_ms": summarize_distribution(
            [s.backend_acceptance_to_completion_ms for s in samples]
        ),
    }


@dataclass
class CarbonMetricsRegistry:
    """Bounded rolling aggregate telemetry registry containing zero user or secret data."""

    max_samples: int = DEFAULT_METRICS_WINDOW_SIZE
    _samples: deque[TaskLatencySample] = field(init=False)
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    cancelled_tasks: int = 0
    fast_path_tasks: int = 0
    switched_tasks: int = 0
    switch_attempts: int = 0
    switch_failures: int = 0
    model_rejections: int = 0
    failure_kind_counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_samples <= 0:
            raise ValueError(f"max_samples must be > 0, got {self.max_samples}.")
        self._samples = deque(maxlen=self.max_samples)

    def record_switch_attempt(self, *, success: bool) -> None:
        """Increment model switch attempt and optional failure counter."""
        self.switch_attempts += 1
        if not success:
            self.switch_failures += 1

    def record_model_rejection(self) -> None:
        """Increment pre-lease or post-lease CMS rejection counter."""
        self.model_rejections += 1

    def record_task_sample(self, sample: TaskLatencySample) -> None:
        """Record one completed/failed/cancelled task sample and update monotonic counters."""
        self._samples.append(sample)
        self.total_tasks += 1
        if sample.terminal_state == "completed":
            self.completed_tasks += 1
        elif sample.terminal_state == "failed":
            self.failed_tasks += 1
        elif sample.terminal_state == "cancelled":
            self.cancelled_tasks += 1

        if sample.is_fast_path:
            self.fast_path_tasks += 1
        if sample.requires_switch:
            self.switched_tasks += 1
        if sample.failure_kind:
            self.failure_kind_counts[sample.failure_kind] = (
                self.failure_kind_counts.get(sample.failure_kind, 0) + 1
            )

    def snapshot(
        self,
        *,
        active_model_id: str | None = None,
        ollama_available: bool = False,
        current_pressure_level: int = 1,
        max_context_tokens: int = 32768,
    ) -> dict[str, Any]:
        """Build sanitized aggregate telemetry payload for GET /api/v1/metrics."""
        samples = list(self._samples)
        by_depth: dict[str, list[TaskLatencySample]] = {
            "instant": [],
            "standard": [],
            "deep": [],
        }
        resident_samples: list[TaskLatencySample] = []
        switched_samples: list[TaskLatencySample] = []
        fast_path_samples: list[TaskLatencySample] = []

        for sample in samples:
            if sample.reasoning_depth in by_depth:
                by_depth[sample.reasoning_depth].append(sample)
            if sample.is_fast_path:
                fast_path_samples.append(sample)
            elif sample.requires_switch:
                switched_samples.append(sample)
            else:
                resident_samples.append(sample)

        eval_rates = [
            s.provider_eval_tokens_per_sec
            for s in samples
            if s.provider_eval_tokens_per_sec is not None
        ]

        return {
            "schema_version": "5a.1",
            "clock_basis": "backend_monotonic_perf_counter",
            "window_sample_count": len(samples),
            "window_max_samples": self.max_samples,
            "counters": {
                "total_tasks": self.total_tasks,
                "completed_tasks": self.completed_tasks,
                "failed_tasks": self.failed_tasks,
                "cancelled_tasks": self.cancelled_tasks,
                "fast_path_tasks": self.fast_path_tasks,
                "switched_tasks": self.switched_tasks,
                "switch_attempts": self.switch_attempts,
                "switch_failures": self.switch_failures,
                "model_rejections": self.model_rejections,
                "failure_kind_counts": dict(self.failure_kind_counts),
            },
            "cms_state": {
                "active_model_id": _sanitize_label(active_model_id),
                "ollama_available": bool(ollama_available),
                "current_pressure_level": int(current_pressure_level),
                "max_context_tokens": int(max_context_tokens),
            },
            "latencies_ms": {
                "overall": _summarize_sample_group(samples),
                "by_reasoning_depth": {
                    depth: _summarize_sample_group(group) for depth, group in by_depth.items()
                },
                "by_routing_path": {
                    "resident": _summarize_sample_group(resident_samples),
                    "switched": _summarize_sample_group(switched_samples),
                    "fast_path": _summarize_sample_group(fast_path_samples),
                },
            },
            "throughput": {
                "provider_eval_tokens_per_sec": summarize_distribution(eval_rates, decimals=2),
            },
        }


def measure_ws_submission(
    ws: Any,
    prompt_text: str,
    *,
    prompt_type: str = "short",
    reasoning: str | None = None,
    iteration: int = 1,
) -> dict[str, Any]:
    """Execute one WebSocket request and compute separate client and backend monotonic metrics."""
    payload: dict[str, Any] = {
        "action": "submit",
        "prompt": prompt_text,
    }
    if reasoning is not None:
        payload["reasoning"] = reasoning

    client_t_submit_mono = time.perf_counter()
    ws.send_json(payload)

    client_t_ack_mono: float | None = None
    client_t_first_reasoning_mono: float | None = None
    client_t_first_answer_mono: float | None = None
    client_t_completed_mono: float | None = None
    backend_timings: dict[str, Any] = {}
    terminal_state = "unknown"
    executed_depth: str | None = None

    while True:
        evt = ws.receive_json()
        now_mono = time.perf_counter()
        event_type = evt.get("event")

        if event_type == "task_created":
            if client_t_ack_mono is None:
                client_t_ack_mono = now_mono
        elif event_type == "intent_classified":
            raw_depth = evt.get("reasoning_depth")
            executed_depth = str(raw_depth) if isinstance(raw_depth, str) else None
        elif event_type == "thinking":
            if client_t_first_reasoning_mono is None:
                client_t_first_reasoning_mono = now_mono
        elif event_type == "token":
            if client_t_first_answer_mono is None:
                client_t_first_answer_mono = now_mono
        elif event_type in ("completed", "failed", "cancelled"):
            client_t_completed_mono = now_mono
            terminal_state = str(event_type)
            raw_timings = evt.get("backend_timings_ms")
            if isinstance(raw_timings, dict):
                backend_timings = raw_timings
            break

    def _client_delta_ms(end_mono: float | None) -> float | None:
        if end_mono is None:
            return None
        return round(max(0.0, (end_mono - client_t_submit_mono) * 1000.0), 3)

    completion_s = (
        round(max(0.0, client_t_completed_mono - client_t_submit_mono), 4)
        if client_t_completed_mono is not None
        else 0.0
    )

    return {
        "iteration": iteration,
        "prompt_type": prompt_type,
        "reasoning_depth": executed_depth,
        "terminal_state": terminal_state,
        "client_clock_basis": "client_monotonic_perf_counter",
        "client_submit_to_ack_ms": _client_delta_ms(client_t_ack_mono),
        "client_submit_to_first_reasoning_ms": _client_delta_ms(client_t_first_reasoning_mono),
        "client_submit_to_first_answer_ms": _client_delta_ms(client_t_first_answer_mono),
        "client_submit_to_completion_s": completion_s,
        "backend_clock_basis": backend_timings.get("clock_basis", "backend_monotonic_perf_counter"),
        "backend_acceptance_to_provider_start_ms": backend_timings.get(
            "backend_acceptance_to_provider_start_ms"
        ),
        "backend_acceptance_to_answer_ms": backend_timings.get("backend_acceptance_to_answer_ms"),
        "provider_execution_to_first_answer_ms": backend_timings.get(
            "provider_execution_to_first_answer_ms"
        ),
        "backend_acceptance_to_first_reasoning_ms": backend_timings.get(
            "backend_acceptance_to_first_reasoning_ms"
        ),
        "provider_execution_to_first_reasoning_ms": backend_timings.get(
            "provider_execution_to_first_reasoning_ms"
        ),
        "backend_acceptance_to_completion_ms": backend_timings.get(
            "backend_acceptance_to_completion_ms"
        ),
        "provider_eval_tokens_per_sec": backend_timings.get("provider_eval_tokens_per_sec"),
    }


def summarize_e2e_runs(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Compute separated same-clock NIST percentile summaries across benchmark runs."""

    def _vals(field: str) -> list[float]:
        result: list[float] = []
        for run in runs:
            val = run.get(field)
            if not isinstance(val, bool) and isinstance(val, (int, float)):
                result.append(float(val))
        return result

    return {
        "schema_version": "5a.1",
        "num_runs": len(runs),
        "measurement_rules": {
            "cross_clock_subtraction": False,
            "client_answer_metric_scope": "ws_json_receipt_only_not_screen_display",
        },
        "client_metrics": {
            "client_submit_to_ack_ms": summarize_distribution(_vals("client_submit_to_ack_ms")),
            "client_submit_to_first_reasoning_ms": summarize_distribution(
                _vals("client_submit_to_first_reasoning_ms")
            ),
            "client_submit_to_first_answer_ms": summarize_distribution(
                _vals("client_submit_to_first_answer_ms")
            ),
            "client_submit_to_completion_s": summarize_distribution(
                _vals("client_submit_to_completion_s"), decimals=4
            ),
        },
        "backend_metrics": {
            "backend_acceptance_to_provider_start_ms": summarize_distribution(
                _vals("backend_acceptance_to_provider_start_ms")
            ),
            "backend_acceptance_to_answer_ms": summarize_distribution(
                _vals("backend_acceptance_to_answer_ms")
            ),
            "provider_execution_to_first_answer_ms": summarize_distribution(
                _vals("provider_execution_to_first_answer_ms")
            ),
            "backend_acceptance_to_first_reasoning_ms": summarize_distribution(
                _vals("backend_acceptance_to_first_reasoning_ms")
            ),
            "provider_execution_to_first_reasoning_ms": summarize_distribution(
                _vals("provider_execution_to_first_reasoning_ms")
            ),
            "backend_acceptance_to_completion_ms": summarize_distribution(
                _vals("backend_acceptance_to_completion_ms")
            ),
            "provider_eval_tokens_per_sec": summarize_distribution(
                _vals("provider_eval_tokens_per_sec"), decimals=2
            ),
        },
    }
