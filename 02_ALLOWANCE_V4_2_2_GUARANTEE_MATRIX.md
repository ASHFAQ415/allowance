# ALLOWANCE v4.2.2 — GUARANTEE & FAILURE ANALYSIS MATRIX

**Status:** Normative; replaces v4.2 guarantee wording where inconsistent.  
**Important:** This maps design intent to enforcement and tests. It does not certify implementation.

| ID | Guarantee / invariant | Classification | Enforcement | Failure behavior | Required test |
|---|---|---|---|---|---|
| G1 | LLM cannot execute payments or set financial facts | Application boundary | Import rules, zero-arg submit_cart, server catalog pricing | Invalid proposal rejected | agent isolation, price tampering |
| G2 | Agent execution is bounded | Application | Six model calls, 45s loop deadline, repeat detector | Deterministic fallback or terminal run | loop guard, outage eval |
| G3 | Daily budget admission is serialized | SQLite + application | BEGIN IMMEDIATE, financial facts, projection and policy snapshot | Reject without reservation | concurrent budget race |
| G4 | Active reservation cap is serialized | SQLite + application | Same admission transaction | Reject before purchase insert | reservation flood |
| G5 | One logical capture operation per purchase | SQLite + application | Unique purchase_id and request_id | Duplicate caller gets current operation | concurrent capture |
| G6 | No capture retry after possible transmission | Application + transport evidence | Typed transport outcome; MAY_HAVE_BEEN_SENT marker | CAPTURE_UNKNOWN; hold funds | V22-01/03/04 |
| G7 | A proven pre-send failure may retry at most once | Application + transport evidence | PREPARED state and explicit transmission_started=false | No retry if uncertain | V22-02 |
| G8 | Reconciliation is single-flight and read-only | SQLite + application | Owner CAS/lease; GET only | Competing callers cannot resolve | #7, V22-14/15 |
| G9 | Provider success is verified | PayPal + application | Validate order/capture ID, status, amount, currency, payee, reference | Keep UNKNOWN on mismatch | V22-05 |
| G10 | Unknown capture remains financially committed | Application + journal | unresolved financial fact | No release on timeout/APPROVED/exhaustion | V22-01/09 |
| G11 | Late capture does not mutate terminal workflow | Application + DB constraints | adjustment + financial fact; no fake ledger transition | Purchase stays FAILED(unresolved_capture) | #6, V22-07 |
| G12 | Financial projection is reconstructible | Application + SQLite | Append-only financial_facts; deterministic rebuild | Alert and controlled repair | V22-10 |
| G13 | Workflow ledger is immutable | SQLite | UPDATE/DELETE abort triggers; unique transition constraints | Transaction rejected | ledger immutability |
| G14 | Purchase transitions are monotonic | Application + DB | State transition matrix and rank validation | Illegal transition rejected | state machine matrix |
| G15 | Queue memory and admission are bounded | Application + SQLite | Four workers, four waiters, eight admitted runs | 9th run rejected before insert | #21, V22-16 |
| G16 | Expired queued run never starts | SQLite + application | Deadline inside atomic QUEUED→RUNNING claim | Mark queue_timeout | #22, V22-17 |
| G17 | Per-agent active run cap is two | SQLite + application | Admission transaction counts QUEUED + RUNNING | Reject excess | concurrent per-agent test |
| G18 | Stale workers cannot mutate run/purchase | Application + SQLite | Worker ownership/status CAS | Drop result and log | #28, V22-14/16 |
| G19 | Data routes are demo-session and tenant scoped | Application | Configured demo tenant; session/admin auth | 401/403 or generic 404 | V22-18 |
| G20 | Public DTOs exclude payment secrets and provider IDs | Application | Explicit Pydantic allowlists | Sensitive fields never serialized | #33, DTO tests |
| G21 | Admin actions are authenticated and audited | Application | X-Admin-Token constant-time compare, rate limit | Deny and audit auth failure | auth tests |
| G22 | SQLite transactions are atomic/durable within deployment constraints | SQLite + infrastructure | WAL, FULL sync, persistent volume, one process | 503 on lock; recovery after restart | SIGKILL, lock exhaustion |
| G23 | Provider availability and idempotency behavior are external dependencies | PayPal | Provider contract + live smoke | Preserve UNKNOWN; no blind retry | PayPal F suite |
| G24 | No distributed exactly-once guarantee is claimed | Explicit non-guarantee | Product language / Devpost review | Correct claims before release | copy/claim review |

## 1. Financial definitions

`R3_committed = captured_cents + reserved_cents + unresolved_cents`.

`auto_approved_committed_cents` is a subset of that amount and is used only by R5. It is never added again to R3.

## 2. Classification meanings

- **Application:** Depends on correct application code and tests.
- **SQLite:** Depends on documented SQLite transaction/locking/trigger behavior and correct single-process deployment.
- **PayPal:** Depends on external provider response semantics and availability.
- **Infrastructure:** Depends on persistent disk, process supervision, secrets and network.
- **Best effort:** Alerting, rate limits, and operational response reduce risk but cannot guarantee prevention.
- **Not guaranteed:** Explicitly outside the product contract.

## 3. Prohibited marketing claims

Do not say “exactly once payments,” “zero risk,” “foolproof,” “100% secure,” “immune to prompt injection,” or “production-ready” solely because the specification exists. Say “deterministic authorization boundary,” “single logical capture initiation,” “conservative handling of ambiguous payment outcomes,” and “append-only audit trail,” with implementation status disclosed.
