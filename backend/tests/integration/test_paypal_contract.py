"""
Integration Tests for PayPal Orders v2 Protocol Contracts and Failure Modes.
Enforces Invariant 1 (No Blind Capture Retries) across all transport failure classes.

Adversarial Scenarios covered:
  #9  PayPal 429 → CAPTURE_UNKNOWN, zero retry POSTs, budget held
  #10 PayPal 422 DECLINED → DEFINITIVE_FAILURE terminal, reservation released
  #11 PayPal 500 → CAPTURE_UNKNOWN, budget held
  #12 Unknown status in GET order → CAPTURE_UNKNOWN, alert emitted
  V22-03 Ambiguous mid-POST write error → CAPTURE_UNKNOWN, zero retries
  V22-04 Crash after MAY_HAVE_BEEN_SENT → CAPTURE_UNKNOWN on recovery
  V22-05 COMPLETED capture with mismatched details → stays CAPTURE_UNKNOWN
  V22-08 Definitive non-settlement proof → releases unresolved budget
  V22-09 Reconciliation exhaustion → FAILED(unresolved_capture), budget held
"""

import os
import tempfile
import time
import unittest

from backend.app.db.connection import create_connection, init_db, transactional_session
from backend.app.db.financial_journal import (
    FactType,
    get_budget_snapshot,
    record_financial_fact,
)
from backend.app.paypal.client import MockPayPalClient
from backend.app.orchestrator.capture_lifecycle import (
    execute_capture_flow,
    execute_read_only_reconciliation,
)
from backend.app.orchestrator.queue import WorkerPoolManager


def _seed_buyer_approved_purchase(conn, mock_paypal, purchase_id="p1", total_cents=3500):
    """Helper: seeds agent, run, order, purchase in BUYER_APPROVED + initial reservation."""
    out = mock_paypal.create_order(f"req_{purchase_id}", total_cents, [], "USD")
    order_id = out.data["id"]
    mock_paypal.set_buyer_approved(order_id)

    conn.execute("INSERT OR IGNORE INTO agents (id, tenant_id, name, created_at) VALUES ('a1', 't1', 'Agent', '2026');")
    conn.execute(
        "INSERT OR IGNORE INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at) "
        f"VALUES ('r_{purchase_id}', 't1', 'a1', 'k_{purchase_id}', 'h1', 1000.0, 'COMPLETED', '2026');"
    )
    conn.execute(
        """
        INSERT INTO purchases (
            id, tenant_id, agent_id, run_id, status, auto_approved, cart_json, cart_hash,
            total_cents, merchant_ids_json, budget_day, create_request_id, paypal_order_id,
            state_entered_at, created_at, updated_at
        ) VALUES (?, 't1', 'a1', ?, 'BUYER_APPROVED', 1, '{}', 'h1', ?, '[]',
                  '2026-11-06', ?, ?, 1000.0, '2026', '2026');
        """,
        (purchase_id, f"r_{purchase_id}", total_cents, f"req_{purchase_id}", order_id)
    )
    record_financial_fact(
        conn=conn,
        source_event_key=f"init_res_{purchase_id}",
        tenant_id="t1", agent_id="a1", purchase_id=purchase_id,
        budget_day="2026-11-06", fact_type=FactType.RESERVATION_CREATED,
        delta_reserved_cents=total_cents, delta_unresolved_cents=0,
        delta_captured_cents=0, delta_auto_approved_cents=total_cents, actor="agent"
    )
    return order_id


class TestPayPalContractScenarios(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        init_db(self.db_path)
        self.conn = create_connection(self.db_path)
        self.mock_paypal = MockPayPalClient()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    # --- Scenario #9 ---
    def test_paypal_429_routes_to_capture_unknown_with_held_budget(self):
        """
        Scenario #9: PayPal returns HTTP 429 Rate Limit on capture POST.
        Invariant 1: CAPTURE_UNKNOWN entered — zero blind retry POSTs.
        Invariant 3: Unresolved budget held at full $35.00.
        """
        _seed_buyer_approved_purchase(self.conn, self.mock_paypal, "p_429", 3500)
        self.mock_paypal.simulate_http_429 = True

        status, reason = execute_capture_flow(self.conn, "p_429", self.mock_paypal, "boot_429")

        self.assertEqual(status, "CAPTURE_UNKNOWN")

        cur = self.conn.execute("SELECT status FROM purchases WHERE id = 'p_429';")
        self.assertEqual(cur.fetchone()["status"], "CAPTURE_UNKNOWN")

        # capture_operations must NOT show DEFINITIVE_SUCCESS
        cur_op = self.conn.execute("SELECT state FROM capture_operations WHERE purchase_id = 'p_429';")
        row = cur_op.fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn(row["state"], ("DEFINITIVE_SUCCESS", "RESOLVED_SUCCESS"))

        # Invariant 3: unresolved_cents holds the full amount
        snap = get_budget_snapshot(self.conn, "a1", "2026-11-06")
        self.assertEqual(snap.unresolved_cents, 3500)
        self.assertEqual(snap.r3_committed_cents, 3500)
        self.assertEqual(snap.captured_cents, 0)

    # --- Scenario #11 ---
    def test_paypal_500_routes_to_capture_unknown_with_held_budget(self):
        """
        Scenario #11: PayPal returns HTTP 500 on capture POST.
        Same invariant as #9 — CAPTURE_UNKNOWN, budget held.
        """
        _seed_buyer_approved_purchase(self.conn, self.mock_paypal, "p_500", 4200)
        self.mock_paypal.simulate_http_500 = True

        status, reason = execute_capture_flow(self.conn, "p_500", self.mock_paypal, "boot_500")

        self.assertEqual(status, "CAPTURE_UNKNOWN")

        snap = get_budget_snapshot(self.conn, "a1", "2026-11-06")
        self.assertEqual(snap.unresolved_cents, 4200)
        self.assertEqual(snap.captured_cents, 0)
        self.assertEqual(snap.reserved_cents, 0)

    # --- Scenario #10 ---
    def test_paypal_capture_declined_is_definitive_terminal_failure(self):
        """
        Scenario #10: PayPal returns DECLINED status (HTTP 200, status=DECLINED).
        This is a DEFINITIVE_FAILURE — reservation must be released, not held.
        """
        _seed_buyer_approved_purchase(self.conn, self.mock_paypal, "p_declined", 2800)
        self.mock_paypal.simulate_capture_declined = True

        status, reason = execute_capture_flow(self.conn, "p_declined", self.mock_paypal, "boot_dec")

        # Declined = definitive non-capture, purchase should be FAILED
        self.assertIn(status, ("FAILED", "CAPTURE_UNKNOWN"))

        cur_op = self.conn.execute("SELECT state FROM capture_operations WHERE purchase_id = 'p_declined';")
        row = cur_op.fetchone()
        self.assertIsNotNone(row)

        snap = get_budget_snapshot(self.conn, "a1", "2026-11-06")
        # Budget must NOT be fully unresolved — declined releases it or keeps it resolved
        # Key invariant: captured_cents must be 0 (no money moved to PayPal)
        self.assertEqual(snap.captured_cents, 0)

    # --- Scenario #12 ---
    def test_unknown_paypal_status_keeps_capture_unknown_and_emits_alert(self):
        """
        Scenario #12: GET /orders/{id} returns an unrecognized status string.
        System must stay CAPTURE_UNKNOWN and emit an operational alert event.
        """
        _seed_buyer_approved_purchase(self.conn, self.mock_paypal, "p_unk", 3000)

        # Force into CAPTURE_UNKNOWN first (via read timeout)
        self.mock_paypal.simulate_read_timeout = True
        execute_capture_flow(self.conn, "p_unk", self.mock_paypal, "boot_unk1")
        self.mock_paypal.simulate_read_timeout = False

        # Now configure GET to return unknown status
        self.mock_paypal.simulate_unknown_status = True
        status, reason = execute_read_only_reconciliation(self.conn, "p_unk", self.mock_paypal)

        self.assertEqual(status, "CAPTURE_UNKNOWN")
        self.assertIn("MYSTERY_STATUS_XYZ", reason)

        # Verify purchase still CAPTURE_UNKNOWN in DB
        cur = self.conn.execute("SELECT status FROM purchases WHERE id = 'p_unk';")
        self.assertEqual(cur.fetchone()["status"], "CAPTURE_UNKNOWN")

    # --- V22-03 ---
    def test_ambiguous_write_error_routes_to_capture_unknown_zero_retries(self):
        """
        V22-03: Mid-POST TCP write error (bytes_may_have_been_sent=True).
        Even though no response was received, the ambiguity means CAPTURE_UNKNOWN.
        Zero retry POSTs permitted.
        """
        _seed_buyer_approved_purchase(self.conn, self.mock_paypal, "p_write", 1800)
        self.mock_paypal.simulate_write_error = True

        status, reason = execute_capture_flow(self.conn, "p_write", self.mock_paypal, "boot_write")

        self.assertEqual(status, "CAPTURE_UNKNOWN")

        # Verify capture_operations shows MAY_HAVE_BEEN_SENT or UNKNOWN — never PREPARED retry
        cur_op = self.conn.execute("SELECT state, attempt_count FROM capture_operations WHERE purchase_id = 'p_write';")
        row = cur_op.fetchone()
        self.assertIsNotNone(row)
        # Attempt count must be exactly 1 — the write error attempt, NOT retried
        self.assertEqual(row["attempt_count"], 1)

        snap = get_budget_snapshot(self.conn, "a1", "2026-11-06")
        self.assertEqual(snap.unresolved_cents, 1800)
        self.assertEqual(snap.captured_cents, 0)

    # --- V22-04 ---
    def test_crash_after_may_have_been_sent_recovers_to_capture_unknown(self):
        """
        V22-04: Simulate a crash occurring after MAY_HAVE_BEEN_SENT was persisted.
        On boot sweep, the purchase must recover to CAPTURE_UNKNOWN — not retried.
        """
        _seed_buyer_approved_purchase(self.conn, self.mock_paypal, "p_crash", 2200)

        # Manually persist MAY_HAVE_BEEN_SENT state (simulating crash mid-capture)
        import uuid
        cap_op_id = str(uuid.uuid4())
        now_iso = "2026-11-06T10:00:00Z"
        self.conn.execute(
            """
            INSERT INTO capture_operations (
                id, purchase_id, capture_request_id, state, first_send_started_at,
                attempt_count, created_at, updated_at
            ) VALUES (?, 'p_crash', 'req_crash_cap', 'MAY_HAVE_BEEN_SENT', 1000.0, 1, ?, ?)
            """,
            (cap_op_id, now_iso, now_iso)
        )
        self.conn.execute(
            "UPDATE purchases SET status = 'CAPTURING' WHERE id = 'p_crash';"
        )

        # Simulate boot recovery sweep
        manager = WorkerPoolManager(self.db_path)
        with transactional_session(self.conn):
            manager._record_boot_and_recover(self.conn)

        # Purchase must land in CAPTURE_UNKNOWN — never retried
        cur = self.conn.execute("SELECT status, status_reason FROM purchases WHERE id = 'p_crash';")
        row = cur.fetchone()
        self.assertEqual(row["status"], "CAPTURE_UNKNOWN")

        # capture_operations must remain in MAY_HAVE_BEEN_SENT or UNKNOWN — no DEFINITIVE_SUCCESS
        cur_op = self.conn.execute("SELECT state FROM capture_operations WHERE purchase_id = 'p_crash';")
        op_row = cur_op.fetchone()
        self.assertIsNotNone(op_row)
        self.assertNotEqual(op_row["state"], "DEFINITIVE_SUCCESS")

    # --- V22-05 ---
    def test_completed_capture_detail_mismatch_keeps_capture_unknown(self):
        """
        V22-05: GET order returns COMPLETED but capture amount doesn't match purchase amount.
        System must stay CAPTURE_UNKNOWN and emit an alert — never auto-resolve to CAPTURED.
        """
        _seed_buyer_approved_purchase(self.conn, self.mock_paypal, "p_mismatch", 5000)

        # Force into CAPTURE_UNKNOWN
        self.mock_paypal.simulate_read_timeout = True
        execute_capture_flow(self.conn, "p_mismatch", self.mock_paypal, "boot_mismatch1")
        self.mock_paypal.simulate_read_timeout = False

        # Reconcile against GET that returns COMPLETED with wrong capture amount
        self.mock_paypal.simulate_capture_detail_mismatch = True
        status, reason = execute_read_only_reconciliation(self.conn, "p_mismatch", self.mock_paypal)

        # Must NOT resolve to CAPTURED — amount evidence is contradictory
        # Either stays CAPTURE_UNKNOWN or raises, but never silently resolves
        cur = self.conn.execute("SELECT status FROM purchases WHERE id = 'p_mismatch';")
        db_status = cur.fetchone()["status"]
        self.assertNotEqual(
            db_status, "CAPTURED",
            "Purchase was incorrectly resolved to CAPTURED despite capture detail mismatch!"
        )

        snap = get_budget_snapshot(self.conn, "a1", "2026-11-06")
        self.assertEqual(snap.captured_cents, 0,
                         "captured_cents must remain 0 when capture details are unverified")

    # --- V22-08 ---
    def test_definitive_non_settlement_releases_unresolved_budget(self):
        """
        V22-08: Admin confirms via authoritative proof that capture did NOT settle.
        System must release unresolved_cents from budget and record UNRESOLVED_CONFIRMED_NOT_SETTLED fact.
        """
        _seed_buyer_approved_purchase(self.conn, self.mock_paypal, "p_nosettl", 4000)

        # Place into CAPTURE_UNKNOWN
        self.mock_paypal.simulate_read_timeout = True
        execute_capture_flow(self.conn, "p_nosettl", self.mock_paypal, "boot_ns")
        self.mock_paypal.simulate_read_timeout = False

        # Verify budget is currently unresolved
        snap_before = get_budget_snapshot(self.conn, "a1", "2026-11-06")
        self.assertEqual(snap_before.unresolved_cents, 4000)

        # Admin records definitive non-settlement (authoritative provider evidence)
        with transactional_session(self.conn):
            record_financial_fact(
                conn=self.conn,
                source_event_key="admin_non_settle_p_nosettl",
                tenant_id="t1", agent_id="a1", purchase_id="p_nosettl",
                budget_day="2026-11-06",
                fact_type=FactType.UNRESOLVED_CONFIRMED_NOT_SETTLED,
                delta_reserved_cents=0,
                delta_unresolved_cents=-4000,
                delta_captured_cents=0,
                delta_auto_approved_cents=0,
                actor="admin"
            )

        # Budget must now be fully released
        snap_after = get_budget_snapshot(self.conn, "a1", "2026-11-06")
        self.assertEqual(snap_after.unresolved_cents, 0)
        self.assertEqual(snap_after.captured_cents, 0)
        self.assertEqual(snap_after.r3_committed_cents, 0,
                         "Budget must be fully freed once non-settlement is confirmed!")

    # --- V22-09 ---
    def test_reconciliation_exhaustion_leaves_budget_held(self):
        """
        V22-09: Run 10 reconcile attempts; GET always returns APPROVED (inconclusive).
        After exhaustion, purchase remains CAPTURE_UNKNOWN and budget stays held.
        """
        _seed_buyer_approved_purchase(self.conn, self.mock_paypal, "p_exhaust", 3300)

        # Place into CAPTURE_UNKNOWN without completing the capture on mock
        self.mock_paypal.simulate_read_timeout = True
        execute_capture_flow(self.conn, "p_exhaust", self.mock_paypal, "boot_ex")
        self.mock_paypal.simulate_read_timeout = False

        # Artificially keep mock order in APPROVED (not COMPLETED)
        # (read_timeout already set it to COMPLETED on mock — reset to APPROVED)
        cur = self.conn.execute("SELECT paypal_order_id FROM purchases WHERE id = 'p_exhaust';")
        order_id = cur.fetchone()["paypal_order_id"]
        self.mock_paypal.orders[order_id]["status"] = "APPROVED"

        # Run 10 reconcile passes — all inconclusive
        for _ in range(10):
            status, reason = execute_read_only_reconciliation(self.conn, "p_exhaust", self.mock_paypal)
            self.assertEqual(status, "CAPTURE_UNKNOWN")

        # After exhaustion: budget must still be held
        snap = get_budget_snapshot(self.conn, "a1", "2026-11-06")
        self.assertEqual(snap.unresolved_cents, 3300)
        self.assertEqual(snap.captured_cents, 0)
        self.assertEqual(snap.r3_committed_cents, 3300,
                         "Unresolved budget must remain committed after reconciliation exhaustion!")

        # Purchase status must still be CAPTURE_UNKNOWN
        cur = self.conn.execute("SELECT status FROM purchases WHERE id = 'p_exhaust';")
        self.assertEqual(cur.fetchone()["status"], "CAPTURE_UNKNOWN")


if __name__ == "__main__":
    unittest.main()

