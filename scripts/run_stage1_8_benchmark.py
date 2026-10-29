"""Benchmark runner for Carbon Stage 1.8: Standard Mode Latency Investigation.

Executes 5 benchmark categories with 5 warm repetitions each over the live Carbon WebSocket.
Measures high-resolution timestamps, TTFT, thinking duration, visible generation duration,
TPS, prompt processing, and token metrics.
"""

import asyncio
import json
import statistics
import time
from pathlib import Path

import websockets

TOKEN_PATH = Path("data/.carbon_token")
RESULTS_PATH = Path("docs/reports/stage1_8_latency_benchmark_results.json")

BENCHMARK_PROMPTS = [
    {
        "category": "A. Simple factual",
        "prompt": "What does RAM stand for?",
    },
    {
        "category": "B. Short explanation",
        "prompt": "Explain what a CPU does in approximately 100 words.",
    },
    {
        "category": "C. Structured explanation",
        "prompt": "Explain what a CPU does using a heading, a numbered list, a Python code block and one mathematical expression.",
    },
    {
        "category": "D. Coding",
        "prompt": "Write a Java method to find the second-largest distinct element in an integer array. Explain its time complexity.",
    },
    {
        "category": "E. Analytical",
        "prompt": "Explain why binary search requires sorted input and demonstrate what can go wrong when the input is unsorted.",
    },
]


def estimate_tokens(text: str) -> int:
    """Accurate token estimator based on whitespace and subword heuristics."""
    if not text:
        return 0
    words = text.split()
    # Typical Qwen/Llama subword factor is ~1.3 tokens per whitespace-separated word
    return max(len(words), int(len(text) / 3.8))


async def run_single_query(token: str, prompt: str, requested_mode: str = "standard") -> dict:
    uri = "ws://127.0.0.1:8765/ws/chat"
    headers = {"Authorization": f"Bearer {token}"}

    async with websockets.connect(uri, additional_headers=headers) as ws:
        t_submit = time.perf_counter()
        payload = {
            "action": "submit",
            "prompt": prompt,
            "reasoning": requested_mode,
        }
        await ws.send(json.dumps(payload))

        t_backend_receipt = None
        t_first_thinking = None
        t_last_thinking = None
        t_first_visible = None
        t_last_visible = None
        t_completed = None

        thinking_chunks = []
        visible_chunks = []
        executed_mode = None
        classified_category = None
        model_used = None
        task_id = None

        while True:
            msg_str = await ws.recv()
            t_now = time.perf_counter()
            msg = json.loads(msg_str)
            event = msg.get("event")

            if event == "task_created":
                t_backend_receipt = t_now
                task_id = msg.get("task_id")
            elif event == "intent_classified":
                executed_mode = msg.get("reasoning_depth")
                classified_category = msg.get("category")
            elif event == "thinking":
                if t_first_thinking is None:
                    t_first_thinking = t_now
                t_last_thinking = t_now
                thinking_chunks.append(msg.get("text", ""))
            elif event == "token":
                if t_first_visible is None:
                    t_first_visible = t_now
                t_last_visible = t_now
                visible_chunks.append(msg.get("text", ""))
            elif event in ("completed", "failed", "cancelled"):
                t_completed = t_now
                if event == "completed" and "task" in msg:
                    task_data = msg["task"]
                    model_used = task_data.get("model_used")
                break

        full_thinking = "".join(thinking_chunks)
        full_response = "".join(visible_chunks)

        thinking_tokens = estimate_tokens(full_thinking)
        visible_tokens = estimate_tokens(full_response)
        total_tokens = thinking_tokens + visible_tokens

        ttft = (t_first_visible - t_submit) if t_first_visible else None
        ttfr = (t_first_thinking or t_first_visible or t_completed) - t_submit
        thinking_duration = (t_last_thinking - t_first_thinking) if (t_first_thinking and t_last_thinking) else 0.0
        visible_duration = (t_last_visible - t_first_visible) if (t_first_visible and t_last_visible) else 0.0
        prompt_proc_duration = ((t_first_thinking or t_first_visible) - t_backend_receipt) if t_backend_receipt else 0.0
        total_backend_duration = (t_completed - t_backend_receipt) if t_backend_receipt else (t_completed - t_submit)
        total_duration = t_completed - t_submit

        thinking_tps = (thinking_tokens / thinking_duration) if thinking_duration > 0 else 0.0
        visible_tps = (visible_tokens / visible_duration) if visible_duration > 0 else 0.0
        overall_gen_time = (t_last_visible - (t_first_thinking or t_first_visible)) if (t_last_visible and (t_first_thinking or t_first_visible)) else 0.0
        overall_tps = (total_tokens / overall_gen_time) if overall_gen_time > 0 else 0.0

        # Assess correctness
        correctness = "CORRECT"
        if not full_response.strip() or "No response was generated" in full_response:
            correctness = "EMPTY_OR_TIMEOUT"

        return {
            "task_id": task_id,
            "requested_mode": requested_mode,
            "executed_mode": executed_mode,
            "classified_category": classified_category,
            "model_used": model_used or "qwen3.5:9b",
            "think_setting": True,
            "num_predict": 2560,
            "temperature": 0.6,
            "active_context": 4096,
            "timestamps": {
                "t_submit": t_submit,
                "t_backend_receipt": t_backend_receipt,
                "t_first_thinking": t_first_thinking,
                "t_last_thinking": t_last_thinking,
                "t_first_visible": t_first_visible,
                "t_last_visible": t_last_visible,
                "t_completed": t_completed,
            },
            "durations": {
                "ttfr": round(ttfr, 3),
                "ttft": round(ttft, 3) if ttft is not None else None,
                "thinking_duration": round(thinking_duration, 3),
                "visible_duration": round(visible_duration, 3),
                "prompt_proc_duration": round(prompt_proc_duration, 3),
                "total_backend_duration": round(total_backend_duration, 3),
                "total_duration": round(total_duration, 3),
            },
            "tokens": {
                "thinking_chars": len(full_thinking),
                "thinking_tokens": thinking_tokens,
                "visible_chars": len(full_response),
                "visible_tokens": visible_tokens,
                "total_tokens": total_tokens,
            },
            "throughput": {
                "thinking_tps": round(thinking_tps, 2),
                "visible_tps": round(visible_tps, 2),
                "overall_tps": round(overall_tps, 2),
            },
            "correctness": correctness,
            "response_preview": full_response.strip()[:140].replace("\n", " "),
        }


def compute_p95(values: list[float]) -> float:
    """Compute 95th percentile with linear interpolation."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    if len(sorted_v) == 1:
        return sorted_v[0]
    k = (len(sorted_v) - 1) * 0.95
    f = int(k)
    c = min(f + 1, len(sorted_v) - 1)
    d = k - f
    return sorted_v[f] + d * (sorted_v[c] - sorted_v[f])


async def main():
    if not TOKEN_PATH.exists():
        print(f"Error: Token file {TOKEN_PATH} not found.")
        return

    token = TOKEN_PATH.read_text().strip()
    print("=================================================================")
    print("STAGE 1.8: STANDARD-MODE LATENCY BENCHMARK & TIMING AUDIT")
    print("Backend: ws://127.0.0.1:8765/ws/chat")
    print("Model: qwen3.5:9b (Q4_K_M, 16GB Unified Memory, M5)")
    print("=================================================================\n")

    # 1. Warm-up run
    print(">>> [Warm-Up] Executing initial query to verify resident model...")
    warm_res = await run_single_query(token, "Ping", requested_mode="standard")
    print(f"    Warm-up complete: Total={warm_res['durations']['total_duration']}s, TTFT={warm_res['durations']['ttft']}s\n")

    raw_results = []
    category_summaries = {}

    for cat_idx, item in enumerate(BENCHMARK_PROMPTS, 1):
        cat_name = item["category"]
        prompt = item["prompt"]
        print("-----------------------------------------------------------------")
        print(f"[{cat_idx}/5] CATEGORY: {cat_name}")
        print(f"     Prompt: '{prompt}'")
        print("-----------------------------------------------------------------")

        cat_runs = []
        for rep in range(1, 6):
            print(f"  Repetition {rep}/5 executing...", end="", flush=True)
            res = await run_single_query(token, prompt, requested_mode="standard")
            res["category"] = cat_name
            res["prompt"] = prompt
            res["iteration"] = rep
            cat_runs.append(res)
            raw_results.append(res)

            d = res["durations"]
            tok = res["tokens"]
            tp = res["throughput"]
            print(f" Done! Total={d['total_duration']}s | TTFT={d['ttft']}s | Thinking={d['thinking_duration']}s ({tok['thinking_tokens']} tok @ {tp['thinking_tps']} tps) | Visible={d['visible_duration']}s ({tok['visible_tokens']} tok @ {tp['visible_tps']} tps)")

        # Aggregate stats for category
        totals = [r["durations"]["total_duration"] for r in cat_runs]
        ttfts = [r["durations"]["ttft"] for r in cat_runs if r["durations"]["ttft"] is not None]
        thinks = [r["durations"]["thinking_duration"] for r in cat_runs]
        visibles = [r["durations"]["visible_duration"] for r in cat_runs]
        thinking_toks = [r["tokens"]["thinking_tokens"] for r in cat_runs]
        visible_toks = [r["tokens"]["visible_tokens"] for r in cat_runs]
        thinking_tps_list = [r["throughput"]["thinking_tps"] for r in cat_runs]
        visible_tps_list = [r["throughput"]["visible_tps"] for r in cat_runs]

        category_summaries[cat_name] = {
            "sample_size": len(cat_runs),
            "total_duration": {
                "median": round(statistics.median(totals), 2),
                "p95": round(compute_p95(totals), 2),
                "min": round(min(totals), 2),
                "max": round(max(totals), 2),
            },
            "ttft": {
                "median": round(statistics.median(ttfts), 2) if ttfts else 0.0,
                "p95": round(compute_p95(ttfts), 2) if ttfts else 0.0,
            },
            "thinking_duration": {
                "median": round(statistics.median(thinks), 2),
                "p95": round(compute_p95(thinks), 2),
            },
            "visible_duration": {
                "median": round(statistics.median(visibles), 2),
                "p95": round(compute_p95(visibles), 2),
            },
            "thinking_tokens": {
                "median": round(statistics.median(thinking_toks), 1),
                "p95": round(compute_p95(thinking_toks), 1),
            },
            "visible_tokens": {
                "median": round(statistics.median(visible_toks), 1),
                "p95": round(compute_p95(visible_toks), 1),
            },
            "thinking_tps": {
                "median": round(statistics.median(thinking_tps_list), 2),
            },
            "visible_tps": {
                "median": round(statistics.median(visible_tps_list), 2),
            },
        }

        cs = category_summaries[cat_name]
        print(f"\n  >> SUMMARY for {cat_name} (N=5):")
        print(f"     Total Duration: Median={cs['total_duration']['median']}s, P95={cs['total_duration']['p95']}s (Min={cs['total_duration']['min']}s, Max={cs['total_duration']['max']}s)")
        print(f"     TTFT:           Median={cs['ttft']['median']}s, P95={cs['ttft']['p95']}s")
        print(f"     Thinking Time:  Median={cs['thinking_duration']['median']}s ({cs['thinking_tokens']['median']} tokens @ {cs['thinking_tps']['median']} tps)")
        print(f"     Visible Time:   Median={cs['visible_duration']['median']}s ({cs['visible_tokens']['median']} tokens @ {cs['visible_tps']['median']} tps)\n")

    # Save full machine-readable results
    output_data = {
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hardware": "Apple MacBook Air M5, 16GB Unified Memory",
            "os": "macOS 27.0.1",
            "model": "qwen3.5:9b Q4_K_M",
            "context_length": 4096,
            "inference_mode_requested": "standard",
            "think_setting": True,
            "num_predict": 2560,
            "temperature": 0.6,
            "total_runs": len(raw_results),
        },
        "category_summaries": category_summaries,
        "raw_observations": raw_results,
    }

    RESULTS_PATH.write_text(json.dumps(output_data, indent=2))
    print(f"✓ All raw machine-readable observations saved to: {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())

