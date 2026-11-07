"""
Policy re-evaluation at checkpoints P2, P3 and P4.

P1 is not durable authority: the budget, allowlist or freeze switch can change
before money moves. Every later checkpoint rebuilds a minimal cart proxy from
the frozen purchase row and re-runs the same pure evaluator.

The purchase's own reservation is excluded from the snapshot at every
checkpoint (Invariant 3), so an active purchase never denies itself.
"""

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from ..db.connection import transactional_session
from ..db.financial_journal import FactType, get_budget_snapshot, record_financial_fact
from ..policy.rules import (
    CartItem,
    Checkpoint,
    Decision,
    FrozenCart,
    PolicyConfig,
    evaluate,
)
from .state_machine import PurchaseState


def get_agent_policy(conn: sqlite3.Connection, agent_id: str) -> Optional[PolicyConfig]:
    """Retrieves the current PolicyConfig for an agent from the policy table."""
    cur = conn.execute("SELECT * FROM policy WHERE agent_id = ?;", (agent_id,))
    row = cur.fetchone()
    if not row:
        return None
    return PolicyConfig(
        agent_id=row["agent_id"],
        tenant_id=row["tenant_id"],
        version=row["version"],
        daily_budget_cents=row["daily_budget_cents"],
        max_per_tx_cents=row["max_per_tx_cents"],
        approval_threshold_cents=row["approval_threshold_cents"],
        velocity_cap_cents=row["velocity_cap_cents"],
        allowlist=set(json.loads(row["allowlist_json"])),
        timezone=row["timezone"],
        frozen=bool(row["frozen"]),
    )


def cart_proxy_for_purchase(purchase: sqlite3.Row) -> FrozenCart:
    """
    Rebuilds just enough of the frozen cart for merchant-allowlist and total
    checks. Per-unit prices remain authoritative in the stored total.
    """
    merchant_ids = json.loads(purchase["merchant_ids_json"])
    items = [
        CartItem(
            product_id="",
            merchant_id=merchant_id,
            name="",
            unit_price_cents=0,
            quantity=1,
        )
        for merchant_id in merchant_ids
    ]
    return FrozenCart(
        items=items,
        total_cents=purchase["total_cents"],
        cart_hash=purchase["cart_hash"],
        currency=purchase["currency"],
    )


def self_excluded_snapshot(conn: sqlite3.Connection, purchase: sqlite3.Row):
    """Budget snapshot with this purchase's own reservation removed."""
    raw = get_budget_snapshot(
        conn,
        purchase["agent_id"],
        purchase["budget_day"],
        purchase["currency"],
    )
    return type(raw)(
        agent_id=raw.agent_id,
        budget_day=raw.budget_day,
        currency=raw.currency,
        captured_cents=raw.captured_cents,
        reserved_cents=max(0, raw.reserved_cents - purchase["total_cents"]),
        unresolved_cents=raw.unresolved_cents,
        auto_approved_committed_cents=raw.auto_approved_committed_cents,
        version=raw.version,
    )


def reevaluate_purchase(
    conn: sqlite3.Connection,
    purchase: sqlite3.Row,
    checkpoint: Checkpoint,
    now: Optional[float] = None,
) -> Tuple[Optional[Decision], Optional[PolicyConfig]]:
    """
    Re-runs the policy at a post-P1 checkpoint.
    Returns (decision, policy); both are None when no policy is configured.
    """
    policy = get_agent_policy(conn, purchase["agent_id"])
    if policy is None:
        return None, None

    now_dt = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    decision = evaluate(
        cart_proxy_for_purchase(purchase),
        policy,
        self_excluded_snapshot(conn, purchase),
        now_dt,
        checkpoint,
    )
    return decision, policy


def release_purchase_reservation(
    conn: sqlite3.Connection,
    purchase: sqlite3.Row,
    actor: str,
    source_suffix: str,
) -> None:
    """Releases a still-active reservation exactly once for the given source."""
    record_financial_fact(
        conn=conn,
        source_event_key=f"{source_suffix}_{purchase['id']}",
        tenant_id=purchase["tenant_id"],
        agent_id=purchase["agent_id"],
        purchase_id=purchase["id"],
        budget_day=purchase["budget_day"],
        fact_type=FactType.RESERVATION_RELEASED,
        delta_reserved_cents=-purchase["total_cents"],
        delta_unresolved_cents=0,
        delta_captured_cents=0,
        delta_auto_approved_cents=-purchase["total_cents"] if purchase["auto_approved"] else 0,
        actor=actor,
    )


def expire_purchase_if_due(
    conn: sqlite3.Connection,
    purchase_id: str,
    now: Optional[float] = None,
) -> Tuple[PurchaseState, str]:
    """
    Applies state-machine time rules to a single purchase on read.
    Expiry releases the reservation and never calls PayPal.
    """
    now = time.time() if now is None else now
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    purchase = conn.execute("SELECT * FROM purchases WHERE id = ?;", (purchase_id,)).fetchone()
    if not purchase:
        return PurchaseState.FAILED, "purchase_not_found"

    status = purchase["status"]
    if status not in (
        PurchaseState.PENDING_APPROVAL.value,
        PurchaseState.PAYPAL_ORDER_CREATED.value,
        PurchaseState.BUYER_APPROVED.value,
    ):
        return PurchaseState(status), "not_expirable"

    expires_at = purchase["expires_at"]
    if expires_at is None or now < expires_at:
        return PurchaseState(status), "not_due"

    reason = {
        PurchaseState.PENDING_APPROVAL.value: "pending_approval_timeout",
        PurchaseState.PAYPAL_ORDER_CREATED.value: "order_approval_timeout",
        PurchaseState.BUYER_APPROVED.value: "buyer_approval_timeout",
    }[status]

    with transactional_session(conn):
        updated = conn.execute(
            """
            UPDATE purchases
            SET status = 'EXPIRED', status_reason = ?, state_entered_at = ?, updated_at = ?
            WHERE id = ? AND status = ?;
            """,
            (reason, now, now_iso, purchase_id, status),
        )
        if updated.rowcount != 1:
            current = conn.execute("SELECT status FROM purchases WHERE id = ?;", (purchase_id,)).fetchone()
            return PurchaseState(current["status"]), "state_changed_before_expiry"

        release_purchase_reservation(conn, purchase, actor="system", source_suffix="expiry_release")
        conn.execute(
            """
            INSERT INTO ledger (
                id, purchase_id, agent_id, from_state, to_state, rule_name,
                reason, checkpoint, amount_cents, policy_version, actor, created_at
            ) VALUES (?, ?, ?, ?, 'EXPIRED', 'TIME_EXPIRY', ?, 'TIME', ?, 1, 'system', ?);
            """,
            (
                str(uuid.uuid4()),
                purchase_id,
                purchase["agent_id"],
                status,
                reason,
                purchase["total_cents"],
                now_iso,
            ),
        )

    return PurchaseState.EXPIRED, reason
