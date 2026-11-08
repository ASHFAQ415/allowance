"""
Regression tests for transactional atomicity of multi-row financial mutations.

Verifies that a failure part-way through cart submission rolls back every row
(purchase, ledger, event, financial fact, and budget projection) instead of
leaving a partially committed financial state.
"""

import os
import tempfile
import time
import unittest
from unittest import mock

from backend.app.db.connection import create_connection
from backend.app.policy.rules import CartItem, FrozenCart
from backend.app.orchestrator.core import submit_cart_to_orchestrator
from scripts.seed_db import seed_database


class TestTransactionAtomicity(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        seed_database(self.db_path)
        self.conn = create_connection(self.db_path)

        # Seed the run that the purchase will reference.
        self.conn.execute(
            """
            INSERT INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at)
            VALUES ('r_tx', 'tenant_demo', 'default', 'k_tx', 'h_tx', 1000.0, 'COMPLETED', '2026');
            """
        )

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_cart_submission_rolls_back_all_rows_on_mid_transaction_failure(self):
        """
        If the financial fact write fails after the purchase and ledger inserts,
        the whole submission must roll back. Under autocommit, the purchase and
        ledger rows would have survived.
        """
        cart = FrozenCart(
            items=[CartItem("shoes_trail_runner", "merchant_shoes", "Shoes", 4500, 1)],
            total_cents=4500,
            cart_hash="hash_tx_4500",
        )

        with mock.patch(
            "backend.app.orchestrator.core.record_financial_fact",
            side_effect=RuntimeError("simulated projection failure"),
        ):
            with self.assertRaises(RuntimeError):
                submit_cart_to_orchestrator(
                    self.conn, "tenant_demo", "default", "r_tx", cart, time.time()
                )

        for table in ("purchases", "ledger", "events", "financial_facts", "budget_accounting"):
            count = self.conn.execute(f"SELECT COUNT(*) AS c FROM {table};").fetchone()["c"]
            self.assertEqual(count, 0, f"{table} must be empty after rollback")


if __name__ == "__main__":
    unittest.main()
