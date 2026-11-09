# ALLOWANCE (v5.0) — Project Status Report & Collaborator Guide

> **"Give your AI agent an allowance, not your wallet."**  
> **Repository:** [https://github.com/ASHFAQ415/allowance](https://github.com/ASHFAQ415/allowance)  
> **Date:** November 2026 (Updated to Allowance v5.0)  
> **Target Audience:** Collaborators, Forkers, Reviewers, Hackathon Judges, and AI Coding Agents (Cursor, Copilot, Antigravity, Claude Code)

---

## 1. Executive Summary & Platform Identity

**ALLOWANCE** is an autonomous AI agent spending control platform built for the **PayPal AI Hackathon 2026**.

Modern LLM agents are increasingly tasked with autonomous purchasing, booking, and procurement. However, granting an AI direct access to credit cards or raw payment APIs presents unacceptable financial risks: hallucinations, prompt injection, blind retry loops, merchant spoofing, and runaway budget exhaustion.

ALLOWANCE solves this by enforcing a strict separation of powers:
- **ShopMind:** An unprivileged, sandboxed product discovery agent that proposes purchases based on catalog items. It has **zero** access to payment credentials, cannot trigger capture calls, and cannot alter policies (enforced at both runtime and AST compile-time).
- **PayPilot:** A deterministic, mathematical financial policy engine that rigorously validates all purchase requests against corporate spending rules (Rules R0–R6, Checkpoints P1–P4).
- **Formal Double-Entry Bookkeeping:** An immutable general ledger enforcing commitment accounting across 6 standard accounts with the strict database-level mathematical invariant $\sum \text{Debits} = \sum \text{Credits}$ per currency.
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
│   │   ├── api/
│   │   │   ├── v1/
│   │   │   │   └── routes.py               <-- Enterprise REST API v1 endpoints with Bearer auth
│   │   │   ├── auth.py                     <-- Constant-time token verification & rate limits
│   │   │   ├── dto.py                      <-- Strict Pydantic input/output schemas
│   │   │   └── routes.py                   <-- Dashboard REST API & Kill Switch endpoints
│   │   ├── auth/
│   │   │   └── credentials.py              <-- SHA-256 API key management & permission scopes
│   │   ├── db/
│   │   │   ├── bootstrap.py                <-- Idempotent schema bootstrapper & personal/demo seed
│   │   │   ├── connection.py               <-- SQLite WAL mode & atomic transactions
│   │   │   ├── double_entry.py             <-- Formal double-entry bookkeeping engine & GL
│   │   │   ├── financial_journal.py        <-- Event-sourced financial facts & budget projection
│   │   │   ├── purchases.py                <-- Purchase lifecycle persistence & queries
│   │   │   ├── quotes.py                   <-- Server-side quote generation & cart hashing
│   │   │   └── schema.sql                  <-- DB DDL: tables, indexes, immutability triggers
│   │   ├── mcp/
│   │   │   └── server.py                   <-- Model Context Protocol (MCP) JSON-RPC 2.0 server
│   │   ├── orchestrator/
│   │   │   ├── admission.py                <-- Concurrency limits & queue rate fencing
│   │   │   ├── capture_lifecycle.py        <-- Safe capture flow, nested capture extraction & recon
│   │   │   ├── core.py                     <-- P1-P4 orchestration pipeline & buyer return handling
│   │   │   ├── queue.py                    <-- Bounded worker pool & queue dispatch
│   │   │   ├── rechecks.py                 <-- Checkpoints P2/P3/P4 pure policy re-eval & self-exclusion
│   │   │   └── state_machine.py            <-- PurchaseState transitions & lease acquisition
│   │   ├── paypal/
│   │   │   ├── client.py                   <-- PayPal client factory (Mock / Sandbox / Live)
│   │   │   ├── exclusive_claim.py          <-- Atomic CAS lease claiming & fence protection
│   │   │   └── merchants.py                <-- Supported merchant directory & payee routing
│   │   └── runtime.py                      <-- App runtime worker pool & FastAPI lifespan wiring
│   └── tests/
│       ├── unit/
│       │   ├── test_agent_isolation.py     <-- AST boundary proof (no imports of db/paypal/secrets)
│       │   ├── test_agent_loop.py          <-- Claude & fallback agent execution
│       │   ├── test_double_entry_accounting.py <-- Balanced debits/credits & immutability triggers
│       │   ├── test_mcp_tools.py           <-- Safe MCP tools & prompt-injection defense
│       │   └── test_policy_engine.py       <-- Rules R0 through R6 edge cases
│       └── integration/
│           ├── test_adversarial_concurrency.py <-- Race conditions, double capture, journal integrity
│           ├── test_adversarial_scenarios.py   <-- Freeze races, tamper detection, lease takeover
│           ├── test_checkpoints.py             <-- P1–P4 deterministic enforcement
│           ├── test_engine_state_machine.py    <-- Monotonic state transitions & invalid edges
│           ├── test_exclusive_claim.py         <-- Lock takeover & lease expiration fencing
│           ├── test_financial_journal.py       <-- Invariants 2 & 3 append-only math tests
│           ├── test_multi_tenant_isolation.py  <-- Tenant boundaries & cryptographic key security
│           ├── test_paypal_contract.py         <-- Protocol contracts: 429, 500, write errors, timeouts
│           ├── test_paypal_reconciliation.py   <-- Read-only reconciliation under uncertainty
│           ├── test_personal_edition_mvp.py    <-- Personal user loop, payee routing & budget checks
│           ├── test_runtime_pipeline.py        <-- Worker queue saturation & idempotent replays
│           ├── test_transaction_atomicity.py   <-- SQLite WAL transactional rollbacks
│           └── test_workflow_step3b.py         <-- Full end-to-end buyer return & policy update
├── docs/
│   ├── devpost_submission.md               <-- Official Devpost submission text & compliance proof
│   ├── sandbox-verification.md             <-- Sanitized Gate 1 PayPal capture evidence
│   ├── submission_readiness_report.md      <-- Tasks T1–T15 submission audit & verification proof
│   └── video_demo_script.md                <-- Rehearsed 3-minute master cut video demonstration script
├── frontend/
│   ├── src/
│   │   ├── components/                     <-- Modular UI cards, ledger tables, order cards
│   │   ├── App.tsx                         <-- Main unified dashboard
│   │   ├── index.css                       <-- Tailwind styling & custom scrollbars
│   │   └── main.tsx                        <-- React DOM entry point
│   ├── dist/                               <-- Pre-compiled production bundle (committed; zero Node needed)
│   ├── package.json                        <-- React 18, Lucide React, Tailwind CSS (v5.0.0)
│   └── vite.config.ts                      <-- Vite dev & production bundler configuration
├── postman/
│   ├── allowance_v5_collection.json        <-- Full Postman Collection v2.1 with automated tests
│   └── allowance_environment.json          <-- Environment variables for local & cloud runs
├── scripts/
│   ├── dev_server.py                       <-- Zero-dependency live development server (Python stdlib)
│   ├── paypal_smoke.py                     <-- PayPal API connectivity validator & evidence logger
│   └── seed_db.py                          <-- Database seeder with sample policies & --reset flag
├── .env.example                            <-- Environment template with safe default configuration
├── create_desktop_shortcut.bat             <-- Windows Desktop shortcut generator
├── Dockerfile                              <-- Production multi-stage Docker build
├── LICENSE                                 <-- Official MIT Open Source License
├── render.yaml                             <-- Render Cloud Deployment Blueprint (IaaS, 100% free-tier)
├── run.bat                                 <-- Windows 1-click launch script
├── project_status_report.md                <-- THIS FILE: Living status and contributor guide
└── README.md                               <-- Master documentation & quick start
```

---

## 3. What Was Recently Changed & Solved (Changelog)

### Update 1: PR #1 Merge (`impl/p0-runtime-wiring`)
- **Runtime Worker Pool & Lifespan Wiring (`backend/app/runtime.py`):**
  - Integrated `WorkerPoolManager` into FastAPI's `lifespan(app)` context manager to start workers on boot and drain gracefully on shutdown.
  - Workers run claimed jobs in isolated worker threads with dedicated SQLite database connections.
- **Pre-Order (P3) and Pre-Capture (P4) Policy Re-evaluation (`backend/app/orchestrator/rechecks.py`):**
  - Implemented pure policy re-checks before creating PayPal orders and before capture POST.
  - Implemented Invariant 3 self-exclusion: active purchase reservation is excluded exactly once during re-evaluation so an approved purchase never denies itself.
- **PayPal Client Mode Selection (`backend/app/paypal/client.py`):**
  - Added factory `get_paypal_mode()` and `get_paypal_client()` supporting `PAYPAL_MODE=mock|sandbox|live`.
  - Added extended fault injectors for 53 protocol failure conditions tested within the test suite.

### Update 2: Search Fallback & Price Modifier Bug ($45 vs. $6)
- **Problem:** Sent prompts such as `"Find trail running shoes under $45 from approved store"`, the agent returned `coffee_paper_filters` for `$6.00`.
- **Root Cause:** In `catalog.py`, `search_catalog_data` performed an exact substring match. Full conversational sentences failed exact match, falling back to the cheapest catalog item.
- **Solution:** Upgraded search in `catalog.py` to extract price constraints (`under $45` $\rightarrow \le 4500$ cents) and score items using tokenized keyword overlap. Accurately returns `Trail Running Shoes` at `$45.00`.

### Update 3: Emergency Freeze (Kill Switch) UI Visibility
- Dedicated **Active Purchases & Execution Orders** panel in `frontend/src/App.tsx` displaying real-time purchase states (`CAPTURED`, `PENDING_APPROVAL`, `BLOCKED`).
- Full-width animated **Emergency Kill Switch Banner** across the top of the interface when the freeze is active.

### Update 4: Interactive One-Click Demo Scenarios
- Added 4 quick-select pill buttons directly under the chat input in `frontend/src/App.tsx` matching the core hackathon scenarios.

### Update 5: Gate 1 Payment Correctness & Nested Capture ID
- **Nested Capture ID Extraction (`backend/app/orchestrator/capture_lifecycle.py`):**
  - Corrected capture ID parsing: extracts genuine nested capture ID (`purchase_units[0].payments.captures[0].id`).
  - Added pre-settlement capture amount and currency code verification against purchase total.
- **Search Fallback Hardening (`backend/app/agent/loop.py`):**
  - Eliminated silent fallback to cheapest catalog item. Unmatched queries cleanly refuse with `no_matching_item`.
- **Two-Step Approval UX (`frontend/src/App.tsx`):**
  - Clearly differentiates Step 1: PayPilot Supervisor Approval (P2), followed by Step 2: PayPal Buyer Consent.

### Update 6: Formal Double-Entry Bookkeeping General Ledger (`backend/app/db/double_entry.py`)
- Standard 6-account Chart of Accounts (`1000 Cash`, `2100 Reservations`, `3100 Allocation`, `5100 Realized Purchases`, `5200 Encumbrance Contra`, `5300 Fees`).
- Mandatory database and Python balance validation: $\sum \text{Debits} = \sum \text{Credits}$ per currency. Unbalanced transactions immediately fail closed with zero database corruption.
- Added SQLite immutability triggers (`abort_journal_entries_update`, `abort_journal_entries_delete`, `abort_journal_lines_update`, `abort_journal_lines_delete`).
- Rebuildable general ledger projection function: `get_general_ledger_summary`.

### Update 7: Supported Merchant Directory & Payee Routing (`backend/app/paypal/merchants.py`)
- Configured payee routing for verified onboarded merchants: `merchant_shoes` (`shoes-facilitator@allowance.dev`), `merchant_coffee`, and `merchant_tech`.
- Prevents arbitrary payee redirection; orders can only be created with approved merchant payee credentials.

### Update 8: Personal Edition MVP Profile & Invariants
- Seeded individual user account (`ashfaq@allowance.dev`) and agent (`Personal Shopping Assistant`).
- Invariant configuration: **$100.00** daily budget, **$50.00** max per-tx, **$30.00** auto-approval threshold, and **$45.00** velocity cap.

### Update 9: Windows 1-Click Launcher & Desktop Shortcut
- Created `run.bat` for instant local execution (checks Python, verifies frontend build, opens browser to `http://localhost:8000`, starts server).
- Created `create_desktop_shortcut.bat` generating an `Allowance.lnk` Desktop shortcut.

### Update 10: Hackathon Submission Package & Integrations
- **Postman API Collection (`postman/allowance_v5_collection.json` & `postman/allowance_environment.json`):** Full Collection v2.1 with automated JavaScript assertion tests for all REST v1 and PayPal Sandbox routes (API collection; no sponsor prize).
- **Render Cloud Deployment Blueprint (`render.yaml` & `Dockerfile`):** Multi-stage production container and Infrastructure-as-Code blueprint configured for Render's 100% free tier.
- **Devpost Submission Narrative (`docs/devpost_submission.md`):** Complete submission document including official compliance proof for hackathon updates.
- **3-Minute Master Cut Video Script (`docs/video_demo_script.md`):** Rehearsed second-by-second presenter script strictly under 3 minutes.
- **Full Test Suite:** **137 / 137 automated tests passing in 6.96s**.

### Update 11: Submission Readiness Review & Hackathon Compliance (Tasks T1–T15)
- **T1 License & Hygiene:** Added standard MIT License (2026 Mohd Ashfaq) and updated `.gitignore` to ensure zero database or environment files are tracked.
- **T2 Secret Scan Sanitization:** Comprehensive audit of working tree and git history; sanitized all API keys to `ak_test_` prefix; redacted PayPal order IDs in `docs/sandbox-verification.md`.
- **T3 Zero-Dependency Path & Pre-built Frontend:** Verified Python standard library execution in `dev_server.py`; tracked pre-built production bundle in `frontend/dist/` (zero Node.js requirement for judges).
- **T4 Dynamic Config & Visibility:** Made `PUBLIC_BASE_URL` configurable; added `PAYPAL_SANDBOX_PAYEE` override; added clear console logs distinguishing Claude vs. Deterministic Fallback mode.
- **T5 Profile Alignment:** Synchronized documentation, demo table, and video script to the Demo Organization Profile ($300 / $150 / $50 / $100).
- **T6 Repeatable Demo Reset:** Implemented `python scripts/seed_db.py --reset` for clean 1-step demo resets with schema re-initialization.
- **T7 Frontend Version Sync:** Synchronized `frontend/package.json` (5.0.0) and UI header to `v5.0`.
- **T8 & T9 Sponsor Tools Alignment:** Clarified Render blueprint (100% free-tier compatible, transient SQLite) and Postman as an API developer collection (no sponsor prize).
- **T10 & T11 Documentation Overhaul:** Rewrote `README.md` with 60-second judge quick start; added dedicated testing instructions block to `docs/devpost_submission.md`.
- **T15 Submission Readiness Report:** Generated `docs/submission_readiness_report.md` with full matrix and verification evidence.

---

## 4. Current Platform Status & Capabilities

| Capability | Status | Implementation Details |
|---|---|---|
| **ShopMind AI Agent** | ✅ Complete | Zero payment access, tokenized catalog matching, deterministic fallback |
| **PayPilot Policy Engine** | ✅ Complete | Rules R0–R6, Checkpoints P1–P4, Invariant 3 self-exclusion |
| **Formal Double-Entry Ledger** | ✅ Complete | Balanced journal lines, $\sum \text{Debits} = \sum \text{Credits}$, SQLite triggers |
| **Hardened PayPal Orders v2** | ✅ Complete | `MAY_HAVE_BEEN_SENT`, atomic CAS leases, nested capture ID parsing |
| **REST API v1 (`/v1/*`)** | ✅ Complete | Signed quotes, idempotent purchase proposals, Bearer `ak_test_...` auth |
| **MCP Server** | ✅ Complete | Standalone JSON-RPC 2.0 with 5 safe tools, zero capture primitives |
| **Personal Edition MVP** | ✅ Complete | $100 daily limit, $30 auto-threshold, merchant payee routing |
| **Automated Test Suite** | ✅ Complete | **137 / 137 passing tests in 6.96s** |
| **Unified UI Dashboard** | ✅ Complete | React 18 + Tailwind, two-step approvals, Kill Switch banner |
| **1-Click Local Execution** | ✅ Complete | `run.bat` & Windows Desktop shortcut `create_desktop_shortcut.bat` |
| **Postman API Collection** | ✅ Complete | Collection v2.1 with automated test assertions (`postman/`) (no sponsor prize) |
| **Render Cloud Deployment** | ⚠️ Blueprint Included | `render.yaml` blueprint + multi-stage `Dockerfile` (free-tier compatible) |

---

## 5. How to Run the Platform

### Option A: Windows 1-Click Launch (Recommended)
Double-click **`run.bat`** in the repository root. It automatically opens `http://localhost:8000`.

### Option B: Cross-Platform Command
```bash
# Windows (PowerShell):
python scripts/dev_server.py

# macOS / Linux (Bash):
python3 scripts/dev_server.py
```
Open **`http://localhost:8000`** in your browser.

> [!NOTE]
> **Understanding Active Spending Profiles & Multi-Currency Support:**  
> - **Demo Organization Profile (Default Agent):** Configured as $300 daily budget, $50 auto-approval threshold, $150 per-tx ceiling. Evaluated by default on `http://localhost:8000` (allowing $45 shoes to auto-approve under $50, and $90 flats to escalate to human review).
> - **Personal Edition Profile (`agent_personal`):** Configured as $100 daily budget, $30 auto-approval threshold, $50 per-tx ceiling. Tested in `test_personal_edition_mvp.py` (where $18 socks auto-approve, $45 shoes escalate, and $90 flats are rejected outright).
> - **Global Multi-Currency Isolation:** While the hackathon prototype evaluates in USD ($) for standard compatibility with PayPal developer sandbox tools, the double-entry accounting engine already indexes budgets and enforces balance constraints independently per currency (`USD`, `EUR`, `GBP`, `CAD`, `AUD`, `JPY`, `INR`), providing turnkey architectural support for international client deployments in Allowance v5.1.

### Option C: Run the Automated Test Suite
```bash
# Windows (PowerShell):
$env:PYTHONPATH="." ; python -m unittest discover -s backend/tests -t . -p "test_*.py"

# macOS / Linux (Bash):
PYTHONPATH="." python3 -m unittest discover -s backend/tests -t . -p "test_*.py"
```

### Option D: Run Postman Collection
Import `postman/allowance_v5_collection.json` and `postman/allowance_environment.json` into Postman and click **Run Collection**.

---

## 6. The 7 Non-Negotiable Financial Safety Invariants

Every contributor, reviewer, and AI assistant working on this codebase **must strictly uphold these 7 invariants**:

1. **No Blind Capture Retries:** Once payment request transmission may have started, the outcome is `UNCERTAIN` until authoritative provider evidence resolves it. Never issue another capture `POST` merely due to a timeout, HTTP 429, or HTTP 5xx. Persist `MAY_HAVE_BEEN_SENT` before sending.
2. **Separate Workflow State from Financial Truth:** Workflow state describes purchase progress, not the balance sheet. Financial movements must be recorded in the append-only `journal_entries` and `financial_facts` tables with unique source-event keys.
3. **Preserve Financial Reservations and Budget Invariants:** Committed spend = Captured funds + Active reservations + Unresolved capture commitments. All arithmetic must use integer minor units (cents), never floating-point numbers.
4. **Atomic Admission and Queue Claims:** Concurrency checks, capacity checks, and job insertion must execute within the same serialized database transaction (`BEGIN IMMEDIATE`). Reject excess work before inserting records.
5. **Ownership and Concurrency Fencing:** Capture and reconciliation operations must have durable identities and explicit lease ownership via atomic CAS. Stale owners must be locked out from committing results.
6. **AI and Payment Authority Separation:** AI agents may propose products and carts only. AI-generated text, tool arguments, and external content are untrusted. The deterministic policy engine is the sole financial authority.
7. **Evidence-Based Engineering:** Never mark a payment as captured without authoritative provider evidence. Regression tests must accompany every fix.

---

## 7. Submission Checklist for Hackathon Submissions

- [x] **T1: License & Hygiene:** Official MIT License present, `.gitignore` comprehensive, no secrets/db files tracked.
- [x] **T2: Secret Scan:** Clean git history and working tree, `ak_test_` sanitized, provider response redacted.
- [x] **T3: Dependency & Runtime Path:** Python stdlib zero-dependency server verified, `frontend/dist` committed.
- [x] **T4: Environment Configuration:** `.env.example` created, dynamic base URL and payee overrides implemented.
- [x] **T5: Policy Profile Consistency:** Default Demo profile ($300/$150/$50/$100) aligned across UI and engine.
- [x] **T6: Repeatable Demo Reset:** `python scripts/seed_db.py --reset` CLI flag implemented and verified.
- [x] **T7: Frontend v5.0 Build:** Header and `package.json` updated to `v5.0.0`; clean production bundle built and committed.
- [x] **T8: Free-Tier Render Blueprint:** `render.yaml` updated for 100% free-tier compatibility (transient SQLite).
- [x] **T9: Sponsor Tools Alignment:** Postman documented as API collection; Render documented as blueprint included.
- [x] **T10: README Overhaul:** 60-second judge quickstart, modes table, cross-platform commands, changelog added.
- [x] **T11: Devpost Narrative & Instructions:** Complete problem narrative, official prize categories, testing block ready.
- [x] **T12: Test Suite Verification:** **137 / 137 automated tests passing in 6.96s**.
- [ ] **Human Task 1:** Record 3-minute video (under 3:00, no music, public YouTube) following `docs/video_demo_script.md`.
- [ ] **Human Task 2:** Take 4–5 clean dashboard screenshots and upload thumbnail.
- [ ] **Human Task 3:** Make GitHub repository public and confirm MIT License is visible in the About section.
- [ ] **Human Task 4:** Submit Devpost entry before the official deadline (Nov 12, 2026, 12:00 PM Pacific / Nov 13, 1:30 AM IST).
