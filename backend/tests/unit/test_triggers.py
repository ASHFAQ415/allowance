"""
Unit Tests for Database Immutability Triggers.
Verifies that SQLite triggers strictly abort UPDATE and DELETE on ledger,
financial_facts, and policy_history.
"""

import os
import sqlite3
import tempfile
import unittest

from backend.app.db.connection import create_connection, init_db


class TestDatabaseTriggers(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        init_db(self.db_path)
        self.conn = create_connection(self.db_path)
        
        # Seed minimal tenant, agent, run, purchase
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

    def test_ledger_update_aborts(self):
        """Updating ledger rows must raise sqlite3.IntegrityError."""
        self.conn.execute(
            """
            INSERT INTO ledger (id, purchase_id, agent_id, from_state, to_state, rule_name, reason,
                               checkpoint, amount_cents, policy_version, actor, created_at)
            VALUES ('l1', 'p1', 'a1', 'CREATED', 'CART_READY', 'R6_AUTO_APPROVE', 'ok', 'P1', 4500, 1, 'agent', '2026-11-06T00:00:00Z');
            """
        )
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.conn.execute("UPDATE ledger SET amount_cents = 0 WHERE id = 'l1';")
        self.assertIn("Ledger entries are strictly immutable", str(ctx.exception))

    def test_ledger_delete_aborts(self):
        """Deleting ledger rows must raise sqlite3.IntegrityError."""
        self.conn.execute(
            """
            INSERT INTO ledger (id, purchase_id, agent_id, from_state, to_state, rule_name, reason,
                               checkpoint, amount_cents, policy_version, actor, created_at)
            VALUES ('l2', 'p1', 'a1', 'CREATED', 'CART_READY', 'R6_AUTO_APPROVE', 'ok', 'P1', 4500, 1, 'agent', '2026-11-06T00:00:00Z');
            """
        )
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.conn.execute("DELETE FROM ledger WHERE id = 'l2';")
        self.assertIn("Ledger entries cannot be deleted", str(ctx.exception))

    def test_financial_facts_update_aborts(self):
        """Updating financial_facts must raise sqlite3.IntegrityError."""
        self.conn.execute(
            """
            INSERT INTO financial_facts (id, source_event_key, tenant_id, agent_id, purchase_id, budget_day,
                                        currency, fact_type, delta_reserved_cents, delta_unresolved_cents,
                                        delta_captured_cents, delta_auto_approved_cents, actor, created_at)
            VALUES ('f1', 'ev1', 't1', 'a1', 'p1', '2026-11-06', 'USD', 'RESERVATION_CREATED', 4500, 0, 0, 4500, 'agent', '2026-11-06T00:00:00Z');
            """
        )
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.conn.execute("UPDATE financial_facts SET delta_reserved_cents = 0 WHERE id = 'f1';")
        self.assertIn("Financial facts are strictly immutable", str(ctx.exception))

    def test_financial_facts_delete_aborts(self):
        """Deleting financial_facts must raise sqlite3.IntegrityError."""
        self.conn.execute(
            """
            INSERT INTO financial_facts (id, source_event_key, tenant_id, agent_id, purchase_id, budget_day,
                                        currency, fact_type, delta_reserved_cents, delta_unresolved_cents,
                                        delta_captured_cents, delta_auto_approved_cents, actor, created_at)
            VALUES ('f2', 'ev2', 't1', 'a1', 'p1', '2026-11-06', 'USD', 'RESERVATION_CREATED', 4500, 0, 0, 4500, 'agent', '2026-11-06T00:00:00Z');
            """
        )
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.conn.execute("DELETE FROM financial_facts WHERE id = 'f2';")
        self.assertIn("Financial facts cannot be deleted", str(ctx.exception))

    def test_policy_history_update_aborts(self):
        """Updating policy_history must raise sqlite3.IntegrityError."""
        self.conn.execute(
            """
            INSERT INTO policy_history (id, agent_id, version, policy_json, created_at)
            VALUES ('ph1', 'a1', 1, '{}', '2026-11-06T00:00:00Z');
            """
        )
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.conn.execute("UPDATE policy_history SET policy_json = '{\"tampered\": true}' WHERE id = 'ph1';")
        self.assertIn("Policy history is strictly immutable", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

