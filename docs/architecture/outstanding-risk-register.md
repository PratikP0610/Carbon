# Carbon 1.0.0 — Outstanding Risk Register

**Document Date:** 2026-10-28  
**Scope:** Phase 0 Final Verification / Phase 1 Foundation Audit  
**Status:** Monitored & Mitigated  

---

## 1. Risk Register Matrix

| Risk ID | Category | Description | Severity | Likelihood | Current Mitigation | Target Resolution Phase |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **RISK-001** | **Performance / UX** | **Reasoning Model Response Latency (TTFT_resp):** Qwen 3.5 9B generates 50–150 `<think>` tokens (~6.0s P50, ~13.9s P95) before emitting user-visible text, exceeding the original PRD sub-second target. | **Medium** | **High** | TTFR begins in ~589 ms. SwiftUI client displays an animated "Thinking..." status immediately. | **Phase 2:** Two-stage UI disclosure and routing of operational queries to a fast non-reasoning specialist model. |
| **RISK-002** | **Security / Storage** | **POSIX 0600 Token Storage vs. Native Keychain:** Developer bootstrap token file (`data/.carbon_token`) does not isolate against other processes running under the same user UID (501). | **Medium** | **Medium** | Atomic file writes, strict `0600` permissions, loopback-only binding, zero network exposure. | **Phase 2:** Dual-custody handshake where Swift client stores secret in macOS Keychain and injects via environment variable. |
| **RISK-003** | **System Contention** | **Unmediated Concurrent Ollama Access:** External processes calling Ollama directly on port 11434 can block Carbon inference (as observed in Iteration 25 during concurrent pytest execution). | **Low** | **Low** | Carbon enforces internal Reader-Writer lease locks and request timeouts (HTTP 503 on model busy). | **Phase 2:** Dedicated Carbon Ollama instance or loopback port isolation. |
| **RISK-004** | **OS Integration** | **Mach Kernel Memory Pressure Signal Hooking:** Current memory pressure checks rely on simulated level injection and daemon introspection rather than live `DISPATCH_SOURCE_TYPE_VM` kernel hooks. | **Low** | **Low** | Reactive reaction ladder (Levels 1, 2, 4) fully tested and hardware-verified with live Ollama daemon. | **Phase 2:** Integrate native macOS `DispatchSourceMemoryPressure` in SwiftUI client to forward live kernel events to `/api/v1/system/memory-pressure`. |
| **RISK-005** | **Client / IPC** | **WebSocket Connection Re-Authentication on Token Rotation:** When `/api/v1/auth/rotate-token` is executed, existing WebSocket connections remain active until closed or next request. | **Low** | **Low** | Token rotation invalidates subsequent handshakes and frames immediately; short heartbeat enforces currency. | **Phase 2:** Server sends `action: "token_rotated"` frame forcing immediate client reconnect with new credentials. |

---

## 2. Risk Evaluation & Phase 2 Gate Alignment

None of the identified risks represent critical blockers or architecture violations for Phase 1. 

1. **Architecture Integrity:** The single-active-chat-LLM policy, CMS lifecycle governance, Fair RWLock lease management, and hardened loopback API are 100% operational and verified.
2. **Security Posture:** Query-string WebSocket authentication has been completely removed; loopback CORS strictly drops `Origin: null`; token rotation is atomic with `0600` permissions; sensitive tokens are redacted in all logs.
3. **Hardware Readiness:** Apple Silicon M5 Unified Memory allocation (5.82 GB) and sub-second cold weight mapping (1.34s) comfortably support Carbon's operational parameters.

All outstanding risks are documented, quantified, and assigned to specific Phase 2 deliverables.
