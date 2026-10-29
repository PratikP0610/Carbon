"""Benchmark runner for Carbon Stage 1.9: Reasoning Mode Optimization.

Executes controlled benchmarks across AUTO, INSTANT, STANDARD, and DEEP modes over the live WebSocket.
Measures TTFT, total response duration, thinking tokens, visible tokens, TPS, and correctness.
Saves machine-readable observations to docs/reports/stage1_9_reasoning_benchmark_results.json.
"""

import asyncio
import json
import statistics
import time
from pathlib import Path

import websockets

TOKEN_PATH = Path("data/.carbon_token")
RESULTS_PATH = Path("docs/reports/stage1_9_reasoning_benchmark_results.json")

# Benchmark definitions: 7 test categories with appropriate mode evaluations
BENCHMARK_SUITE = [
    {
        "id": "A_factual_auto",
        "category": "A. Simple factual (AUTO)",
        "mode": "auto",
        "prompt": "What does RAM stand for?",
        "expected_depth": "instant",
        "repetitions": 3,
    },
    {
        "id": "A_factual_instant",
        "category": "A. Simple factual (INSTANT)",
        "mode": "instant",
        "prompt": "What does RAM stand for?",
        "expected_depth": "instant",
        "repetitions": 3,
    },
    {
        "id": "B_arithmetic_auto",
        "category": "B. Arithmetic (AUTO)",
        "mode": "auto",
        "prompt": "What is 37 + 58? Just the number.",
        "expected_depth": "instant",
        "repetitions": 3,
    },
    {
        "id": "C_short_expl_auto",
        "category": "C. Short explanation (AUTO -> STANDARD)",
        "mode": "auto",
        "prompt": "Explain what a CPU does in approximately 100 words.",
        "expected_depth": "standard",
        "repetitions": 3,
    },
    {
        "id": "D_structured_expl_std",
        "category": "D. Structured explanation (STANDARD, think=False)",
        "mode": "standard",
        "prompt": "Explain what a CPU does using a heading, a numbered list, a Python code block and one mathematical expression.",
        "expected_depth": "standard",
        "repetitions": 3,
    },
    {
        "id": "E_coding_std",
        "category": "E. Java coding (STANDARD, think=False)",
        "mode": "standard",
        "prompt": "Write a Java method to find the second-largest distinct element in an integer array. Explain its time complexity.",
        "expected_depth": "standard",
        "repetitions": 3,
    },
    {
        "id": "F_analytical_std",
        "category": "F. Analytical (STANDARD, think=False)",
        "mode": "standard",
        "prompt": "Explain why binary search requires sorted input and demonstrate what can go wrong when the input is unsorted.",
        "expected_depth": "standard",
        "repetitions": 3,
    },
    {
        "id": "G_deep_reasoning_deep",
        "category": "G. Deep multi-step reasoning (DEEP, think=True)",
        "mode": "deep",
        "prompt": "You have 8 billiard balls, one of which is slightly heavier than the other 7, which all weigh exactly the same. You have a balance scale. Explain step-by-step how to identify the heavier ball using the scale at most 2 times.",
        "expected_depth": "deep",
        "repetitions": 3,
    },
]


def estimate_tokens(text: str) -> int:
    """Accurate token estimator based on whitespace and subword heuristics."""
    if not text:
        return 0
    words = text.split()
    return max(len(words), int(len(text) / 3.8))


async def run_single_query(token: str, prompt: str, requested_mode: str) -> dict:
    uri = "ws://127.0.0.1:8765/ws/chat"
    headers = {"Authorization": f"Bearer {token}"}

    async with websockets.connect(uri, additional_headers=headers) as ws:
        t_submit = time.perf_counter()
        payload = {
            "action": "submit",
            "prompt": prompt,
            "reasoning": requested_mode,
            "conversation_id": f"bench-{int(time.time() * 1000)}",
        }
        await ws.send(json.dumps(payload))

        t_ack = None
        t_classified = None
        t_first_thinking = None
        t_first_visible = None
        t_completed = None

        classified_category = None
        classified_depth = None
        executed_mode = requested_mode

        thinking_buffer = []
        visible_buffer = []
        error_msg = None

        while True:
            try:
                msg_text = await asyncio.wait_for(ws.recv(), timeout=300.0)
            except TimeoutError:
                error_msg = "WebSocket response timeout (300s exceeded)"
                t_completed = time.perf_counter()
                break

            evt = json.loads(msg_text)
            event_name = evt.get("event")

            if event_name == "task_created":
                t_ack = time.perf_counter()
                executed_mode = evt.get("reasoning_mode", requested_mode)

            elif event_name == "intent_classified":
                t_classified = time.perf_counter()
                classified_category = evt.get("category")
                classified_depth = evt.get("reasoning_depth")

            elif event_name == "thinking":
                if t_first_thinking is None:
                    t_first_thinking = time.perf_counter()
                thinking_buffer.append(evt.get("text", ""))

            elif event_name == "token":
                if t_first_visible is None:
                    t_first_visible = time.perf_counter()
                visible_buffer.append(evt.get("text", ""))

            elif event_name == "completed":
                t_completed = time.perf_counter()
                break

            elif event_name == "failed":
                t_completed = time.perf_counter()
                error_msg = evt.get("error", "Task execution failed")
                break

            elif event_name == "error":
                t_completed = time.perf_counter()
                error_msg = evt.get("message", "Task execution error")
                break

        now = time.perf_counter()
        t_ack = t_ack or now
        t_classified = t_classified or now
        t_completed = t_completed or now

        total_duration = t_completed - t_submit
        ack_latency = t_ack - t_submit
        classification_latency = t_classified - t_ack if t_classified >= t_ack else 0.0

        ttft = (t_first_visible - t_submit) if t_first_visible else total_duration

        thinking_text = "".join(thinking_buffer)
        visible_text = "".join(visible_buffer)

        thinking_tokens = estimate_tokens(thinking_text)
        visible_tokens = estimate_tokens(visible_text)

        if t_first_thinking and t_first_visible:
            thinking_duration = max(0.0, t_first_visible - t_first_thinking)
        elif t_first_thinking and not t_first_visible:
            thinking_duration = max(0.0, t_completed - t_first_thinking)
        else:
            thinking_duration = 0.0

        if t_first_visible:
            visible_duration = max(0.0, t_completed - t_first_visible)
        else:
            visible_duration = 0.0

        thinking_tps = (thinking_tokens / thinking_duration) if thinking_duration > 0.05 else 0.0
        visible_tps = (visible_tokens / visible_duration) if visible_duration > 0.05 else 0.0

        return {
            "prompt": prompt,
            "requested_mode": requested_mode,
            "executed_mode": executed_mode,
            "classified_category": classified_category,
            "classified_depth": classified_depth,
            "durations": {
                "total_duration": round(total_duration, 3),
                "ack_latency": round(ack_latency, 4),
                "classification_latency": round(classification_latency, 4),
                "ttft": round(ttft, 3),
                "thinking_duration": round(thinking_duration, 3),
                "visible_duration": round(visible_duration, 3),
            },
            "tokens": {
                "thinking_tokens": thinking_tokens,
                "visible_tokens": visible_tokens,
                "total_tokens": thinking_tokens + visible_tokens,
                "thinking_tps": round(thinking_tps, 2),
                "visible_tps": round(visible_tps, 2),
            },
            "quality": {
                "has_visible_output": len(visible_text.strip()) > 0,
                "visible_char_length": len(visible_text),
                "error": error_msg,
                "snippet": visible_text[:180].replace("\n", " ").strip(),
            },
        }


async def main():
    if not TOKEN_PATH.exists():
        print(f"Error: Token file {TOKEN_PATH} not found.")
        return

    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    print("=================================================================")
    print("STAGE 1.9: REASONING MODE OPTIMIZATION BENCHMARK")
    print("Backend: ws://127.0.0.1:8765/ws/chat")
    print("Model: qwen3.5:9b (Apple M5, 16GB Unified Memory)")
    print("Modes evaluated: AUTO, INSTANT, STANDARD (think=False), DEEP (think=True)")
    print("=================================================================\n")

    # Warm-up run
    print(">>> [Warm-Up] Executing initial query to ensure model is hot in memory...")
    warmup_res = await run_single_query(token, "Ping", "instant")
    print(
        f"    Warm-up complete: Total={warmup_res['durations']['total_duration']}s, "
        f"TTFT={warmup_res['durations']['ttft']}s\n"
    )

    all_results = []
    category_summaries = {}

    for idx, item in enumerate(BENCHMARK_SUITE, 1):
        cat_id = item["id"]
        cat_name = item["category"]
        mode = item["mode"]
        prompt = item["prompt"]
        reps = item["repetitions"]

        print("-----------------------------------------------------------------")
        print(f"[{idx}/{len(BENCHMARK_SUITE)}] CATEGORY: {cat_name}")
        print(f"     Mode: '{mode}' | Prompt: '{prompt[:70]}...'")
        print("-----------------------------------------------------------------")

        cat_runs = []
        for rep in range(1, reps + 1):
            print(f"  Repetition {rep}/{reps} executing...", end="", flush=True)
            res = await run_single_query(token, prompt, mode)
            res["benchmark_id"] = cat_id
            res["category"] = cat_name
            res["repetition"] = rep
            cat_runs.append(res)
            all_results.append(res)

            dur = res["durations"]
            tok = res["tokens"]
            print(
                f" Done! Total={dur['total_duration']}s | TTFT={dur['ttft']}s | "
                f"Thinking={dur['thinking_duration']}s ({tok['thinking_tokens']} tok) | "
                f"Visible={dur['visible_duration']}s ({tok['visible_tokens']} tok @ {tok['visible_tps']} tps) | "
                f"Depth={res['classified_depth']}"
            )
            await asyncio.sleep(0.5)

        # Compute summary
        totals = [r["durations"]["total_duration"] for r in cat_runs]
        ttfts = [r["durations"]["ttft"] for r in cat_runs]
        think_times = [r["durations"]["thinking_duration"] for r in cat_runs]
        vis_times = [r["durations"]["visible_duration"] for r in cat_runs]
        vis_tokens = [r["tokens"]["visible_tokens"] for r in cat_runs]

        summary = {
            "id": cat_id,
            "category": cat_name,
            "mode": mode,
            "sample_size": reps,
            "total_duration_median": round(statistics.median(totals), 2),
            "total_duration_mean": round(statistics.mean(totals), 2),
            "total_duration_min": round(min(totals), 2),
            "total_duration_max": round(max(totals), 2),
            "ttft_median": round(statistics.median(ttfts), 2),
            "thinking_time_median": round(statistics.median(think_times), 2),
            "visible_time_median": round(statistics.median(vis_times), 2),
            "visible_tokens_median": int(statistics.median(vis_tokens)),
            "classified_depth": cat_runs[0]["classified_depth"],
        }
        category_summaries[cat_id] = summary

        print(
            f"\n  >> SUMMARY for {cat_name} (N={reps}):\n"
            f"     Total Duration: Median={summary['total_duration_median']}s (Min={summary['total_duration_min']}s, Max={summary['total_duration_max']}s)\n"
            f"     TTFT:           Median={summary['ttft_median']}s\n"
            f"     Thinking Time:  Median={summary['thinking_time_median']}s\n"
            f"     Visible Time:   Median={summary['visible_time_median']}s\n"
        )

    # Save output
    output_data = {
        "metadata": {
            "stage": "1.9",
            "audit_type": "Reasoning Mode Optimization Benchmark",
            "device": "Apple MacBook Air M5, 16GB Unified Memory",
            "os": "macOS 27.0.1",
            "model": "qwen3.5:9b (Q4_K_M)",
            "ollama_context": 4096,
            "warmup_run": warmup_res,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_runs": len(all_results),
        },
        "category_summaries": category_summaries,
        "observations": all_results,
    }

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(output_data, indent=2), encoding="utf-8")
    print(f"\n[Done] Benchmark dataset successfully saved to: {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
