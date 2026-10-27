# Carbon Permission & Autonomy Matrix Specification

## 1. Five-Tier Permission Ladder (P0 – P4)

Every capability, tool, and system interaction in Carbon is classified under an immutable permission tier reflecting its potential system impact and reversibility.

```
┌────────────────────────────────────────────────────────────────────────┐
│                      PERMISSION TIER LADDER                            │
├────┬─────────────────────────────┬────────────────────────────────────┤
│ P0 │ Conversational              │ Read-only text, reasoning, facts   │
├────┼─────────────────────────────┼────────────────────────────────────┤
│ P1 │ Read Approved               │ System metrics, app listings       │
├────┼─────────────────────────────┼────────────────────────────────────┤
│ P2 │ Reversible Execution        │ Approved app launch, clipboard     │
├────┼─────────────────────────────┼────────────────────────────────────┤
│ P3 │ State Modification          │ File write, calendar, network      │
├────┼─────────────────────────────┼────────────────────────────────────┤
│ P4 │ Sensitive / Destructive     │ File deletion, Keychain, restarts  │
└────┴─────────────────────────────┴────────────────────────────────────┘
```

### Detailed Classification Table:

| Tier | Name | Reversibility | Impact Level | Confirmation Policy | Examples |
|---|---|---|---|---|---|
| **P0** | **Conversational** | Perfectly reversible | Zero system impact | Never required | Answering general questions, explaining concepts, parsing in-memory text. |
| **P1** | **Read Approved** | Read-only | Zero modification | Auto-approved in A1–A3; manual in A0 | Reading CPU/RAM load, checking battery percentage, listing installed `/Applications`. |
| **P2** | **Reversible Action** | Easily dismissed | Low impact | Auto-approved in A2/A3; single-click in A1 | Launching approved macOS apps (Safari, Notes, Calculator), copying text to clipboard. |
| **P3** | **State Modification** | Permanent / Non-trivial | Moderate to High | **Always Requires Explicit Confirmation** | Modifying user files, creating calendar entries, making authorized HTTP requests. |
| **P4** | **Sensitive / Destructive** | Irreversible / Critical | Severe | **Mandatory Double Confirmation or Hard Block** | Deleting files, modifying permissions, accessing macOS Keychain, executing system reboot. |

---

## 2. Autonomy Tiers (A0 – A3)

The autonomy level dictates the default authorization behavior of Carbon Core when tools are requested by the model:

```
┌─────────┬──────────────────────┬─────────────┬─────────────┬─────────────┬─────────────┐
│ Mode    │ Description          │ P0 Auto?    │ P1 Auto?    │ P2 Auto?    │ P3/P4 Auto? │
├─────────┼──────────────────────┼─────────────┼─────────────┼─────────────┼─────────────┤
│ **A0**  │ Observe Only         │ Manual      │ Manual      │ Manual      │ Manual      │
│ **A1**  │ Assist & Recommend   │ **Yes**     │ Card Prompt │ Card Prompt │ Modal Prompt│
│ **A2**  │ Execute Approved     │ **Yes**     │ **Yes**     │ **Yes**     │ Modal Prompt│
│ **A3**  │ Bounded Autonomy     │ **Yes**     │ **Yes**     │ **Yes**     │ Bounded Auto│
└─────────┴──────────────────────┴─────────────┴─────────────┴─────────────┴─────────────┘
```

- **A0 (Observe Only):** Carbon operates strictly in an advisory role. No tool executions are permitted without individual manual authorization.
- **A1 (Assist & Recommend — Default):** P0 operations execute automatically. P1–P4 operations generate interactive approval cards in the UI.
- **A2 (Execute Approved):** Carbon automatically executes benign read (P1) and reversible (P2) operations; immediately halts and prompts for any P3/P4 operation.
- **A3 (Bounded Autonomy):** Carbon autonomously executes multi-step workflows across P0–P3 strictly within a pre-declared policy envelope (e.g. "Allowed to modify files strictly inside `~/Projects/Carbon`"). P4 is always blocked or double-confirmed.

---

## 3. Human-in-the-Loop Confirmation UI Contract

When a requested tool requires confirmation, Carbon Core transitions the task to `AWAITING_APPROVAL` and emits an event over the loopback WebSocket to the native SwiftUI frontend.

### JSON Payload Schema:
```json
{
  "event": "awaiting_approval",
  "task_id": "task-a1b2c3d4",
  "tool_id": "open_application",
  "permission_level": "P2",
  "title": "Launch Application Request",
  "arguments": {
    "app_name": "Safari"
  },
  "impact_summary": "Launches the Safari desktop web browser on macOS.",
  "is_reversible": true,
  "timeout_seconds": 60.0
}
```

### UI Modal Behavior:
1. **Visual Prominence:** P2 displays as an inline action card. P3/P4 display as a modal sheet requiring an explicit click on **"Approve"** or **"Deny"**.
2. **Argument Inspection:** Parameters are displayed with syntax-highlighted JSON or visual file diffs.
3. **Automatic Timeout:** If unapproved within 60 seconds, Carbon Core auto-cancels the action and resumes conversational execution.
