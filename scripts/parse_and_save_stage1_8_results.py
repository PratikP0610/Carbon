"""Parse completed observations from task-3089.log and output machine-readable JSON."""

import json
import re
import statistics
from pathlib import Path

LOG_PATH = Path("/Users/pratikpradeshi/.gemini/antigravity/brain/f0cc0d37-8694-461a-acd0-6770725ec4f9/.system_generated/tasks/task-3089.log")
JSON_OUTPUT = Path("docs/reports/stage1_8_latency_benchmark_results.json")

def compute_p95(values: list[float]) -> float:
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

def parse_log():
    text = LOG_PATH.read_text()

    # Category mapping

    # Warmup
    warmup_match = re.search(r"Warm-up complete: Total=([\d\.]+)s, TTFT=([\d\.]+)s", text)
    warmup_data = {}
    if warmup_match:
        warmup_data = {
            "total_duration": float(warmup_match.group(1)),
            "ttft": float(warmup_match.group(2))
        }

    # Match each category section
    sections = re.findall(r"\[(\d)/5\] CATEGORY: (.*?)\n\s+Prompt: '(.*?)'\n-+\n(.*?)(?=\[\d/5\] CATEGORY:|\Z)", text, re.DOTALL)

    raw_observations = []
    category_summaries = {}

    line_regex = re.compile(
        r"Repetition (\d)/5 executing\.\.\. Done! Total=([\d\.]+)s \| TTFT=([\d\.]+)s \| Thinking=([\d\.]+)s \((\d+) tok @ ([\d\.]+) tps\) \| Visible=([\d\.]+)s \((\d+) tok @ ([\d\.]+) tps\)"
    )

    for _cat_num, cat_name, prompt, cat_body in sections:
        cat_name = cat_name.strip()
        matches = line_regex.findall(cat_body)
        cat_runs = []
        for rep, total, ttft, think_time, think_tok, think_tps, vis_time, vis_tok, vis_tps in matches:
            obs = {
                "category": cat_name,
                "prompt": prompt,
                "iteration": int(rep),
                "requested_mode": "standard",
                "executed_mode": "standard",
                "think_setting": True,
                "num_predict": 2560,
                "temperature": 0.6,
                "active_context": 4096,
                "durations": {
                    "total_duration": float(total),
                    "ttft": float(ttft),
                    "thinking_duration": float(think_time),
                    "visible_duration": float(vis_time),
                },
                "tokens": {
                    "thinking_tokens": int(think_tok),
                    "visible_tokens": int(vis_tok),
                    "total_tokens": int(think_tok) + int(vis_tok),
                },
                "throughput": {
                    "thinking_tps": float(think_tps),
                    "visible_tps": float(vis_tps),
                },
                "status": "COMPLETED"
            }
            cat_runs.append(obs)
            raw_observations.append(obs)

        if cat_runs:
            totals = [r["durations"]["total_duration"] for r in cat_runs]
            ttfts = [r["durations"]["ttft"] for r in cat_runs]
            thinks = [r["durations"]["thinking_duration"] for r in cat_runs]
            visibles = [r["durations"]["visible_duration"] for r in cat_runs]
            think_toks = [r["tokens"]["thinking_tokens"] for r in cat_runs]
            vis_toks = [r["tokens"]["visible_tokens"] for r in cat_runs]
            think_tps_list = [r["throughput"]["thinking_tps"] for r in cat_runs]
            vis_tps_list = [r["throughput"]["visible_tps"] for r in cat_runs]

            category_summaries[cat_name] = {
                "sample_size": len(cat_runs),
                "is_complete": len(cat_runs) == 5,
                "total_duration": {
                    "median": round(statistics.median(totals), 2),
                    "p95": round(compute_p95(totals), 2) if len(totals) >= 5 else None,
                    "min": round(min(totals), 2),
                    "max": round(max(totals), 2),
                },
                "ttft": {
                    "median": round(statistics.median(ttfts), 2),
                    "p95": round(compute_p95(ttfts), 2) if len(ttfts) >= 5 else None,
                    "min": round(min(ttfts), 2),
                    "max": round(max(ttfts), 2),
                },
                "thinking_duration": {
                    "median": round(statistics.median(thinks), 2),
                    "p95": round(compute_p95(thinks), 2) if len(thinks) >= 5 else None,
                    "min": round(min(thinks), 2),
                    "max": round(max(thinks), 2),
                },
                "visible_duration": {
                    "median": round(statistics.median(visibles), 2),
                    "p95": round(compute_p95(visibles), 2) if len(visibles) >= 5 else None,
                    "min": round(min(visibles), 2),
                    "max": round(max(visibles), 2),
                },
                "thinking_tokens": {
                    "median": round(statistics.median(think_toks), 1),
                    "min": min(think_toks),
                    "max": max(think_toks),
                },
                "visible_tokens": {
                    "median": round(statistics.median(vis_toks), 1),
                    "min": min(vis_toks),
                    "max": max(vis_toks),
                },
                "thinking_tps": {
                    "median": round(statistics.median(think_tps_list), 2),
                },
                "visible_tps": {
                    "median": round(statistics.median(vis_tps_list), 2),
                },
            }

    output = {
        "metadata": {
            "report_type": "PARTIAL STAGE 1.8 AUDIT (Gracefully halted after E.3)",
            "hardware": "Apple MacBook Air M5, 16GB Unified Memory",
            "os": "macOS 27.0.1",
            "model": "qwen3.5:9b Q4_K_M",
            "context_length": 4096,
            "inference_mode_requested": "standard",
            "think_setting": True,
            "num_predict": 2560,
            "temperature": 0.6,
            "total_completed_runs": len(raw_observations),
            "warmup_run": warmup_data,
        },
        "category_summaries": category_summaries,
        "raw_observations": raw_observations,
    }

    JSON_OUTPUT.write_text(json.dumps(output, indent=2))
    print(f"Successfully wrote {len(raw_observations)} raw observations to {JSON_OUTPUT}")

if __name__ == "__main__":
    parse_log()
