# Learning Proposal: Financial State Machine & Adversarial Payment Protocol Guardrails

## 1. Context & Motivation
During the deep architectural analysis of the **Allowance** platform across iterations v4 -> v4.1 -> v4.2 -> v4.2.1 -> v4.2.2, critical design contradictions were uncovered and resolved:
1. Retrying payment captures after ambiguous responses (e.g. read timeout or PayPal returning `APPROVED`) risks catastrophic duplicate charges.
2. Conflating workflow state machines with financial settlement records forces unsafe cyclic transitions (e.g., trying to flip `FAILED` to `CAPTURED`).
3. Checking queue deadlines *after* claiming a job allows expired jobs to start executing.

## 2. Classification: Rules vs. Skills
- **Type**: **Rule** (Universal Architectural & Financial Safety Invariant)
- **Target File**: `.agents/rules/financial_safety_invariants.md` (or workspace rule `GEMINI.md`)
- **Scope**: Applied whenever engineering financial state machines, payment gateway integrations, or bounded worker queues.

## 3. Proposed Rule Content

```markdown
---
trigger: always_on
description: Mandatory architectural and concurrency invariants for payment integrations, financial state machines, and task queues.
---

# Financial State Machine & Payment Protocol Invariants

1. **Zero Blind Capture Retries on Ambiguous Transport:**
   - If HTTP request transmission to a payment provider may have started (`WriteTimeout`, `ReadTimeout`, 429, 5xx), the transaction outcome is UNCERTAIN.
   - NEVER issue a subsequent capture POST based on a query or timeout.
   - Subsequent verification MUST be strictly read-only (GET order/capture resource).
   - Only proven pre-transmission connect failures (before any socket bytes were sent) may retry once with the identical idempotency key.

2. **Decouple Workflow Lifecycle from Financial Accounting:**
   - The entity state machine (`purchases.status`) describes workflow progression only and must remain strictly forward-only and terminal-immutable.
   - Financial movements MUST be recorded in an append-only journal (`financial_facts`).
   - Late-discovered financial facts (e.g., a capture settling after reconciliation was exhausted) MUST be recorded in an adjustment table (`ledger_adjustments`) without mutating the terminal workflow state.

3. **Atomic Admission & Queue Deadline Claims:**
   - Enforce capacity and concurrency limits *before* inserting job records; reject excess requests (HTTP 503) without creating phantom rows.
   - Enforce queue deadlines within the atomic SQL claim:
     `UPDATE jobs SET status = 'RUNNING' WHERE id = :id AND status = 'QUEUED' AND queued_at >= :deadline`.
     Never claim first and check age afterward.
```

---

## 4. Next Steps
Once approved, this rule can be added to `.agents/rules/financial_safety_invariants.md` in the project workspace.
