# ALLOWANCE v4.2.2 — CHANGELOG & SUPERSESSION REGISTER

**Status:** Normative document-control record  
**Release date:** November 6, 2026

## 1. Evolution

| Version | Role | Authority in v4.2.2 |
|---|---|---|
| v4 baseline | Original end-to-end implementation plan | Historical reference only |
| v4.1 | Adversarial review and hardening patch | Historical; retain fixes not superseded |
| v4.2 | Architecture, implementation, test matrix, guarantee matrix, PayPal protocol and risk register | Historical baseline; conflicting clauses superseded |
| v4.2.1 | Consistency and financial-safety corrective patch | Incorporated; its binding corrections are consolidated here |
| v4.2.2 | Consolidated release specification and companion evidence documents | Sole normative release baseline |

## 2. Binding changes

1. **Capture retry:** Remove v4.2 retry when GET says APPROVED after an ambiguous capture. Reconciliation is read-only. Only a proven pre-send failure may be retried once while operation remains PREPARED.
2. **Purchase vs settlement:** `ledger` contains workflow transitions only. Late settlement is a financial adjustment/fact, never a fake `FAILED -> RESOLVED_CAPTURED` state transition.
3. **Financial source of truth:** Add append-only `financial_facts`; `budget_accounting` is a rebuildable projection. R3 = captured + reserved + unresolved. R5 auto-approved commitment is a subset.
4. **Capture ownership:** Add one `capture_operations` row per purchase, stable request ID, owner token/lease, CAS resolution.
5. **Queue deadline:** Enforce deadline inside atomic QUEUED→RUNNING claim.
6. **Queue crash gap:** Durable QUEUED rows are re-dispatched; queue capacity is admitted before row creation using durable active-run count.
7. **API scope:** Single configured demo tenant and authenticated demo-session reads; admin operations remain admin-only. This is not general multi-tenant login/RBAC.
8. **Test coverage:** Preserve original 35 scenarios, correct affected expectations, add 18 v4.2.2 scenario groups.
9. **Guarantees:** Reclassify each guarantee by application, SQLite, PayPal, infrastructure or best-effort dependency. No exactly-once claim.
10. **Evidence:** Mocks, live sandbox, implementation tests and deployment evidence are separate categories.

## 3. Supersession map

| Existing source | Sections/claims superseded | Replacement |
|---|---|---|
| `allowance-implementation-plan-v4.md` | Any earlier capture retry, state, queue or budget rules conflicting with v4.2.2 | Master specification + implementation plan |
| `ALLOWANCE_V4_1_REVIEW_AND_PATCH.md` | F3 retry-after-APPROVED rule and any prior capture reconciliation rule conflicting with GET-only | Master specification §5 |
| `ALLOWANCE_V4_2_FINAL_ARCHITECTURE.md` | Capture retry on APPROVED; pseudo-state ledger entry; post-claim queue timeout; old budget sum semantics | Master specification §§4–7 |
| `ALLOWANCE_V4_2_IMPLEMENTATION_PLAN.md` | Capture retry on APPROVED; queue claim-then-check; old ledger/budget projection assumptions; queue saturation conflict | Master specification §§5–7 |
| `ALLOWANCE_V4_2_ADVERSARIAL_TEST_MATRIX.md` | #10 release on any generic 422; #21/22 queue details; any capture retry expectations | v4.2.2 test matrix §§2–3 |
| `ALLOWANCE_V4_2_GUARANTEE_MATRIX.md` | G7/G13 claims if implying any capture retry after possible transmission or provider idempotency guarantees local correctness | v4.2.2 guarantee matrix |
| `ALLOWANCE_V4_2_CHANGELOG.md` | “zero known critical contradictions” and v4.2 retry claims | This changelog |
| `ALLOWANCE_V4_2_PAYPAL_VERIFICATION.md` | Any replay test interpreted as permission for Allowance to retry an ambiguous capture | v4.2.2 PayPal protocol |
| `ALLOWANCE_V4_2_OPEN_RISKS.md` | Any claim that all risks are fully mitigated or that auto-reset is safe for financial state | v4.2.2 risk disclosure below |
| v4.2.1 patch/addendum/plan | Consolidated and clarified where ambiguous (transport marker, queue admission, tenant scope, accounting source facts) | v4.2.2 master specification |

## 4. Document status rules

- Keep old files unchanged as historical evidence; add a header: “SUPERSEDED FOR RELEASE DECISIONS BY v4.2.2”.
- Do not delete historical files or rewrite their original claims; document corrections here.
- Every implementation PR must reference a v4.2.2 requirement ID and test ID.
- If a future change modifies financial semantics, increment version and update master spec, schema, tests, guarantee matrix, PayPal protocol and risk register together.
- If documents disagree, stop release and fix the contradiction before code is declared complete.
