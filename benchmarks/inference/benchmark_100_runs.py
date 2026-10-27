"""Comprehensive 100-Iteration Statistical Benchmark for Carbon 1.0.0.

Measures:
1. Cold Start: Load duration from APFS to Metal GPU unified memory, total cold turnaround.
2. 100 Warm Iterations (50 Short Prompts, 50 Long Prompts).
3. Separation of:
   - First Reasoning Token (TTFR - Time to First Reasoning Token)
   - First User-Visible Response Token (TTFT_resp - Time to First Response Token)
4. Total Request Latency.
5. Generation throughput (eval tokens / sec).
6. Mach Kernel Memory Pressure sampling per iteration.
7. Outlier and failure tracking without silent exclusion.
8. Documented percentile method (NIST / Nearest Rank & Linear Interpolation).
"""

import json
import subprocess
import time
from pathlib import Path

import httpx

URL = "http://127.0.0.1:11434"
MODEL = "qwen3.5:9b"
OUTPUT_FILE = Path("benchmarks/inference/results_100_runs.json")

SHORT_PROMPTS = [
    "What is the capital of France? Answer in one word.",
    "Calculate 15 multiplied by 8.",
    "Name three primary colors.",
    "What is the chemical formula for table salt?",
    "Convert 100 degrees Celsius to Fahrenheit.",
    "What is the largest ocean on Earth?",
    "What is the square root of 144?",
    "Define the term 'bandwidth' in one sentence.",
    "What is the speed of light in vacuum in km/s?",
    "Who wrote the play Romeo and Juliet?",
]

LONG_PROMPTS = [
    "Explain the difference between synchronous and asynchronous execution in Python. Discuss how the asyncio event loop cooperatively schedules coroutines, how non-blocking I/O differs from multithreading, and when each concurrency model is appropriate.",
    "Describe the architectural mechanics of virtual memory in modern Unix-like operating systems. Explain page tables, the role of the MMU, how translation lookaside buffers (TLB) accelerate address translation, what occurs during a page fault, and how memory compression and swap disks are utilized under memory pressure.",
    "Analyze the architectural trade-offs between monolithic architectures and microservices. Detail aspects such as deployment complexity, data consistency, fault isolation, inter-service latency, network serialization overhead, and operational observability.",
    "Explain how SQLite implements ACID transactions using Write-Ahead Logging (WAL). Contrast WAL mode with traditional rollback journaling, covering concurrent readers, single-writer serialization, checkpointing strategies, and crash recovery semantics.",
    "Provide a detailed technical explanation of the Transformer attention mechanism. Explain query, key, and value vectors, scaled dot-product calculation, multi-head attention projection, and how KV caching reduces computational complexity during autoregressive token generation.",
]


def get_memory_pressure_level() -> int:
    try:
        out = subprocess.check_output(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            text=True,
        ).strip()
        return int(out)
    except Exception:
        return -1


def calculate_percentile(sorted_data: list[float], percentile: float) -> float:
    """Calculate percentile using standard linear interpolation method (numpy/NIST equivalent).

    percentile in range [0, 100].
    """
    if not sorted_data:
        return 0.0
    if len(sorted_data) == 1:
        return sorted_data[0]

    n = len(sorted_data)
    rank = (percentile / 100.0) * (n - 1)
    low_idx = int(rank)
    high_idx = min(low_idx + 1, n - 1)
    weight = rank - low_idx
    return sorted_data[low_idx] * (1.0 - weight) + sorted_data[high_idx] * weight


def run_benchmark(total_iterations: int = 100):
    client = httpx.Client(base_url=URL, timeout=180.0)
    print(f"=== CARBON 1.0.0 BENCHMARK: {total_iterations} ITERATIONS ===", flush=True)

    # 1. Measure Cold Start
    print("\n--- 1. Establishing Cold Start Baseline ---", flush=True)
    # Force unload
    client.post("/api/generate", json={"model": MODEL, "keep_alive": 0})
    time.sleep(2.5)

    cold_pressure = get_memory_pressure_level()
    t_cold_start = time.perf_counter()
    cold_first_reasoning = None
    cold_first_response = None
    cold_eval_count = 0
    cold_eval_dur = 0.0
    cold_load_dur = 0.0

    with client.stream(
        "POST",
        "/api/generate",
        json={"model": MODEL, "prompt": "System status check.", "stream": True},
    ) as stream:
        for line in stream.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            now = time.perf_counter()
            if cold_first_reasoning is None and chunk.get("thinking"):
                cold_first_reasoning = now - t_cold_start
            if cold_first_response is None and chunk.get("response"):
                cold_first_response = now - t_cold_start
            if chunk.get("done"):
                cold_eval_count = chunk.get("eval_count", 0)
                cold_eval_dur = chunk.get("eval_duration", 1) / 1e9
                cold_load_dur = chunk.get("load_duration", 0) / 1e9

    t_cold_end = time.perf_counter()
    cold_total_latency = t_cold_end - t_cold_start

    cold_result = {
        "load_duration_s": cold_load_dur,
        "total_latency_s": cold_total_latency,
        "first_reasoning_s": cold_first_reasoning or cold_total_latency,
        "first_response_s": cold_first_response,
        "eval_count": cold_eval_count,
        "eval_speed_tok_s": (cold_eval_count / cold_eval_dur) if cold_eval_dur > 0 else 0.0,
        "memory_pressure_level": cold_pressure,
    }
    print(
        f"Cold Start: Load = {cold_load_dur:.3f}s | Total = {cold_total_latency:.3f}s", flush=True
    )

    # 2. Warm Iterations
    print(f"\n--- 2. Executing {total_iterations} Warm Iterations ---", flush=True)
    warm_records = []
    failures = []

    for i in range(total_iterations):
        is_short = i % 2 == 0
        prompt_type = "short" if is_short else "long"
        prompt_list = SHORT_PROMPTS if is_short else LONG_PROMPTS
        prompt_text = prompt_list[(i // 2) % len(prompt_list)]

        pressure_before = get_memory_pressure_level()
        t0 = time.perf_counter()
        first_reasoning_time = None
        first_response_time = None
        thinking_toks = 0
        resp_toks = 0
        eval_count = 0
        eval_dur = 0.0

        try:
            with client.stream(
                "POST",
                "/api/generate",
                json={
                    "model": MODEL,
                    "prompt": prompt_text,
                    "stream": True,
                    "options": {"num_predict": 120 if is_short else 250},
                },
            ) as stream:
                for line in stream.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    now = time.perf_counter()
                    if chunk.get("thinking"):
                        thinking_toks += 1
                        if first_reasoning_time is None:
                            first_reasoning_time = now - t0
                    if chunk.get("response"):
                        resp_toks += 1
                        if first_response_time is None:
                            first_response_time = now - t0
                    if chunk.get("done"):
                        eval_count = chunk.get("eval_count", 0)
                        eval_dur = chunk.get("eval_duration", 1) / 1e9

            t1 = time.perf_counter()
            total_lat = t1 - t0
            speed = (eval_count / eval_dur) if eval_dur > 0 else 0.0

            record = {
                "iteration": i + 1,
                "prompt_type": prompt_type,
                "prompt_snippet": prompt_text[:60] + "...",
                "first_reasoning_ms": round((first_reasoning_time or total_lat) * 1000, 2),
                "first_response_ms": round((first_response_time * 1000), 2)
                if first_response_time
                else None,
                "total_latency_s": round(total_lat, 3),
                "thinking_tokens": thinking_toks,
                "response_tokens": resp_toks,
                "generation_speed_tok_s": round(speed, 2),
                "memory_pressure": pressure_before,
                "success": True,
            }
            warm_records.append(record)

            print(
                f"[{i + 1:03d}/{total_iterations}] {prompt_type.upper():5s} | "
                f"TTFR: {record['first_reasoning_ms']:6.1f}ms | "
                f"TTFT_resp: {str(record['first_response_ms']):>7s}ms | "
                f"Speed: {speed:4.1f} tok/s | "
                f"Total: {total_lat:5.2f}s | "
                f"RAM: L{pressure_before}",
                flush=True,
            )

        except Exception as exc:
            t1 = time.perf_counter()
            error_record = {
                "iteration": i + 1,
                "prompt_type": prompt_type,
                "prompt_snippet": prompt_text[:60] + "...",
                "error": str(exc),
                "total_latency_s": round(t1 - t0, 3),
                "success": False,
            }
            failures.append(error_record)
            print(f"[{i + 1:03d}/{total_iterations}] ERROR: {exc}", flush=True)

        time.sleep(0.1)

    # 3. Compute Statistical Summaries
    successful_runs = [r for r in warm_records if r["success"]]
    ttfr_values = sorted([r["first_reasoning_ms"] for r in successful_runs])

    # User-visible response token might be None if prompt finished during thinking
    ttft_resp_values = sorted(
        [r["first_response_ms"] for r in successful_runs if r["first_response_ms"] is not None]
    )
    speed_values = sorted([r["generation_speed_tok_s"] for r in successful_runs])
    total_lat_values = sorted([r["total_latency_s"] for r in successful_runs])

    # Short vs Long breakdown
    short_runs = [r for r in successful_runs if r["prompt_type"] == "short"]
    long_runs = [r for r in successful_runs if r["prompt_type"] == "long"]

    stats_summary = {
        "total_attempted": total_iterations,
        "successful_count": len(successful_runs),
        "failure_count": len(failures),
        "percentile_method": "NIST Linear Interpolation over sorted observations",
        "cold_start": cold_result,
        "first_reasoning_token_ms": {
            "min": round(min(ttfr_values), 2),
            "p50_median": round(calculate_percentile(ttfr_values, 50), 2),
            "p95": round(calculate_percentile(ttfr_values, 95), 2),
            "max": round(max(ttfr_values), 2),
        },
        "first_response_token_ms": {
            "count": len(ttft_resp_values),
            "min": round(min(ttft_resp_values), 2) if ttft_resp_values else None,
            "p50_median": round(calculate_percentile(ttft_resp_values, 50), 2)
            if ttft_resp_values
            else None,
            "p95": round(calculate_percentile(ttft_resp_values, 95), 2)
            if ttft_resp_values
            else None,
            "max": round(max(ttft_resp_values), 2) if ttft_resp_values else None,
        },
        "generation_speed_tok_s": {
            "min": round(min(speed_values), 2),
            "p50_median": round(calculate_percentile(speed_values, 50), 2),
            "p95": round(calculate_percentile(speed_values, 95), 2),
            "max": round(max(speed_values), 2),
        },
        "total_latency_s": {
            "min": round(min(total_lat_values), 2),
            "p50_median": round(calculate_percentile(total_lat_values, 50), 2),
            "p95": round(calculate_percentile(total_lat_values, 95), 2),
            "max": round(max(total_lat_values), 2),
        },
        "prompt_breakdown": {
            "short_prompts": {
                "count": len(short_runs),
                "ttfr_p50_ms": round(
                    calculate_percentile(sorted([r["first_reasoning_ms"] for r in short_runs]), 50),
                    2,
                ),
                "ttfr_p95_ms": round(
                    calculate_percentile(sorted([r["first_reasoning_ms"] for r in short_runs]), 95),
                    2,
                ),
                "total_latency_p50_s": round(
                    calculate_percentile(sorted([r["total_latency_s"] for r in short_runs]), 50), 2
                ),
            },
            "long_prompts": {
                "count": len(long_runs),
                "ttfr_p50_ms": round(
                    calculate_percentile(sorted([r["first_reasoning_ms"] for r in long_runs]), 50),
                    2,
                ),
                "ttfr_p95_ms": round(
                    calculate_percentile(sorted([r["first_reasoning_ms"] for r in long_runs]), 95),
                    2,
                ),
                "total_latency_p50_s": round(
                    calculate_percentile(sorted([r["total_latency_s"] for r in long_runs]), 50), 2
                ),
            },
        },
        "failures": failures,
    }

    full_payload = {
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hardware": "Apple Silicon ARM64 (MacBook Air M5, 10-core SoC, 16GB Unified RAM)",
            "os": "macOS 27.0.1",
            "model": MODEL,
            "provider": "Ollama v0.33.0",
            "total_iterations": total_iterations,
        },
        "summary": stats_summary,
        "raw_records": warm_records,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(full_payload, indent=2), encoding="utf-8")
    print(f"\nSaved raw data and statistical summary to {OUTPUT_FILE}", flush=True)

    print("\n========================================================", flush=True)
    print("      CARBON 1.0.0 STATISTICAL BENCHMARK (N=100)        ", flush=True)
    print("========================================================", flush=True)
    print(f"Cold Model Load Duration : {cold_load_dur:.3f} s")
    print(f"Cold Total Latency       : {cold_total_latency:.3f} s")
    print("--------------------------------------------------------", flush=True)
    print("First Reasoning Token (TTFR) [ms]:")
    print(
        f"  Min={stats_summary['first_reasoning_token_ms']['min']} | Median={stats_summary['first_reasoning_token_ms']['p50_median']} | P95={stats_summary['first_reasoning_token_ms']['p95']} | Max={stats_summary['first_reasoning_token_ms']['max']}"
    )
    print("First Response Token (TTFT_resp) [ms]:")
    print(
        f"  Min={stats_summary['first_response_token_ms']['min']} | Median={stats_summary['first_response_token_ms']['p50_median']} | P95={stats_summary['first_response_token_ms']['p95']} | Max={stats_summary['first_response_token_ms']['max']}"
    )
    print("Generation Speed [tokens/sec]:")
    print(
        f"  Min={stats_summary['generation_speed_tok_s']['min']} | Median={stats_summary['generation_speed_tok_s']['p50_median']} | P95={stats_summary['generation_speed_tok_s']['p95']} | Max={stats_summary['generation_speed_tok_s']['max']}"
    )
    print("Total Latency [seconds]:")
    print(
        f"  Min={stats_summary['total_latency_s']['min']} | Median={stats_summary['total_latency_s']['p50_median']} | P95={stats_summary['total_latency_s']['p95']} | Max={stats_summary['total_latency_s']['max']}"
    )
    print(f"Failures / Outliers: {len(failures)}")
    print("========================================================", flush=True)


if __name__ == "__main__":
    run_benchmark(100)
