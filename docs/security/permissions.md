# Carbon Permission & Autonomy Architecture

**Stage 7.5 Alignment Status:** Aligned strictly to [models.py](../../src/carbon/interfaces/models.py), [governance_engine.py](../../src/carbon/governance/governance_engine.py), [settings.py](../../src/carbon/config/settings.py), [orchestrator.py](../../src/carbon/core/orchestrator.py), [builtin.py](../../src/carbon/tools/builtin.py), and [ContentView.swift](../../apps/macos/Carbon/Sources/ContentView.swift).

---

## 1. Permission Ladder (`P0` – `P4`) (`IMPLEMENTED`)

Every registered tool and governed operation uses the `PermissionLevel` enum (`src/carbon/interfaces/models.py`):

| Level | Policy Role | Actual `GovernanceEngine` Behavior | Examples in Codebase |
| :--- | :--- | :--- | :--- |
| **P0** | Conversational / safe read | Auto-authorized under `A1`, `A2`, `A3`; denied under `A0` (`authorize_action` requires approval in `A0`). | Direct chat responses (do not invoke tools) |
| **P1** | Read approved local state | Requires explicit `approval_id` in default `A1` (and `A0`); auto-authorized in `A2` and `A3`. | `system_info`, `list_applications` (`timeout_seconds=5.0`) |
| **P2** | Reversible local action | Requires explicit `approval_id` in default `A1` (and `A0`); auto-authorized in `A2` and `A3`. | `open_application` (`timeout_seconds=5.0`, allowlisted `APPROVED_APPS`) |
| **P3** | Persistent state mutation | **Always requires explicit approval** across all autonomy tiers `A0`–`A3`. | `procedural` memory write/update governance challenge (`POST /api/v1/memory/procedural/requests`) |
| **P4** | Destructive / highest-impact | **Always returns `authorized = False`** (`P4 destructive operations strictly require explicit user confirmation` in `authorize_action()`). No built-in `P4` tools exist; "double confirmation" is not implemented. | Reserved (**PLANNED**) |

> [!NOTE]
> `Settings.autonomy_level` defaults to `"A1"`. Under `A1`, `GovernanceEngine.authorize_action()` sets `authorized = (permission_level == PermissionLevel.P0)`. Therefore, both `P1` and `P2` tools require explicit approval unless autonomy is raised to `A2` or `A3` (or `approval_id` is supplied via a trusted human decision).

---

## 2. Autonomy Tiers (`A0` – `A3`)

Verified from `GovernanceEngine.authorize_action()` (`src/carbon/governance/governance_engine.py:290-308`):

- **`A0` (Observe Only) — IMPLEMENTED:** `authorize_action()` always returns `False` for all permission levels (`P0`–`P4`).
- **`A1` (Assist & Recommend, Default) — IMPLEMENTED:** Auto-authorizes **`P0` only**. `P1`, `P2`, `P3`, and `P4` return `False` and require human approval before tool execution.
- **`A2` (Execute Approved) — IMPLEMENTED:** Auto-authorizes **`P0`, `P1`, and `P2`**. `P3` and `P4` return `False` and require human approval.
- **`A3` (Bounded Autonomy) — PARTIALLY IMPLEMENTED:** Currently shares the exact same code branch as `A2` (`authorized = permission_level in (P0, P1, P2)`). Fine-grained per-directory/per-envelope policy rules (e.g., scoped workspace file edits) are **PLANNED** (`authorize_action()` comment: conservative fail-closed until bounded policy envelopes exist).

---

## 3. Human-in-the-Loop Approval Flow (`IMPLEMENTED`)

Only two paths currently initiate server-side `GovernanceEngine.request_approval()`:
1. **`open <app>` Chat Fast-Path (`P2`):** Handled in `CarbonCore.execute_task_stream()` (`orchestrator.py`). Pauses the task in `TaskState.AWAITING_APPROVAL` and emits the WebSocket event **`"awaiting_approval"`** with:
   - `approval_id`, `task_id`, `tool_id`, `permission_level`, `title`, `arguments`, `impact_summary`, `is_reversible`, `timeout_seconds` (`60.0` default).
2. **Procedural Memory Governance Challenge (`P3`):** Requested via `POST /api/v1/memory/procedural/requests` and resolved via `POST /api/v1/approvals/{approval_id}/decision`.

In `apps/macos/Carbon/Sources/ContentView.swift`, `approvalBannerView` renders an inline banner showing the title, permission badge (`P0`–`P4`), `impactSummary`, `Tool: ... • Args: ...`, and **Deny** / **Approve** buttons (styled orange for `P0`–`P2` or red for `P3`–`P4`).

**Approval Enforcement & Expiry Behavior (`verified in code`):**
- Single-use approvals bind via SHA-256 `arguments_digest` (`compute_arguments_digest`) to the exact `(tool_id, arguments)` pair.
- Only `DecisionSource.USER_UI` (WebSocket `"approve"`/`"deny"`) and `DecisionSource.USER_API` (`POST /api/v1/approvals/{id}/decision`) can resolve an approval; `MODEL_OUTPUT` and `MEMORY_RECORD` cannot.
- If the `60.0s` approval window expires or is denied during `open_application`, `CarbonCore` marks the task `TaskState.CANCELLED`, emits `"approval_resolved"` (`"expired"` or rejected status), a notice `"token"`, and `"cancelled"`, and **exits** (it does not continue running LLM inference).
- General LLM-initiated tool calling (`ToolRegistry.get_schemas_for_llm()`) is not wired into chat streaming yet (**PLANNED**).
