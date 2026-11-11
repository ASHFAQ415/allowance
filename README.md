# ALLOWANCE (v5.0)

> **"Give your AI agent an allowance, not your wallet."**  
> *Autonomous AI Spending Control, Policy Engine & Double-Entry Financial Authorization Platform.*  
> Built for the **PayPal AI Hackathon 2026**.

[![Tests Passing](https://img.shields.io/badge/Tests-157%20passing-brightgreen.svg)](#6-automated-testing-157--157-passing)
[![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.14-blue.svg)](https://www.python.org/)
[![PayPal Orders v2](https://img.shields.io/badge/PayPal-Orders%20v2%20(Sandbox--Verified%20%7C%20Mock%20Rail)-003087.svg)](docs/sandbox-verification.md)
[![Submission Readiness](https://img.shields.io/badge/Submission-Readiness%20Verified-blue.svg)](docs/submission_readiness_report.md)
[![Postman Collection](https://img.shields.io/badge/Postman-Collection%20v2.1-FF6C37.svg)](postman/allowance_v5_collection.json)
[![Render Blueprint](https://img.shields.io/badge/Render-Blueprint%20Included-46E3B7.svg)](render.yaml)
[![MCP Compatible](https://img.shields.io/badge/MCP-JSON--RPC%202.0%20Compatible-orange.svg)](#option-b-model-context-protocol-mcp-server)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## 1. Executive Summary

Modern AI agents are increasingly tasked with autonomous purchasing, booking, and procurement. However, granting an AI direct access to credit cards or raw payment credentials presents catastrophic financial risks: model hallucinations, prompt injection, blind retry loops, merchant spoofing, and runaway budget exhaustion.

**ALLOWANCE** solves this by establishing a strict separation of powers:

```
+-----------------------------------------------------------------------------------+
|                           EXTERNAL AI PROCUREMENT AGENT                           |
|                  (Claude / Gemini / GPT / Custom LangChain / MCP)                 |
+-----------------------------------------------------------------------------------+
                                          |
                        Proposes item carts & quotes ONLY
                        (ZERO payment credentials / keys)
                                          v
+-----------------------------------------------------------------------------------+
|                        ALLOWANCE AUTHORIZATION & ORCHESTRATION                    |
|                                                                                   |
|  [REST API v1 & MCP Server]      [PayPilot Deterministic Policy Engine]           |
|  - Bearer `ak_test_...` auth     - Rules R0-R6: Limits, Velocity, Allowlist       |
|  - Server-side trusted quotes    - Checkpoints P1-P4 (Self-exclusion math)        |
|                                                                                   |
|  [Formal Double-Entry Ledger]    [Hardened PayPal Orders v2 Orchestrator]         |
|  - Accounts: 1000, 2100, 5100    - `MAY_HAVE_BEEN_SENT` state persistence         |
|  - sum(Debits) == sum(Credits)   - Exclusive CAS lease fencing (No blind retries) |
+-----------------------------------------------------------------------------------+
                                          |
                      Authoritative captured settlement funds
                                          v
+-----------------------------------------------------------------------------------+
|                       PAYPAL ORDERS v2 / SUPPORTED MERCHANTS                      |
|                (Buyer Consent -> Nested Capture ID Verification)                  |
+-----------------------------------------------------------------------------------+
```

- **Zero Credentials for AI:** The AI agent never holds PayPal credentials, credit card numbers, or capture authorization. It can only propose carts against server-authenticated quotes.
- **PayPilot Policy Engine:** A deterministic, mathematical engine enforcing budget limits, per-transaction ceilings, velocity caps, and approved merchant allowlists (Rules R0–R6, Checkpoints P1–P4).
- **Formal Double-Entry Bookkeeping:** Every financial movement is recorded in an append-only double-entry general ledger satisfying the mathematical invariant $\sum \text{Debits} = \sum \text{Credits}$ per currency.
- **Hardened PayPal Orders v2 Rails:** Strict adherence to financial safety invariants: exclusive-claim state machines, atomic CAS fencing, durable idempotency markers (`MAY_HAVE_BEEN_SENT`), and an absolute prohibition against blind capture retries.

---

## 2. Judge Quick Start (60 Seconds)

Allowance is designed for immediate testing with **zero configuration and zero API keys**. The pre-compiled frontend distribution (`frontend/dist/`) is committed to the repository, meaning **Node.js is not required to run the application**.

### 1. Prerequisites
- **Python 3.10, 3.11, or 3.12** installed and on your `PATH`.
- (Optional) Node.js is only needed if you wish to rebuild the frontend from source.

### 2. Launch the Application

| Platform | Command | Notes |
|---|---|---|
| **Windows (One-Click)** | Double-click `run.bat` | Launches dev server and opens browser automatically |
| **Windows (PowerShell)** | `$env:PYTHONPATH="." ; python scripts/dev_server.py` | Runs on `http://localhost:8000` |
| **macOS / Linux (Bash)** | `PYTHONPATH=. python3 scripts/dev_server.py` | Runs on `http://localhost:8000` |

Open **`http://localhost:8000`** in your web browser.

### 3. Click the 4 Interactive Scenarios

The dashboard loads with the **Demo Organization Profile** ($300 daily budget, $50 auto-approval threshold, $150 max per-transaction). Click each quick-test chip:

| # | Interactive Scenario Chip | Action | Enforced Rule & Checkpoint | Authoritative Outcome |
|---|---|---|---|---|
| 1 | 👟 **Trail Shoes ($45)** | Click chip → Click **Execute Autonomous Order** | **Rule R6** (Auto-Approve within $50 threshold) | **`AUTO_APPROVED` → `CAPTURED`**<br/>Order created and captured on PayPal Orders v2 (`$45.00` settled). Ledger records balanced double-entry lines. |
| 2 | ⚡ **Road Flats ($90)** | Click chip → Click **Execute Autonomous Order** | **Rule R4** (Approval Threshold: > $50) | **`PENDING_APPROVAL`**<br/>Held in Human Review Queue. Click **Authorize & Complete** → Supervisor approves (P2) + PayPal buyer consent confirmed → `$90.00` settles. |
| 3 | 🚫 **DarkWeb Store** | Click chip → Click **Execute Autonomous Order** | **Rule R1** (Merchant Allowlist) | **`BLOCKED`**<br/>Immediate deterministic block: *`Merchant 'merchant_unapproved' is not in approved allowlist`*. Zero funds encumbered. |
| 4 | 🛡️ **Emergency Kill Switch** | Click **Toggle Kill Switch** in header → Order shoes | **Rule R0** (Emergency Freeze) | **`BLOCKED`**<br/>System banner turns red. All agent transactions blocked at DB layer. Click **Deactivate Kill Switch** to resume. |

### 4. Reset to Clean State at Any Time

To return the database, purchases, budget, and ledger to a pristine 0-state:

- **Windows:** `python scripts/seed_db.py --reset`
- **macOS / Linux:** `python3 scripts/seed_db.py --reset`

---

## 3. Modes & Environment Configuration

### Execution Modes

Allowance operates in two payment modes and two AI agent modes:

| Mode Category | Mode Name | Trigger / Config | Requires Credentials? | Behavior |
|---|---|---|---|---|
| **Payment Rails** | **Mock Mode** *(Default)* | `PAYPAL_MODE=mock` (or omitted) | **No keys required** | In-memory Orders v2 state machine with strict schema parity, integer-cent arithmetic, durable idempotency, and simulated buyer consent (default for local dev and dashboard demo). |
| **Payment Rails** | **Sandbox Mode** | `PAYPAL_MODE=sandbox` in `.env` | **PayPal Sandbox App** | PayPal Orders v2 (sandbox-verified via `scripts/paypal_smoke.py`; connects to `https://api-m.sandbox.paypal.com/v2/checkout/orders` with live buyer consent and nested capture verification). |
| **AI Agent** | **Deterministic Fallback** *(Default)* | `ANTHROPIC_API_KEY` omitted | **No keys required** (Works offline) | Parses search query against verified catalog, calculates quantities, and builds compliant cart with zero external network dependencies. |
| **AI Agent** | **Claude 3.5 Sonnet** | `ANTHROPIC_API_KEY` set in `.env` | **Anthropic API Key** | Uses Claude 3.5 Sonnet with function calling to explore catalog, evaluate specifications, and construct cart. |

### Admin Key & Security Tokens

- **Admin Key:** Used by the `X-Admin-Token` header for toggling the emergency freeze (`/api/freeze`) and supervisor approvals (`/api/purchases/{id}/approve`).
- **Default Value:** Pre-configured as `dev-secret-admin-token-min-32-chars-long-abc-123` and pre-filled in the dashboard header for frictionless hackathon evaluation.
- **Custom Value:** Set `ADMIN_TOKEN` in `.env` to override.

### Environment Variables Reference

Copy [`.env.example`](.env.example) to `.env` to configure optional live sandbox credentials:

| Variable | Description | Default Value | Required? |
|---|---|---|---|
| `PAYPAL_MODE` | PayPal rail mode (`mock` or `sandbox`) | `mock` | No |
| `PAYPAL_CLIENT_ID` | PayPal Developer Sandbox App Client ID | *(empty)* | Only if `PAYPAL_MODE=sandbox` |
| `PAYPAL_CLIENT_SECRET` | PayPal Developer Sandbox App Secret | *(empty)* | Only if `PAYPAL_MODE=sandbox` |
| `PAYPAL_SANDBOX_PAYEE` | Global Sandbox Merchant Payee Email | *(empty)* | Optional (overrides merchant routing) |
| `PAYPAL_MERCHANT_SHOES_PAYEE` | Sandbox Payee for Footwear Emporium | `shoes-facilitator@allowance.dev` | Optional |
| `ANTHROPIC_API_KEY` | Anthropic Claude 3.5 Sonnet API key | *(empty)* | Optional (defaults to fallback agent) |
| `ADMIN_TOKEN` | Secret token for freeze & supervisor review | `dev-secret-admin-token-min-32-chars-long-abc-123` | No |
| `DEMO_SESSION_TOKEN` | Token for local dashboard API access | `demo-session-token-allowance-hackathon-2026` | No |
| `PUBLIC_BASE_URL` | Base URL for PayPal return/cancel callbacks | `http://localhost:8000` | No |
| `PORT` | Web server listening port | `8000` | No |
| `DB_PATH` | SQLite database file location | `allowance.db` | No |

---

## 4. The 7 Non-Negotiable Financial Safety Invariants

Every operation within Allowance strictly adheres to [`.agents/rules/financial_safety_invariants.md`](.agents/rules/financial_safety_invariants.md):

1. **No Blind Capture Retries:** Once payment request transmission may have started, the outcome is `UNCERTAIN` until authoritative provider evidence resolves it. Never issue another capture `POST` merely due to a timeout, HTTP 429, or HTTP 5xx. Persist `MAY_HAVE_BEEN_SENT` before sending.
2. **Separate Workflow State from Financial Truth:** Workflow state describes purchase lifecycle progress, not the balance sheet. Financial movements are immutable and recorded in the double-entry general ledger. Late settlements are recorded via adjustments without rewriting terminal purchases.
3. **Preserve Financial Reservations and Budget Invariants:** Committed spend is calculated as:
   $$\text{Committed Spend} = \text{Captured Funds} + \text{Active Reservations} + \text{Unresolved Capture Commitments}$$
   All calculations use integer minor currency units (cents), never floating-point arithmetic.
4. **Atomic Admission and Queue Claims:** Capacity checks, concurrency limits, and job insertion execute within the same serialized database transaction (`BEGIN IMMEDIATE`). Queue deadlines are verified inside the atomic claim.
5. **Ownership and Concurrency Fencing:** Capture operations and reconciliation tasks have durable identities and explicit lease ownership via atomic CAS. Stale workers are locked out from committing late writes.
6. **AI and Payment Authority Separation:** AI agents are untrusted entities that may only propose purchases. The deterministic policy engine is the sole financial authority. Validate all prices, currencies, and merchants using server-side data.
7. **Evidence-Based Engineering:** Never mark a payment as captured without authoritative provider evidence. Capture parsing extracts the genuine nested capture ID (`purchase_units[0].payments.captures[0].id`).

---

## 5. Cross-Platform Commands & Developer Workflows

| Task | Windows (PowerShell) | macOS / Linux (Bash) |
|---|---|---|
| **Run Dev Server** | `$env:PYTHONPATH="." ; python scripts/dev_server.py` | `PYTHONPATH=. python3 scripts/dev_server.py` |
| **Run Tests (157)** | `$env:PYTHONPATH="." ; python -m unittest discover -s backend/tests -t . -p "test_*.py"` | `PYTHONPATH=. python3 -m unittest discover -s backend/tests -t . -p "test_*.py"` |
| **Reset Database** | `python scripts/seed_db.py --reset` | `python3 scripts/seed_db.py --reset` |
| **PayPal Smoke Test** | `python scripts/paypal_smoke.py` | `python3 scripts/paypal_smoke.py` |
| **Build Frontend** | `cd frontend ; npm run build ; cd ..` | `cd frontend && npm run build && cd ..` |
| **Run Docker** | `docker build -t allowance . ; docker run --rm -p 8000:8000 allowance` | `docker build -t allowance . && docker run --rm -p 8000:8000 allowance` |

---

## 6. Automated Testing (157 / 157 Passing)

Allowance features a 157-test automated verification suite covering unit isolation, adversarial race conditions, double-entry balancing, Claude agent function-calling, and multi-tenant security across both Python 3.11 and Python 3.14.

```text
.............................................................................................................................................................
----------------------------------------------------------------------
Ran 157 tests in 8.701s

OK
```

### Key Test Suites:
- `backend/tests/unit/test_double_entry_accounting.py`: Verifies Chart of Accounts, balanced debits/credits, rejection of unbalanced transactions with complete rollback, encumbrance liquidation, and SQLite immutability triggers.
- `backend/tests/integration/test_personal_edition_mvp.py`: Verifies Personal Edition limits ($100 budget, $30 threshold), supported merchant payee routing, auto-approvals, supervisor reviews, and over-budget refusal.
- `backend/tests/integration/test_multi_tenant_isolation.py`: Cross-tenant boundary enforcement, SHA-256 API key authentication, constant-time verification, and credential revocation.
- `backend/tests/unit/test_mcp_tools.py`: MCP tool definitions, server-side price validation, and prompt-injection immunity.
- `backend/tests/integration/test_adversarial_scenarios.py`: 53 fault-injected protocol conditions tested within the suite (tamper detection, lease takeovers, freeze races, and network fault injectors).

---

## 7. Real PayPal Sandbox Verification

To execute live against PayPal Sandbox developer credentials:

1. Create a REST API App in the [PayPal Developer Dashboard](https://developer.paypal.com/dashboard/applications/sandbox).
2. Configure credentials in your environment:
   ```bash
   export PAYPAL_MODE="sandbox"
   export PAYPAL_CLIENT_ID="<your_sandbox_client_id>"
   export PAYPAL_CLIENT_SECRET="<your_sandbox_client_secret>"
   # Optional: set your sandbox business facilitator email as the payee
   export PAYPAL_SANDBOX_PAYEE="<your_sandbox_business_email>"
   ```
3. Run the automated smoke script:
   ```bash
   python3 scripts/paypal_smoke.py
   ```
4. The smoke script creates an Orders v2 order, outputs the approval URL, awaits buyer authorization, executes the capture call, extracts the genuine nested capture ID (`purchase_units[0].payments.captures[0].id`), and verifies amount and currency match.
5. Review the sanitized provider capture receipt in [`docs/sandbox-verification.md`](docs/sandbox-verification.md).

---

## 8. Client Integration Models & Tools

### Option A: Enterprise REST API v1 (`/v1/*`)
External purchasing applications authenticate machine-to-machine using high-entropy scoped API keys (`Authorization: Bearer ak_test_...`):

- `POST /v1/quotes`: Generate a signed server quote with a 10-minute expiry and cart hash.
- `POST /v1/purchase-requests`: Submit a purchase proposal with a mandatory `Idempotency-Key` header.
- `GET /v1/purchase-requests/{id}`: Poll purchase status and tracking receipts.
- `GET /v1/allowance`: Inspect remaining daily allowance and velocity status.
- `POST /v1/freeze` / `POST /v1/unfreeze`: Programmatic emergency kill switch control.

### Option B: Model Context Protocol (MCP) Server
Connect Claude, Gemini, or custom LLM agents via our native MCP JSON-RPC 2.0 server ([`backend/app/mcp/server.py`](backend/app/mcp/server.py)).

**Exposed Bounded Tools:**
1. `search_products(query)`: Search authorized catalog items.
2. `get_product_quote(product_id, quantity)`: Retrieve a server-signed price quote.
3. `get_allowance()`: Check remaining agent spending allowance.
4. `request_purchase(quote_id)`: Submit a purchase proposal for authorization.
5. `get_purchase_status(purchase_id)`: Track progress of a submitted order.

*Crucially, `capture_payment` is completely omitted from the MCP toolset. Agents cannot initiate payments directly.*

### Option C: Postman Collection (Automated API Test Suite)
Import [`postman/allowance_v5_collection.json`](postman/allowance_v5_collection.json) and [`postman/allowance_environment.json`](postman/allowance_environment.json) into Postman to execute our automated API test suite covering Quotes, Idempotent Purchases, Human Approvals, and Emergency Kill Switch. *(Note: Postman is an API tool integration, not an official sponsor prize category).*

### Option D: Render Cloud Deployment (Blueprint Included)
Allowance includes an Infrastructure-as-Code blueprint [`render.yaml`](render.yaml) configured for Render's 100% free tier (no paid persistent disks required). Developers can connect their repository to Render and deploy in one click. *(First request after idle can take ~30-50s to wake up on the free tier).*

---

## 9. Formal Double-Entry Chart of Accounts

Allowance implements formal commitment accounting with balanced debits and credits:

| Account Number | Account Name | Type | Purpose |
| :--- | :--- | :--- | :--- |
| **`1000`** | Cash / Payment Settlement Gateway | `ASSET` | Realized settlement liquidity via payment gateway. |
| **`2100`** | Encumbered Spend Reservations | `LIABILITY` | Active encumbrances held against approved purchases. |
| **`3100`** | Authorized Spending Allocation | `EQUITY` | Daily spending authority allocated by the owner. |
| **`5100`** | Realized Merchant Purchases | `EXPENSE` | Authoritatively settled merchant expenditures. |
| **`5200`** | Encumbrance Commitments Contra | `EXPENSE` | Contra expense tracking active commitments. |
| **`5300`** | Payment Processing Fees | `EXPENSE` | Processing and interchange fees. |

### Deterministic Balanced Posting Rules:
- **Reservation Created:** `DR 5200` (Encumbrance) / `CR 2100` (Liability)
- **Reservation Released:** `DR 2100` / `CR 5200`
- **Capture Settled:** `DR 2100` + `DR 5100` / `CR 5200` + `CR 1000`
- **Refund / Reversal:** `DR 1000` / `CR 5100`
- **Late Capture:** `DR 5100` / `CR 1000`

> [!NOTE]
> **Global Hackathon Evaluation & Multi-Currency Architecture:**  
> For the PayPal AI Hackathon, demo catalog items and policy presets are configured in **USD ($)** to provide standard out-of-the-box evaluation with PayPal developer sandbox test stores. However, the underlying double-entry engine strictly enforces **independent balancing per currency** ($\sum \text{Debits} = \sum \text{Credits}$ separately for each currency code). All ledger lines and budget snapshots are indexed by `(agent_id, budget_day, currency)`, establishing direct architectural readiness for client-specific international currencies (EUR, GBP, JPY, INR, CAD) in Allowance v5.1.

---

## 10. Repository Structure

```
allowance/
├── .agents/
│   └── rules/
│       └── financial_safety_invariants.md  <-- Core architectural invariants (MANDATORY)
├── backend/
│   ├── app/
│   │   ├── agent/
│   │   │   ├── catalog.py                  <-- Authoritative product database & token scoring
│   │   │   ├── loop.py                     <-- Claude agent loop with deterministic fallback
│   │   │   └── tools.py                    <-- Sandboxed discovery tools (zero price-injection)
│   │   ├── api/
│   │   │   ├── v1/
│   │   │   │   └── routes.py               <-- REST API v1 endpoints with Bearer auth
│   │   │   ├── auth.py                     <-- Constant-time token verification & rate limits
│   │   │   ├── dto.py                      <-- Strict Pydantic input/output schemas
│   │   │   └── routes.py                   <-- Dashboard REST API & Kill Switch endpoints
│   │   ├── auth/
│   │   │   └── credentials.py              <-- SHA-256 API key management & permission scopes
│   │   ├── db/
│   │   │   ├── bootstrap.py                <-- Idempotent schema bootstrapper & profile seed
│   │   │   ├── connection.py               <-- SQLite WAL mode & transaction sessions
│   │   │   ├── double_entry.py             <-- Formal double-entry bookkeeping engine
│   │   │   ├── financial_journal.py        <-- Event-sourced financial facts & budget projection
│   │   │   ├── purchases.py                <-- Purchase lifecycle queries & state persistence
│   │   │   ├── quotes.py                   <-- Server-side quote generation & cart hashing
│   │   │   └── schema.sql                  <-- SQLite DDL: tables, indexes, immutability triggers
│   │   ├── mcp/
│   │   │   └── server.py                   <-- Model Context Protocol (MCP) JSON-RPC 2.0 server
│   │   ├── orchestrator/
│   │   │   ├── admission.py                <-- Concurrency limits & queue rate fencing
│   │   │   ├── capture_lifecycle.py        <-- Safe capture flow & nested capture ID parsing
│   │   │   ├── core.py                     <-- Checkpoints P1-P4 orchestration pipeline
│   │   │   ├── queue.py                    <-- Bounded worker pool & queue dispatch
│   │   │   ├── rechecks.py                 <-- Pure policy re-evaluation with self-exclusion
│   │   │   └── state_machine.py            <-- PurchaseState transitions & lease acquisition
│   │   ├── paypal/
│   │   │   ├── client.py                   <-- PayPal client factory (Mock / Sandbox / Live)
│   │   │   ├── exclusive_claim.py          <-- Atomic CAS lease claiming & fence protection
│   │   │   └── merchants.py                <-- Supported merchant directory & payee routing
│   │   └── runtime.py                      <-- Worker pool manager & lifespan lifecycle
│   └── tests/
│       ├── unit/                           <-- Isolation, double-entry, policy & agent tests
│       └── integration/                    <-- Concurrency, multi-tenant & sandbox tests
├── docs/
│   ├── devpost_submission.md               <-- Official Devpost submission text & compliance proof
│   ├── sandbox-verification.md             <-- Sanitized Gate 1 PayPal capture evidence
│   ├── submission_readiness_report.md      <-- Tasks T1–T15 submission audit & verification proof
│   └── video_demo_script.md                <-- Rehearsed 3-minute master cut video demonstration script
├── frontend/
│   ├── src/                                <-- React 18 + Tailwind CSS unified dashboard
│   ├── dist/                               <-- Pre-compiled production bundle (zero Node needed)
│   └── package.json                        <-- Frontend dependencies & scripts (v5.0.0)
├── postman/
│   ├── allowance_v5_collection.json        <-- Full Postman Collection v2.1 with automated tests
│   └── allowance_environment.json          <-- Environment variables for local & cloud runs
├── scripts/
│   ├── dev_server.py                       <-- Zero-dependency full-stack server (Python stdlib)
│   ├── paypal_smoke.py                     <-- PayPal connectivity smoke & evidence collector
│   └── seed_db.py                          <-- Database seeder with sample policies & --reset flag
├── .env.example                            <-- Environment configuration template
├── create_desktop_shortcut.bat             <-- Windows Desktop shortcut generator
├── Dockerfile                              <-- Production multi-stage Docker build
├── LICENSE                                 <-- Official MIT Open Source License
├── render.yaml                             <-- Render Cloud Deployment Blueprint (IaaS)
├── run.bat                                 <-- One-click Windows launch script
├── project_status_report.md                <-- Living engineering report & contributor guide
└── README.md                               <-- THIS FILE
```

---

## 11. Third-Party Dependencies & Licenses

| Package / Tool | Version | Purpose | License |
|---|---|---|---|
| **Python Standard Library** | 3.10+ | Core runtime (`http.server`, `sqlite3`, `urllib`, `json`, `hashlib`) | PSF License |
| **FastAPI** | `>=0.110.0` | ASGI REST API framework (production server option) | MIT |
| **Uvicorn** | `>=0.28.0` | High-performance ASGI web server | BSD-3-Clause |
| **Pydantic** | `>=2.6.0` | Schema validation and settings management | MIT |
| **HTTPX** | `>=0.27.0` | Async HTTP client for test suite | BSD-3-Clause |
| **Pytest** | `>=8.0.0` | Automated test runner | MIT |
| **Hypothesis** | `>=6.98.0` | Property-based adversarial testing | MPL-2.0 |
| **Anthropic** | `>=0.39.0` | Claude 3.5 Sonnet client for ShopMind agent | MIT |
| **React** | `^18.2.0` | User interface library | MIT |
| **Tailwind CSS** | `^3.4.1` | Utility-first styling framework | MIT |
| **Lucide React** | `^0.344.0` | Dashboard icons | ISC |
| **Vite** | `^5.1.4` | Frontend asset bundler | MIT |

---

## 12. What Changed During the Submission Period (Oct 1 – Nov 12, 2026)

The Allowance project was **created entirely during the official PayPal AI Hackathon submission period** (initiated November 6, 2026). Below is the dated development log of major architectural milestones:

- **2026-11-06 (`950063a`):** Initial project release. Implemented Invariants 1–7 baseline, SQLite WAL schema, and deterministic PayPilot policy engine (Rules R0–R6).
- **2026-11-07 (`a933248`, `a1a118e`):** Implemented runtime worker pool with serialized queue claims, bounded agent loop, PayPal workflow routes, and administrative freeze/unfreeze endpoints.
- **2026-11-07 (`e153a5c`):** Expanded automated test suite to 114 tests covering 53 adversarial fault-injection scenarios.
- **2026-11-08 (`a6834ce`, `83439f3`):** Verified genuine nested capture ID extraction (`purchase_units[0].payments.captures[0].id`), recorded Gate 1 capture verification, and enforced 2-step human review flow.
- **2026-11-08 (`af74ba3`):** Implemented formal double-entry general ledger (Chart of Accounts `1000`-`5300`), immutable SQLite triggers, and Personal Edition MVP profile with 137 passing tests.
- **2026-11-08 (`67a141f`):** Built Model Context Protocol (MCP) JSON-RPC 2.0 server (`backend/app/mcp/server.py`) and Enterprise REST API v1 (`/v1/*`) with Bearer API key authentication.
- **2026-11-08 (`b51f233`, `78faf37`):** Authored Postman Collection v2.1 with automated JavaScript assertions, free-tier compatible `render.yaml` blueprint, multi-stage `Dockerfile`, and `run.bat` one-click Windows launcher.
- **2026-11-09 (`c2239e5`, `7c71853`, `354ab59`):** Rebuilt and committed production frontend bundle (`frontend/dist/`), added `--reset` CLI flag for repeatable demo resets, added dynamic payee routing overrides, and completed submission readiness verification.

---

## 13. Honest Disclosures & Technical Limitations

- **Simulated & Sandbox Funds Only:** All transactions depicted, recorded, and tested execute exclusively against mock simulation or the PayPal Developer Sandbox environment. Zero real-world money is ever debited or transferred.
- **Hackathon Currency Focus vs. Multi-Currency Engine:** For the global hackathon evaluation, catalog prices and default policy thresholds are denominated in USD ($) for standard compatibility with PayPal developer sandbox tools. However, the double-entry accounting engine already indexes budgets and enforces balance constraints independently per currency (`USD`, `EUR`, `GBP`, `CAD`, `AUD`, `JPY`, `INR`), providing turnkey architectural support for international client deployments in Allowance v5.1.
- **In-Flight Transport Recall:** Once HTTP bytes are accepted by PayPal, an emergency freeze cannot recall an in-flight network packet if PayPal settles it before the freeze is processed.
- **Unresolved Capture Encumbrance:** If a capture attempt enters an ambiguous transport state (e.g. read timeout), the budget reservation remains locked and committed until either authoritative reconciliation establishes settlement or the budget day expires.
- **Adversarial Scenarios vs. Test Count:** The phrase "53 adversarial scenarios" refers to the comprehensive fault-injection and protocol stress-testing matrix evaluated within the **151 automated unit and integration tests**.
- **Single-Process Prototype Constraint:** The prototype uses SQLite in WAL mode with `BEGIN IMMEDIATE` serialization. For production multi-tenant scale, see the PostgreSQL migration roadmap in the [Project Status Report](project_status_report.md).
- **Buyer Consent vs. Zero-Click Charging:** Personal purchases require 2-step approval (supervisor check + PayPal buyer consent). True zero-click autonomous recurring payments require PayPal Vaulting / Billing Agreements, which require dedicated partner approval.

---

## 14. License & Acknowledgments

This project is open-source under the [MIT License](LICENSE).  
Built for the **PayPal AI Hackathon 2026** by Mohd Ashfaq and contributors.
