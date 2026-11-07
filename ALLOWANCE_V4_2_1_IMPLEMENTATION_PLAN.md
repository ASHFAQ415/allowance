# ALLOWANCE v4.2.1 — IMPLEMENTATION PLAN

**Date:** November 6, 2026  
**Baseline:** v4.2 architecture and implementation plan  
**Purpose:** Implement only the consistency and financial-safety corrections in the v4.2.1 patch.

## 1. Scope lock

Keep the v4.2 product, stack, hackathon scope, UI, ShopMind, PayPilot rules R0–R6, checkpoints P1–P4, PayPal Orders v2, and single-process SQLite deployment.

Do not introduce new infrastructure or broaden the product. This plan is an implementation sequence, not evidence that code exists.

## 2. Work packages

### WP0 — Baseline and migration safety
- Preserve v4.2 documents as historical baseline.
- Add versioned, transactional SQLite migration.
- Test clean install and upgrade from populated v4.2 database.
- Ensure migration can be safely retried after process interruption.

**Gate:** Migration tests pass; no historical ledger rows are rewritten or deleted.

### WP1 — Financial facts and budget projection
- Add `capture_operations`, `budget_accounting`, and required indexes.
- Implement a single budget snapshot repository.
- Implement projection rebuild and verification.
- Define exact movement among reserved, unresolved, captured, and released categories.
- Ensure P2–P4 self-exclusion occurs exactly once.
- Keep all money in integer minor units.

**Gate:** Budget tests in v4.2.1 patch pass, including concurrent overspend and projection repair.

### WP2 — Capture safety and ownership
- Implement one capture operation per purchase and stable request ID.
- Persist `MAY_HAVE_BEEN_SENT` before network I/O.
- Use typed transport outcomes; classify ambiguous outcomes conservatively.
- Remove all retry capture POST behavior after possible transmission.
- Implement serialized owner-token/lease claims.
- Make reconciliation read-only.
- Implement idempotent success/failure resolution and late adjustments.

**Gate:** Mock provider tests simulate delayed acceptance, lost response, late completion, duplicate observations, and competing resolvers. No ambiguous scenario produces a second capture POST.

### WP3 — Queue lifecycle and crash recovery
- Make queue deadline part of atomic `QUEUED -> RUNNING` SQL claim.
- Define four running + four waiting capacity precisely.
- Enforce two active runs per agent in the same transaction as admission.
- Implement durable dispatch/recovery for DB-commit-to-enqueue gaps.
- Ensure duplicate queue delivery cannot execute twice.

**Gate:** Concurrency tests prove no stale queued job starts, no duplicate execution, and no phantom accepted run.

### WP4 — API and tenant isolation
- Require authenticated demo session for data routes.
- Keep admin mutations and approval links admin-only.
- Scope every lookup, list, and SSE stream to tenant/agent identity.
- Add DTO allowlists, pagination, rate limits, SSE bounds, and retention.
- Keep health response minimal.

**Gate:** Cross-tenant ID substitution and SSE replay tests fail closed.

### WP5 — Full regression and PayPal sandbox
- Run all existing v4.2 tests plus v4.2.1 adversarial tests.
- Verify auto-approval and human-approval flows.
- Demonstrate an ambiguous capture that later resolves successfully.
- Demonstrate unresolved capture remains budget-committed.
- Record evidence in `docs/sandbox-verification.md`.
- Update README and demo script to state only verified guarantees.

**Gate:** No release if any P0 invariant fails or if the sandbox behavior contradicts the documented capture protocol.

## 3. Required CI gates

1. Unit tests: policy, state machine, budget accounting, capture classification.
2. Property tests: budget never exceeds cap under concurrent submissions.
3. Integration tests: SQLite transactions, migrations, PayPal mock, queue dispatcher.
4. Adversarial tests: ambiguous capture, late success, competing resolvers, stale worker, cross-tenant access.
5. Static boundary tests: ShopMind imports remain isolated; PayPilot remains pure.
6. Coverage: retain v4.2 targets for policy and state machine; add branch coverage for capture classification and budget movement.
7. Formatting, type checks, lint, and full test suite must pass from a clean checkout.

## 4. Demo-safe fallback

If real sandbox capture verification is unavailable, demo the payment protocol with the deterministic mock provider and clearly label it as simulated. Do not claim a real capture or production-grade payment guarantee without recorded evidence.

## 5. Definition of done

- All v4.2.1 tests pass.
- Clean and upgrade migrations pass.
- No capture retry occurs after possible transmission.
- Budget projection is consistent with immutable facts.
- Late capture resolution is idempotent and does not mutate terminal purchase state.
- Queue claim and timeout are atomic.
- Tenant data isolation is tested.
- At least one real sandbox capture is verified if claiming live PayPal execution in the submission.
- Documentation, API behavior, and demo script agree.
