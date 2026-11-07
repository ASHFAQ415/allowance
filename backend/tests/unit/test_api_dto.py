"""
Unit tests for API DTOs, Security Validation, and Rate Limiting.
Enforces Invariant 6 (AI & Payment Authority Separation) and Administrative Security.
"""

import time
import unittest
from backend.app.api.dto import (
    PublicCartItemDTO,
    PublicPurchaseDTO,
    PublicLedgerDTO,
    PublicPolicyDTO,
    AdminPurchaseDetailDTO,
    AdminApprovalLinkDTO,
    RunAgentRequest,
)
from backend.app.api.auth import (
    verify_admin_token,
    get_demo_tenant,
    enforce_rate_limit,
    get_client_ip,
    ADMIN_TOKEN,
    DEMO_SESSION_TOKEN,
    _rate_limits,
    _auth_failures,
)

# Lightweight request mock
class DummyRequest:
    def __init__(self, ip="127.0.0.1", headers=None):
        self.client = type("Client", (), {"host": ip})
        self.headers = headers or {}


class TestApiDtoAndSecurity(unittest.TestCase):
    def setUp(self):
        _rate_limits.clear()
        _auth_failures.clear()

    def test_public_purchase_dto_redacts_sensitive_paypal_fields(self):
        """
        Public purchase DTO must NEVER contain or expose paypal_order_id,
        paypal_capture_id, approval_url, request_id, or internal trace_json.
        """
        public_dto = PublicPurchaseDTO(
            id="pur_123",
            agent_id="shopmind_1",
            status="CAPTURED",
            status_reason="Payment completed successfully",
            auto_approved=True,
            total_cents=4500,
            currency="USD",
            merchant_ids=["merch_shoes_direct"],
            budget_day="2026-11-06",
            created_at="2026-11-06T12:00:00Z",
            updated_at="2026-11-06T12:01:00Z"
        )
        data = public_dto.model_dump() if hasattr(public_dto, "model_dump") else public_dto.dict()
        
        self.assertNotIn("paypal_order_id", data)
        self.assertNotIn("paypal_capture_id", data)
        self.assertNotIn("approval_url", data)
        self.assertNotIn("request_id", data)
        self.assertNotIn("trace_json", data)
        self.assertEqual(data["id"], "pur_123")
        self.assertEqual(data["total_cents"], 4500)

    def test_admin_dto_contains_operational_fields_safely(self):
        """Admin DTO exposes approval links and IDs only to authorized admins."""
        admin_link = AdminApprovalLinkDTO(
            purchase_id="pur_123",
            paypal_order_id="ORDER_ABC_999",
            approval_url="https://www.sandbox.paypal.com/checkoutnow?token=ORDER_ABC_999",
            expires_at="2026-11-06T12:15:00Z"
        )
        data = admin_link.model_dump() if hasattr(admin_link, "model_dump") else admin_link.dict()
        self.assertEqual(data["purchase_id"], "pur_123")
        self.assertEqual(data["paypal_order_id"], "ORDER_ABC_999")
        self.assertIn("sandbox.paypal.com", data["approval_url"])

    def test_admin_token_verification_success(self):
        """Valid admin token succeeds."""
        req = DummyRequest()
        token = verify_admin_token(request=req, token=ADMIN_TOKEN)
        self.assertTrue(token)

    def test_admin_token_verification_rejects_missing_or_invalid(self):
        """Missing or malformed admin token raises 401 Unauthorized."""
        from backend.app.api.auth import HTTPException
        req = DummyRequest()
        
        with self.assertRaises(HTTPException) as ctx:
            verify_admin_token(request=req, token=None)
        self.assertEqual(ctx.exception.status_code, 401)

        with self.assertRaises(HTTPException) as ctx2:
            verify_admin_token(request=req, token="wrong-token-abc")
        self.assertEqual(ctx2.exception.status_code, 401)

    def test_admin_token_lockout_after_consecutive_failures(self):
        """5 consecutive invalid auth attempts triggers a 429 lockout."""
        from backend.app.api.auth import HTTPException
        req = DummyRequest()
        
        for _ in range(5):
            with self.assertRaises(HTTPException):
                verify_admin_token(request=req, token="invalid_guess")
        
        # 6th attempt is blocked with 429
        with self.assertRaises(HTTPException) as ctx:
            verify_admin_token(request=req, token="invalid_guess")
        self.assertEqual(ctx.exception.status_code, 429)

    def test_demo_tenant_scoping(self):
        """Session token maps cleanly to default demo tenant."""
        req = DummyRequest()
        tenant = get_demo_tenant(request=req, token=DEMO_SESSION_TOKEN)
        self.assertEqual(tenant, "tenant_demo")

    def test_sliding_window_rate_limiting(self):
        """Rate limiting allows up to limit requests and throttles excess."""
        from backend.app.api.auth import HTTPException
        
        req = DummyRequest(ip="192.168.1.100")
        
        # 3 calls permitted with limit=3
        enforce_rate_limit(req, limit=3, window_seconds=60)
        enforce_rate_limit(req, limit=3, window_seconds=60)
        enforce_rate_limit(req, limit=3, window_seconds=60)
        
        # 4th call raises 429
        with self.assertRaises(HTTPException) as ctx:
            enforce_rate_limit(req, limit=3, window_seconds=60)
        self.assertEqual(ctx.exception.status_code, 429)


if __name__ == "__main__":
    unittest.main()
