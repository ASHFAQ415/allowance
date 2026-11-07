# ALLOWANCE v4.2.2 — CONSOLIDATED RELEASE SPECIFICATION

**Release:** v4.2.2  
**Date:** November 6, 2026  
**Status:** FINAL DOCUMENTATION BASELINE — implementation and verification status must be reported separately  
**Supersedes:** Conflicting v4.2 and v4.2.1 clauses identified in the release register  
**Product tagline:** *Give your AI agent an allowance, not your wallet.*

---

## 0. Release authority and truthfulness

This document is the normative source of truth for the v4.2.2 release. Companion documents define the adversarial tests, guarantee classification, implementation sequence, provider verification, and residual risks. If a companion document conflicts with this specification, this specification wins and the conflict must be corrected before release.

This is a **specification freeze**, not a statement that the application has been implemented, tested, sandbox-verified, or deployed. Every claim in the demo, README, Devpost submission, and pitch must be labelled as one of:
- **Specified:** documented design only.
- **Implemented:** code exists and is reviewable.
- **Tested:** named automated tests pass in the recorded environment.
- **Sandbox-verified:** a dated PayPal sandbox experiment has reproducible evidence.
- **Deployed:** the stated build is running at the stated URL.

No “exactly once payment”, “zero risk”, “foolproof”, or “100% immune” claims are permitted.

## 1. Product and scope

Allowance is an independent financial authorization layer between AI shopping agents and payment execution.

- **ShopMind:** untrusted shopping proposal agent. It searches the seeded catalog and proposes product IDs and quantities. It cannot read policy, database, payment credentials, or PayPal APIs.
- **PayPilot:** pure deterministic policy engine implementing R0–R6 at checkpoints P1–P4.
- **Allowance Orchestrator:** sole coordinator for admission, state transitions, budget reservations, capture-operation lifecycle, and provider calls.
- **PayPal Orders v2:** hackathon payment rail. A buyer consent action is required in the sandbox flow.
- **Dashboard:** React 18, Vite, Tailwind, API and bounded SSE/polling interface.

### 1.1 Frozen stack and deployment model

Python 3.11+, FastAPI, Pydantic v2, SQLite WAL with `synchronous=FULL`, HTTPX, PayPal Orders v2, Claude API, React 18, Vite, Tailwind, Pytest, Hypothesis, Playwright. One Uvicorn process with four internal worker threads. SQLite is not a multi-process or horizontally scaled database in this release.

No Kafka, Redis, PostgreSQL, Kubernetes, distributed worker fleet, additional payment provider, automated refunds, chargebacks, or real merchant split settlement is added by v4.2.2.

## 2. Binding architecture boundaries

1. `agent/` cannot import `orchestrator/`, `api/`, `paypal/`, `db/`, or `policy/`.
2. `policy/` is pure: no I/O, DB access, network, or system clock reads. Time and accounting snapshots are injected.
3. `paypal/` returns typed provider observations and never writes application DB state.
4. `orchestrator/` is the only writer/coordinator for purchase, capture, journal, projection, and run lifecycle.
5. SQLite multi-row financial mutations use `BEGIN IMMEDIATE`; related facts, state transition, projection updates, and audit event commit or roll back together.
6. The in-memory queue is a delivery mechanism. Durable run rows are authoritative; dispatcher recovery handles process interruption.
7. All money uses integer minor units and a single currency per budget bucket. No floating-point arithmetic or implicit currency conversion.

## 3. Policy engine

`evaluate(cart, policy_version, budget_snapshot, now, checkpoint) -> Decision`

Rules are evaluated in order:

| Rule | Condition | Result |
|---|---|---|
| R0 | Emergency freeze active | DENY |
| R1 | Any merchant outside allowlist | DENY |
| R2 | Cart exceeds per-transaction cap | DENY |
| R3 | Captured + reserved + unresolved commitment plus proposed amount exceeds daily budget | DENY |
| R4 | Cart exceeds human-approval threshold | REVIEW at P1 |
| R5 | Auto-approved commitment plus proposed amount exceeds velocity cap | REVIEW at P1 |
| R6 | No earlier rule triggers | ALLOW |

Checkpoints:
- **P1:** cart submission; R0–R6.
- **P2:** human approval; R0–R3.
- **P3:** before PayPal order creation; R0–R3.
- **P4:** after buyer approval, immediately before exclusive capture claim; R0–R3.

At P2–P4, the repository constructs a snapshot that excludes the current purchase’s own reservation **exactly once**. PayPilot does not subtract it again. Policy timezone is immutable through ordinary policy updates. `budget_day` is fixed at purchase creation and remains attached to the purchase throughout its lifecycle.

### 3.1 R3 and R5 definitions

`R3_committed = captured_cents + reserved_cents + unresolved_cents`.

`R5_auto_approved_committed_cents` is a subset metric, not an additional R3 amount. It tracks the portion of current budget-day commitment originating from P1 auto-approvals. It is incremented on auto-approved reservation, moves between reserved/unresolved/captured classifications without being double-counted, and is reduced only when the corresponding commitment is definitively released. Human-approved purchases do not contribute to R5.

## 4. Purchase workflow and financial lifecycle are separate

`purchases.status` is the workflow state. `capture_operations.state` is the provider operation state. `financial_facts` is the append-only source of financial movements. `budget_accounting` is a rebuildable projection. `ledger` contains valid purchase workflow transitions only. `ledger_adjustments` records late or exceptional financial facts. `events` is the operational/audit stream.

### 4.1 Purchase states

The existing v4.2 forward-only purchase states remain: `CREATED`, `CART_READY`, `PENDING_APPROVAL`, `AUTO_APPROVED`, `APPROVED`, `PAYPAL_ORDER_CREATED`, `BUYER_APPROVED`, `CAPTURING`, `CAPTURE_UNKNOWN`, and terminal states `CAPTURED`, `BLOCKED`, `REJECTED`, `EXPIRED`, `FAILED`.

`FAILED(status_reason='unresolved_capture')` is terminal for workflow history but **not financially released**. The unresolved financial commitment remains until authoritative provider evidence resolves it.

No fake purchase transition to `RESOLVED_CAPTURED` is permitted. Late capture success leaves the purchase terminal and inserts a financial adjustment/fact.

## 5. Capture safety — final binding protocol

### 5.1 One logical operation

There is exactly one `capture_operations` row per purchase and one immutable UUID4 `capture_request_id` per logical capture operation. A unique DB constraint enforces both. The capture operation has a stable operation ID, provider order ID, request ID, status, attempt count, owner token/lease, timestamps, last provider observation, and capture ID if verified.

### 5.2 Capture POST rule

**At most one capture POST may be transmitted after the request may have reached PayPal.** The only permitted second POST is one retry after a transport outcome proves that no request bytes could have been transmitted and the operation remains in `PREPARED`. If the transport cannot prove pre-transmission failure, the result is ambiguous and no retry is permitted.

The transport adapter must expose a typed outcome with `transmission_started` / `bytes_may_have_been_sent`; do not infer safety from a broad exception name alone. A pre-send retry is allowed only when:
- no request body/header bytes could have reached the provider;
- the operation remains `PREPARED`;
- the original attempt is recorded;
- the same request ID is reused;
- the one-retry limit has not been consumed.

Immediately before the transport can write request bytes, persist `MAY_HAVE_BEEN_SENT` transactionally. A crash after this persistence but before actual network transmission is conservatively uncertain. This false-positive uncertainty is intentional.

### 5.3 Outcome mapping

| Outcome | Operation / purchase result | Capture POST retry |
|---|---|---|
| Local validation failure before network | `DEFINITIVE_FAILURE`; `FAILED` with precise reason | No |
| Proven connection failure before any bytes | Remain `PREPARED`; record typed pre-send failure | One retry maximum |
| Write timeout/error or any possible partial transmission | `UNKNOWN`; `CAPTURE_UNKNOWN` | Never |
| Read timeout/error after transmission | `UNKNOWN`; `CAPTURE_UNKNOWN` | Never |
| HTTP 429 or 5xx | `UNKNOWN`; `CAPTURE_UNKNOWN` | Never |
| HTTP success with validated `COMPLETED` capture resource | `DEFINITIVE_SUCCESS`; `CAPTURED` | No |
| HTTP success with `PENDING` | `UNKNOWN`; `CAPTURE_UNKNOWN` | Never |
| `ORDER_ALREADY_CAPTURED` | Read-only GET and validate capture resource | Never |
| `PAYER_ACTION_REQUIRED` / instrument decline | Map only according to provider semantics; a definitive no-settlement result may become `DEFINITIVE_FAILURE`; otherwise UNKNOWN | Never |
| Unknown/malformed provider response | `UNKNOWN`; preserve commitment and alert | Never |

### 5.4 Reconciliation is read-only

Reconciliation calls GET order / capture resources only. It never issues a capture POST. `APPROVED` without a matching completed capture resource is **not** proof that an earlier ambiguous capture will never complete. It remains `CAPTURE_UNKNOWN`.

Resolve success only after validating order ID, provider capture ID, status `COMPLETED`, amount, currency, payee, and purchase reference. Resolve failure/release only when authoritative provider evidence establishes that no capture settled and cannot later settle. Timeout, retry exhaustion, missing data, `APPROVED`, 429, 5xx, or an unknown state never release budget.

Reconciliation claims are single-flight CAS operations: claim and attempt schedule are committed before the network read. Owner tokens/leases prevent a stale worker, reconciler, startup process, or admin action from racing a resolution.

### 5.5 Late resolution

If `FAILED(unresolved_capture)` later resolves as captured:
- leave purchase status and terminal workflow ledger unchanged;
- insert one idempotent `LATE_CAPTURE_SETTLED` adjustment;
- append `FINANCIAL_DISCREPANCY_RESOLVED`;
- update financial facts and budget projection in one transaction;
- mark capture operation `RESOLVED_SUCCESS`.

If authoritative evidence proves no capture can settle, append a `LATE_CAPTURE_CONFIRMED_NOT_SETTLED` financial fact/adjustment, release unresolved commitment exactly once, and mark operation `RESOLVED_FAILURE`.

No admin action may manually release unresolved budget without recorded authoritative evidence and an audit trail.

## 6. Authoritative financial accounting

### 6.1 Append-only financial facts

`financial_facts` is the rebuild source for the daily budget projection. Each fact has a unique idempotency key, tenant/agent, purchase, budget day, currency, event type, signed deltas for reserved/unresolved/captured/auto-approved commitment, provider identity where applicable, actor, and UTC timestamp. UPDATE and DELETE are prohibited by DB triggers.

Examples:
- `RESERVATION_CREATED`: `reserved +amount`; if auto-approved, `auto_approved +amount`.
- `RESERVATION_RELEASED`: `reserved -amount`; if auto-approved, `auto_approved -amount`.
- `CAPTURE_BECAME_UNKNOWN`: `reserved -amount`, `unresolved +amount`; auto-approved subset unchanged.
- `CAPTURE_SETTLED`: `unresolved -amount` (or `reserved -amount` if direct definitive success), `captured +actual_amount`; auto-approved subset unchanged except any amount difference must be explicitly reconciled.
- `UNRESOLVED_CONFIRMED_NOT_SETTLED`: `unresolved -amount`; auto-approved subset decreases if that was its source.
- `LATE_CAPTURE_SETTLED`: `unresolved -amount`, `captured +actual_amount`; auto-approved subset remains represented once.

Each logical event uses a stable unique `source_event_key`. Provider capture identity is unique within `(provider, merchant_account_id, paypal_capture_id)`. Duplicate facts are rejected/idempotently treated as already applied.

### 6.2 Projection

`budget_accounting(agent_id, budget_day, currency)` contains `captured_cents`, `reserved_cents`, `unresolved_cents`, `auto_approved_committed_cents`, `version`, and `updated_at`. It is updated in the same transaction as the financial fact and rebuilt by summing immutable facts. It is never manually edited.

R3 uses captured + reserved + unresolved. R5 uses the auto-approved subset. Projection rebuild/verification compares expected vs actual. Production mismatch emits an alert and requires a controlled audited repair; never silently rewrite financial facts.

### 6.3 Atomic reservation

Admission, active reservation cap, daily budget snapshot, policy evaluation, purchase creation, financial fact insertion, projection update, ledger/event writes all occur under one `BEGIN IMMEDIATE` transaction. Rejection creates no purchase and no reservation unless the documented API idempotency record is intentionally retained separately.

## 7. Queue, worker and run lifecycle

- Four worker threads.
- Four waiting slots.
- Up to eight admitted runs total (four running + four waiting), subject to maximum two active runs per agent.
- Admission checks the durable active run count and all limits inside `BEGIN IMMEDIATE` **before** inserting the run. A ninth active run is rejected with 503 and creates no `agent_runs` row.
- After admission commit, the dispatcher delivers queued run IDs. A DB-commit-to-enqueue crash gap is recovered by scanning durable `QUEUED` rows. Temporary in-memory queue fullness delays dispatch; it does not create a phantom accepted run or fail a valid durable run.
- Worker claim is one conditional SQL update requiring `status='QUEUED'` and `queued_at >= :queue_deadline`. If not claimed, the worker does not invoke ShopMind. An expired queued row is conditionally marked `FAILED(queue_timeout)`.
- Active per-agent count includes `QUEUED` + `RUNNING`, updated/checked atomically.
- Duplicate queue delivery is harmless; only one CAS can claim.
- Run execution is bounded to six model calls and 45 seconds. A separate worker lease/run deadline handles process interruption. A run is not failed merely because it waited in a valid queue slot.
- On restart, queued rows are re-dispatched if still within queue deadline; stale queued rows become `FAILED(queue_timeout)`. Previously RUNNING rows become `FAILED(interrupted)` and late results are rejected. Capture operations in uncertain send state become `CAPTURE_UNKNOWN`, never retried.

Persisted timestamps use UTC epoch milliseconds or normalized UTC ISO-8601 consistently. Do not compare monotonic-clock values persisted across process restarts.

## 8. Authentication, tenant boundary and public demo

This release does **not** add general-purpose multi-tenant login, RBAC, or customer account management.

It implements a single configured hackathon demo tenant:
- one configured `DEMO_TENANT_ID`;
- a high-entropy `DEMO_SESSION_TOKEN` required for dashboard data reads, agent runs, and SSE;
- `X-Admin-Token` required for approve/reject/freeze/policy/reset/approval-link/reconciliation administration;
- constant-time token comparison and IP/session rate limiting;
- every DB read/write is scoped to the configured tenant ID;
- a synthetic public showcase may expose only seeded non-user demo data and cannot enumerate live records.

`GET /health` is public and minimal. All purchases, runs, ledger, policy, and event routes require the demo session. Admin operations require admin auth. Approval URLs and provider IDs are admin-only and never included in public DTOs. Cookie-based sessions require CSRF/origin protection; bearer headers are preferred for this hackathon dashboard.

Use explicit DTO allowlists, generic 404 for cross-tenant IDs, bounded pagination, SSE connection/replay limits, retention, and redacted structured logs.

## 9. API contract summary

| Route | Access | Required behavior |
|---|---|---|
| `GET /health` | Public | Minimal status only; no provider IDs/secrets |
| `POST /agent/run` | Demo session | Idempotency key; tenant scope; admission before row insert |
| `GET /agent/run/{id}` | Demo session | Tenant-scoped |
| `GET /purchases`, `GET /purchases/{id}` | Demo session | Tenant-scoped, paginated |
| `GET /ledger` | Demo session | Tenant-scoped, paginated; workflow ledger only |
| `GET /events` | Demo session | Tenant-scoped SSE, bounded replay |
| `GET /policy` | Demo session | Safe policy DTO |
| `GET /purchases/{id}/approval-link` | Admin | Never public |
| approve/reject/freeze/unfreeze/policy/reset/reconcile | Admin | Audited; CSRF/origin protection if cookie auth |

## 10. Database and deployment

SQLite WAL, FULL synchronous, foreign keys ON, busy timeout 5000 ms, one Uvicorn process, thread-local DB connections. Every migration is versioned, transactional, restart-safe, and tested from both clean install and populated v4.2 DB. Persistent disk/volume is mandatory. On lock timeout return 503 with Retry-After and no partial mutation.

## 11. AI safety and deterministic fallback

ShopMind tools:
- `search_catalog(query<=100 chars)` returns at most 10 catalog items; descriptions are untrusted and truncated.
- `build_cart(items)` accepts product IDs and quantities only; server fetches price/availability; quantity 1–10; line items 1–8.
- `submit_cart()` has zero arguments and submits only the latest server-held validated cart.
- Max six model calls, 45-second loop wall clock, repeated identical tool calls terminate, deterministic search fallback is bounded and still goes through PayPilot.
- No model-provided price, discount, merchant trust, budget, approval, or payment status is authoritative.

## 12. Observability, incident handling and recovery

Append-only `events` and `financial_facts`; structured redacted logs; minimal health endpoint; counters for active runs, reservations, unresolved captures, reconcile age, provider errors, DB lock errors, queue depth, and projection mismatch. Alert on unresolved captures, mismatched provider data, stale capture leases, projection drift, and unexpected late provider observations.

Operational runbook must cover: freeze, provider outage, unresolved capture, late settlement, DB restore, migration rollback policy, credential rotation, and public-demo reset. Never reset/delete financial history in production. Demo reset is disabled in production and may only recreate a disposable demo database under explicit admin control.

## 13. Release gates

No v4.2.2 release claim until:
1. migration from clean and populated v4.2 databases passes;
2. all mandatory tests in companion matrix pass;
3. no capture POST occurs after possible transmission;
4. budget projection matches append-only financial facts;
5. late capture resolution is idempotent and preserves terminal purchase state;
6. queue admission, dispatch recovery, deadline claim and per-agent limits pass concurrency tests;
7. tenant-scoped data access and DTO leakage tests pass;
8. PayPal sandbox verification evidence is recorded for every claimed live behavior;
9. a fresh clone can run the documented setup;
10. demo, README, guarantee matrix, and implementation report agree.

## 14. Explicit non-guarantees

- No distributed exactly-once payment guarantee.
- No recall/cancellation of an already-transmitted capture.
- No horizontal scaling across multiple app processes with SQLite.
- No guarantee of PayPal availability or stable sandbox behavior.
- No zero-click buyer payment in the sandbox.
- No automated refunds/chargebacks or merchant split settlement.
- No proof of production readiness from documentation alone.
