"""
Allowance Platform v1 REST API Routes.
Enforces Invariant 6, Multi-Tenant Isolation, Scoped Identity, and Header-Only Idempotency.
"""

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional
try:
    from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
except ImportError:
    class APIRouter:
        def __init__(self, *args, **kwargs):
            self.routes = []
        def get(self, path, **kwargs):
            def decorator(func):
                self.routes.append(("GET", path, func))
                return func
            return decorator
        def post(self, path, **kwargs):
            def decorator(func):
                self.routes.append(("POST", path, func))
                return func
            return decorator
        def put(self, path, **kwargs):
            def decorator(func):
                self.routes.append(("PUT", path, func))
                return func
            return decorator
    def Depends(dependency=None):
        return dependency
    class Header:
        def __init__(self, default=..., **kwargs):
            self.default = default
    class Response:
        def __init__(self, content="", status_code=200):
            self.content = content
            self.status_code = status_code
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = "", headers: dict = None, **kwargs):
            super().__init__(f"{status_code}: {detail}")
            self.status_code = status_code
            self.detail = detail
            self.headers = headers or {}

from ...auth.credentials import (
    CredentialContext,
    authenticate_api_key,
)
from ...db.connection import get_thread_connection, transactional_session
from ...db.financial_journal import get_budget_snapshot
from ...db.quotes import create_trusted_quote, get_valid_quote
from ...orchestrator.admission import check_and_admit_run, AdmissionRejectedError
from ...orchestrator.core import (
    approve_pending_purchase,
    create_paypal_order_for_purchase,
    get_agent_policy,
    reject_pending_purchase,
    submit_cart_to_orchestrator,
    PurchaseState,
)
from ...orchestrator.capture_lifecycle import execute_capture_flow
from ...paypal import get_paypal_client


v1_router = APIRouter(prefix="/v1")


def get_current_credential(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
) -> CredentialContext:
    """
    Extracts and authenticates API key from Authorization: Bearer <key> or X-API-Key header.
    Tenant and agent identity are derived strictly from the credential.
    """
    token = x_api_key or authorization
    if not token:
        # Development fallback for unauthenticated requests in non-production environments
        import os
        if os.environ.get("APP_ENV", "development") != "production":
            return CredentialContext(
                tenant_id="tenant_demo",
                agent_id="default",
                credential_id="cred_demo",
                name="Demo Dev Context",
                scopes=["propose", "read", "admin"],
                expires_at=None
            )
        raise HTTPException(status_code=401, detail="Missing API Key or Authorization header.")

    conn = get_thread_connection()
    cred = authenticate_api_key(conn, token)
    if not cred:
        raise HTTPException(status_code=401, detail="Invalid, revoked, or expired API Key.")
    return cred


@v1_router.get("/allowance")
def get_allowance(
    cred: CredentialContext = Depends(get_current_credential)
):
    """
    Returns spending authority and remaining allowance for calling agent.
    Never exposes exact policy rules to prevent threshold-gaming attacks.
    """
    conn = get_thread_connection()
    now = time.time()
    budget_day = time.strftime("%Y-%m-%d", time.gmtime(now))

    policy = get_agent_policy(conn, cred.agent_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Agent policy not configured.")

    snapshot = get_budget_snapshot(conn, cred.agent_id, budget_day)
    remaining_cents = max(0, policy.daily_budget_cents - snapshot.r3_committed_cents)

    return {
        "agent_id": cred.agent_id,
        "tenant_id": cred.tenant_id,
        "daily_budget_cents": policy.daily_budget_cents,
        "remaining_cents": remaining_cents,
        "reserved_cents": snapshot.reserved_cents,
        "captured_cents": snapshot.captured_cents,
        "unresolved_cents": snapshot.unresolved_cents,
        "currency": "USD",
        "frozen": policy.frozen,
        "budget_day": budget_day
    }


@v1_router.post("/quotes", status_code=201)
def request_quote(
    payload: dict,
    cred: CredentialContext = Depends(get_current_credential)
):
    """
    Requests a server-calculated trusted price quote.
    Prices are looked up authoritative from catalog metadata.
    """
    items = payload.get("items", [])
    currency = payload.get("currency", "USD")

    conn = get_thread_connection()
    try:
        with transactional_session(conn):
            quote_id, cart, expires_at = create_trusted_quote(
                conn=conn,
                tenant_id=cred.tenant_id,
                agent_id=cred.agent_id,
                raw_items=items,
                currency=currency
            )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return {
        "quote_id": quote_id,
        "items": [
            {
                "product_id": i.product_id,
                "merchant_id": i.merchant_id,
                "name": i.name,
                "unit_price_cents": i.unit_price_cents,
                "quantity": i.quantity,
                "line_total_cents": i.unit_price_cents * i.quantity
            }
            for i in cart.items
        ],
        "total_cents": cart.total_cents,
        "amount_usd": f"{cart.total_cents / 100:.2f}",
        "currency": cart.currency,
        "cart_hash": cart.cart_hash,
        "expires_at": expires_at
    }


@v1_router.post("/purchase-requests", status_code=202)
def submit_purchase_request(
    payload: dict,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    cred: CredentialContext = Depends(get_current_credential)
):
    """
    Submits a purchase proposal to Allowance.
    Enforces Invariants 4 & 6:
    - Tenant and agent identity are derived strictly from authenticated API key.
    - Prices are looked up from server quote or server catalog.
    - Capacity and policy check under serialized BEGIN IMMEDIATE.
    """
    conn = get_thread_connection()
    now = time.time()
    quote_id = payload.get("quote_id")
    raw_items = payload.get("items", [])
    currency = payload.get("currency", "USD")
    purpose = payload.get("purpose", "")

    # 1. Resolve FrozenCart from trusted quote or direct items
    cart = None
    if quote_id:
        cached = get_valid_quote(conn, quote_id, cred.tenant_id, cred.agent_id, now)
        if not cached:
            raise HTTPException(status_code=422, detail="Quote not found, expired, or belongs to another agent.")
        cart, _ = cached
    elif raw_items:
        try:
            with transactional_session(conn):
                _, cart, _ = create_trusted_quote(conn, cred.tenant_id, cred.agent_id, raw_items, currency)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err))
    else:
        raise HTTPException(status_code=422, detail="Request must provide either 'quote_id' or 'items'.")

    # 2. Compute request hash for idempotency checking
    req_hash = hashlib.sha256(f"{cart.cart_hash}:{purpose}".encode("utf-8")).hexdigest()
    run_id = str(uuid.uuid4())

    try:
        with transactional_session(conn):
            admitted, status = check_and_admit_run(
                conn=conn,
                tenant_id=cred.tenant_id,
                agent_id=cred.agent_id,
                idempotency_key=idempotency_key,
                request_hash=req_hash,
                run_id=run_id,
                now=now,
                user_prompt=purpose
            )
    except AdmissionRejectedError as e:
        if e.reason == "idempotency_hash_mismatch":
            raise HTTPException(status_code=422, detail="Idempotency-Key already used with different payload.")
        raise HTTPException(status_code=503, detail=f"Admission rejected: {e.reason}", headers={"Retry-After": str(e.retry_after_seconds)})

    if status == "ALREADY_EXISTS":
        existing_run = conn.execute(
            "SELECT id, status FROM agent_runs WHERE idempotency_key = ? AND tenant_id = ?;",
            (idempotency_key, cred.tenant_id)
        ).fetchone()
        existing_pur = conn.execute(
            "SELECT id, status, total_cents FROM purchases WHERE run_id = ? AND tenant_id = ?;",
            (existing_run["id"], cred.tenant_id)
        ).fetchone() if existing_run else None

        response.status_code = 200
        return {
            "run_id": existing_run["id"] if existing_run else None,
            "purchase_request_id": existing_pur["id"] if existing_pur else None,
            "status": existing_pur["status"] if existing_pur else existing_run["status"],
            "idempotent_replay": True
        }

    # 3. Submit cart to orchestrator for P1 evaluation & financial reservation
    try:
        pid, target_state, decision = submit_cart_to_orchestrator(
            conn=conn,
            tenant_id=cred.tenant_id,
            agent_id=cred.agent_id,
            run_id=run_id,
            cart=cart,
            now=now
        )
    except Exception as exc:
        with transactional_session(conn):
            conn.execute("UPDATE agent_runs SET status = 'FAILED', status_reason = ? WHERE id = ?;", (str(exc), run_id))
        raise HTTPException(status_code=500, detail=f"Orchestration error: {str(exc)}")

    # 4. If auto-approved, advance through PayPal order creation and mock capture
    paypal_client = get_paypal_client()
    if target_state == PurchaseState.AUTO_APPROVED:
        create_paypal_order_for_purchase(conn, pid, paypal_client)
        p_row = conn.execute("SELECT paypal_order_id FROM purchases WHERE id = ?;", (pid,)).fetchone()
        if p_row and p_row["paypal_order_id"]:
            # In mock environment, advance to buyer approval and capture
            if hasattr(paypal_client, "set_buyer_approved"):
                paypal_client.set_buyer_approved(p_row["paypal_order_id"])
                with transactional_session(conn):
                    conn.execute("UPDATE purchases SET status = 'BUYER_APPROVED' WHERE id = ?;", (pid,))
                execute_capture_flow(conn, pid, paypal_client, boot_id="v1_runtime")
                target_state = PurchaseState.CAPTURED

    with transactional_session(conn):
        conn.execute("UPDATE agent_runs SET status = 'COMPLETED' WHERE id = ?;", (run_id,))

    response.status_code = 201 if target_state == PurchaseState.CAPTURED else 202
    return {
        "purchase_request_id": pid,
        "run_id": run_id,
        "status": target_state.value,
        "decision": {
            "outcome": decision.outcome.value,
            "rule": decision.rule_name,
            "reason": decision.reason,
            "policy_version": decision.policy_version
        },
        "amount": {
            "value_cents": cart.total_cents,
            "value_usd": f"{cart.total_cents / 100:.2f}",
            "currency": cart.currency
        },
        "line_items": [
            {"product_id": i.product_id, "name": i.name, "quantity": i.quantity, "unit_price_cents": i.unit_price_cents}
            for i in cart.items
        ]
    }


@v1_router.get("/purchase-requests")
def list_purchase_requests(
    cred: CredentialContext = Depends(get_current_credential)
):
    """Lists recent purchase requests for this tenant."""
    conn = get_thread_connection()
    cur = conn.execute(
        """
        SELECT id, agent_id, status, status_reason, auto_approved, total_cents,
               currency, cart_json, created_at, updated_at
        FROM purchases
        WHERE tenant_id = ?
        ORDER BY created_at DESC LIMIT 50;
        """,
        (cred.tenant_id,)
    )
    rows = cur.fetchall()
    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "agent_id": r["agent_id"],
            "status": r["status"],
            "status_reason": r["status_reason"],
            "auto_approved": bool(r["auto_approved"]),
            "total_cents": r["total_cents"],
            "amount_usd": f"{r['total_cents'] / 100:.2f}",
            "currency": r["currency"],
            "items": json.loads(r["cart_json"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"]
        })
    return results


@v1_router.get("/purchase-requests/{purchase_id}")
def get_purchase_request(
    purchase_id: str,
    cred: CredentialContext = Depends(get_current_credential)
):
    """Retrieves authoritative status of a single purchase request."""
    conn = get_thread_connection()
    cur = conn.execute(
        "SELECT * FROM purchases WHERE id = ? AND tenant_id = ?;",
        (purchase_id, cred.tenant_id)
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Purchase request not found.")

    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "status": row["status"],
        "status_reason": row["status_reason"],
        "auto_approved": bool(row["auto_approved"]),
        "total_cents": row["total_cents"],
        "amount_usd": f"{row['total_cents'] / 100:.2f}",
        "currency": row["currency"],
        "items": json.loads(row["cart_json"]),
        "paypal_order_id": row["paypal_order_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"]
    }


@v1_router.post("/purchase-requests/{purchase_id}/approve")
def approve_request(
    purchase_id: str,
    cred: CredentialContext = Depends(get_current_credential)
):
    """
    Supervisor approval for purchases in PENDING_APPROVAL.
    Enforces separation of duties: requires admin or approver scope.
    """
    if "admin" not in cred.scopes and "approver" not in cred.scopes:
        raise HTTPException(status_code=403, detail="Credential lacks 'admin' or 'approver' scope.")

    conn = get_thread_connection()
    cur = conn.execute(
        "SELECT * FROM purchases WHERE id = ? AND tenant_id = ?;",
        (purchase_id, cred.tenant_id)
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Purchase request not found.")

    # Execute deterministic approval check P2
    new_state, reason = approve_pending_purchase(conn, purchase_id, admin_actor=cred.name)
    if new_state != PurchaseState.APPROVED:
        raise HTTPException(status_code=409, detail=f"Cannot approve: {reason}")

    # Create PayPal order and execute capture
    paypal_client = get_paypal_client()
    create_paypal_order_for_purchase(conn, purchase_id, paypal_client)
    p_cur = conn.execute("SELECT paypal_order_id FROM purchases WHERE id = ?;", (purchase_id,))
    p_row = p_cur.fetchone()
    if p_row and p_row["paypal_order_id"] and hasattr(paypal_client, "set_buyer_approved"):
        paypal_client.set_buyer_approved(p_row["paypal_order_id"])
        with transactional_session(conn):
            conn.execute("UPDATE purchases SET status = 'BUYER_APPROVED' WHERE id = ?;", (purchase_id,))
        execute_capture_flow(conn, purchase_id, paypal_client, boot_id="v1_approval")

    final_row = conn.execute("SELECT status FROM purchases WHERE id = ?;", (purchase_id,)).fetchone()
    return {
        "purchase_id": purchase_id,
        "status": final_row["status"] if final_row else "APPROVED",
        "reason": reason
    }


@v1_router.post("/purchase-requests/{purchase_id}/reject")
def reject_request(
    purchase_id: str,
    payload: Optional[dict] = None,
    cred: CredentialContext = Depends(get_current_credential)
):
    """Rejects a purchase request, releasing the reserved budget immediately."""
    if "admin" not in cred.scopes and "approver" not in cred.scopes:
        raise HTTPException(status_code=403, detail="Credential lacks 'admin' or 'approver' scope.")

    conn = get_thread_connection()
    cur = conn.execute("SELECT * FROM purchases WHERE id = ? AND tenant_id = ?;", (purchase_id, cred.tenant_id))
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Purchase request not found.")

    reason = (payload or {}).get("reason", "Rejected by supervisor")
    new_state, result_reason = reject_pending_purchase(conn, purchase_id, admin_actor=cred.name, reason=reason)
    return {
        "purchase_id": purchase_id,
        "status": new_state.value,
        "reason": result_reason
    }


@v1_router.post("/freeze")
def freeze_agent(
    payload: Optional[dict] = None,
    cred: CredentialContext = Depends(get_current_credential)
):
    """Emergency Kill Switch: Activates Rule R0."""
    target_agent = (payload or {}).get("agent_id", cred.agent_id)
    conn = get_thread_connection()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with transactional_session(conn):
        cur = conn.execute(
            "UPDATE policy SET frozen = 1, updated_at = ? WHERE agent_id = ? AND tenant_id = ?;",
            (now_iso, target_agent, cred.tenant_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Target agent policy not found.")

    return {"agent_id": target_agent, "tenant_id": cred.tenant_id, "frozen": True}


@v1_router.post("/unfreeze")
def unfreeze_agent(
    payload: Optional[dict] = None,
    cred: CredentialContext = Depends(get_current_credential)
):
    """Deactivates Rule R0."""
    target_agent = (payload or {}).get("agent_id", cred.agent_id)
    conn = get_thread_connection()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with transactional_session(conn):
        cur = conn.execute(
            "UPDATE policy SET frozen = 0, updated_at = ? WHERE agent_id = ? AND tenant_id = ?;",
            (now_iso, target_agent, cred.tenant_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Target agent policy not found.")

    return {"agent_id": target_agent, "tenant_id": cred.tenant_id, "frozen": False}

