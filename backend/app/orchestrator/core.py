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
from .state_machine import (
    EXPIRY_BUYER_APPROVED_SECONDS,
    EXPIRY_ORDER_CREATED_SECONDS,
    PurchaseState,
)
from .admission import check_and_admit_cart_reservation
from ..paypal.client import PayPalClientInterface
from .capture_lifecycle import execute_capture_flow, verify_order, VerificationError
from .rechecks import (
    get_agent_policy,
    reevaluate_purchase,
    release_purchase_reservation,
)


def _now_pair(now: float = None):
    now = time.time() if now is None else now
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    return now, now_iso


def _block_purchase(
    conn: sqlite3.Connection,
    purchase: sqlite3.Row,
    decision: Decision,
    checkpoint: str,
    actor: str,
) -> None:
    """BLOCKED transition plus reservation release and ledger entry."""
    now, now_iso = _now_pair()
    conn.execute(
        """
        UPDATE purchases
        SET status = 'BLOCKED', status_reason = ?, state_entered_at = ?, expires_at = NULL, updated_at = ?
        WHERE id = ? AND status = ?;
        """,
        (decision.reason, now, now_iso, purchase["id"], purchase["status"]),
    )
    release_purchase_reservation(conn, purchase, actor=actor, source_suffix=f"{checkpoint}_deny_release")
    conn.execute(
        """
        INSERT INTO ledger (
            id, purchase_id, agent_id, from_state, to_state, rule_name,
            reason, checkpoint, amount_cents, policy_version, actor, created_at
        ) VALUES (?, ?, ?, ?, 'BLOCKED', ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            str(uuid.uuid4()),
            purchase["id"],
            purchase["agent_id"],
            purchase["status"],
            decision.rule_name,
            decision.reason,
            checkpoint,
            purchase["total_cents"],
            decision.policy_version,
            actor,
            now_iso,
        ),
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
    now, now_iso = _now_pair()

    with transactional_session(conn):
        cur = conn.execute("SELECT * FROM purchases WHERE id = ?;", (purchase_id,))
        purchase = cur.fetchone()
        if not purchase or purchase["status"] != PurchaseState.PENDING_APPROVAL.value:
            return PurchaseState.FAILED, "purchase_not_pending_approval"

        decision, policy = reevaluate_purchase(conn, purchase, Checkpoint.P2, now)
        if decision is None or policy is None:
            return PurchaseState.FAILED, "policy_not_found"

        if decision.outcome == Outcome.DENY:
            target_state = PurchaseState.BLOCKED
            _block_purchase(conn, purchase, decision, "P2", admin_actor)
            return target_state, decision.reason

        target_state = PurchaseState.APPROVED
        conn.execute(
            """
            UPDATE purchases
            SET status = ?, status_reason = NULL, state_entered_at = ?, expires_at = NULL, updated_at = ?
            WHERE id = ? AND status = 'PENDING_APPROVAL';
            """,
            (target_state.value, now, now_iso, purchase_id)
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


def reject_pending_purchase(
    conn: sqlite3.Connection,
    purchase_id: str,
    admin_actor: str = "admin",
    reason: str = "Rejected by admin"
) -> Tuple[PurchaseState, str]:
    """Admin rejection of a PENDING_APPROVAL purchase. PayPal is never called."""
    now, now_iso = _now_pair()

    with transactional_session(conn):
        purchase = conn.execute("SELECT * FROM purchases WHERE id = ?;", (purchase_id,)).fetchone()
        if not purchase or purchase["status"] != PurchaseState.PENDING_APPROVAL.value:
            return PurchaseState.FAILED, "purchase_not_pending_approval"

        updated = conn.execute(
            """
            UPDATE purchases
            SET status = 'REJECTED', status_reason = ?,
                state_entered_at = ?, expires_at = NULL, updated_at = ?
            WHERE id = ? AND status = 'PENDING_APPROVAL';
            """,
            (reason, now, now_iso, purchase_id)
        )
        if updated.rowcount != 1:
            return PurchaseState.FAILED, "purchase_not_pending_approval"

        release_purchase_reservation(conn, purchase, actor=admin_actor, source_suffix="reject_release")
        conn.execute(
            """
            INSERT INTO ledger (
                id, purchase_id, agent_id, from_state, to_state, rule_name,
                reason, checkpoint, amount_cents, policy_version, actor, created_at
            ) VALUES (?, ?, ?, 'PENDING_APPROVAL', 'REJECTED', 'ADMIN_REJECTED', ?, 'P2', ?, 1, ?, ?);
            """,
            (str(uuid.uuid4()), purchase_id, purchase["agent_id"], reason, purchase["total_cents"], admin_actor, now_iso)
        )

    return PurchaseState.REJECTED, reason



def create_paypal_order_for_purchase(
    conn: sqlite3.Connection,
    purchase_id: str,
    paypal_client: PayPalClientInterface
) -> Tuple[PurchaseState, Optional[str]]:
    """
    Evaluates Checkpoint P3, then invokes PayPal Orders v2 create_order.
    A P3 DENY transitions to BLOCKED and never calls PayPal.
    """
    now, now_iso = _now_pair()

    purchase = conn.execute("SELECT * FROM purchases WHERE id = ?;", (purchase_id,)).fetchone()
    if not purchase:
        return PurchaseState.FAILED, "purchase_not_found"

    if purchase["status"] not in (PurchaseState.AUTO_APPROVED.value, PurchaseState.APPROVED.value):
        # Idempotent: an already-created order returns its current state.
        return PurchaseState(purchase["status"]), "purchase_not_in_approved_state"

    # P3: re-authorize against the current policy/budget before touching PayPal.
    with transactional_session(conn):
        decision, policy = reevaluate_purchase(conn, purchase, Checkpoint.P3, now)
        if decision is None:
            return PurchaseState(purchase["status"]), "policy_not_found"
        if decision.outcome == Outcome.DENY:
            _block_purchase(conn, purchase, decision, "P3", "system")
            return PurchaseState.BLOCKED, decision.reason

    # PayPal create is idempotent on the stable create_request_id stored at P1.
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
        updated = conn.execute(
            """
            UPDATE purchases
            SET status = 'PAYPAL_ORDER_CREATED', paypal_order_id = ?, approval_url = ?,
                state_entered_at = ?, expires_at = ?, updated_at = ?
            WHERE id = ? AND status IN ('AUTO_APPROVED', 'APPROVED');
            """,
            (
                order_id, approval_url, now, now + EXPIRY_ORDER_CREATED_SECONDS, now_iso, purchase_id
            )
        )

        if updated.rowcount != 1:
            current = conn.execute("SELECT status FROM purchases WHERE id = ?;", (purchase_id,)).fetchone()
            return PurchaseState(current["status"]), "state_changed_before_order_recorded"

        conn.execute(
            """
            INSERT INTO ledger (
                id, purchase_id, agent_id, from_state, to_state, rule_name,
                reason, checkpoint, amount_cents, policy_version, actor, created_at
            ) VALUES (?, ?, ?, ?, 'PAYPAL_ORDER_CREATED', 'ORDER_CREATED', 'PayPal Order v2 created', 'P3', ?, ?, 'system', ?);
            """,
            (
                str(uuid.uuid4()), purchase_id, purchase["agent_id"], purchase["status"],
                purchase["total_cents"], policy.version, now_iso
            )
        )

    return PurchaseState.PAYPAL_ORDER_CREATED, approval_url


def expire_purchase_if_due(
    conn: sqlite3.Connection,
    purchase_id: str,
    now: Optional[float] = None
) -> Tuple[PurchaseState, str]:
    """
    Applies state-machine time rules to a single purchase on read.
    Expiry releases the reservation and never calls PayPal.
    """
    now, now_iso = _now_pair(now)
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
            (reason, now, now_iso, purchase_id, status)
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
            (str(uuid.uuid4()), purchase_id, purchase["agent_id"], status, reason, purchase["total_cents"], now_iso)
        )

    return PurchaseState.EXPIRED, reason


def get_boot_id(conn: sqlite3.Connection) -> str:
    """Returns the current process boot id, or a stable placeholder."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'boot_id';").fetchone()
    return row["value"] if row else "boot_unknown"


def advance_buyer_return(
    conn: sqlite3.Connection,
    purchase_id: str,
    paypal_client: PayPalClientInterface,
    boot_id: str
) -> Tuple[str, str]:
    """
    Server-verified buyer return: GET the order, only accept APPROVED, then
    transition to BUYER_APPROVED and capture. Idempotent across repeat visits.
    """
    now, now_iso = _now_pair()
    purchase = conn.execute("SELECT * FROM purchases WHERE id = ?;", (purchase_id,)).fetchone()
    if not purchase:
        return "FAILED", "purchase_not_found"

    status = purchase["status"]
    if status == PurchaseState.CAPTURED.value:
        return "CAPTURED", "already_captured"
    if status in (
        PurchaseState.BLOCKED.value,
        PurchaseState.REJECTED.value,
        PurchaseState.EXPIRED.value,
        PurchaseState.FAILED.value,
    ):
        return status, "terminal_state"
    if status not in (PurchaseState.PAYPAL_ORDER_CREATED.value, PurchaseState.BUYER_APPROVED.value):
        return status, "not_awaiting_buyer_approval"

    expired_state, expire_reason = expire_purchase_if_due(conn, purchase_id, now)
    if expired_state == PurchaseState.EXPIRED:
        return "EXPIRED", expire_reason

    # A repeat return after a crash may already be BUYER_APPROVED: capture directly.
    if status == PurchaseState.BUYER_APPROVED.value:
        return execute_capture_flow(conn, purchase_id, paypal_client, boot_id)

    order_outcome = paypal_client.get_order(purchase["paypal_order_id"])
    if not order_outcome.success or not order_outcome.data:
        return status, "get_order_failed_before_buyer_approval"

    order_data = order_outcome.data
    if order_data.get("status") != "APPROVED":
        # Never trust the return query string: a non-approved order changes nothing.
        return status, f"order_not_approved:{order_data.get('status')}"

    try:
        verify_order(order_data, purchase)
    except VerificationError as err:
        with transactional_session(conn):
            updated = conn.execute(
                """
                UPDATE purchases
                SET status = 'FAILED', status_reason = 'amount_mismatch',
                    state_entered_at = ?, updated_at = ?
                WHERE id = ? AND status = 'PAYPAL_ORDER_CREATED';
                """,
                (now, now_iso, purchase_id)
            )
            if updated.rowcount == 1:
                release_purchase_reservation(conn, purchase, actor="system", source_suffix="mismatch_release")
                conn.execute(
                    """
                    INSERT INTO ledger (
                        id, purchase_id, agent_id, from_state, to_state, rule_name,
                        reason, checkpoint, amount_cents, policy_version, actor, created_at
                    ) VALUES (?, ?, ?, 'PAYPAL_ORDER_CREATED', 'FAILED', 'ORDER_VERIFICATION_FAILED', ?, 'P4', ?, 1, 'system', ?);
                    """,
                    (str(uuid.uuid4()), purchase_id, purchase["agent_id"], str(err), purchase["total_cents"], now_iso)
                )
        return "FAILED", f"amount_mismatch:{err}"

    with transactional_session(conn):
        updated = conn.execute(
            """
            UPDATE purchases
            SET status = 'BUYER_APPROVED', state_entered_at = ?, expires_at = ?, updated_at = ?
            WHERE id = ? AND status = 'PAYPAL_ORDER_CREATED';
            """,
            (now, now + EXPIRY_BUYER_APPROVED_SECONDS, now_iso, purchase_id)
        )
        if updated.rowcount != 1:
            current = conn.execute("SELECT status FROM purchases WHERE id = ?;", (purchase_id,)).fetchone()
            return current["status"], "state_changed_before_buyer_approval"

        conn.execute(
            """
            INSERT INTO ledger (
                id, purchase_id, agent_id, from_state, to_state, rule_name,
                reason, checkpoint, amount_cents, policy_version, actor, created_at
            ) VALUES (?, ?, ?, 'PAYPAL_ORDER_CREATED', 'BUYER_APPROVED', 'BUYER_RETURN_VERIFIED', 'PayPal reports buyer approved', 'RETURN', ?, 1, 'paypal', ?);
            """,
            (str(uuid.uuid4()), purchase_id, purchase["agent_id"], purchase["total_cents"], now_iso)
        )

    return execute_capture_flow(conn, purchase_id, paypal_client, boot_id)

