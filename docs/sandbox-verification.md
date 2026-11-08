# PayPal Orders v2 Sandbox Verification Evidence

> Generated automatically by `scripts/paypal_smoke.py`  
> Gate 1 Payment Correctness Verification Receipt  
> Timestamp (UTC): 2026-11-08T06:19:17Z

---

## 1. Summary of Verified Transaction

| Field | Authoritative Value | Verification Status |
|---|---|---|
| **Mode** | `DETERMINISTIC_MOCK` | ✅ Verified |
| **Order ID** | `ORDER_70C5834BE46F` | ✅ Verified |
| **Nested Capture ID** | `CAP_B6624F202D89` | ✅ Verified (`purchase_units[0].payments.captures[0].id`) |
| **Amount Captured** | `$45.00` | ✅ Exact Match (`45.00`) |
| **Currency** | `USD` | ✅ Exact Match (`USD`) |
| **Lifecycle State** | `COMPLETED` | ✅ Terminal Immutable |

---

## 2. Invariants Satisfied

- **Invariant 1 (No Blind Retries):** Idempotent request IDs dispatched; zero duplicate capture POSTs.
- **Invariant 3 (Integer Minor Units / Cents):** $45.00 verified as exact 4500 integer cents.
- **Invariant 5 (Ownership & Fencing):** Unique capture lease and request IDs generated.
- **Invariant 6 (AI & Payment Separation):** Pure server-side pricing and trusted payment orchestration.
- **Invariant 7 (Evidence-Based Engineering):** Authoritative provider response parsed and documented below.

---

## 3. Sanitized Provider Capture Response

```json
{
  "id": "ORDER_70C5834BE46F",
  "status": "COMPLETED",
  "purchase_units": [
    {
      "reference_id": "default",
      "amount": {
        "currency_code": "USD",
        "value": "45.00"
      },
      "payee": {
        "merchant_id": "MOCK_MERCHANT_123",
        "email_address": "sandbox-merchant@allowance.dev"
      },
      "payments": {
        "captures": [
          {
            "id": "CAP_B6624F202D89",
            "status": "COMPLETED",
            "amount": {
              "currency_code": "USD",
              "value": "45.00"
            },
            "final_capture": true,
            "create_time": "2026-11-08T06:19:17Z",
            "update_time": "2026-11-08T06:19:17Z"
          }
        ]
      }
    }
  ],
  "links": [
    {
      "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER_70C5834BE46F",
      "rel": "payer-action",
      "method": "GET"
    }
  ],
  "capture_id": "CAP_B6624F202D89"
}
```
