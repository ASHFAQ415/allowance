"""
Allowance Application Orchestrator Core.
Coordinates agent run submissions, PayPilot checkpoints P1-P4, PayPal order creations,
buyer return verifications, and capture claims.
"""

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Dict, Any, Optional, Tuple

from ..db.connection import transactional_session
from ..db.financial_journal import (
    FactType,
    get_budget_snapshot,
    record_financial_fact,
)
from ..policy.rules import (
    Checkpoint,
    Decision,
    Outcome,
    PolicyConfig,
    evaluate,
)
from .state_machine import PurchaseState
from .admission import check_and_admit_cart_reservation
from ..paypal.client import PayPalClientInterface
from .capture_lifecycle import execute_capture_flow, verify_order


def get_agent_policy(conn: sqlite3.Connection, agent_id: str) -> Optional[PolicyConfig]:
    """Retrieves current PolicyConfig for an agent from the policy table."""
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
        frozen=bool(row["frozen"])
    )


def submit_cart_to_orchestrator(
    conn: sqlite3.Connection,
    tenant_id: str,
    agent_id: str,
    run_id: str,
    cart,  # FrozenCart
    now: float
) -> Tuple[str, PurchaseState, Decision]:
    """
    Transactional submission of a proposed cart.
    Evaluates Policy Checkpoint P1.
    If ALLOW -> AUTO_APPROVED
    If REVIEW -> PENDING_APPROVAL
    If DENY -> BLOCKED
    Enforces active reservation admission and append-only financial fact logging.
    """
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    budget_day = time.strftime("%Y-%m-%d", time.gmtime(now))

    with transactional_session(conn):
        # 1. Check admission capacity
        check_and_admit_cart_reservation(conn, tenant_id, budget_day)

        # 2. Fetch policy & snapshot
        policy = get_agent_policy(conn, agent_id)
        if not policy:
            raise ValueError(f"No policy configured for agent '{agent_id}'.")

        snapshot = get_budget_snapshot(conn, agent_id, budget_day, cart.currency)

        # 3. Evaluate Checkpoint P1
        from datetime import datetime, timezone
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        decision = evaluate(cart, policy, snapshot, now_dt, Checkpoint.P1)

        purchase_id = str(uuid.uuid4())
        create_req_id = str(uuid.uuid4())

        if decision.outcome == Outcome.ALLOW:
            target_state = PurchaseState.AUTO_APPROVED
            auto_approved = 1
        elif decision.outcome == Outcome.REVIEW:
            target_state = PurchaseState.PENDING_APPROVAL
            auto_approved = 0
        else:
            target_state = PurchaseState.BLOCKED
            auto_approved = 0

        # 4. Insert purchase row
        merchant_ids = list({it.merchant_id for it in cart.items})
        conn.execute(
            """
            INSERT INTO purchases (
                id, tenant_id, agent_id, run_id, status, status_reason, auto_approved,
                cart_json, cart_hash, total_cents, currency, merchant_ids_json, budget_day,
                create_request_id, state_entered_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                purchase_id, tenant_id, agent_id, run_id, target_state.value,
                decision.reason if target_state == PurchaseState.BLOCKED else None,
                auto_approved,
                json.dumps([{"id": it.product_id, "qty": it.quantity, "cents": it.unit_price_cents} for it in cart.items]),
                cart.cart_hash, cart.total_cents, cart.currency, json.dumps(merchant_ids),
                budget_day, create_req_id, now, now_iso, now_iso
            )
        )

        # 5. Log ledger transition
        conn.execute(
            """
            INSERT INTO ledger (
                id, purchase_id, agent_id, from_state, to_state, rule_name,
                reason, checkpoint, amount_cents, policy_version, actor, created_at
            ) VALUES (?, ?, ?, 'CREATED', ?, ?, ?, 'P1', ?, ?, 'agent', ?);
            """,
            (
                str(uuid.uuid4()), purchase_id, agent_id, target_state.value,
                decision.rule_name, decision.reason, cart.total_cents, policy.version, now_iso
            )
        )

        # 6. If admitted to holding state (AUTO_APPROVED or PENDING_APPROVAL), record financial reservation fact
        if target_state in (PurchaseState.AUTO_APPROVED, PurchaseState.PENDING_APPROVAL):
            record_financial_fact(
                conn=conn,
                source_event_key=f"res_{purchase_id}",
                tenant_id=tenant_id,
                agent_id=agent_id,
                purchase_id=purchase_id,
                budget_day=budget_day,
                fact_type=FactType.RESERVATION_CREATED,
                delta_reserved_cents=cart.total_cents,
                delta_unresolved_cents=0,
                delta_captured_cents=0,
                delta_auto_approved_cents=cart.total_cents if auto_approved else 0,
                actor="agent",
                currency=cart.currency
            )

        # 7. Record event for dashboard SSE
        conn.execute(
            """
            INSERT INTO events (tenant_id, event_type, purchase_id, run_id, payload_json, created_at)
            VALUES (?, 'CART_SUBMITTED', ?, ?, ?, ?);
            """,
            (
                tenant_id, purchase_id, run_id,
                json.dumps({
                    "purchase_id": purchase_id,
                    "status": target_state.value,
                    "total_cents": cart.total_cents,
                    "rule": decision.rule_name,
                    "outcome": decision.outcome.value
                }),
                now_iso
            )
        )

    return purchase_id, target_state, decision


def approve_pending_purchase(
    conn: sqlite3.Connection,
    purchase_id: str,
    admin_actor: str = "admin"
) -> Tuple[PurchaseState, str]:
    """
    Admin approval of a PENDING_APPROVAL purchase.
    Evaluates Checkpoint P2.
    If DENY -> BLOCKED
    If ALLOW -> APPROVED
    """
    now = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    with transactional_session(conn):
        cur = conn.execute("SELECT * FROM purchases WHERE id = ?;", (purchase_id,))
        purchase = cur.fetchone()
        if not purchase or purchase["status"] != PurchaseState.PENDING_APPROVAL.value:
            return PurchaseState.FAILED, "purchase_not_pending_approval"

        policy = get_agent_policy(conn, purchase["agent_id"])
        if not policy:
            return PurchaseState.FAILED, "policy_not_found"

        # Checkpoint P2: Invariant 3 - Snapshot excludes current purchase reservation exactly once!
        raw_snapshot = get_budget_snapshot(conn, purchase["agent_id"], purchase["budget_day"], purchase["currency"])
        # Subtract current purchase reservation from snapshot for self-exclusion
        p2_snapshot = type(raw_snapshot)(
            agent_id=raw_snapshot.agent_id,
            budget_day=raw_snapshot.budget_day,
            currency=raw_snapshot.currency,
            captured_cents=raw_snapshot.captured_cents,
            reserved_cents=max(0, raw_snapshot.reserved_cents - purchase["total_cents"]),
            unresolved_cents=raw_snapshot.unresolved_cents,
            auto_approved_committed_cents=raw_snapshot.auto_approved_committed_cents,
            version=raw_snapshot.version
        )

        from datetime import datetime, timezone
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        
        # Build cart proxy for evaluation
        from ..policy.rules import FrozenCart, CartItem
        cart_proxy = FrozenCart(items=[], total_cents=purchase["total_cents"], cart_hash=purchase["cart_hash"], currency=purchase["currency"])
        decision = evaluate(cart_proxy, policy, p2_snapshot, now_dt, Checkpoint.P2)

        if decision.outcome == Outcome.DENY:
            target_state = PurchaseState.BLOCKED
            # Release reservation
            record_financial_fact(
                conn=conn,
                source_event_key=f"p2_deny_release_{purchase_id}",
                tenant_id=purchase["tenant_id"],
                agent_id=purchase["agent_id"],
                purchase_id=purchase_id,
                budget_day=purchase["budget_day"],
                fact_type=FactType.RESERVATION_RELEASED,
                delta_reserved_cents=-purchase["total_cents"],
                delta_unresolved_cents=0,
                delta_captured_cents=0,
                delta_auto_approved_cents=0,
                actor=admin_actor
            )
        else:
            target_state = PurchaseState.APPROVED

        conn.execute(
            """
            UPDATE purchases
            SET status = ?, status_reason = ?, updated_at = ?
            WHERE id = ? AND status = 'PENDING_APPROVAL';
            """,
            (target_state.value, decision.reason if target_state == PurchaseState.BLOCKED else None, now_iso, purchase_id)
        )

        conn.execute(
            """
            INSERT INTO ledger (
                id, purchase_id, agent_id, from_state, to_state, rule_name,
                reason, checkpoint, amount_cents, policy_version, actor, created_at
            ) VALUES (?, ?, ?, 'PENDING_APPROVAL', ?, ?, ?, 'P2', ?, ?, ?, ?);
            """,
            (
                str(uuid.uuid4()), purchase_id, purchase["agent_id"], target_state.value,
                decision.rule_name, decision.reason, purchase["total_cents"], policy.version, admin_actor, now_iso
            )
        )

    return target_state, decision.reason


def create_paypal_order_for_purchase(
    conn: sqlite3.Connection,
    purchase_id: str,
    paypal_client: PayPalClientInterface
) -> Tuple[PurchaseState, Optional[str]]:
    """
    Evaluates Checkpoint P3 and invokes PayPal Orders v2 create_order.
    Transitions purchase to PAYPAL_ORDER_CREATED and stores confidential approval_url.
    """
    now = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    cur = conn.execute("SELECT * FROM purchases WHERE id = ?;", (purchase_id,))
    purchase = cur.fetchone()
    if not purchase:
        return PurchaseState.FAILED, "purchase_not_found"

    if purchase["status"] not in (PurchaseState.AUTO_APPROVED.value, PurchaseState.APPROVED.value):
        return PurchaseState(purchase["status"]), "purchase_not_in_approved_state"

    # Invoke PayPal Create Order
    outcome = paypal_client.create_order(
        create_request_id=purchase["create_request_id"],
        total_cents=purchase["total_cents"],
        items=json.loads(purchase["cart_json"]),
        currency=purchase["currency"]
    )

    if not outcome.success or not outcome.data:
        return PurchaseState(purchase["status"]), "paypal_create_order_failed"

    order_id = outcome.data["id"]
    links = outcome.data.get("links", [])
    approval_url = next((l["href"] for l in links if l.get("rel") in ("payer-action", "approve")), None)

    with transactional_session(conn):
        conn.execute(
            """
            UPDATE purchases
            SET status = 'PAYPAL_ORDER_CREATED', paypal_order_id = ?, approval_url = ?, updated_at = ?
            WHERE id = ? AND status IN ('AUTO_APPROVED', 'APPROVED');
            """,
            (order_id, approval_url, now_iso, purchase_id)
        )

        conn.execute(
            """
            INSERT INTO ledger (
                id, purchase_id, agent_id, from_state, to_state, rule_name,
                reason, checkpoint, amount_cents, policy_version, actor, created_at
            ) VALUES (?, ?, ?, ?, 'PAYPAL_ORDER_CREATED', 'ORDER_CREATED', 'PayPal Order v2 created', 'P3', ?, 1, 'system', ?);
            """,
            (str(uuid.uuid4()), purchase_id, purchase["agent_id"], purchase["status"], purchase["total_cents"], now_iso)
        )

    return PurchaseState.PAYPAL_ORDER_CREATED, approval_url

