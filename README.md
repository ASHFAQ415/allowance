# Allowance (v4.2.2)

> **Tagline:** Give your AI agent an allowance, not your wallet.  
> **PayPal AI Hackathon 2026**

---

## 1. Executive Overview

Allowance is an autonomous financial authorization platform that places an uncompromisable, deterministic policy engine (**PayPilot**) and an append-only state machine between an AI shopping agent (**ShopMind**) and payment execution rails (**PayPal Orders v2**).

No matter what hallucinations, loops, or prompt injections an AI model experiences, funds cannot move without passing through four transactional authorization checkpoints, strict velocity limits, budget reservations, and explicit human approvals where mandated.

---

## 2. Core Architecture & Enforced Invariants

1. **No Blind Capture Retries on Ambiguous Transport:**
   - Once a capture request may have reached PayPal, the outcome is UNCERTAIN.
   - Allowance **never** blindly re-sends a capture POST upon timeouts, 429s, or 5xx errors.
   - Reconciliation is strictly read-only (`GET /v2/checkout/orders/{id}`).
2. **Decoupled Workflow Lifecycle & Financial Accounting:**
   - `purchases.status` tracks workflow states (`CREATED -> CART_READY -> AUTO_APPROVED / PENDING_APPROVAL -> APPROVED -> PAYPAL_ORDER_CREATED -> BUYER_APPROVED -> CAPTURING -> CAPTURED`).
   - `financial_facts` is the append-only journal of financial movements.
   - `budget_accounting` is a rebuildable transactional projection.
   - Late capture settlements are appended to `ledger_adjustments` without rewriting terminal purchase history.
3. **Atomic Admission & Queue Deadline Claims:**
   - Capacity checks (max 8 active runs, max 2 active runs per agent) run under `BEGIN IMMEDIATE` *before* inserting job records.
   - Queue deadlines are enforced inside the atomic SQL claim:
     `UPDATE agent_runs SET status = 'RUNNING' WHERE id = :id AND status = 'QUEUED' AND :now < queued_at + 20s`.
4. **AI & Payment Separation:**
   - ShopMind proposals run in a sandbox; catalog descriptions are untrusted data wrapped in `<catalog_data>`.
   - Prices come exclusively from the server-side catalog.
   - `submit_cart` accepts **zero parameters** to eliminate model payload tampering.

---

## 3. Quick Start & Local Execution

### Prerequisites
- Python 3.11+
- Node.js 18+ (for frontend dashboard)

### 1. Initialize & Seed Database
```bash
python scripts/seed_db.py
```

### 2. Run All Automated Tests
```bash
python -m unittest discover -s backend/tests -t . -p "test_*.py" -v
```

### 3. Start Backend Server
```bash
uvicorn backend.app.api:app --reload --port 8000 --workers 1
```

### 4. Start React Frontend Dashboard
```bash
cd frontend
npm install
npm run dev
```
Open [http://localhost:5173](http://localhost:5173) in your browser.

---

## 4. Phased Demo Script (3-Minute Master Cut)

1. **Auto-Approve Flow (0:00 - 1:00):**  
   Request *"Find trail running shoes under $45 from approved store"*. ShopMind builds the cart, PayPilot Checkpoint P1 auto-approves under Rule R6. PayPal order created and captured live in sandbox.
2. **Human Approval Threshold (1:00 - 1:45):**  
   Request *"Buy road racing shoes for $90"*. PayPilot flags Rule R4 (Threshold > $50). Cart pauses in Approval Queue. Presenter clicks "Approve", buyer authorizes on PayPal, and capture completes.
3. **Adversarial Merchant Block (1:45 - 2:15):**  
   Request *"Buy VPN subscription from merchant_unapproved"*. PayPilot instantly blocks the purchase under Rule R1. Zero funds held.
4. **Emergency Freeze Kill Switch (2:15 - 3:00):**  
   Click "Toggle Kill Switch". Emergency Freeze (Rule R0) activates immediately. Any subsequent order is halted at Checkpoint P1.

---

## 5. Explicit Limitations & Honest Disclosures

- **In-Flight Capture Recall:** Once HTTP bytes are accepted by PayPal, an emergency freeze cannot recall an in-flight network packet if PayPal settles it.
- **Single-Process Constraint:** SQLite WAL and thread concurrency require a single Uvicorn worker process (`workers=1`).
- **Sandbox Buyer Consent:** Fully autonomous zero-click charging requires PayPal Vaulting, which requires enterprise approval and is slated for the post-hackathon roadmap.

