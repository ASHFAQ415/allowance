"""
Bounded Task Queue, Dispatcher, and Atomic Worker Claim Lifecycle.
Enforces Invariant 4:
- Enforce the queue deadline inside the atomic QUEUED-to-RUNNING claim.
- Predicate: :now < queued_at + :queue_timeout_seconds
- Never claim a job first and check its deadline afterward.
- Expired jobs transition to queue_timeout without invoking LLM or payment provider.
- Startup and crash-recovery sweeps restore dispatch consistency.
"""

import queue
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .state_machine import PurchaseState


QUEUE_TIMEOUT_SECONDS = 20.0
MAX_WORKERS = 4
MAX_WAITING_QUEUE = 4


class WorkerPoolManager:
    def __init__(self, db_path: str, run_processor: Optional[Callable[[str, str], None]] = None):
        self.db_path = db_path
        self.run_processor = run_processor
        self.dispatch_queue: queue.Queue = queue.Queue(maxsize=MAX_WAITING_QUEUE)
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="allowance_worker")
        self.boot_id = str(uuid.uuid4())
        self.shutting_down = False
        self._dispatcher_thread: Optional[threading.Thread] = None

    def start(self, conn: sqlite3.Connection):
        """Initializes boot metadata, executes startup crash recovery, and starts worker pool."""
        self.shutting_down = False
        self._record_boot_and_recover(conn)
        self._start_workers()

    def shutdown(self, timeout: float = 30.0):
        """Graceful shutdown draining in-flight runs."""
        self.shutting_down = True
        self.executor.shutdown(wait=True, cancel_futures=False)

    def _record_boot_and_recover(self, conn: sqlite3.Connection):
        """
        Startup crash recovery:
        1. Records fresh boot_id in meta table.
        2. Lingering RUNNING runs -> FAILED(interrupted).
        3. Stale QUEUED runs -> FAILED(queue_timeout); active unexpired QUEUED runs re-enqueued.
        4. Orphaned CAPTURING purchases from prior boots -> CAPTURE_UNKNOWN(interrupted).
        """
        now = time.time()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

        conn.execute("INSERT OR REPLACE INTO meta (key, value, updated_at) VALUES ('boot_id', ?, ?);", (self.boot_id, now_iso))

        # 2. Lingering RUNNING runs from prior process crash -> FAILED(interrupted)
        conn.execute(
            """
            UPDATE agent_runs
            SET status = 'FAILED', status_reason = 'interrupted'
            WHERE status = 'RUNNING';
            """
        )

        # 3. Handle QUEUED runs
        deadline_threshold = now - QUEUE_TIMEOUT_SECONDS
        conn.execute(
            """
            UPDATE agent_runs
            SET status = 'FAILED', status_reason = 'queue_timeout'
            WHERE status = 'QUEUED' AND queued_at < ?;
            """,
            (deadline_threshold,)
        )

        # Re-dispatch valid unexpired QUEUED runs
        cur = conn.execute("SELECT id FROM agent_runs WHERE status = 'QUEUED' AND queued_at >= ?;", (deadline_threshold,))
        for row in cur.fetchall():
            try:
                self.dispatch_queue.put_nowait(row["id"])
            except queue.Full:
                break

        # 4. CAPTURING purchases from prior boots -> CAPTURE_UNKNOWN(interrupted)
        conn.execute(
            """
            UPDATE purchases
            SET status = 'CAPTURE_UNKNOWN', status_reason = 'interrupted', updated_at = ?
            WHERE status = 'CAPTURING' AND (capture_boot_id IS NULL OR capture_boot_id != ?);
            """,
            (now_iso, self.boot_id)
        )

    def _start_workers(self):
        """Launches continuous worker loop in thread pool."""
        for i in range(MAX_WORKERS):
            self.executor.submit(self._worker_loop, f"worker-{i+1}")

    def enqueue_run(self, run_id: str) -> bool:
        """Enqueues admitted run ID into in-memory queue."""
        if self.shutting_down:
            return False
        try:
            self.dispatch_queue.put_nowait(run_id)
            return True
        except queue.Full:
            return False

    def _worker_loop(self, worker_id: str):
        """Worker thread main loop claiming and executing tasks."""
        while not self.shutting_down:
            try:
                run_id = self.dispatch_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            self._process_single_run(run_id, worker_id)
            self.dispatch_queue.task_done()

    def _process_single_run(self, run_id: str, worker_id: str):
        """
        Executes atomic deadline claim:
        Enforces :now < queued_at + :queue_timeout_seconds inside SQL claim.
        """
        now = time.time()
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row

        try:
            conn.execute("BEGIN IMMEDIATE;")
            
            # Atomic QUEUED -> RUNNING with deadline predicate
            cur = conn.execute(
                """
                UPDATE agent_runs
                SET status = 'RUNNING', started_at = ?, worker_id = ?
                WHERE id = ? AND status = 'QUEUED' AND ? < queued_at + ?;
                """,
                (now, worker_id, run_id, now, QUEUE_TIMEOUT_SECONDS)
            )

            if cur.rowcount == 1:
                # Successfully claimed! Commit claim and invoke processor
                conn.execute("COMMIT;")
                if self.run_processor:
                    self.run_processor(run_id, worker_id)
            else:
                # Expired or already claimed. Check if expired and mark FAILED(queue_timeout)
                conn.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'FAILED', status_reason = 'queue_timeout'
                    WHERE id = ? AND status = 'QUEUED' AND ? >= queued_at + ?;
                    """,
                    (run_id, now, QUEUE_TIMEOUT_SECONDS)
                )
                conn.execute("COMMIT;")
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
        finally:
            conn.close()

