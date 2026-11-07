# ALLOWANCE v4.2.2 — IMPLEMENTATION & MIGRATION PLAN

**Purpose:** Deliver the frozen release specification without expanding scope.  
**Target:** Hackathon submission Nov 11, 2026; buffer Nov 12, 2026. Reconfirm the official deadline on Devpost.

## 1. Non-negotiable scope

Keep Python 3.11+, FastAPI, Pydantic v2, SQLite WAL, HTTPX, PayPal Orders v2, Claude API, React/Vite/Tailwind, Pytest/Hypothesis/Playwright, one Uvicorn process and four worker threads. No new infrastructure.

## 2. Work packages and gates

### WP0 — Baseline and inventory
- Tag existing v4.2 source documents as historical.
- Confirm actual repository code and current DB schema; do not assume docs equal implementation.
- Create a requirements-to-code-to-test traceability sheet.
- Capture clean DB backup and migration test fixtures.

**Gate:** Every v4.2.2 requirement has a target module, migration, test and owner.

### WP1 — Schema and migration
- Add versioned migration for `capture_operations`, `financial_facts`, `budget_accounting`, ownership/lease fields, tenant ID and required indexes.
- Add immutable triggers for `financial_facts`, `ledger`, and `ledger_adjustments`.
- Preserve historical ledger rows; never rewrite them to fit new semantics.
- Backfill financial facts from existing purchase/ledger history only where the historical record unambiguously proves the financial movement. Mark ambiguous historical rows as `LEGACY_UNVERIFIED` and hold conservatively; do not invent settlement facts.
- Run projection reconciliation after backfill; require operator review for unexplained differences.

**Gate:** Clean install, populated v4.2 upgrade, interrupted migration restart, rollback/restore drill pass.

### WP2 — Financial journal and projection
- Implement append-only financial fact repository with unique source_event_key.
- Implement `budget_accounting` transactional projection.
- Implement R3 and R5 snapshot builder with single self-exclusion.
- Implement rebuild, verify, discrepancy alert and audited repair command.
- Use integer cents and currency-keyed buckets only.

**Gate:** all financial property tests pass; concurrent admissions cannot exceed budget.

### WP3 — Capture operation and transport classification
- One capture operation per purchase; stable UUID4 request ID.
- Add explicit transport adapter outcome describing whether transmission could have started.
- Persist MAY_HAVE_BEEN_SENT immediately before possible body transmission.
- Allow at most one retry only when transport proves no bytes were sent and operation remains PREPARED.
- Any uncertainty, write/read error, 429, 5xx, PENDING, malformed response → UNKNOWN.
- Reconciliation GET-only; no capture POST retries.
- Add owner-token/lease CAS and idempotent result resolver.
- Add late settlement/non-settlement adjustment facts.

**Gate:** mock provider can accept capture but drop response, delay completion, return APPROVED, and later return COMPLETED. No ambiguous path sends another capture POST.

### WP4 — Queue and run lifecycle
- Implement admission cap before row insert (4 workers + 4 waiters; maximum 8 active; max 2 per agent).
- Durable queued rows and dispatcher recovery.
- Atomic queue deadline in QUEUED→RUNNING claim.
- Re-dispatch eligible QUEUED jobs after restart; fail expired queued jobs.
- RUNNING rows interrupted on startup; reject stale worker result by ownership/status CAS.

**Gate:** queue saturation, enqueue crash gap, duplicate delivery, queue deadline boundary, and per-agent concurrency tests pass.

### WP5 — API security and DTOs
- Implement configured single demo tenant; no general customer account system.
- Demo session required for data reads, runs, and SSE.
- Admin token required for administrative mutations and approval URLs.
- Scope every SQL read/write by tenant ID.
- DTO allowlists, generic 404, pagination, SSE limits, rate limits, redaction and retention.

**Gate:** cross-tenant/object enumeration tests and sensitive-field scans pass.

### WP6 — PayPal and end-to-end
- Validate order amount, currency, payee and purchase reference before capture.
- Execute buyer consent flow.
- Record actual sandbox observations in `docs/sandbox-verification.md`.
- Test mock ambiguous capture separately from live sandbox smoke.
- Never claim sandbox behavior based on a mock.

**Gate:** all required live tests for any claimed live behavior have dated redacted evidence.

### WP7 — Release readiness
- Run all v4.2 and v4.2.2 tests.
- Run formatting, lint, type checks, security checks, clean build and browser E2E.
- Fresh clone and run from README.
- Confirm production startup guards, persistent volume, single process, no demo reset in production.
- Review Devpost, demo script and video for unsupported claims.

**Gate:** all P0 tests pass; no unresolved P0 discrepancy; release checklist signed.

## 3. Migration design requirements

- Increment `schema_version` only after the full migration commits.
- Use `BEGIN IMMEDIATE` and transactional DDL where supported by SQLite version.
- Make migration idempotent through schema version checks, not by silently swallowing SQL errors.
- Never drop or rewrite historical financial evidence.
- On failure, rollback fully and keep the prior schema usable.
- Test with a copy of a realistic populated DB and with a process kill at each migration phase.
- Backups must be made before migration; restore must be tested.

## 4. Delivery order under deadline pressure

1. P0: capture no-retry safety, financial facts/projection, state machine and policy.
2. P0: migration safety, capture resolution ownership, budget concurrency tests.
3. P0: queue admission/deadline/recovery.
4. P0: authentication and DTO boundaries.
5. P1: live PayPal verification, SSE polish, animations and expanded eval runner.

Do not cut financial invariants, capture uncertainty handling, budget concurrency tests, terminal-state immutability, or emergency Freeze tests. If live sandbox is unavailable, show a clearly labelled mock demonstration and disclose that live capture verification is incomplete.

## 5. Definition of done

A requirement is done only if code, test, and evidence are linked. The spec itself is not proof. The release report must identify each requirement as Specified / Implemented / Tested / Sandbox-verified / Deployed.
