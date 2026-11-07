# ALLOWANCE v4.2 — FORMAL GUARANTEE & FAILURE ANALYSIS MATRIX

**Status:** APPROVED ENGINEERING SPECIFICATION (v4.2)  
**Verification Level:** Rigorous Formal Verification Mapping  
**Classification Categories:**  
1. *Guaranteed by application code*  
2. *Dependent on SQLite behaviour*  
3. *Dependent on PayPal behaviour*  
4. *Dependent on infrastructure*  
5. *Best-effort only*  
6. *Not guaranteed*  

---

## 1. Guarantees vs. External Realities: Guiding Principles

In financial payment systems, overclaiming guarantees creates severe security blind spots. Allowance v4.2 explicitly rejects marketing buzzwords such as "foolproof," "zero-risk," or "absolute security." 

Every guarantee is tied directly to an enforcing software component, an atomic transaction or database constraint, a defined failure scenario, a concrete recovery mechanism, and an executable regression test.

---

## 2. Guarantee-to-Implementation-to-Test Matrix

| # | System Guarantee | Classification Category | Exact Enforcing Component | Relevant Database Constraint or Transaction | Failure Scenario | Recovery Mechanism | Regression Test | External Dependency |
|---|---|---|---|---|---|---|---|---|
| **G1** | **The LLM never controls money or executes payments** | Guaranteed by application code | Module boundary (`agent/` vs `orchestrator/`); `submit_cart` tool takes 0 args | Pydantic cart validation; server-side catalog price lookup | Model hallucinates payment token or modifies cart total in transit | Orchestrator fetches real prices from catalog; ignores model numbers | `test_agent_isolation.py`, `test_cart_pricing.py` (Suite A, C) | None (Internal) |
| **G2** | **No unbounded agent execution loops** | Guaranteed by application code | `app/agent/loop.py` bounded loop runner | `agent_runs.steps_used` integer counter; `agent_runs.status` CAS | Model loops repeatedly calling `search_catalog` or hangs on tool calls | Step cap at 6 calls; repeat-call detector; 45s wall-clock timeout | `test_loop_guard.py` (Suite C) | Anthropic API (for timeouts) |
| **G3** | **Spend never exceeds daily budget under concurrency** | Dependent on SQLite behaviour | `app/orchestrator/core.py` & `app/policy/rules.py` (Rule R3) | `BEGIN IMMEDIATE` lock; atomic sum over `purchases` in `budget_day` | 10 concurrent threads submit carts simultaneously against $10 remaining budget | SQLite serializes write transactions; 9 threads read committed reservation and get `BLOCKED` | `test_concurrent_budget_race.py` (Suite E #1, #15) | SQLite WAL file locking |
| **G4** | **At most one capture attempt in flight per order** | Dependent on SQLite behaviour | `app/orchestrator/core.py` capture claim step | Atomic CAS: `UPDATE purchases SET status = 'CAPTURING' WHERE id = :id AND status = 'BUYER_APPROVED'` | 2 concurrent webhook/return requests attempt to capture approved order | Only 1 worker updates 1 row; 2nd worker updates 0 rows and aborts | `test_concurrent_capture_claim.py` (Suite E #2, #14) | SQLite single-writer lock |
| **G5** | **Zero cyclic state machine transitions** | Guaranteed by application code & DB | `app/orchestrator/state_machine.py` rank validation | `UNIQUE(purchase_id, to_state)` on `ledger` table | Bug attempts to move purchase from `CAPTURED` back to `CAPTURING` or `CREATED` | Code raises `IllegalTransitionError`; Database raises `sqlite3.IntegrityError` | `test_state_machine_matrix.py`, `test_rank_progression.py` (Suite A) | None (Internal) |
| **G6** | **Ledger entries and policy history are strictly immutable** | Dependent on SQLite behaviour | SQLite Database Engine | Database triggers: `abort_ledger_update`, `abort_ledger_delete`, etc. | Attacker or rogue code executes `UPDATE ledger SET amount_cents = 0` | SQLite trigger raises `ABORT`; transaction fails; zero rows mutated | `test_ledger_immutability.py` (Suite A) | SQLite engine trigger compliance |
| **G7** | **A transport timeout is never treated as a payment failure** | Guaranteed by application code | `app/paypal/client.py` network error classifier | State transition to `CAPTURE_UNKNOWN`; budget remains reserved | WiFi drop or PayPal 15s delay occurs during capture POST | Purchase marked `CAPTURE_UNKNOWN`; reconciliation engine verifies state before retry | `test_capture_timeout_recovery.py` (Suite D, E #3, #4) | None (Internal policy) |
| **G8** | **Single-flight, bounded reconciliation** | Dependent on SQLite behaviour | `app/orchestrator/reconcile.py` | Atomic CAS: `SET reconcile_attempts = reconcile_attempts + 1, next_reconcile_at = :now + 30 WHERE next_reconcile_at <= :now` | 5 concurrent requests hit `/purchases/{id}/reconcile` | Only 1 caller increments counter and obtains lock; others receive 409 | `test_concurrent_reconcile.py` (Suite E #7, N4) | SQLite concurrency |
| **G9** | **Worker crashes during capture are safely recovered** | Guaranteed by application code | Startup boot sweep & `meta.boot_id` | `UPDATE purchases SET status = 'CAPTURE_UNKNOWN' WHERE status = 'CAPTURING' AND capture_boot_id != :current_boot` | Host server suffers power cut while worker is in `CAPTURING` | Next boot generates new `boot_id`; orphaned `CAPTURING` flipped to `CAPTURE_UNKNOWN` | `test_crash_recovery_boot.py` (Suite E #5, #23, N9) | File persistence across restarts |
| **G10** | **Emergency Freeze halts payment before capture** | Dependent on SQLite behaviour | Checkpoint P4 in `app/orchestrator/core.py` | `BEGIN IMMEDIATE` transaction re-evaluating `policy.frozen` | Admin flips Freeze while buyer is authenticating on PayPal checkout | Checkpoint P4 runs before capture claim; transitions to `BLOCKED(R0)`; capture aborted | `test_freeze_during_approval.py` (Suite E #10, #16, N26) | None (Internal) |
| **G11** | **Worker queue memory is bounded** | Guaranteed by application code | `app/orchestrator/queue.py` | `queue.Queue(maxsize=4)` with immediate 503 fallback | Traffic spike sends 20 requests/sec to `/agent/run` | Requests 9+ receive `503 Service Unavailable` with `Retry-After: 5`; zero RAM leaks | `test_queue_saturation.py` (Suite E #21, N8) | None (Internal) |
| **G12** | **Public endpoints never leak payment identifiers** | Guaranteed by application code | `app/api/dto.py` explicit Pydantic allowlists | Database fields omitted from public serialization models | Malicious visitor inspects SSE stream or `/purchases` JSON for PayPal order IDs | Public models strip `paypal_order_id`, `approval_url`, and `capture_id` | `test_dto_leakage.py` (Suite E #33, N13) | None (Internal) |
| **G13** | **No duplicate capture on PayPal side for same request ID** | Dependent on PayPal behaviour | `PayPal-Request-Id: {purchase.capture_request_id}` | Persistent UUID4 stored in `purchases.capture_request_id` | Network retry or duplicate reconcile POST reaches PayPal | PayPal Orders v2 idempotency engine matches Request-Id and returns cached capture | `test_paypal_idempotency_live.py` (Suite D, F) | **PayPal Orders v2 backend** |
| **G14** | **Order amount, currency, and payee match cart before capture** | Guaranteed by application code | `app/orchestrator/core.py` `verify_order()` | Pre-capture verification assertions on GET Order payload | Tampered PayPal order or currency mismatch in sandbox | Assertions fail; state transitions to `FAILED(amount_mismatch)`; capture aborted | `test_verify_order_tamper.py` (Suite D, E #13, #14, #15) | PayPal GET Order accuracy |
| **G15** | **Late capture success does not rewrite historical records** | Guaranteed by application code & DB | `app/orchestrator/ledger_resolution.py` | Append-only insert to `ledger_adjustments` table; unique index on capture ID | Reconciliation exhausted -> `FAILED`; PayPal later confirms money settled | Terminal status preserved; adjustment record inserted with audit trail | `test_late_capture_adjustment.py` (Suite E #6) | None (Internal) |

---

## 3. Explicit Analysis of Dependencies & Non-Guaranteed Items

### 3.1 Dependent on SQLite Behaviour
- **File System Durability:** Guarantees G3, G4, G6, and G9 rely on SQLite WAL operating with `PRAGMA synchronous = FULL`. If the underlying disk hardware reports write completion before flushing to non-volatile media, an abrupt host crash could lose the most recent committed page. This is accepted within standard POSIX database operating parameters.
- **Single Writer Constraint:** SQLite handles concurrency via a single writer lock. If multiple uvicorn worker processes are launched, lock contention will rise, resulting in 503 errors. Allowance explicitly enforces **a single uvicorn worker process**.

### 3.2 Dependent on PayPal Behaviour
- **Idempotency Retention Period:** PayPal documentation recommends UUIDs and states idempotency retention "up to 45 days," but Orders v2 specific retention windows are not formally guaranteed in SLA. Allowance windows operate within 15 minutes, where idempotency retention is experimentally verified.
- **Concurrent Request Collisions:** Official PayPal docs acknowledge that two simultaneous requests with the identical `PayPal-Request-Id` might result in one processing while the second returns HTTP 400/409. Allowance guarantees that duplicate calls are never made concurrently from our code (G4).

### 3.3 What Allowance Honestly CANNOT Guarantee
1. **Recalling In-Flight Captures:** Once HTTP bytes for a capture POST have been accepted by PayPal's servers, activating Emergency Freeze cannot pull the money back if PayPal's settlement engine processes it.
2. **Zero Bugs in External Models:** Allowance does not guarantee that the Claude LLM will understand every complex shopping request. It guarantees that when the LLM fails or hallucinates, money is not spent outside policy.
3. **Autonomous Settlement Without Buyer Interaction:** Allowance cannot bypass PayPal's buyer approval login/redirect requirement in sandbox. Fully autonomous charging requires PayPal Vaulting, which requires enterprise approval.

