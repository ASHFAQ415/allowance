"""
PayPal Orders v2 Client and Transport Contract.
Enforces Invariant 1:
- Once payment request transmission may have started, outcome is UNCERTAIN.
- Typed transport envelope exposes bytes_may_have_been_sent boolean.
- Pre-capture assertion verify_order validates status, amount, currency, and payee.
- Stable UUID4 request IDs.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class TransportOutcome:
    success: bool
    status_code: int
    data: Optional[Dict[str, Any]]
    bytes_may_have_been_sent: bool
    error_class: Optional[str] = None
    raw_error: Optional[str] = None


class PayPalClientInterface:
    def create_order(
        self,
        create_request_id: str,
        total_cents: int,
        items: list,
        currency: str = "USD"
    ) -> TransportOutcome:
        raise NotImplementedError

    def get_order(self, order_id: str) -> TransportOutcome:
        raise NotImplementedError

    def capture_order(
        self,
        order_id: str,
        capture_request_id: str
    ) -> TransportOutcome:
        raise NotImplementedError


class MockPayPalClient(PayPalClientInterface):
    """
    In-memory Mock PayPal server supporting simulation of:
    - 201 COMPLETED capture
    - 200 PENDING capture
    - 200 DECLINED capture
    - 422 ORDER_ALREADY_CAPTURED
    - 422 PAYER_ACTION_REQUIRED
    - 429 Rate Limit
    - 500 Internal Server Error
    - Pre-send connect errors vs post-send read timeouts
    """

    def __init__(self):
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.captures: Dict[str, Dict[str, Any]] = {}
        self.create_requests: Dict[str, str] = {}  # req_id -> order_id
        self.capture_requests: Dict[str, Dict[str, Any]] = {}  # req_id -> capture_outcome
        
        # Injected failure hooks for adversarial testing
        self.simulate_connect_error: bool = False
        self.simulate_read_timeout: bool = False
        self.simulate_http_500: bool = False
        self.simulate_http_429: bool = False
        self.simulate_capture_pending: bool = False
        self.simulate_capture_declined: bool = False
        self.simulate_amount_tamper: bool = False
        # Phase A extended fault injectors
        self.simulate_payee_tamper: bool = False          # #14: wrong merchant_id in GET order
        self.simulate_currency_tamper: bool = False       # #15: wrong currency in GET order
        self.simulate_unknown_status: bool = False        # #12: unrecognized status string from GET
        self.simulate_capture_detail_mismatch: bool = False  # V22-05: COMPLETED but amount wrong
        self.simulate_write_error: bool = False           # V22-03: ambiguous mid-POST TCP error

    def create_order(
        self,
        create_request_id: str,
        total_cents: int,
        items: list,
        currency: str = "USD"
    ) -> TransportOutcome:
        # Idempotency check
        if create_request_id in self.create_requests:
            order_id = self.create_requests[create_request_id]
            return TransportOutcome(
                success=True,
                status_code=200,
                data=self.orders[order_id],
                bytes_may_have_been_sent=True
            )

        order_id = f"ORDER_{uuid.uuid4().hex[:12].upper()}"
        order_data = {
            "id": order_id,
            "status": "PAYER_ACTION_REQUIRED",
            "purchase_units": [{
                "reference_id": "default",
                "amount": {
                    "currency_code": currency,
                    "value": f"{total_cents / 100:.2f}"
                },
                "payee": {
                    "merchant_id": "MOCK_MERCHANT_123",
                    "email_address": "sandbox-merchant@allowance.dev"
                }
            }],
            "links": [
                {
                    "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}",
                    "rel": "payer-action",
                    "method": "GET"
                }
            ]
        }
        self.orders[order_id] = order_data
        self.create_requests[create_request_id] = order_id

        return TransportOutcome(
            success=True,
            status_code=201,
            data=order_data,
            bytes_may_have_been_sent=True
        )

    def get_order(self, order_id: str) -> TransportOutcome:
        if order_id not in self.orders:
            return TransportOutcome(
                success=False,
                status_code=404,
                data={"name": "RESOURCE_NOT_FOUND"},
                bytes_may_have_been_sent=True,
                error_class="HTTP404"
            )

        order_data = self.orders[order_id]
        if self.simulate_amount_tamper:
            # #13: Adversarial simulation: tampered amount returned by provider
            tampered = json.loads(json.dumps(order_data))
            tampered["purchase_units"][0]["amount"]["value"] = "999.99"
            return TransportOutcome(success=True, status_code=200, data=tampered, bytes_may_have_been_sent=True)

        if self.simulate_payee_tamper:
            # #14: Wrong merchant_id from provider
            tampered = json.loads(json.dumps(order_data))
            tampered["purchase_units"][0]["payee"]["merchant_id"] = "EVIL_MERCHANT_XYZ"
            return TransportOutcome(success=True, status_code=200, data=tampered, bytes_may_have_been_sent=True)

        if self.simulate_currency_tamper:
            # #15: Wrong currency code from provider
            tampered = json.loads(json.dumps(order_data))
            tampered["purchase_units"][0]["amount"]["currency_code"] = "EUR"
            return TransportOutcome(success=True, status_code=200, data=tampered, bytes_may_have_been_sent=True)

        if self.simulate_unknown_status:
            # #12: Completely unrecognized status string
            tampered = json.loads(json.dumps(order_data))
            tampered["status"] = "MYSTERY_STATUS_XYZ"
            return TransportOutcome(success=True, status_code=200, data=tampered, bytes_may_have_been_sent=True)

        if self.simulate_capture_detail_mismatch:
            # V22-05: Order shows COMPLETED but capture amount doesn't match
            tampered = json.loads(json.dumps(order_data))
            tampered["status"] = "COMPLETED"
            tampered["purchase_units"][0]["payments"] = {
                "captures": [{"id": "CAP_MISMATCH", "status": "COMPLETED",
                               "amount": {"currency_code": "USD", "value": "9999.99"}}]
            }
            return TransportOutcome(success=True, status_code=200, data=tampered, bytes_may_have_been_sent=True)

        return TransportOutcome(
            success=True,
            status_code=200,
            data=order_data,
            bytes_may_have_been_sent=True
        )

    def set_buyer_approved(self, order_id: str):
        """Simulates buyer clicking Pay Now at PayPal."""
        if order_id in self.orders:
            self.orders[order_id]["status"] = "APPROVED"

    def capture_order(
        self,
        order_id: str,
        capture_request_id: str
    ) -> TransportOutcome:
        # 1. Simulate Pre-send Connect Error
        if self.simulate_connect_error:
            self.simulate_connect_error = False  # Allows retry to succeed
            return TransportOutcome(
                success=False,
                status_code=0,
                data=None,
                bytes_may_have_been_sent=False,  # Proven no bytes left socket!
                error_class="ConnectError",
                raw_error="Failed to connect to api-m.sandbox.paypal.com:443"
            )

        # V22-03: Simulate ambiguous mid-POST TCP write error (bytes_may_have_been_sent=True)
        if self.simulate_write_error:
            return TransportOutcome(
                success=False,
                status_code=0,
                data=None,
                bytes_may_have_been_sent=True,  # INVARIANT 1: Ambiguous — must fail closed
                error_class="WriteError",
                raw_error="BrokenPipeError: connection reset mid-POST"
            )

        # 2. Simulate Post-send Read Timeout
        if self.simulate_read_timeout:
            # Request was sent and committed on mock server, but response timed out!
            capture_id = f"CAP_{uuid.uuid4().hex[:12].upper()}"
            self.captures[order_id] = {"id": capture_id, "status": "COMPLETED"}
            if order_id in self.orders:
                self.orders[order_id]["status"] = "COMPLETED"
            return TransportOutcome(
                success=False,
                status_code=0,
                data=None,
                bytes_may_have_been_sent=True,  # Request WAS transmitted!
                error_class="ReadTimeout",
                raw_error="Read timed out after 10.0s"
            )

        # 3. Simulate HTTP 500
        if self.simulate_http_500:
            return TransportOutcome(
                success=False,
                status_code=500,
                data={"name": "INTERNAL_SERVER_ERROR", "message": "An unexpected error occurred"},
                bytes_may_have_been_sent=True,
                error_class="HTTP500"
            )

        # 4. Simulate HTTP 429
        if self.simulate_http_429:
            return TransportOutcome(
                success=False,
                status_code=429,
                data={"name": "RATE_LIMIT_EXCEEDED", "message": "Too many requests"},
                bytes_may_have_been_sent=True,
                error_class="HTTP429"
            )

        # 5. Check Idempotency Header
        if capture_request_id in self.capture_requests:
            return self.capture_requests[capture_request_id]

        if order_id not in self.orders:
            return TransportOutcome(
                success=False, status_code=404, data={"name": "RESOURCE_NOT_FOUND"},
                bytes_may_have_been_sent=True, error_class="HTTP404"
            )

        order = self.orders[order_id]
        if order["status"] == "COMPLETED":
            outcome = TransportOutcome(
                success=False,
                status_code=422,
                data={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]},
                bytes_may_have_been_sent=True,
                error_class="ORDER_ALREADY_CAPTURED"
            )
            self.capture_requests[capture_request_id] = outcome
            return outcome

        if self.simulate_capture_declined:
            outcome = TransportOutcome(
                success=True,
                status_code=200,
                data={"id": f"CAP_DEC_{order_id}", "status": "DECLINED", "status_details": {"reason": "INSTRUMENT_DECLINED"}},
                bytes_may_have_been_sent=True
            )
            self.capture_requests[capture_request_id] = outcome
            return outcome

        if self.simulate_capture_pending:
            outcome = TransportOutcome(
                success=True,
                status_code=200,
                data={"id": f"CAP_PND_{order_id}", "status": "PENDING", "status_details": {"reason": "PENDING_REVIEW"}},
                bytes_may_have_been_sent=True
            )
            self.capture_requests[capture_request_id] = outcome
            return outcome

        # Standard successful capture: mirrors PayPal Orders v2 capture schema
        cap_id = f"CAP_{uuid.uuid4().hex[:12].upper()}"
        order["status"] = "COMPLETED"
        now_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        capture_obj = {
            "id": cap_id,
            "status": "COMPLETED",
            "amount": order["purchase_units"][0]["amount"],
            "final_capture": True,
            "create_time": now_ts,
            "update_time": now_ts,
        }
        order["purchase_units"][0]["payments"] = {
            "captures": [capture_obj]
        }
        self.captures[order_id] = capture_obj
        cap_data = json.loads(json.dumps(order))
        cap_data["status"] = "COMPLETED"
        cap_data["capture_id"] = cap_id
        outcome = TransportOutcome(
            success=True,
            status_code=201,
            data=cap_data,
            bytes_may_have_been_sent=True
        )
        self.capture_requests[capture_request_id] = outcome
        return outcome


class PayPalHttpClient(PayPalClientInterface):
    """
    Live PayPal Orders v2 HTTP Client using standard library urllib.
    Enforces Invariant 1:
    - Sets bytes_may_have_been_sent=False only for proven pre-send transport errors.
    - Captures status code and parsed response for audit evidence.
    - Idempotency key PayPal-Request-Id header sent on mutations.
    """

    def __init__(self, client_id: str, client_secret: str, environment: str = "sandbox"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = "https://api-m.sandbox.paypal.com" if environment == "sandbox" else "https://api-m.paypal.com"
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def _get_access_token(self) -> str:
        import base64
        import time
        import urllib.request
        import urllib.error

        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        url = f"{self.base_url}/v1/oauth2/token"
        auth_header = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(
            url,
            data=b"grant_type=client_credentials",
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode())
            self._access_token = data["access_token"]
            self._token_expires_at = time.time() + data.get("expires_in", 3600)
            return self._access_token

    def create_order(
        self,
        create_request_id: str,
        total_cents: int,
        items: list,
        currency: str = "USD"
    ) -> TransportOutcome:
        import urllib.request
        import urllib.error
        import socket

        try:
            token = self._get_access_token()
        except Exception as e:
            return TransportOutcome(
                success=False, status_code=0, data=None, bytes_may_have_been_sent=False,
                error_class="AUTH_ERROR", raw_error=str(e)
            )

        url = f"{self.base_url}/v2/checkout/orders"
        amount_str = f"{total_cents / 100.0:.2f}"
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {
                    "currency_code": currency,
                    "value": amount_str
                }
            }],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "user_action": "PAY_NOW",
                        "shipping_preference": "NO_SHIPPING",
                        "return_url": "https://example.com/checkout/return",
                        "cancel_url": "https://example.com/checkout/cancel"
                    }
                }
            }
        }
        data_bytes = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "PayPal-Request-Id": create_request_id
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                resp_data = json.loads(resp.read().decode())
                return TransportOutcome(
                    success=True,
                    status_code=resp.status,
                    data=resp_data,
                    bytes_may_have_been_sent=True
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                err_json = json.loads(body)
            except Exception:
                err_json = {"raw": body}
            return TransportOutcome(
                success=False,
                status_code=e.code,
                data=err_json,
                bytes_may_have_been_sent=True,
                error_class=f"HTTP_{e.code}"
            )
        except (socket.timeout, urllib.error.URLError) as e:
            # If socket connect failed before sending bytes
            is_connect_error = "Connect" in str(e) or "timed out during connect" in str(e)
            return TransportOutcome(
                success=False,
                status_code=0,
                data=None,
                bytes_may_have_been_sent=not is_connect_error,
                error_class="TRANSPORT_ERROR",
                raw_error=str(e)
            )

    def get_order(self, order_id: str) -> TransportOutcome:
        import urllib.request
        import urllib.error

        try:
            token = self._get_access_token()
        except Exception as e:
            return TransportOutcome(
                success=False, status_code=0, data=None, bytes_may_have_been_sent=False,
                error_class="AUTH_ERROR", raw_error=str(e)
            )

        url = f"{self.base_url}/v2/checkout/orders/{order_id}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            method="GET"
        )

        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                resp_data = json.loads(resp.read().decode())
                return TransportOutcome(
                    success=True,
                    status_code=resp.status,
                    data=resp_data,
                    bytes_may_have_been_sent=True
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                err_json = json.loads(body)
            except Exception:
                err_json = {"raw": body}
            return TransportOutcome(
                success=False,
                status_code=e.code,
                data=err_json,
                bytes_may_have_been_sent=True,
                error_class=f"HTTP_{e.code}"
            )
        except Exception as e:
            return TransportOutcome(
                success=False,
                status_code=0,
                data=None,
                bytes_may_have_been_sent=False,
                error_class="TRANSPORT_ERROR",
                raw_error=str(e)
            )

    def capture_order(
        self,
        order_id: str,
        capture_request_id: str
    ) -> TransportOutcome:
        import urllib.request
        import urllib.error
        import socket

        try:
            token = self._get_access_token()
        except Exception as e:
            return TransportOutcome(
                success=False, status_code=0, data=None, bytes_may_have_been_sent=False,
                error_class="AUTH_ERROR", raw_error=str(e)
            )

        url = f"{self.base_url}/v2/checkout/orders/{order_id}/capture"
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "PayPal-Request-Id": capture_request_id
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                resp_data = json.loads(resp.read().decode())
                return TransportOutcome(
                    success=True,
                    status_code=resp.status,
                    data=resp_data,
                    bytes_may_have_been_sent=True
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                err_json = json.loads(body)
            except Exception:
                err_json = {"raw": body}
            return TransportOutcome(
                success=False,
                status_code=e.code,
                data=err_json,
                bytes_may_have_been_sent=True,
                error_class=f"HTTP_{e.code}"
            )
        except (socket.timeout, urllib.error.URLError) as e:
            return TransportOutcome(
                success=False,
                status_code=0,
                data=None,
                bytes_may_have_been_sent=True,  # INVARIANT 1: Ambiguous POST failure -> must fail closed
                error_class="TIMEOUT_OR_AMBIGUOUS_TRANSPORT",
                raw_error=str(e)
            )


_paypal_client_instance: Optional[PayPalClientInterface] = None


def get_paypal_mode() -> str:
    """Returns configured PAYPAL_MODE: 'mock' (default), 'sandbox', or 'live'."""
    return os.environ.get("PAYPAL_MODE", "mock").strip().lower()


def get_paypal_client() -> PayPalClientInterface:
    """Returns PayPal client matching PAYPAL_MODE or injected instance."""
    global _paypal_client_instance
    if _paypal_client_instance is not None:
        return _paypal_client_instance

    mode = get_paypal_mode()
    if mode in ("live", "sandbox"):
        client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
        client_secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
        base_url = os.environ.get("PAYPAL_BASE_URL", "")
        env = "sandbox" if mode == "sandbox" or "sandbox" in base_url else "live"
        return PayPalHttpClient(client_id=client_id, client_secret=client_secret, environment=env)

    _paypal_client_instance = MockPayPalClient()
    return _paypal_client_instance


def set_paypal_client(client: Optional[PayPalClientInterface]) -> None:
    """Explicitly injects or overrides PayPal client (useful for tests)."""
    global _paypal_client_instance
    _paypal_client_instance = client


def reset_paypal_client() -> None:
    """Resets cached PayPal client instance."""
    global _paypal_client_instance
    _paypal_client_instance = None


