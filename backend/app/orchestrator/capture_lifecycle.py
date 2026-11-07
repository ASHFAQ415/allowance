"""
Safe Capture Execution, Reconcile, and Late Settlement Protocol.
Enforces Invariant 1 (No Blind Retries) and Invariant 2 (Workflow vs Financial Separation).
"""

import json
import sqlite3
import time
import uuid
from typing import Optional, Tuple

from .state_machine import PurchaseState, validate_transition
from ..db.connection import transactional_session
from ..db.financial_journal import FactType, record_financial_fact
from ..paypal.client import PayPalClientInterface, TransportOutcome
from ..policy.rules import Checkpoint, Outcome
from .rechecks import get_agent_policy, reevaluate_purchase, release_purchase_reservation


EXPECTED_MERCHANT_ID = "MOCK_MERCHANT_123"


class VerificationError(Exception):
    """Raised when pre-capture verification of order amount, currency, or payee fails."""
    pass


def verify_order(order_data: dict, purchase_row: sqlite3.Row) -> None:
    """
    Pre-capture verification assertions on GET /orders/{id} payload.
    Asserts:
    1. status == 'APPROVED'
    2. amount matches purchase.total_cents
    3. currency == 'USD'
    4. payee matches expected merchant ID
    """
    if order_data.get("status") != "APPROVED":
        raise VerificationError(f"Expected order status 'APPROVED', got '{order_data.get('status')}'.")

    units = order_data.get("purchase_units", [])
    if not units:
        raise VerificationError("No purchase_units found in order.")

    unit = units[0]
    amount_val = float(unit.get("amount", {}).get("value", 0))
    expected_val = purchase_row["total_cents"] / 100.0
    if abs(amount_val - expected_val) > 0.001:
        raise VerificationError(f"Amount mismatch: order has ${amount_val:.2f}, expected ${expected_val:.2f}.")

    currency = unit.get("amount", {}).get("currency_code")
    if currency != purchase_row["currency"]:
        raise VerificationError(f"Currency mismatch: order has '{currency}', expected '{purchase_row['currency']}'.")

    payee_id = unit.get("payee", {}).get("merchant_id")
    if payee_id != EXPECTED_MERCHANT_ID:
        raise VerificationError(f"Payee mismatch: order payee '{payee_id}' does not match expected '{EXPECTED_MERCHANT_ID}'.")


def execute_capture_flow(
    conn: sqlite3.Connection,
    purchase_id: str,
    paypal_client: PayPalClientInterface,
    boot_id: str
) -> Tuple[str, str]:
    """
    Executes safe capture operation under Invariant 1 & 2.
    Returns: (final_status, reason_or_capture_id)
    """
    now = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    # 1. Fetch purchase
    cur = conn.execute("SELECT * FROM purchases WHERE id = ?;", (purchase_id,))
    purchase = cur.fetchone()
    if not purchase:
        return "FAILED", "purchase_not_found"

    if purchase["status"] != PurchaseState.BUYER_APPROVED.value:
        # A concurrent capture worker may already own the claim. Treat any
        # post-claim state as a lost claim rather than a precondition failure
        # so callers observe a consistent "single winner" result. The loser
        # reports CAPTURING (never CAPTURED) so exactly one caller owns the
        # successful outcome.
        if purchase["status"] in (
            PurchaseState.CAPTURING.value,
            PurchaseState.CAPTURED.value,
            PurchaseState.CAPTURE_UNKNOWN.value,
        ):
            return "CAPTURING", "capture_claim_lost"
        return purchase["status"], "not_in_buyer_approved_state"

    order_id = purchase["paypal_order_id"]

    # 2. Pre-capture Verification (GET order)
    get_outcome = paypal_client.get_order(order_id)
    if not get_outcome.success or not get_outcome.data:
        return "BUYER_APPROVED", "get_order_failed_before_capture"

    try:
        verify_order(get_outcome.data, purchase)
    except VerificationError as v_err:
        # Tampering detected! Transition to FAILED(amount_mismatch) and release reservation
        with transactional_session(conn):
            conn.execute(
                """
                UPDATE purchases
                SET status = 'FAILED', status_reason = 'amount_mismatch', updated_at = ?
                WHERE id = ? AND status = 'BUYER_APPROVED';
                """,
                (now_iso, purchase_id)
            )
            record_financial_fact(
                conn=conn,
                source_event_key=f"mismatch_release_{purchase_id}",
                tenant_id=purchase["tenant_id"],
                agent_id=purchase["agent_id"],
                purchase_id=purchase_id,
                budget_day=purchase["budget_day"],
                fact_type=FactType.RESERVATION_RELEASED,
                delta_reserved_cents=-purchase["total_cents"],
                delta_unresolved_cents=0,
                delta_captured_cents=0,
                delta_auto_approved_cents=-purchase["total_cents"] if purchase["auto_approved"] else 0,
                actor="system"
            )
        return "FAILED", "amount_mismatch"

    # 2b. Checkpoint P4: Pre-capture policy re-evaluation (e.g. freeze, daily budget)
    policy = get_agent_policy(conn, purchase["agent_id"])
    if policy is not None:
        decision, _ = reevaluate_purchase(conn, purchase, Checkpoint.P4, now)
        if decision and decision.outcome == Outcome.DENY:
            with transactional_session(conn):
                conn.execute(
                    """
                    UPDATE purchases
                    SET status = 'BLOCKED', status_reason = ?, state_entered_at = ?, expires_at = NULL, updated_at = ?
                    WHERE id = ? AND status = 'BUYER_APPROVED';
                    """,
                    (decision.reason, now, now_iso, purchase_id)
                )
                release_purchase_reservation(conn, purchase, actor="system", source_suffix="P4_deny_release")
                conn.execute(
                    """
                    INSERT INTO ledger (
                        id, purchase_id, agent_id, from_state, to_state, rule_name,
                        reason, checkpoint, amount_cents, policy_version, actor, created_at
                    ) VALUES (?, ?, ?, 'BUYER_APPROVED', 'BLOCKED', ?, ?, 'P4', ?, ?, 'system', ?);
                    """,
                    (str(uuid.uuid4()), purchase_id, purchase["agent_id"], decision.rule_name, decision.reason, purchase["total_cents"], policy.version, now_iso)
                )
            return "BLOCKED", decision.reason

    # 3. Atomic Capture Claim (BUYER_APPROVED -> CAPTURING)
    owner_token = str(uuid.uuid4())
    capture_req_id = str(uuid.uuid4())
    claimed = False

    # The claim, the durable capture operation, and the ledger transition are
    # one serialized unit. Only the winner of the CAS may write them.
    with transactional_session(conn):
        cur_claim = conn.execute(
            """
            UPDATE purchases
            SET status = 'CAPTURING', capture_boot_id = ?, updated_at = ?
            WHERE id = ? AND status = 'BUYER_APPROVED';
            """,
            (boot_id, now_iso, purchase_id)
        )
        claimed = cur_claim.rowcount == 1

        if claimed:
            # Record durable capture_operation in PREPARED state
            op_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT OR REPLACE INTO capture_operations (
                    id, purchase_id, capture_request_id, state, owner_token,
                    owner_lease_until, attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, 'PREPARED', ?, ?, 1, ?, ?);
                """,
                (op_id, purchase_id, capture_req_id, owner_token, now + 30.0, now_iso, now_iso)
            )

            # Log ledger transition
            conn.execute(
                """
                INSERT INTO ledger (
                    id, purchase_id, agent_id, from_state, to_state, rule_name,
                    reason, checkpoint, amount_cents, policy_version, actor, created_at
                ) VALUES (?, ?, ?, 'BUYER_APPROVED', 'CAPTURING', 'CAPTURE_CLAIMED', 'Exclusive worker claim', 'P4', ?, ?, 'worker', ?);
                """,
                (str(uuid.uuid4()), purchase_id, purchase["agent_id"], purchase["total_cents"], policy.version if policy else 1, now_iso)
            )

    if not claimed:
        # Another worker won the race between our read and the CAS.
        return "CAPTURING", "capture_claim_lost"

    # 4. Persist MAY_HAVE_BEEN_SENT marker before transmission begins
    with transactional_session(conn):
        conn.execute(
            """
            UPDATE capture_operations
            SET state = 'MAY_HAVE_BEEN_SENT', first_send_started_at = ?, updated_at = ?
            WHERE purchase_id = ? AND owner_token = ?;
            """,
            (now, now_iso, purchase_id, owner_token)
        )

    # 5. Socket Transmission (POST /capture)
    cap_outcome = paypal_client.capture_order(order_id, capture_req_id)

    # Invariant 1: If pre-send connect error occurred (proven no bytes sent), retry exactly ONCE
    if not cap_outcome.bytes_may_have_been_sent and cap_outcome.error_class == "ConnectError":
        time.sleep(1.0)
        with transactional_session(conn):
            conn.execute("UPDATE capture_operations SET attempt_count = 2 WHERE purchase_id = ?;", (purchase_id,))
        cap_outcome = paypal_client.capture_order(order_id, capture_req_id)

    # 6. Parse and Record Outcome
    with transactional_session(conn):
        # Verify ownership lease
        cur_op = conn.execute("SELECT state FROM capture_operations WHERE purchase_id = ? AND owner_token = ?;", (purchase_id, owner_token))
        if not cur_op.fetchone():
            return "CAPTURE_UNKNOWN", "lease_expired"

        # Case A: Definitive Success (HTTP 200/201 with status COMPLETED)
        if cap_outcome.success and cap_outcome.data and cap_outcome.data.get("status") == "COMPLETED":
            cap_id = cap_outcome.data["id"]
            conn.execute(
                """
                UPDATE purchases
                SET status = 'CAPTURED', updated_at = ?
                WHERE id = ? AND status IN ('CAPTURING', 'CAPTURE_UNKNOWN');
                """,
                (now_iso, purchase_id)
            )
            conn.execute(
                """
                UPDATE capture_operations
                SET state = 'DEFINITIVE_SUCCESS', paypal_capture_id = ?, updated_at = ?
                WHERE purchase_id = ?;
                """,
                (cap_id, now_iso, purchase_id)
            )
            conn.execute(
                """
                INSERT INTO ledger (
                    id, purchase_id, agent_id, from_state, to_state, rule_name,
                    reason, checkpoint, amount_cents, policy_version, actor, created_at
                ) VALUES (?, ?, ?, 'CAPTURING', 'CAPTURED', 'CAPTURE_COMPLETED', 'PayPal capture completed', 'P4', ?, 1, 'paypal', ?);
                """,
                (str(uuid.uuid4()), purchase_id, purchase["agent_id"], purchase["total_cents"], now_iso)
            )
            # Settle financial fact: reserved -> captured
            record_financial_fact(
                conn=conn,
                source_event_key=f"capture_settled_{purchase_id}",
                tenant_id=purchase["tenant_id"],
                agent_id=purchase["agent_id"],
                purchase_id=purchase_id,
                budget_day=purchase["budget_day"],
                fact_type=FactType.CAPTURE_SETTLED,
                delta_reserved_cents=-purchase["total_cents"],
                delta_unresolved_cents=0,
                delta_captured_cents=purchase["total_cents"],
                delta_auto_approved_cents=0,
                actor="paypal",
                paypal_capture_id=cap_id
            )
            return "CAPTURED", cap_id

        # Case B: Definitive Failure (HTTP 200 with DECLINED status)
        elif cap_outcome.data and cap_outcome.data.get("status") == "DECLINED":
            conn.execute(
                """
                UPDATE purchases
                SET status = 'FAILED', status_reason = 'capture_declined', updated_at = ?
                WHERE id = ? AND status = 'CAPTURING';
                """,
                (now_iso, purchase_id)
            )
            conn.execute("UPDATE capture_operations SET state = 'DEFINITIVE_FAILURE', updated_at = ? WHERE purchase_id = ?;", (now_iso, purchase_id))
            # Release reservation
            record_financial_fact(
                conn=conn,
                source_event_key=f"decline_release_{purchase_id}",
                tenant_id=purchase["tenant_id"],
                agent_id=purchase["agent_id"],
                purchase_id=purchase_id,
                budget_day=purchase["budget_day"],
                fact_type=FactType.RESERVATION_RELEASED,
                delta_reserved_cents=-purchase["total_cents"],
                delta_unresolved_cents=0,
                delta_captured_cents=0,
                delta_auto_approved_cents=-purchase["total_cents"] if purchase["auto_approved"] else 0,
                actor="paypal"
            )
            return "FAILED", "capture_declined"

        # Case C: Ambiguous Transport Error / Timeout / 429 / 500 / PENDING -> CAPTURE_UNKNOWN
        else:
            conn.execute(
                """
                UPDATE purchases
                SET status = 'CAPTURE_UNKNOWN', status_reason = 'transport_ambiguity', updated_at = ?
                WHERE id = ? AND status = 'CAPTURING';
                """,
                (now_iso, purchase_id)
            )
            conn.execute("UPDATE capture_operations SET state = 'UNKNOWN', updated_at = ? WHERE purchase_id = ?;", (now_iso, purchase_id))
            conn.execute(
                """
                INSERT INTO ledger (
                    id, purchase_id, agent_id, from_state, to_state, rule_name,
                    reason, checkpoint, amount_cents, policy_version, actor, created_at
                ) VALUES (?, ?, ?, 'CAPTURING', 'CAPTURE_UNKNOWN', 'TRANSPORT_AMBIGUITY', 'Ambiguous capture response', 'P4', ?, 1, 'orchestrator', ?);
                """,
                (str(uuid.uuid4()), purchase_id, purchase["agent_id"], purchase["total_cents"], now_iso)
            )
            # Move reservation: reserved -> unresolved. Total daily commitment remains strictly preserved!
            record_financial_fact(
                conn=conn,
                source_event_key=f"unknown_hold_{purchase_id}",
                tenant_id=purchase["tenant_id"],
                agent_id=purchase["agent_id"],
                purchase_id=purchase_id,
                budget_day=purchase["budget_day"],
                fact_type=FactType.CAPTURE_BECAME_UNKNOWN,
                delta_reserved_cents=-purchase["total_cents"],
                delta_unresolved_cents=purchase["total_cents"],
                delta_captured_cents=0,
                delta_auto_approved_cents=0,
                actor="orchestrator"
            )
            return "CAPTURE_UNKNOWN", "transport_ambiguity"


def execute_read_only_reconciliation(
    conn: sqlite3.Connection,
    purchase_id: str,
    paypal_client: PayPalClientInterface
) -> Tuple[str, str]:
    """
    Read-only reconciliation for purchases in CAPTURE_UNKNOWN.
    Enforces Invariant 1: NEVER issues another capture POST.
    Decides authoritatively from GET /v2/checkout/orders/{id} only.
    """
    now = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    cur = conn.execute("SELECT * FROM purchases WHERE id = ?;", (purchase_id,))
    purchase = cur.fetchone()
    if not purchase or purchase["status"] != PurchaseState.CAPTURE_UNKNOWN.value:
        return "IGNORED", "not_in_capture_unknown"

    order_id = purchase["paypal_order_id"]
    get_outcome = paypal_client.get_order(order_id)
    if not get_outcome.success or not get_outcome.data:
        return "CAPTURE_UNKNOWN", "reconcile_get_failed"

    order = get_outcome.data
    order_status = order.get("status")

    # Authoritative resolution:
    if order_status == "COMPLETED":
        # Capture completed on PayPal! Resolve to CAPTURED
        cap_id = f"CAP_RECON_{uuid.uuid4().hex[:8]}"
        with transactional_session(conn):
            conn.execute("UPDATE purchases SET status = 'CAPTURED', updated_at = ? WHERE id = ?;", (now_iso, purchase_id))
            conn.execute("UPDATE capture_operations SET state = 'RESOLVED_SUCCESS', paypal_capture_id = ?, updated_at = ? WHERE purchase_id = ?;", (cap_id, now_iso, purchase_id))
            conn.execute(
                """
                INSERT INTO ledger (
                    id, purchase_id, agent_id, from_state, to_state, rule_name,
                    reason, checkpoint, amount_cents, policy_version, actor, created_at
                ) VALUES (?, ?, ?, 'CAPTURE_UNKNOWN', 'CAPTURED', 'RECONCILE_RESOLVED', 'Reconciliation verified completed capture', 'P4', ?, 1, 'reconciler', ?);
                """,
                (str(uuid.uuid4()), purchase_id, purchase["agent_id"], purchase["total_cents"], now_iso)
            )
            # Move unresolved -> captured
            record_financial_fact(
                conn=conn,
                source_event_key=f"reconcile_settled_{purchase_id}",
                tenant_id=purchase["tenant_id"],
                agent_id=purchase["agent_id"],
                purchase_id=purchase_id,
                budget_day=purchase["budget_day"],
                fact_type=FactType.CAPTURE_SETTLED,
                delta_reserved_cents=0,
                delta_unresolved_cents=-purchase["total_cents"],
                delta_captured_cents=purchase["total_cents"],
                delta_auto_approved_cents=0,
                actor="reconciler",
                paypal_capture_id=cap_id
            )
        return "CAPTURED", cap_id

    # If still APPROVED or PENDING, remain CAPTURE_UNKNOWN. Zero retry POSTs!
    return "CAPTURE_UNKNOWN", f"order_status_still_{order_status}"


def record_late_capture_adjustment(
    conn: sqlite3.Connection,
    purchase_id: str,
    paypal_capture_id: str,
    resolved_by: str = "admin",
    notes: str = "Discovered late capture settlement via provider audit"
) -> bool:
    """
    Handles late capture success after purchase reached terminal FAILED(unresolved_capture).
    Enforces Invariant 2:
    - Leaves purchases.status='FAILED' intact.
    - Inserts append-only record into ledger_adjustments.
    - Records LATE_CAPTURE_SETTLED financial fact.
    - Zero fake ledger transitions!
    """
    now = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    cur = conn.execute("SELECT * FROM purchases WHERE id = ?;", (purchase_id,))
    purchase = cur.fetchone()
    if not purchase:
        return False

    adj_id = str(uuid.uuid4())
    with transactional_session(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO ledger_adjustments (
                id, purchase_id, agent_id, adjustment_type, amount_cents,
                paypal_capture_id, discovered_at, resolved_by, notes, created_at
            ) VALUES (?, ?, ?, 'LATE_CAPTURE_SETTLED', ?, ?, ?, ?, ?, ?);
            """,
            (adj_id, purchase_id, purchase["agent_id"], purchase["total_cents"],
             paypal_capture_id, now_iso, resolved_by, notes, now_iso)
        )
        conn.execute(
            """
            UPDATE capture_operations
            SET state = 'RESOLVED_SUCCESS', paypal_capture_id = ?, updated_at = ?
            WHERE purchase_id = ?;
            """,
            (paypal_capture_id, now_iso, purchase_id)
        )
        record_financial_fact(
            conn=conn,
            source_event_key=f"late_settlement_{purchase_id}",
            tenant_id=purchase["tenant_id"],
            agent_id=purchase["agent_id"],
            purchase_id=purchase_id,
            budget_day=purchase["budget_day"],
            fact_type=FactType.LATE_CAPTURE_SETTLED,
            delta_reserved_cents=0,
            delta_unresolved_cents=-purchase["total_cents"],
            delta_captured_cents=purchase["total_cents"],
            delta_auto_approved_cents=0,
            actor=resolved_by,
            paypal_capture_id=paypal_capture_id
        )

    return True

