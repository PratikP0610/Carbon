# Carbon Permission & Autonomy Architecture

## 1. Permission Matrix (P0 – P4)

Every capability and tool registered in Carbon is assigned an immutable permission classification based on its potential system impact.

| Level | Classification | Description | User Confirmation | Examples |
|---|---|---|---|---|
| **P0** | **Conversational** | Pure reasoning, conversational dialogue, status checks. | Never required | Answering questions, summarizing in-context text. |
| **P1** | **Read Approved** | Reading approved system state, window titles, hardware metrics. | Auto-approved in A1–A3 | Getting battery status, checking available disk space. |
| **P2** | **Reversible Action** | Non-destructive operations that can be easily undone or dismissed. | Auto-approved in A2/A3; single-click in A1 | Opening a macOS app (e.g. Safari, Notes), copying to clipboard. |
| **P3** | **State Modification** | Permanent modifications to files, configuration, or external network requests. | **Always Requires Explicit Confirmation** | Modifying user files, creating calendar events, sending API requests. |
| **P4** | **High-Impact / Destructive** | Irreversible actions, file deletion, system restarts, credential access. | **Double Confirmation or Prohibited** | Deleting directories, clearing system caches, accessing Keychain. |

---

## 2. Autonomy Tiers (A0 – A3)

Carbon's autonomy level dictates how proactively it may execute tools across permission classifications without manual intervention.

### A0: Observe Only
- Carbon is strictly an advisor and informational assistant.
- Zero tools may execute automatically.
- Every tool action proposed by the model requires explicit manual triggering.

### A1: Assist & Recommend (Default)
- Carbon formulates execution plans and recommends next steps.
- P0 operations execute automatically.
- P1–P4 operations generate an interactive approval card in the UI. The user clicks "Approve" or "Deny".

### A2: Execute Approved
- Carbon auto-executes benign operations (**P0**, **P1**, and **P2**).
- **P3** and **P4** operations immediately halt execution and trigger a high-visibility modal confirmation dialog.

### A3: Bounded Autonomy
- Carbon can execute multi-step workflows across P0–P3 within strictly defined, pre-authorized policy envelopes (e.g. "You may read and edit files strictly inside `~/Projects/Carbon`").
- Any action outside the declared envelope or any P4 action immediately triggers a hard confirmation prompt.

---

## 3. The Confirmation UI Contract

When an action requires confirmation, Carbon Core dispatches an `AWAITING_APPROVAL` event to the SwiftUI client containing:
1. **Tool Identifier & Description**
2. **Permission Level (P2, P3, P4)**
3. **Exact Arguments (Pretty-printed JSON / Diffs)**
4. **Estimated Side Effects & Rollback Instructions**
5. **Timeout (Default 60 seconds; auto-cancels if unconfirmed)**

Execution remains strictly suspended until an authenticated affirmative approval event is received over the loopback WebSocket.
