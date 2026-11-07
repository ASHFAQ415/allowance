# ALLOWANCE v4.2 — ARCHITECTURAL & SPECIFICATION CHANGELOG

**Document Evolution:** `v4 Baseline` -> `v4.1 Patch` -> `v4.2 Final Adversarial Specification`  
**Date:** November 6, 2026  
**Status:** CONSOLIDATED AUDIT TRAIL

---

## 1. Evolution Summary

| Iteration | Primary Focus | Major Weaknesses Identified |
|---|---|---|
| **v4 Baseline** | Core state machine, PayPilot rules R0–R6, single-transaction decisions, bounded agent loop. | Blind capture retries on timeout; reconcile lacked atomic claim; queue wait exceeded run timeout; missing timezone immutability; approval link derivable from order ID; open pending-cart budget exhaustion. |
| **v4.1 Hardening Patch** | Addressed 27 findings (F1–F27); introduced N1–N30 tests; added DTO allowlists; tightened tool contracts; fixed budget day windows. | Lacked unified admission control across all 8 active states; late capture resolution lacked explicit financial adjustment table; transport exceptions lacked granular HTTPX mapping; queue lifecycle lacked explicit CAS ownership. |
| **v4.2 Final Adversarial Specification** | Complete adversarial engineering hardening; resolved F1–F5; added formal guarantee matrix; defined 35 executable adversarial scenarios; created append-only `ledger_adjustments`. | **Current Production Standard.** Zero known critical architectural contradictions. |

---

## 2. Comprehensive Itemized Changes: v4 Baseline to v4.2

### 2.1 F1: Complete Budget-Reservation Admission Control
- **v4 Flaw:** Only `PENDING_APPROVAL` carts were counted in pending limits. Carts in `AUTO_APPROVED`, `BUYER_APPROVED`, or `CAPTURING` held budget without admission bounds.
- **v4.1 Patch:** Proposed a static cap `MAX_PENDING_CARTS=3` inside `submit`.
- **v4.2 Resolution:** Established a formal dual-layer control architecture:
  - **Operational Admission Layer:** Separate from PayPilot. Enforces `MAX_ACTIVE_RESERVATIONS=5`, `MAX_PENDING_APPROVALS=3`, `MAX_CONCURRENT_RUNS_PER_AGENT=2`, and `QUEUE_MAX_SIZE=4`.
  - Defined explicit set of **8 active reservation states** (`PENDING_APPROVAL`, `AUTO_APPROVED`, `APPROVED`, `PAYPAL_ORDER_CREATED`, `BUYER_APPROVED`, `CAPTURING`, `CAPTURE_UNKNOWN`, and `FAILED(unresolved_capture)`).
  - Implemented atomic `BEGIN IMMEDIATE` reservation claims. Rejected admissions create zero purchases and hold zero budget.

### 2.2 F2: Capture Retry and Uncertainty Protocol
- **v4 Flaw:** Stated "never blindly retry," yet specified 3 capture retry attempts on network error/timeout. Reconcile had no atomic claim.
- **v4.1 Patch:** Prohibited retries once bytes were transmitted. Added pre-send connect error retry.
- **v4.2 Resolution:** 
  - Standardized durable persisted UUID4 identifiers: `purchases.create_request_id` and `purchases.capture_request_id` (guaranteed <= 38 chars).
  - Explicit capture ownership: Compare-and-set claim `BUYER_APPROVED -> CAPTURING` stamping `capture_boot_id = meta.boot_id`.
  - Replaced arbitrary 30s delay assumptions with authoritative `GET /v2/checkout/orders/{id}` reconciliation decisions. Capture POST is retried on attempt 2+ ONLY if order is still `APPROVED` for >= 30s.

### 2.3 F3: Late Capture Success & Financial Ledger Resolution
- **v4 Flaw:** Stated that `FAILED(unresolved_capture)` "stays counted until a human resolves it," but provided no mechanism or schema.
- **v4.1 Patch:** Acknowledged limitation as an open risk in Section 9.
- **v4.2 Resolution:**
  - Evaluated and rejected cyclic purchase state transitions (`FAILED` -> `CAPTURED`).
  - Created append-only `ledger_adjustments` table with unique constraint `UNIQUE(purchase_id, paypal_capture_id)`.
  - Retains immutable terminal state `FAILED(unresolved_capture)` on the purchase while recording `RESOLVED_CAPTURED` adjustments with full audit metadata.

### 2.4 F4: Queue Lifecycle and Worker Ownership
- **v4 Flaw:** Queue size was 8 with 4 workers (45s runs), allowing queued runs to wait 90s, while run timeout marked jobs interrupted at 60s.
- **v4.1 Patch:** Reduced queue to 4; added `queued_at`, `started_at`, and `QUEUE_WAIT_TIMEOUT=20s`.
- **v4.2 Resolution:**
  - Designed formal CAS dequeue claim: `UPDATE agent_runs SET status = 'RUNNING', started_at = :now, worker_id = :worker_id WHERE id = :id AND status = 'QUEUED'`.
  - Worker evaluates `queued_at` before running; if expired, transitions to `FAILED(queue_timeout)` with zero LLM execution.
  - Added stale worker response rejection: All state mutations check that run remains `RUNNING`.
  - Added graceful SIGTERM draining (30s) and startup boot recovery sweeps.

### 2.5 F5: Precise Network Error Classification
- **v4 Flaw:** Broad exception handling classified generic network errors as retryable.
- **v4.1 Patch:** Split connect errors from post-send errors.
- **v4.2 Resolution:**
  - Mapped HTTPX exceptions into 4 deterministic categories:
    1. Pre-transmission (`ConnectTimeout`, `ConnectError`) -> Safe to retry once.
    2. Potentially transmitted (`WriteTimeout`, `WriteError`) -> `CAPTURE_UNKNOWN`; zero retry.
    3. Transmitted / Response Lost (`ReadTimeout`, `ReadError`) -> `CAPTURE_UNKNOWN`; zero retry.
    4. HTTP Status Rejections (4xx, 429, 5xx) -> Distinct mappings for `ORDER_ALREADY_CAPTURED`, instrument declines, rate limits, and server errors.

### 2.6 Schema & API Changes Summary

| Table / Area | v4 Baseline | v4.1 Patch | v4.2 Final Standard |
|---|---|---|---|
| `purchases` table | Missing persistent request IDs; single timeout column. | Added `create_request_id`, `capture_request_id`, `capture_boot_id`, `reconcile_claim_at`. | Fully persisted; UUID4 validated; foreign keys enforced; index on `(budget_day, status)`. |
| `ledger` table | Standard append-only table. | Added `UNIQUE(purchase_id, to_state)`. | Unique rank enforcement; SQLite `ABORT` triggers for UPDATE and DELETE. |
| `ledger_adjustments` | Non-existent. | Non-existent. | **NEW:** Explicit double-entry financial discrepancy resolution table. |
| `meta` table | Non-existent. | Added `boot_id`. | Persistent system metadata table tracking runtime instance boot ID. |
| `policy.timezone` | Mutable via PUT. | Immutable via PUT (422). | Immutability strictly enforced; seeds only; prevents budget day manipulation. |
| Public DTOs | Undefined filtering; `approval_url` hidden. | Added initial DTO allowlists. | Explicit allowlist stripping `paypal_order_id`, `paypal_capture_id`, `approval_url`, and `trace_json`. |
| API Status Reasons | Generic error strings. | Added `paypal_bad_response`, `queue_timeout`, `pending_cap`. | Full taxonomy of deterministic terminal error reasons. |

---

## 3. Contradiction Resolution Matrix

| Issue Location | Original Contradiction | Final v4.2 Resolution |
|---|---|---|
| **Capture Count** | §0 claimed "at most one capture attempt"; §6 specified up to 3 retries. | Resolved: Exactly 1 capture POST path. Retries only permitted via spaced reconciliation if GET order remains `APPROVED`. |
| **Budget Permanence** | §0 claimed "no budget held forever"; §3.1 left `CAPTURE_UNKNOWN` unexpiring. | Resolved: Budget held until end of `budget_day` for unresolved captures. This is deliberate and documented. |
| **Queue vs Timeout** | §4 queue depth of 8 caused queued runs to be marked interrupted before worker started. | Resolved: Queue bounded to 4; `QUEUE_WAIT_TIMEOUT=20s`; workers cancel expired jobs immediately. |
| **Worker Overwrite** | Crash recovery flipped purchase to `CAPTURE_UNKNOWN`; worker result had no legal transition to `CAPTURED`. | Resolved: CAS update accepts `status IN ('CAPTURING', 'CAPTURE_UNKNOWN')`. |
| **Approval Link Privacy** | Link was admin-only, but contained `order_id` which leaked in public endpoints. | Resolved: Public DTOs strictly strip `paypal_order_id`, `paypal_capture_id`, and `approval_url`. |
| **Order Status on Create** | Assumed `CREATED` status; PayPal returns `PAYER_ACTION_REQUIRED`. | Resolved: State machine maps `PAYER_ACTION_REQUIRED` to awaiting buyer without failure. |

