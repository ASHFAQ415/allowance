"""
Formal Double-Entry Bookkeeping General Ledger.

Enforces:
1. Every posted journal transaction must satisfy: sum(debits) == sum(credits).
2. Independent balancing per currency.
3. Chart of Accounts enforcing distinct Asset, Liability, Equity, and Expense categories.
4. Purchase reservations treated as commitments/encumbrances, not cash movements.
5. Rebuildable budget projection from the double-entry general ledger.
"""

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


class AccountNumber:
    ASSET_CASH_GATEWAY = 1000           # Cash / Payment Settlement Gateway
    LIABILITY_RESERVATIONS = 2100       # Encumbered Spend Reservations
    EQUITY_BUDGET_ALLOCATION = 3100     # Authorized Spending Allocation
    EXPENSE_REALIZED_PURCHASES = 5100   # Realized Merchant Purchases
    EXPENSE_ENCUMBRANCE_CONTRA = 5200   # Encumbrance Commitments Contra
    EXPENSE_PROCESSING_FEES = 5300      # Payment Processing Fees


CHART_OF_ACCOUNTS_DEFAULTS = [
    (AccountNumber.ASSET_CASH_GATEWAY, "Cash / Payment Settlement Gateway", "ASSET", "Realized settlement gateway liquidity"),
    (AccountNumber.LIABILITY_RESERVATIONS, "Encumbered Spend Reservations", "LIABILITY", "Active purchase reservations held against budget"),
    (AccountNumber.EQUITY_BUDGET_ALLOCATION, "Authorized Spending Allocation", "EQUITY", "Owner authorized spending authority"),
    (AccountNumber.EXPENSE_REALIZED_PURCHASES, "Realized Merchant Purchases", "EXPENSE", "Authoritatively captured purchases"),
    (AccountNumber.EXPENSE_ENCUMBRANCE_CONTRA, "Encumbrance Commitments Contra", "EXPENSE", "Contra expense tracking active encumbrance commitments"),
    (AccountNumber.EXPENSE_PROCESSING_FEES, "Payment Processing Fees", "EXPENSE", "Provider interchange and processing fees"),
]


class DoubleEntryUnbalancedError(ValueError):
    """Raised when an attempted journal entry violates sum(debits) == sum(credits)."""
    pass


@dataclass(frozen=True)
class JournalLine:
    account_number: int
    debit_cents: int = 0
    credit_cents: int = 0

    def __post_init__(self):
        if self.debit_cents < 0 or self.credit_cents < 0:
            raise ValueError(f"Negative amounts not permitted: debit={self.debit_cents}, credit={self.credit_cents}")
        if self.debit_cents == 0 and self.credit_cents == 0:
            raise ValueError("Journal line cannot have both zero debit and zero credit")
        if self.debit_cents > 0 and self.credit_cents > 0:
            raise ValueError("Journal line cannot have both debit and credit; split into separate lines")


def seed_chart_of_accounts(conn: sqlite3.Connection) -> None:
    """Idempotently seeds standard chart of accounts into the database."""
    for acct_num, name, acct_type, desc in CHART_OF_ACCOUNTS_DEFAULTS:
        conn.execute(
            """
            INSERT OR IGNORE INTO chart_of_accounts (account_number, account_name, account_type, description)
            VALUES (?, ?, ?, ?);
            """,
            (acct_num, name, acct_type, desc)
        )


def record_double_entry_transaction(
    conn: sqlite3.Connection,
    source_event_key: str,
    tenant_id: str,
    agent_id: str,
    purchase_id: Optional[str],
    event_type: str,
    description: str,
    lines: List[JournalLine],
    currency: str = "USD"
) -> str:
    """
    Atomically records a balanced double-entry journal transaction.
    Must be called inside an active transaction.
    Returns entry_id.
    """
    # 0. Ensure Chart of Accounts exists
    seed_chart_of_accounts(conn)

    # 1. Idempotency Check
    cur = conn.execute(
        "SELECT id FROM journal_entries WHERE source_event_key = ?;",
        (source_event_key,)
    )
    existing = cur.fetchone()
    if existing:
        return existing["id"] if isinstance(existing, sqlite3.Row) else existing[0]

    # 2. Strict Balance Verification: sum(debits) == sum(credits)
    total_debits = sum(line.debit_cents for line in lines)
    total_credits = sum(line.credit_cents for line in lines)

    if total_debits != total_credits:
        raise DoubleEntryUnbalancedError(
            f"Double-entry transaction '{source_event_key}' is UNBALANCED for currency {currency}: "
            f"Total Debits ({total_debits} cents) != Total Credits ({total_credits} cents). "
            f"Posting aborted to prevent financial corruption."
        )

    if total_debits == 0:
        raise DoubleEntryUnbalancedError(f"Double-entry transaction '{source_event_key}' has zero volume.")

    entry_id = f"je_{uuid.uuid4().hex}"
    now_iso = datetime.now(timezone.utc).isoformat()

    # 3. Insert Journal Entry Header
    conn.execute(
        """
        INSERT INTO journal_entries (
            id, source_event_key, tenant_id, agent_id, purchase_id,
            event_type, description, currency, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (entry_id, source_event_key, tenant_id, agent_id, purchase_id, event_type, description, currency, now_iso)
    )

    # 4. Insert Journal Lines
    for line_idx, line in enumerate(lines, start=1):
        line_id = f"jel_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO journal_entry_lines (
                id, entry_id, line_number, account_number, debit_cents, credit_cents, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (line_id, entry_id, line_idx, line.account_number, line.debit_cents, line.credit_cents, now_iso)
        )

    return entry_id


# =========================================================================
# Deterministic Posting Rules
# =========================================================================

def post_reservation_created(
    conn: sqlite3.Connection,
    source_event_key: str,
    tenant_id: str,
    agent_id: str,
    purchase_id: str,
    amount_cents: int,
    currency: str = "USD"
) -> str:
    """
    Rule 1: Commitment Encumbrance Created.
    DR 5200 Encumbrance Commitments Contra (Encumbrance Expense)
    CR 2100 Encumbered Spend Reservations (Liability/Encumbrance)
    """
    lines = [
        JournalLine(account_number=AccountNumber.EXPENSE_ENCUMBRANCE_CONTRA, debit_cents=amount_cents),
        JournalLine(account_number=AccountNumber.LIABILITY_RESERVATIONS, credit_cents=amount_cents),
    ]
    return record_double_entry_transaction(
        conn,
        source_event_key=source_event_key,
        tenant_id=tenant_id,
        agent_id=agent_id,
        purchase_id=purchase_id,
        event_type="RESERVATION_CREATED",
        description=f"Encumbered commitment reservation of {amount_cents} cents for purchase {purchase_id[:8]}",
        lines=lines,
        currency=currency
    )


def post_reservation_released(
    conn: sqlite3.Connection,
    source_event_key: str,
    tenant_id: str,
    agent_id: str,
    purchase_id: str,
    amount_cents: int,
    currency: str = "USD"
) -> str:
    """
    Rule 2: Commitment Encumbrance Released.
    DR 2100 Encumbered Spend Reservations
    CR 5200 Encumbrance Commitments Contra
    """
    lines = [
        JournalLine(account_number=AccountNumber.LIABILITY_RESERVATIONS, debit_cents=amount_cents),
        JournalLine(account_number=AccountNumber.EXPENSE_ENCUMBRANCE_CONTRA, credit_cents=amount_cents),
    ]
    return record_double_entry_transaction(
        conn,
        source_event_key=source_event_key,
        tenant_id=tenant_id,
        agent_id=agent_id,
        purchase_id=purchase_id,
        event_type="RESERVATION_RELEASED",
        description=f"Released commitment reservation of {amount_cents} cents for purchase {purchase_id[:8]}",
        lines=lines,
        currency=currency
    )


def post_capture_settled(
    conn: sqlite3.Connection,
    source_event_key: str,
    tenant_id: str,
    agent_id: str,
    purchase_id: str,
    amount_cents: int,
    paypal_capture_id: str,
    currency: str = "USD"
) -> str:
    """
    Rule 3: Capture Settled.
    Liquidates the active encumbrance and recognizes the actual realized expenditure.
    DR 2100 Encumbered Spend Reservations (Liquidate reservation)
    DR 5100 Realized Merchant Purchases (Recognize expense)
    CR 5200 Encumbrance Commitments Contra (Clear encumbrance contra)
    CR 1000 Cash / Payment Settlement Gateway (Settlement outflow)
    """
    lines = [
        # Encumbrance liquidation
        JournalLine(account_number=AccountNumber.LIABILITY_RESERVATIONS, debit_cents=amount_cents),
        JournalLine(account_number=AccountNumber.EXPENSE_ENCUMBRANCE_CONTRA, credit_cents=amount_cents),
        # Expense recognition & cash settlement
        JournalLine(account_number=AccountNumber.EXPENSE_REALIZED_PURCHASES, debit_cents=amount_cents),
        JournalLine(account_number=AccountNumber.ASSET_CASH_GATEWAY, credit_cents=amount_cents),
    ]
    return record_double_entry_transaction(
        conn,
        source_event_key=source_event_key,
        tenant_id=tenant_id,
        agent_id=agent_id,
        purchase_id=purchase_id,
        event_type="CAPTURE_SETTLED",
        description=f"Settled purchase {purchase_id[:8]} via PayPal capture {paypal_capture_id} ({amount_cents} cents)",
        lines=lines,
        currency=currency
    )


def post_late_capture_settled(
    conn: sqlite3.Connection,
    source_event_key: str,
    tenant_id: str,
    agent_id: str,
    purchase_id: str,
    amount_cents: int,
    paypal_capture_id: str,
    currency: str = "USD"
) -> str:
    """
    Rule 4: Late-Discovered Settlement (Reservation was already released/expired).
    Directly recognizes realized purchase expenditure.
    DR 5100 Realized Merchant Purchases
    CR 1000 Cash / Payment Settlement Gateway
    """
    lines = [
        JournalLine(account_number=AccountNumber.EXPENSE_REALIZED_PURCHASES, debit_cents=amount_cents),
        JournalLine(account_number=AccountNumber.ASSET_CASH_GATEWAY, credit_cents=amount_cents),
    ]
    return record_double_entry_transaction(
        conn,
        source_event_key=source_event_key,
        tenant_id=tenant_id,
        agent_id=agent_id,
        purchase_id=purchase_id,
        event_type="LATE_CAPTURE_SETTLED",
        description=f"Late capture discovered: {paypal_capture_id} for purchase {purchase_id[:8]} ({amount_cents} cents)",
        lines=lines,
        currency=currency
    )


def post_refund_settled(
    conn: sqlite3.Connection,
    source_event_key: str,
    tenant_id: str,
    agent_id: str,
    purchase_id: str,
    amount_cents: int,
    currency: str = "USD"
) -> str:
    """
    Rule 5: Refund / Payment Reversal.
    Restores cash and reduces realized expense.
    DR 1000 Cash / Payment Settlement Gateway
    CR 5100 Realized Merchant Purchases
    """
    lines = [
        JournalLine(account_number=AccountNumber.ASSET_CASH_GATEWAY, debit_cents=amount_cents),
        JournalLine(account_number=AccountNumber.EXPENSE_REALIZED_PURCHASES, credit_cents=amount_cents),
    ]
    return record_double_entry_transaction(
        conn,
        source_event_key=source_event_key,
        tenant_id=tenant_id,
        agent_id=agent_id,
        purchase_id=purchase_id,
        event_type="REFUND_SETTLED",
        description=f"Refund credited for purchase {purchase_id[:8]} ({amount_cents} cents)",
        lines=lines,
        currency=currency
    )


# =========================================================================
# Rebuildable Projection from Double-Entry General Ledger
# =========================================================================

@dataclass(frozen=True)
class DoubleEntryBudgetSummary:
    realized_purchases_cents: int
    active_encumbrances_cents: int
    total_committed_cents: int
    is_balanced: bool
    total_journal_entries: int


def get_general_ledger_summary(
    conn: sqlite3.Connection,
    tenant_id: str,
    agent_id: str,
    currency: str = "USD"
) -> DoubleEntryBudgetSummary:
    """
    Rebuilds authoritative financial state directly from balanced general ledger lines.
    """
    # 1. Verify global ledger balance: sum(debits) must equal sum(credits)
    cur = conn.execute(
        """
        SELECT 
            COALESCE(SUM(l.debit_cents), 0) AS total_debits,
            COALESCE(SUM(l.credit_cents), 0) AS total_credits,
            COUNT(DISTINCT e.id) AS entry_count
        FROM journal_entries e
        JOIN journal_entry_lines l ON e.id = l.entry_id
        WHERE e.tenant_id = ? AND e.agent_id = ? AND e.currency = ?;
        """,
        (tenant_id, agent_id, currency)
    )
    row = cur.fetchone()
    total_debits = row["total_debits"]
    total_credits = row["total_credits"]
    entry_count = row["entry_count"]
    is_balanced = (total_debits == total_credits)

    # 2. Calculate Realized Purchases (Account 5100 net debit balance)
    cur = conn.execute(
        """
        SELECT 
            COALESCE(SUM(l.debit_cents), 0) - COALESCE(SUM(l.credit_cents), 0) AS net_realized
        FROM journal_entries e
        JOIN journal_entry_lines l ON e.id = l.entry_id
        WHERE e.tenant_id = ? AND e.agent_id = ? AND e.currency = ?
          AND l.account_number = ?;
        """,
        (tenant_id, agent_id, currency, AccountNumber.EXPENSE_REALIZED_PURCHASES)
    )
    realized_cents = cur.fetchone()["net_realized"]

    # 3. Calculate Active Encumbrances (Account 5200 net debit balance, which equals active commitments)
    cur = conn.execute(
        """
        SELECT 
            COALESCE(SUM(l.debit_cents), 0) - COALESCE(SUM(l.credit_cents), 0) AS net_encumbrances
        FROM journal_entries e
        JOIN journal_entry_lines l ON e.id = l.entry_id
        WHERE e.tenant_id = ? AND e.agent_id = ? AND e.currency = ?
          AND l.account_number = ?;
        """,
        (tenant_id, agent_id, currency, AccountNumber.EXPENSE_ENCUMBRANCE_CONTRA)
    )
    encumbrances_cents = cur.fetchone()["net_encumbrances"]

    total_committed = realized_cents + encumbrances_cents

    return DoubleEntryBudgetSummary(
        realized_purchases_cents=realized_cents,
        active_encumbrances_cents=encumbrances_cents,
        total_committed_cents=total_committed,
        is_balanced=is_balanced,
        total_journal_entries=entry_count
    )
