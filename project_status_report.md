# ALLOWANCE (v4.2.2) — Project Status Report & Collaborator Guide

> **"Give your AI agent an allowance, not your wallet."**  
> **Repository:** [https://github.com/ASHFAQ415/allowance](https://github.com/ASHFAQ415/allowance)  
> **Date:** November 2026  
> **Target Audience:** Collaborators, Forkers, Reviewers, and AI Coding Agents (Cursor, Copilot, Antigravity, Claude Code)

---

## 1. Executive Summary & Platform Identity

**ALLOWANCE** is an autonomous AI agent spending control platform built for the **PayPal AI Hackathon 2026**.

Modern LLM agents are increasingly tasked with autonomous purchasing, booking, and procurement. However, granting an AI direct access to credit cards or raw payment APIs presents unacceptable financial risks: hallucinations, prompt injection, blind retry loops, merchant spoofing, and runaway budget exhaustion.

ALLOWANCE solves this by enforcing a strict separation of powers:
- **ShopMind:** An unprivileged, sandboxed product discovery agent that proposes purchases based on catalog items. It has **zero** access to payment credentials, cannot trigger capture calls, and cannot alter policies.
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
│   │   │   └── tools.py                    <-- Propose purchase & search tools
│   │   ├── db/
│   │   │   ├── connection.py               <-- SQLite WAL mode & atomic transactions
│   │   │   ├── financial_journal.py        <-- Append-only double-entry financial ledger
│   │   │   ├── purchases.py                <-- Purchase lifecycle persistence & queries
│   │   │   └── schema.py                   <-- DB DDL: tables, indexes, constraints
│   │   ├── engine/
│   │   │   ├── admission.py                <-- Concurrency limits & queue rate fencing
│   │   │   ├── checkpoints.py              <-- P1 (Pre-auth), P2 (Escalation), P3 (Post-approval), P4 (Recon)
│   │   │   ├── policy.py                   <-- Deterministic Rules R0 (Kill Switch) through R6 (Category)
│   │   │   └── state_machine.py            <-- PurchaseState transitions & lease acquisition
│   │   ├── paypal/
│   │   │   ├── client.py                   <-- PayPal Orders v2 API client (Sandbox / Live)
│   │   │   ├── exclusive_claim.py          <-- Atomic CAS lease claiming & fence protection
│   │   │   ├── mock_service.py             <-- Authoritative PayPal mock for sandbox testing
│   │   │   └── reconciliation.py           <-- Read-only GET bounded recovery loop
│   │   └── api/                            <-- REST endpoints (agent, policies, purchases, kill switch)
│   └── tests/
│       ├── test_adversarial_concurrency.py <-- Race conditions, double capture, journal integrity
│       ├── test_agent_loop.py              <-- Claude & fallback agent execution
│       ├── test_checkpoints.py             <-- P1–P4 deterministic enforcement
│       ├── test_engine_state_machine.py    <-- Monotonic state transitions & invalid edges
│       ├── test_exclusive_claim.py         <-- Lock takeover & lease expiration fencing
│       ├── test_financial_journal.py       <-- Invariant 2 & 3 append-only math tests
│       ├── test_paypal_reconciliation.py   <-- Read-only reconciliation under uncertainty
│       └── test_policy_engine.py           <-- Rules R0 through R6 edge cases
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

Before recent updates, several critical edge cases affected usability during demo testing. Here is what was diagnosed and fixed:

### Fix 1: Search Fallback & Price Modifier Bug ($45 vs. $6)
- **Problem:** When sending prompts such as `"Find trail running shoes under $45 from approved store"`, the search agent returned `coffee_paper_filters` for `$6.00` instead of the expected `Trail Running Shoes` (`$45.00`).
- **Root Cause:** In `backend/app/agent/catalog.py`, `search_catalog_data` performed a rigid exact substring match (`if query.lower() in item.name.lower()`). Because the full conversational sentence was passed as the search query, no items matched by substring, triggering the fallback heuristic to pick the cheapest item in the catalog ($6 coffee filters).
- **Solution:** Upgraded search in `backend/app/agent/catalog.py` to extract price constraints (e.g. `under $45` $\rightarrow \le 4500$ cents) and score items using tokenized keyword overlap (stripping conversational filler words). `run_deterministic_fallback` in `backend/app/agent/loop.py` was aligned to use this tokenized search. Now accurately returns `Trail Running Shoes` at `$45.00`.

### Fix 2: Emergency Freeze (Kill Switch) UI Visibility
- **Problem:** When the Kill Switch (Rule R0) was activated and a user submitted a purchase request, the resulting blocked purchase was invisible in the UI.
- **Root Cause:** The dashboard's orders section was hardcoded to filter exclusively for `status === 'PENDING_APPROVAL'`, causing `BLOCKED` and `CAPTURED` orders to disappear from the card feed.
- **Solution:** 
  1. Added a dedicated **Active Purchases & Execution Orders** card panel in `frontend/src/App.tsx` displaying real-time purchase states (`CAPTURED`, `PENDING_APPROVAL`, `BLOCKED`).
  2. Enriched blocked cards with explicit, human-readable rejection reasons (e.g., `Emergency spending freeze is ACTIVE (Rule R0)` or `Merchant 'DarkWeb Store' is not in allowlist`).
  3. Added an animated full-width **Emergency Kill Switch Banner** across the top of the interface when the freeze is active, with an optimistic toggle for instant feedback.

### Fix 3: One-Click Demo Scenarios
- **Addition:** Added 4 quick-select pill buttons directly under the chat input in `frontend/src/App.tsx`:
  - 🏃 **Trail Shoes ($45)** — Triggers an autonomous auto-approval and immediate PayPal capture.
  - 👟 **Road Flats ($90)** — Triggers Rule R2 escalation for human supervisor review (exceeds $50 auto-limit).
  - ⚠️ **DarkWeb Store ($15)** — Demonstrates deterministic merchant block via Rule R4.
  - ☕ **2x Coffee Beans ($44)** — Verifies multi-quantity cart arithmetic and budget compliance.

### Fix 4: Concurrency & Database Foreign Key Constraints
- **Problem:** Several tests in `backend/tests/test_adversarial_concurrency.py` failed due to foreign key violations on `agent_runs` and parameter mismatches in `record_financial_fact`.
- **Solution:** Updated test fixtures to create prerequisite `agents` and `agent_runs` rows, aligned `record_financial_fact` call signatures with `backend/app/db/financial_journal.py`, and updated lease race tests to verify that exactly one thread captures while nine fail closed.

### Fix 5: Markdown Specification & Repository Hygiene
- **Cleanup:** Purged 19 obsolete and fragmented draft markdown files (`00_` through `06_`, `ALLOWANCE_V4_1...`, `allowance-implementation-plan-v4.md`). Retained `allowance_v4_2_2_master_plan.md` and `learning_proposal.md` as the authoritative specifications.
- **Git Repository:** Initialized on `main`, tracked cleanly, and synchronized with GitHub remote `https://github.com/ASHFAQ415/allowance`.

---

## 4. Current System & Verification Status

| Component | Status | Verification Detail |
|---|---|---|
| **Deterministic Policy Engine (R0–R6)** | ✅ Complete | Verified with 100% test coverage for kill switch, limits, whitelist, and budget checks |
| **Checkpoints P1, P2, P3, P4** | ✅ Complete | Enforces state gating prior to provider calls and post-approval |
| **PayPal Orders v2 Integration** | ✅ Complete | Exclusive lease claiming, mock service, and idempotent recovery |
| **Append-Only Financial Journal** | ✅ Complete | Zero floating-point math; integer cents; double-entry journal balance tracking |
| **Admission Queue & Concurrency Fencing** | ✅ Complete | Atomic transaction claim; queue expiry enforcement; CAS leasing |
| **Agent Discovery Loop (ShopMind)** | ✅ Complete | Claude tool-calling loop + tokenized deterministic fallback |
| **Frontend Dashboard (React 18)** | ✅ Complete | Single-page real-time dashboard; compiled with Vite (0 build errors) |
| **Automated Test Suite** | ✅ Complete | **72 / 72 tests passing** across 8 test suites in ~3.2 seconds |
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
# Set PYTHONPATH to project root and run all 72 tests
python -m unittest discover -s backend/tests -t . -p "test_*.py"
```
*Expected output:* `Ran 72 tests in ~3.2s ... OK`.

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

### Task Backlog for Contributors

#### Task 1: Real PayPal Sandbox Integration Toggle
- **Current State:** The system uses `backend/app/paypal/mock_service.py` with full latency and error injection capabilities.
- **Next Step:** Wire in live PayPal Sandbox credentials via `.env` (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`). Update `backend/app/paypal/client.py` to seamlessly switch between mock mode and real PayPal OAuth 2.0 + Orders v2 endpoints based on config flags.

#### Task 2: Multi-Agent Allowance Profiles
- **Current State:** Single default agent policy configuration.
- **Next Step:** Add database support for multiple distinct agents (e.g. `ProcurementAgent`, `TravelAgent`, `CloudOpsAgent`), each with independent daily/monthly allowances, supervisor lists, and category permissions.

#### Task 3: External Catalog Ingestion & MCP Tools
- **Current State:** In-memory SQLite catalog with 6 verified products and token-based scoring.
- **Next Step:** Build an MCP (Model Context Protocol) tool or OpenAPI adapter to query external store APIs (e.g. Shopify, BigCommerce, or mock e-commerce REST APIs) dynamically while sanitizing product names and prices.

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
- Never modify `backend/app/engine/policy.py` or `backend/app/paypal/exclusive_claim.py` without verifying that concurrency invariants hold.
- Always preserve integer cents for all money representations.
- Reference `.agents/rules/financial_safety_invariants.md` before making architectural decisions.
