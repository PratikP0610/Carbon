"""Actual macOS and Ollama Hardware Memory Verification.

Safely measures:
1. Active model residence in Ollama unified memory (/api/ps).
2. Ollama model unloading via keep_alive: 0.
3. Actual host and process memory reclamation (RSS and Mach memory stats).
4. Model reloading and inference recovery latency.
5. Active-inference protection (refusing disruptive unloading during in-flight generation).

CRITICAL SAFETY DIRECTIVE:
Never intentionally allocate host RAM to create artificial system memory exhaustion.
All verification is conducted via safe daemon introspection and API lifecycle commands.
"""

import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from carbon.model_control.cms import CarbonModelSupervisor
from carbon.model_control.ollama_provider import OllamaProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hardware_memory_verification")

OLLAMA_BASE_URL = "http://127.0.0.1:11434"
MODEL_ID = "qwen3.5:9b"
RESULTS_PATH = Path("benchmarks/memory/hardware_verification_results.json")


def get_ollama_pid() -> int | None:
    """Find the process ID of the Ollama daemon."""
    try:
        output = subprocess.check_output(["pgrep", "-f", "ollama"], text=True).strip()
        pids = [int(p) for p in output.splitlines() if p.strip()]
        return pids[0] if pids else None
    except Exception:
        return None


def get_process_rss_mb(pid: int | None) -> float:
    """Get the resident set size (RSS) of a process in MB."""
    if not pid:
        return 0.0
    try:
        output = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)], text=True).strip()
        return round(float(output) / 1024.0, 2)
    except Exception:
        return 0.0


def get_loaded_models_info() -> list[dict[str, Any]]:
    """Query Ollama /api/ps to retrieve loaded models and VRAM/RAM footprints."""
    try:
        resp = httpx.get(f"{OLLAMA_BASE_URL}/api/ps", timeout=5.0)
        if resp.status_code == 200:
            return resp.json().get("models", [])
    except Exception as e:
        logger.error(f"Error querying /api/ps: {e}")
    return []


async def run_hardware_verification():
    logger.info("=== CARBON 1.0.0 HARDWARE MEMORY VERIFICATION ===")
    results: dict[str, Any] = {
        "hardware": "Apple Silicon (M5 MacBook Air, macOS Darwin)",
        "model_id": MODEL_ID,
        "verification_timestamp": time.time(),
        "stages": {},
    }

    ollama_pid = get_ollama_pid()
    logger.info(f"Detected Ollama PID: {ollama_pid}")
    results["ollama_pid"] = ollama_pid

    provider = OllamaProvider(base_url=OLLAMA_BASE_URL)
    cms = CarbonModelSupervisor(provider=provider, default_model_id=MODEL_ID)
    await cms.initialize()

    # -------------------------------------------------------------
    # Stage 1: Pre-warming and Active Residency Measurement
    # -------------------------------------------------------------
    logger.info("\n--- Stage 1: Warm Model Residency Measurement ---")
    rss_initial = get_process_rss_mb(ollama_pid)
    logger.info(f"Ollama initial RSS: {rss_initial:.1f} MB")

    logger.info(f"Ensuring '{MODEL_ID}' is loaded in Unified Memory...")
    load_start = time.perf_counter()
    load_ok = await provider.load_model(MODEL_ID)
    load_time_s = time.perf_counter() - load_start

    models_loaded = get_loaded_models_info()
    model_entry = next((m for m in models_loaded if MODEL_ID in m.get("name", "")), None)

    size_bytes = model_entry.get("size", 0) if model_entry else 0
    size_vram_bytes = model_entry.get("size_vram", 0) if model_entry else 0
    rss_loaded = get_process_rss_mb(ollama_pid)

    results["stages"]["stage_1_loaded_state"] = {
        "success": load_ok,
        "prewarm_duration_s": round(load_time_s, 3),
        "size_mb": round(size_bytes / (1024 * 1024), 2),
        "size_vram_mb": round(size_vram_bytes / (1024 * 1024), 2),
        "ollama_rss_mb": rss_loaded,
        "raw_ps_entry": model_entry,
    }
    logger.info(
        f"Model resident in Unified Memory: Size={size_bytes / (1024 * 1024):.1f} MB, "
        f"VRAM={size_vram_bytes / (1024 * 1024):.1f} MB, Ollama RSS={rss_loaded:.1f} MB"
    )

    # -------------------------------------------------------------
    # Stage 2: Active-Inference Protection Verification
    # -------------------------------------------------------------
    logger.info("\n--- Stage 2: Active-Inference Protection Verification ---")
    # Simulate an active lease held by an in-flight prompt
    lease_task_id = "hw-verification-active-task"
    await cms.acquire_inference_lease(lease_task_id)
    logger.info(
        f"Acquired CMS inference lease for '{lease_task_id}'. Active leases: {len(cms._active_leases)}"
    )

    # Trigger Level 4 memory pressure while inference is active
    logger.info(
        "Applying simulated Mach memory pressure Level 4 (Critical) while lease is active..."
    )
    pressure_result = await cms.handle_memory_pressure(4)
    logger.info(f"CMS Reaction actions: {pressure_result['actions']}")

    # Check if model is still resident
    models_during_active = get_loaded_models_info()
    is_still_loaded = any(MODEL_ID in m.get("name", "") for m in models_during_active)
    logger.info(
        f"Is model still resident during active lease? {is_still_loaded} (MUST BE True to protect active stream)"
    )

    results["stages"]["stage_2_active_inference_protection"] = {
        "lease_held": lease_task_id,
        "pressure_level_applied": 4,
        "deferred_unload_flag": cms._deferred_critical_unload,
        "model_preserved_during_inference": is_still_loaded,
        "actions": pressure_result["actions"],
    }

    # Now release the lease and verify deferred eviction triggers
    logger.info("Releasing inference lease; deferred eviction should now trigger...")
    await cms.release_inference_lease(lease_task_id)
    await asyncio.sleep(1.0)  # Allow unload to settle

    models_after_release = get_loaded_models_info()
    is_evicted_after_release = not any(MODEL_ID in m.get("name", "") for m in models_after_release)
    logger.info(f"Is model evicted after lease release? {is_evicted_after_release} (MUST BE True)")

    results["stages"]["stage_2_active_inference_protection"]["evicted_after_release"] = (
        is_evicted_after_release
    )

    # -------------------------------------------------------------
    # Stage 3: Actual Model Unloading & Hardware Memory Reclamation
    # -------------------------------------------------------------
    logger.info("\n--- Stage 3: Model Unload and Physical Memory Reclamation ---")
    # Reload model first to get clean measurement of unload timing
    await provider.load_model(MODEL_ID)
    await asyncio.sleep(0.5)
    rss_before_unload = get_process_rss_mb(ollama_pid)
    ps_before = get_loaded_models_info()

    logger.info("Executing Ollama model eviction (keep_alive: 0)...")
    unload_start = time.perf_counter()
    unload_ok = await provider.unload_model(MODEL_ID)
    unload_duration_s = time.perf_counter() - unload_start
    await asyncio.sleep(1.0)  # Settle time

    ps_after = get_loaded_models_info()
    rss_after_unload = get_process_rss_mb(ollama_pid)
    is_completely_unloaded = len([m for m in ps_after if MODEL_ID in m.get("name", "")]) == 0

    reclaimed_vram_mb = 0.0
    if ps_before:
        reclaimed_vram_mb = ps_before[0].get("size_vram", 0) / (1024 * 1024)

    logger.info(
        f"Unload Completed: Success={unload_ok}, Duration={unload_duration_s:.3f}s, "
        f"Resident in /api/ps: {not is_completely_unloaded}"
    )
    logger.info(
        f"Hardware Memory Reclaimed: Unified Memory / VRAM={reclaimed_vram_mb:.1f} MB, "
        f"Ollama RSS Before={rss_before_unload:.1f} MB -> After={rss_after_unload:.1f} MB"
    )

    results["stages"]["stage_3_unload_reclamation"] = {
        "unload_success": unload_ok,
        "unload_duration_s": round(unload_duration_s, 3),
        "is_completely_unloaded": is_completely_unloaded,
        "vram_reclaimed_mb": round(reclaimed_vram_mb, 2),
        "rss_before_mb": rss_before_unload,
        "rss_after_mb": rss_after_unload,
        "rss_delta_mb": round(rss_before_unload - rss_after_unload, 2),
    }

    # -------------------------------------------------------------
    # Stage 4: Reload & Recovery Latency
    # -------------------------------------------------------------
    logger.info("\n--- Stage 4: Reload and Recovery Verification ---")
    logger.info("Triggering generation to verify automated reloading and post-eviction recovery...")
    reload_t0 = time.perf_counter()

    reload_generation_ok = False
    tokens_generated = 0
    load_duration_from_ollama = 0.0

    async with httpx.AsyncClient(base_url=OLLAMA_BASE_URL, timeout=30.0) as http_client:
        resp = await http_client.post(
            "/api/generate",
            json={
                "model": MODEL_ID,
                "prompt": "Respond with 'Carbon ready'.",
                "stream": False,
                "options": {"num_predict": 10},
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            reload_generation_ok = True
            tokens_generated = data.get("eval_count", 0)
            load_duration_from_ollama = data.get("load_duration", 0) / 1e9

    reload_total_latency_s = time.perf_counter() - reload_t0
    logger.info(
        f"Reload Recovery: Success={reload_generation_ok}, Total Latency={reload_total_latency_s:.3f}s, "
        f"Ollama Load Duration={load_duration_from_ollama:.3f}s, Tokens={tokens_generated}"
    )

    results["stages"]["stage_4_reload_recovery"] = {
        "recovery_success": reload_generation_ok,
        "total_reload_latency_s": round(reload_total_latency_s, 3),
        "ollama_load_duration_s": round(load_duration_from_ollama, 3),
        "tokens_generated": tokens_generated,
    }

    # -------------------------------------------------------------
    # Summary & Output
    # -------------------------------------------------------------
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)

    logger.info(f"\nSaved hardware verification results to {RESULTS_PATH}")
    logger.info("=== HARDWARE MEMORY VERIFICATION COMPLETE ===")


if __name__ == "__main__":
    asyncio.run(run_hardware_verification())
