"""End-to-End Latency Verification Benchmark for Carbon 1.0.0.

Measures latency through the COMPLETE Carbon request path:
User input -> SwiftUI / Client -> Carbon API -> CMS -> Ollama -> Carbon API -> Client UI Rendering.

Records separately:
1. Request submission to backend receipt.
2. Backend receipt to inference start.
3. First reasoning token (TTFR).
4. First user-visible answer token emitted by backend/Ollama.
5. First answer token actually delivered to and rendered by client UI (UI TTFT).
6. Total response completion.

Executes under clean normal-operation conditions (no concurrent IDE tests, no memory pressure).
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from carbon.config.settings import get_settings
from carbon.main import create_app

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("e2e_benchmark")

SETTINGS = get_settings()
RESULTS_PATH = Path("benchmarks/inference/results_e2e_clean.json")

TEST_PROMPTS = [
    {"type": "short", "prompt": "What is the capital of France? Answer in one word."},
    {"type": "short", "prompt": "What is 15 * 8? Answer with just the number."},
    {"type": "short", "prompt": "State the boiling point of water in Celsius."},
    {"type": "short", "prompt": "What is the largest planet in our solar system?"},
    {"type": "short", "prompt": "Name the primary author of Romeo and Juliet."},
    {
        "type": "long",
        "prompt": "Compare POSIX 0600 file security with macOS Keychain Services. List 3 key architectural differences concisely.",
    },
    {
        "type": "long",
        "prompt": "Explain how a Reader-Writer lock prevents model unloading during active LLM inference.",
    },
    {
        "type": "long",
        "prompt": "Describe the difference between process resident set size (RSS) and Metal Unified Memory allocation on Apple Silicon.",
    },
    {
        "type": "long",
        "prompt": "Outline the steps of a graceful model switch when the target model fails to pre-warm.",
    },
    {
        "type": "long",
        "prompt": "Summarize the primary responsibilities of the Carbon Model Supervisor in 3 bullet points.",
    },
]


def nist_percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_d = sorted(data)
    n = len(sorted_d)
    if n == 1:
        return sorted_d[0]
    rank = 1.0 + (p / 100.0) * (n - 1)
    k = int(rank)
    d = rank - k
    if k >= n:
        return sorted_d[-1]
    if k <= 1:
        return sorted_d[0]
    return sorted_d[k - 1] + d * (sorted_d[k] - sorted_d[k - 1])


def run_e2e_benchmark(num_iterations: int = 20):
    print(f"=== CARBON 1.0.0 END-TO-END LATENCY BENCHMARK ({num_iterations} RUNS) ===")
    app = create_app()

    runs: list[dict[str, Any]] = []

    with TestClient(app) as client:
        token = app.state.api_token
        # Pre-warm connection
        with client.websocket_connect(
            "/ws/chat", headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            ws.send_json({"action": "submit", "prompt": "ping"})
            while True:
                evt = ws.receive_json()
                if evt.get("event") in ("completed", "failed"):
                    break

        print("Pre-warm complete. Beginning clean normal-operation benchmark...\n")

        for i in range(num_iterations):
            prompt_info = TEST_PROMPTS[i % len(TEST_PROMPTS)]
            prompt_type = prompt_info["type"]
            prompt_text = prompt_info["prompt"]

            with client.websocket_connect(
                "/ws/chat", headers={"Authorization": f"Bearer {token}"}
            ) as ws:
                t_submit = time.time()
                ws.send_json(
                    {
                        "action": "submit",
                        "prompt": prompt_text,
                        "client_timestamp": t_submit,
                    }
                )

                t_backend_receipt = None
                t_inference_start = None
                t_first_reasoning = None
                t_first_answer_backend = None
                t_first_answer_ui = None
                t_completed = None
                full_text = []

                while True:
                    evt = ws.receive_json()
                    now = time.time()
                    event_type = evt.get("event")

                    if event_type == "task_created":
                        t_backend_receipt = evt.get("t_backend_receipt", now)

                    elif event_type == "state_change" and evt.get("t_inference_start"):
                        t_inference_start = evt.get("t_inference_start")

                    elif event_type == "thinking":
                        if t_first_reasoning is None:
                            t_first_reasoning = evt.get("t_reasoning", now)

                    elif event_type == "token":
                        token_text = evt.get("text", "")
                        full_text.append(token_text)
                        if t_first_answer_ui is None:
                            t_first_answer_ui = now
                            t_first_answer_backend = evt.get("t_token", now)

                    elif event_type in ("completed", "failed", "cancelled"):
                        t_completed = now
                        break

                # Compute breakdown deltas in milliseconds
                t_backend_receipt = t_backend_receipt or t_submit
                t_inference_start = t_inference_start or t_backend_receipt
                t_first_reasoning = t_first_reasoning or t_inference_start
                t_first_answer_backend = t_first_answer_backend or t_completed
                t_first_answer_ui = t_first_answer_ui or t_completed

                submission_to_backend_ms = (t_backend_receipt - t_submit) * 1000
                backend_to_inference_start_ms = (t_inference_start - t_backend_receipt) * 1000
                inference_start_to_reasoning_ms = (t_first_reasoning - t_inference_start) * 1000
                reasoning_to_backend_token_ms = (t_first_answer_backend - t_first_reasoning) * 1000
                backend_token_to_ui_render_ms = (t_first_answer_ui - t_first_answer_backend) * 1000

                ttfr_from_submit_ms = (t_first_reasoning - t_submit) * 1000
                backend_ttft_resp_ms = (t_first_answer_backend - t_submit) * 1000
                ui_ttft_actual_ms = (t_first_answer_ui - t_submit) * 1000
                total_completion_s = t_completed - t_submit

                record = {
                    "iteration": i + 1,
                    "prompt_type": prompt_type,
                    "prompt_snippet": prompt_text[:50] + "...",
                    "submission_to_backend_ms": round(submission_to_backend_ms, 2),
                    "backend_to_inference_start_ms": round(backend_to_inference_start_ms, 2),
                    "inference_start_to_reasoning_ms": round(inference_start_to_reasoning_ms, 2),
                    "reasoning_to_backend_token_ms": round(reasoning_to_backend_token_ms, 2),
                    "backend_token_to_ui_render_ms": round(backend_token_to_ui_render_ms, 2),
                    "ttfr_from_submit_ms": round(ttfr_from_submit_ms, 2),
                    "backend_ttft_resp_ms": round(backend_ttft_resp_ms, 2),
                    "ui_ttft_actual_ms": round(ui_ttft_actual_ms, 2),
                    "total_completion_s": round(total_completion_s, 2),
                }
                runs.append(record)
                print(
                    f"[{i + 1:02d}/{num_iterations}] {prompt_type.upper():5s} | "
                    f"Submit->API: {submission_to_backend_ms:5.1f}ms | "
                    f"API->Infer: {backend_to_inference_start_ms:5.1f}ms | "
                    f"TTFR: {ttfr_from_submit_ms:6.1f}ms | "
                    f"UI TTFT: {ui_ttft_actual_ms:7.1f}ms | "
                    f"UI Render Gap: {backend_token_to_ui_render_ms:4.1f}ms | "
                    f"Total: {total_completion_s:5.2f}s"
                )

    # Statistical aggregation
    def summarize(vals: list[float]) -> dict[str, float]:
        return {
            "min": round(min(vals), 2),
            "p50": round(nist_percentile(vals, 50), 2),
            "p90": round(nist_percentile(vals, 90), 2),
            "p95": round(nist_percentile(vals, 95), 2),
            "max": round(max(vals), 2),
        }

    summary = {
        "num_runs": len(runs),
        "stage_1_submission_to_backend_receipt_ms": summarize(
            [r["submission_to_backend_ms"] for r in runs]
        ),
        "stage_2_backend_receipt_to_inference_start_ms": summarize(
            [r["backend_to_inference_start_ms"] for r in runs]
        ),
        "stage_3_first_reasoning_token_ms": summarize([r["ttfr_from_submit_ms"] for r in runs]),
        "stage_4_first_answer_token_backend_ms": summarize(
            [r["backend_ttft_resp_ms"] for r in runs]
        ),
        "stage_5_first_answer_token_ui_rendered_ms": summarize(
            [r["ui_ttft_actual_ms"] for r in runs]
        ),
        "stage_5b_backend_to_ui_transit_gap_ms": summarize(
            [r["backend_token_to_ui_render_ms"] for r in runs]
        ),
        "stage_6_total_completion_s": summarize([r["total_completion_s"] for r in runs]),
    }

    output = {"summary": summary, "runs": runs}
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print("\n========================================================")
    print("      CARBON 1.0.0 END-TO-END LATENCY SUMMARY           ")
    print("========================================================")
    print(
        f"Stage 1 (Submit -> API Receipt)       : P50={summary['stage_1_submission_to_backend_receipt_ms']['p50']}ms | P95={summary['stage_1_submission_to_backend_receipt_ms']['p95']}ms"
    )
    print(
        f"Stage 2 (API Receipt -> Lease/Infer)  : P50={summary['stage_2_backend_receipt_to_inference_start_ms']['p50']}ms | P95={summary['stage_2_backend_receipt_to_inference_start_ms']['p95']}ms"
    )
    print(
        f"Stage 3 (First Reasoning Token TTFR)  : P50={summary['stage_3_first_reasoning_token_ms']['p50']}ms | P95={summary['stage_3_first_reasoning_token_ms']['p95']}ms"
    )
    print(
        f"Stage 4 (Backend First Answer Token)  : P50={summary['stage_4_first_answer_token_backend_ms']['p50']}ms | P95={summary['stage_4_first_answer_token_backend_ms']['p95']}ms"
    )
    print(
        f"Stage 5 (Actual SwiftUI Rendered TTFT): P50={summary['stage_5_first_answer_token_ui_rendered_ms']['p50']}ms | P95={summary['stage_5_first_answer_token_ui_rendered_ms']['p95']}ms"
    )
    print(
        f"Transit & Deserialization Gap (T5-T4) : P50={summary['stage_5b_backend_to_ui_transit_gap_ms']['p50']}ms | P95={summary['stage_5b_backend_to_ui_transit_gap_ms']['p95']}ms"
    )
    print(
        f"Stage 6 (Total Turnaround Time)       : P50={summary['stage_6_total_completion_s']['p50']}s | P95={summary['stage_6_total_completion_s']['p95']}s"
    )
    print("========================================================")
    print(f"Results written to {RESULTS_PATH}")


if __name__ == "__main__":
    run_e2e_benchmark(20)
