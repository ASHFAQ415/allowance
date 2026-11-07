# ALLOWANCE v4.2 — OPEN RISKS, LIMITATIONS & RESIDUAL THREAT MODEL

**Status:** APPROVED RISK DISCLOSURE & OPERATIONAL MITIGATION GUIDE (v4.2)  
**Date:** November 6, 2026  
**Audience:** Judges, Presenters, Security Reviewers, DevSecOps Engineers.

---

## 1. Executive Risk Disclosure

Allowance v4.2 enforces an adversarial-grade financial authorization architecture. However, in any distributed financial system interacting with third-party payment rails and non-deterministic foundation models, external risks and operational trade-offs exist. 

This document provides a transparent, uncompromising disclosure of all known residual risks, external dependencies, and mitigation strategies.

---

## 2. Itemized Residual Risks & Mitigation Strategies

### 2.1 External Payment Provider Risks (PayPal Sandbox)

| Risk ID | Risk Description | Severity | Likelihood | Impact on System | Engineering Mitigation in v4.2 | Fallback / Presentation Strategy |
|---|---|---|---|---|---|---|
| **R-01** | **PayPal Sandbox Outage or 5xx Flakiness** | High | Medium | Sandbox endpoints return 500/503 during live demo. | Bounded retries on create; capture lands safely in `CAPTURE_UNKNOWN`; reconcile spaced by 30s. | `PAYPAL_MODE=mock` local server fallback; pre-recorded video backup take. |
| **R-02** | **Orders v2 Idempotency Retention Window SLA** | Medium | Low | Request-ID retention in Orders v2 is documented as "up to 45 days," but lacks formal contractual SLA. | Allowance transactions complete within a 15-minute window; idempotency validated in sandbox smoke. | If PayPal fails duplicate detection after 45 days, our local `ledger` unique constraint prevents duplicate processing. |
| **R-03** | **Manual Buyer-Consent Click Required in Sandbox** | Low | High | Fully autonomous zero-click payment cannot run in sandbox without enterprise Vaulting permissions. | Admin opens stored `approval_url`; human logs in to sandbox buyer account and clicks "Pay Now". | Clearly explain on pitch slides that Vaulted Payment Methods is the roadmap architecture for enterprise autonomy. |
| **R-04** | **In-Flight Capture Recall Impossibility** | Medium | Low | Admin activates Freeze while PayPal's settlement engine is already processing a capture POST. | Once HTTP bytes are accepted, PayPal moves money. Freeze blocks all future claims, not in-flight network packets. | Explicitly disclosed in pitch and documentation; double-entry adjustment handles discrepancy. |

---

### 2.2 Host & Database Architecture Risks

| Risk ID | Risk Description | Severity | Likelihood | Impact on System | Engineering Mitigation in v4.2 | Fallback / Presentation Strategy |
|---|---|---|---|---|---|---|
| **R-05** | **Single-Process Constraint of SQLite WAL** | High | Low | If deployed with multiple uvicorn worker processes, SQLite lock contention causes 503 errors. | Architecture explicitly mandates a single uvicorn worker (`workers=1`) with internal `ThreadPoolExecutor(4)`. | Enforced in Docker Compose and Procfile configurations. |
| **R-06** | **In-Memory Rate Limit Reset on Restart** | Low | Medium | Restarting the server resets in-memory sliding window IP rate limits. | Database holds all persistent state; brief window of reset limits does not breach financial policy. | Acceptable trade-off for hackathon scope; avoids Redis operational overhead. |
| **R-07** | **Ephemeral Disk Storage on Serverless Hosts** | Critical | Medium | Deploying to stateless containers (e.g., standard Heroku or Render without disk) deletes SQLite file on restart. | Docker Compose configuration specifies persistent volume mount; Phase 1 skeleton deploy verifies persistence. | If hosted on ephemeral container, app re-seeds automatically on boot and README documents persistence rules. |

---

### 2.3 AI Foundation Model Risks (Claude API)

| Risk ID | Risk Description | Severity | Likelihood | Impact on System | Engineering Mitigation in v4.2 | Fallback / Presentation Strategy |
|---|---|---|---|---|---|---|
| **R-08** | **Claude API Rate Limit or Global Outage** | High | Medium | Model calls return 429 or 500, stalling shopping agent loop. | Loop transitions immediately to deterministic keyword search fallback; prices from catalog; policy still checked. | `AGENT_MODE=fixture` replays recorded Claude responses; demo continues seamlessly. |
| **R-09** | **Model Hallucination of Prices or Quantities** | High | High | Claude suggests negative numbers, out-of-stock items, or arbitrary discounts. | Server recomputes prices from catalog; Pydantic validates quantity (1–10); model-set prices discarded. | 100% immune by architectural design. |
| **R-10** | **Adversarial Prompt Injection via User or Catalog** | High | High | User or product description commands agent to bypass PayPilot or buy unapproved goods. | Agent has zero financial authority; PayPilot Checkpoint P1 inspects items against allowlist; triggers `BLOCKED(R1)`. | 100% immune by architectural design. |

---

### 2.4 Hackathon Scope & Timeline Risks

| Risk ID | Risk Description | Severity | Likelihood | Impact on System | Engineering Mitigation in v4.2 | Fallback / Presentation Strategy |
|---|---|---|---|---|---|---|
| **R-11** | **Time Slippage on Frontend Polishing** | Medium | Medium | Building React timeline and chat exceeds allocated schedule. | Frontend scaffold built in Phase 3; cut order prioritizes core over animations. | Present via FastAPI Swagger UI and pre-recorded high-fidelity video if UI is delayed. |
| **R-12** | **Public Demo Budget Exhaustion by Judges** | High | Medium | Anonymous judges run out the daily allowance before judging concludes. | In production, `/agent/run` is token-protected; read-only `/agent/preview` allows safe judging; daily reset at midnight. | Presenter resets demo database between judging sessions if permitted. |

---

## 3. Explicit Cut Order Under Time Pressure

If implementation milestones slip behind the Phase Schedule:

```
CUT ORDER (First to sacrifice -> Last to sacrifice):
  1. Frontend animations and micro-interactions.
  2. Suite H expanded 15-prompt eval runner (rely on core 10 fixtures).
  3. POST /agent/preview endpoint (rely on token-protected /agent/run).
  4. PayPal Agent Toolkit adapter (rely entirely on direct Orders v2 httpx client).
  5. Overall backend test coverage target relaxed from 85% to 75% (NEVER relax Policy or State Machine).
  6. SSE live streaming (fallback to 2-second polling).

NEVER CUT UNDER ANY CIRCUMSTANCES (Non-Negotiable Core):
  - State machine rank monotonicity and terminal immutability.
  - PayPilot rules R0 to R6 and Checkpoints P1 to P4.
  - Exclusive capture claim (BUYER_APPROVED -> CAPTURING).
  - Verify_order assertions before capture.
  - Concurrency tests (10-thread budget race).
  - Emergency Freeze toggle and tests.
  - 3-minute demo video backup.
```

---

## 4. Final Risk Certification

The Allowance v4.2 architecture contains no undisclosed single-points-of-failure or speculative guarantees. All known failure modes terminate in conservative, budget-preserving states (`BLOCKED`, `EXPIRED`, `CAPTURE_UNKNOWN`, or `FAILED`).

