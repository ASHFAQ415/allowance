# ALLOWANCE v4.2.1 — CONSOLIDATED ARCHITECTURE ADDENDUM

**Date:** November 6, 2026  
**Authority:** This addendum is normative and overrides conflicting v4.2 architecture clauses. All unchanged v4.2 architecture remains applicable.

## 1. Architecture invariants

1. ShopMind is an untrusted proposal generator and has no access to policy storage, payment credentials, PayPal APIs, or financial state.
2. PayPilot is a pure deterministic policy function. It receives a frozen cart, policy version, `BudgetSnapshot`, explicit timestamp, checkpoint, and optional purchase exclusion already reflected in the snapshot.
3. The Orchestrator is the only component allowed to coordinate state transitions, budget projection changes, capture operations, and provider calls.
4. PayPal client methods return typed provider observations. They never write application database state.
5. SQLite WAL is authoritative for this single-process deployment. All multi-row financial mutations use `BEGIN IMMEDIATE`.
6. The in-memory queue is not durable truth. Database run state plus a dispatcher/recovery mechanism is authoritative.

## 2. Canonical data ownership

| Data | Authority | Mutability |
|---|---|---|
| Purchase workflow state | `purchases` + immutable `ledger` transition history | State changes only through validated transitions |
| Capture attempt/uncertainty | `capture_operations` | Controlled CAS updates; identity immutable |
| Settled/late financial facts | `ledger_adjustments` | Append-only |
| Daily budget totals | `budget_accounting` projection | Transactionally updated; rebuildable from facts |
| Operational/audit timeline | `events` | Append-only |
| Policy configuration | `policy` + `policy_history` | Versioned; history immutable |

## 3. Capture lifecycle

```text
BUYER_APPROVED
   |
   | BEGIN IMMEDIATE: P4 + exclusive claim + capture operation
   v
CAPTURING / PREPARED
   |
   | persist MAY_HAVE_BEEN_SENT before network transmission
   v
CAPTURING / MAY_HAVE_BEEN_SENT
   |                         \
   | verified COMPLETED       \ ambiguous transport / 429 / 5xx / PENDING
   v                           v
CAPTURED                  CAPTURE_UNKNOWN
                               |
                               | read-only reconciliation
                    +----------+-----------+
                    |                      |
             verified success       definitive no-capture
                    |                      |
                    v                      v
             CAPTURED or              FAILED with a
             late adjustment          confirmed resolution
                    |
                    +-- after terminal unresolved failure:
                        keep purchase FAILED(unresolved_capture);
                        append LATE_CAPTURE_SETTLED adjustment
```

**Forbidden:** a capture POST retry after the request may have been transmitted. `GET order = APPROVED` is not sufficient evidence to retry.

## 4. Budget formula

For an agent and immutable budget day:

`committed_cents = captured_cents + reserved_cents + unresolved_cents`

R3 denies when:

`committed_cents + proposed_total_cents > daily_budget_cents`

R5 uses `auto_approved_cents` as a subset metric and must not double-count it in R3. At P2–P4 the repository constructs a snapshot with the current purchase's own reservation excluded exactly once.

## 5. Worker pool

- Four worker threads.
- Four waiting queue slots.
- Up to eight total admitted jobs (four running + four waiting), subject to the two-active-runs-per-agent limit and global admission rules.
- A run is claimable only if `status='QUEUED'` and `queued_at >= now - 20 seconds`.
- Claim and deadline check occur in one SQL update.
- Database state is authoritative; duplicate queue delivery is harmless.
- DB commit/enqueue gaps are recovered by a durable dispatcher.
- A worker may create a purchase only while it owns a valid RUNNING claim.

## 6. Public data boundary

All non-health data APIs require an authenticated demo session and tenant scope, except the deliberately synthetic public showcase mode. Admin mutations and approval links require admin authorization. DTO allowlists, pagination, rate limits, SSE limits, and event replay bounds are mandatory.

## 7. Explicit non-guarantees

- No distributed exactly-once payment guarantee.
- No cancellation of a capture already being processed by PayPal.
- No horizontal scaling across multiple application processes with SQLite.
- No release of unresolved funds based solely on timeout, retry exhaustion, or an `APPROVED` order status.
- No claim that a design invariant is implemented until its tests and sandbox evidence pass.
