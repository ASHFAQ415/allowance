# ALLOWANCE v4.2 — FINAL HARDENED SYSTEM ARCHITECTURE

**Status:** APPROVED & FROZEN SPECIFICATION (v4.2)  
**Date:** November 6, 2026  
**Review Team:** Principal FinTech Architect, Senior Payment Systems Engineer, Distributed Systems & Concurrency Specialist, AI Agent Architecture Researcher, Application Security Engineer, Database Reliability Engineer, DevSecOps Engineer, Adversarial QA & Formal Verification Engineer, Hackathon Technical Strategy Advisor.  
**Baseline Inputs:** `allowance-implementation-plan-v4.md` (v4 baseline), `ALLOWANCE_V4_1_REVIEW_AND_PATCH.md` (v4.1 patch).

---

## 1. Executive Summary & Product Vision

### 1.1 Product Identity
- **Product Name:** Allowance
- **Tagline:** *"Give your AI agent an allowance, not your wallet."*
- **Core Mission:** An autonomous financial authorization platform allowing AI agents to discover, select, and assemble purchase carts while subjecting every transaction to an uncompromisable, deterministic financial policy engine and a verified payment execution protocol.
- **Architectural Separation:**
  1. **ShopMind (Agent Loop):** Explores catalogs, builds carts, Proposes purchases. Untrusted, sandboxed, completely isolated from funds, policies, and payment credentials.
  2. **PayPilot (Deterministic Policy Engine):** Pure functional authorization rules (R0–R6) evaluated across 4 transactional checkpoints (P1–P4).
  3. **Payment Execution Infrastructure:** PayPal Orders v2 integration governed by an exclusive-claim state machine, bounded non-blocking reconciliation, and append-only ledger accounting.

---

## 2. Core Subsystems & Module Boundaries

```
+----------------------------------------------------------------------------------------------------+
|                                         ALLOWANCE PLATFORM                                         |
+----------------------------------------------------------------------------------------------------+
|                                                                                                    |
|  +------------------------+      HTTP / SSE       +---------------------------------------------+  |
|  |   Frontend Dashboard   | <-------------------> |                 FastAPI API                 |  |
|  |  (React/Vite/Tailwind) |                       |  (Single uvicorn worker, DTO allowlists)    |  |
|  +------------------------+                       +---------------------------------------------+  |
|                                                                  |                                 |
|                     +--------------------------------------------+                                 |
|                     |                                            |                                 |
|                     v                                            v                                 |
|        +-------------------------+                  +-------------------------+                    |
|        |    Admission Control    |                  |  Orchestrator Pipeline  |                    |
|        | (Global/Agent Limits)   |                  | (State Machine Driver)  |                    |
|        +-------------------------+                  +-------------------------+                    |
|                     |                                            |                                 |
|                     |            +-------------------------------+                                 |
|                     v            v                               v                                 |
|        +-------------------------------+           +---------------------------+                   |
|        |     Worker Pool & Queue       |           |   PayPilot Policy Engine  |                   |
|        | (4 workers, queue cap 4, CAS) |           |  (Pure function, R0-R6,   |                   |
|        +-------------------------------+           |   Checkpoints P1-P4)      |                   |
|                     |                               +---------------------------+                   |
|                     v                                            |                                 |
|        +-------------------------------+                         |                                 |
|        |     ShopMind Agent Loop       |                         |                                 |
|        | (Claude API, bounded 6 steps, |                         |                                 |
|        |  catalog data sandbox, tools) |                         |                                 |
|        +-------------------------------+                         |                                 |
|                     |                                            |                                 |
|                     +--------------------+   +-------------------+                                 |
|                                          |   |                                                     |
|                                          v   v                                                     |
|                           +---------------------------------+                                      |
|                           |      SQLite Database (WAL)      |                                      |
|                           | (FULL sync, busy_timeout=5000,  |                                      |
|                           |  triggers, append-only ledger)  |                                      |
|                           +---------------------------------+                                      |
|                                          |                                                         |
|                                          v                                                         |
|                           +---------------------------------+                                      |
|                           |   PayPal Orders v2 Client       |                                      |
|                           | (Request-ID idempotency,        |                                      |
|                           |  verify_order, single-capture)  |                                      |
|                           +---------------------------------+                                      |
|                                                                                                    |
+----------------------------------------------------------------------------------------------------+
```

### 2.1 Enforced Architectural Boundaries
1. **`backend/app/agent/`**:
   - Strictly forbidden from importing `backend/app/orchestrator/`, `api/`, `paypal/`, `db/`, or `policy/`.
   - Only receives read-only catalog models and returns raw proposed cart items (`product_id`, `quantity`).
   - Import violations fail CI via a static AST analysis test (`test_import_boundaries.py`).
2. **`backend/app/policy/`**:
   - Pure functional library: Zero network I/O, zero database access, zero system time reads.
   - All evaluation parameters (`cart`, `policy`, `ledger_snapshot`, `now`, `checkpoint`) are explicitly injected.
3. **`backend/app/paypal/`**:
   - Encapsulates Orders v2 communications. Implements `PayPalClient` protocol with `LivePayPalClient` (httpx) and `MockPayPalClient`.
   - Never directly modifies database records; returns typed response envelopes to Orchestrator.
4. **`backend/app/orchestrator/`**:
   - Sole coordinator that bridges Agent proposals, Database transactions, PayPilot authorizations, and PayPal requests.

---

## 3. F1: Complete Budget-Reservation Admission Control

### 3.1 Problem Statement
In previous iterations, only `PENDING_APPROVAL` carts were counted in pending-cart limits, allowing concurrent runs or carts in later stages (`AUTO_APPROVED`, `BUYER_APPROVED`, `CAPTURING`) to bypass pool saturation limits and exhaust daily reserves unexpectedly.

### 3.2 Dual-Layer Control: Admission Control vs. Financial Policy
Allowance v4.2 strictly separates **Operational Admission Control** from **Financial Authorization Policy**:
- **Operational Admission Control**: Enforces concurrency caps, worker queue depth, and active reservation slots to prevent system starvation and demo exhaustion.
- **PayPilot Financial Policy**: Evaluates monetary spending against daily budgets, per-transaction thresholds, allowlists, and freeze rules.

### 3.3 Active Purchase Reservation States
A purchase is defined as **Holding Active Budget Reservation** if it is in any of the following 8 states:
1. `PENDING_APPROVAL` (Waiting for admin review; max 10 min expiry)
2. `AUTO_APPROVED` (Pre-approved by policy, awaiting order creation)
3. `APPROVED` (Approved by admin, awaiting order creation)
4. `PAYPAL_ORDER_CREATED` (PayPal order active, awaiting buyer approval; max 15 min expiry)
5. `BUYER_APPROVED` (Buyer approved at PayPal, awaiting capture claim; max 15 min expiry)
6. `CAPTURING` (Actively in-flight to PayPal capture endpoint; max 2 min expiry)
7. `CAPTURE_UNKNOWN` (Transport ambiguity; awaiting reconciliation; max 3 spaced attempts)
8. `FAILED(unresolved_capture)` (Reconciliation exhausted; money may have moved; budget held for remainder of `budget_day`)

**Reservation Release States:**
Budget reservation is released immediately when a purchase transitions to:
- `BLOCKED` (Denied at any checkpoint)
- `REJECTED` (Admin rejected)
- `EXPIRED` (Time rule lapsed)
- `FAILED` (Except where reason is `unresolved_capture`)
- `CAPTURED` (Transitions from *reservation* to *realized spend*; remains counted in `R3` daily spend)

### 3.4 Concurrency-Safe Admission Limits & Enforcement
Admission checks are performed atomically inside SQLite transactions using `BEGIN IMMEDIATE`:

```sql
-- Evaluated inside Orchestrator.submit_cart_tx before creating or advancing a purchase
SELECT 
  COUNT(CASE WHEN status IN ('PENDING_APPROVAL','AUTO_APPROVED','APPROVED','PAYPAL_ORDER_CREATED','BUYER_APPROVED','CAPTURING','CAPTURE_UNKNOWN') 
             OR (status = 'FAILED' AND status_reason = 'unresolved_capture') THEN 1 END) AS active_reservations,
  COUNT(CASE WHEN status = 'PENDING_APPROVAL' THEN 1 END) AS pending_approvals
FROM purchases
WHERE budget_day = :budget_day;
```

**Limits Configuration:**
- `MAX_ACTIVE_RESERVATIONS = 5`: Global cap on concurrent unfinalized purchases.
- `MAX_PENDING_APPROVALS = 3`: Cap on carts in human-review queue (`PENDING_APPROVAL`).
- `MAX_CONCURRENT_RUNS_PER_AGENT = 2`: Active worker thread limit per agent.
- `QUEUE_MAX_SIZE = 4`: Capacity of worker job queue.

If limits are exceeded:
- Admission rejection does **NOT** create a purchase row or hold funds.
- The agent run terminates with `FAILED(pending_cap)` or `FAILED(admission_rejected)`.

---

## 4. F2 & F5: Robust Capture Protocol & Network Error Classification

### 4.1 Strict Transport Uncertainty Principles
1. **Never interpret an ambiguous transport error as proof money did not move.**
2. **Never blindly retry a capture POST after request bytes have been transmitted.**
3. **Never retry solely because a timeout elapsed.**

### 4.2 Precise HTTPX Network Error Classification Matrix

| HTTPX Exception / Status | Transmission Status | Classification | Immediate State Action | Retry Permitted? |
|---|---|---|---|---|
| `httpx.ConnectTimeout` | Before bytes sent | Definitive Pre-Send Failure | No state change | **YES**: Exactly 1 immediate retry after 1s backoff |
| `httpx.ConnectError` | Before bytes sent | Definitive Pre-Send Failure | No state change | **YES**: Exactly 1 immediate retry after 1s backoff |
| `httpx.WriteTimeout` | During byte transmission | Potentially Transmitted | `CAPTURE_UNKNOWN` | **NO**: Blind retry prohibited |
| `httpx.WriteError` | During byte transmission | Potentially Transmitted | `CAPTURE_UNKNOWN` | **NO**: Blind retry prohibited |
| `httpx.ReadTimeout` | After bytes sent | Transmitted, Response Lost | `CAPTURE_UNKNOWN` | **NO**: Blind retry prohibited |
| `httpx.ReadError` | After bytes sent | Transmitted, Connection Reset | `CAPTURE_UNKNOWN` | **NO**: Blind retry prohibited |
| HTTP 429 (Too Many Requests) | Response Received | Provider Rate Limit | `CAPTURE_UNKNOWN` | **NO**: Do not hammer provider |
| HTTP 422 (`ORDER_ALREADY_CAPTURED`) | Response Received | Idempotent Success | Fetch GET Order -> `CAPTURED` | **NO** |
| HTTP 422 (`PAYER_ACTION_REQUIRED`) | Response Received | Excessive Amount / Consent Void | `FAILED(paypal_4xx)` | **NO**: Terminal rejection |
| HTTP 400 / 401 / 403 / 404 | Response Received | Provider Rejection | `FAILED(paypal_4xx)` | **NO**: Terminal rejection |
| HTTP 500 / 502 / 503 / 504 | Response Received | Provider Server Error | `CAPTURE_UNKNOWN` | **NO**: Blind retry prohibited |
| HTTP 200/201 (`status: COMPLETED`) | Response Received | Definitive Success | `CAPTURED` | N/A |
| HTTP 200/201 (`status: PENDING`) | Response Received | Asynchronous Settlement | `CAPTURE_UNKNOWN` | **NO**: Await reconciliation |
| HTTP 200/201 (`status: DECLINED`) | Response Received | Definitive Payment Decline | `FAILED(capture_declined)` | **NO**: Terminal rejection |

### 4.3 Capture Operation Protocol Step-by-Step
1. **Pre-Capture Verification (`verify_order`):**
   - Execute `GET /v2/checkout/orders/{id}`.
   - Assert `status == 'APPROVED'`.
   - Assert `purchase_units[0].amount.value == purchase.total_cents / 100`.
   - Assert `purchase_units[0].amount.currency_code == 'USD'`.
   - Assert `purchase_units[0].payee.merchant_id == EXPECTED_MERCHANT_ID` (or email).
   - Any mismatch transitions purchase directly to `FAILED(amount_mismatch)` with zero capture attempts.
2. **Atomic Capture Claim:**
   - Execute `BEGIN IMMEDIATE`.
   - Evaluate Policy Checkpoint P4 (Freeze check & budget re-verification).
   - If DENY, transition `BUYER_APPROVED -> BLOCKED`.
   - If ALLOW, CAS update:
     ```sql
     UPDATE purchases 
     SET status = 'CAPTURING', 
         capture_boot_id = :current_boot_id, 
         updated_at = :now 
     WHERE id = :id AND status = 'BUYER_APPROVED';
     ```
   - Commit transaction.
3. **Execute Capture:**
   - Transmit `POST /v2/checkout/orders/{id}/capture` with header `PayPal-Request-Id: {purchase.capture_request_id}` (persisted UUID4).
   - Network timeout configuration: `Connect: 3.0s`, `Read: 10.0s`, `Write: 5.0s`.
4. **Record Outcome:**
   - In a new transaction, CAS update purchase state based on the classification matrix.
   - If the purchase was already flipped to `CAPTURE_UNKNOWN` by a crash-recovery sweep, the worker’s successful `COMPLETED` response is safely recorded:
     ```sql
     UPDATE purchases 
     SET status = 'CAPTURED', 
         paypal_capture_id = :capture_id, 
         updated_at = :now 
     WHERE id = :id AND status IN ('CAPTURING', 'CAPTURE_UNKNOWN');
     ```

---

## 5. F3: Late Capture Success & Financial Ledger Resolution

### 5.1 The Scenario
1. Capture enters `CAPTURING`.
2. Connection drops or times out (`ReadTimeout`). State becomes `CAPTURE_UNKNOWN`.
3. 3 spaced reconciliation attempts fail (PayPal sandbox 500s or network outages).
4. Purchase transitions to terminal state `FAILED(status_reason='unresolved_capture')`.
5. Hours or days later, PayPal batch completes the capture, or an admin discovers funds were settled.

### 5.2 Architectural Decision: Append-Only Adjustment vs. Cyclic State
- **Rejected Option:** Reverting terminal state (`FAILED` -> `CAPTURED`).  
  *Why Rejected:* Violates state machine monotonicity (rank progression), breaks idempotency invariants, corrupts historical ledger snapshots, and invalidates previously emitted audit events.
- **Adopted Option:** **Append-Only Financial Adjustment Record (`ledger_adjustments`)**.  
  *Why Chosen:* Preserves terminal state immutability, provides zero-loss auditability, conforms to double-entry ledger standards, and explicitly models reconciliation discrepancies.

### 5.3 Schema & Adjustment Specification
```sql
CREATE TABLE ledger_adjustments (
    id TEXT PRIMARY KEY,                       -- UUID4
    purchase_id TEXT NOT NULL,                 -- Foreign key to purchases.id
    agent_id TEXT NOT NULL,
    adjustment_type TEXT NOT NULL,             -- 'LATE_CAPTURE_SETTLED', 'MANUAL_DISCREPANCY_REFUND'
    amount_cents INTEGER NOT NULL,             -- Settled amount in cents
    paypal_capture_id TEXT NOT NULL,           -- Actual PayPal capture reference
    discovered_at TEXT NOT NULL,               -- ISO-8601 UTC timestamp of discovery
    resolved_by TEXT NOT NULL,                 -- 'admin' or 'reconcile_daemon'
    notes TEXT NOT NULL,                       -- Human or system explanation
    created_at TEXT NOT NULL,
    FOREIGN KEY(purchase_id) REFERENCES purchases(id)
);

CREATE UNIQUE INDEX idx_adj_purchase_capture ON ledger_adjustments(purchase_id, paypal_capture_id);
```

**Resolution Invariants:**
1. `purchases.status` remains `FAILED(unresolved_capture)`.
2. An audit event `FINANCIAL_DISCREPANCY_RESOLVED` is appended to `events` table.
3. A ledger entry is written with `from_state = 'FAILED'`, `to_state = 'RESOLVED_CAPTURED'`, `actor = 'admin'`.
4. The unique index `idx_adj_purchase_capture` guarantees an adjustment for a specific PayPal capture ID cannot be duplicated.

---

## 6. F4: Queue Lifecycle, Worker Ownership & Concurrency

### 6.1 Worker Pool & Queue Architecture
- **Worker Concurrency:** Fixed pool of 4 active worker threads (`ThreadPoolExecutor(max_workers=4)`).
- **Queue Bounds:** Capacity capped at 4 (`queue.Queue(maxsize=4)`).
- **Queue Saturation:** When queue is full (4 items queued, 4 running), incoming `POST /agent/run` requests immediately return `503 Service Unavailable` with header `Retry-After: 5`. No database record is created.

### 6.2 Job State Lifecycle & Explicit Ownership
```
[POST /agent/run]
       |
       v (CAS insert)
   +--------+   (older than 20s)   +-------------------------+
   | QUEUED | -------------------> | FAILED(queue_timeout)   | (Terminal)
   +--------+                      +-------------------------+
       |
       v (Worker atomic claim)
   +---------+  (older than 60s)   +-------------------------+
   | RUNNING | -------------------> | FAILED(interrupted)     | (Terminal)
   +---------+                      +-------------------------+
       |
       +---> [Agent loop completes -> Submit cart -> Terminal]
```

### 6.3 Worker Claim & Stale Execution Protection
When a worker picks a job from the queue:
```sql
-- Worker atomic claim
UPDATE agent_runs 
SET status = 'RUNNING', 
    started_at = :now, 
    worker_id = :worker_id 
WHERE id = :run_id AND status = 'QUEUED';
```
If 0 rows are updated, the job was already claimed or cancelled; worker aborts.

**Queue Wait Timeout (`QUEUE_WAIT_TIMEOUT = 20s`):**
Before beginning execution, the worker checks:
```python
if (now - run.queued_at) > 20.0:
    cas_transition_run(run_id, from_state='RUNNING', to_state='FAILED', reason='queue_timeout')
    return
```

**Stale Result Prevention:**
All transitions from `RUNNING` verify that `agent_runs.status == 'RUNNING'`. If a sweep marked the run `FAILED(interrupted)`, the worker’s late submission is discarded without creating purchases.

### 6.4 Graceful Shutdown & Crash Recovery
- **Shutdown (SIGTERM):**
  1. Global flag `shutting_down = True` is activated.
  2. Health checks report degraded; `/agent/run` returns 503.
  3. Worker pool is granted 30 seconds to complete active loops (`pool.shutdown(wait=True, timeout=30)`).
- **Startup Recovery (Before accepting HTTP traffic):**
  1. Generate unique runtime instance identifier: `boot_id = uuid4()`. Store in `meta` table.
  2. Mark all lingering `agent_runs` in state `QUEUED` or `RUNNING` as `FAILED(interrupted)`.
  3. Scan `purchases`: Any purchase in `CAPTURING` with a `capture_boot_id != current_boot_id` is updated to `CAPTURE_UNKNOWN(interrupted)` with `next_reconcile_at = now`.

---

## 7. Purchase State Machine

### 7.1 State Machine Transition Matrix

| Rank | State | Permitted Next States | Entry Condition | Budget Impact | Expiry / Timeout |
|---|---|---|---|---|---|
| **0** | `CREATED` | `CART_READY`, `FAILED` | Purchase initialized | None | None |
| **1** | `CART_READY` | `AUTO_APPROVED`, `PENDING_APPROVAL`, `BLOCKED`, `FAILED` | Cart built & catalog-priced | None | None |
| **2** | `PENDING_APPROVAL` | `APPROVED`, `REJECTED`, `EXPIRED`, `BLOCKED` | P1 returns `REVIEW` | Reserved | 10 minutes |
| **3** | `AUTO_APPROVED` | `PAYPAL_ORDER_CREATED`, `BLOCKED`, `FAILED` | P1 returns `ALLOW` | Reserved | None (advances immediately) |
| **3** | `APPROVED` | `PAYPAL_ORDER_CREATED`, `BLOCKED`, `FAILED` | Admin manual approval | Reserved | None (advances immediately) |
| **4** | `PAYPAL_ORDER_CREATED` | `BUYER_APPROVED`, `EXPIRED`, `BLOCKED`, `FAILED` | PayPal Order created via v2 | Reserved | 15 minutes |
| **5** | `BUYER_APPROVED` | `CAPTURING`, `BLOCKED`, `EXPIRED`, `FAILED` | Buyer approved at PayPal | Reserved | 15 minutes |
| **6** | `CAPTURING` | `CAPTURED`, `CAPTURE_UNKNOWN`, `FAILED` | Exclusive claim by worker | Reserved | 2 minutes -> `CAPTURE_UNKNOWN` |
| **7** | `CAPTURE_UNKNOWN` | `CAPTURED`, `FAILED` | Network timeout / 5xx / PENDING | Reserved | 3 spaced reconcile attempts |
| **9** | `CAPTURED` | *None (Terminal)* | PayPal capture completed | Realized Spend | None |
| **9** | `BLOCKED` | *None (Terminal)* | Policy DENY at P1, P2, P3, or P4 | Released | None |
| **9** | `REJECTED` | *None (Terminal)* | Admin rejected purchase | Released | None |
| **9** | `EXPIRED` | *None (Terminal)* | Time limit exceeded | Released | None |
| **9** | `FAILED` | *None (Terminal)* | Unrecoverable error / declination | Released (except `unresolved_capture` holds until end of `budget_day`) | None |

### 7.2 Strict Invariants
1. **Strict Monotonicity:** State rank must strictly increase (`to_rank > from_rank`). Backward transitions raise `IllegalTransitionError`.
2. **Terminal Immutability:** Once rank 9 is reached, no update or transition can alter `status` or `total_cents`.
3. **Database Level Enforcement:**
   - Unique transition index: `UNIQUE(purchase_id, to_state)` on `ledger`.
   - SQLite triggers `prevent_ledger_update` and `prevent_ledger_delete` raise `ABORT`.

---

## 8. PayPilot Deterministic Policy Engine

### 8.1 Pure Functional Specification
`evaluate(cart, policy, ledger_snapshot, now, checkpoint, exclude_purchase_id=None) -> Decision`
- Zero I/O; deterministic output for given inputs.
- Evaluates rules in strict priority order R0 -> R6.

### 8.2 Rule Precedence Table

| Priority | Rule Name | Condition Checked | Output if Triggered |
|---|---|---|---|
| **R0** | **Emergency Freeze** | `policy.frozen == True` | `DENY` ("Kill switch active") |
| **R1** | **Merchant Allowlist** | Any item `merchant_id` not in `policy.allowlist` | `DENY` ("Unapproved merchant") |
| **R2** | **Per-Transaction Limit** | `cart.total_cents > policy.max_per_tx_cents` | `DENY` ("Exceeds per-tx limit") |
| **R3** | **Daily Budget Limit** | `spent_today_cents + cart.total_cents > policy.daily_budget_cents` | `DENY` ("Exceeds daily budget") |
| **R4** | **Approval Threshold** | `cart.total_cents > policy.approval_threshold_cents` | `REVIEW` ("Requires human approval") |
| **R5** | **Velocity Limit** | `auto_approved_today_cents + cart.total_cents > policy.velocity_cap_cents` | `REVIEW` ("Velocity cap exceeded") |
| **R6** | **Default Allow** | None of R0–R5 triggered | `ALLOW` ("Auto-approved") |

### 8.3 Checkpoints Definition

| Checkpoint | Execution Point | Active Rules Evaluated | Deny Action | Review Action |
|---|---|---|---|---|
| **P1** | Cart submission | R0, R1, R2, R3, R4, R5, R6 | `BLOCKED` | `PENDING_APPROVAL` |
| **P2** | Admin approval click | R0, R1, R2, R3 | `BLOCKED` | Ignored (Human already approved) |
| **P3** | Pre-PayPal Order create | R0, R1, R2, R3 | `BLOCKED` | Ignored |
| **P4** | Capture Claim (post-buyer approval) | R0, R1, R2, R3 | `BLOCKED` | Ignored |

*Note on P2–P4:* The purchase excludes its own active reservation from `spent_today_cents` via `exclude_purchase_id`.

### 8.4 Budget Day & Timezone Immutability
- `budget_day` is calculated once upon purchase creation: `datetime.now(ZoneInfo(policy.timezone)).strftime("%Y-%m-%d")`.
- `policy.timezone` is **immutable** via PUT. Any attempt to alter timezone returns HTTP 422. Timezone can only be changed via complete system reset.
- P2–P4 checks evaluate spend against the purchase’s stored `budget_day`, preventing midnight rollover exploits.

---

## 9. AI Agent Security & Sandboxing (ShopMind)

### 9.1 The Separation Principle
The Claude AI model has **zero access** to money, wallets, credit cards, payment APIs, database records, or policy configurations.

### 9.2 Tool Contract & Bounds
- `search_catalog(query: str)`:
  - `query` bounded to 100 characters.
  - Returns max 10 products. Descriptions truncated to 300 characters.
  - Formatted inside `<catalog_data>` XML delimiter. Prompt explicitly treats data inside tags as untrusted data, not instructions.
- `build_cart(items: list[CartItemInput])`:
  - Validates `product_id` against regex `^[a-z0-9_-]{1,40}$`.
  - Line items: min 1, max 8. Quantity: min 1, max 10.
  - Server fetches real catalog prices. Model-provided prices are ignored.
- `submit_cart()`:
  - **Takes zero arguments.** Submits the latest validated cart held in server loop state. Eliminates model payload tampering during submission.

### 9.3 Bounded Loop Execution Limits
- `MAX_STEPS = 6` model calls per run.
- `RUN_TIMEOUT = 45.0s` total wall-clock execution.
- Repeat call detector: Identical tool name and normalized arguments twice in a row terminates run immediately with `FAILED(repeat_call)`.
- Global usage cap: Daily Claude calls tracked in `usage` table (default 300 calls). When exhausted, requests gracefully fallback to deterministic search.

---

## 10. Database Reliability & Concurrency Architecture

### 10.1 SQLite Configuration & Connection Factory
- **Engine:** SQLite 3 in WAL (Write-Ahead Logging) mode.
- **Synchronous Flag:** `synchronous = FULL` (Essential for financial durability).
- **Busy Timeout:** `busy_timeout = 5000` (5.0 seconds).
- **Isolation Setting:** `isolation_level = None` (Disables Python sqlite3 auto-transactions; allows explicit `BEGIN IMMEDIATE`).
- **Connection Model:** Dedicated connection per thread via thread-local factory.

```python
def create_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = FULL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn
```

### 10.2 Database Lock Contention Handling
If SQLite reports `sqlite3.OperationalError: database is locked` after 5 seconds:
- The API intercepts the error.
- Returns `HTTP 503 Service Unavailable` with `Retry-After: 1`.
- Zero database state is mutated; financial invariants remain intact.

---

## 11. Security Architecture & Deployment Hardening

### 11.1 Authentication & Threat Model
- **Single Admin Token:** Token-based authentication via `X-Admin-Token` header.
- **Timing Attack Mitigation:** Constant-time comparison using `hmac.compare_digest(provided_bytes, expected_bytes)`.
- **Brute Force Protection:** IP-based failure tracker blocks IPs exceeding 5 failed token attempts per minute (HTTP 429).
- **Production Guard:** If `APP_ENV=production` and `ADMIN_TOKEN` is missing or `< 32` characters, application refuses to start.

### 11.2 Public vs. Private DTO Allowlist

```
Public Endpoints (GET /purchases, GET /ledger, SSE /events)
  ALLOWED:
    - id (UUID4)
    - status
    - status_reason
    - rule_name
    - total_cents
    - merchant_names
    - created_at
    - expires_at
  STRIPPED / FORBIDDEN:
    - paypal_order_id
    - paypal_capture_id
    - approval_url
    - create_request_id
    - capture_request_id
    - trace_json

Admin Endpoints (GET /purchases/{id}/approval-link, etc.)
  - Full financial and debug payload visible only with verified X-Admin-Token.
```

---

## 12. Lightweight Observability Specification

Allowance rejects heavy monitoring infrastructure (Prometheus/Grafana/ELK) in favor of lightweight, embedded operational metrics:
1. **Audit Event Log (`events` table):**
   - Every state transition, reconcile claim, alert, and agent action appends a typed JSON payload.
   - Replayable via `GET /events` with `Last-Event-ID`.
2. **Structured Log Output:**
   - Standard stdout formatted in structured JSON:
     `{"timestamp": "...", "level": "INFO", "component": "PayPilot", "event": "POLICY_EVAL", "purchase_id": "...", "decision": "ALLOW", "latency_ms": 1.2}`
3. **Health & Telemetry Endpoint (`GET /health`):**
   - Reports: DB connection latency, WAL file size, active reservations count, worker pool utilization, and PayPal sandbox ping status.

