"""Benchmark runner for Carbon Stage 1.4: Reasoning Performance & UI Rendering Reliability.

Executes 5 benchmark categories with 5 warm repetitions each over the real Carbon WebSocket.
Measures TTFR, TTFT, Total Time, Thinking Tokens, and Visible Tokens.
"""

import asyncio
import json
import statistics
import time
from pathlib import Path

import websockets

TOKEN_PATH = Path("data/.carbon_token")

BENCHMARK_PROMPTS = [
    {
        "category": "1. Simple Factual",
        "prompt": "What is the capital of Japan?",
        "expected_mode": "INSTANT",
    },
    {
        "category": "2. Arithmetic",
        "prompt": "What is 37 + 58?",
        "expected_mode": "INSTANT",
    },
    {
        "category": "3. Standard Explanation",
        "prompt": "Explain why Python is interpreted.",
        "expected_mode": "STANDARD",
    },
    {
        "category": "4. Standard Coding",
        "prompt": "Write a Java method to find the second largest distinct element in an integer array.",
        "expected_mode": "STANDARD",
    },
    {
        "category": "5. Deep Reasoning",
        "prompt": "Solve this eight-ball balance-scale problem: You have 8 identical-looking balls, one of which is slightly heavier than the others. Using a balance scale only twice, how do you find the heavier ball?",
        "expected_mode": "DEEP",
    },
]


async def run_single_query(token: str, prompt: str) -> dict:
    uri = "ws://127.0.0.1:8765/ws/chat"
    headers = {"Authorization": f"Bearer {token}"}

    async with websockets.connect(uri, additional_headers=headers) as ws:
        t_submit = time.perf_counter()
        await ws.send(json.dumps({"action": "submit", "prompt": prompt}))

        t_first_receipt = None
        t_first_token = None
        t_completed = None
        thinking_chunks = 0
        visible_tokens = []
        reasoning_depth = None
        task_category = None

        while True:
            msg_str = await ws.recv()
            t_now = time.perf_counter()
            msg = json.loads(msg_str)
            event = msg.get("event")

            if t_first_receipt is None:
                t_first_receipt = t_now

            if event == "intent_classified":
                reasoning_depth = msg.get("reasoning_depth")
                task_category = msg.get("category")
            elif event == "thinking":
                thinking_chunks += 1
            elif event == "token":
                if t_first_token is None:
                    t_first_token = t_now
                visible_tokens.append(msg.get("text", ""))
            elif event in ("completed", "failed", "cancelled"):
                t_completed = t_now
                break

        full_answer = "".join(visible_tokens)
        ttfr = (t_first_receipt - t_submit) if t_first_receipt else 0.0
        ttft = (t_first_token - t_submit) if t_first_token else 0.0
        total_time = (t_completed - t_submit) if t_completed else 0.0

        return {
            "prompt": prompt,
            "category": task_category,
            "reasoning_depth": reasoning_depth,
            "ttfr_s": ttfr,
            "ttft_s": ttft,
            "total_time_s": total_time,
            "thinking_chunks": thinking_chunks,
            "visible_token_count": len(visible_tokens),
            "answer_preview": full_answer[:120].strip() + ("..." if len(full_answer) > 120 else ""),
            "full_answer": full_answer,
        }


def calc_p95(values: list[float]) -> float:
    if not values:
        return 0.0
    values_sorted = sorted(values)
    idx = int(0.95 * len(values_sorted))
    idx = min(idx, len(values_sorted) - 1)
    return values_sorted[idx]


async def main():
    if not TOKEN_PATH.exists():
        print("Error: token file not found at data/.carbon_token")
        return

    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    print("Token loaded successfully. Starting Stage 1.4 Benchmark Suite...")

    all_results = {}

    for item in BENCHMARK_PROMPTS:
        cat_name = item["category"]
        prompt = item["prompt"]
        print("\n========================================================")
        print(f"Benchmarking: {cat_name}")
        print(f"Prompt: {prompt}")
        print("========================================================")

        # 1. Warm-up run
        print("  Running warm-up iteration...")
        warmup = await run_single_query(token, prompt)
        print(f"  Warm-up complete: mode={warmup['reasoning_depth']}, total={warmup['total_time_s']:.2f}s")

        # 2. 5 timed repetitions
        runs = []
        for rep in range(1, 6):
            print(f"  Repetition {rep}/5 running...", end="", flush=True)
            res = await run_single_query(token, prompt)
            runs.append(res)
            print(f" done. TTFR: {res['ttfr_s']*1000:.1f}ms, TTFT: {res['ttft_s']:.2f}s, Total: {res['total_time_s']:.2f}s, Tokens: {res['visible_token_count']}")

        # Compute summary stats
        ttfrs = [r["ttfr_s"] for r in runs]
        ttfts = [r["ttft_s"] for r in runs]
        totals = [r["total_time_s"] for r in runs]
        think_counts = [r["thinking_chunks"] for r in runs]
        vis_counts = [r["visible_token_count"] for r in runs]

        summary = {
            "mode": runs[0]["reasoning_depth"],
            "category": runs[0]["category"],
            "sample_answer": runs[0]["answer_preview"],
            "ttfr_median_ms": statistics.median(ttfrs) * 1000,
            "ttfr_p95_ms": calc_p95(ttfrs) * 1000,
            "ttft_median_s": statistics.median(ttfts),
            "ttft_p95_s": calc_p95(ttfts),
            "total_median_s": statistics.median(totals),
            "total_p95_s": calc_p95(totals),
            "total_min_s": min(totals),
            "total_max_s": max(totals),
            "thinking_chunks_median": statistics.median(think_counts),
            "visible_tokens_median": statistics.median(vis_counts),
            "runs": runs,
        }
        all_results[cat_name] = summary

    # Save benchmark results
    out_path = Path("docs/reports/stage1_4_benchmark_results.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nAll benchmark results successfully written to {out_path}")

    # Print summary table
    print("\n" + "=" * 90)
    print(f"{'Category':<25} | {'Mode':<8} | {'TTFR (med)':<10} | {'TTFT (med)':<10} | {'Total (med)':<11} | {'Total (P95)':<11}")
    print("-" * 90)
    for cat_name, s in all_results.items():
        print(f"{cat_name:<25} | {s['mode']:<8} | {s['ttfr_median_ms']:.1f} ms    | {s['ttft_median_s']:.2f} s     | {s['total_median_s']:.2f} s     | {s['total_p95_s']:.2f} s")
    print("=" * 90)


if __name__ == "__main__":
    asyncio.run(main())
