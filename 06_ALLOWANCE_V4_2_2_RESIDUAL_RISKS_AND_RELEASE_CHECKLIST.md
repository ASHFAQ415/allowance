# ALLOWANCE v4.2.2 — RESIDUAL RISKS & RELEASE CHECKLIST

## 1. Residual risks

| ID | Risk | Severity | Mitigation | Residual limitation |
|---|---|---|---|---|
| RR-01 | PayPal outage/5xx | High | UNKNOWN state, GET-only reconciliation, mock fallback | Live payment unavailable; unresolved funds remain held |
| RR-02 | Provider idempotency retention/semantics | High | Stable UUID, one logical operation, no retry after possible send | External behavior cannot be guaranteed forever |
| RR-03 | Capture already in flight when Freeze activates | High | P4 immediately before claim; audit and reconcile | Cannot recall a request already transmitted |
| RR-04 | SQLite single-process limit | High | One Uvicorn process, four threads, WAL, persistent volume | No horizontal scaling |
| RR-05 | Host disk loss/corruption | Critical | Persistent volume, backups, restore drills | If storage is lost, availability and recovery depend on backup integrity |
| RR-06 | Rate-limit reset on restart | Medium | Short-lived process limits plus durable admission/budget caps | IP/session abuse limits may reset; financial caps remain authoritative |
| RR-07 | Claude outage/rate limit | Medium | Bounded deterministic catalog fallback | Agent quality may degrade; policy still runs |
| RR-08 | Prompt injection in user/catalog data | High | No financial authority in model; server-side price and policy | Cannot guarantee model output quality; security boundary is deterministic code |
| RR-09 | Sandbox buyer consent | Medium | Explicit manual buyer action | No zero-click charging in demo |
| RR-10 | Unresolved capture never settles or remains unknown | High | Keep commitment, alert, operator runbook | May require provider support; no unsafe auto-release |
| RR-11 | Single demo tenant/session | Medium | Session token, tenant scoping, admin token | Not a complete customer identity/RBAC product |
| RR-12 | Mock/live mismatch | High | Separate evidence classes; live smoke record | Mock success is not proof of PayPal behavior |

## 2. Security and operational constraints

- Production mode refuses startup without high-entropy admin and demo-session secrets.
- No secrets or raw provider payloads in logs, SSE or public DTOs.
- Persistent SQLite volume is mandatory; ephemeral filesystem deployments are prohibited.
- One Uvicorn worker process only.
- Backups must be tested by restore, not merely created.
- Demo reset is disabled in production and never deletes production financial history.
- Unknown provider status, missing capture ID, mismatch or projection drift alerts operators.
- No unresolved capture may be released due solely to timeout, age, retry exhaustion, `APPROVED`, or a missing API response.

## 3. Release checklist

### Specification and traceability
- [ ] All v4.2.2 documents share release number and date.
- [ ] All v4.2 documents marked historical/superseded.
- [ ] Every P0 requirement maps to code, test and evidence.
- [ ] No capture retry-after-APPROVED language remains in active docs.

### Database and financial correctness
- [ ] Clean schema install passes.
- [ ] Populated v4.2 migration passes.
- [ ] Interrupted migration recovery passes.
- [ ] Financial facts immutable.
- [ ] Budget projection rebuild matches journal.
- [ ] Concurrent budget admission cannot overspend.
- [ ] Late capture adjustment idempotent.
- [ ] Terminal purchase history remains unchanged.

### Payment
- [ ] Capture transport classification tested.
- [ ] No ambiguous capture retry.
- [ ] Read-only reconciliation tested.
- [ ] Amount/currency/payee/order/capture identity validated.
- [ ] PayPal V1–V12 evidence recorded for claimed behavior.
- [ ] Mock and sandbox results clearly distinguished.

### Queue and API security
- [ ] Four running/four waiting admission semantics tested.
- [ ] Ninth active run rejected before DB row insertion.
- [ ] Queue deadline atomic.
- [ ] DB-to-queue crash gap recovered.
- [ ] Max two active runs per agent.
- [ ] Demo session required for data routes.
- [ ] Admin token required for admin routes.
- [ ] Tenant scope and DTO leakage tests pass.

### Product and submission
- [ ] Fresh clone can install and run.
- [ ] Docker uses persistent volume and one Uvicorn worker.
- [ ] Production reset is disabled.
- [ ] Demo script labels mock vs live behavior.
- [ ] 3-minute video backup exists.
- [ ] Devpost claims match actual implementation evidence.
- [ ] Official submission deadline reconfirmed.

## 4. Release status template

| Requirement | Status | Evidence |
|---|---|---|
| Specified | Not yet / Yes | Document section |
| Implemented | Not yet / Yes | Commit + module |
| Tested | Not yet / Yes | CI run + test IDs |
| Sandbox-verified | Not yet / Yes / N/A | `docs/sandbox-verification.md` |
| Deployed | Not yet / Yes | Build ID + URL |

Never mark a row “Yes” without evidence.
