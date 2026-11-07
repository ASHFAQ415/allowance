# ALLOWANCE v4.2.1 — CONSISTENCY & FINANCIAL SAFETY PATCH

**Status:** Proposed corrective patch to the v4.2 frozen specification  
**Date:** November 6, 2026  
**Patch type:** Specification and implementation-plan consistency correction  
**Scope:** Financial correctness, capture uncertainty, accounting, worker ownership, and public API safety  
**Non-goal:** No product redesign, new infrastructure, or feature expansion

## 1. Purpose and change control

v4.2.1 resolves the internal contradictions identified between `ALLOWANCE_V4_2_FINAL_ARCHITECTURE.md` and `ALLOWANCE_V4_2_IMPLEMENTATION_PLAN.md`. It supersedes conflicting v4.2 clauses only where explicitly stated below. All unaffected v4.2 decisions remain in force.

The implementation baseline remains Python 3.11+, FastAPI, Pydantic v2, SQLite WAL, HTTPX, PayPal Orders v2, React/Vite/Tailwind, and the existing bounded worker pool. SQLite remains a single-process deployment constraint.

### Patch acceptance rule

A v4.2.1 implementation is not accepted merely because happy-path tests pass. Every invariant and adversarial test in this patch must pass. If PayPal's external state cannot be established, the system must preserve uncertainty and hold the corresponding budget reservation rather than infer a safe outcome.

## 2. P0 — Capture retry: remove the unsafe APPROVED retry

### 2.1 Binding decision

**No capture POST may be issued again after a capture request may have reached PayPal.** A PayPal `GET order` response showing `APPROVED` is not proof that a previously transmitted capture request was not accepted, is not still processing, or cannot complete later.

Therefore, delete the v4.2 implementation-plan instruction:

> If order is APPROVED and attempts >= 2, transmit one retry capture POST with the identical capture_request_id.

This instruction is prohibited in v4.2.1.

### 2.2 Capture attempt record

Add a durable `capture_operations` table. One logical purchase has one capture operation and one immutable `capture_request_id`.

```sql
CREATE TABLE capture_operations (
    id TEXT PRIMARY KEY,
    purchase_id TEXT NOT NULL UNIQUE,
    capture_request_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL, -- PREPARED, MAY_HAVE_BEEN_SENT, DEFINITIVE_SUCCESS,
                         -- DEFINITIVE_FAILURE, UNKNOWN, RESOLVED_SUCCESS,
                         -- RESOLVED_FAILURE
    first_send_started_at TEXT,
    last_observed_at TEXT,
    paypal_order_status TEXT,
    paypal_capture_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    owner_token TEXT,
    owner_lease_until TEXT,
    last_error_class TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(purchase_id) REFERENCES purchases(id)
);
```

The operation row is created in the same `BEGIN IMMEDIATE` transaction as the exclusive `BUYER_APPROVED -> CAPTURING` claim. Enforce exactly one row per purchase and exactly one stable request ID per operation.

### 2.3 Safe transport classification

| Condition | What can be concluded | Purchase / operation action | Capture POST retry |
|---|---|---|---|
| Local validation fails before HTTP call | No request was sent | `FAILED` with specific local reason; operation `DEFINITIVE_FAILURE` | No |
| `ConnectTimeout` / `ConnectError`, where transport can establish no connection was made | Request not transmitted | Keep exclusive claim only for the single explicitly classified pre-send retry; record attempt and stable ID | At most one retry |
| `WriteTimeout`, `WriteError` | Some bytes may have been transmitted | `CAPTURE_UNKNOWN`; operation `UNKNOWN` | Never |
| `ReadTimeout`, `ReadError`, remote protocol loss after request submission | Request may have been accepted | `CAPTURE_UNKNOWN`; operation `UNKNOWN` | Never |
| HTTP 429 or 5xx | Provider response does not establish non-execution | `CAPTURE_UNKNOWN`; operation `UNKNOWN` | Never |
| HTTP 200/201 with `COMPLETED` and validated capture resource | Definitive success | `CAPTURED`; operation `DEFINITIVE_SUCCESS` | No |
| HTTP 200/201 with `PENDING` | Not final | `CAPTURE_UNKNOWN`; operation `UNKNOWN` | Never |
| HTTP 422 `ORDER_ALREADY_CAPTURED` | Possible prior success; must verify | GET order and validate capture resource; resolve only on verified completed capture | Never |
| HTTP 200/201 `DECLINED` or explicit definitive provider rejection | Definitive failure only if provider response semantics establish no capture | `FAILED`; operation `DEFINITIVE_FAILURE` | No |

Do not assume an exception class alone proves pre-send if the HTTP transport has already begun transmission. The PayPal client must expose an explicit typed outcome indicating whether request transmission could have started. If this cannot be proven, classify as `MAY_HAVE_BEEN_SENT` and do not retry.

### 2.4 Reconciliation protocol

Reconciliation is read-only with respect to the PayPal capture endpoint. It may call `GET /v2/checkout/orders/{id}` and, where supported, fetch the specific capture resource.

- `COMPLETED` plus a matching capture resource, amount, currency, payee, and order reference: resolve success.
- `APPROVED` without a matching capture resource: remain `CAPTURE_UNKNOWN`. Do not retry capture.
- `PENDING`: remain `CAPTURE_UNKNOWN`.
- `VOIDED`, `DECLINED`, or another definitive terminal provider state: resolve failure only when the provider's documented response proves no capture settled.
- Provider unavailable, malformed response, missing capture detail, or inconsistent amount/payee: remain `CAPTURE_UNKNOWN` and raise an operational alert.

The retry budget applies to reconciliation reads, not capture writes. After the bounded reconciliation schedule is exhausted, transition the purchase to `FAILED` with `status_reason='unresolved_capture'`, but preserve the unresolved financial reservation. Exhaustion means “unable to determine”, not “payment failed”.

### 2.5 Crash windows

Before sending the HTTP request, atomically set the operation to `MAY_HAVE_BEEN_SENT` and persist `first_send_started_at`. A crash after this commit but before actual socket transmission may conservatively create a false-positive uncertainty. This is intentional: safety is preferred over duplicate capture risk.

A worker restart must never infer that a capture was not sent merely because no response was persisted.

## 3. P0 — Separate purchase lifecycle from financial resolution

### 3.1 Binding decision

Purchase state and financial settlement are separate dimensions.

- `purchases.status` describes the workflow lifecycle.
- `capture_operations.state` describes the provider capture operation.
- `ledger_adjustments` records financial facts discovered after the workflow reaches a terminal state.
- `events` records audit history.

A terminal purchase state is never rewritten to represent a later-discovered financial fact. Do not create a fake `ledger` state transition from `FAILED` to `RESOLVED_CAPTURED`.

### 3.2 Late capture success

If a purchase is `FAILED(unresolved_capture)` and a later authoritative provider lookup confirms capture:

1. Leave `purchases.status='FAILED'` and `status_reason='unresolved_capture'` unchanged.
2. In one `BEGIN IMMEDIATE` transaction, insert an idempotent `ledger_adjustments` row of type `LATE_CAPTURE_SETTLED`.
3. Resolve the capture operation to `RESOLVED_SUCCESS`.
4. Append `FINANCIAL_DISCREPANCY_RESOLVED` to `events`.
5. Update the budget accounting projection in the same transaction.
6. Do not insert a `ledger` row pretending that purchase state changed.

Use a uniqueness constraint on provider capture identity, not just a generated adjustment ID:

```sql
CREATE UNIQUE INDEX uq_adjustment_provider_capture
ON ledger_adjustments(paypal_capture_id)
WHERE paypal_capture_id IS NOT NULL;
```

Also enforce one adjustment resolution per capture operation. If provider capture IDs are not globally unique in the integration context, use `(provider, merchant_account_id, paypal_capture_id)` as the unique key.

### 3.3 Late definitive failure

A purchase in `FAILED(unresolved_capture)` may be released only after authoritative evidence proves the capture did not occur and cannot later settle. Record a `LATE_CAPTURE_CONFIRMED_NOT_SETTLED` financial resolution event and a corresponding append-only adjustment/resolution record. Never release on timeout, `APPROVED` alone, missing provider data, or retry exhaustion.

## 4. P0 — One authoritative daily-budget accounting model

### 4.1 Binding decision

The policy engine must not derive spend from a loosely defined collection of workflow ledger transitions. Define a single authoritative budget accounting projection keyed by `(agent_id, budget_day, currency)`.

```sql
CREATE TABLE budget_accounting (
    agent_id TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    currency TEXT NOT NULL,
    captured_cents INTEGER NOT NULL DEFAULT 0,
    reserved_cents INTEGER NOT NULL DEFAULT 0,
    unresolved_cents INTEGER NOT NULL DEFAULT 0,
    auto_approved_cents INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(agent_id, budget_day, currency)
);
```

`budget_accounting` is a transactional projection, not an independently editable source. Its values must be derivable from immutable purchase, capture-operation, and adjustment facts. Add a rebuild/verification routine for startup and tests.

### 4.2 Amount classification

For each purchase, exactly one of the following budget classifications applies:

| Financial condition | `reserved_cents` | `unresolved_cents` | `captured_cents` |
|---|---:|---:|---:|
| Active purchase before capture | `total_cents` | 0 | 0 |
| Capture request may have been sent / `CAPTURE_UNKNOWN` | 0 | `total_cents` | 0 |
| `FAILED(unresolved_capture)` | 0 | `total_cents` | 0 |
| Definitive capture success | 0 | 0 | actual validated captured amount |
| Definitive no-capture failure, rejection, block, expiry | 0 | 0 | 0 |

For budget enforcement, `reserved_cents + unresolved_cents + captured_cents` is the committed daily amount. Unresolved amounts remain fully committed until resolved. Captured amounts remain committed for that `budget_day`.

`auto_approved_cents` is a separate subset metric for R5 and must not be added again to the R3 total. It is incremented only for purchases classified as auto-approved under the policy decision, and moves consistently with the purchase's final financial classification. Define whether human-approved purchases are excluded from R5; under this specification they are excluded.

### 4.3 Policy snapshot and exclusion rule

PayPilot remains a pure function. Its injected `ledger_snapshot` is replaced or explicitly defined as an immutable `BudgetSnapshot`:

```python
@dataclass(frozen=True)
class BudgetSnapshot:
    captured_cents: int
    reserved_cents: int
    unresolved_cents: int
    auto_approved_cents: int
    currency: str
    budget_day: str
    version: int
```

R3 uses:

```python
committed = (
    snapshot.captured_cents
    + snapshot.reserved_cents
    + snapshot.unresolved_cents
)
if committed + proposed_total_cents > policy.daily_budget_cents:
    return DENY
```

At P2–P4, exclude the current purchase's own reservation exactly once. The repository must build a snapshot that excludes that purchase's contribution; the policy engine must not subtract it a second time. Document and test this contract.

R5 uses `auto_approved_cents` plus the proposed amount, with the same self-exclusion rule. All amounts must use integer minor units and matching currency. Never use floating-point money.

### 4.4 Atomicity and concurrent admission

Within a single `BEGIN IMMEDIATE` transaction:

1. Read current budget projection/version.
2. Evaluate the checkpoint against the consistent snapshot.
3. If allowed, create or advance the purchase and update the projection.
4. Append the corresponding ledger/event facts.
5. Commit.

Any failed step rolls back all writes. A concurrent request must observe either the full prior transaction or the full committed transaction, never a partial reservation.

For reservation creation, enforce the active reservation cap and daily budget cap in the same serialized transaction. Do not count a purchase twice because it appears in both a state-based query and the projection.

### 4.5 Reconciliation and projection repair

Provide:
- `rebuild_budget_accounting(agent_id, budget_day, currency)` from authoritative immutable facts;
- `verify_budget_accounting(...)` returning expected vs actual values;
- a test that deliberately corrupts the projection, rebuilds it, and proves policy decisions return to the expected result.

Do not silently overwrite unexplained differences in production. Emit an alert and require a controlled repair operation with an audit event.

## 5. P1 — Atomic queue deadline claim

### 5.1 Binding decision

Queue timeout must be enforced in the same atomic SQL claim that changes `QUEUED` to `RUNNING`. Remove the current sequence “claim RUNNING, then inspect age”; it permits a timed-out job to start.

Use a single authoritative clock value `:now` from the application and a deadline computed consistently in the same time domain:

```sql
UPDATE agent_runs
SET status = 'RUNNING',
    started_at = :now,
    worker_id = :worker_id
WHERE id = :run_id
  AND status = 'QUEUED'
  AND queued_at >= :queue_deadline;
```

Here `:queue_deadline = :now - QUEUE_WAIT_TIMEOUT_SECONDS`. If `rowcount == 0`, perform a separate conditional update in a transaction:

```sql
UPDATE agent_runs
SET status = 'FAILED',
    status_reason = 'queue_timeout'
WHERE id = :run_id
  AND status = 'QUEUED'
  AND queued_at < :queue_deadline;
```

Only one conditional update can win. The worker must not invoke the agent unless the claim update affected exactly one row. Store timestamps in one consistent numeric UTC epoch or ISO-8601 UTC representation; do not mix wall-clock and monotonic-clock values in persisted comparisons.

## 6. P1 — Exact worker and queue capacity semantics

### 6.1 Binding limits

Keep v4.2 limits:
- worker threads: 4;
- waiting queue capacity: 4;
- maximum concurrent runs per agent: 2.

Define `queue.Queue(maxsize=4)` as four waiting jobs in addition to up to four running jobs. Therefore, the maximum admitted in-memory workload is eight jobs, subject to the per-agent cap and any global admission cap.

### 6.2 Durable admission protocol

The database is authoritative for run state; the in-memory queue is a delivery mechanism only.

1. Under `BEGIN IMMEDIATE`, check per-agent active count (`QUEUED` + `RUNNING`) and global admission limits.
2. Insert the `QUEUED` run and idempotency record.
3. Commit.
4. Enqueue the run ID.
5. If enqueue fails because the queue is full or shutdown begins, conditionally transition that exact still-QUEUED run to `FAILED(queue_capacity)` or leave it in a durable dispatch-pending state handled by a dispatcher. Do not leave a phantom accepted job.
6. A periodic/startup dispatcher may enqueue durable `QUEUED` rows, using an atomic claim to prevent duplicate execution.
7. Workers must claim in the database before running.
8. A run may be executed at most once. Duplicate queue deliveries are harmless because only one `QUEUED -> RUNNING` claim can succeed.

Prefer a durable dispatch-pending pattern if there is a gap between database commit and in-memory enqueue. Document the selected approach and its crash recovery. Do not claim “no row is created” on queue saturation if the design inserts a row before attempting enqueue; the API contract and transaction order must agree.

### 6.3 Per-agent concurrency

The check and insertion must be serialized in the same `BEGIN IMMEDIATE` transaction. A run counts as active from successful `QUEUED` insertion until terminal completion/failure. Test two simultaneous submissions for the same agent and prove no more than two active runs are admitted.

## 7. P0 — Single-writer ownership for capture resolution

### 7.1 Binding decision

A worker response, reconciler, startup recovery process, and admin action must not independently resolve the same capture operation.

Every capture-operation mutation uses one transactional claim/ownership protocol:

- `owner_token` is a fresh UUID for each claim.
- `owner_lease_until` is persisted.
- Claim is a compare-and-swap under `BEGIN IMMEDIATE`.
- A claimant may commit a result only if its `owner_token` still matches.
- Expired leases may be reclaimed only by a reconciler, and reclaim does not authorize a new capture POST.
- A late worker response is accepted only if it carries the operation identity and the resolution transaction verifies the operation has not already been resolved. If already resolved, it is recorded as a late observation event and cannot overwrite the established resolution.

### 7.2 Atomic resolution transaction

For a verified success or definitive failure:

1. `BEGIN IMMEDIATE`.
2. Read capture operation and purchase.
3. Verify operation is unresolved and claim ownership/idempotency conditions.
4. Validate provider identity, order ID, capture ID, amount, currency, and payee.
5. Insert immutable adjustment if the purchase is already terminal, otherwise perform the permitted purchase transition.
6. Update `capture_operations`.
7. Update `budget_accounting`.
8. Append event and ledger facts appropriate to the lifecycle.
9. Commit.

Unique constraints and compare-and-swap conditions must make duplicate resolution attempts harmless. A conflict must be surfaced as “already resolved” after reading the committed resolution, not as a second financial event.

## 8. P1 — Public API access, tenant isolation, and data minimization

### 8.1 Binding decision

“Public endpoint” means unauthenticated, not globally enumerable. Public read routes must not disclose another agent's purchases, run IDs, policy, spending, event stream, or ledger.

For the hackathon demo, choose one of these explicit modes:

**Mode A — Authenticated demo (recommended):**
- All dashboard data routes require an authenticated demo-session token.
- Admin mutations additionally require `X-Admin-Token`.
- Every query is scoped to the authenticated agent/demo tenant.
- SSE requires the same session and tenant scoping.

**Mode B — Public read-only showcase:**
- Expose only a synthetic, seeded demo tenant.
- No real user or real purchase data.
- Do not expose arbitrary `/purchases/{id}`, `/agent/run/{id}`, `/ledger`, `/policy`, or `/events` records.
- Apply strict pagination, rate limits, and response allowlists.

Do not mix public unscoped data endpoints with private write endpoints.

### 8.2 Route policy

| Route | v4.2.1 access rule |
|---|---|
| `GET /health` | Public, minimal operational status only; no secrets, IDs, or detailed provider diagnostics |
| `POST /agent/run` | Authenticated demo session; rate limited; idempotency key required |
| `GET /agent/run/{id}` | Authenticated and tenant-scoped |
| `GET /events` | Authenticated and tenant-scoped SSE; bounded replay window |
| `GET /purchases` and `GET /purchases/{id}` | Authenticated and tenant-scoped; pagination |
| `GET /ledger` | Authenticated and tenant-scoped; pagination |
| `GET /policy` | Authenticated and tenant-scoped; no secrets |
| `GET /purchases/{id}/approval-link` | Admin-only; never included in public DTO |
| approve/reject/reconcile/freeze/unfreeze/policy update/reset | Admin-only, CSRF/origin protection where cookie sessions are used |

### 8.3 DTO and operational controls

- Use explicit response DTO allowlists; never serialize ORM/database rows directly.
- Never expose PayPal access tokens, client secrets, request IDs, capture IDs, approval URLs, raw provider payloads, trace JSON, stack traces, or internal worker identifiers to public clients.
- Treat capture IDs and order IDs as sensitive operational identifiers even if they are not credentials.
- Enforce pagination and maximum page sizes.
- Apply per-IP and per-session rate limits to reads, writes, and SSE connections.
- Bound SSE connection count and replay history.
- Set retention for public events and logs; do not log payment credentials or full provider payloads.
- Use generic 404 behavior where appropriate to avoid cross-tenant object enumeration.
- Add tests proving tenant A cannot read tenant B's resources by changing IDs, query parameters, or `Last-Event-ID`.

## 9. Canonical state, ledger, and event semantics

The following rules are binding:

1. Purchase state transitions are workflow facts. Each permitted transition is appended to `ledger` in the same database transaction.
2. `ledger` is immutable and contains only valid purchase state transitions. It must not be used for `RESOLVED_CAPTURED` pseudo-states.
3. `ledger_adjustments` records financial facts that occur outside or after the normal purchase state lifecycle, including late capture settlement and confirmed non-settlement.
4. `events` is the append-only operational/audit stream. Events do not replace ledger facts.
5. `budget_accounting` is a rebuildable transactional projection of immutable financial facts.
6. A purchase with `FAILED(unresolved_capture)` remains budget-committed until authoritative resolution.
7. A captured purchase moves value from reserved/unresolved to captured exactly once.
8. Refunds, if implemented, require their own explicit adjustment type and must not retroactively erase the original capture fact.
9. No negative amount or currency conversion is permitted in this patch. All amounts are integer minor units in the purchase currency.

## 10. Required tests and acceptance gates

### 10.1 Capture safety tests

- A timeout after request transmission never causes a second capture POST.
- `GET order = APPROVED` after an ambiguous capture never triggers a retry POST.
- Crash after `MAY_HAVE_BEEN_SENT` and before response persistence recovers to `CAPTURE_UNKNOWN`.
- `COMPLETED` without matching amount/currency/payee/capture identity does not resolve success.
- Duplicate webhook/reconcile/worker success observations create one financial resolution.
- A late success after `FAILED(unresolved_capture)` leaves purchase state unchanged and creates one adjustment.
- A late definitive no-capture result releases unresolved budget exactly once.
- Reconciliation exhaustion does not release budget.

### 10.2 Budget accounting tests

- Active reservation reduces available daily budget.
- Capture moves reserved to captured without changing committed total.
- Ambiguous capture moves reserved to unresolved without changing committed total.
- Terminal unresolved failure continues to count against budget.
- Definitive no-capture resolution releases commitment.
- Late capture success moves unresolved to captured exactly once.
- P2/P3/P4 exclude the current purchase reservation exactly once.
- R5 auto-approved subset is not double-counted in R3.
- Concurrent submissions cannot overspend daily budget.
- Projection rebuild restores deliberately corrupted projection from immutable facts.

### 10.3 Queue and worker tests

- Job older than 20 seconds cannot be claimed RUNNING.
- Concurrent workers cannot claim the same run.
- Duplicate queue delivery does not execute a run twice.
- Queue full and DB commit/enqueue failure paths do not leave phantom accepted runs.
- Per-agent active run cap remains two under concurrent submissions.
- Crash between DB insertion and enqueue is recovered by the durable dispatcher.
- Late worker result after terminal run cannot create a purchase.

### 10.4 Ownership and API tests

- Worker and reconciler racing to resolve one capture produce one result.
- Stale owner token cannot commit after lease takeover.
- Tenant A cannot read tenant B purchase, run, ledger, policy, or SSE events.
- Public health response contains no internal secrets or provider identifiers.
- Public DTOs omit all prohibited fields.
- Pagination, rate limits, SSE connection limits, and event replay bounds are enforced.

### 10.5 Release gate

The patch is accepted only when:
- all tests above pass;
- migration tests pass from a clean v4.2 database and from a populated v4.2 database;
- schema migration is transactional and restart-safe;
- the architecture and implementation plan contain no conflicting capture retry, ledger, budget, queue, ownership, or route-access rules;
- a sandbox capture is verified end-to-end;
- unresolved capture behavior is demonstrated with a mock provider that can delay, drop, and later complete a capture;
- no claim of “exactly once payment” is made. The system provides single logical capture initiation, provider idempotency key reuse, conservative uncertainty handling, and idempotent local resolution—not a distributed exactly-once guarantee.

## 11. Migration and implementation order

1. Freeze v4.2 docs as historical baseline; create v4.2.1 architecture and implementation plan.
2. Add schema migration for `capture_operations` and `budget_accounting`; retain old ledger rows unchanged.
3. Build deterministic budget projection/rebuild and verification tests.
4. Implement capture operation ownership and no-retry-after-possible-send behavior.
5. Implement read-only reconciliation and late adjustment resolution.
6. Fix atomic queue claim/deadline and durable dispatch recovery.
7. Apply tenant-scoped API access, DTO allowlists, pagination, and rate limits.
8. Run the full adversarial suite and sandbox verification.
9. Update README/demo claims to describe the actual tested guarantees and limitations.

Do not add Kafka, Redis, PostgreSQL, Kubernetes, distributed workers, or a new payment provider as part of v4.2.1. Those would expand scope and do not solve the immediate specification contradictions.

## 12. Final guarantee statement

Allowance v4.2.1 is designed to ensure that:

- an ambiguous capture is never blindly retried;
- unresolved money remains budget-committed;
- late provider facts are recorded without mutating terminal purchase history;
- one capture operation has one stable identity and one serialized local resolution;
- daily budget decisions account for captured, reserved, and unresolved amounts exactly once;
- queue timeout and worker ownership are enforced atomically;
- users can access only the data belonging to their authorized demo tenant.

These are design guarantees only until the corresponding implementation, migration, adversarial tests, and PayPal sandbox evidence pass. The specification must never be presented as proof that the running application already satisfies them.
