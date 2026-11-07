"""
Unit Tests for Queue Admission, Atomic Deadline Claims, and Crash Recovery.
Tests Invariant 4.
"""

import os
import tempfile
import time
import unittest

from backend.app.db.connection import create_connection, init_db, transactional_session
from backend.app.orchestrator.admission import (
    check_and_admit_run,
    check_and_admit_cart_reservation,
    AdmissionRejectedError,
    MAX_ACTIVE_RUNS_TOTAL,
    MAX_ACTIVE_RUNS_PER_AGENT,
)
from backend.app.orchestrator.queue import (
    WorkerPoolManager,
    QUEUE_TIMEOUT_SECONDS,
)


class TestQueueAdmission(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        init_db(self.db_path)
        self.conn = create_connection(self.db_path)

        # Seed minimal tenant and agents
        self.conn.execute("INSERT INTO agents (id, tenant_id, name, created_at) VALUES ('agent_1', 't1', 'Agent 1', '2026-11-06T00:00:00Z');")
        self.conn.execute("INSERT INTO agents (id, tenant_id, name, created_at) VALUES ('agent_2', 't1', 'Agent 2', '2026-11-06T00:00:00Z');")

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_per_agent_active_run_concurrency_cap(self):
        """No more than 2 active runs permitted per agent."""
        now = time.time()
        with transactional_session(self.conn):
            # Run 1: OK
            check_and_admit_run(self.conn, "t1", "agent_1", "k1", "h1", "r1", now)
            # Run 2: OK
            check_and_admit_run(self.conn, "t1", "agent_1", "k2", "h2", "r2", now)

        # Run 3 for agent_1: Must raise AdmissionRejectedError
        with self.assertRaises(AdmissionRejectedError) as ctx:
            with transactional_session(self.conn):
                check_and_admit_run(self.conn, "t1", "agent_1", "k3", "h3", "r3", now)
        self.assertEqual(ctx.exception.reason, "agent_concurrency_limit_exceeded")

        # But agent_2 can still be admitted
        with transactional_session(self.conn):
            admitted, status = check_and_admit_run(self.conn, "t1", "agent_2", "k4", "h4", "r4", now)
            self.assertTrue(admitted)
            self.assertEqual(status, "ADMITTED")

    def test_global_capacity_admission_limit(self):
        """9th active run is rejected before inserting a database row."""
        now = time.time()
        # Seed 8 active runs across multiple agents
        with transactional_session(self.conn):
            for i in range(MAX_ACTIVE_RUNS_TOTAL):
                ag_id = f"ag_{i}"
                self.conn.execute(f"INSERT OR IGNORE INTO agents (id, tenant_id, name, created_at) VALUES ('{ag_id}', 't1', 'A', '2026');")
                check_and_admit_run(self.conn, "t1", ag_id, f"key_{i}", f"h_{i}", f"run_{i}", now)

        # 9th run attempt must fail with queue_capacity_full
        with self.assertRaises(AdmissionRejectedError) as ctx:
            with transactional_session(self.conn):
                check_and_admit_run(self.conn, "t1", "ag_new", "key_9", "h_9", "run_9", now)
        self.assertEqual(ctx.exception.reason, "queue_capacity_full")

        # Prove zero database rows were inserted for run_9!
        cur = self.conn.execute("SELECT COUNT(*) AS c FROM agent_runs WHERE id = 'run_9';")
        self.assertEqual(cur.fetchone()["c"], 0)

    def test_atomic_queue_deadline_claim_expired(self):
        """Worker executing deadline claim transitions expired job to FAILED(queue_timeout) without invoking processor."""
        # Insert a run that was queued 25 seconds ago (deadline is 20s)
        old_time = time.time() - 25.0
        with transactional_session(self.conn):
            self.conn.execute(
                """
                INSERT INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at)
                VALUES ('run_stale', 't1', 'agent_1', 'k_stale', 'h_stale', ?, 'QUEUED', '2026');
                """,
                (old_time,)
            )

        processor_invoked = False
        def fake_processor(run_id, worker_id):
            nonlocal processor_invoked
            processor_invoked = True

        manager = WorkerPoolManager(self.db_path, run_processor=fake_processor)
        # Execute single run processing directly
        manager._process_single_run("run_stale", "worker_test")

        self.assertFalse(processor_invoked)
        cur = self.conn.execute("SELECT status, status_reason FROM agent_runs WHERE id = 'run_stale';")
        row = cur.fetchone()
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["status_reason"], "queue_timeout")

    def test_startup_crash_recovery(self):
        """Startup boot sweep recovers crashed RUNNING runs and orphaned CAPTURING purchases."""
        with transactional_session(self.conn):
            # Lingering RUNNING run
            self.conn.execute(
                """
                INSERT INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at)
                VALUES ('run_crashed', 't1', 'agent_1', 'k_crashed', 'h_crashed', 1000.0, 'RUNNING', '2026');
                """
            )
            # Orphaned CAPTURING purchase
            self.conn.execute(
                """
                INSERT INTO purchases (id, tenant_id, agent_id, run_id, status, auto_approved, cart_json, cart_hash,
                                      total_cents, merchant_ids_json, budget_day, create_request_id, capture_boot_id,
                                      state_entered_at, created_at, updated_at)
                VALUES ('p_crashed', 't1', 'agent_1', 'run_crashed', 'CAPTURING', 1, '{}', 'h1', 4500, '[]', '2026-11-06',
                        'req_crashed', 'old_boot_id_999', 1000.0, '2026', '2026');
                """
            )

        manager = WorkerPoolManager(self.db_path)
        with transactional_session(self.conn):
            manager._record_boot_and_recover(self.conn)

        # Assert RUNNING run flipped to FAILED(interrupted)
        cur = self.conn.execute("SELECT status, status_reason FROM agent_runs WHERE id = 'run_crashed';")
        r_row = cur.fetchone()
        self.assertEqual(r_row["status"], "FAILED")
        self.assertEqual(r_row["status_reason"], "interrupted")

        # Assert CAPTURING purchase flipped to CAPTURE_UNKNOWN(interrupted)
        cur = self.conn.execute("SELECT status, status_reason FROM purchases WHERE id = 'p_crashed';")
        p_row = cur.fetchone()
        self.assertEqual(p_row["status"], "CAPTURE_UNKNOWN")
        self.assertEqual(p_row["status_reason"], "interrupted")


if __name__ == "__main__":
    unittest.main()

