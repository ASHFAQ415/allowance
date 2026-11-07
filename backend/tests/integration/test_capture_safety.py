"""
Integration Tests for Safe Capture Protocol and Uncertainty Handling.
Tests Invariants 1 & 2 (Scenarios #2, #3, #4, #6, #13, V22-01, V22-02, V22-03, V22-07).
"""

import os
import tempfile
import unittest

from backend.app.db.connection import create_connection, init_db, transactional_session
from backend.app.db.financial_journal import get_budget_snapshot
from backend.app.paypal.client import MockPayPalClient
from backend.app.orchestrator.capture_lifecycle import (
    execute_capture_flow,
    execute_read_only_reconciliation,
    record_late_capture_adjustment,
)


class TestCaptureSafety(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        init_db(self.db_path)
        self.conn = create_connection(self.db_path)
        self.mock_paypal = MockPayPalClient()

        # Seed minimal tenant, agent, run, purchase in BUYER_APPROVED
        self.conn.execute("INSERT INTO agents (id, tenant_id, name, created_at) VALUES ('a1', 't1', 'Agent 1', '2026');")
        self.conn.execute("INSERT INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at) VALUES ('r1', 't1', 'a1', 'k1', 'h1', 1000.0, 'COMPLETED', '2026');")

        # Create real mock order
        out = self.mock_paypal.create_order("req_create_1", 4500, [], "USD")
        self.order_id = out.data["id"]
        self.mock_paypal.set_buyer_approved(self.order_id)

        self.conn.execute(
            """
            INSERT INTO purchases (
                id, tenant_id, agent_id, run_id, status, auto_approved, cart_json, cart_hash,
                total_cents, merchant_ids_json, budget_day, create_request_id, paypal_order_id,
                state_entered_at, created_at, updated_at
            ) VALUES (
                'p1', 't1', 'a1', 'r1', 'BUYER_APPROVED', 1, '{}', 'h1', 4500, '[]',
                '2026-11-06', 'req_create_1', ?, 1000.0, '2026', '2026'
            );
            """,
            (self.order_id,)
        )

        # Record initial reservation fact for p1
        from backend.app.db.financial_journal import FactType, record_financial_fact
        record_financial_fact(
            conn=self.conn,
            source_event_key="init_res_p1",
            tenant_id="t1",
            agent_id="a1",
            purchase_id="p1",
            budget_day="2026-11-06",
            fact_type=FactType.RESERVATION_CREATED,
            delta_reserved_cents=4500,
            delta_unresolved_cents=0,
            delta_captured_cents=0,
            delta_auto_approved_cents=4500,
            actor="agent"
        )

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_happy_path_capture_success(self):
        """Normal capture succeeds, transitions to CAPTURED, moves funds reserved -> captured."""
        status, cap_id = execute_capture_flow(self.conn, "p1", self.mock_paypal, "boot_1")
        self.assertEqual(status, "CAPTURED")
        self.assertTrue(cap_id.startswith("CAP_"))

        cur = self.conn.execute("SELECT status FROM purchases WHERE id = 'p1';")
        self.assertEqual(cur.fetchone()["status"], "CAPTURED")

        cur_op = self.conn.execute("SELECT state FROM capture_operations WHERE purchase_id = 'p1';")
        self.assertEqual(cur_op.fetchone()["state"], "DEFINITIVE_SUCCESS")

        snapshot = get_budget_snapshot(self.conn, "a1", "2026-11-06")
        self.assertEqual(snapshot.captured_cents, 4500)
        self.assertEqual(snapshot.reserved_cents, 0)
        self.assertEqual(snapshot.unresolved_cents, 0)

    def test_read_timeout_after_transmission_lands_in_unknown(self):
        """Read timeout after request sent lands in CAPTURE_UNKNOWN with ZERO blind retries."""
        self.mock_paypal.simulate_read_timeout = True
        status, reason = execute_capture_flow(self.conn, "p1", self.mock_paypal, "boot_1")
        self.assertEqual(status, "CAPTURE_UNKNOWN")
        self.assertEqual(reason, "transport_ambiguity")

        cur = self.conn.execute("SELECT status FROM purchases WHERE id = 'p1';")
        self.assertEqual(cur.fetchone()["status"], "CAPTURE_UNKNOWN")

        # Invariant 3: Funds move to unresolved_cents, total commitment stays $45.00!
        snapshot = get_budget_snapshot(self.conn, "a1", "2026-11-06")
        self.assertEqual(snapshot.unresolved_cents, 4500)
        self.assertEqual(snapshot.r3_committed_cents, 4500)

    def test_proven_pre_send_connect_error_retries_once(self):
        """A connect error (proven no bytes sent) retries once and completes successfully."""
        self.mock_paypal.simulate_connect_error = True
        status, cap_id = execute_capture_flow(self.conn, "p1", self.mock_paypal, "boot_1")
        self.assertEqual(status, "CAPTURED")

        cur_op = self.conn.execute("SELECT attempt_count, state FROM capture_operations WHERE purchase_id = 'p1';")
        row = cur_op.fetchone()
        self.assertEqual(row["attempt_count"], 2)
        self.assertEqual(row["state"], "DEFINITIVE_SUCCESS")

    def test_reconciliation_is_strictly_read_only_and_resolves_completed_order(self):
        """Reconciliation reads GET order and resolves to CAPTURED without sending any capture POST."""
        # 1. Force purchase into CAPTURE_UNKNOWN via read timeout
        self.mock_paypal.simulate_read_timeout = True
        execute_capture_flow(self.conn, "p1", self.mock_paypal, "boot_1")

        # 2. Run read-only reconciliation
        # Note: on mock server, the timeout call had completed the capture internally!
        status, cap_id = execute_read_only_reconciliation(self.conn, "p1", self.mock_paypal)
        self.assertEqual(status, "CAPTURED")

        cur = self.conn.execute("SELECT status FROM purchases WHERE id = 'p1';")
        self.assertEqual(cur.fetchone()["status"], "CAPTURED")

        # Ledger contains valid transition CAPTURE_UNKNOWN -> CAPTURED
        cur_led = self.conn.execute("SELECT from_state, to_state FROM ledger WHERE purchase_id = 'p1' ORDER BY created_at DESC;")
        last_trans = cur_led.fetchone()
        self.assertEqual(last_trans["from_state"], "CAPTURE_UNKNOWN")
        self.assertEqual(last_trans["to_state"], "CAPTURED")

    def test_reconciliation_approved_order_never_retries_capture(self):
        """If GET order still says APPROVED during reconciliation, remain CAPTURE_UNKNOWN and send ZERO capture POSTs!"""
        # Set purchase to CAPTURE_UNKNOWN manually
        self.conn.execute("UPDATE purchases SET status = 'CAPTURE_UNKNOWN' WHERE id = 'p1';")

        # Order on mock is still APPROVED (no capture was attempted)
        status, reason = execute_read_only_reconciliation(self.conn, "p1", self.mock_paypal)
        self.assertEqual(status, "CAPTURE_UNKNOWN")
        self.assertEqual(reason, "order_status_still_APPROVED")

        # Ensure order on mock was NOT captured!
        self.assertEqual(self.mock_paypal.orders[self.order_id]["status"], "APPROVED")

    def test_pre_capture_amount_tampering_aborts(self):
        """Tampered amount in PayPal order fails pre-capture verification with ZERO capture attempts."""
        self.mock_paypal.simulate_amount_tamper = True
        status, reason = execute_capture_flow(self.conn, "p1", self.mock_paypal, "boot_1")
        self.assertEqual(status, "FAILED")
        self.assertEqual(reason, "amount_mismatch")

        # Assert no capture was created in mock
        self.assertNotIn(self.order_id, self.mock_paypal.captures)

    def test_late_capture_adjustment_preserves_terminal_failed_state(self):
        """Late settlement after terminal failure records adjustment record; leaves purchases.status='FAILED' intact."""
        # Purchase reached terminal FAILED(unresolved_capture)
        self.conn.execute("UPDATE purchases SET status = 'FAILED', status_reason = 'unresolved_capture' WHERE id = 'p1';")

        # Record late capture adjustment
        success = record_late_capture_adjustment(self.conn, "p1", "CAP_LATE_999", resolved_by="admin")
        self.assertTrue(success)

        # Invariant 2: purchases.status is STILL 'FAILED' (never mutated!)
        cur = self.conn.execute("SELECT status, status_reason FROM purchases WHERE id = 'p1';")
        row = cur.fetchone()
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["status_reason"], "unresolved_capture")

        # Adjustment record exists
        cur_adj = self.conn.execute("SELECT adjustment_type, paypal_capture_id, amount_cents FROM ledger_adjustments WHERE purchase_id = 'p1';")
        adj = cur_adj.fetchone()
        self.assertEqual(adj["adjustment_type"], "LATE_CAPTURE_SETTLED")
        self.assertEqual(adj["paypal_capture_id"], "CAP_LATE_999")
        self.assertEqual(adj["amount_cents"], 4500)


if __name__ == "__main__":
    unittest.main()
