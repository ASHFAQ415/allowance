"""
Admission Controller for Allowance Platform.
Enforces Invariant 4:
- Capacity checks, concurrency checks, and job insertion must execute within the same serialized database transaction.
- Reject excess work before inserting a job record. Rejected work must not create phantom jobs or reservations.
"""

import sqlite3
from typing import Tuple


MAX_ACTIVE_RUNS_TOTAL = 8           # 4 workers running + 4 queue capacity
MAX_ACTIVE_RUNS_PER_AGENT = 2       # Per-agent concurrency cap
MAX_ACTIVE_RESERVATIONS = 5         # Global limit on concurrent unfinalized purchases
MAX_PENDING_APPROVALS = 3           # Human approval queue ceiling


class AdmissionRejectedError(Exception):
    """Raised when admission control rejects a run or cart submission due to capacity."""
    def __init__(self, reason: str, retry_after_seconds: int = 5):
        super().__init__(reason)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


def check_and_admit_run(
    conn: sqlite3.Connection,
    tenant_id: str,
    agent_id: str,
    idempotency_key: str,
    request_hash: str,
    run_id: str,
    now: float
) -> Tuple[bool, str]:
    """
    Atomically checks active run limits and inserts agent_run record under BEGIN IMMEDIATE.
    Returns (is_admitted, status_or_reason).
    If an existing idempotency key exists:
      - same request_hash: returns (True, "ALREADY_EXISTS")
      - different request_hash: raises AdmissionRejectedError("idempotency_hash_mismatch")
    """
    # 1. Idempotency Check
    cur = conn.execute(
        "SELECT id, request_hash, status FROM agent_runs WHERE idempotency_key = ?;",
        (idempotency_key,)
    )
    existing = cur.fetchone()
    if existing:
        if existing["request_hash"] != request_hash:
            raise AdmissionRejectedError("idempotency_hash_mismatch", retry_after_seconds=0)
        return True, "ALREADY_EXISTS"

    # 2. Global active run limit check (QUEUED + RUNNING)
    cur = conn.execute(
        "SELECT COUNT(*) AS total_active FROM agent_runs WHERE status IN ('QUEUED', 'RUNNING');"
    )
    total_active = cur.fetchone()["total_active"]
    if total_active >= MAX_ACTIVE_RUNS_TOTAL:
        raise AdmissionRejectedError("queue_capacity_full", retry_after_seconds=5)

    # 3. Per-agent active run limit check
    cur = conn.execute(
        "SELECT COUNT(*) AS agent_active FROM agent_runs WHERE agent_id = ? AND status IN ('QUEUED', 'RUNNING');",
        (agent_id,)
    )
    agent_active = cur.fetchone()["agent_active"]
    if agent_active >= MAX_ACTIVE_RUNS_PER_AGENT:
        raise AdmissionRejectedError("agent_concurrency_limit_exceeded", retry_after_seconds=5)

    # 4. Insert admitted QUEUED run atomically
    now_iso = str(now)
    conn.execute(
        """
        INSERT INTO agent_runs (
            id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?);
        """,
        (run_id, tenant_id, agent_id, idempotency_key, request_hash, now, now_iso)
    )

    return True, "ADMITTED"


def check_and_admit_cart_reservation(
    conn: sqlite3.Connection,
    tenant_id: str,
    budget_day: str
) -> None:
    """
    Checks global active reservation ceiling before a purchase can be created or advance.
    Executed under BEGIN IMMEDIATE.
    """
    cur = conn.execute(
        """
        SELECT 
            COUNT(CASE WHEN status IN (
                'PENDING_APPROVAL', 'AUTO_APPROVED', 'APPROVED', 
                'PAYPAL_ORDER_CREATED', 'BUYER_APPROVED', 'CAPTURING', 'CAPTURE_UNKNOWN'
            ) THEN 1 END) AS active_reservations,
            COUNT(CASE WHEN status = 'PENDING_APPROVAL' THEN 1 END) AS pending_approvals
        FROM purchases
        WHERE tenant_id = ? AND budget_day = ?;
        """,
        (tenant_id, budget_day)
    )
    row = cur.fetchone()
    if row["active_reservations"] >= MAX_ACTIVE_RESERVATIONS:
        raise AdmissionRejectedError("max_active_reservations_exceeded", retry_after_seconds=5)

    if row["pending_approvals"] >= MAX_PENDING_APPROVALS:
        raise AdmissionRejectedError("max_pending_approvals_exceeded", retry_after_seconds=10)

