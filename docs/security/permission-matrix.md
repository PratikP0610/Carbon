# Carbon Permission & Autonomy Matrix Specification

**Stage 7.5 Alignment Status:** Aligned strictly to [models.py](../../src/carbon/interfaces/models.py), [governance_engine.py](../../src/carbon/governance/governance_engine.py), [orchestrator.py](../../src/carbon/core/orchestrator.py), [base.py](../../src/carbon/tools/base.py), and [builtin.py](../../src/carbon/tools/builtin.py).

---

## 1. Permission Tier Ladder (`P0` – `P4`) (`IMPLEMENTED`)

| Tier | Intent | Actual Auto-Approval Behavior (`authorize_action`) | Current Registered Built-Ins / Uses |
| :--- | :--- | :--- | :--- |
| **P0** | Conversational / zero side effects | Auto in `A1`, `A2`, `A3`; `False` in `A0` | None registered in `ToolRegistry` |
| **P1** | Read local state | Requires approval in `A0`, `A1`; auto in `A2`, `A3` | `system_info`, `list_applications` (`timeout_seconds=5.0`) |
| **P2** | Reversible local action | Requires approval in `A0`, `A1`; auto in `A2`, `A3` | `open_application` (`timeout_seconds=5.0`, allowlisted `APPROVED_APPS`) |
| **P3** | Persistent state mutation | **Always returns `False`** (`A0`–`A3`); requires human `approval_id` | Procedural memory governance (`procedural_memory_write` approval gate) |
| **P4** | Destructive / high-impact | **Always returns `False`** (`A0`–`A3`); not auto-authorizable | None registered (**PLANNED**) |

---

## 2. Autonomy Matrix (`verified in GovernanceEngine.authorize_action()`)

| Autonomy Tier | `P0` | `P1` | `P2` | `P3` | `P4` | Code Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`A0` (Observe Only)** | `False` | `False` | `False` | `False` | `False` | **IMPLEMENTED** |
| **`A1` (Assist & Recommend, Default)** | **`True`** | `False` (Approval req.) | `False` (Approval req.) | `False` (Approval req.) | `False` (Approval req.) | **IMPLEMENTED** |
| **`A2` (Execute Approved)** | **`True`** | **`True`** | **`True`** | `False` (Approval req.) | `False` (Approval req.) | **IMPLEMENTED** |
| **`A3` (Bounded Autonomy)** | **`True`** | **`True`** | **`True`** | `False` (Approval req.) | `False` (Approval req.) | **PARTIALLY IMPLEMENTED** *(Identical to `A2`; scoped policy envelopes are **PLANNED**)* |

**Immutable Enforcement Notes:**
- `Settings.autonomy_level` defaults to `"A1"` (`settings.py`).
- `ToolRegistry.execute_tool()` enforces schema validation (`validate_tool_call`) before checking `consume_approval` or `authorize_action`, and defaults to `DENY` if governance or `autonomy_provider` is unconfigured (`tools/base.py`).
- Pending approvals exist in memory (`GovernanceEngine._pending_approvals`), expire after `timeout_seconds` (`60.0s` default), bind via SHA-256 `arguments_digest`, and accept resolution only from `DecisionSource.USER_UI` or `DecisionSource.USER_API`.

---

## 3. Verified `"awaiting_approval"` WebSocket Payload Contract (`IMPLEMENTED`)

Emitted by `CarbonCore.execute_task_stream()` (`orchestrator.py:1328-1339`) during the `"open <app>"` `SYSTEM_COMMAND` fast-path:

```json
{
  "event": "awaiting_approval",
  "approval_id": "apr-...",
  "task_id": "task-...",
  "tool_id": "open_application",
  "permission_level": "P2",
  "title": "Open Application Request",
  "arguments": {
    "app_name": "Safari"
  },
  "impact_summary": "Launch approved application 'Safari' on macOS.",
  "is_reversible": true,
  "timeout_seconds": 60.0
}
```

### Actual Client Behavior (`ContentView.swift` & `orchestrator.py`)

1. **Presentation:** `ContentView` displays `approvalBannerView` inline above the composer showing `title`, `permissionLevel` pill (`orange` for `P0`–`P2`, `red` for `P3`–`P4`), `impactSummary`, `Tool: ... • Args: ...`, and **Deny** / **Approve** buttons.
2. **Resolution:** Client sends `{"action": "approve" | "deny", "approval_id": "...", "task_id": "..."}` over `/ws/chat` (or `POST /api/v1/approvals/{approval_id}/decision`).
3. **Timeout / Denial:** If unapproved after `60.0s` or denied, `CarbonCore` cancels the approval, sets `task.state = TaskState.CANCELLED`, yields `"approval_resolved"`, a notice `"token"`, and `"cancelled"`, and returns immediately without calling LLM inference.
