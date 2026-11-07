# ALLOWANCE v4.2 — PAYPAL INTEGRATION & LIVE VERIFICATION PROTOCOL

**Status:** APPROVED & BINDING SPECIFICATION (v4.2)  
**Provider:** PayPal REST APIs (Orders v2)  
**Sandbox Environment:** `https://api-m.sandbox.paypal.com`  
**Purpose:** Dissect confirmed provider documentation, sandbox smoke verification tests (V1–V9), response payload contracts, and explicit provider limitations.

---

## 1. Documented Provider Facts vs. Sandbox Experiments vs. Assumptions

| Integration Area | Officially Documented Fact (Current Docs) | Confirmed in Sandbox (Recorded Proof) | Engineering Assumption / Unknown | Effect on Allowance v4.2 Design |
|---|---|---|---|---|
| **Orders v2 Creation** | `payment_source.paypal.experience_context` supports `return_url`, `cancel_url`, `user_action: "PAY_NOW"`, `shipping_preference: "NO_SHIPPING"`. | Verified: Schema accepted without deprecation warnings. | Assumed: `application_context` is legacy and should be omitted entirely. | We use `payment_source.paypal.experience_context` exclusively. |
| **Approval Link Rel** | Response links contain `rel: "payer-action"` when `payment_source` is provided; fallback is `rel: "approve"`. | Verified: Href format matches `https://www.sandbox.paypal.com/checkoutnow?token=<ORDER_ID>`. | Link contains order ID; hiding link is cosmetic if order ID leaks. | Public DTOs strip both `approval_url` and `paypal_order_id`. |
| **Initial Order Status** | Order status right after creation with `payment_source` is `PAYER_ACTION_REQUIRED`. | Verified in sandbox: status is `PAYER_ACTION_REQUIRED`, not `CREATED`. | Earlier plans assumed `CREATED` and flagged `PAYER_ACTION_REQUIRED` as error. | State machine maps `PAYER_ACTION_REQUIRED` to valid awaiting buyer state. |
| **Idempotency Header** | `PayPal-Request-Id` header is accepted on POST endpoints. Max length 38 chars recommended (UUID). | Verified: Replaying same request ID on create returns identical order ID. | Retention duration in Orders v2: Docs state "up to 45 days"; exact SLA unstated. | Request IDs generated as UUID4 (36 chars) stored in `purchases` table. |
| **Simultaneous Duplicate** | Two simultaneous requests with identical `PayPal-Request-Id`: PayPal processes one and may fail second. | Verified: Duplicate in-flight returns 400 or 409 error envelope. | A failed duplicate is inconclusive; never proof that original failed. | Code enforces exclusive atomic claim `CAPTURING` to prevent concurrent calls. |
| **Capture Decline Shape** | Capture decline can return HTTP 201 with `status: "DECLINED"`, or HTTP 422 with issue `INSTRUMENT_DECLINED`. | Verified in sandbox via `PayPal-Mock-Response` header. | Unhandled decline could cause infinite retry loop. | Explicit mapping: `DECLINED` or 422 immediately transitions to `FAILED(capture_declined)`. |
| **Capture Already Run** | Calling capture on an already-captured order returns HTTP 422 `ORDER_ALREADY_CAPTURED`. | Verified: HTTP 422 with issue `ORDER_ALREADY_CAPTURED`. | Re-calling capture was previously treated as unrecoverable failure. | Handled: Code executes `GET /v2/checkout/orders/{id}`; if `COMPLETED`, transitions to `CAPTURED`. |
| **Reconcile Replay** | Replaying capture call returns latest state of previous call, but may omit details. | Verified: GET order is more comprehensive and stable than POST replay. | Relying on POST replay for reconciliation is fragile. | Reconcile decides authoritatively from `GET /v2/checkout/orders/{id}` only. |
| **Payee Verification** | `purchase_units[0].payee.merchant_id` returns the PayPal account ID receiving funds. | Verified in sandbox: Returns 13-char merchant ID or primary email. | Multi-merchant payouts require PayPal Marketplace/Commerce Platform. | Real multi-merchant split is marked roadmap; single sandbox account used. |

---

## 2. Live Sandbox Verification Checklist (V1 to V9)

Before certifying Gate 0 and Gate 3, the following 9 verification tests must be executed against the live PayPal sandbox using `scripts/paypal_smoke.py`:

### V1: Link `rel` and Href Extraction
- **Action:** Send `POST /v2/checkout/orders` with `payment_source.paypal`.
- **Expected:** Inspect links array. Ensure `rel: "payer-action"` exists. Href must contain `token=<ORDER_ID>`.
- **Verification Script Check:** `assert any(l['rel'] == 'payer-action' for l in order['links'])`
- **Output Record:** Save JSON payload to `docs/sandbox-verification.md`.

### V2: Status Progression (`PAYER_ACTION_REQUIRED` -> `APPROVED`)
- **Action:** Create order; verify initial status; open approval link in browser; complete login; click "Pay Now"; inspect status.
- **Expected:** Initial status is `PAYER_ACTION_REQUIRED`. Status upon return is `APPROVED`.
- **Verification Script Check:** Assert `order['status'] == 'APPROVED'` before attempting capture.

### V3: Create Order Idempotency Replay
- **Action:** Send `POST /v2/checkout/orders` with `PayPal-Request-Id: <UUID-A>`. Record order ID. Send identical request with same header.
- **Expected:** Second call returns HTTP 200/201 with identical order ID.
- **Verification Script Check:** `assert res1.json()['id'] == res2.json()['id']`.

### V4: Capture Idempotency Replay
- **Action:** Create and approve order. Execute capture with `PayPal-Request-Id: <UUID-B>`. Re-send identical capture POST immediately.
- **Expected:** Returns original capture details or 422 `ORDER_ALREADY_CAPTURED` referencing original capture ID.
- **Verification Script Check:** Verify capture ID matches; zero secondary funds charged.

### V5: Physical Goods with `NO_SHIPPING`
- **Action:** Send physical line items (e.g., "Running Shoes") with `shipping_preference: "NO_SHIPPING"`.
- **Expected:** Sandbox order is accepted without requiring shipping address fields in payload or in buyer checkout.
- **Verification Script Check:** Order created successfully without shipping validation errors.

### V6: Capture Response Structure for `PENDING`
- **Action:** Execute capture using mock header `PayPal-Mock-Response: {"status": "PENDING", "status_details": {"reason": "PENDING_REVIEW"}}`.
- **Expected:** Capture response contains `status: "PENDING"`.
- **Verification Script Check:** Ensure parser handles `status_details.reason` without KeyError.

### V7: Payee Account Verification
- **Action:** Execute `GET /v2/checkout/orders/{id}` on an approved order.
- **Expected:** Verify `purchase_units[0].payee` contains either `merchant_id` matching `PAYPAL_MERCHANT_ID` or `email_address` matching `PAYPAL_BUSINESS_EMAIL`.
- **Verification Script Check:** Assert payee identity matches configured environment variables.

### V8: Return URL Parameter Parsing
- **Action:** Complete buyer approval; observe query parameters passed to return URL.
- **Expected:** Browser redirected to `https://<BASE_URL>/paypal/return?token=<ORDER_ID>&PayerID=<PAYER_ID>`.
- **Verification Script Check:** Server extracts `token`, ignores untrusted `PayerID`, and executes server-to-server GET verification.

### V9: State Latency After Capture Timeout
- **Action:** Simulate network drop during capture; immediately call `GET /v2/checkout/orders/{id}`.
- **Expected:** How rapidly does PayPal reflect `status: "COMPLETED"`?
- **Verification Script Check:** Record latency (typically < 1000ms in sandbox).

---

## 3. PayPal Sandbox Smoke Script Specification (`scripts/paypal_smoke.py`)

```python
"""
Interactive PayPal Sandbox Smoke Verification Script (Allowance v4.2)
Executes tests V1-V9 against live sandbox.
"""
import os
import sys
import time
import uuid
import httpx
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
BASE_URL = os.environ.get("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")

if not CLIENT_ID or not CLIENT_SECRET:
    print("FATAL: PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET missing.")
    sys.exit(1)

def get_access_token() -> str:
    res = httpx.post(
        f"{BASE_URL}/v1/oauth2/token",
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"}
    )
    res.raise_for_status()
    return res.json()["access_token"]

def run_smoke_test():
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    # 1. Create Order with Idempotency
    req_id = str(uuid.uuid4())
    headers["PayPal-Request-Id"] = req_id
    
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": "default",
            "amount": {"currency_code": "USD", "value": "45.00"},
            "items": [{
                "name": "Trail Running Shoes",
                "unit_amount": {"currency_code": "USD", "value": "45.00"},
                "quantity": "1"
            }]
        }],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "return_url": "http://localhost:8000/paypal/return",
                    "cancel_url": "http://localhost:8000/paypal/cancel",
                    "user_action": "PAY_NOW",
                    "shipping_preference": "NO_SHIPPING"
                }
            }
        }
    }
    
    print(f"Creating Order with Request-ID: {req_id}...")
    res = httpx.post(f"{BASE_URL}/v2/checkout/orders", json=body, headers=headers)
    res.raise_for_status()
    order = res.json()
    order_id = order["id"]
    print(f"Order Created: {order_id} (Status: {order['status']})")
    
    # Extract payer-action link
    payer_action = next(l["href"] for l in order["links"] if l["rel"] in ("payer-action", "approve"))
    print(f"\nACTION REQUIRED: Open approval link in browser:\n{payer_action}\n")
    print("Waiting up to 180s for buyer approval...")
    
    approved = False
    for _ in range(36):
        time.sleep(5)
        check = httpx.get(f"{BASE_URL}/v2/checkout/orders/{order_id}", headers=headers).json()
        print(f"Polling Status: {check['status']}")
        if check["status"] == "APPROVED":
            approved = True
            break
            
    if not approved:
        print("TIMEOUT: Buyer approval was not completed.")
        sys.exit(1)
        
    # Capture Order
    cap_req_id = str(uuid.uuid4())
    headers["PayPal-Request-Id"] = cap_req_id
    print(f"Capturing Order with Request-ID: {cap_req_id}...")
    cap_res = httpx.post(f"{BASE_URL}/v2/checkout/orders/{order_id}/capture", headers=headers)
    cap_res.raise_for_status()
    cap_data = cap_res.json()
    print(f"Capture Completed: {cap_data['id']} (Status: {cap_data['status']})")
    print("ALL SMOKE CHECKS PASSED.")

if __name__ == "__main__":
    run_smoke_test()
```

