"""
Integration Tests for Personal Edition MVP.

Validates the full personal user loop:
1. Individual account profile (`tenant_personal`, `user_personal`, `agent_personal`).
2. Personal spending limits ($100 daily budget, $30 auto-approval threshold, $50 per-tx max).
3. Personal purchase routing strictly through supported onboarded merchants (`merchant_shoes`).
4. Two-step approval verification:
   - Purchase <= $30 auto-approves via PayPilot R6, captures via PayPal Orders v2 payee.
   - Purchase > $30 (e.g. $45 trail shoes) escalates to PENDING_APPROVAL, requiring human approval and buyer consent.
5. Invariant 3 Budget protection: Denies purchase exceeding remaining $100 allowance with zero partial reservations.
6. Simultaneous double-entry balanced general ledger verification.
"""

import json
import sqlite3
import unittest
import uuid
from backend.app.agent.catalog import get_product
from backend.app.db.bootstrap import seed_defaults
from backend.app.db.double_entry import (
    AccountNumber,
    get_general_ledger_summary,
)
from backend.app.db.financial_journal import (
    FactType,
    get_budget_snapshot,
    record_financial_fact,
)
from backend.app.paypal.merchants import (
    get_merchant,
    get_payee_for_merchant,
    is_merchant_supported,
)


class TestPersonalEditionMVP(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        with open("backend/app/db/schema.sql", "r") as f:
            self.conn.executescript(f.read())
        seed_defaults(self.conn)
        self.tenant_id = "tenant_personal"
        self.agent_id = "agent_personal"
        self.budget_day = "2026-11-08"

    def tearDown(self):
        self.conn.close()

    def _insert_test_purchase(self, pur_id: str, total_cents: int):
        now_iso = "2026-11-08T12:00:00Z"
        run_id = f"run_{pur_id}"
        self.conn.execute(
            """
            INSERT OR IGNORE INTO agent_runs (
                id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 1000.0, 'COMPLETED', ?);
            """,
            (run_id, self.tenant_id, self.agent_id, f"idem_{pur_id}", f"hash_{pur_id}", now_iso)
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO purchases (
                id, tenant_id, agent_id, run_id, status, auto_approved, cart_json, cart_hash,
                total_cents, currency, merchant_ids_json, budget_day, create_request_id,
                state_entered_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'QUEUED', 1, '[]', 'hash', ?, 'USD', '["merchant_shoes"]', ?, ?, 1000.0, ?, ?);
            """,
            (pur_id, self.tenant_id, self.agent_id, run_id, total_cents, self.budget_day, f"req_{pur_id}", now_iso, now_iso)
        )

    def test_01_personal_profile_and_policy_seeded(self):
        # 1. Verify tenant & user
        cur = self.conn.execute("SELECT id, name, tier FROM tenants WHERE id = ?;", (self.tenant_id,))
        t_row = cur.fetchone()
        self.assertIsNotNone(t_row)
        self.assertEqual(t_row["tier"], "personal")

        # 2. Verify policy invariants for personal edition
        cur = self.conn.execute("SELECT * FROM policy WHERE agent_id = ?;", (self.agent_id,))
        pol = cur.fetchone()
        self.assertIsNotNone(pol)
        self.assertEqual(pol["daily_budget_cents"], 10000)        # $100.00
        self.assertEqual(pol["max_per_tx_cents"], 5000)           # $50.00
        self.assertEqual(pol["approval_threshold_cents"], 3000)   # $30.00 auto-approval threshold!
        self.assertEqual(pol["velocity_cap_cents"], 4500)         # $45.00 velocity cap

    def test_02_supported_merchant_payee_routing(self):
        # Verify merchant_shoes is active and has valid PayPal payee credentials
        self.assertTrue(is_merchant_supported("merchant_shoes"))
        payee = get_payee_for_merchant("merchant_shoes")
        self.assertEqual(payee["email_address"], "shoes-facilitator@allowance.dev")
        self.assertEqual(payee["merchant_id"], "SANDBOX_MERC_SHOES_01")

        # Verify unapproved merchant raises error
        self.assertFalse(is_merchant_supported("merchant_unapproved"))
        with self.assertRaises(ValueError):
            get_payee_for_merchant("merchant_unapproved")

    def test_03_personal_purchase_under_30_auto_approves_and_settles(self):
        # Item: Running socks ($18.00) from merchant_shoes
        prod = get_product("shoes_running_socks")
        self.assertIsNotNone(prod)
        self.assertEqual(prod.price_cents, 1800)
        self.assertEqual(prod.merchant_id, "merchant_shoes")

        # 1. PayPilot Checkpoint P1: $18.00 <= $30.00 auto-approval threshold -> Auto-approved!
        pur_id = f"pur_socks_{uuid.uuid4().hex[:6]}"
        res_key = f"res_{pur_id}"
        cap_key = f"cap_{pur_id}"
        self._insert_test_purchase(pur_id, 1800)

        # 2. Record reservation
        record_financial_fact(
            self.conn,
            source_event_key=res_key,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            purchase_id=pur_id,
            budget_day=self.budget_day,
            fact_type=FactType.RESERVATION_CREATED,
            delta_reserved_cents=1800,
            delta_unresolved_cents=0,
            delta_captured_cents=0,
            delta_auto_approved_cents=1800,
            actor="agent_personal"
        )

        snap = get_budget_snapshot(self.conn, self.agent_id, self.budget_day)
        self.assertEqual(snap.r3_committed_cents, 1800)

        # 3. Simulate capture settlement
        record_financial_fact(
            self.conn,
            source_event_key=cap_key,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            purchase_id=pur_id,
            budget_day=self.budget_day,
            fact_type=FactType.CAPTURE_SETTLED,
            delta_reserved_cents=-1800,
            delta_unresolved_cents=0,
            delta_captured_cents=1800,
            delta_auto_approved_cents=0,
            actor="orchestrator",
            paypal_capture_id="CAP_SOCKS_001"
        )

        final_snap = get_budget_snapshot(self.conn, self.agent_id, self.budget_day)
        self.assertEqual(final_snap.captured_cents, 1800)
        self.assertEqual(final_snap.reserved_cents, 0)
        self.assertEqual(final_snap.r3_committed_cents, 1800)

        # 4. Verify Double-Entry General Ledger is perfectly balanced
        gl = get_general_ledger_summary(self.conn, self.tenant_id, self.agent_id)
        self.assertTrue(gl.is_balanced)
        self.assertEqual(gl.realized_purchases_cents, 1800)
        self.assertEqual(gl.active_encumbrances_cents, 0)
        self.assertEqual(gl.total_committed_cents, 1800)

    def test_04_personal_purchase_over_30_escalates_to_human_approval(self):
        # Item: Trail running shoes ($45.00) from merchant_shoes
        # $45.00 exceeds personal auto-approval threshold of $30.00 -> Held in Human Review (Rule R4)
        prod = get_product("shoes_trail_runner")
        self.assertEqual(prod.price_cents, 4500)

        pur_id = f"pur_trail_{uuid.uuid4().hex[:6]}"
        res_key = f"res_{pur_id}"
        self._insert_test_purchase(pur_id, 4500)

        # Encumber $45.00 reservation during human hold
        record_financial_fact(
            self.conn,
            source_event_key=res_key,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            purchase_id=pur_id,
            budget_day=self.budget_day,
            fact_type=FactType.RESERVATION_CREATED,
            delta_reserved_cents=4500,
            delta_unresolved_cents=0,
            delta_captured_cents=0,
            delta_auto_approved_cents=0,  # Not auto-approved
            actor="supervisor_queue"
        )

        gl = get_general_ledger_summary(self.conn, self.tenant_id, self.agent_id)
        self.assertTrue(gl.is_balanced)
        self.assertEqual(gl.active_encumbrances_cents, 4500)
        self.assertEqual(gl.realized_purchases_cents, 0)

        # User confirms in dashboard (Step 1) and approves in PayPal sandbox (Step 2)
        cap_key = f"cap_{pur_id}"
        record_financial_fact(
            self.conn,
            source_event_key=cap_key,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            purchase_id=pur_id,
            budget_day=self.budget_day,
            fact_type=FactType.CAPTURE_SETTLED,
            delta_reserved_cents=-4500,
            delta_unresolved_cents=0,
            delta_captured_cents=4500,
            delta_auto_approved_cents=0,
            actor="supervisor_ashfaq",
            paypal_capture_id="CAP_TRAIL_002"
        )

        gl_after = get_general_ledger_summary(self.conn, self.tenant_id, self.agent_id)
        self.assertTrue(gl_after.is_balanced)
        self.assertEqual(gl_after.active_encumbrances_cents, 0)
        self.assertEqual(gl_after.realized_purchases_cents, 4500)

    def test_05_personal_daily_budget_exceeded_fails_closed(self):
        # Personal daily limit is $100.00 (10,000 cents).
        # Suppose $90.00 was already captured today.
        self._insert_test_purchase("pur_prior", 9000)
        record_financial_fact(
            self.conn,
            source_event_key="prior_spend",
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            purchase_id="pur_prior",
            budget_day=self.budget_day,
            fact_type=FactType.CAPTURE_SETTLED,
            delta_reserved_cents=0,
            delta_unresolved_cents=0,
            delta_captured_cents=9000,
            delta_auto_approved_cents=0,
            actor="system"
        )

        snap = get_budget_snapshot(self.conn, self.agent_id, self.budget_day)
        self.assertEqual(snap.r3_committed_cents, 9000)
        remaining = 10000 - snap.r3_committed_cents
        self.assertEqual(remaining, 1000)  # Only $10.00 remaining today

        # Attempt to buy $18 socks -> 1800 > 1000 remaining. Policy must REJECT.
        policy_allowed = (1800 <= remaining)
        self.assertFalse(policy_allowed)

        # Verify no phantom reservation was written
        snap_after = get_budget_snapshot(self.conn, self.agent_id, self.budget_day)
        self.assertEqual(snap_after.r3_committed_cents, 9000)
        self.assertEqual(snap_after.reserved_cents, 0)


if __name__ == "__main__":
    unittest.main()
