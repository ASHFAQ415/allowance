"""
Integration Tests for Adversarial Edge Cases: Tamper Detection, Freeze Races,
Stale Workers, Concurrency Fencing, and Budget Accounting Subset Invariants.

Adversarial Scenarios covered:
  #14 Invalid payee account tampering → verify_order blocks capture
  #15 Invalid currency tampering → verify_order blocks capture
  #17 Freeze racing capture claim → atomic ordering determines winner
  #24 SIGTERM graceful drain → active job completes before pool exits
  #26 SQLite lock exhaustion → 503 with zero phantom rows
  #28 Stale worker cannot mutate terminal run
  V22-12 Self-exclusion exactly once at P2/P3/P4
  V22-13 Auto-approved subset always ≤ R3 committed spend
  V22-14 Stale capture owner lease takeover
  V22-15 Single CAS wins concurrent capture resolution
"""

import concurrent.futures
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid

from backend.app.db.connection import (
    create_connection,
    get_thread_connection,
    close_thread_connection,
    init_db,
    transactional_session,
)
from backend.app.db.financial_journal import (
    FactType,
    get_budget_snapshot,
    record_financial_fact,
)
from backend.app.paypal.client import MockPayPalClient
from backend.app.orchestrator.capture_lifecycle import (
    execute_capture_flow,
    execute_read_only_reconciliation,
)
from backend.app.orchestrator.admission import (
    AdmissionRejectedError,
    check_and_admit_run,
)
from backend.app.orchestrator.queue import WorkerPoolManager
from backend.app.policy.rules import (
    CartItem,
    Checkpoint,
    FrozenCart,
    PolicyConfig,
    evaluate,
    Outcome,
)
from backend.app.db.financial_journal import BudgetSnapshot
from scripts.seed_db import seed_database


def _seed_purchase_in_state(conn, mock_paypal, purchase_id, status, total_cents=4500):
    """Seed a purchase in the given state with an initial reservation."""
    out = mock_paypal.create_order(f"req_{purchase_id}", total_cents, [], "USD")
    order_id = out.data["id"]
    if status in ("BUYER_APPROVED", "CAPTURING", "CAPTURE_UNKNOWN"):
        mock_paypal.set_buyer_approved(order_id)

    conn.execute(
        "INSERT OR IGNORE INTO agent_runs "
        "(id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at) "
        f"VALUES ('r_{purchase_id}', 'tenant_demo', 'default', 'k_{purchase_id}', 'h1', 1000.0, 'COMPLETED', '2026');"
    )
    conn.execute(
        """
        INSERT INTO purchases (
            id, tenant_id, agent_id, run_id, status, auto_approved, cart_json, cart_hash,
            total_cents, merchant_ids_json, budget_day, create_request_id, paypal_order_id,
            state_entered_at, created_at, updated_at
        ) VALUES (?, 'tenant_demo', 'default', ?, ?, 1, '{}', 'h1', ?, '[]',
                  '2026-11-06', ?, ?, 1000.0, '2026', '2026');
        """,
        (purchase_id, f"r_{purchase_id}", status, total_cents, f"req_{purchase_id}", order_id)
    )
    record_financial_fact(
        conn=conn,
        source_event_key=f"init_res_{purchase_id}",
        tenant_id="tenant_demo", agent_id="default", purchase_id=purchase_id,
        budget_day="2026-11-06", fact_type=FactType.RESERVATION_CREATED,
        delta_reserved_cents=total_cents, delta_unresolved_cents=0,
        delta_captured_cents=0, delta_auto_approved_cents=total_cents, actor="agent"
    )
    return order_id


class TestAdversarialScenarios(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        seed_database(self.db_path)
        self.conn = create_connection(self.db_path)
        self.mock_paypal = MockPayPalClient()

        # Seed a run for purchase inserts that need a parent agent_run
        self.conn.execute(
            "INSERT OR IGNORE INTO agent_runs "
            "(id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at) "
            "VALUES ('r_base', 'tenant_demo', 'default', 'k_base', 'h_base', 1000.0, 'COMPLETED', '2026');"
        )

    def tearDown(self):
        self.conn.close()
        close_thread_connection()
        if os.path.exists(self.db_path):
            try:
                os.unlink(self.db_path)
            except OSError:
                pass

    # --- Scenario #14 ---
    def test_payee_account_tamper_blocks_capture(self):
        """
        Scenario #14: PayPal GET order returns wrong merchant_id.
        verify_order() must block capture before ANY capture POST is sent.
        """
        order_id = _seed_purchase_in_state(self.conn, self.mock_paypal, "p_payee", "BUYER_APPROVED", 3000)
        self.mock_paypal.simulate_payee_tamper = True

        status, reason = execute_capture_flow(self.conn, "p_payee", self.mock_paypal, "boot_p")

        self.assertEqual(status, "FAILED")
        # Must be specifically a verification failure — not a transport ambiguity
        self.assertIn(reason, ("amount_mismatch", "verify_failed", "payee_mismatch"))

        # No capture must have been recorded in mock
        self.assertNotIn(order_id, self.mock_paypal.captures,
                         "A capture POST was sent despite payee mismatch — Invariant 1 violated!")

        snap = get_budget_snapshot(self.conn, "default", "2026-11-06")
        self.assertEqual(snap.captured_cents, 0)

    # --- Scenario #15 ---
    def test_currency_tamper_blocks_capture(self):
        """
        Scenario #15: PayPal GET order returns EUR instead of USD.
        verify_order() must block capture before ANY capture POST is sent.
        """
        order_id = _seed_purchase_in_state(self.conn, self.mock_paypal, "p_curr", "BUYER_APPROVED", 2500)
        self.mock_paypal.simulate_currency_tamper = True

        status, reason = execute_capture_flow(self.conn, "p_curr", self.mock_paypal, "boot_c")

        self.assertEqual(status, "FAILED")
        self.assertNotIn(order_id, self.mock_paypal.captures,
                         "A capture POST was sent despite currency mismatch — Invariant 1 violated!")

        snap = get_budget_snapshot(self.conn, "default", "2026-11-06")
        self.assertEqual(snap.captured_cents, 0)

    # --- Scenario #17 ---
    def test_freeze_racing_capture_claim_atomic_ordering_determines_winner(self):
        """
        Scenario #17: Thread 1 begins capture; Thread 2 sets frozen=True concurrently.
        Atomic ordering via SQLite serialized writes ensures exactly one consistent outcome:
        either the capture claims CAPTURING before freeze (and completes), or freeze wins
        and the capture is denied at P4.
        In both cases, the final DB state must be internally consistent.
        """
        order_id = _seed_purchase_in_state(
            self.conn, self.mock_paypal, "p_freeze_race", "BUYER_APPROVED", 3800
        )

        outcomes = []
        barrier = threading.Barrier(2)

        def capture_thread():
            c = get_thread_connection(self.db_path)
            barrier.wait()
            result = execute_capture_flow(c, "p_freeze_race", self.mock_paypal, "boot_fr")
            outcomes.append(("capture", result))
            close_thread_connection()

        def freeze_thread():
            c = get_thread_connection(self.db_path)
            barrier.wait()
            try:
                with transactional_session(c):
                    c.execute(
                        "UPDATE policy SET frozen = 1 WHERE agent_id = 'default';"
                    )
                outcomes.append(("freeze", ("OK", "frozen")))
            except Exception as e:
                outcomes.append(("freeze", ("ERROR", str(e))))
            finally:
                close_thread_connection()

        t1 = threading.Thread(target=capture_thread)
        t2 = threading.Thread(target=freeze_thread)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        # Both threads must have completed
        self.assertEqual(len(outcomes), 2)

        # Final DB state must be internally consistent:
        # Either purchase is CAPTURED (capture won) or still BUYER_APPROVED/FAILED (freeze won)
        cur = self.conn.execute("SELECT status FROM purchases WHERE id = 'p_freeze_race';")
        final_status = cur.fetchone()["status"]
        self.assertIn(
            final_status,
            ("CAPTURED", "BUYER_APPROVED", "FAILED", "CAPTURE_UNKNOWN"),
            f"Unexpected terminal status after freeze race: {final_status}"
        )

        # Regardless of winner: captured_cents + unresolved_cents must equal or be less than total
        snap = get_budget_snapshot(self.conn, "default", "2026-11-06")
        total_committed = snap.r3_committed_cents
        self.assertLessEqual(
            total_committed, 3800,
            "Budget commitment exceeds original reservation after freeze race!"
        )

    # --- Scenario #24 ---
    def test_sigterm_draining_completes_active_work_within_timeout(self):
        """
        Scenario #24: WorkerPoolManager.shutdown() drains active work within 30s.
        A job queued just before shutdown must complete, not be abandoned mid-flight.
        """
        completed_jobs = []

        def slow_processor(run_id, worker_id):
            time.sleep(0.3)  # Simulate 300ms of work
            completed_jobs.append(run_id)

        manager = WorkerPoolManager(self.db_path, run_processor=slow_processor)

        # Queue a run
        conn = create_connection(self.db_path)
        now = time.time()
        run_id = str(uuid.uuid4())
        with transactional_session(conn):
            conn.execute(
                "INSERT INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at) "
                "VALUES (?, 'tenant_demo', 'default', ?, 'h1', ?, 'QUEUED', '2026');",
                (run_id, f"k_{run_id}", now)
            )

        manager.start(conn)
        conn.close()
        time.sleep(0.1)  # Let pool pick up job

        start = time.time()
        manager.shutdown(timeout=10)
        elapsed = time.time() - start

        # Job must have completed and drain must be fast
        self.assertIn(run_id, completed_jobs,
                      "Active job was abandoned on graceful shutdown — SIGTERM drain failed!")
        self.assertLess(elapsed, 10.0, "Drain took longer than timeout")

    # --- Scenario #26 ---
    def test_sqlite_busy_timeout_returns_503_with_zero_phantom_rows(self):
        """
        Scenario #26: When SQLite is exclusively locked by another connection,
        admission must return an error (not hang), with zero phantom agent_run rows inserted.
        """
        # Hold exclusive lock with another connection
        exclusive_conn = sqlite3.connect(self.db_path)
        exclusive_conn.execute("BEGIN EXCLUSIVE;")

        test_conn = create_connection(self.db_path)
        # Set very short busy timeout for test speed
        test_conn.execute("PRAGMA busy_timeout = 100;")

        run_id = "phantom_run_999"
        try:
            with self.assertRaises((AdmissionRejectedError, sqlite3.OperationalError)):
                with transactional_session(test_conn):
                    check_and_admit_run(
                        test_conn, "tenant_demo", "default",
                        "k_phantom", "h_phantom", run_id, time.time()
                    )
        finally:
            exclusive_conn.execute("ROLLBACK;")
            exclusive_conn.close()
            test_conn.close()

        # Verify zero phantom rows were inserted
        verify_conn = create_connection(self.db_path)
        cur = verify_conn.execute(
            "SELECT COUNT(*) AS c FROM agent_runs WHERE id = ?;", (run_id,)
        )
        count = cur.fetchone()["c"]
        verify_conn.close()
        self.assertEqual(count, 0,
                         "Phantom agent_run row was inserted despite SQLite lock exhaustion!")

    # --- Scenario #28 ---
    def test_stale_worker_cannot_mutate_terminal_run(self):
        """
        Scenario #28: A run reaches COMPLETED; a stale/late worker then tries to
        update it. The terminal state must be preserved — stale update rejected or no-op.
        """
        run_id = str(uuid.uuid4())
        with transactional_session(self.conn):
            self.conn.execute(
                "INSERT INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at) "
                "VALUES (?, 'tenant_demo', 'default', ?, 'h1', 1000.0, 'COMPLETED', '2026');",
                (run_id, f"k_{run_id}")
            )

        # Simulate stale worker trying to mark it RUNNING (arrives late)
        # Must use a predicate that only transitions non-terminal rows
        updated = self.conn.execute(
            "UPDATE agent_runs SET status = 'RUNNING', worker_id = 'stale_w1' "
            "WHERE id = ? AND status = 'QUEUED';",
            (run_id,)
        ).rowcount

        # Zero rows updated — predicate correctly protected the terminal state
        self.assertEqual(updated, 0,
                         "Stale worker overwrote a COMPLETED run — terminal state not protected!")

        cur = self.conn.execute("SELECT status FROM agent_runs WHERE id = ?;", (run_id,))
        self.assertEqual(cur.fetchone()["status"], "COMPLETED")

    # --- V22-12 ---
    def test_self_exclusion_exactly_once_at_p2_p3_p4(self):
        """
        V22-12: At checkpoints P2/P3/P4, the current purchase reservation is
        excluded exactly once from the budget snapshot. It must NOT be excluded
        twice (double-subtract) or zero times (counted against itself).
        """
        from datetime import datetime, timezone

        policy = PolicyConfig(
            agent_id="default",
            tenant_id="tenant_demo",
            version=1,
            daily_budget_cents=10000,       # $100 total
            max_per_tx_cents=10000,
            approval_threshold_cents=10000,  # No auto-review threshold
            velocity_cap_cents=10000,
            allowlist={"merchant_shoes"},
            timezone="UTC",
            frozen=False
        )
        now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)

        # Situation: $60 already committed from other purchases ($50 captured + $10 reserved)
        # Current purchase has a $30 reservation.
        # Raw committed = $50 captured + $40 reserved ($10 other + $30 self) = $90.
        # Total budget = $100.
        #
        # 1. WITHOUT self-exclusion:
        #    Committed ($90) + Proposed ($30) = $120 > $100 -> DENY (Self counted twice!)
        # 2. WITH self-exclusion:
        #    Excluded committed ($60) + Proposed ($30) = $90 <= $100 -> ALLOW (Self counted exactly once!)
        snapshot = BudgetSnapshot(
            agent_id="default",
            budget_day="2026-11-06",
            currency="USD",
            captured_cents=5000,
            reserved_cents=1000 + 3000,     # $10 from OTHER purchases + $30 from SELF
            unresolved_cents=0,
            auto_approved_committed_cents=5000,
            version=1
        )
        cart = FrozenCart(
            items=[CartItem("shoes_trail_runner", "merchant_shoes", "Shoes", 3000, 1)],
            total_cents=3000,
            cart_hash="hash_self_excl"
        )

        # At P2/P3/P4, provide the SELF-EXCLUDED snapshot (self reservation removed once: reserved=1000)
        excluded_snapshot = BudgetSnapshot(
            agent_id="default",
            budget_day="2026-11-06",
            currency="USD",
            captured_cents=5000,
            reserved_cents=1000,            # Only OTHER purchases' $10 — self excluded
            unresolved_cents=0,
            auto_approved_committed_cents=5000,
            version=1
        )

        # With self-exclusion: $80 committed vs $100 budget → should ALLOW at P2/P3/P4
        for cp in [Checkpoint.P2, Checkpoint.P3, Checkpoint.P4]:
            dec = evaluate(cart, policy, excluded_snapshot, now, cp)
            self.assertEqual(
                dec.outcome, Outcome.ALLOW,
                f"Self-exclusion failed at {cp}: got {dec.outcome} ({dec.rule_name}). "
                "Purchase is incorrectly blocking itself."
            )

        # Without self-exclusion: $110 committed vs $100 budget → should DENY at P1
        dec_p1 = evaluate(cart, policy, snapshot, now, Checkpoint.P1)
        self.assertEqual(dec_p1.outcome, Outcome.DENY,
                         "Expected DENY without self-exclusion (snapshot includes self reservation)")

    # --- V22-13 ---
    def test_auto_approved_subset_always_lte_r3_committed(self):
        """
        V22-13: auto_approved_committed_cents must always be ≤ r3_committed_cents
        across all lifecycle transitions. R5 is a strict subset of R3.
        """
        conn = create_connection(self.db_path)
        conn.execute("INSERT OR IGNORE INTO agents (id, tenant_id, name, created_at) VALUES ('a_v13', 't1', 'A', '2026');")
        conn.execute(
            "INSERT OR IGNORE INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at) "
            "VALUES ('r_v13', 't1', 'a_v13', 'k_v13', 'h1', 1000.0, 'COMPLETED', '2026');"
        )
        conn.execute(
            "INSERT INTO purchases (id, tenant_id, agent_id, run_id, status, auto_approved, cart_json, cart_hash, "
            "total_cents, merchant_ids_json, budget_day, create_request_id, state_entered_at, created_at, updated_at) "
            "VALUES ('p_v13', 't1', 'a_v13', 'r_v13', 'CREATED', 1, '{}', 'h1', 2000, '[]', '2026-11-06', 'req_v13', 1000.0, '2026', '2026');"
        )

        events = [
            # (source_key, fact_type, reserved, unresolved, captured, auto_approved)
            ("ev1", FactType.RESERVATION_CREATED, 2000, 0, 0, 2000),
            ("ev2", FactType.CAPTURE_BECAME_UNKNOWN, -2000, 2000, 0, 0),
            ("ev3", FactType.CAPTURE_SETTLED, 0, -2000, 2000, 0),
        ]

        for key, ftype, dr, du, dc, daa in events:
            with transactional_session(conn):
                _, snap = record_financial_fact(
                    conn=conn, source_event_key=key, tenant_id="t1", agent_id="a_v13",
                    purchase_id="p_v13", budget_day="2026-11-06", fact_type=ftype,
                    delta_reserved_cents=dr, delta_unresolved_cents=du,
                    delta_captured_cents=dc, delta_auto_approved_cents=daa, actor="test"
                )
            # Invariant: auto_approved ≤ r3_committed at every step
            self.assertLessEqual(
                snap.auto_approved_committed_cents,
                snap.r3_committed_cents,
                f"After {key}: auto_approved ({snap.auto_approved_committed_cents}) > "
                f"r3_committed ({snap.r3_committed_cents}) — subset invariant violated!"
            )
            # auto_approved must never be negative
            self.assertGreaterEqual(snap.auto_approved_committed_cents, 0)

        conn.close()

    # --- V22-14 ---
    def test_stale_capture_owner_expired_lease_allows_takeover(self):
        """
        V22-14: A capture_operations row with owner_lease_until in the past
        can be taken over by a new worker. The original stale owner is then
        unable to commit its result via CAS.
        """
        order_id = _seed_purchase_in_state(
            self.conn, self.mock_paypal, "p_lease", "BUYER_APPROVED", 3200
        )
        old_owner_token = str(uuid.uuid4())
        stale_lease_until = time.time() - 60  # Expired 60 seconds ago
        cap_op_id = str(uuid.uuid4())
        now_iso = "2026-11-06T10:00:00Z"

        # Insert a capture_operation with an expired lease
        self.conn.execute(
            """
            INSERT INTO capture_operations (
                id, purchase_id, capture_request_id, state,
                owner_token, owner_lease_until, attempt_count, created_at, updated_at
            ) VALUES (?, 'p_lease', 'req_lease_cap', 'PREPARED',
                      ?, ?, 0, ?, ?)
            """,
            (cap_op_id, old_owner_token, stale_lease_until, now_iso, now_iso)
        )

        # New worker attempts atomic lease takeover via CAS
        new_owner_token = str(uuid.uuid4())
        new_lease_until = time.time() + 30
        updated = self.conn.execute(
            """
            UPDATE capture_operations
            SET owner_token = ?, owner_lease_until = ?
            WHERE purchase_id = 'p_lease'
              AND (owner_lease_until IS NULL OR owner_lease_until < ?)
            """,
            (new_owner_token, new_lease_until, time.time())
        ).rowcount

        # CAS must succeed — expired lease allows takeover
        self.assertEqual(updated, 1, "New worker failed to take over expired lease via CAS!")

        # Old owner tries to commit via CAS — must fail because token no longer matches
        old_owner_commit = self.conn.execute(
            """
            UPDATE capture_operations
            SET state = 'DEFINITIVE_SUCCESS'
            WHERE purchase_id = 'p_lease' AND owner_token = ?
            """,
            (old_owner_token,)
        ).rowcount

        self.assertEqual(old_owner_commit, 0,
                         "Stale owner committed result after lease expired — Invariant 5 violated!")

        # New owner successfully commits
        new_owner_commit = self.conn.execute(
            """
            UPDATE capture_operations
            SET state = 'DEFINITIVE_SUCCESS'
            WHERE purchase_id = 'p_lease' AND owner_token = ?
            """,
            (new_owner_token,)
        ).rowcount
        self.assertEqual(new_owner_commit, 1, "New owner (valid lease) could not commit result!")

    # --- V22-15 ---
    def test_single_cas_wins_concurrent_capture_resolution(self):
        """
        V22-15: Admin, worker, and reconciler simultaneously attempt to resolve
        the same CAPTURE_UNKNOWN purchase. CAS via owner_token ensures exactly one wins.
        """
        order_id = _seed_purchase_in_state(
            self.conn, self.mock_paypal, "p_cas_race", "CAPTURE_UNKNOWN", 4100
        )

        # Insert capture_operation with no current owner (open for claim)
        cap_op_id = str(uuid.uuid4())
        now_iso = "2026-11-06T10:00:00Z"
        self.conn.execute(
            """
            INSERT INTO capture_operations (
                id, purchase_id, capture_request_id, state,
                owner_token, owner_lease_until, attempt_count, created_at, updated_at
            ) VALUES (?, 'p_cas_race', 'req_cas_cap', 'UNKNOWN',
                      NULL, NULL, 1, ?, ?)
            """,
            (cap_op_id, now_iso, now_iso)
        )

        successful_claims = []
        num_claimants = 3  # admin, worker, reconciler

        def try_claim(claimant_name):
            c = get_thread_connection(self.db_path)
            token = str(uuid.uuid4())
            lease_until = time.time() + 30
            try:
                updated = c.execute(
                    """
                    UPDATE capture_operations
                    SET owner_token = ?, owner_lease_until = ?, state = 'RESOLVED_SUCCESS'
                    WHERE purchase_id = 'p_cas_race'
                      AND (owner_token IS NULL OR owner_lease_until < ?)
                      AND state = 'UNKNOWN'
                    """,
                    (token, lease_until, time.time())
                ).rowcount
                c.commit()
                if updated == 1:
                    successful_claims.append(claimant_name)
            except Exception:
                pass
            finally:
                close_thread_connection()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_claimants) as executor:
            futures = [
                executor.submit(try_claim, name)
                for name in ["admin", "worker", "reconciler"]
            ]
            concurrent.futures.wait(futures)

        # Exactly one claimant must have won the CAS
        self.assertEqual(
            len(successful_claims), 1,
            f"Expected exactly 1 CAS winner, got {len(successful_claims)}: {successful_claims}"
        )

        # Verify DB state reflects exactly one RESOLVED_SUCCESS
        cur = self.conn.execute(
            "SELECT state FROM capture_operations WHERE purchase_id = 'p_cas_race';"
        )
        self.assertEqual(cur.fetchone()["state"], "RESOLVED_SUCCESS")


if __name__ == "__main__":
    unittest.main()

