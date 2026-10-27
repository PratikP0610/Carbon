"""Statistical benchmark runner for local inference performance.

Measures Cold Load, Warm TTFT (Min, Median, P95, Max), Generation Speed (tokens/sec),
and Total Latency across repeated iterations under documented test conditions.
"""

import json
import statistics
import time

import httpx

URL = "http://127.0.0.1:11434"
MODEL = "qwen3.5:9b"


def run_benchmark(iterations: int = 5):
    client = httpx.Client(base_url=URL, timeout=120.0)

    # 1. Measure Cold Load State
    print("--- 1. Establishing Cold State ---", flush=True)
    client.post("/api/generate", json={"model": MODEL, "keep_alive": 0})
    time.sleep(2)

    print("--- 2. Executing Cold Load Inference ---", flush=True)
    t0 = time.perf_counter()
    cold_res = client.post(
        "/api/generate", json={"model": MODEL, "prompt": "Status", "stream": False}
    )
    t_cold_elapsed = time.perf_counter() - t0
    cold_data = cold_res.json()
    cold_load_dur = cold_data.get("load_duration", 0) / 1e9
    print(
        f"Cold Total Elapsed: {t_cold_elapsed:.3f}s | Internal Load Duration: {cold_load_dur:.3f}s",
        flush=True,
    )

    # 2. Warm Iterations
    print(f"\n--- 3. Running Warm Test Iterations (N = {iterations}) ---", flush=True)
    ttft_list = []
    speed_list = []
    total_lat_list = []

    for i in range(iterations):
        prompt = f"Iteration {i + 1}: What is 2 + 2? Answer in one short sentence."
        t_start = time.perf_counter()
        first_token_time = None
        eval_count = 0
        eval_dur = 0.0

        with client.stream(
            "POST", "/api/generate", json={"model": MODEL, "prompt": prompt, "stream": True}
        ) as stream:
            for line in stream.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if first_token_time is None and (chunk.get("response") or chunk.get("thinking")):
                    first_token_time = time.perf_counter()
                if chunk.get("done", False):
                    eval_count = chunk.get("eval_count", 0)
                    eval_dur = chunk.get("eval_duration", 1) / 1e9

        t_end = time.perf_counter()
        ttft = (first_token_time - t_start) if first_token_time else (t_end - t_start)
        total_lat = t_end - t_start
        speed = (eval_count / eval_dur) if eval_dur > 0 else 0.0

        ttft_list.append(ttft)
        speed_list.append(speed)
        total_lat_list.append(total_lat)

        print(
            f"Iter {i + 1:02d}: TTFT = {ttft * 1000:.1f}ms | Speed = {speed:.1f} tok/s | Total = {total_lat:.2f}s",
            flush=True,
        )
        time.sleep(0.5)

    def compute_stats(arr):
        s = sorted(arr)
        p95_val = s[min(int(0.95 * len(s)), len(s) - 1)]
        return {
            "min": min(s),
            "median": statistics.median(s),
            "p95": p95_val,
            "max": max(s),
        }

    ttft_stats = compute_stats(ttft_list)
    speed_stats = compute_stats(speed_list)
    lat_stats = compute_stats(total_lat_list)

    print("\n==================================================", flush=True)
    print(f"      STATISTICAL BENCHMARK RESULTS (N={iterations})        ", flush=True)
    print("==================================================", flush=True)
    print(f"Cold Model Load Duration : {cold_load_dur:.3f} s")
    print(f"Cold Total Latency       : {t_cold_elapsed:.3f} s")
    print(
        "Warm TTFT (ms)           : Min={:.1f} | Median={:.1f} | P95={:.1f} | Max={:.1f}".format(
            ttft_stats["min"] * 1000,
            ttft_stats["median"] * 1000,
            ttft_stats["p95"] * 1000,
            ttft_stats["max"] * 1000,
        ),
        flush=True,
    )
    print(
        "Generation Speed (tok/s) : Min={:.1f} | Median={:.1f} | P95={:.1f} | Max={:.1f}".format(
            speed_stats["min"], speed_stats["median"], speed_stats["p95"], speed_stats["max"]
        ),
        flush=True,
    )
    print(
        "Total Latency (s)        : Min={:.2f} | Median={:.2f} | P95={:.2f} | Max={:.2f}".format(
            lat_stats["min"], lat_stats["median"], lat_stats["p95"], lat_stats["max"]
        ),
        flush=True,
    )
    print("==================================================", flush=True)


if __name__ == "__main__":
    run_benchmark(iterations=5)
