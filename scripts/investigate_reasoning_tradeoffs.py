"""Systematic comparison of think=true vs think=false across Carbon benchmark prompts.

Measures:
- prompt_eval_count, prompt_eval_duration
- thinking_token_count, visible_token_count
- TTFT (time to first visible token)
- total_duration
- answer quality, correctness, and completeness
"""

import asyncio
import json
import time
from pathlib import Path

import httpx

TEST_PROMPTS = [
    {
        "name": "1. Factual",
        "prompt": "What is the capital of Japan?",
    },
    {
        "name": "2. Arithmetic",
        "prompt": "What is 37 + 58?",
    },
    {
        "name": "3. Standard Explanation",
        "prompt": "Explain why Python is interpreted.",
    },
    {
        "name": "4. Standard Coding",
        "prompt": "Write a Java method to find the second largest distinct element in an integer array.",
    },
    {
        "name": "5. Deep Reasoning",
        "prompt": "Solve this eight-ball balance-scale problem: You have 8 identical-looking balls, one of which is slightly heavier than the others. Using a balance scale only twice, how do you find the heavier ball?",
    },
]


async def query_ollama(prompt: str, think: bool) -> dict:
    url = "http://127.0.0.1:11434/api/generate"
    payload = {
        "model": "qwen3.5:9b",
        "prompt": prompt,
        "think": think,
        "stream": True,
        "options": {
            "num_predict": 2560 if think else 1024,
            "temperature": 0.6 if think else 0.2,
        },
    }

    t_start = time.perf_counter()
    ttft = None
    thinking_tokens = 0
    visible_tokens = []
    final_stats = {}

    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream("POST", url, json=payload) as response:
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except Exception:
                    continue

                if "thinking" in chunk and chunk["thinking"]:
                    thinking_tokens += 1
                elif "response" in chunk and chunk["response"]:
                    if ttft is None:
                        ttft = time.perf_counter() - t_start
                    visible_tokens.append(chunk["response"])

                if chunk.get("done"):
                    final_stats = {
                        "prompt_eval_count": chunk.get("prompt_eval_count", 0),
                        "prompt_eval_duration_ms": chunk.get("prompt_eval_duration", 0) / 1e6,
                        "eval_count": chunk.get("eval_count", 0),
                        "eval_duration_ms": chunk.get("eval_duration", 0) / 1e6,
                        "done_reason": chunk.get("done_reason", ""),
                    }

    t_total = time.perf_counter() - t_start
    full_text = "".join(visible_tokens)

    return {
        "think": think,
        "prompt": prompt,
        "ttft_s": ttft if ttft is not None else t_total,
        "total_time_s": t_total,
        "thinking_tokens_est": thinking_tokens,
        "visible_token_count": len(visible_tokens),
        "response_length_chars": len(full_text),
        "final_stats": final_stats,
        "response_text": full_text,
    }


async def main():
    print("=" * 80)
    print("Carbon Stage 1.5: think=true vs think=false Tradeoff Investigation")
    print("=" * 80)

    comparison_results = []

    for item in TEST_PROMPTS:
        name = item["name"]
        prompt = item["prompt"]
        print(f"\n--- Testing: {name} ---")
        print(f"Prompt: {prompt}")

        # Test A: think=True
        print("  Running with think=True...")
        res_true = await query_ollama(prompt, think=True)
        print(f"    think=True:  TTFT={res_true['ttft_s']:.2f}s, Total={res_true['total_time_s']:.2f}s, VisibleToks={res_true['visible_token_count']}, ThinkToks={res_true['thinking_tokens_est']}")

        # Test B: think=False
        print("  Running with think=False...")
        res_false = await query_ollama(prompt, think=False)
        print(f"    think=False: TTFT={res_false['ttft_s']:.2f}s, Total={res_false['total_time_s']:.2f}s, VisibleToks={res_false['visible_token_count']}, ThinkToks={res_false['thinking_tokens_est']}")

        # Speedup
        speedup = res_true["total_time_s"] / max(0.001, res_false["total_time_s"])
        print(f"    --> Speedup with think=False: {speedup:.1f}x")

        comparison_results.append({
            "name": name,
            "prompt": prompt,
            "think_true": res_true,
            "think_false": res_false,
            "speedup_factor": speedup,
        })

    out_file = Path("docs/reports/reasoning_tradeoffs_comparison.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(comparison_results, f, indent=2)

    print(f"\nResults saved to {out_file}")


if __name__ == "__main__":
    asyncio.run(main())

