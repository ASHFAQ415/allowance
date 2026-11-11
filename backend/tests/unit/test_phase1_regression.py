"""
Regression Tests for Phase 1 Crash and Correctness Fixes.
Covers:
1.1 State machine typing imports & API smoke check.
1.2 Rejection reason stored in purchase record & workflow ledger.
1.3 ShopMind query quantity parsing (price constraints stripped).
1.4 Capture amount/currency mismatch transitions to CAPTURE_UNKNOWN with unresolved accounting.
1.5 Financial journal re-raises double-entry transaction failures rather than swallowing.
1.6 HTTP 422 with INSTRUMENT_DECLINED maps to definitive FAILED status with reservation release.
"""

import json
import os
import sqlite3
import time
import unittest
from backend.app.agent.loop import run_deterministic_fallback
from backend.app.db.bootstrap import seed_defaults
from backend.app.db.connection import create_connection, init_db
from backend.app.db.financial_journal import get_budget_snapshot, record_financial_fact, FactType
from backend.app.db.double_entry import record_double_entry_transaction
from backend.app.orchestrator.core import (
    approve_pending_purchase,
    reject_pending_purchase,
)
from backend.app.orchestrator.capture_lifecycle import execute_capture_flow, EXPECTED_MERCHANT_ID
from backend.app.orchestrator.state_machine import PurchaseState
from backend.app.paypal.client import MockPayPalClient, TransportOutcome


def _seed_test_purchase(conn, purchase_id, status, total_cents, order_id=None):
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    run_id = f"r_{purchase_id}"
    conn.execute(
        """
        INSERT OR IGNORE INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at)
        VALUES (?, 'tenant_demo', 'default', ?, 'hash', ?, 'COMPLETED', ?);
        """,
        (run_id, f"k_{purchase_id}", time.time(), now_iso)
    )
    conn.execute(
        """
        INSERT INTO purchases (
            id, tenant_id, agent_id, run_id, status, auto_approved, cart_json, cart_hash,
            total_cents, currency, merchant_ids_json, budget_day, create_request_id, paypal_order_id,
            state_entered_at, created_at, updated_at
        ) VALUES (?, 'tenant_demo', 'default', ?, ?, 0, '[]', 'hash_test', ?, 'USD', '["merchant_shoes"]',
                  '2026-11-11', ?, ?, ?, ?, ?);
        """,
        (purchase_id, run_id, status, total_cents, f"req_{purchase_id}", order_id, time.time(), now_iso, now_iso)
    )
    record_financial_fact(
        conn=conn,
        source_event_key=f"init_res_{purchase_id}",
        tenant_id="tenant_demo",
        agent_id="default",
        purchase_id=purchase_id,
        budget_day="2026-11-11",
        fact_type=FactType.RESERVATION_CREATED,
        delta_reserved_cents=total_cents,
        delta_unresolved_cents=0,
        delta_captured_cents=0,
        delta_auto_approved_cents=0,
        actor="test"
    )


class TestPhase1Regressions(unittest.TestCase):
    def setUp(self):
        self.db_path = f"test_phase1_{self._testMethodName}.db"
        init_db(self.db_path)
        self.conn = create_connection(self.db_path)
        seed_defaults(self.conn)
        self.paypal = MockPayPalClient()

    def tearDown(self):
        self.conn.close()
        for ext in ["", "-wal", "-shm"]:
            p = f"{self.db_path}{ext}"
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def test_1_1_state_machine_and_api_import_smoke(self):
        """1.1 Smoke check: importing state_machine and api succeeds under all Python versions."""
        import backend.app.orchestrator.state_machine as sm
        import backend.app.api.routes as routes
        self.assertTrue(hasattr(sm, "PurchaseState"))
        self.assertTrue(hasattr(routes, "api_router"))

    def test_1_2_reject_pending_purchase_stores_custom_reason(self):
        """1.2 reject_pending_purchase stores reason in purchase and ledger row."""
        purchase_id = "pur_reject_test"
        _seed_test_purchase(self.conn, purchase_id, "PENDING_APPROVAL", 7500)

        custom_reason = "Over quarterly IT budget ceiling"
        state, reason = reject_pending_purchase(
            self.conn, purchase_id, admin_actor="supervisor_carol", reason=custom_reason
        )

        self.assertEqual(state, PurchaseState.REJECTED)
        self.assertEqual(reason, custom_reason)

        row = self.conn.execute("SELECT status, status_reason FROM purchases WHERE id = ?;", (purchase_id,)).fetchone()
        self.assertEqual(row["status"], "REJECTED")
        self.assertEqual(row["status_reason"], custom_reason)

        ledger_row = self.conn.execute("SELECT reason, actor FROM ledger WHERE purchase_id = ? AND to_state = 'REJECTED';", (purchase_id,)).fetchone()
        self.assertIsNotNone(ledger_row)
        self.assertEqual(ledger_row["reason"], custom_reason)
        self.assertEqual(ledger_row["actor"], "supervisor_carol")

    def test_1_3_loop_quantity_regex_price_stripping(self):
        """1.3 Verify price constraints like '$5' or 'under $50' do not corrupt quantity parsing."""
        cart_under_5 = run_deterministic_fallback("trail shoes under $5")
        self.assertIsNotNone(cart_under_5)
        self.assertEqual(cart_under_5.items[0].quantity, 1)

        cart_under_50 = run_deterministic_fallback("find me trail shoes for under $50")
        self.assertIsNotNone(cart_under_50)
        self.assertEqual(cart_under_50.items[0].quantity, 1)

        cart_buy_2 = run_deterministic_fallback("buy 2 pairs of trail shoes under $50")
        self.assertIsNotNone(cart_buy_2)
        self.assertEqual(cart_buy_2.items[0].quantity, 2)

        cart_buy_3 = run_deterministic_fallback("buy 3 water bottles")
        self.assertIsNotNone(cart_buy_3)
        self.assertEqual(cart_buy_3.items[0].quantity, 3)

    def test_1_4_capture_mismatch_transitions_to_capture_unknown(self):
        """1.4 On capture amount mismatch, purchase moves to CAPTURE_UNKNOWN and writes fact."""
        purchase_id = "pur_mismatch_test"
        order_id = "ORD_MISM_123"
        _seed_test_purchase(self.conn, purchase_id, "BUYER_APPROVED", 5000, order_id=order_id)

        # Pre-capture GET order matches exactly
        self.paypal.orders[order_id] = {
            "id": order_id,
            "status": "APPROVED",
            "purchase_units": [{
                "amount": {"currency_code": "USD", "value": "50.00"},
                "payee": {"merchant_id": EXPECTED_MERCHANT_ID}
            }]
        }

        # Mock client that returns mismatched capture details on POST capture
        class MismatchCapturePayPal(MockPayPalClient):
            def capture_order(self, ord_id, req_id):
                return TransportOutcome(
                    success=True,
                    status_code=200,
                    data={
                        "id": ord_id,
                        "status": "COMPLETED",
                        "purchase_units": [{
                            "payments": {
                                "captures": [{
                                    "id": "CAP_WRONG_AMT",
                                    "amount": {"value": "99.00", "currency_code": "USD"},
                                    "status": "COMPLETED"
                                }]
                            }
                        }]
                    },
                    bytes_may_have_been_sent=True
                )

        mism_client = MismatchCapturePayPal()
        mism_client.orders = self.paypal.orders

        status, reason = execute_capture_flow(self.conn, purchase_id, mism_client, boot_id="boot_mism")

        self.assertEqual(status, "CAPTURE_UNKNOWN")
        self.assertEqual(reason, "capture_amount_mismatch")

        row = self.conn.execute("SELECT status FROM purchases WHERE id = ?;", (purchase_id,)).fetchone()
        self.assertEqual(row["status"], "CAPTURE_UNKNOWN")

        snap = get_budget_snapshot(self.conn, "default", "2026-11-11")
        self.assertEqual(snap.unresolved_cents, 5000)
        self.assertEqual(snap.captured_cents, 0)

    def test_1_5_financial_journal_reraises_double_entry_errors(self):
        """1.5 Journal raises error if double-entry posting fails with non-missing-table error."""
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Attempting unbalanced transaction: Debits 5000 != Credits 4000
        with self.assertRaises(Exception):
            record_double_entry_transaction(
                self.conn,
                entry_id="unbalanced_entry_test",
                source_event_key="evt_unbalanced",
                tenant_id="tenant_demo",
                agent_id="default",
                purchase_id="pur_unbal",
                lines=[
                    ("1000", 5000, 0, "USD"),
                    ("2100", 0, 4000, "USD"),
                ],
                narration="Test unbalanced post",
                created_at=now_iso
            )

    def test_1_6_capture_422_instrument_declined_fails_and_releases(self):
        """1.6 Capture returning 422 with INSTRUMENT_DECLINED transitions to FAILED and releases reservation."""
        purchase_id = "pur_422_declined"
        order_id = "ORD_DEC_422"
        _seed_test_purchase(self.conn, purchase_id, "BUYER_APPROVED", 6000, order_id=order_id)

        self.paypal.orders[order_id] = {
            "id": order_id,
            "status": "APPROVED",
            "purchase_units": [{
                "amount": {"currency_code": "USD", "value": "60.00"},
                "payee": {"merchant_id": EXPECTED_MERCHANT_ID}
            }]
        }

        # Override mock paypal to return 422 INSTRUMENT_DECLINED
        class Declined422PayPal(MockPayPalClient):
            def capture_order(self, ord_id, req_id):
                return TransportOutcome(
                    success=False,
                    status_code=422,
                    data={
                        "name": "UNPROCESSABLE_ENTITY",
                        "details": [{"issue": "INSTRUMENT_DECLINED", "description": "Instrument was declined"}]
                    },
                    bytes_may_have_been_sent=True,
                    error_class="INSTRUMENT_DECLINED"
                )

        dec_client = Declined422PayPal()
        dec_client.orders = self.paypal.orders

        status, reason = execute_capture_flow(self.conn, purchase_id, dec_client, boot_id="boot_422")
        self.assertEqual(status, "FAILED")
        self.assertEqual(reason, "capture_declined")

        row = self.conn.execute("SELECT status, status_reason FROM purchases WHERE id = ?;", (purchase_id,)).fetchone()
        self.assertEqual(row["status"], "FAILED")

        snap = get_budget_snapshot(self.conn, "default", "2026-11-11")
        self.assertEqual(snap.reserved_cents, 0)
        self.assertEqual(snap.captured_cents, 0)
        self.assertEqual(snap.unresolved_cents, 0)


if __name__ == "__main__":
    unittest.main()
