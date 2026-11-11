"""
Append-Only Financial Journal and Budget Accounting Projection.
Enforces that every financial movement is an immutable fact, and budget_accounting
is an authoritative rebuildable projection.
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple
from backend.app.db.double_entry import (
    post_reservation_created,
    post_reservation_released,
    post_capture_settled,
    post_late_capture_settled
)


class FactType:
    RESERVATION_CREATED = "RESERVATION_CREATED"
    RESERVATION_RELEASED = "RESERVATION_RELEASED"
    CAPTURE_BECAME_UNKNOWN = "CAPTURE_BECAME_UNKNOWN"
    CAPTURE_SETTLED = "CAPTURE_SETTLED"
    UNRESOLVED_CONFIRMED_NOT_SETTLED = "UNRESOLVED_CONFIRMED_NOT_SETTLED"
    LATE_CAPTURE_SETTLED = "LATE_CAPTURE_SETTLED"


@dataclass(frozen=True)
class BudgetSnapshot:
    agent_id: str
    budget_day: str
    currency: str
    captured_cents: int
    reserved_cents: int
    unresolved_cents: int
    auto_approved_committed_cents: int
    version: int

    @property
    def r3_committed_cents(self) -> int:
        """Total daily financial commitment: captured + active reservations + unresolved in-flight captures."""
        return self.captured_cents + self.reserved_cents + self.unresolved_cents


def get_budget_snapshot(
    conn: sqlite3.Connection,
    agent_id: str,
    budget_day: str,
    currency: str = "USD"
) -> BudgetSnapshot:
    """
    Reads the authoritative budget accounting projection for an agent and day.
    If no row exists, returns an initial zeroed snapshot.
    """
    cursor = conn.execute(
        """
        SELECT captured_cents, reserved_cents, unresolved_cents, 
               auto_approved_committed_cents, version
        FROM budget_accounting
        WHERE agent_id = ? AND budget_day = ? AND currency = ?;
        """,
        (agent_id, budget_day, currency)
    )
    row = cursor.fetchone()
    if row is None:
        return BudgetSnapshot(
            agent_id=agent_id,
            budget_day=budget_day,
            currency=currency,
            captured_cents=0,
            reserved_cents=0,
            unresolved_cents=0,
            auto_approved_committed_cents=0,
            version=0
        )
    return BudgetSnapshot(
        agent_id=agent_id,
        budget_day=budget_day,
        currency=currency,
        captured_cents=row["captured_cents"],
        reserved_cents=row["reserved_cents"],
        unresolved_cents=row["unresolved_cents"],
        auto_approved_committed_cents=row["auto_approved_committed_cents"],
        version=row["version"]
    )


def record_financial_fact(
    conn: sqlite3.Connection,
    source_event_key: str,
    tenant_id: str,
    agent_id: str,
    purchase_id: str,
    budget_day: str,
    fact_type: str,
    delta_reserved_cents: int,
    delta_unresolved_cents: int,
    delta_captured_cents: int,
    delta_auto_approved_cents: int,
    actor: str,
    currency: str = "USD",
    paypal_capture_id: Optional[str] = None
) -> Tuple[str, BudgetSnapshot]:
    """
    Atomically records an immutable financial fact and updates the budget_accounting projection.
    Must be called inside a BEGIN IMMEDIATE transaction.
    Returns (fact_id, updated_snapshot).
    """
    # 1. Idempotency check on source_event_key
    cur = conn.execute(
        "SELECT id FROM financial_facts WHERE source_event_key = ?;",
        (source_event_key,)
    )
    existing = cur.fetchone()
    if existing:
        # Already applied; return current snapshot
        return existing["id"], get_budget_snapshot(conn, agent_id, budget_day, currency)

    fact_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()

    # 2. Insert immutable fact
    conn.execute(
        """
        INSERT INTO financial_facts (
            id, source_event_key, tenant_id, agent_id, purchase_id, budget_day,
            currency, fact_type, delta_reserved_cents, delta_unresolved_cents,
            delta_captured_cents, delta_auto_approved_cents, paypal_capture_id,
            actor, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            fact_id, source_event_key, tenant_id, agent_id, purchase_id, budget_day,
            currency, fact_type, delta_reserved_cents, delta_unresolved_cents,
            delta_captured_cents, delta_auto_approved_cents, paypal_capture_id,
            actor, now_iso
        )
    )

    # 2b. Post balanced double-entry transaction to general ledger
    try:
        if fact_type == FactType.RESERVATION_CREATED and delta_reserved_cents > 0:
            post_reservation_created(
                conn, source_event_key, tenant_id, agent_id, purchase_id, delta_reserved_cents, currency
            )
        elif fact_type == FactType.RESERVATION_RELEASED and delta_reserved_cents < 0:
            post_reservation_released(
                conn, source_event_key, tenant_id, agent_id, purchase_id, abs(delta_reserved_cents), currency
            )
        elif fact_type == FactType.CAPTURE_SETTLED and delta_captured_cents > 0:
            post_capture_settled(
                conn, source_event_key, tenant_id, agent_id, purchase_id, delta_captured_cents,
                paypal_capture_id or f"CAP_{fact_id[:8]}", currency
            )
        elif fact_type == FactType.UNRESOLVED_CONFIRMED_NOT_SETTLED and delta_unresolved_cents < 0:
            post_reservation_released(
                conn, source_event_key, tenant_id, agent_id, purchase_id, abs(delta_unresolved_cents), currency
            )
        elif fact_type == FactType.LATE_CAPTURE_SETTLED and delta_captured_cents > 0:
            post_late_capture_settled(
                conn, source_event_key, tenant_id, agent_id, purchase_id, delta_captured_cents,
                paypal_capture_id or f"CAP_LATE_{fact_id[:8]}", currency
            )
    except sqlite3.OperationalError as op_err:
        # Only allow missing table error in minimal schema environments; re-raise everything else
        if "no such table: journal_entries" in str(op_err) or "no such table: accounts" in str(op_err):
            pass
        else:
            raise
    conn.execute(
        """
        INSERT INTO budget_accounting (
            agent_id, budget_day, currency, captured_cents, reserved_cents,
            unresolved_cents, auto_approved_committed_cents, version, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(agent_id, budget_day, currency) DO UPDATE SET
            captured_cents = captured_cents + excluded.captured_cents,
            reserved_cents = reserved_cents + excluded.reserved_cents,
            unresolved_cents = unresolved_cents + excluded.unresolved_cents,
            auto_approved_committed_cents = auto_approved_committed_cents + excluded.auto_approved_committed_cents,
            version = version + 1,
            updated_at = excluded.updated_at;
        """,
        (
            agent_id, budget_day, currency,
            delta_captured_cents, delta_reserved_cents,
            delta_unresolved_cents, delta_auto_approved_cents, now_iso
        )
    )

    snapshot = get_budget_snapshot(conn, agent_id, budget_day, currency)
    return fact_id, snapshot


def rebuild_budget_accounting(
    conn: sqlite3.Connection,
    agent_id: str,
    budget_day: str,
    currency: str = "USD"
) -> BudgetSnapshot:
    """
    Deterministic rebuild of the budget accounting projection by summing all immutable financial_facts.
    Used for startup verification and integrity audit repair drills.
    """
    cursor = conn.execute(
        """
        SELECT 
            COALESCE(SUM(delta_captured_cents), 0) AS total_captured,
            COALESCE(SUM(delta_reserved_cents), 0) AS total_reserved,
            COALESCE(SUM(delta_unresolved_cents), 0) AS total_unresolved,
            COALESCE(SUM(delta_auto_approved_cents), 0) AS total_auto_approved
        FROM financial_facts
        WHERE agent_id = ? AND budget_day = ? AND currency = ?;
        """,
        (agent_id, budget_day, currency)
    )
    row = cursor.fetchone()
    now_iso = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT INTO budget_accounting (
            agent_id, budget_day, currency, captured_cents, reserved_cents,
            unresolved_cents, auto_approved_committed_cents, version, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(agent_id, budget_day, currency) DO UPDATE SET
            captured_cents = excluded.captured_cents,
            reserved_cents = excluded.reserved_cents,
            unresolved_cents = excluded.unresolved_cents,
            auto_approved_committed_cents = excluded.auto_approved_committed_cents,
            version = version + 1,
            updated_at = excluded.updated_at;
        """,
        (
            agent_id, budget_day, currency,
            row["total_captured"], row["total_reserved"],
            row["total_unresolved"], row["total_auto_approved"], now_iso
        )
    )

    return get_budget_snapshot(conn, agent_id, budget_day, currency)

