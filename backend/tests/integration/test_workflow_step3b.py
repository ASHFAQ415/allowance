"""
Regression and Integration Tests for Step 3b:
- PayPal routes: /paypal/return, /paypal/cancel, /purchases/{id}/pay, /purchases/{id}/reject
- Policy/demo routes: PUT /policy, POST /demo/reset
- PayPal mode selection: PAYPAL_MODE wiring and client selection
- Checkpoints P3 (pre-order-create) and P4 (pre-capture) policy re-evaluation
"""

import json
import os
import tempfile
import time
import types
import unittest
from unittest import mock

from backend.app.api.dto import UpdatePolicyRequest
from backend.app.api.routes import (
    admin_reject,
    demo_reset,
    health_check,
    pay_purchase,
    paypal_cancel,
    paypal_return,
    update_policy,
)
from backend.app.db.connection import (
    DEFAULT_DB_PATH,
    close_thread_connection,
    create_connection,
    get_thread_connection,
    init_db,
    transactional_session,
)
from backend.app.db.financial_journal import (
    FactType,
    get_budget_snapshot,
    record_financial_fact,
)
from backend.app.orchestrator.capture_lifecycle import execute_capture_flow
from backend.app.orchestrator.core import (
    create_paypal_order_for_purchase,
    submit_cart_to_orchestrator,
)
from backend.app.orchestrator.state_machine import PurchaseState
from backend.app.paypal import (
    MockPayPalClient,
    PayPalHttpClient,
    get_paypal_client,
    get_paypal_mode,
    reset_paypal_client,
    set_paypal_client,
)
from backend.app.policy.rules import CartItem, FrozenCart
from scripts.seed_db import seed_database


def _fake_request() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        headers={},
        client=types.SimpleNamespace(host="127.0.0.1"),
    )


class TestWorkflowStep3b(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        seed_database(self.db_path)
        self.conn = create_connection(self.db_path)
        self.mock_paypal = MockPayPalClient()
        set_paypal_client(self.mock_paypal)

        # Seed test agent run
        self.conn.execute(
            """
            INSERT INTO agent_runs (
                id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at
            ) VALUES ('r_test3b', 'tenant_demo', 'default', 'k_3b', 'h_3b', 1000.0, 'COMPLETED', '2026');
            """
        )

    def tearDown(self):
        self.conn.close()
        close_thread_connection()
        reset_paypal_client()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _create_test_purchase(self, amount_cents: int = 4500, merchant_id: str = "merchant_shoes") -> str:
        cart = FrozenCart(
            items=[CartItem("shoes_trail_runner", merchant_id, "Shoes", amount_cents, 1)],
            total_cents=amount_cents,
            cart_hash=f"hash_{amount_cents}_{time.time()}",
        )
        pid, state, _ = submit_cart_to_orchestrator(
            self.conn, "tenant_demo", "default", "r_test3b", cart, time.time()
        )
        return pid

    # 1. PayPal Mode Selection & Health Check
    def test_paypal_mode_and_client_factory(self):
        with mock.patch.dict(os.environ, {"PAYPAL_MODE": "mock"}):
            self.assertEqual(get_paypal_mode(), "mock")
            reset_paypal_client()
            client = get_paypal_client()
            self.assertIsInstance(client, MockPayPalClient)

        with mock.patch.dict(os.environ, {
            "PAYPAL_MODE": "sandbox",
            "PAYPAL_CLIENT_ID": "test_cid",
            "PAYPAL_CLIENT_SECRET": "test_sec",
        }):
            self.assertEqual(get_paypal_mode(), "sandbox")
            reset_paypal_client()
            client = get_paypal_client()
            self.assertIsInstance(client, PayPalHttpClient)

        reset_paypal_client()
        set_paypal_client(self.mock_paypal)

        health = health_check()
        self.assertEqual(health["status"], "ok")
        self.assertIn("paypal_mode", health)

    # 2. Checkpoint P3: Pre-order-create Policy Re-evaluation
    def test_checkpoint_p3_blocks_when_policy_frozen(self):
        pid = self._create_test_purchase(4500)
        cur = self.conn.execute("SELECT status FROM purchases WHERE id = ?;", (pid,))
        self.assertEqual(cur.fetchone()["status"], "AUTO_APPROVED")

        # Freeze the policy before PayPal order creation
        self.conn.execute("UPDATE policy SET frozen = 1 WHERE agent_id = 'default';")

        new_state, reason = create_paypal_order_for_purchase(self.conn, pid, self.mock_paypal)
        self.assertEqual(new_state, PurchaseState.BLOCKED)
        self.assertIn("freeze", reason.lower())

        cur = self.conn.execute("SELECT status, status_reason FROM purchases WHERE id = ?;", (pid,))
        row = cur.fetchone()
        self.assertEqual(row["status"], "BLOCKED")

        # Reservation must be released
        snapshot = get_budget_snapshot(self.conn, "default", time.strftime("%Y-%m-%d", time.gmtime()))
        self.assertEqual(snapshot.reserved_cents, 0)

        # Ledger row with checkpoint P3
        ledger = self.conn.execute(
            "SELECT * FROM ledger WHERE purchase_id = ? AND to_state = 'BLOCKED';", (pid,)
        ).fetchone()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger["checkpoint"], "P3")

    def test_checkpoint_p3_blocks_when_daily_budget_exhausted(self):
        # Default daily budget is $300.00 (30000 cents).
        # Commit $280.00 of spend
        today = time.strftime("%Y-%m-%d", time.gmtime())
        self.conn.execute(
            """
            INSERT INTO purchases (
                id, tenant_id, agent_id, run_id, status, auto_approved, cart_json, cart_hash,
                total_cents, currency, merchant_ids_json, budget_day, create_request_id,
                state_entered_at, created_at, updated_at
            ) VALUES (
                'pid_prior', 'tenant_demo', 'default', 'r_test3b', 'CAPTURED', 1, '[]', 'h0',
                28000, 'USD', '["merchant_shoes"]', ?, 'req_p3_prior', 1000.0, '2026', '2026'
            );
            """,
            (today,),
        )
        record_financial_fact(
            conn=self.conn,
            source_event_key="burn_budget_p3",
            tenant_id="tenant_demo",
            agent_id="default",
            purchase_id="pid_prior",
            budget_day=today,
            fact_type=FactType.CAPTURE_SETTLED,
            delta_reserved_cents=0,
            delta_unresolved_cents=0,
            delta_captured_cents=28000,
            delta_auto_approved_cents=28000,
            actor="test",
        )

        # Create purchase for $45.00 (28000 + 4500 = 32500 > 30000)
        # Manually seed an AUTO_APPROVED purchase to test P3 isolation
        pid = "pur_over_p3"
        self.conn.execute(
            """
            INSERT INTO purchases (
                id, tenant_id, agent_id, run_id, status, auto_approved, cart_json, cart_hash,
                total_cents, currency, merchant_ids_json, budget_day, create_request_id,
                state_entered_at, created_at, updated_at
            ) VALUES (
                ?, 'tenant_demo', 'default', 'r_test3b', 'AUTO_APPROVED', 1, '[]', 'h',
                4500, 'USD', '["merchant_shoes"]', ?, 'req_p3_over', 1000.0, '2026', '2026'
            );
            """,
            (pid, today),
        )

        new_state, reason = create_paypal_order_for_purchase(self.conn, pid, self.mock_paypal)
        self.assertEqual(new_state, PurchaseState.BLOCKED)
        self.assertIn("budget", reason.lower())

    # 3. Checkpoint P4: Pre-capture Policy Re-evaluation
    def test_checkpoint_p4_blocks_capture_when_policy_frozen(self):
        pid = self._create_test_purchase(4500)
        state_order, _ = create_paypal_order_for_purchase(self.conn, pid, self.mock_paypal)
        self.assertEqual(state_order, PurchaseState.PAYPAL_ORDER_CREATED)

        order_id = self.conn.execute("SELECT paypal_order_id FROM purchases WHERE id = ?;", (pid,)).fetchone()["paypal_order_id"]
        self.mock_paypal.set_buyer_approved(order_id)
        self.conn.execute("UPDATE purchases SET status = 'BUYER_APPROVED' WHERE id = ?;", (pid,))

        # Emergency freeze before capture executes
        self.conn.execute("UPDATE policy SET frozen = 1 WHERE agent_id = 'default';")

        cap_status, reason = execute_capture_flow(self.conn, pid, self.mock_paypal, "boot_test")
        self.assertEqual(cap_status, "BLOCKED")
        self.assertIn("freeze", reason.lower())

        cur = self.conn.execute("SELECT status FROM purchases WHERE id = ?;", (pid,))
        self.assertEqual(cur.fetchone()["status"], "BLOCKED")

        # PayPal capture was never executed
        self.assertNotIn(order_id, self.mock_paypal.captures)

        # Ledger row with checkpoint P4
        ledger = self.conn.execute(
            "SELECT * FROM ledger WHERE purchase_id = ? AND to_state = 'BLOCKED';", (pid,)
        ).fetchone()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger["checkpoint"], "P4")

    def test_checkpoint_p4_blocks_capture_when_daily_budget_exhausted(self):
        pid = self._create_test_purchase(4500)
        create_paypal_order_for_purchase(self.conn, pid, self.mock_paypal)
        order_id = self.conn.execute("SELECT paypal_order_id FROM purchases WHERE id = ?;", (pid,)).fetchone()["paypal_order_id"]
        self.mock_paypal.set_buyer_approved(order_id)
        self.conn.execute("UPDATE purchases SET status = 'BUYER_APPROVED' WHERE id = ?;", (pid,))

        # Exhaust daily budget through concurrent capture
        today = time.strftime("%Y-%m-%d", time.gmtime())
        self.conn.execute(
            """
            INSERT INTO purchases (
                id, tenant_id, agent_id, run_id, status, auto_approved, cart_json, cart_hash,
                total_cents, currency, merchant_ids_json, budget_day, create_request_id,
                state_entered_at, created_at, updated_at
            ) VALUES (
                'pid_concurrent', 'tenant_demo', 'default', 'r_test3b', 'CAPTURED', 1, '[]', 'h0',
                29000, 'USD', '["merchant_shoes"]', ?, 'req_p4_concurrent', 1000.0, '2026', '2026'
            );
            """,
            (today,),
        )
        record_financial_fact(
            conn=self.conn,
            source_event_key="burn_budget_p4",
            tenant_id="tenant_demo",
            agent_id="default",
            purchase_id="pid_concurrent",
            budget_day=today,
            fact_type=FactType.CAPTURE_SETTLED,
            delta_reserved_cents=0,
            delta_unresolved_cents=0,
            delta_captured_cents=29000,
            delta_auto_approved_cents=29000,
            actor="test",
        )

        cap_status, reason = execute_capture_flow(self.conn, pid, self.mock_paypal, "boot_test")
        self.assertEqual(cap_status, "BLOCKED")
        self.assertIn("budget", reason.lower())

    # 4. Admin Reject Endpoint: POST /purchases/{id}/reject
    def test_admin_reject_pending_purchase(self):
        from backend.app.api.routes import HTTPException
        # $90 triggers REVIEW -> PENDING_APPROVAL
        pid = self._create_test_purchase(9000)
        cur = self.conn.execute("SELECT status FROM purchases WHERE id = ?;", (pid,))
        self.assertEqual(cur.fetchone()["status"], "PENDING_APPROVAL")

        with mock.patch("backend.app.api.routes.get_thread_connection", return_value=self.conn):
            res = admin_reject(purchase_id=pid, is_admin=True)

        self.assertEqual(res["status"], "REJECTED")
        self.assertEqual(res["reason"], "admin_rejected")

        cur = self.conn.execute("SELECT status FROM purchases WHERE id = ?;", (pid,))
        self.assertEqual(cur.fetchone()["status"], "REJECTED")

        # Rejecting already rejected or non-pending purchase raises 409
        with mock.patch("backend.app.api.routes.get_thread_connection", return_value=self.conn):
            with self.assertRaises(HTTPException) as ctx:
                admin_reject(purchase_id=pid, is_admin=True)
            self.assertEqual(ctx.exception.status_code, 409)

    # 5. Idempotent Pay Advancement: POST /purchases/{id}/pay
    def test_pay_purchase_advances_through_lifecycle(self):
        from backend.app.api.routes import HTTPException
        pid = self._create_test_purchase(4500)

        with mock.patch("backend.app.api.routes.get_thread_connection", return_value=self.conn):
            # 1. First pay call: AUTO_APPROVED -> PAYPAL_ORDER_CREATED
            res1 = pay_purchase(purchase_id=pid, is_admin=True, tenant_id="tenant_demo")
            self.assertEqual(res1["status"], "PAYPAL_ORDER_CREATED")
            self.assertIsNotNone(res1.get("approval_url"))

            # 2. Simulate buyer approval at PayPal
            order_id = self.conn.execute("SELECT paypal_order_id FROM purchases WHERE id = ?;", (pid,)).fetchone()["paypal_order_id"]
            self.mock_paypal.set_buyer_approved(order_id)

            # 3. Second pay call: advances to CAPTURED
            res2 = pay_purchase(purchase_id=pid, is_admin=True, tenant_id="tenant_demo")
            self.assertEqual(res2["status"], "CAPTURED")

            # 4. Third pay call: idempotent replay of CAPTURED
            res3 = pay_purchase(purchase_id=pid, is_admin=True, tenant_id="tenant_demo")
            self.assertEqual(res3["status"], "CAPTURED")
            self.assertEqual(res3["detail"], "already_captured")

    def test_pay_purchase_rejects_pending_approval(self):
        from backend.app.api.routes import HTTPException
        pid = self._create_test_purchase(9000)  # PENDING_APPROVAL

        with mock.patch("backend.app.api.routes.get_thread_connection", return_value=self.conn):
            with self.assertRaises(HTTPException) as ctx:
                pay_purchase(purchase_id=pid, is_admin=True, tenant_id="tenant_demo")
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("requires human approval", ctx.exception.detail)

    # 6. PayPal Return & Cancel Handlers: GET /paypal/return, GET /paypal/cancel
    def test_paypal_return_and_cancel_routes(self):
        from backend.app.api.routes import HTTPException
        pid = self._create_test_purchase(4500)
        create_paypal_order_for_purchase(self.conn, pid, self.mock_paypal)
        order_id = self.conn.execute("SELECT paypal_order_id FROM purchases WHERE id = ?;", (pid,)).fetchone()["paypal_order_id"]

        with mock.patch("backend.app.api.routes.get_thread_connection", return_value=self.conn):
            # Missing token -> 400
            with self.assertRaises(HTTPException) as ctx400:
                paypal_return(token=None)
            self.assertEqual(ctx400.exception.status_code, 400)

            # Non-existent token -> 404
            with self.assertRaises(HTTPException) as ctx404:
                paypal_return(token="ORDER_NONEXISTENT")
            self.assertEqual(ctx404.exception.status_code, 404)

            # Return without buyer approving at PayPal leaves order created
            res_not_approved = paypal_return(token=order_id)
            self.assertEqual(res_not_approved["status"], "PAYPAL_ORDER_CREATED")

            # Buyer approves at PayPal -> return captures successfully
            self.mock_paypal.set_buyer_approved(order_id)
            res_success = paypal_return(token=order_id)
            self.assertEqual(res_success["status"], "CAPTURED")

            # Repeat visit -> idempotent CAPTURED
            res_replay = paypal_return(token=order_id)
            self.assertEqual(res_replay["status"], "CAPTURED")

            # Cancel handler returns cancelled payload with no mutations
            cancel_res = paypal_cancel(token=order_id)
            self.assertEqual(cancel_res["status"], "cancelled")

    def test_paypal_return_detects_amount_tamper(self):
        pid = self._create_test_purchase(4500)
        create_paypal_order_for_purchase(self.conn, pid, self.mock_paypal)
        order_id = self.conn.execute("SELECT paypal_order_id FROM purchases WHERE id = ?;", (pid,)).fetchone()["paypal_order_id"]
        self.mock_paypal.set_buyer_approved(order_id)
        self.mock_paypal.simulate_amount_tamper = True

        with mock.patch("backend.app.api.routes.get_thread_connection", return_value=self.conn):
            res = paypal_return(token=order_id)

        self.assertEqual(res["status"], "FAILED")
        self.assertIn("amount_mismatch", res["detail"])

        cur = self.conn.execute("SELECT status, status_reason FROM purchases WHERE id = ?;", (pid,))
        row = cur.fetchone()
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["status_reason"], "amount_mismatch")

    # 7. Policy Update: PUT /policy
    def test_update_policy_success_and_validations(self):
        from backend.app.api.routes import HTTPException

        with mock.patch("backend.app.api.routes.get_thread_connection", return_value=self.conn):
            # Valid update
            valid_req = UpdatePolicyRequest(
                daily_budget_cents=50000,
                max_per_tx_cents=20000,
                approval_threshold_cents=10000,
                velocity_cap_cents=15000,
                allowlist=["merchant_shoes", "merchant_coffee"],
            )
            pol = update_policy(payload=valid_req, agent_id="default", is_admin=True)
            self.assertEqual(pol.daily_budget_cents, 50000)
            self.assertEqual(pol.version, 2)

            # Verify policy_history table has appended record
            hist = self.conn.execute("SELECT * FROM policy_history WHERE agent_id = 'default' AND version = 2;").fetchone()
            self.assertIsNotNone(hist)
            hist_json = json.loads(hist["policy_json"])
            self.assertEqual(hist_json["daily_budget_cents"], 50000)

            # Invariant check: approval_threshold < max_per_tx
            invalid_thresh = UpdatePolicyRequest(
                daily_budget_cents=50000,
                max_per_tx_cents=20000,
                approval_threshold_cents=25000,  # > max_per_tx
                velocity_cap_cents=30000,
                allowlist=["merchant_shoes"],
            )
            with self.assertRaises(HTTPException) as ctx:
                update_policy(payload=invalid_thresh, agent_id="default", is_admin=True)
            self.assertEqual(ctx.exception.status_code, 422)

            # Invariant check: max_per_tx <= daily_budget
            invalid_tx = UpdatePolicyRequest(
                daily_budget_cents=50000,
                max_per_tx_cents=60000,  # > daily_budget
                approval_threshold_cents=10000,
                velocity_cap_cents=15000,
                allowlist=["merchant_shoes"],
            )
            with self.assertRaises(HTTPException) as ctx:
                update_policy(payload=invalid_tx, agent_id="default", is_admin=True)
            self.assertEqual(ctx.exception.status_code, 422)

            # Invariant check: unknown merchant
            invalid_merchant = UpdatePolicyRequest(
                daily_budget_cents=50000,
                max_per_tx_cents=20000,
                approval_threshold_cents=10000,
                velocity_cap_cents=15000,
                allowlist=["merchant_darkweb_illegal"],
            )
            with self.assertRaises(HTTPException) as ctx:
                update_policy(payload=invalid_merchant, agent_id="default", is_admin=True)
            self.assertEqual(ctx.exception.status_code, 422)

    # 8. Demo Reset: POST /demo/reset
    def test_demo_reset_forbidden_and_allowed(self):
        from backend.app.api.routes import HTTPException

        # When ALLOW_DEMO_RESET is false -> 403
        with mock.patch.dict(os.environ, {"ALLOW_DEMO_RESET": "false"}):
            with self.assertRaises(HTTPException) as ctx:
                demo_reset(is_admin=True)
            self.assertEqual(ctx.exception.status_code, 403)

        # When ALLOW_DEMO_RESET is true -> resets database cleanly
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as r_tmp:
            r_db = r_tmp.name

        try:
            with mock.patch.dict(os.environ, {"ALLOW_DEMO_RESET": "true", "DB_PATH": r_db}):
                with mock.patch("backend.app.db.connection.DEFAULT_DB_PATH", r_db):
                    res = demo_reset(is_admin=True)
                    self.assertEqual(res["status"], "ok")

                    test_c = create_connection(r_db)
                    try:
                        has_pol = test_c.execute("SELECT COUNT(*) AS c FROM policy WHERE agent_id = 'default';").fetchone()["c"]
                        self.assertEqual(has_pol, 1)
                    finally:
                        test_c.close()
        finally:
            close_thread_connection()
            for p in (r_db, f"{r_db}-wal", f"{r_db}-shm"):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass


if __name__ == "__main__":
    unittest.main()
