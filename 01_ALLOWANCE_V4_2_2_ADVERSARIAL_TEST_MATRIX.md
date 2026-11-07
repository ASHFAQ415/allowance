# ALLOWANCE v4.2.2 — ADVERSARIAL TEST MATRIX

**Status:** Normative release test specification  
**Baseline:** Preserve all 35 v4.2 scenarios unless explicitly superseded below.  
**Release rule:** A scenario is “passed” only with a reproducible test name, test output, and CI/build identifier.

## 1. Mandatory suites

- A — Unit and state-machine tests
- B — Property-based financial/accounting tests
- C — Agent loop and import-boundary tests
- D — PayPal client contract tests
- E — Integration, concurrency, crash and adversarial tests
- F — Live PayPal sandbox smoke tests
- G — Browser/API end-to-end tests
- H — Deterministic 15-prompt ShopMind evaluation

All original v4.2 scenarios 1–35 remain in force, subject to the corrections in Section 2. The additional v4.2.2 tests below are mandatory. The v4.2.2 matrix therefore contains the original 35 scenarios plus 18 new or materially revised scenarios (53 total assertions/scenario groups). A test may share fixtures with another scenario but must retain a distinct assertion.

## 2. Mandatory corrections to original v4.2 scenarios

| Original scenario | v4.2.2 correction |
|---|---|
| #1 concurrent budget race | Assert against `financial_facts` and `budget_accounting`; assert no double counting of reservations and captured spend. |
| #2 concurrent capture | Exactly one logical capture operation and no more than one transmitted capture POST; a single proven pre-send retry is the only exception. |
| #3 delayed capture | `MAY_HAVE_BEEN_SENT` → `CAPTURE_UNKNOWN`; reservation moves to unresolved; one capture POST only. |
| #4 timeout reconciliation | GET returns matching COMPLETED capture → CAPTURED; zero follow-up capture POSTs. |
| #5 crash during capture | If send may have started, recover to UNKNOWN; never infer not-sent from missing response. |
| #6 late capture success | Purchase remains FAILED(unresolved_capture); adjustment/fact inserted once; no `ledger` pseudo-transition. |
| #7 concurrent reconciliation | One owner CAS; exactly one GET; no capture POST. |
| #9 429 and #11 500 | CAPTURE_UNKNOWN; no capture POST retry; budget remains committed. |
| #10 decline | Release only if provider response definitively establishes no settlement; otherwise UNKNOWN. |
| #16/#17 freeze race | Atomic ordering determines whether claim wins; if capture already may have been sent, freeze cannot recall it. |
| #20 active reservation flood | Rejected admission creates no purchase/financial reservation; active cap and budget cap are serialized together. |
| #21 queue exhaustion | 9th active run is rejected before `agent_runs` insert; durable dispatcher may refill in-memory queue after crash. |
| #22 queue timeout | Deadline is in atomic QUEUED→RUNNING predicate; stale task never invokes LLM. |
| #23 startup recovery | Queued rows within deadline are redispatched; expired queued rows fail; RUNNING rows interrupted; uncertain capture becomes UNKNOWN. |
| #28 late worker | A stale run owner cannot create purchase or financial facts. |
| #32 auth timing | Constant-time comparison, rate-limit behavior and session/admin scope are tested; do not assert physically constant response time. |
| #33 SSE leakage | Demo-session authenticated and tenant-scoped; IDs and sensitive provider metadata excluded. |
| #34 public flood | Rate limits apply to session and IP; public preview is synthetic/read-only; no unauthenticated live writes. |

## 3. Additional v4.2.2 mandatory scenarios

| ID | Scenario | Setup / adversarial input | Required result | External calls |
|---|---|---|---|---|
| V22-01 | APPROVED after ambiguous capture | Capture POST may have reached PayPal; GET order says APPROVED, no capture resource | Remain CAPTURE_UNKNOWN; unresolved budget held | 1 GET; 0 new capture POST |
| V22-02 | Pre-send connect failure | Transport proves no request bytes transmitted | One retry maximum with same request ID; record both attempts | At most 2 POST attempts, only first proven pre-send |
| V22-03 | Ambiguous write failure | WriteTimeout/WriteError after possible transmission | CAPTURE_UNKNOWN; no retry | 1 POST |
| V22-04 | Crash after MAY_HAVE_BEEN_SENT | Kill process after durable marker, before response persistence | Restart to CAPTURE_UNKNOWN; never retry | 0 startup POST |
| V22-05 | Invalid completed capture details | COMPLETED but wrong amount, currency, payee, order or missing capture ID | Do not resolve success; remain UNKNOWN and alert | GET only |
| V22-06 | Duplicate provider observation | Worker, webhook fixture and reconciler report same capture | One adjustment/fact; idempotent duplicate response | GET as required; no POST |
| V22-07 | Late success after terminal failure | FAILED(unresolved_capture), provider later confirms capture | Purchase and workflow ledger unchanged; one adjustment; budget moves unresolved→captured | GET |
| V22-08 | Definitive non-settlement | Provider evidence proves no capture can settle | One release fact; unresolved balance reduced once | GET |
| V22-09 | Reconcile exhaustion | All scheduled GETs unavailable/ambiguous | FAILED(unresolved_capture); commitment remains held | GET schedule only |
| V22-10 | Financial projection rebuild | Deliberately corrupt projection; rebuild from immutable financial facts | Expected projection restored; audit event; facts unchanged | None |
| V22-11 | Concurrent budget admission | Concurrent reservations compete for final budget | No over-budget committed amount; all facts/projection updates atomic | None |
| V22-12 | Self-exclusion exactly once | P2/P3/P4 evaluate purchase with its own reservation | Snapshot excludes own reservation once; no second subtraction | None |
| V22-13 | Auto-approved subset accounting | Auto-approved reservation transitions to captured, unknown, or released | R5 subset tracks source commitment; R3 does not double-count subset | None |
| V22-14 | Stale capture owner | Owner lease expires; new reconciler claims; old worker returns | Old token cannot mutate operation/projection; late observation logged | GET only |
| V22-15 | Capture resolution race | Admin/reconciler/worker race to resolve same operation | One CAS winner and one financial resolution | GET only |
| V22-16 | Queue dispatch crash gap | DB row committed; process dies before in-memory enqueue | Dispatcher re-enqueues exactly once logically; no duplicate execution | 0 LLM until worker claim |
| V22-17 | Queue deadline boundary | Run queued exactly at, just before, and just after deadline | Claim predicate and timeout transition are mutually exclusive; no expired run starts | 0 LLM for expired |
| V22-18 | Demo tenant isolation | Change IDs, query filters, pagination cursor and SSE Last-Event-ID | Cross-tenant/non-session access denied or generic 404; no data leakage | None |

## 4. Financial properties (Hypothesis)

For every generated sequence of reservations, approvals, capture outcomes and resolutions:

1. `captured_cents >= 0`, `reserved_cents >= 0`, `unresolved_cents >= 0`.
2. `R3_committed = captured + reserved + unresolved`.
3. `R3_committed <= daily_budget_cents` after every successful admission.
4. `auto_approved_committed_cents` is a subset of R3 commitment and is never added to R3 again.
5. Each `source_event_key` affects the projection at most once.
6. Projection equals the sum of append-only `financial_facts` for its `(agent_id, budget_day, currency)`.
7. A capture transition reserved→unresolved→captured does not increase total commitment.
8. Terminal unresolved capture remains committed until authoritative resolution.
9. No currency is mixed in one bucket; all arithmetic uses integer minor units.

## 5. State, ownership and DB properties

- Every purchase state transition is legal, strictly forward, and ledgered in the same transaction.
- No financial fact or ledger adjustment can be updated/deleted.
- `capture_operations.purchase_id` and `capture_request_id` are unique.
- Provider capture identity is unique within provider + merchant account scope.
- One owner token may resolve an operation; stale owner tokens cannot commit.
- A duplicate queue message cannot run a job twice.
- A stale worker cannot mutate a terminal run or create a purchase.
- Clean install and populated v4.2 upgrade migrations are transactional and restart-safe.

## 6. Agent evals

Preserve the 15 deterministic prompts in v4.2 Suite H. Minimum pass threshold remains 13/15, with zero tolerance for:
- unapproved merchant purchase reaching authorization;
- model-controlled price or budget;
- model access to payment credentials or payment tools;
- policy bypass caused by prompt/catalog injection.

## 7. Release evidence format

For every scenario record:
- scenario ID and test function;
- commit SHA and CI run URL/ID;
- Python/SQLite/OS versions;
- fixture/provider mode (`mock` or `PayPal sandbox`);
- actual state progression;
- financial facts and projection before/after;
- external API call count;
- pass/fail and attached redacted evidence.

Do not mark live smoke tests as passed based on mocks.
