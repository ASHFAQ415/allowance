# Allowance v4.2.2: 10/10 Master Adversarial Implementation Plan

## Goal Description
Consolidate, reconcile, and execute the complete engineering specification for **Allowance**—the autonomous financial authorization platform that gives AI shopping agents an allowance, not a wallet.

Through rigorous cross-analysis of all project source documents:
- `allowance-implementation-plan-v4.md` (v4 baseline)
- `ALLOWANCE_V4_1_REVIEW_AND_PATCH.md` (v4.1 patch)
- `ALLOWANCE_V4_2_FINAL_ARCHITECTURE.md` & companion documents (v4.2)
- `ALLOWANCE_V4_2_1_CONSISTENCY_FINANCIAL_SAFETY_PATCH.md` (v4.2.1 financial safety patch)
- `00` through `06_ALLOWANCE_V4_2_2_*.md` (v4.2.2 consolidated release specification)

This plan eliminates every remaining architectural contradiction, closes every payment uncertainty loophole, and establishes a concrete, step-by-step implementation blueprint ready for code execution.

---

## User Review Required

> [!IMPORTANT]
> **Definitive Architectural Invariants in v4.2.2:**
> 1. **Zero Capture Retries After Possible Transmission:**  
>    The dangerous instruction from v4.2 (*"retry capture if order is APPROVED on attempt 2+"*) is **strictly prohibited**. A PayPal `GET order` showing `APPROVED` is not proof that a prior capture request was not accepted or is not still processing. Only a proven pre-send transport error (before any bytes leave the socket) may retry once. Any other transport error, 429, 5xx, or timeout enters `CAPTURE_UNKNOWN` and holds the budget reservation.
> 2. **Separation of Workflow State from Financial Accounting:**  
>    `purchases.status` represents workflow progression only. `financial_facts` is the append-only source of truth. `budget_accounting` is a rebuildable projection. A purchase marked `FAILED(unresolved_capture)` **never changes state**; if capture is later proven, a `LATE_CAPTURE_SETTLED` fact is inserted into `ledger_adjustments`. Fake transitions (`RESOLVED_CAPTURED` in `ledger`) are eliminated.
> 3. **Unambiguous Daily Budget Accounting ($R3$ & $R5$):**  
>    $\text{Committed Daily Spend } (R3) = \text{captured\_cents} + \text{reserved\_cents} + \text{unresolved\_cents}$.  
>    Unresolved captures stay committed until authoritative provider proof of non-settlement is established. $R5$ (auto-approved velocity) is a strict subset of $R3$, never double-counted.
> 4. **Atomic Queue Admission & Deadline Claim:**  
>    Admission control checks active run limits under `BEGIN IMMEDIATE` *before* inserting `agent_runs`. A 9th run receives `HTTP 503` with zero database writes. The queue deadline (`queued_at >= :deadline`) is enforced inside the atomic SQL `QUEUED -> RUNNING` claim, preventing stale runs from invoking LLMs.
> 5. **Demo Tenant Scoping:**  
>    All dashboard reads, agent runs, and SSE connections require an authenticated `DEMO_SESSION_TOKEN` scoped to `DEMO_TENANT_ID`. Admin mutations require `X-Admin-Token`. Raw provider IDs and approval links are excluded from public DTOs.

---

## Architectural Decomposition & Data Flow

```
+---------------------------------------------------------------------------------------------------+
|                                      ALLOWANCE v4.2.2 PLATFORM                                    |
+---------------------------------------------------------------------------------------------------+
|                                                                                                   |
|  [ Authenticated Client / Dashboard ]                                                            |
|          |  (X-Demo-Session: DEMO_SESSION_TOKEN, X-Admin-Token)                                   |
|          v                                                                                        |
|  [ FastAPI API Layer ]                                                                            |
|    ├── Tenant Scoping (WHERE tenant_id = :tenant_id)                                              |
|    ├── Strict DTO Serialization Allowlists                                                        |
|    └── Rate Limiting (IP & Session Sliding Windows)                                               |
|          |                                                                                        |
|          v                                                                                        |
|  [ Admission Controller ]                                                                         |
|    ├── Check: Active runs (QUEUED + RUNNING) < 8; Per-agent active runs <= 2                      |
|    └── Check: Active budget reservations < 5                                                      |
|          |                                                                                        |
|          v (Atomic BEGIN IMMEDIATE)                                                               |
|  [ SQLite WAL Database ]                                                                          |
|    ├── agent_runs (Authoritative state)                                                           |
|    ├── purchases (Workflow lifecycle: 0:CREATED -> 9:CAPTURED / FAILED)                           |
|    ├── capture_operations (Durable 1:1 operation, UUID4 request ID, lease)                        |
|    ├── financial_facts (Append-only journal of financial movements)                               |
|    ├── budget_accounting (Rebuildable transactional projection)                                   |
|    ├── ledger (Valid workflow transitions only)                                                   |
|    ├── ledger_adjustments (Late settlements / verified non-settlement)                            |
|    └── events (Operational audit & SSE stream)                                                    |
|          |                                                                                        |
|          +─────────────────────────────+─────────────────────────────+                            |
|          |                             |                             |                            |
|          v                             v                             v                            |
|  [ Worker Pool & Dispatcher ]   [ PayPilot Policy Engine ]   [ PayPal Orders v2 Client ]           |
|    ├── 4 Worker Threads           ├── Pure Function (R0-R6)    ├── Typed Transport Envelope       |
|    ├── 4 Waiting Queue Slots      ├── Checkpoints P1-P4        ├── Stable UUID4 Request-Id        |
|    ├── Atomic Deadline Claim      ├── Single Self-Exclusion    ├── Pre-capture verify_order       |
|    └── Dispatcher Recovery        └── Integer Cents / Currency └── Read-Only Reconciliation      |
|                                                                                                   |
+---------------------------------------------------------------------------------------------------+
```

---

## Database Schema & Integrity Invariants

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

-- 1. Metadata & Runtime Boot
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 2. Tenant & Agents
CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 3. Policy & Immutable History
CREATE TABLE policy (
    agent_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    daily_budget_cents INTEGER NOT NULL,
    max_per_tx_cents INTEGER NOT NULL,
    approval_threshold_cents INTEGER NOT NULL,
    velocity_cap_cents INTEGER NOT NULL,
    allowlist_json TEXT NOT NULL,
    timezone TEXT NOT NULL, -- IMMUTABLE via API
    frozen INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(agent_id) REFERENCES agents(id)
);

CREATE TABLE policy_history (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    policy_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(agent_id) REFERENCES agents(id)
);

-- 4. Authoritative Agent Runs & Queue
CREATE TABLE agent_runs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    request_hash TEXT NOT NULL,
    user_prompt TEXT,            -- Stored for audit trail; excluded from public DTOs
    worker_id TEXT,
    queued_at REAL NOT NULL,
    started_at REAL,
    status TEXT NOT NULL, -- QUEUED, RUNNING, COMPLETED, FAILED
    status_reason TEXT,
    trace_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(agent_id) REFERENCES agents(id)
);
CREATE INDEX idx_runs_tenant_status ON agent_runs(tenant_id, status);

-- 5. Purchase Workflow Lifecycle
CREATE TABLE purchases (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL, -- Forward-only state machine
    status_reason TEXT,
    auto_approved INTEGER NOT NULL,
    cart_json TEXT NOT NULL,
    cart_hash TEXT NOT NULL,
    total_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    merchant_ids_json TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    create_request_id TEXT NOT NULL UNIQUE,
    capture_boot_id TEXT,        -- Idempotency fence for capture dispatch on worker restart
    paypal_order_id TEXT,
    approval_url TEXT,
    state_entered_at REAL NOT NULL,
    expires_at REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(agent_id) REFERENCES agents(id),
    FOREIGN KEY(run_id) REFERENCES agent_runs(id)
);
CREATE INDEX idx_purchases_budget_day ON purchases(tenant_id, budget_day, status);

-- 6. Durable Capture Operations (1:1 with Purchase)
CREATE TABLE capture_operations (
    id TEXT PRIMARY KEY,
    purchase_id TEXT NOT NULL UNIQUE,
    capture_request_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL, -- PREPARED, MAY_HAVE_BEEN_SENT, DEFINITIVE_SUCCESS, DEFINITIVE_FAILURE, UNKNOWN, RESOLVED_SUCCESS, RESOLVED_FAILURE
    first_send_started_at REAL,
    last_observed_at REAL,
    paypal_order_status TEXT,
    paypal_capture_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    owner_token TEXT,
    owner_lease_until REAL,
    last_error_class TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(purchase_id) REFERENCES purchases(id)
);

-- 7. Append-Only Financial Facts (Source of Truth)
CREATE TABLE financial_facts (
    id TEXT PRIMARY KEY,
    source_event_key TEXT NOT NULL UNIQUE, -- Idempotency key per financial movement
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    purchase_id TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    currency TEXT NOT NULL,
    fact_type TEXT NOT NULL, -- RESERVATION_CREATED, RESERVATION_RELEASED, CAPTURE_BECAME_UNKNOWN, CAPTURE_SETTLED, UNRESOLVED_CONFIRMED_NOT_SETTLED, LATE_CAPTURE_SETTLED
    delta_reserved_cents INTEGER NOT NULL,
    delta_unresolved_cents INTEGER NOT NULL,
    delta_captured_cents INTEGER NOT NULL,
    delta_auto_approved_cents INTEGER NOT NULL,
    paypal_capture_id TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(purchase_id) REFERENCES purchases(id)
);
CREATE INDEX idx_financial_facts_budget ON financial_facts(agent_id, budget_day, currency);

-- 8. Rebuildable Budget Accounting Projection
CREATE TABLE budget_accounting (
    agent_id TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    currency TEXT NOT NULL,
    captured_cents INTEGER NOT NULL DEFAULT 0,
    reserved_cents INTEGER NOT NULL DEFAULT 0,
    unresolved_cents INTEGER NOT NULL DEFAULT 0,
    auto_approved_committed_cents INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(agent_id, budget_day, currency)
);

-- 9. Purchase Workflow Ledger (State Transitions Only)
CREATE TABLE ledger (
    id TEXT PRIMARY KEY,
    purchase_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    checkpoint TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    policy_version INTEGER NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(purchase_id) REFERENCES purchases(id),
    UNIQUE(purchase_id, to_state) -- Guarantees rank acyclicity
);

-- 10. Late Adjustments & Post-Terminal Financial Facts
CREATE TABLE ledger_adjustments (
    id TEXT PRIMARY KEY,
    purchase_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    adjustment_type TEXT NOT NULL, -- LATE_CAPTURE_SETTLED, LATE_CAPTURE_CONFIRMED_NOT_SETTLED
    amount_cents INTEGER NOT NULL,
    paypal_capture_id TEXT,
    discovered_at TEXT NOT NULL,
    resolved_by TEXT NOT NULL,
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(purchase_id) REFERENCES purchases(id),
    UNIQUE(purchase_id, paypal_capture_id)
);

-- 11. Operational & SSE Audit Events
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    purchase_id TEXT,
    run_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 12. Immutability Triggers
CREATE TRIGGER IF NOT EXISTS abort_ledger_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT, 'Ledger entries are strictly immutable'); END;
CREATE TRIGGER IF NOT EXISTS abort_ledger_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT, 'Ledger entries cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS abort_financial_facts_update BEFORE UPDATE ON financial_facts BEGIN SELECT RAISE(ABORT, 'Financial facts are strictly immutable'); END;
CREATE TRIGGER IF NOT EXISTS abort_financial_facts_delete BEFORE DELETE ON financial_facts BEGIN SELECT RAISE(ABORT, 'Financial facts cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS abort_policy_history_update BEFORE UPDATE ON policy_history BEGIN SELECT RAISE(ABORT, 'Policy history is strictly immutable'); END;
CREATE TRIGGER IF NOT EXISTS abort_policy_history_delete BEFORE DELETE ON policy_history BEGIN SELECT RAISE(ABORT, 'Policy history cannot be deleted'); END;
```

---

## Proposed Changes: Step-by-Step Implementation Blueprint

### Phase 1: Repository Structure, Database Engine & Triggers
#### [NEW] `backend/app/db/connection.py`
- Thread-local SQLite connection factory with `isolation_level=None`, `synchronous=FULL`, `busy_timeout=5000`, `foreign_keys=ON`.
- Context manager `transactional_session()` executing explicit `BEGIN IMMEDIATE`.
#### [NEW] `backend/app/db/schema.sql`
- Full v4.2.2 schema definition, indices, and immutability triggers.
#### [NEW] `backend/app/db/financial_journal.py`
- Append-only `financial_facts` insertion with deterministic delta calculation.
- `budget_accounting` projection updates and `rebuild_budget_accounting()` function.
#### [NEW] `backend/tests/unit/test_triggers.py`
- Verifies that any direct `UPDATE` or `DELETE` on `ledger`, `financial_facts`, or `policy_history` raises `sqlite3.IntegrityError` / `ABORT`.

---

### Phase 2: State Machine & Pure Policy Engine
#### [NEW] `backend/app/orchestrator/state_machine.py`
- Monotonic state progression: `CREATED (0) -> CART_READY (1) -> PENDING_APPROVAL (2) / AUTO_APPROVED (3) -> APPROVED (3) -> PAYPAL_ORDER_CREATED (4) -> BUYER_APPROVED (5) -> CAPTURING (6) -> CAPTURE_UNKNOWN (7) -> CAPTURED (9) / BLOCKED (9) / REJECTED (9) / EXPIRED (9) / FAILED (9)`.
- Illegal transition detection, time rules expiration sweep.
#### [NEW] `backend/app/policy/rules.py`
- Pure function `evaluate(cart, policy, budget_snapshot, now, checkpoint) -> Decision`.
- Strict evaluation order: R0 (Freeze) -> R1 (Allowlist) -> R2 (Per-Tx) -> R3 (Daily Budget) -> R4 (Review Threshold) -> R5 (Velocity Cap) -> R6 (Auto-Approve).
- Checkpoints P1–P4 with single self-exclusion of current purchase reservation.
#### [NEW] `backend/tests/unit/test_policy.py` & `backend/tests/property/test_financial_properties.py`
- 100% branch coverage on R0–R6.
- Hypothesis tests proving budget monotonicity, auto-approved subset properties, and acyclicity.

---

### Phase 3: Sandboxed ShopMind Agent & Bounded Loop
#### [NEW] `backend/app/agent/catalog.py`
- 20 seeded products across 4 merchants (3 approved, 1 unapproved).
- Server-side pricing; untrusted catalog descriptions wrapped in `<catalog_data>` tags.
#### [NEW] `backend/app/agent/tools.py`
- `search_catalog(query<=100)`: max 10 items, descriptions truncated to 300 chars.
- `build_cart(items)`: validates `product_id`, quantities 1–10, lines 1–8; overrides prices with catalog price.
- `submit_cart()`: takes **zero arguments**; submits only latest validated cart.
#### [NEW] `backend/app/agent/loop.py`
- Bounded runner: max 6 model calls, 45s wall clock, repeat call detector (`FAILED(repeat_call)`).
- Deterministic keyword search fallback on API errors, rate limits, or usage caps.
#### [NEW] `backend/tests/unit/test_agent_isolation.py` & `backend/tests/loop_guard/test_loop_guards.py`
- AST import boundary verification (agent never imports db, orchestrator, or paypal).
- Step cap, timeout, repeat call, and injection resilience tests.

---

### Phase 4: Bounded Queue, Dispatcher & Admission Control
#### [NEW] `backend/app/orchestrator/admission.py`
- Pre-admission check in `BEGIN IMMEDIATE`: asserts active runs < 8, active runs per agent <= 2, active reservations < 5.
- 9th run receives HTTP 503 `Retry-After: 5` with zero rows inserted.
#### [NEW] `backend/app/orchestrator/queue.py`
- 4 worker threads, 4 waiting slots.
- Atomic dequeue claim: `UPDATE agent_runs SET status = 'RUNNING', started_at = :now, worker_id = :worker_id WHERE id = :id AND status = 'QUEUED' AND queued_at >= :queue_deadline`.
- If deadline elapsed, atomic transition to `FAILED(queue_timeout)`.
- Dispatcher recovery on startup: re-enqueues eligible `QUEUED` runs; transitions lingering `RUNNING` runs to `FAILED(interrupted)`.
#### [NEW] `backend/tests/concurrency/test_queue_lifecycle.py`
- Concurrency tests for queue saturation, deadline boundaries, and crash gap recovery.

---

### Phase 5: PayPal Orders v2 Client, Safe Capture & Reconcile
#### [NEW] `backend/app/paypal/client.py`
- Protocol interface defining `create_order`, `get_order`, `capture_order`.
- `LivePayPalClient` (HTTPX) and `MockPayPalClient`.
- Typed transport envelope exposing `bytes_may_have_been_sent` boolean.
- Pre-capture assertion `verify_order()` checking status `APPROVED`, exact amount, currency, and payee.
- Stable persistent UUID4 `create_request_id` and `capture_request_id`.
#### [NEW] `backend/app/orchestrator/capture_lifecycle.py`
- Pre-send marker `MAY_HAVE_BEEN_SENT` persisted before socket transmission.
- At most one retry allowed ONLY when transport proves no bytes transmitted and state is `PREPARED`.
- Any ambiguity, write error, read timeout, 429, or 5xx -> `CAPTURE_UNKNOWN`; zero blind retries.
- Read-only reconciliation via `GET /v2/checkout/orders/{id}` with owner-token CAS.
- Late capture settlement recorded in `ledger_adjustments` and `financial_facts`; purchase remains `FAILED(unresolved_capture)`.
#### [NEW] `backend/tests/contract/test_paypal_contract.py` & `backend/tests/integration/test_capture_safety.py`
- Validates all outcomes: timeout, 429, 422, 500, PENDING, DECLINED, COMPLETED, and late settlement.

---

### Phase 6: FastAPI Application Layer, Tenant Scoping & DTO Allowlists
#### [NEW] `backend/app/api/dto.py`
- Strict Pydantic response models. Public DTOs explicitly exclude `paypal_order_id`, `paypal_capture_id`, `approval_url`, `request_id`, and `trace_json`.
#### [NEW] `backend/app/api/auth.py`
- `DEMO_SESSION_TOKEN` validation for data reads, runs, and SSE; tenant scoping injection.
- `X-Admin-Token` constant-time comparison via `hmac.compare_digest` for administrative actions.
#### [NEW] `backend/app/api/routes.py`
- REST endpoints (`/health`, `/agent/run`, `/purchases`, `/ledger`, `/events`, `/approve`, `/freeze`, etc.).
#### [NEW] `backend/tests/e2e_api/test_api_security.py`
- Verifies tenant isolation, DTO leakage prevention, and brute force rate limiting.

---

### Phase 7: React Dashboard, Live SSE & E2E Testing
#### [NEW] `frontend/src/App.tsx`, `components/`
- Production-grade UI built with React 18, Vite, and Tailwind CSS.
- Chat interface, agent thought timeline (SSE with `Last-Event-ID`), human review approval queue, double-entry financial ledger stream, and emergency Freeze toggle.
#### [NEW] `e2e/test_dashboard.spec.ts`
- Playwright end-to-end tests covering auto-approve flow, human approval flow, blocked merchant flow, and emergency freeze flow against `PAYPAL_MODE=mock`.

---

## Complete 53 Adversarial Test Matrix

The test suite executes all 35 original scenarios plus the 18 new v4.2.2 mandatory scenarios:

```
Original Scenarios (1 to 35):
  #1 Concurrent budget race (asserts financial_facts and budget_accounting)
  #2 Concurrent captures on same order (exclusive CAPTURING CAS claim)
  #3 Delayed capture response (MAY_HAVE_BEEN_SENT -> CAPTURE_UNKNOWN)
  #4 Capture timeout & auto-reconcile (GET-only reconciliation -> CAPTURED)
  #5 Worker crash during capture (startup sweep recovers to UNKNOWN)
  #6 Late capture success audit (adjustment record; no fake ledger transition)
  #7 Concurrent reconciliation race (owner token CAS; single GET)
  #8 Duplicate idempotency key mismatch (422 Unprocessable Entity)
  #9 PayPal returns 429 Rate Limit (CAPTURE_UNKNOWN; no immediate retry)
  #10 PayPal returns 422 decline (definitive terminal failure)
  #11 PayPal returns 500 Server Error (CAPTURE_UNKNOWN; hold budget)
  #12 Unknown PayPal status in GET (remains UNKNOWN; alert emitted)
  #13 Invalid payment amount tampering (verify_order blocks capture)
  #14 Invalid payee account tampering (verify_order blocks capture)
  #15 Invalid currency tampering (verify_order blocks capture)
  #16 Freeze during buyer approval (Checkpoint P4 blocks claim)
  #17 Freeze racing capture claim (atomic ordering determines winner)
  #18 Policy change during pending (Checkpoint P2 blocks approve)
  #19 Budget exhaustion threshold (Hypothesis sequence test)
  #20 Active reservation flood (admission rejected before purchase insert)
  #21 Worker queue exhaustion (9th run rejected 503 before DB insert)
  #22 Queue timeout and dequeue race (atomic QUEUED->RUNNING claim predicate)
  #23 Application restart recovery (durable QUEUED runs re-dispatched)
  #24 SIGTERM graceful draining (30s timeout drains active work)
  #25 SIGKILL mid-transaction (SQLite WAL durability guarantees consistency)
  #26 SQLite lock exhaustion (503 Retry-After; zero state mutation)
  #27 Duplicate ledger transition (UNIQUE constraint raises IntegrityError)
  #28 Late worker response vs timeout (stale worker cannot mutate terminal run)
  #29 Prompt injection via user input (PayPilot Checkpoint P1 blocks)
  #30 Malicious catalog content injection (server prices override model)
  #31 Tool argument manipulation (Pydantic schema validation rejects)
  #32 Admin token timing attack (constant-time compare; rate-limited)
  #33 SSE data leakage prevention (DTO allowlists strip sensitive IDs)
  #34 Public endpoint abuse flood (IP and session sliding window rate limits)
  #35 Claude API outage simulation (deterministic keyword search fallback)

New v4.2.2 Mandatory Scenarios (V22-01 to V22-18):
  V22-01: APPROVED after ambiguous capture -> Remain CAPTURE_UNKNOWN; 0 retry POSTs.
  V22-02: Pre-send connect failure -> Exactly 1 retry allowed while PREPARED.
  V22-03: Ambiguous write failure -> CAPTURE_UNKNOWN; zero retries.
  V22-04: Crash after MAY_HAVE_BEEN_SENT -> Recovers to CAPTURE_UNKNOWN; no retry.
  V22-05: Invalid completed capture details -> Mismatch keeps UNKNOWN and alerts.
  V22-06: Duplicate provider observation -> Idempotent single adjustment.
  V22-07: Late success after terminal failure -> Terminal state preserved; adjustment created.
  V22-08: Definitive non-settlement -> Authoritative proof releases unresolved budget.
  V22-09: Reconcile exhaustion -> FAILED(unresolved_capture); budget remains held.
  V22-10: Financial projection rebuild -> Reconstructs budget_accounting from financial_facts.
  V22-11: Concurrent budget admission -> Atomic admission prevents oversell.
  V22-12: Self-exclusion exactly once -> P2/P3/P4 exclude current purchase reservation once.
  V22-13: Auto-approved subset accounting -> R5 subset tracked consistently with R3.
  V22-14: Stale capture owner -> Expired lease allows takeover; stale worker rejected.
  V22-15: Capture resolution race -> Admin, worker, and reconciler resolve via single CAS.
  V22-16: Queue dispatch crash gap -> Un-enqueued QUEUED rows recovered on startup.
  V22-17: Queue deadline boundary -> Runs at/past 20s transition to queue_timeout.
  V22-18: Demo tenant isolation -> Cross-tenant resource enumeration returns 404/denial.
```

---

## Verification Plan & Commands

### Automated Test Suite Execution
```bash
# 1. Zero-Dependency Invocation (no virtualenv required, uses system Python)
python -m unittest discover -s backend/tests -t . -p "test_*.py" -v

# Sub-suite targeting:
python -m unittest discover -s backend/tests/unit -t . -p "test_*.py" -v
python -m unittest discover -s backend/tests/integration -t . -p "test_*.py" -v

# 2. Virtualenv + pytest Invocation (requires pip install -r requirements.txt)
source .venv/bin/activate  # Or .venv\Scripts\Activate.ps1 on Windows

# Run All Unit, Property, Loop Guard, and Contract Tests
pytest backend/tests/unit backend/tests/property backend/tests/loop_guard backend/tests/contract -v

# 3. Run Concurrency, Crash, and Adversarial Suites (53 Scenarios)
pytest backend/tests/concurrency backend/tests/integration backend/tests/e2e_api -v

# 4. Run Playwright E2E UI Tests
pytest e2e/ -v

# 5. Full Release Verification Target (uses python -m unittest under the hood)
make test
```

### Live PayPal Sandbox Verification
```bash
# Run interactive live sandbox smoke script (V1 to V12 checks)
python scripts/paypal_smoke.py
```
*Results will be recorded with timestamps, transaction IDs, and redacted response envelopes in `docs/sandbox-verification.md`.*
