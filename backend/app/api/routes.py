"""
FastAPI Routes for Allowance Platform.
Enforces Invariant 6, Tenant Scoping, DTO Allowlists, and Constant-time Token Validation.
"""

import hashlib
import json
import sqlite3
import time
import uuid
from typing import List, Optional
try:
    from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
    from fastapi.responses import StreamingResponse
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
    class StreamingResponse(Response):
        pass
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = ""):
            super().__init__(f"{status_code}: {detail}")
            self.status_code = status_code
            self.detail = detail

from ..db.connection import get_thread_connection, transactional_session
from .dto import (
    PublicPurchaseDTO,
    PublicLedgerDTO,
    PublicPolicyDTO,
    PublicAgentRunDTO,
    PublicEventDTO,
    AdminPurchaseDetailDTO,
    AdminApprovalLinkDTO,
    RunAgentRequest,
    UpdatePolicyRequest,
)
from .auth import (
    verify_admin_token,
    get_demo_tenant,
    enforce_rate_limit,
)
from ..orchestrator.admission import check_and_admit_run, AdmissionRejectedError
from ..orchestrator.core import approve_pending_purchase, get_agent_policy
from ..orchestrator.capture_lifecycle import execute_read_only_reconciliation
from ..paypal.client import MockPayPalClient
from ..runtime import get_worker_pool


api_router = APIRouter()
mock_paypal_instance = MockPayPalClient()


@api_router.get("/health")
def health_check():
    """Public minimal operational status check."""
    return {"status": "ok", "platform": "Allowance v4.2.2", "env": "operational"}


@api_router.post("/agent/run", status_code=202)
def run_agent(
    payload: RunAgentRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    tenant_id: str = Depends(get_demo_tenant)
):
    """
    Submits new autonomous agent purchasing run.
    Enforces Invariant 4: capacity checks & atomic admission under BEGIN IMMEDIATE before row insertion.

    The run is enqueued only after the admission transaction commits, so a worker
    can never observe a run row that is not yet durable.
    """
    enforce_rate_limit(request, limit=10)
    
    conn = get_thread_connection()
    now = time.time()
    run_id = str(uuid.uuid4())
    req_hash = hashlib.sha256(payload.user_prompt.encode("utf-8")).hexdigest()

    try:
        with transactional_session(conn):
            admitted, status = check_and_admit_run(
                conn=conn,
                tenant_id=tenant_id,
                agent_id=payload.agent_id,
                idempotency_key=idempotency_key,
                request_hash=req_hash,
                run_id=run_id,
                now=now,
                user_prompt=payload.user_prompt,
            )
    except AdmissionRejectedError as e:
        if e.reason == "idempotency_hash_mismatch":
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key was already used with a different request payload."
            )
        raise HTTPException(
            status_code=503,
            detail=f"Admission rejected: {e.reason}",
            headers={"Retry-After": str(e.retry_after_seconds)}
        )

    if status == "ALREADY_EXISTS":
        # Replay: return the durable run instead of a phantom id that 404s.
        existing = conn.execute(
            "SELECT id, status FROM agent_runs WHERE idempotency_key = ? AND tenant_id = ?;",
            (idempotency_key, tenant_id)
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=409, detail="Idempotent run is no longer available.")
        response.status_code = 200
        return {
            "run_id": existing["id"],
            "status": existing["status"],
            "admitted": True,
            "idempotent_replay": True,
        }

    # Post-commit dispatch: the row is durable before it can be claimed.
    pool = get_worker_pool()
    if pool is None or not pool.enqueue_run(run_id):
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        with transactional_session(conn):
            conn.execute(
                "UPDATE agent_runs SET status = 'FAILED', status_reason = 'queue_rejected' WHERE id = ? AND status = 'QUEUED';",
                (run_id,)
            )
            conn.execute(
                """
                INSERT INTO events (tenant_id, event_type, run_id, payload_json, created_at)
                VALUES (?, 'RUN_REJECTED', ?, ?, ?);
                """,
                (tenant_id, run_id, json.dumps({"reason": "queue_rejected"}), now_iso)
            )
        raise HTTPException(
            status_code=503,
            detail="Worker queue is temporarily full; retry with a new Idempotency-Key.",
            headers={"Retry-After": "1"}
        )

    return {"run_id": run_id, "status": "QUEUED", "admitted": admitted}


@api_router.get("/agent/run/{run_id}", response_model=PublicAgentRunDTO)
def get_run_status(
    run_id: str,
    tenant_id: str = Depends(get_demo_tenant)
):
    """Returns tenant-scoped run status."""
    conn = get_thread_connection()
    cur = conn.execute("SELECT * FROM agent_runs WHERE id = ? AND tenant_id = ?;", (run_id, tenant_id))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Run not found.")
    return PublicAgentRunDTO(
        id=row["id"],
        agent_id=row["agent_id"],
        status=row["status"],
        status_reason=row["status_reason"],
        queued_at=row["queued_at"],
        started_at=row["started_at"],
        created_at=row["created_at"]
    )


@api_router.get("/purchases", response_model=List[PublicPurchaseDTO])
def list_purchases(
    tenant_id: str = Depends(get_demo_tenant)
):
    """Lists purchases for active tenant. Strips confidential payment IDs."""
    conn = get_thread_connection()
    cur = conn.execute(
        "SELECT * FROM purchases WHERE tenant_id = ? ORDER BY created_at DESC LIMIT 50;",
        (tenant_id,)
    )
    rows = cur.fetchall()
    return [
        PublicPurchaseDTO(
            id=r["id"],
            agent_id=r["agent_id"],
            status=r["status"],
            status_reason=r["status_reason"],
            auto_approved=bool(r["auto_approved"]),
            total_cents=r["total_cents"],
            currency=r["currency"],
            merchant_ids=json.loads(r["merchant_ids_json"]),
            budget_day=r["budget_day"],
            created_at=r["created_at"],
            updated_at=r["updated_at"]
        )
        for r in rows
    ]


@api_router.get("/purchases/{purchase_id}", response_model=PublicPurchaseDTO)
def get_purchase(
    purchase_id: str,
    tenant_id: str = Depends(get_demo_tenant)
):
    """Gets purchase details. Strips confidential payment IDs."""
    conn = get_thread_connection()
    cur = conn.execute("SELECT * FROM purchases WHERE id = ? AND tenant_id = ?;", (purchase_id, tenant_id))
    r = cur.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Purchase not found.")
    return PublicPurchaseDTO(
        id=r["id"],
        agent_id=r["agent_id"],
        status=r["status"],
        status_reason=r["status_reason"],
        auto_approved=bool(r["auto_approved"]),
        total_cents=r["total_cents"],
        currency=r["currency"],
        merchant_ids=json.loads(r["merchant_ids_json"]),
        budget_day=r["budget_day"],
        created_at=r["created_at"],
        updated_at=r["updated_at"]
    )


@api_router.get("/purchases/{purchase_id}/approval-link", response_model=AdminApprovalLinkDTO)
def get_approval_link(
    purchase_id: str,
    is_admin: bool = Depends(verify_admin_token),
    tenant_id: str = Depends(get_demo_tenant)
):
    """Admin-only endpoint retrieving confidential PayPal buyer approval URL."""
    conn = get_thread_connection()
    cur = conn.execute(
        "SELECT approval_url, paypal_order_id, expires_at FROM purchases WHERE id = ? AND tenant_id = ?;",
        (purchase_id, tenant_id)
    )
    r = cur.fetchone()
    if not r or not r["approval_url"]:
        raise HTTPException(status_code=404, detail="Approval URL not found or not yet generated.")
    expires_at = r["expires_at"]
    if expires_at is not None:
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at))
    return AdminApprovalLinkDTO(
        purchase_id=purchase_id,
        paypal_order_id=r["paypal_order_id"],
        approval_url=r["approval_url"],
        expires_at=expires_at
    )


@api_router.post("/purchases/{purchase_id}/approve")
def admin_approve(
    purchase_id: str,
    is_admin: bool = Depends(verify_admin_token)
):
    """Admin-only approval of a PENDING_APPROVAL purchase."""
    conn = get_thread_connection()
    new_state, reason = approve_pending_purchase(conn, purchase_id, admin_actor="admin")
    if new_state.value == "FAILED":
        raise HTTPException(status_code=409, detail=f"Cannot approve: {reason}")
    return {"purchase_id": purchase_id, "status": new_state.value, "reason": reason}


@api_router.post("/purchases/{purchase_id}/reconcile")
def reconcile_purchase(
    purchase_id: str,
    is_admin: bool = Depends(verify_admin_token)
):
    """Admin-only trigger for read-only reconciliation of CAPTURE_UNKNOWN purchases."""
    conn = get_thread_connection()
    status, detail = execute_read_only_reconciliation(conn, purchase_id, mock_paypal_instance)
    return {"purchase_id": purchase_id, "reconciliation_status": status, "detail": detail}


@api_router.get("/ledger", response_model=List[PublicLedgerDTO])
def get_ledger(
    tenant_id: str = Depends(get_demo_tenant)
):
    """Returns immutable financial ledger transitions."""
    conn = get_thread_connection()
    cur = conn.execute("SELECT * FROM ledger ORDER BY created_at DESC LIMIT 100;")
    rows = cur.fetchall()
    return [
        PublicLedgerDTO(
            id=r["id"],
            purchase_id=r["purchase_id"],
            agent_id=r["agent_id"],
            from_state=r["from_state"],
            to_state=r["to_state"],
            rule_name=r["rule_name"],
            reason=r["reason"],
            checkpoint=r["checkpoint"],
            amount_cents=r["amount_cents"],
            actor=r["actor"],
            created_at=r["created_at"]
        )
        for r in rows
    ]


@api_router.get("/policy", response_model=PublicPolicyDTO)
def get_policy(
    agent_id: str = "default",
    tenant_id: str = Depends(get_demo_tenant)
):
    """Returns active financial policy limits."""
    conn = get_thread_connection()
    pol = get_agent_policy(conn, agent_id)
    if not pol:
        raise HTTPException(status_code=404, detail="Policy not found.")
    return PublicPolicyDTO(
        agent_id=pol.agent_id,
        version=pol.version,
        daily_budget_cents=pol.daily_budget_cents,
        max_per_tx_cents=pol.max_per_tx_cents,
        approval_threshold_cents=pol.approval_threshold_cents,
        velocity_cap_cents=pol.velocity_cap_cents,
        allowlist=list(pol.allowlist),
        timezone=pol.timezone,
        frozen=pol.frozen
    )


@api_router.post("/freeze")
def freeze_policy(
    agent_id: str = "default",
    is_admin: bool = Depends(verify_admin_token)
):
    """Emergency Kill Switch: Activates Rule R0."""
    conn = get_thread_connection()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with transactional_session(conn):
        conn.execute("UPDATE policy SET frozen = 1, updated_at = ? WHERE agent_id = ?;", (now_iso, agent_id))
    return {"agent_id": agent_id, "frozen": True}


@api_router.post("/unfreeze")
def unfreeze_policy(
    agent_id: str = "default",
    is_admin: bool = Depends(verify_admin_token)
):
    """Deactivates Rule R0."""
    conn = get_thread_connection()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with transactional_session(conn):
        conn.execute("UPDATE policy SET frozen = 0, updated_at = ? WHERE agent_id = ?;", (now_iso, agent_id))
    return {"agent_id": agent_id, "frozen": False}


@api_router.get("/events")
def stream_events(
    request: Request,
    last_event_id: Optional[int] = Header(None, alias="Last-Event-ID"),
    tenant_id: str = Depends(get_demo_tenant)
):
    """Server-Sent Events stream delivering live updates with Last-Event-ID replay."""
    def event_generator():
        conn = get_thread_connection()
        current_id = last_event_id or 0
        while True:
            cur = conn.execute(
                "SELECT * FROM events WHERE tenant_id = ? AND id > ? ORDER BY id ASC LIMIT 20;",
                (tenant_id, current_id)
            )
            rows = cur.fetchall()
            for r in rows:
                current_id = r["id"]
                data = json.dumps({"type": r["event_type"], "payload": json.loads(r["payload_json"])})
                yield f"id: {current_id}\nevent: {r['event_type']}\ndata: {data}\n\n"

            time.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
