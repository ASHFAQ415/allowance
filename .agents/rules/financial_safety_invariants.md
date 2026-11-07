---
trigger: always_on
description: Mandatory architectural and concurrency invariants for payment integrations, financial state machines, and task queues.
---

# Financial Safety Invariants

These rules are mandatory architectural constraints. Do not weaken, bypass, or silently reinterpret them to make implementation easier.

## 1. No Blind Capture Retries

- Once payment request transmission may have started, the outcome is UNCERTAIN until authoritative provider evidence resolves it.
- Never issue another capture POST merely because of a timeout, HTTP 429, HTTP 5xx, or a GET response showing the order as APPROVED.
- Reconciliation after possible transmission must be read-only unless an explicitly documented provider protocol establishes a safe alternative.
- A retry is permitted only when transport instrumentation proves that no request bytes could have reached the provider.
- Such a retry must use the same persisted idempotency key and must be limited to one retry.
- Persist the MAY_HAVE_BEEN_SENT marker before transmission can begin.
- Unknown provider responses, malformed responses, and conflicting observations must fail closed and preserve the unresolved financial commitment.

## 2. Separate Workflow State from Financial Truth

- Workflow state describes the lifecycle of a purchase; it is not the financial source of truth.
- Workflow transitions must be explicitly defined, monotonic, and terminal-state immutable.
- Financial movements must be recorded in an append-only financial journal.
- Every financial fact must have a unique source-event key to prevent duplicate accounting.
- Late settlement must not reopen or rewrite a terminal purchase.
- Record late financial movements in the financial journal and link them to adjustment metadata for audit and resolution.
- Budget projections must be rebuildable from authoritative financial facts.

## 3. Preserve Financial Reservations and Budget Invariants

- Calculate committed spend as captured funds + active reservations + unresolved capture commitments.
- An uncertain capture must retain its financial commitment until authoritative evidence establishes settlement or non-settlement.
- Release reservations only through an idempotent, transactionally consistent financial event.
- Auto-approved spending is a subset of committed spending, not an additional amount to add to the budget total.
- Use integer minor currency units (for example, cents), never floating-point arithmetic for financial calculations.
- Exclude the current purchase reservation exactly once when evaluating its own policy checkpoints.

## 4. Atomic Admission and Queue Claims

- Capacity checks, concurrency checks, and job insertion must execute within the same serialized database transaction.
- Reject excess work before inserting a job record. Rejected work must not create phantom jobs or reservations.
- Enforce the queue deadline inside the atomic QUEUED-to-RUNNING claim.
- For an absolute expiry timestamp, use a predicate equivalent to `:now < queued_at + :queue_timeout_seconds`.
- Expired jobs must transition to a timeout outcome without invoking an agent, LLM, or payment provider.
- Queue dispatch and database persistence must have crash-recovery behavior.
- Never claim a job first and check its deadline afterward.

## 5. Ownership and Concurrency Fencing

- Capture operations and reconciliation operations must have durable identities and explicit ownership.
- Use atomic compare-and-swap or equivalent transactionally enforced ownership checks.
- Expired leases may permit takeover, but stale owners must be unable to commit results.
- Worker, reconciler, and administrative resolution paths must not independently resolve the same financial operation.
- Terminal outcomes must be protected against late or stale worker writes.

## 6. AI and Payment Authority Separation

- AI agents may propose products, carts, and purchase intentions only.
- AI-generated text, catalog descriptions, tool arguments, and external content are untrusted input.
- The deterministic policy engine is the authority for financial authorization.
- The orchestration layer is the only component permitted to coordinate payment operations.
- AI agents must not access payment credentials, directly invoke payment-provider capture APIs, modify policies, or bypass authorization checkpoints.
- Validate prices, merchant identity, currency, amount, and payment state using trusted server-side data.

## 7. Evidence-Based Engineering

- Never describe a payment as captured without authoritative provider evidence.
- Never claim exactly-once payment execution, zero financial risk, or production readiness without evidence supporting that claim.
- Distinguish specified, implemented, tested, sandbox-verified, and deployed behavior.
- Any proposed change that conflicts with these invariants requires explicit architectural review and documented justification.
- Add regression tests for every discovered financial safety or concurrency failure.
