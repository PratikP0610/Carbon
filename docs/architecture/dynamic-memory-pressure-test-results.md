# Carbon 1.0.0 — Dynamic Memory Pressure & Switching Test Results

**Document Date:** 2026-10-28  
**Author:** Google Antigravity (Primary Development Agent)  
**Test Suite:** `tests/unit/test_memory_pressure_and_switching.py`  
**Evaluation Mode:** Simulated Condition Unit Testing  
**Status:** All 7 Simulated Verification Tests Passed (100% Pass Rate)  

> [!IMPORTANT]
> In accordance with user development instructions, the test results documented herein reflect **controlled simulated condition tests** executed via the automated test harness. They are explicitly distinguished from real hardware benchmarks.

---

## 1. Test Suite Architecture & Verification Scope

The test suite validates the core resource governance and concurrency invariants of the Carbon Model Supervisor (CMS) under simulated stress and fault-injection conditions:

1. **Mach Kernel Memory Pressure Reaction Ladder:** Verifies proper behavioral adaptations across Normal (Level 1), Warning (Level 2), and Critical (Level 4) memory pressure levels.
2. **Fair Reader-Writer Lock (RWLock) Lease Safety:** Verifies that model switching can never unload a model while an active inference lease is held.
3. **Single Active Chat LLM Concurrency Invariant:** Verifies that simultaneous chat LLM inference attempts are strictly blocked and serialized.
4. **Fault Recovery Under Unload Failure:** Verifies that if an active model fails to unload, the switch is immediately aborted to protect host memory.
5. **Fault Recovery Under Load Failure:** Verifies that if a target model fails to pre-warm, CMS automatically executes a clean rollback to the primary approved default (`qwen3.5:9b`).

---

## 2. Simulated Test Results Breakdown

### 2.1 Test 1: Level 1 Normal Operation (`test_memory_pressure_level_1_normal`)
- **Simulated Input:** `handle_memory_pressure(level=1)`
- **Observed Behavior:**
  - `supervisor.current_pressure_level == 1`
  - `supervisor.max_context_tokens == 32768` (Full 32K context enabled)
  - `active_model.is_active is True` (Primary model kept resident in GPU memory)
  - Idle specialist models retained.
- **Result:** **PASS**

### 2.2 Test 2: Level 2 Warning Response (`test_memory_pressure_level_2_warning`)
- **Simulated Input:** `handle_memory_pressure(level=2)`
- **Observed Behavior:**
  - `supervisor.current_pressure_level == 2`
  - `supervisor.max_context_tokens == 8192` (Context window constrained to 8K)
  - `supervisor.idle_specialist_models_unloaded is True` (Evicts background embedding/TTS weights)
  - Reasoning depth downgraded from `deep` to `standard` to avoid expanding KV cache buffers.
- **Result:** **PASS**

### 2.3 Test 3: Level 4 Critical Response (`test_memory_pressure_level_4_critical`)
- **Simulated Input:** `handle_memory_pressure(level=4)`
- **Observed Behavior:**
  - `supervisor.current_pressure_level == 4`
  - `supervisor.max_context_tokens == 4096`
  - Provider received explicit `unload_model("qwen3.5:9b")` command.
  - Active chat model immediately unloaded (`active_model.is_active == False`), relinquishing Metal unified memory allocations to the host OS.
  - Deep reasoning prohibited; critical memory alert generated.
- **Result:** **PASS**

### 2.4 Test 4: Guaranteed Lease Safety (`test_no_model_unloading_during_active_inference`)
- **Simulated Scenario:**
  1. Task `task-1` acquires an active inference lease on `qwen3.5:9b`.
  2. While `task-1` is generating tokens, a model switch request (`switch_model("qwen-coder:7b")`) is dispatched concurrently in a background task.
- **Observed Behavior:**
  - CMS marks `_switch_pending = True`, blocking any subsequent inference requests.
  - CMS awaits the drain event (`_drain_event.wait()`).
  - Active model `qwen3.5:9b` **remains loaded and active** throughout the duration of `task-1`'s lease. Provider's `unloaded_models` list remains empty.
  - Upon `task-1` calling `release_inference_lease("task-1")`, `_drain_event` triggers.
  - CMS safely unloads `qwen3.5:9b` and pre-warms `qwen-coder:7b`.
- **Result:** **PASS** (Zero model unloads during active inference).

### 2.5 Test 5: Single Active Chat LLM Mutual Exclusion (`test_no_simultaneous_chat_llm_inference`)
- **Simulated Scenario:** Task A acquires inference lease. Task B attempts to acquire a concurrent chat inference lease.
- **Observed Behavior:**
  - Task B's lease acquisition blocks on `_inference_lock`.
  - When timed out (`timeout=0.1`), Task B raises `TimeoutError`.
  - Upon Task A releasing its lease, Task B successfully acquires the lock.
- **Result:** **PASS** (Simultaneous chat LLM execution strictly prohibited).

### 2.6 Test 6: Recovery After Failed Model Unload (`test_recovery_after_failed_unload`)
- **Simulated Scenario:** Fault injected causing `provider.unload_model()` to return `False` on all retry attempts.
- **Observed Behavior:**
  - CMS attempts unload up to 3 times with exponential backoff.
  - Upon final failure, CMS **aborts the model switch** (`switch_model` returns `False`).
  - Target model is NOT loaded, preventing concurrent model residency on 16 GB hardware.
  - Previous model `qwen3.5:9b` remains active and healthy.
- **Result:** **PASS** (Memory exhaustion prevented; switch safely aborted).

### 2.7 Test 7: Recovery After Failed Model Load (`test_recovery_after_failed_model_loading`)
- **Simulated Scenario:** Fault injected causing `provider.load_model("failing-model")` to return `False`.
- **Observed Behavior:**
  - CMS detects target model failure.
  - CMS automatically triggers an automated rollback to `default_model_id` (`qwen3.5:9b`).
  - Default model is re-loaded via provider and marked active.
  - System state remains operational rather than left in an orphaned state.
- **Result:** **PASS** (Automated rollback verified).

---

## 3. Test Suite Execution Summary

```
tests/unit/test_memory_pressure_and_switching.py::test_memory_pressure_level_1_normal PASSED
tests/unit/test_memory_pressure_and_switching.py::test_memory_pressure_level_2_warning PASSED
tests/unit/test_memory_pressure_and_switching.py::test_memory_pressure_level_4_critical PASSED
tests/unit/test_memory_pressure_and_switching.py::test_no_model_unloading_during_active_inference PASSED
tests/unit/test_memory_pressure_and_switching.py::test_no_simultaneous_chat_llm_inference PASSED
tests/unit/test_memory_pressure_and_switching.py::test_recovery_after_failed_unload PASSED
tests/unit/test_memory_pressure_and_switching.py::test_recovery_after_failed_model_loading PASSED

======================== 7 passed in 0.18s =========================
```
