"""
Integration Tests for End-to-End Orchestrator Flows.
Tests Checkpoint P1, P2, P3, P4, Auto-Approve, Human Approval, and Freeze.
"""

import os
import tempfile
import time
import unittest

from backend.app.db.connection import create_connection, init_db, transactional_session
from backend.app.db.financial_journal import get_budget_snapshot
from backend.app.policy.rules import Outcome, PolicyConfig, CartItem, FrozenCart
from backend.app.orchestrator.state_machine import PurchaseState
from backend.app.orchestrator.core import (
    submit_cart_to_orchestrator,
    approve_pending_purchase,
    create_paypal_order_for_purchase,
)
from backend.app.orchestrator.capture_lifecycle import execute_capture_flow
from backend.app.paypal.client import MockPayPalClient
from scripts.seed_db import seed_database


class TestEndToEndOrchestrator(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        seed_database(self.db_path)
        self.conn = create_connection(self.db_path)
        self.mock_paypal = MockPayPalClient()

        # Seed run
        self.conn.execute("INSERT INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at) VALUES ('r_test', 'tenant_demo', 'default', 'k_test', 'h_test', 1000.0, 'COMPLETED', '2026');")

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_flow_1_auto_approve_under_50_to_captured(self):
        """
        Flow 1 (Auto-Approve):
        Cart under $50 (Trail Running Shoes $45.00) from approved merchant:
        1. submit -> AUTO_APPROVED (R6)
        2. create_order -> PAYPAL_ORDER_CREATED
        3. buyer approval -> BUYER_APPROVED
        4. capture -> CAPTURED
        """
        cart = FrozenCart(
            items=[CartItem("shoes_trail_runner", "merchant_shoes", "Shoes", 4500, 1)],
            total_cents=4500,
            cart_hash="hash_4500"
        )
        now = time.time()

        # 1. Submit cart
        p_id, state, dec = submit_cart_to_orchestrator(
            self.conn, "tenant_demo", "default", "r_test", cart, now
        )
        self.assertEqual(state, PurchaseState.AUTO_APPROVED)
        self.assertEqual(dec.outcome, Outcome.ALLOW)
        self.assertEqual(dec.rule_name, "R6_AUTO_APPROVE")

        # Check financial reservation
        snapshot = get_budget_snapshot(self.conn, "default", time.strftime("%Y-%m-%d", time.gmtime(now)))
        self.assertEqual(snapshot.reserved_cents, 4500)
        self.assertEqual(snapshot.r3_committed_cents, 4500)

        # 2. PayPal create order
        state_order, approval_url = create_paypal_order_for_purchase(self.conn, p_id, self.mock_paypal)
        self.assertEqual(state_order, PurchaseState.PAYPAL_ORDER_CREATED)
        self.assertIsNotNone(approval_url)

        # 3. Simulate buyer approval at PayPal
        cur = self.conn.execute("SELECT paypal_order_id FROM purchases WHERE id = ?;", (p_id,))
        order_id = cur.fetchone()["paypal_order_id"]
        self.mock_paypal.set_buyer_approved(order_id)
        self.conn.execute("UPDATE purchases SET status = 'BUYER_APPROVED' WHERE id = ?;", (p_id,))

        # 4. Safe capture
        cap_status, cap_id = execute_capture_flow(self.conn, p_id, self.mock_paypal, "boot_1")
        self.assertEqual(cap_status, "CAPTURED")
        self.assertTrue(cap_id.startswith("CAP_"))

        # Final state check
        cur = self.conn.execute("SELECT status FROM purchases WHERE id = ?;", (p_id,))
        self.assertEqual(cur.fetchone()["status"], "CAPTURED")

        # Snapshot check: reserved -> captured
        snapshot = get_budget_snapshot(self.conn, "default", time.strftime("%Y-%m-%d", time.gmtime(now)))
        self.assertEqual(snapshot.captured_cents, 4500)
        self.assertEqual(snapshot.reserved_cents, 0)
        self.assertEqual(snapshot.r3_committed_cents, 4500)

    def test_flow_2_human_approval_over_threshold_to_captured(self):
        """
        Flow 2 (Human Approval):
        Cart $90.00 triggers Review Threshold (R4):
        1. submit -> PENDING_APPROVAL
        2. admin approve -> APPROVED
        3. create_order -> PAYPAL_ORDER_CREATED
        4. buyer approval -> BUYER_APPROVED
        5. capture -> CAPTURED
        """
        cart = FrozenCart(
            items=[CartItem("shoes_road_racer", "merchant_shoes", "Racer", 9000, 1)],
            total_cents=9000,
            cart_hash="hash_9000"
        )
        now = time.time()

        # 1. Submit cart
        p_id, state, dec = submit_cart_to_orchestrator(
            self.conn, "tenant_demo", "default", "r_test", cart, now
        )
        self.assertEqual(state, PurchaseState.PENDING_APPROVAL)
        self.assertEqual(dec.outcome, Outcome.REVIEW)
        self.assertEqual(dec.rule_name, "R4_APPROVAL_THRESHOLD")

        # 2. Admin clicks approve (Checkpoint P2)
        app_state, reason = approve_pending_purchase(self.conn, p_id, admin_actor="admin")
        self.assertEqual(app_state, PurchaseState.APPROVED)

        # 3. Create PayPal order
        state_order, approval_url = create_paypal_order_for_purchase(self.conn, p_id, self.mock_paypal)
        self.assertEqual(state_order, PurchaseState.PAYPAL_ORDER_CREATED)

        # 4. Buyer approves
        cur = self.conn.execute("SELECT paypal_order_id FROM purchases WHERE id = ?;", (p_id,))
        order_id = cur.fetchone()["paypal_order_id"]
        self.mock_paypal.set_buyer_approved(order_id)
        self.conn.execute("UPDATE purchases SET status = 'BUYER_APPROVED' WHERE id = ?;", (p_id,))

        # 5. Capture
        cap_status, cap_id = execute_capture_flow(self.conn, p_id, self.mock_paypal, "boot_1")
        self.assertEqual(cap_status, "CAPTURED")

    def test_flow_3_unapproved_merchant_blocked(self):
        """
        Flow 3 (Adversarial Merchant):
        Cart from merchant_unapproved triggers Rule R1 -> BLOCKED immediately with zero PayPal calls.
        """
        cart = FrozenCart(
            items=[CartItem("unapproved_vpn_sub", "merchant_unapproved", "VPN", 4000, 1)],
            total_cents=4000,
            cart_hash="hash_vpn"
        )
        now = time.time()

        p_id, state, dec = submit_cart_to_orchestrator(
            self.conn, "tenant_demo", "default", "r_test", cart, now
        )
        self.assertEqual(state, PurchaseState.BLOCKED)
        self.assertEqual(dec.outcome, Outcome.DENY)
        self.assertEqual(dec.rule_name, "R1_MERCHANT_ALLOWLIST")

        # Zero funds held in reservation
        snapshot = get_budget_snapshot(self.conn, "default", time.strftime("%Y-%m-%d", time.gmtime(now)))
        self.assertEqual(snapshot.reserved_cents, 0)
        self.assertEqual(snapshot.r3_committed_cents, 0)

    def test_flow_4_emergency_freeze_kill_switch(self):
        """
        Flow 4 (Emergency Freeze):
        When policy.frozen is True, any cart is denied under Rule R0 at Checkpoint P1.
        """
        self.conn.execute("UPDATE policy SET frozen = 1 WHERE agent_id = 'default';")
        cart = FrozenCart(
            items=[CartItem("shoes_trail_runner", "merchant_shoes", "Shoes", 4500, 1)],
            total_cents=4500,
            cart_hash="hash_freeze"
        )
        now = time.time()

        p_id, state, dec = submit_cart_to_orchestrator(
            self.conn, "tenant_demo", "default", "r_test", cart, now
        )
        self.assertEqual(state, PurchaseState.BLOCKED)
        self.assertEqual(dec.outcome, Outcome.DENY)
        self.assertEqual(dec.rule_name, "R0_FREEZE")


if __name__ == "__main__":
    unittest.main()

