"""Controlled A/B testing of prompt efficiency and repetition penalties for Stage 1.10.

Compares:
1. Baseline: Current standard prompt, default options (presence_penalty 1.5)
2. PromptOpt_DefaultPenalties: Optimized prompt, default options (presence_penalty 1.5)
3. PromptOpt_ZeroPresence: Optimized prompt, presence_penalty 0.0
4. PromptOpt_LowPresence_Repeat: Optimized prompt, presence_penalty 0.1, repeat_penalty 1.05
5. PromptOpt_RepeatOnly: Optimized prompt, presence_penalty 0.0, repeat_penalty 1.05
"""

import asyncio
import json
import time
from pathlib import Path

import httpx

RESULTS_FILE = Path("docs/reports/stage1_10-ab-experiment-results.json")

CURRENT_SYSTEM_PROMPT = (
    "You are Carbon, an intelligent personal AI computing assistant on macOS.\n"
    "Provide clear, accurate, and direct responses.\n"
    "Provide a complete, well-structured, and clear answer.\n"
)

OPTIMIZED_CODING_PROMPT = (
    "You are Carbon, an intelligent personal AI computing assistant on macOS.\n"
    "Provide clear, accurate, and direct responses.\n"
    "Provide a direct, high-quality, and optimal solution:\n"
    "- Identify the optimal approach and provide a complete working implementation.\n"
    "- Include essential edge-case handling.\n"
    "- Clearly state the time and space complexity.\n"
    "- Avoid unnecessary conversational introductions, repeated explanations, duplicate implementations, or tutorial filler.\n"
)

OPTIMIZED_EXPLANATION_PROMPT = (
    "You are Carbon, an intelligent personal AI computing assistant on macOS.\n"
    "Provide clear, accurate, and direct responses.\n"
    "Provide a direct, clear, and well-structured response:\n"
    "- Answer the question directly and explain essential concepts clearly.\n"
    "- Use examples when helpful to illustrate key points.\n"
    "- Avoid conversational filler, repetitive conclusions, or tutorial preamble.\n"
)

TEST_CASES = [
    {
        "id": "java_coding",
        "type": "coding",
        "prompt": "Write a Java method to find the second-largest distinct element in an integer array. Explain its time complexity.",
        "validator": lambda t: (
            "int" in t
            and ("second" in t.lower() or "secondlargest" in t.lower())
            and ("o(n)" in t.lower() or "time complexity" in t.lower())
        ),
    },
    {
        "id": "binary_search",
        "type": "explanation",
        "prompt": "Explain why binary search requires sorted input and demonstrate what can go wrong when the input is unsorted.",
        "validator": lambda t: (
            "binary search" in t.lower()
            and "sorted" in t.lower()
            and any(w in t.lower() for w in ["wrong", "fail", "miss", "not found", "incorrect"])
        ),
    },
    {
        "id": "cpu_structured",
        "type": "explanation",
        "prompt": "Explain what a CPU does using a heading, a numbered list, a Python code block and one mathematical expression.",
        "validator": lambda t: (
            ("#" in t or "CPU" in t)
            and ("```python" in t or "```" in t)
            and any(f"{i}." in t for i in range(1, 4))
        ),
    },
]

CONFIGURATIONS = [
    {
        "name": "1_Baseline",
        "desc": "Current prompt, default Modelfile presence_penalty (1.5)",
        "use_optimized_prompt": False,
        "options": {"num_predict": 2560, "temperature": 0.6},
    },
    {
        "name": "2_PromptOpt_DefaultPenalties",
        "desc": "Optimized concise prompt, default Modelfile presence_penalty (1.5)",
        "use_optimized_prompt": True,
        "options": {"num_predict": 2560, "temperature": 0.6},
    },
    {
        "name": "3_PromptOpt_ZeroPresence",
        "desc": "Optimized concise prompt, presence_penalty: 0.0",
        "use_optimized_prompt": True,
        "options": {"num_predict": 2560, "temperature": 0.6, "presence_penalty": 0.0},
    },
    {
        "name": "4_PromptOpt_LowPresence_Repeat",
        "desc": "Optimized concise prompt, presence_penalty: 0.1, repeat_penalty: 1.05",
        "use_optimized_prompt": True,
        "options": {
            "num_predict": 2560,
            "temperature": 0.6,
            "presence_penalty": 0.1,
            "repeat_penalty": 1.05,
        },
    },
    {
        "name": "5_PromptOpt_RepeatOnly",
        "desc": "Optimized concise prompt, presence_penalty: 0.0, repeat_penalty: 1.05",
        "use_optimized_prompt": True,
        "options": {
            "num_predict": 2560,
            "temperature": 0.6,
            "presence_penalty": 0.0,
            "repeat_penalty": 1.05,
        },
    },
]


async def run_single(
    client: httpx.AsyncClient, system_prompt: str, prompt: str, options: dict
) -> dict:
    payload = {
        "model": "qwen3.5:9b",
        "prompt": prompt,
        "system": system_prompt,
        "stream": True,
        "think": False,
        "options": options,
    }
    t_start = time.perf_counter()
    t_first_token = None
    accumulated = []
    done_reason = None
    final_data = {}

    async with client.stream("POST", "/api/generate", json=payload) as resp:
        if resp.status_code != 200:
            err = await resp.aread()
            raise RuntimeError(f"Ollama error: {err.decode('utf-8')}")
        async for line in resp.aiter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            part = chunk.get("response", "")
            if part:
                if t_first_token is None:
                    t_first_token = time.perf_counter()
                accumulated.append(part)
            if chunk.get("done"):
                done_reason = chunk.get("done_reason")
                final_data = chunk

    t_end = time.perf_counter()
    full_text = "".join(accumulated)
    ttft = (t_first_token - t_start) if t_first_token else (t_end - t_start)
    total_sec = t_end - t_start
    eval_count = final_data.get("eval_count", len(full_text.split()))
    eval_duration_ms = (final_data.get("eval_duration", 0) or 0) / 1_000_000
    tps = (
        (eval_count / (eval_duration_ms / 1000.0))
        if eval_duration_ms > 0
        else (eval_count / total_sec)
    )

    return {
        "total_duration_sec": round(total_sec, 3),
        "ttft_sec": round(ttft, 3),
        "token_count": eval_count,
        "tps": round(tps, 2),
        "done_reason": done_reason,
        "text": full_text,
    }


async def main():
    print("=" * 70)
    print("CARBON STAGE 1.10 CONTROLLED A/B EXPERIMENT: PENALTIES & PROMPTS")
    print("=" * 70)

    results = {}
    async with httpx.AsyncClient(base_url="http://127.0.0.1:11434", timeout=300.0) as client:
        # Warm up the model first
        print("Warming up model...")
        await client.post(
            "/api/generate",
            json={
                "model": "qwen3.5:9b",
                "prompt": "Warm up",
                "stream": False,
                "options": {"num_predict": 5},
            },
        )
        print("Warm-up complete.\n")

        for test in TEST_CASES:
            test_id = test["id"]
            print(f">>> TEST CASE: {test_id} ('{test['prompt'][:50]}...')")
            results[test_id] = {}

            for cfg in CONFIGURATIONS:
                cfg_name = cfg["name"]
                if not cfg["use_optimized_prompt"]:
                    sys_p = CURRENT_SYSTEM_PROMPT
                else:
                    sys_p = (
                        OPTIMIZED_CODING_PROMPT
                        if test["type"] == "coding"
                        else OPTIMIZED_EXPLANATION_PROMPT
                    )

                print(f"  Running config {cfg_name}...", end="", flush=True)
                res = await run_single(client, sys_p, test["prompt"], cfg["options"])
                is_correct = test["validator"](res["text"])
                res["is_correct"] = is_correct

                # Print summary
                print(
                    f" Done. Total={res['total_duration_sec']}s | TTFT={res['ttft_sec']}s | Tokens={res['token_count']} | TPS={res['tps']} | Correct={is_correct}"
                )

                results[test_id][cfg_name] = {
                    "config_desc": cfg["desc"],
                    "total_duration_sec": res["total_duration_sec"],
                    "ttft_sec": res["ttft_sec"],
                    "token_count": res["token_count"],
                    "tps": res["tps"],
                    "done_reason": res["done_reason"],
                    "is_correct": is_correct,
                    "preview": res["text"][:250].replace("\n", " "),
                }

    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"\nExperiment complete. Results saved to: {RESULTS_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
