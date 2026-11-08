"""
Application runtime wiring.

Owns the single worker pool for this process and the run processor that turns a
claimed agent run into a policy-evaluated cart. Started and stopped by the
FastAPI lifespan; nothing here touches the network except the (mockable) PayPal
client used by the specific workflow that asks for it.
"""

import json
import time
from typing import Optional

from .agent.loop import execute_agent_loop
from .db.bootstrap import seed_defaults
from .db.connection import DEFAULT_DB_PATH, create_connection, init_db, transactional_session
from .orchestrator.core import submit_cart_to_orchestrator
from .orchestrator.queue import WorkerPoolManager


_worker_pool: Optional[WorkerPoolManager] = None
_db_path: str = DEFAULT_DB_PATH


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _record_event(conn, tenant_id: str, event_type: str, run_id: str, payload: dict) -> None:
    conn.execute(
        """
        INSERT INTO events (tenant_id, event_type, run_id, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?);
        """,
        (tenant_id, event_type, run_id, json.dumps(payload), _now_iso()),
    )


def _finish_run(conn, run_id: str, status: str, reason: Optional[str], trace: list) -> bool:
    """CAS-updates a still-RUNNING run. Returns False if the run was taken over."""
    with transactional_session(conn):
        cur = conn.execute(
            """
            UPDATE agent_runs
            SET status = ?, status_reason = ?, trace_json = ?
            WHERE id = ? AND status = 'RUNNING';
            """,
            (status, reason, json.dumps(trace, default=str), run_id),
        )
        return cur.rowcount == 1


def _fail_run(conn, run_id: str, tenant_id: str, reason: str, trace: list) -> None:
    if _finish_run(conn, run_id, "FAILED", reason, trace):
        with transactional_session(conn):
            _record_event(conn, tenant_id, "RUN_FAILED", run_id, {"reason": reason})


def process_agent_run(run_id: str, worker_id: str) -> None:
    """
    Worker processor: claimed run -> bounded agent loop -> P1 admission.

    Runs in a worker thread. Uses its own short-lived connection so worker-thread
    identity and database path never collide across tests or restarts.
    """
    conn = create_connection(_db_path)
    try:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?;", (run_id,)).fetchone()
        if not row or row["status"] != "RUNNING":
            # Stale dispatch or a run already recovered by the startup sweep.
            return

        tenant_id = row["tenant_id"]
        agent_id = row["agent_id"]
        prompt = row["user_prompt"] or ""

        with transactional_session(conn):
            _record_event(conn, tenant_id, "RUN_STARTED", run_id, {"worker_id": worker_id})

        try:
            status, reason, cart, trace = execute_agent_loop(prompt)
        except Exception as exc:  # noqa: BLE001 - agent failures must not kill the worker
            _fail_run(conn, run_id, tenant_id, "agent_error", [{"event": "agent_error", "error": str(exc)}])
            return

        if status != "COMPLETED" or cart is None:
            _fail_run(conn, run_id, tenant_id, reason or "agent_failed", trace)
            return

        try:
            purchase_id, target_state, decision = submit_cart_to_orchestrator(
                conn=conn,
                tenant_id=tenant_id,
                agent_id=agent_id,
                run_id=run_id,
                cart=cart,
                now=time.time(),
            )
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            trace = list(trace or []) + [{"event": "orchestrator_error", "error": str(exc)}]
            _fail_run(conn, run_id, tenant_id, "orchestrator_error", trace)
            return

        if not _finish_run(conn, run_id, "COMPLETED", None, trace):
            return

        with transactional_session(conn):
            _record_event(conn, tenant_id, "RUN_COMPLETED", run_id, {
                "purchase_id": purchase_id,
                "status": target_state.value,
                "rule": decision.rule_name,
                "outcome": decision.outcome.value,
            })
    finally:
        conn.close()


def get_worker_pool() -> Optional[WorkerPoolManager]:
    return _worker_pool


def start_runtime(db_path: Optional[str] = None) -> WorkerPoolManager:
    """Initializes the schema, seeds defaults on a fresh database, starts workers."""
    global _worker_pool, _db_path

    if _worker_pool is not None:
        return _worker_pool

    _db_path = db_path or DEFAULT_DB_PATH
    init_db(_db_path)

    boot_conn = create_connection(_db_path)
    try:
        has_policy = boot_conn.execute("SELECT COUNT(*) AS c FROM policy;").fetchone()["c"] > 0
        if not has_policy:
            seed_defaults(boot_conn)

        pool = WorkerPoolManager(_db_path, run_processor=process_agent_run)
        pool.start(boot_conn)
        _worker_pool = pool
        return pool
    finally:
        boot_conn.close()


def stop_runtime() -> None:
    global _worker_pool
    pool, _worker_pool = _worker_pool, None
    if pool is not None:
        pool.shutdown()
