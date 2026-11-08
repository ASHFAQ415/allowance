# ALLOWANCE (v4.2.2) — Project Status Report & Collaborator Guide

> **"Give your AI agent an allowance, not your wallet."**  
> **Repository:** [https://github.com/ASHFAQ415/allowance](https://github.com/ASHFAQ415/allowance)  
> **Date:** November 2026 (Updated post-PR #1 merge)  
> **Target Audience:** Collaborators, Forkers, Reviewers, and AI Coding Agents (Cursor, Copilot, Antigravity, Claude Code)

---

## 1. Executive Summary & Platform Identity

**ALLOWANCE** is an autonomous AI agent spending control platform built for the **PayPal AI Hackathon 2026**.

Modern LLM agents are increasingly tasked with autonomous purchasing, booking, and procurement. However, granting an AI direct access to credit cards or raw payment APIs presents unacceptable financial risks: hallucinations, prompt injection, blind retry loops, merchant spoofing, and runaway budget exhaustion.

ALLOWANCE solves this by enforcing a strict separation of powers:
- **ShopMind:** An unprivileged, sandboxed product discovery agent that proposes purchases based on catalog items. It has **zero** access to payment credentials, cannot trigger capture calls, and cannot alter policies (enforced at both runtime and AST compile-time).
- **PayPilot:** A deterministic, mathematical financial policy engine that rigorously validates all purchase requests against corporate spending rules (Rules R0–R6, Checkpoints P1–P4).
- **Payment Safety Layer:** A hardened PayPal Orders v2 integration governed by exclusive-claim state machines, append-only financial journal ledgers, idempotency markers (`MAY_HAVE_BEEN_SENT`), and an absolute prohibition against blind capture retries.
- **Unified Control Dashboard:** A real-time React 18 + Tailwind CSS interface providing human-in-the-loop oversight, an immediate Emergency Kill Switch, real-time audit logs, purchase progress tracking, and double-entry financial journal streams.

---

## 2. Architecture Map & Codebase Structure

```
allowance/
├── .agents/
│   └── rules/
│       └── financial_safety_invariants.md  <-- Core architectural invariants (MANDATORY)
├── backend/
│   ├── app/
│   │   ├── agent/
│   │   │   ├── catalog.py                  <-- Catalog database, search & token scoring
│   │   │   ├── loop.py                     <-- Claude-powered tool loop with deterministic fallback
│   │   │   └── tools.py                    <-- Propose purchase & search tools (zero price-injection)
│   │   ├── db/
│   │   │   ├── bootstrap.py                <-- Idempotent schema bootstrapper & default policies
│   │   │   ├── connection.py               <-- SQLite WAL mode & atomic transactions
│   │   │   ├── financial_journal.py        <-- Append-only double-entry financial ledger
│   │   │   ├── purchases.py                <-- Purchase lifecycle persistence & queries
│   │   │   └── schema.sql                  <-- DB DDL: tables, indexes, immutability triggers
│   │   ├── orchestrator/
│   │   │   ├── admission.py                <-- Concurrency limits & queue rate fencing
│   │   │   ├── capture_lifecycle.py        <-- Safe capture flow, tamper verification & read-only recon
│   │   │   ├── core.py                     <-- P1-P4 orchestration pipeline & buyer return handling
│   │   │   ├── queue.py                    <-- Bounded worker pool & queue dispatch
│   │   │   ├── rechecks.py                 <-- Checkpoints P2/P3/P4 pure policy re-eval & self-exclusion
│   │   │   └── state_machine.py            <-- PurchaseState transitions & lease acquisition
│   │   ├── paypal/
│   │   │   ├── client.py                   <-- PayPal client factory (Mock / Sandbox / Live)
│   │   │   ├── exclusive_claim.py          <-- Atomic CAS lease claiming & fence protection
│   │   │   └── mock_service.py             <-- Authoritative PayPal mock with fault injectors
│   │   ├── api/
│   │   │   ├── auth.py                     <-- Constant-time token verification & rate limits
│   │   │   ├── dto.py                      <-- Strict Pydantic input/output schemas
│   │   │   └── routes.py                   <-- REST endpoints (agent, policies, purchases, kill switch)
│   │   └── runtime.py                      <-- App runtime worker pool & FastAPI lifespan wiring
│   └── tests/
│       ├── unit/
│       │   ├── test_agent_isolation.py     <-- AST boundary proof (no imports of db/paypal/secrets)
│       │   ├── test_agent_loop.py          <-- Claude & fallback agent execution
│       │   └── test_policy_engine.py       <-- Rules R0 through R6 edge cases
│       └── integration/
│           ├── test_adversarial_concurrency.py <-- Race conditions, double capture, journal integrity
│           ├── test_adversarial_scenarios.py   <-- Freeze races, tamper detection, lease takeover
│           ├── test_checkpoints.py             <-- P1–P4 deterministic enforcement
│           ├── test_engine_state_machine.py    <-- Monotonic state transitions & invalid edges
│           ├── test_exclusive_claim.py         <-- Lock takeover & lease expiration fencing
│           ├── test_financial_journal.py       <-- Invariants 2 & 3 append-only math tests
│           ├── test_paypal_contract.py         <-- Protocol contracts: 429, 500, write errors, timeouts
│           ├── test_paypal_reconciliation.py   <-- Read-only reconciliation under uncertainty
│           ├── test_runtime_pipeline.py        <-- Worker queue saturation & idempotent replays
│           ├── test_transaction_atomicity.py   <-- SQLite WAL transactional rollbacks
│           └── test_workflow_step3b.py         <-- Full end-to-end buyer return & policy update
├── frontend/
│   ├── src/
│   │   ├── components/                     <-- Modular UI cards, ledger tables, order cards
│   │   ├── App.tsx                         <-- Main unified dashboard
│   │   ├── index.css                       <-- Tailwind styling & custom scrollbars
│   │   └── main.tsx                        <-- React DOM entry point
│   ├── package.json                        <-- React 18, Lucide React, Tailwind CSS
│   └── vite.config.ts                      <-- Vite dev & production bundler configuration
├── scripts/
│   ├── dev_server.py                       <-- Zero-dependency live development server
│   ├── paypal_smoke.py                     <-- PayPal API connectivity validator
│   └── seed_db.py                          <-- Database seeder with sample policies & items
├── allowance_v4_2_2_master_plan.md          <-- Master architectural specification
├── learning_proposal.md                    <-- Self-directed learning & agent constraints
└── project_status_report.md                <-- THIS FILE: Living status and contributor guide
```

---

## 3. What Was Recently Changed & Solved (Changelog)

### Update 1: PR #1 Merge (`impl/p0-runtime-wiring`)
- **Contributor:** Lohith Padavala (`LohithP07`), merged into `main`.
- **Runtime Worker Pool & Lifespan Wiring (`backend/app/runtime.py`):**
  - Integrated `WorkerPoolManager` into FastAPI's `lifespan(app)` context manager to start workers on boot and drain gracefully on shutdown.
  - Workers run claimed jobs in isolated worker threads with dedicated, short-lived SQLite database connections.
- **Pre-Order (P3) and Pre-Capture (P4) Policy Re-evaluation (`backend/app/orchestrator/rechecks.py`):**
  - Implemented pure policy re-checks before creating PayPal orders and before capture POST.
  - Implemented Invariant 3 self-exclusion: active purchase reservation is excluded exactly once during re-evaluation so an approved purchase never denies itself.
- **PayPal Client Mode Selection (`backend/app/paypal/client.py`):**
  - Added factory `get_paypal_mode()` and `get_paypal_client()` supporting `PAYPAL_MODE=mock|sandbox|live`.
  - Added extended fault injectors for all 53 adversarial test matrix scenarios (tamper simulation, mid-POST TCP write error, capture detail mismatch).
- **AST Agent Sandbox Boundary Proof (`backend/tests/unit/test_agent_isolation.py`):**
  - Proves statically via Python AST inspection that `backend/app/agent/` modules cannot import `db`, `orchestrator`, `paypal`, or access secret environment variables.
- **Review Bug Fix (`3a37591`):**
  - Caught and fixed `TypeError: HTTPException.__init__() got an unexpected keyword argument 'headers'` in standard library fallback stub in `backend/app/api/routes.py`.
- **Full Test Suite Expansion:**
  - Expanded test matrix from 72 to **114 automated tests** (100% passing).

### Update 2: Search Fallback & Price Modifier Bug ($45 vs. $6)
- **Problem:** When sending prompts such as `"Find trail running shoes under $45 from approved store"`, the search agent returned `coffee_paper_filters` for `$6.00` instead of the expected `Trail Running Shoes` (`$45.00`).
- **Root Cause:** In `backend/app/agent/catalog.py`, `search_catalog_data` performed a rigid exact substring match (`if query.lower() in item.name.lower()`). Because the full conversational sentence was passed as the search query, no items matched by substring, triggering the fallback heuristic to pick the cheapest item in the catalog ($6 coffee filters).
- **Solution:** Upgraded search in `backend/app/agent/catalog.py` to extract price constraints (e.g. `under $45` $\rightarrow \le 4500$ cents) and score items using tokenized keyword overlap (stripping conversational filler words). Now accurately returns `Trail Running Shoes` at `$45.00`.

### Update 3: Emergency Freeze (Kill Switch) UI Visibility
- **Problem:** When the Kill Switch (Rule R0) was activated and a user submitted a purchase request, the resulting blocked purchase was invisible in the UI.
- **Root Cause:** The dashboard's orders section was hardcoded to filter exclusively for `status === 'PENDING_APPROVAL'`, causing `BLOCKED` and `CAPTURED` orders to disappear from the card feed.
- **Solution:** 
  1. Added a dedicated **Active Purchases & Execution Orders** card panel in `frontend/src/App.tsx` displaying real-time purchase states (`CAPTURED`, `PENDING_APPROVAL`, `BLOCKED`).
  2. Enriched blocked cards with explicit, human-readable rejection reasons (e.g., `Emergency spending freeze is ACTIVE (Rule R0)` or `Merchant 'DarkWeb Store' is not in allowlist`).
  3. Added an animated full-width **Emergency Kill Switch Banner** across the top of the interface when the freeze is active, with an optimistic toggle for instant feedback.

### Update 4: Interactive One-Click Demo Scenarios
- Added 4 quick-select pill buttons directly under the chat input in `frontend/src/App.tsx`:
  - 🏃 **Trail Shoes ($45)** — Triggers an autonomous auto-approval and immediate PayPal capture.
  - 👟 **Road Flats ($90)** — Triggers Rule R2 escalation for human supervisor review (exceeds $50 auto-limit).
  - ⚠️ **DarkWeb Store ($15)** — Demonstrates deterministic merchant block via Rule R4.
  - ☕ **2x Coffee Beans ($44)** — Verifies multi-quantity cart arithmetic and budget compliance.

### Update 5: Gate 1 Payment Correctness & Nested Capture ID
- **Nested Capture ID Extraction (`backend/app/orchestrator/capture_lifecycle.py`):**
  - Corrected capture ID parsing: PayPal Orders v2 provides the actual capture ID within nested `purchase_units[0].payments.captures[0].id` rather than the top-level order ID.
  - Added pre-settlement capture amount and currency code verification against purchase total.
- **Search Fallback Hardening (`backend/app/agent/loop.py`):**
  - Eliminated silent fallback to cheapest catalog item. Any search query that fails relevance criteria refuses cleanly with `no_matching_item`.
  - Added regression test `test_eval_16_unmatched_query_refuses_without_cheap_fallback`.
- **Two-Step Approval UX (`frontend/src/App.tsx`):**
  - Made the two approvals explicitly distinct: Step 1: PayPilot Supervisor Approval (P2), followed by Step 2: PayPal Buyer Consent.
- **Genuine Smoke Check & Evidence Recording (`scripts/paypal_smoke.py`):**
  - Performs full order create, buyer consent, capture, amount/currency check, and saves sanitized audit evidence to `docs/sandbox-verification.md`.

### Update 6: Client Integration Architecture (REST API v1, MCP Server & Multi-Tenant Foundations)
- **Option A: Scoped Versioned REST API v1 (`backend/app/api/v1/`):**
  - Implemented `/v1/quotes` (trusted server-side catalog quotes with 10-minute expiry and cart hash).
  - Implemented `/v1/purchase-requests` with header-only `Idempotency-Key` and strict client identity resolution from authenticated API keys (never trusted from request body).
  - Implemented `/v1/allowance`, `/v1/purchase-requests/{id}/approve`, and `/v1/freeze` / `/v1/unfreeze`.
- **Option B: Model Context Protocol (MCP) Server (`backend/app/mcp/server.py`):**
  - Standalone MCP JSON-RPC 2.0 server enabling external AI models (Claude Desktop, Cursor, Gemini Live) to interact safely through 5 bounded tools: `search_products`, `get_product_quote`, `get_allowance`, `request_purchase`, `get_purchase_status`.
  - Invariant 6 enforced: `capture_payment`, `approve_purchase`, and `modify_policy` are strictly omitted from the MCP tool catalog.
- **Multi-Tenant Foundation & Scoped API Credentials (`backend/app/auth/credentials.py`):**
  - Added multi-tenant entities (`tenants`, `users`, `tenant_memberships`, `agent_credentials`, `quotes`).
  - Implemented SHA-256 hashed API key management (`ak_live_...`) with constant-time verification, role scopes (`propose`, `read`, `admin`), and instant revocation.
  - Added comprehensive cross-tenant isolation and MCP tool contract test suites (**126 / 126 tests passing**).

---

## 4. Current System & Verification Status

| Component | Status | Verification Detail |
|---|---|---|
| **Deterministic Policy Engine (R0–R6)** | ✅ Complete | Verified with 100% test coverage for kill switch, limits, whitelist, and budget checks |
| **Checkpoints P1, P2, P3, P4** | ✅ Complete | Enforces state gating prior to provider calls and post-approval with self-exclusion |
| **PayPal Orders v2 Client (Mock/Sandbox/Live)** | ✅ Complete | Factory selection via `PAYPAL_MODE`; nested capture ID extraction; idempotent recovery |
| **Append-Only Financial Journal** | ✅ Complete | Zero floating-point math; integer cents; double-entry journal balance tracking |
| **Admission Queue & Concurrency Fencing** | ✅ Complete | Atomic transaction claim; queue expiry enforcement; CAS leasing |
| **Scoped REST API v1 & Auth** | ✅ Complete | `/v1/quotes`, `/v1/purchase-requests`, `/v1/allowance` with API key auth |
| **Model Context Protocol (MCP) Server** | ✅ Complete | 5 bounded tools for external AI models; JSON-RPC 2.0 stdio & programmatic dispatch |
| **AST Agent Sandbox Boundary** | ✅ Complete | Verified compile-time isolation preventing DB, payment API, or credential access |
| **Frontend Dashboard (React 18)** | ✅ Complete | Single-page real-time dashboard with 2-step approval UX; compiled with Vite |
| **Automated Test Suite** | ✅ Complete | **126 / 126 tests passing** across 16 test suites in ~6.8 seconds |
| **Gate 1 Evidence Receipt** | ✅ Generated | Documented in `docs/sandbox-verification.md` |
| **Local Standalone Dev Server** | ✅ Active | Running on `http://localhost:8000` via `scripts/dev_server.py` |

---

## 5. Quickstart: How to Run and Test

The project is designed to be completely zero-friction. You do not need external microservices or Docker to run the core platform locally.

### Prerequisites
- Python 3.10+ (with virtualenv)
- Node.js 18+ and npm (for frontend modifications)

### Step 1: Run the Automated Test Suite
From the repository root, run:
```bash
# Set PYTHONPATH to project root and run all 114 tests
python -m unittest discover -s backend/tests -t . -p "test_*.py"
```
*Expected output:* `Ran 114 tests in ~6.8s ... OK`.

### Step 2: Start the Live Dev Server
The standalone server in `scripts/dev_server.py` serves both the backend JSON REST API and the production frontend assets:
```bash
python scripts/dev_server.py
```
Open your browser to:
👉 **`http://localhost:8000`**

### Step 3: Run the Interactive Demo Scenarios
1. **Auto-Approved Purchase:** Click `🏃 Trail Shoes ($45)` and click **Send**. The agent parses the request, validates it against Policy R1 & R2, auto-approves, and immediately captures the order via PayPal.
2. **Escalated Review Purchase:** Click `👟 Road Flats ($90)` and click **Send**. The policy engine detects that $90 exceeds the $50 auto-approval threshold and escalates to `PENDING_APPROVAL`. In the UI, click **Approve** to execute the capture.
3. **Merchant Policy Block:** Click `⚠️ DarkWeb Store ($15)` and click **Send**. The request is rejected immediately with `Merchant 'DarkWeb Store' is not in allowlist (Rule R4)`.
4. **Emergency Spending Freeze:** Toggle the **Emergency Spending Freeze** switch in the top banner. Submit any purchase. It will be rejected immediately with `Rule R0: Emergency spending freeze is ACTIVE`.

---

## 6. The 7 Non-Negotiable Financial Safety Invariants

Every collaborator, contributor, and AI assistant working on this codebase **must strictly uphold these 7 invariants**. Do not weaken, bypass, or reinterpret them.

1. **No Blind Capture Retries:** Once payment request transmission may have started, the outcome is `UNCERTAIN` until authoritative provider evidence resolves it. Never issue another capture `POST` merely due to a timeout, HTTP 429, or HTTP 5xx. Persist `MAY_HAVE_BEEN_SENT` before sending.
2. **Separate Workflow State from Financial Truth:** Workflow state describes purchase progress, not the balance sheet. Financial movements must be recorded in the append-only `financial_journal` table with unique source-event keys.
3. **Preserve Financial Reservations and Budget Invariants:** Committed spend = Captured funds + Active reservations + Unresolved capture commitments. All arithmetic must use integer minor units (cents), never floating-point numbers.
4. **Atomic Admission and Queue Claims:** Concurrency checks, capacity checks, and job insertion must execute within the same serialized database transaction. Reject excess work before inserting records.
5. **Ownership and Concurrency Fencing:** Capture and reconciliation operations must have durable identities and explicit lease ownership via atomic CAS. Stale owners must be locked out from committing results.
6. **AI and Payment Authority Separation:** AI agents may propose products and carts only. AI-generated text, tool arguments, and external content are untrusted. The deterministic policy engine is the sole financial authority.
7. **Evidence-Based Engineering:** Never mark a payment as captured without authoritative provider evidence. Regression tests must accompany every fix.

---

## 7. Contributor & IDE Directive: What to Do Next

If you or your AI IDE (Cursor, Copilot, Antigravity) are extending this repository, here is the prioritized development backlog:

### Completed Milestones
- [x] **P0 Runtime Wiring & Worker Pool:** Claimed agent runs processed asynchronously via `WorkerPoolManager` and FastAPI lifespan.
- [x] **Safe Capture Protocol & Concurrency Fencing:** Atomic CAS capture claims, `MAY_HAVE_BEEN_SENT` marker, read-only reconciliation.
- [x] **Full Adversarial Test Matrix:** 126 automated unit and integration tests covering tamper detection, freeze races, lease takeovers, and network failures.
- [x] **PayPal Mode Selection & Nested Capture Parsing:** Factory supporting `PAYPAL_MODE=mock|sandbox|live` with verified Orders v2 nested capture ID extraction.
- [x] **Enterprise REST API v1 (`/v1/*`):** Standardized external purchase requests, trusted quote signing (`/v1/quotes`), daily allowance checks, and emergency freeze controls with SHA-256 hashed API key authentication.
- [x] **Model Context Protocol (MCP) Server:** Native MCP JSON-RPC 2.0 server (`backend/app/mcp/server.py`) with 5 safe tools (`search_products`, `get_product_quote`, `get_allowance`, `request_purchase`, `get_purchase_status`), eliminating raw payment credentials from client AI agents.
- [x] **Multi-Tenant Foundation & Credentials:** Segregated tenant databases with cryptographic API keys (`ak_live_...`), constant-time validation (`hmac.compare_digest`), and granular permissions (`propose`, `read`, `admin`).

### Remaining Milestones

#### Task 1: Live PayPal Sandbox Execution & Receipt Verification
- **Status:** Architecture ready (`PAYPAL_MODE=sandbox`).
- **Next Step:** Run `scripts/paypal_smoke.py` against live PayPal Sandbox developer credentials (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`), execute a real sandbox order creation & capture, and save the signed provider response in `docs/sandbox-verification.md`.

#### Task 2: Automated Playwright E2E UI Tests
- **Status:** Manual UI verified.
- **Next Step:** Implement headless browser tests (`e2e/test_dashboard.spec.ts`) using Playwright to automatically verify the 4 interactive demo flows (Auto-Approve, Human Escalation, Merchant Block, Kill Switch) against the live dev server.

#### Task 3: Multi-Agent Allowance Profiles
- **Current State:** Single default agent policy configuration.
- **Next Step:** Add database support for multiple distinct agents (e.g. `ProcurementAgent`, `TravelAgent`, `CloudOpsAgent`), each with independent daily/monthly allowances, supervisor lists, and category permissions.

#### Task 4: Webhook & Mobile Push Notifications for Human Approvals
- **Current State:** Supervisor approvals are handled synchronously through the React dashboard.
- **Next Step:** Add a webhook or notification dispatcher (Slack/Discord webhook, email, or SMS) when a purchase transitions to `PENDING_APPROVAL`, with secure HMAC-signed one-time approval links.

#### Task 5: Containerized Cloud Deployment
- **Current State:** Local standalone server (`scripts/dev_server.py`).
- **Next Step:** Polish `Dockerfile` and `docker-compose.yml` to bundle a production multi-stage build (Node Vite build + Python FastAPI/Uvicorn backend) for zero-configuration cloud deployment on Railway, Render, or Google Cloud Run.

---

## 8. Summary for AI Assistants

When modifying this repository:
- Run tests before and after your changes: `python -m unittest discover -s backend/tests -t . -p "test_*.py"`.
- Never modify `backend/app/orchestrator/capture_lifecycle.py` or `backend/app/orchestrator/rechecks.py` without verifying that concurrency invariants hold.
- Always preserve integer cents for all money representations.
- Reference `.agents/rules/financial_safety_invariants.md` before making architectural decisions.
