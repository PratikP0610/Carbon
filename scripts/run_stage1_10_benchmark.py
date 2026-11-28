"""Carbon Stage 1.10 Performance Investigation Benchmark Runner.

Executes controlled, warm and cold model benchmarks through the actual Carbon WebSocket pipeline.
Captures end-to-end WebSocket timings, token metrics, correctness, and Ollama engine metrics.
Saves machine-readable observations to docs/reports/stage1_10-performance-benchmark-results.json.
"""

import asyncio
import datetime
import json
import os
import re
import statistics
import time
from pathlib import Path

import websockets

TOKEN_PATH = Path("data/.carbon_token")
RESULTS_PATH = Path("docs/reports/stage1_10-performance-benchmark-results.json")
OLLAMA_LOG_PATH = Path("/opt/homebrew/var/log/ollama.log")

BENCHMARK_CATEGORIES = [
    {
        "id": "1_factual_instant",
        "category": "1. Simple factual",
        "mode": "instant",
        "prompt": "What does RAM stand for?",
        "expected_depth": "instant",
        "repetitions": 5,
        "validator": lambda text: "random access memory" in text.lower(),
    },
    {
        "id": "2_short_expl_auto",
        "category": "2. Short explanation",
        "mode": "auto",
        "prompt": "Explain what a CPU does in approximately 100 words.",
        "expected_depth": "standard",
        "repetitions": 5,
        "validator": lambda text: (
            len(text.split()) >= 40
            and any(
                w in text.lower() for w in ["fetch", "execute", "instruction", "brain", "process"]
            )
        ),
    },
    {
        "id": "3_structured_expl_std",
        "category": "3. Structured explanation",
        "mode": "standard",
        "prompt": "Explain what a CPU does using a heading, a numbered list, a Python code block and one mathematical expression.",
        "expected_depth": "standard",
        "repetitions": 5,
        "validator": lambda text: (
            ("#" in text or "CPU" in text)
            and ("```python" in text or "```" in text)
            and any(f"{i}." in text for i in range(1, 4))
        ),
    },
    {
        "id": "4_java_coding_std",
        "category": "4. Java coding problem",
        "mode": "standard",
        "prompt": "Write a Java method to find the second-largest distinct element in an integer array. Explain its time complexity.",
        "expected_depth": "standard",
        "repetitions": 5,
        "validator": lambda text: (
            "int" in text
            and ("second" in text.lower() or "secondlargest" in text.lower())
            and (
                "o(n)" in text.lower()
                or "o(n log n)" in text.lower()
                or "time complexity" in text.lower()
            )
        ),
    },
    {
        "id": "5_analytical_std",
        "category": "5. Analytical problem",
        "mode": "standard",
        "prompt": "Explain why binary search requires sorted input and demonstrate what can go wrong when the input is unsorted.",
        "expected_depth": "standard",
        "repetitions": 5,
        "validator": lambda text: (
            "binary search" in text.lower()
            and any(w in text.lower() for w in ["sorted", "order"])
            and any(w in text.lower() for w in ["wrong", "fail", "miss", "not found", "incorrect"])
        ),
    },
    {
        "id": "6_deep_reasoning_deep",
        "category": "6. Deep reasoning problem",
        "mode": "deep",
        "prompt": (
            "You have 8 billiard balls, one of which is slightly heavier than the other 7, "
            "which all weigh exactly the same. You have a balance scale. "
            "Explain step-by-step how to identify the heavier ball using the scale at most 2 times."
        ),
        "expected_depth": "deep",
        "repetitions": 5,
        "validator": lambda text: (
            ("3" in text and "2" in text)
            and any(w in text.lower() for w in ["scale", "balance", "weigh"])
            and any(w in text.lower() for w in ["step 1", "first weighing", "first weigh", "group"])
        ),
    },
]


def resolve_token() -> str:
    token = os.environ.get("CARBON_API_TOKEN")
    if token and token.strip():
        return token.strip()
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    return "dev-local-token-change-in-production"


def get_ollama_log_offset() -> int:
    if OLLAMA_LOG_PATH.exists():
        try:
            with open(OLLAMA_LOG_PATH, encoding="utf-8", errors="ignore") as f:
                f.seek(0, 2)
                return f.tell()
        except Exception:
            return 0
    return 0


def parse_ollama_log_delta(start_offset: int) -> dict:
    result = {
        "prompt_eval_duration_ms": None,
        "prompt_eval_count": None,
        "prompt_eval_tps": None,
        "eval_duration_ms": None,
        "eval_count": None,
        "eval_tps": None,
        "load_duration_ms": None,
    }
    if not OLLAMA_LOG_PATH.exists():
        return result

    try:
        with open(OLLAMA_LOG_PATH, encoding="utf-8", errors="ignore") as f:
            f.seek(start_offset)
            delta_text = f.read()

        prompt_match = re.search(
            r"prompt eval time =\s*([\d\.]+)\s*ms\s*/\s*(\d+)\s*tokens.*?([\d\.]+)\s*tokens per second",
            delta_text,
        )
        if prompt_match:
            result["prompt_eval_duration_ms"] = float(prompt_match.group(1))
            result["prompt_eval_count"] = int(prompt_match.group(2))
            result["prompt_eval_tps"] = float(prompt_match.group(3))

        eval_match = re.search(
            r"(?<!prompt )eval time =\s*([\d\.]+)\s*ms\s*/\s*(\d+)\s*tokens.*?([\d\.]+)\s*tokens per second",
            delta_text,
        )
        if eval_match:
            result["eval_duration_ms"] = float(eval_match.group(1))
            result["eval_count"] = int(eval_match.group(2))
            result["eval_tps"] = float(eval_match.group(3))

        load_matches = re.findall(
            r"time=([\d\-T:\.\+\_]+).*?loading model via llama-server", delta_text
        )
        loaded_matches = re.findall(r"time=([\d\-T:\.\+\_]+).*?msg=\"loaded runners\"", delta_text)
        if load_matches and loaded_matches:
            try:
                t0 = datetime.datetime.fromisoformat(load_matches[-1])
                t1 = datetime.datetime.fromisoformat(loaded_matches[-1])
                result["load_duration_ms"] = round((t1 - t0).total_seconds() * 1000.0, 2)
            except Exception:
                pass
    except Exception:
        pass

    return result


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    words = text.split()
    return max(len(words), int(len(text) / 3.8))


async def run_single_query(
    token: str,
    prompt: str,
    requested_mode: str,
    timeout_sec: float = 300.0,
) -> dict:
    uri = "ws://127.0.0.1:8765/ws/chat"
    headers = {"Authorization": f"Bearer {token}"}

    log_start_offset = get_ollama_log_offset()
    t_submit = time.perf_counter()

    async with websockets.connect(uri, additional_headers=headers) as ws:
        payload = {
            "action": "submit",
            "prompt": prompt,
            "reasoning": requested_mode,
            "conversation_id": f"bench110-{int(time.time() * 1000)}",
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
                msg_text = await asyncio.wait_for(ws.recv(), timeout=timeout_sec)
            except TimeoutError:
                error_msg = f"WebSocket response timeout ({timeout_sec}s exceeded)"
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
    classification_latency = max(0.0, t_classified - t_ack)
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

    # Capture ground-truth Ollama metrics from log delta
    await asyncio.sleep(0.05)
    ollama_stats = parse_ollama_log_delta(log_start_offset)

    return {
        "prompt": prompt,
        "requested_mode": requested_mode,
        "executed_mode": executed_mode,
        "classified_category": classified_category,
        "classified_depth": classified_depth,
        "error": error_msg,
        "durations": {
            "total_duration": round(total_duration, 3),
            "ack_latency": round(ack_latency, 4),
            "classification_latency": round(classification_latency, 4),
            "ttft": round(ttft, 3),
            "thinking_duration": round(thinking_duration, 3),
            "visible_duration": round(visible_duration, 3),
            "prompt_eval_duration_ms": ollama_stats.get("prompt_eval_duration_ms"),
            "eval_duration_ms": ollama_stats.get("eval_duration_ms"),
            "load_duration_ms": ollama_stats.get("load_duration_ms"),
        },
        "tokens": {
            "thinking_tokens": thinking_tokens,
            "visible_tokens": visible_tokens,
            "total_tokens": thinking_tokens + visible_tokens,
            "thinking_tps": round(thinking_tps, 2),
            "visible_tps": round(visible_tps, 2),
            "ollama_prompt_eval_tokens": ollama_stats.get("prompt_eval_count"),
            "ollama_prompt_eval_tps": ollama_stats.get("prompt_eval_tps"),
            "ollama_eval_tokens": ollama_stats.get("eval_count"),
            "ollama_eval_tps": ollama_stats.get("eval_tps"),
        },
        "output_preview": visible_text[:200] + ("..." if len(visible_text) > 200 else ""),
        "full_output": visible_text,
    }


def save_results(data: dict) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


async def main():
    token = resolve_token()
    print("=" * 70)
    print("CARBON STAGE 1.10 GENERATION SPEED BENCHMARK SUITE")
    print("=" * 70)
    print(f"Timestamp: {datetime.datetime.now().isoformat()}")
    print("Target WebSocket: ws://127.0.0.1:8765/ws/chat")
    print(f"Results File: {RESULTS_PATH}")

    benchmark_data = {
        "benchmark_id": "C-1.10-AUDIT",
        "timestamp": datetime.datetime.now().isoformat(),
        "platform": {
            "device": "MacBook Air M5",
            "memory": "16GB unified",
            "os": "macOS 27.0.1",
            "model": "qwen3.5:9b Q4_K_M",
            "context_length": 4096,
        },
        "cold_start": None,
        "categories": {},
    }

    # 1. Capture Cold-Start Benchmark (Run 0)
    print("\n>>> CAPTURING COLD-START OBSERVATION (Factual query)...")
    cold_result = await run_single_query(
        token=token,
        prompt="What does RAM stand for?",
        requested_mode="instant",
    )
    is_correct = "random access memory" in cold_result["full_output"].lower()
    cold_result["is_correct"] = is_correct
    del cold_result["full_output"]  # Keep raw JSON clean
    benchmark_data["cold_start"] = cold_result
    save_results(benchmark_data)
    print(
        f"  Cold Start: Total={cold_result['durations']['total_duration']}s | "
        f"TTFT={cold_result['durations']['ttft']}s | "
        f"Load={cold_result['durations']['load_duration_ms']}ms | "
        f"Correct={is_correct}"
    )

    # 2. Run Warm Benchmarks across all 6 categories (5 runs each)
    for cat in BENCHMARK_CATEGORIES:
        cat_id = cat["id"]
        cat_name = cat["category"]
        mode = cat["mode"]
        prompt = cat["prompt"]
        reps = cat["repetitions"]
        validator = cat["validator"]

        print(f"\n>>> BENCHMARK CATEGORY: {cat_name} (Mode: {mode.upper()}, Repetitions: {reps})")
        cat_observations = []

        for rep in range(1, reps + 1):
            print(f"  [Run {rep}/{reps}] Executing '{prompt[:45]}...' ...", end="", flush=True)
            res = await run_single_query(
                token=token,
                prompt=prompt,
                requested_mode=mode,
            )
            is_valid = validator(res["full_output"])
            res["is_correct"] = is_valid
            res_summary = {
                "rep": rep,
                "durations": res["durations"],
                "tokens": res["tokens"],
                "is_correct": is_valid,
                "error": res["error"],
                "output_preview": res["output_preview"],
            }
            cat_observations.append(res_summary)

            # Incremental save after every single run
            benchmark_data["categories"][cat_id] = {
                "category": cat_name,
                "mode": mode,
                "prompt": prompt,
                "expected_depth": cat["expected_depth"],
                "runs": cat_observations,
            }
            save_results(benchmark_data)

            print(
                f" Done. Total={res['durations']['total_duration']}s | "
                f"TTFT={res['durations']['ttft']}s | "
                f"VisTokens={res['tokens']['visible_tokens']} | "
                f"TPS={res['tokens'].get('ollama_eval_tps') or res['tokens']['visible_tps']} | "
                f"Correct={is_valid}"
            )

        # Compute summary statistics for category
        totals = [r["durations"]["total_duration"] for r in cat_observations if not r["error"]]
        ttfts = [r["durations"]["ttft"] for r in cat_observations if not r["error"]]
        vis_tokens = [r["tokens"]["visible_tokens"] for r in cat_observations if not r["error"]]
        thk_tokens = [r["tokens"]["thinking_tokens"] for r in cat_observations if not r["error"]]
        tps_list = [
            (r["tokens"]["ollama_eval_tps"] or r["tokens"]["visible_tps"])
            for r in cat_observations
            if not r["error"]
        ]

        if totals:
            totals_sorted = sorted(totals)
            p95_idx = int(len(totals_sorted) * 0.95)
            p95_total = totals_sorted[min(p95_idx, len(totals_sorted) - 1)]

            summary_stats = {
                "completed_runs": len(totals),
                "median_total_duration": round(statistics.median(totals), 3),
                "p95_total_duration": round(p95_total, 3),
                "min_total_duration": round(min(totals), 3),
                "max_total_duration": round(max(totals), 3),
                "median_ttft": round(statistics.median(ttfts), 3),
                "median_visible_tokens": round(statistics.median(vis_tokens), 1),
                "median_thinking_tokens": round(statistics.median(thk_tokens), 1),
                "median_eval_tps": round(statistics.median(tps_list), 2) if tps_list else 0.0,
                "all_correct": all(r["is_correct"] for r in cat_observations),
            }
            benchmark_data["categories"][cat_id]["summary"] = summary_stats
            save_results(benchmark_data)
            print(
                f"  --> Summary for {cat_name}: Median Total={summary_stats['median_total_duration']}s, P95={summary_stats['p95_total_duration']}s, Median TTFT={summary_stats['median_ttft']}s, Median TPS={summary_stats['median_eval_tps']}"
            )

    print("\n" + "=" * 70)
    print("STAGE 1.10 BENCHMARK SUITE COMPLETE")
    print(f"Results saved to: {RESULTS_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
