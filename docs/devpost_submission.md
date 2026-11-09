# Devpost Submission Master Document

> **PayPal AI Hackathon 2026 Submission**  
> **Repository:** [https://github.com/ASHFAQ415/allowance](https://github.com/ASHFAQ415/allowance)

---

## 1. Project Overview

- **Project Title:** ALLOWANCE: Give your AI Agent an Allowance, Not Your Wallet
- **Tagline:** Autonomous AI spending control, policy engine, and double-entry financial authorization platform built on PayPal Orders v2.
- **Track & Main Categories Targeted:**
  - **Best Use of Agentic Commerce**
  - **Best Use of PayPal + AI**
  - **Most Impactful**
  - **Best Demo Delivery**
- **Sponsor Prize Targeted:**
  - **Render:** One-click containerized deployment via free-tier compatible `render.yaml` Infrastructure-as-Code blueprint.
- **Additional Tool Integrations:**
  - **Postman:** Comprehensive API Collection v2.1 with automated JavaScript assertions for all REST v1 and PayPal Sandbox routes *(tool integration; no official sponsor prize)*.

---

## 2. Devpost Submission Text

### Inspiration: The Autonomous Spending Dilemma
We are rapidly entering the era of autonomous AI agents. Businesses and individuals want LLMs to book flights, buy office supplies, pay SaaS bills, and replenish inventory. 

However, granting an AI direct access to credit cards or raw payment credentials creates an unacceptable financial catastrophe:
- **Hallucinations & Loop Spends:** A runaway prompt can place dozens of orders in seconds.
- **Prompt Injection:** An adversary can trick a customer service or procurement agent into buying unapproved products or routing money to malicious third parties.
- **Blind Payment Retries:** When a network timeout occurs, naive code retries the payment capture, causing double or triple charges.
- **Lack of Accounting:** Traditional apps track "workflow state" (Order Placed) rather than authoritative financial truth.

We asked: *What if AI agents didn't hold wallets at all? What if we gave them an Allowance instead?*

### What It Does
**ALLOWANCE** is an uncompromisable, deterministic financial authorization platform and policy engine that sits between any AI agent and PayPal payment execution rails.

1. **Separation of Powers:** The AI agent (ShopMind) runs in a strict sandbox. It has zero access to payment credentials, cannot initiate payment captures, and cannot modify policies. It can only propose purchases against server-verified quotes.
2. **Deterministic Policy Engine (PayPilot):** Every purchase proposal is evaluated against mathematical invariants (Rules R0–R6, Checkpoints P1–P4):
   - **Rule R0 (Emergency Kill Switch):** System-wide freeze instantly blocks all autonomous transactions.
   - **Rule R1 (Merchant Allowlist):** Only verified, onboarded merchants can receive funds.
   - **Rule R2 & R3 (Budget Ceilings):** Strict daily allowances and per-transaction limits.
   - **Rule R4 (Human Review Threshold):** Purchases over $50 (business) or $30 (personal) escalate to a human review queue.
   - **Rule R5 (Velocity Cap):** Cumulative auto-approved spend within a window cannot exceed velocity limits.
   - **Rule R6 (Auto-Approve):** Legitimate, in-policy carts under the threshold are authorized automatically.
3. **Formal Double-Entry Bookkeeping:** Allowance implements formal commitment accounting with balanced debits and credits across 6 standard accounts (`1000 Assets`, `2100 Liabilities`, `3100 Equity`, `5100 Expenses`, etc.). Every journal entry strictly enforces $\sum \text{Debits} = \sum \text{Credits}$.
4. **Hardened PayPal Orders v2 Settlement:** Allowance enforces exclusive atomic CAS lease fencing, durable idempotency markers (`MAY_HAVE_BEEN_SENT`), and an absolute prohibition against blind capture retries. It extracts and verifies genuine nested capture IDs (`purchase_units[0].payments.captures[0].id`).
5. **Two-Step Approval Visibility:** Differentiates Step 1 (PayPilot Supervisor Approval) from Step 2 (PayPal Buyer Consent), ensuring human oversight is transparent.

---

### How We Built It
- **Backend & Policy Engine:** Python 3.11 with SQLite WAL mode and serialized `BEGIN IMMEDIATE` transactions, providing atomic admission and zero phantom reservations.
- **Payment Rails:** PayPal Orders v2 (supporting Mock and Live Sandbox environments).
- **Accounting Architecture:** Formal double-entry general ledger with database-level immutability triggers (`abort_journal_entries_update`, `abort_journal_entries_delete`).
- **AI Integration (MCP & REST):** 
  - Model Context Protocol (MCP) JSON-RPC 2.0 server (`backend/app/mcp/server.py`) exposing 5 bounded tools (`search_products`, `get_product_quote`, `get_allowance`, `request_purchase`, `get_purchase_status`), while strictly withholding capture primitives.
  - Enterprise REST API v1 (`/v1/*`) secured with SHA-256 hashed API keys (`ak_test_...`).
- **Frontend Dashboard:** React 18, Vite, Lucide Icons, and Tailwind CSS (pre-built production bundle committed to repository).
- **Infrastructure & Automation:** One-click Windows launcher (`run.bat`), Desktop shortcut generator, Render deployment blueprint (`render.yaml`), and multi-stage Docker container.

---

### The 7 Non-Negotiable Financial Safety Invariants
We built Allowance around 7 mandatory architectural constraints:
1. **No Blind Capture Retries:** Once payment bytes may have been sent, the outcome is `UNCERTAIN`. Never issue another capture `POST` on timeouts, 429s, or 5xx. Persist `MAY_HAVE_BEEN_SENT` before sending.
2. **Separate Workflow State from Financial Truth:** Workflow state is not the balance sheet. All money movements live in an append-only journal.
3. **Preserve Financial Reservations & Cents Math:** Committed spend = Captured funds + Active reservations + Unresolved commitments. Zero floating-point arithmetic.
4. **Atomic Admission & Queue Claims:** Capacity checks and queue claims execute within a single serialized transaction.
5. **Ownership & Concurrency Fencing:** Exclusive CAS leases prevent worker collisions and stale writes.
6. **AI & Payment Authority Separation:** AI proposals are untrusted input. The deterministic policy engine is the sole financial authority.
7. **Evidence-Based Engineering:** Payments are only marked captured with authoritative provider evidence. 137 automated regression tests verify every behavior.

---

### Compliance: What Was Built & Significantly Updated During Hackathon Period
In accordance with official hackathon rules for project updates, the entire Allowance platform was architected, implemented, and verified during the hackathon submission period (initiated November 6, 2026):
- **Formal Double-Entry Bookkeeping Engine (`backend/app/db/double_entry.py`):** Built from scratch with Chart of Accounts, balancing constraint validation ($\sum \text{debits} = \sum \text{credits}$), and rebuildable projections.
- **Model Context Protocol (MCP) Server (`backend/app/mcp/server.py`):** Designed and built a standalone JSON-RPC 2.0 MCP server with 5 safe tools.
- **Enterprise REST API v1 (`backend/app/api/v1/`):** Implemented `/v1/quotes`, `/v1/purchase-requests`, `/v1/allowance`, `/v1/freeze`, and `/v1/unfreeze`.
- **Cryptographic Credential Subsystem (`backend/app/auth/`):** SHA-256 hashed API keys (`ak_test_...`), constant-time validation (`hmac.compare_digest`), and scoped permissions.
- **PayPal Orders v2 Nested Capture Extraction:** Upgraded response parsing in `capture_lifecycle.py` and `client.py` to parse nested captures (`purchase_units[0].payments.captures[0].id`).
- **Personal Edition MVP Profile:** Individual $100 daily allowance, $30 auto-threshold, and payee routing.
- **Test Suite Expansion:** Expanded automated test coverage from initial draft to **137 passing tests**.
- **Windows Zero-Configuration Launcher & Reset:** Created `run.bat`, `create_desktop_shortcut.bat`, and `seed_db.py --reset`.

---

### Sponsor Prizes Targeted & Justification

#### Render Sponsor Prize
- **Artifacts:** `render.yaml` (Render Blueprint) and multi-stage `Dockerfile`.
- **Capabilities:** Production-ready container building the React frontend and Python runtime, with zero-configuration one-click deployment via free-tier compatible `render.yaml` blueprint (using transient SQLite storage; no paid persistent disks required).

*(Note: Postman Collection v2.1 with embedded automated test assertions is also included in `postman/` as an API developer tool).*

---

## 3. Testing Instructions (for Devpost "Testing instructions" field)

```text
================================================================================
ALLOWANCE (v5.0) — JUDGE TESTING INSTRUCTIONS (60 SECONDS)
================================================================================

1. ZERO-CONFIGURATION QUICK START:
   Allowance runs completely out-of-the-box with NO API keys or external setups.
   Node.js is NOT required (the pre-built frontend bundle is included).

   - On Windows: Double-click "run.bat" (or run "$env:PYTHONPATH='.' ; python scripts/dev_server.py")
   - On macOS/Linux: Run "PYTHONPATH=. python3 scripts/dev_server.py"
   - Open browser: http://localhost:8000

2. EVALUATION OF THE 4 CORE DEMO SCENARIOS:
   The UI evaluates against the Demo Organization Profile ($300 budget, $50 auto-threshold).
   Click the preset chips on the dashboard:

   Scenario 1: [Trail Shoes ($45)] -> Click "Execute Autonomous Order"
   -> Outcome: AUTO_APPROVED and settled on PayPal Orders v2 ($45.00) under Rule R6.

   Scenario 2: [Road Flats ($90)] -> Click "Execute Autonomous Order"
   -> Outcome: Held in Human Review Queue under Rule R4 (exceeds $50 auto-threshold).
   -> Click "Authorize & Complete": Supervisor approves (P2) + PayPal buyer consent confirmed -> $90.00 settles.

   Scenario 3: [DarkWeb Store] -> Click "Execute Autonomous Order"
   -> Outcome: Immediate BLOCKED under Rule R1 (Merchant not allowlisted). Zero funds moved.

   Scenario 4: [Emergency Kill Switch]
   -> Click "Toggle Kill Switch" in header -> Try ordering shoes.
   -> Outcome: Immediate BLOCKED under Rule R0 (Emergency spending freeze ACTIVE).
   -> Click "Deactivate Kill Switch" to resume normal operations.

3. REPEATABLE CLEAN RESET:
   To return the database, budget, and ledger to a pristine 0-state at any time:
   python scripts/seed_db.py --reset  (or python3 scripts/seed_db.py --reset)

4. ADMIN KEY:
   The admin key is pre-filled on the dashboard header:
   dev-secret-admin-token-min-32-chars-long-abc-123
   (Passed via 'X-Admin-Token' for freeze/unfreeze and approvals).

5. AUTOMATED TEST SUITE:
   Run all 137 tests covering isolation, double-entry balancing, and adversarial conditions:
   python -m unittest discover -s backend/tests -t . -p "test_*.py"

6. (OPTIONAL) LIVE PAYPAL SANDBOX:
   To test with your own PayPal developer sandbox app:
   Copy .env.example to .env, set PAYPAL_MODE=sandbox, set PAYPAL_CLIENT_ID and
   PAYPAL_CLIENT_SECRET, and run: python scripts/paypal_smoke.py
   (Verified evidence is available in docs/sandbox-verification.md).
================================================================================
```

---

## 4. Built With
`Python`, `PayPal Orders v2`, `FastAPI`, `SQLite WAL`, `React 18`, `Tailwind CSS`, `Model Context Protocol (MCP)`, `Postman`, `Render`, `Docker`.
