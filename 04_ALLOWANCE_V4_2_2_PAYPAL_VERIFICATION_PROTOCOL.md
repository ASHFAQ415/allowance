# ALLOWANCE v4.2.2 — PAYPAL ORDERS V2 VERIFICATION PROTOCOL

**Purpose:** Provider contract boundary and live sandbox evidence record.  
**Rule:** This document distinguishes documented behavior, locally tested behavior, live sandbox observations and unresolved assumptions.

## 1. Integration rules

- Use PayPal Orders v2.
- Use `payment_source.paypal.experience_context` with configured return/cancel URLs and buyer action.
- Extract `payer-action` link when present; fallback to `approve` only if the actual response contract supports it.
- Treat initial `PAYER_ACTION_REQUIRED` as awaiting buyer consent, not failure.
- Use UUID4 `PayPal-Request-Id` values within provider length limits.
- Verify order status, order ID, purchase reference, amount, currency and payee before capture.
- Do not trust browser query parameters as proof of approval; perform server-to-server GET verification.
- PayPal client never mutates local DB.

## 2. Capture contract

A capture POST is issued only after P4 and exclusive claim. The same request ID is persisted. If request transmission may have started, do not issue another capture POST. Reconciliation is GET-only.

A GET order status `APPROVED` after ambiguous capture does not prove the earlier capture request cannot complete. Keep `CAPTURE_UNKNOWN` and hold budget. Only a validated `COMPLETED` capture resource resolves success. A definitive no-settlement state may release funds only when provider semantics establish that no capture can later settle.

## 3. Outcome table

| Provider observation | Local handling |
|---|---|
| Valid `COMPLETED` capture with matching amount/currency/payee/order | Resolve captured |
| `PENDING` | UNKNOWN; hold budget |
| `APPROVED` after ambiguous capture, no matching capture resource | UNKNOWN; no capture POST |
| `ORDER_ALREADY_CAPTURED` | GET order and validate capture; never retry POST |
| `PAYER_ACTION_REQUIRED` | Classify only from exact provider issue semantics; do not equate all 422s with decline |
| 429 / 5xx / timeout / malformed body | UNKNOWN unless proven pre-send before any bytes |
| Unknown status or mismatched provider fields | UNKNOWN + alert |

## 4. Required live smoke checks

Preserve v4.2 V1–V9 but record actual date, endpoint, request shape, redacted response, status, observation and code commit for each:
- V1 payer-action link relation and URL extraction.
- V2 initial status and buyer-return status.
- V3 create-order idempotency replay.
- V4 capture idempotency replay (diagnostic only; not permission to retry ambiguous captures).
- V5 NO_SHIPPING behavior for seeded goods.
- V6 PENDING response shape and status_details.
- V7 payee identity field and configured match.
- V8 return URL query parameters; ignore untrusted payer identifiers.
- V9 GET order behavior/latency after a deliberately delayed capture.

Add:
- V10 ambiguous capture followed by GET APPROVED: confirm no second capture POST is sent by Allowance.
- V11 delayed/lost response later becomes COMPLETED: verify GET reconciliation resolves exactly once.
- V12 capture response/GET mismatch: verify amount/currency/payee mismatch remains unresolved and raises alert.

## 5. Evidence template

For each test record:
- date/time UTC;
- sandbox account type (never credentials);
- app commit SHA;
- endpoint and method;
- redacted request headers/body;
- redacted response status/body;
- provider request/correlation ID if safe for internal evidence;
- local operation ID;
- local state before/after;
- capture POST count and GET count;
- result and reviewer;
- whether the result is documented, sandbox-observed, or still assumed.

Never store access tokens, client secrets, buyer passwords, full approval URLs, or unredacted payment payloads in committed docs.
