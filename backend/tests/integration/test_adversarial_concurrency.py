"""
Adversarial Concurrency & Stress Integration Tests.
Enforces:
- Invariant 3: Preserve Financial Reservations and Budget Invariants (Never breach budget under race).
- Invariant 4: Atomic Admission and Queue Claims (Exactly-once claim under concurrency).
- Invariant 5: Exclusive Ownership and Concurrency Fencing (Single lease acquisition).
"""

import concurrent.futures
import os
import sqlite3
import tempfile
import time
import unittest

from backend.app.db.connection import (
    get_thread_connection,
    close_thread_connection,
    transactional_session,
)
from backend.app.db.financial_journal import (
    record_financial_fact,
    get_budget_snapshot,
    rebuild_budget_accounting,
    FactType,
)
from backend.app.orchestrator.capture_lifecycle import (
    execute_capture_flow,
)
from backend.app.paypal.client import MockPayPalClient
from backend.app.orchestrator.queue import (
    WorkerPoolManager,
)
from scripts.seed_db import seed_database


class TestAdversarialConcurrency(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.db_path = self.temp_db.name
        seed_database(self.db_path)

    def tearDown(self):
        close_thread_connection()
        if os.path.exists(self.db_path):
            try:
                os.unlink(self.db_path)
            except OSError:
                pass

    def test_concurrent_budget_reservation_race_never_exceeds_budget(self):
        """
        Adversarial Test: 20 threads simultaneously attempt to reserve $10 against a $50 budget.
        Invariant 3: Committed spend MUST NOT exceed $50 (5000 cents) under any concurrency.
        """
        daily_cap_cents = 5000
        reservation_amount_cents = 1000
        budget_day = "2026-11-06"
        num_threads = 20

        # Pre-seed runs and purchases for the workers
        conn = get_thread_connection(self.db_path)
        now = time.time()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        with transactional_session(conn):
            for i in range(num_threads):
                conn.execute(
                    """
                    INSERT INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at)
                    VALUES (?, 'tenant_demo', 'default', ?, ?, ?, 'COMPLETED', ?);
                    """,
                    (f"run_race_{i}", f"k_race_{i}", f"h_race_{i}", now, now_iso)
                )
                conn.execute(
                    """
                    INSERT INTO purchases (
                        id, tenant_id, agent_id, run_id, status, auto_approved, total_cents, currency,
                        cart_json, cart_hash, merchant_ids_json, budget_day, create_request_id, state_entered_at,
                        created_at, updated_at
                    ) VALUES (
                        ?, 'tenant_demo', 'default', ?, 'CREATED', 1, 1000, 'USD',
                        '{}', 'h_test', '[]', ?, ?, ?, ?, ?
                    );
                    """,
                    (f"pur_race_{i}", f"run_race_{i}", budget_day, f"req_race_{i}", now, now_iso, now_iso)
                )

        successful_reservations = []
        rejected_reservations = []

        def worker_task(worker_idx: int):
            # Each worker thread gets its own thread-local SQLite connection
            c = get_thread_connection(self.db_path)
            fact_id = f"fact_race_{worker_idx}"
            purchase_id = f"pur_race_{worker_idx}"

            try:
                with transactional_session(c):
                    # Check current committed spend inside transaction
                    b = get_budget_snapshot(c, "default", budget_day)
                    current_committed = b.r3_committed_cents if b else 0
                    
                    if current_committed + reservation_amount_cents > daily_cap_cents:
                        # Exceeds budget -> reject
                        return False
                    
                    record_financial_fact(
                        conn=c,
                        source_event_key=f"event_{fact_id}",
                        tenant_id="tenant_demo",
                        agent_id="default",
                        purchase_id=purchase_id,
                        budget_day=budget_day,
                        currency="USD",
                        fact_type=FactType.RESERVATION_CREATED,
                        delta_captured_cents=0,
                        delta_reserved_cents=reservation_amount_cents,
                        delta_unresolved_cents=0,
                        delta_auto_approved_cents=reservation_amount_cents,
                        actor="worker_race"
                    )
                    return True
            finally:
                close_thread_connection()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker_task, i) for i in range(num_threads)]
            for fut in concurrent.futures.as_completed(futures):
                res = fut.result()
                if res:
                    successful_reservations.append(res)
                else:
                    rejected_reservations.append(res)

        # Inspect final financial accounting from main thread
        conn = get_thread_connection(self.db_path)
        b_final = get_budget_snapshot(conn, "default", budget_day)
        total_committed = b_final.r3_committed_cents

        # Exactly 5 should succeed (5 * 1000 = 5000 cents), 15 must be rejected
        self.assertEqual(len(successful_reservations), 5)
        self.assertEqual(len(rejected_reservations), 15)
        self.assertEqual(total_committed, 5000)
        self.assertLessEqual(total_committed, daily_cap_cents)

        # Invariant 2: Rebuilt budget accounting from immutable facts MUST match row-for-row
        rebuilt = rebuild_budget_accounting(conn, "default", budget_day)
        self.assertEqual(rebuilt.reserved_cents, b_final.reserved_cents)
        self.assertEqual(rebuilt.captured_cents, b_final.captured_cents)
        self.assertEqual(rebuilt.r3_committed_cents, b_final.r3_committed_cents)

    def test_concurrent_queue_claim_race_has_strictly_one_winner(self):
        """
        Adversarial Test: 10 worker threads simultaneously attempt to claim a single queued job.
        Invariant 4: Exactly one worker must claim the job; 9 must fail to claim.
        """
        conn = get_thread_connection(self.db_path)

        # Insert a single queued run
        now = time.time()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        with transactional_session(conn):
            conn.execute(
                """
                INSERT INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at)
                VALUES ('run_contest_1', 'tenant_demo', 'default', 'k_contest_1', 'hash1', :now, 'QUEUED', :now_iso)
                """,
                {"now": now, "now_iso": now_iso}
            )

        processor_invocations = []

        def tracking_processor(run_id: str, worker_id: str):
            processor_invocations.append((run_id, worker_id))

        manager = WorkerPoolManager(self.db_path, run_processor=tracking_processor)
        num_workers = 10

        def claimer_task(worker_id: str):
            manager._process_single_run("run_contest_1", worker_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(claimer_task, f"worker_{i}") for i in range(num_workers)]
            concurrent.futures.wait(futures)

        # Exactly 1 worker must have claimed and executed the processor
        self.assertEqual(len(processor_invocations), 1)
        self.assertEqual(processor_invocations[0][0], "run_contest_1")
        self.assertTrue(processor_invocations[0][1].startswith("worker_"))

        # Database must record status as RUNNING with winner's worker ID
        cur = conn.execute("SELECT status, worker_id FROM agent_runs WHERE id = 'run_contest_1'")
        row = cur.fetchone()
        self.assertEqual(row["status"], "RUNNING")
        self.assertEqual(row["worker_id"], processor_invocations[0][1])

    def test_concurrent_exclusive_capture_lease_race(self):
        """
        Adversarial Test: 10 worker threads race to execute capture on the same purchase.
        Invariant 5: Exactly one thread claims and executes the capture; 9 lose the claim.
        """
        conn = get_thread_connection(self.db_path)
        mock_paypal = MockPayPalClient()

        # Create valid mock order
        out = mock_paypal.create_order("req_race_1", 3500, [], "USD")
        order_id = out.data["id"]
        mock_paypal.set_buyer_approved(order_id)

        now = time.time()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

        # Insert agent run and purchase in BUYER_APPROVED state first
        with transactional_session(conn):
            conn.execute(
                """
                INSERT INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at)
                VALUES ('run_lease_race', 'tenant_demo', 'default', 'k_lease_race', 'h_lease_race', :now, 'COMPLETED', :now_iso);
                """,
                {"now": now, "now_iso": now_iso}
            )
            conn.execute(
                """
                INSERT INTO purchases (
                    id, tenant_id, agent_id, run_id, status, auto_approved, total_cents, currency,
                    cart_json, cart_hash, merchant_ids_json, budget_day, paypal_order_id,
                    create_request_id, state_entered_at, created_at, updated_at
                ) VALUES (
                    'pur_lease_race', 'tenant_demo', 'default', 'run_lease_race', 'BUYER_APPROVED', 1, 3500, 'USD',
                    '{}', 'h1', '[]', '2026-11-06', :order_id,
                    'req_race_1', :now, :now_iso, :now_iso
                )
                """,
                {"now": now, "now_iso": now_iso, "order_id": order_id}
            )

        # Seed initial reservation fact for purchase to maintain invariant 3
        with transactional_session(conn):
            record_financial_fact(
                conn=conn,
                source_event_key="init_res_race",
                tenant_id="tenant_demo",
                agent_id="default",
                purchase_id="pur_lease_race",
                budget_day="2026-11-06",
                fact_type=FactType.RESERVATION_CREATED,
                delta_captured_cents=0,
                delta_reserved_cents=3500,
                delta_unresolved_cents=0,
                delta_auto_approved_cents=3500,
                actor="test"
            )

        execution_results = []
        num_threads = 10

        def capture_task(worker_idx: int):
            c = get_thread_connection(self.db_path)
            try:
                res = execute_capture_flow(
                    conn=c,
                    purchase_id="pur_lease_race",
                    paypal_client=mock_paypal,
                    boot_id=f"boot_{worker_idx}"
                )
                return res
            finally:
                close_thread_connection()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(capture_task, i) for i in range(num_threads)]
            for fut in concurrent.futures.as_completed(futures):
                res = fut.result()
                execution_results.append(res)

        # Exactly 1 caller should capture the purchase; 9 callers must NOT capture
        captured_outcomes = [r for r in execution_results if r[0] == "CAPTURED" and r[1] != "not_in_buyer_approved_state"]
        non_capturing_outcomes = [r for r in execution_results if r[0] != "CAPTURED" or r[1] == "not_in_buyer_approved_state"]

        self.assertEqual(len(captured_outcomes), 1)
        self.assertEqual(len(non_capturing_outcomes), 9)

        # Invariant 2: Database purchase status is CAPTURED
        cur = conn.execute("SELECT status FROM purchases WHERE id = 'pur_lease_race'")
        row = cur.fetchone()
        self.assertEqual(row["status"], "CAPTURED")


if __name__ == "__main__":
    unittest.main()
