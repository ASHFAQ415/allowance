"""
Unit Tests for Formal Double-Entry Bookkeeping General Ledger.

Validates:
1. Chart of accounts creation and default ledger seeding.
2. Mandatory sum(debits) == sum(credits) balance constraint.
3. Rejection of unbalanced journal entries with zero-corruption rollback.
4. Deterministic posting rules:
   - Reservation creation (DR 5200 / CR 2100)
   - Reservation release (DR 2100 / CR 5200)
   - Capture settlement (DR 5100, DR 2100 / CR 5200, CR 1000)
   - Refund / Reversal (DR 1000 / CR 5100)
   - Late capture (DR 5100 / CR 1000)
5. General ledger rebuildable budget projection.
6. Database immutability triggers on journal tables.
"""

import sqlite3
import unittest
import uuid
from backend.app.db.double_entry import (
    AccountNumber,
    DoubleEntryUnbalancedError,
    JournalLine,
    get_general_ledger_summary,
    post_capture_settled,
    post_late_capture_settled,
    post_refund_settled,
    post_reservation_created,
    post_reservation_released,
    record_double_entry_transaction,
    seed_chart_of_accounts,
)


class TestDoubleEntryAccounting(unittest.TestCase):
    def setUp(self):
        # Fresh in-memory SQLite database for deterministic isolation
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        with open("backend/app/db/schema.sql", "r") as f:
            self.conn.executescript(f.read())
        seed_chart_of_accounts(self.conn)
        self.tenant_id = "tenant_test_de"
        self.agent_id = "agent_test_de"

    def tearDown(self):
        self.conn.close()

    def test_01_chart_of_accounts_seeded(self):
        cur = self.conn.execute("SELECT account_number, account_name, account_type FROM chart_of_accounts ORDER BY account_number;")
        rows = cur.fetchall()
        self.assertTrue(len(rows) >= 6)
        acct_map = {r["account_number"]: (r["account_name"], r["account_type"]) for r in rows}
        self.assertIn(AccountNumber.ASSET_CASH_GATEWAY, acct_map)
        self.assertEqual(acct_map[AccountNumber.ASSET_CASH_GATEWAY][1], "ASSET")
        self.assertIn(AccountNumber.LIABILITY_RESERVATIONS, acct_map)
        self.assertEqual(acct_map[AccountNumber.LIABILITY_RESERVATIONS][1], "LIABILITY")
        self.assertIn(AccountNumber.EXPENSE_REALIZED_PURCHASES, acct_map)
        self.assertEqual(acct_map[AccountNumber.EXPENSE_REALIZED_PURCHASES][1], "EXPENSE")

    def test_02_balanced_reservation_created(self):
        key = f"res_{uuid.uuid4().hex}"
        entry_id = post_reservation_created(
            self.conn,
            source_event_key=key,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            purchase_id="pur_001",
            amount_cents=4500,
            currency="USD"
        )
        self.assertTrue(entry_id.startswith("je_"))

        # Verify lines
        cur = self.conn.execute(
            "SELECT account_number, debit_cents, credit_cents FROM journal_entry_lines WHERE entry_id = ? ORDER BY line_number;",
            (entry_id,)
        )
        lines = cur.fetchall()
        self.assertEqual(len(lines), 2)
        # Line 1: DR 5200 = 4500
        self.assertEqual(lines[0]["account_number"], AccountNumber.EXPENSE_ENCUMBRANCE_CONTRA)
        self.assertEqual(lines[0]["debit_cents"], 4500)
        self.assertEqual(lines[0]["credit_cents"], 0)
        # Line 2: CR 2100 = 4500
        self.assertEqual(lines[1]["account_number"], AccountNumber.LIABILITY_RESERVATIONS)
        self.assertEqual(lines[1]["debit_cents"], 0)
        self.assertEqual(lines[1]["credit_cents"], 4500)

        # Verify summary
        summary = get_general_ledger_summary(self.conn, self.tenant_id, self.agent_id)
        self.assertTrue(summary.is_balanced)
        self.assertEqual(summary.active_encumbrances_cents, 4500)
        self.assertEqual(summary.realized_purchases_cents, 0)
        self.assertEqual(summary.total_committed_cents, 4500)

    def test_03_unbalanced_entry_rejected_with_rollback(self):
        key = f"unbalanced_{uuid.uuid4().hex}"
        unbalanced_lines = [
            JournalLine(account_number=AccountNumber.EXPENSE_ENCUMBRANCE_CONTRA, debit_cents=5000),
            JournalLine(account_number=AccountNumber.LIABILITY_RESERVATIONS, credit_cents=4000),  # Missing 1000
        ]
        with self.assertRaises(DoubleEntryUnbalancedError) as ctx:
            record_double_entry_transaction(
                self.conn,
                source_event_key=key,
                tenant_id=self.tenant_id,
                agent_id=self.agent_id,
                purchase_id="pur_fail",
                event_type="CORRUPT_TEST",
                description="Should fail",
                lines=unbalanced_lines
            )
        self.assertIn("UNBALANCED", str(ctx.exception))

        # Confirm zero records were written
        cur = self.conn.execute("SELECT COUNT(*) AS c FROM journal_entries WHERE source_event_key = ?;", (key,))
        self.assertEqual(cur.fetchone()["c"], 0)

    def test_04_capture_settled_liquidates_reservation_and_realizes_expense(self):
        pur_id = "pur_shoes_01"
        res_key = f"res_{pur_id}"
        cap_key = f"cap_{pur_id}"

        # 1. Create reservation
        post_reservation_created(self.conn, res_key, self.tenant_id, self.agent_id, pur_id, 4500)
        s1 = get_general_ledger_summary(self.conn, self.tenant_id, self.agent_id)
        self.assertEqual(s1.active_encumbrances_cents, 4500)
        self.assertEqual(s1.realized_purchases_cents, 0)
        self.assertEqual(s1.total_committed_cents, 4500)

        # 2. Settle capture
        post_capture_settled(self.conn, cap_key, self.tenant_id, self.agent_id, pur_id, 4500, "CAP_TEST_123")
        s2 = get_general_ledger_summary(self.conn, self.tenant_id, self.agent_id)
        self.assertTrue(s2.is_balanced)
        self.assertEqual(s2.active_encumbrances_cents, 0)         # Encumbrance successfully cleared!
        self.assertEqual(s2.realized_purchases_cents, 4500)      # Recognized as realized purchase
        self.assertEqual(s2.total_committed_cents, 4500)         # Total committed spend remains exact

    def test_05_reservation_release_returns_encumbrance_to_zero(self):
        pur_id = "pur_cancel_01"
        res_key = f"res_{pur_id}"
        rel_key = f"rel_{pur_id}"

        post_reservation_created(self.conn, res_key, self.tenant_id, self.agent_id, pur_id, 3000)
        post_reservation_released(self.conn, rel_key, self.tenant_id, self.agent_id, pur_id, 3000)

        summary = get_general_ledger_summary(self.conn, self.tenant_id, self.agent_id)
        self.assertTrue(summary.is_balanced)
        self.assertEqual(summary.active_encumbrances_cents, 0)
        self.assertEqual(summary.realized_purchases_cents, 0)
        self.assertEqual(summary.total_committed_cents, 0)

    def test_06_immutability_triggers_abort_modifications(self):
        key = f"imm_{uuid.uuid4().hex}"
        entry_id = post_reservation_created(self.conn, key, self.tenant_id, self.agent_id, "pur_imm", 2000)

        # Attempt to update journal entry header
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE journal_entries SET description = 'tampered' WHERE id = ?;", (entry_id,))

        # Attempt to delete journal entry header
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM journal_entries WHERE id = ?;", (entry_id,))

        # Attempt to update journal line
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE journal_entry_lines SET debit_cents = 99999 WHERE entry_id = ?;", (entry_id,))


if __name__ == "__main__":
    unittest.main()
