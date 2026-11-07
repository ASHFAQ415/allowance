"""
Unit Tests for Financial Journal and Budget Accounting Projection.
Verifies append-only financial_facts, projection updates, deterministic rebuilds, and idempotency.
"""

import os
import tempfile
import unittest

from backend.app.db.connection import create_connection, init_db, transactional_session
from backend.app.db.financial_journal import (
    FactType,
    record_financial_fact,
    get_budget_snapshot,
    rebuild_budget_accounting,
)


class TestFinancialJournal(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        init_db(self.db_path)
        self.conn = create_connection(self.db_path)

        # Seed agent and purchase
        self.conn.execute("INSERT INTO agents (id, tenant_id, name, created_at) VALUES ('a1', 't1', 'Agent1', '2026-11-06T00:00:00Z');")
        self.conn.execute(
            """
            INSERT INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at)
            VALUES ('r1', 't1', 'a1', 'k1', 'h1', 1000.0, 'COMPLETED', '2026-11-06T00:00:00Z');
            """
        )
        self.conn.execute(
            """
            INSERT INTO purchases (id, tenant_id, agent_id, run_id, status, auto_approved, cart_json, cart_hash,
                                  total_cents, merchant_ids_json, budget_day, create_request_id, state_entered_at,
                                  created_at, updated_at)
            VALUES ('p1', 't1', 'a1', 'r1', 'CART_READY', 1, '{}', 'h1', 4500, '[]', '2026-11-06', 'req1', 1000.0,
                    '2026-11-06T00:00:00Z', '2026-11-06T00:00:00Z');
            """
        )

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_reservation_and_capture_accounting_lifecycle(self):
        """Verifies reserved -> captured accounting moves correctly."""
        with transactional_session(self.conn):
            # 1. Create reservation for $45.00 auto-approved
            _, s1 = record_financial_fact(
                conn=self.conn,
                source_event_key="ev_res_1",
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
        self.assertEqual(s1.reserved_cents, 4500)
        self.assertEqual(s1.captured_cents, 0)
        self.assertEqual(s1.unresolved_cents, 0)
        self.assertEqual(s1.r3_committed_cents, 4500)
        self.assertEqual(s1.auto_approved_committed_cents, 4500)

        with transactional_session(self.conn):
            # 2. Settle capture ($45.00 moves from reserved to captured)
            _, s2 = record_financial_fact(
                conn=self.conn,
                source_event_key="ev_cap_1",
                tenant_id="t1",
                agent_id="a1",
                purchase_id="p1",
                budget_day="2026-11-06",
                fact_type=FactType.CAPTURE_SETTLED,
                delta_reserved_cents=-4500,
                delta_unresolved_cents=0,
                delta_captured_cents=4500,
                delta_auto_approved_cents=0,
                actor="paypal",
                paypal_capture_id="CAP_123"
            )
        self.assertEqual(s2.reserved_cents, 0)
        self.assertEqual(s2.captured_cents, 4500)
        self.assertEqual(s2.unresolved_cents, 0)
        self.assertEqual(s2.r3_committed_cents, 4500)  # Total commitment unchanged!
        self.assertEqual(s2.auto_approved_committed_cents, 4500)

    def test_unresolved_capture_accounting(self):
        """Ambiguous capture moves reserved -> unresolved, total commitment stays identical."""
        with transactional_session(self.conn):
            record_financial_fact(
                conn=self.conn,
                source_event_key="ev_res_2",
                tenant_id="t1",
                agent_id="a1",
                purchase_id="p1",
                budget_day="2026-11-06",
                fact_type=FactType.RESERVATION_CREATED,
                delta_reserved_cents=5000,
                delta_unresolved_cents=0,
                delta_captured_cents=0,
                delta_auto_approved_cents=0,
                actor="admin"
            )
            _, s = record_financial_fact(
                conn=self.conn,
                source_event_key="ev_unk_2",
                tenant_id="t1",
                agent_id="a1",
                purchase_id="p1",
                budget_day="2026-11-06",
                fact_type=FactType.CAPTURE_BECAME_UNKNOWN,
                delta_reserved_cents=-5000,
                delta_unresolved_cents=5000,
                delta_captured_cents=0,
                delta_auto_approved_cents=0,
                actor="orchestrator"
            )
        self.assertEqual(s.reserved_cents, 0)
        self.assertEqual(s.unresolved_cents, 5000)
        self.assertEqual(s.captured_cents, 0)
        self.assertEqual(s.r3_committed_cents, 5000)

    def test_rebuild_budget_accounting_matches_facts(self):
        """Verifies deterministic rebuild from immutable financial_facts."""
        with transactional_session(self.conn):
            record_financial_fact(
                conn=self.conn,
                source_event_key="ev_1",
                tenant_id="t1", agent_id="a1", purchase_id="p1",
                budget_day="2026-11-06", fact_type=FactType.RESERVATION_CREATED,
                delta_reserved_cents=3000, delta_unresolved_cents=0,
                delta_captured_cents=0, delta_auto_approved_cents=3000, actor="agent"
            )
            record_financial_fact(
                conn=self.conn,
                source_event_key="ev_2",
                tenant_id="t1", agent_id="a1", purchase_id="p1",
                budget_day="2026-11-06", fact_type=FactType.CAPTURE_SETTLED,
                delta_reserved_cents=-3000, delta_unresolved_cents=0,
                delta_captured_cents=3000, delta_auto_approved_cents=0, actor="paypal"
            )

        # Corrupt projection manually
        self.conn.execute("UPDATE budget_accounting SET captured_cents = 999999 WHERE agent_id = 'a1';")
        bad = get_budget_snapshot(self.conn, "a1", "2026-11-06")
        self.assertEqual(bad.captured_cents, 999999)

        # Rebuild from facts
        with transactional_session(self.conn):
            rebuilt = rebuild_budget_accounting(self.conn, "a1", "2026-11-06")

        self.assertEqual(rebuilt.captured_cents, 3000)
        self.assertEqual(rebuilt.reserved_cents, 0)
        self.assertEqual(rebuilt.r3_committed_cents, 3000)

    def test_idempotent_source_event_key(self):
        """Re-recording the same source_event_key does not double-count."""
        with transactional_session(self.conn):
            _, s1 = record_financial_fact(
                conn=self.conn,
                source_event_key="dup_key",
                tenant_id="t1", agent_id="a1", purchase_id="p1",
                budget_day="2026-11-06", fact_type=FactType.RESERVATION_CREATED,
                delta_reserved_cents=2500, delta_unresolved_cents=0,
                delta_captured_cents=0, delta_auto_approved_cents=2500, actor="agent"
            )
            _, s2 = record_financial_fact(
                conn=self.conn,
                source_event_key="dup_key",
                tenant_id="t1", agent_id="a1", purchase_id="p1",
                budget_day="2026-11-06", fact_type=FactType.RESERVATION_CREATED,
                delta_reserved_cents=2500, delta_unresolved_cents=0,
                delta_captured_cents=0, delta_auto_approved_cents=2500, actor="agent"
            )
        self.assertEqual(s1.reserved_cents, 2500)
        self.assertEqual(s2.reserved_cents, 2500)


if __name__ == "__main__":
    unittest.main()

