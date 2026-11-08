"""
Runtime pipeline tests: worker processor, run completion, and API idempotency.

These exercise the pieces that connect an admitted agent run to a policy decision
without starting the HTTP server or a long-lived worker thread.
"""

import os
import tempfile
import time
import types
import unittest
from unittest import mock

from backend.app.api.dto import RunAgentRequest
from backend.app.api.routes import run_agent
from backend.app.db.connection import create_connection
from backend.app.runtime import process_agent_run, start_runtime, stop_runtime
from scripts.seed_db import seed_database


def _fake_request() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        headers={},
        client=types.SimpleNamespace(host="127.0.0.1"),
    )


class TestRunProcessor(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        seed_database(self.db_path)
        self.conn = create_connection(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _insert_run(self, run_id: str, prompt: str, status: str = "RUNNING") -> None:
        self.conn.execute(
            """
            INSERT INTO agent_runs (
                id, tenant_id, agent_id, idempotency_key, request_hash, user_prompt,
                queued_at, status, created_at
            ) VALUES (?, 'tenant_demo', 'default', ?, 'h', ?, ?, ?, '2026');
            """,
            (run_id, f"key_{run_id}", prompt, time.time(), status)
        )

    def test_completes_run_and_creates_auto_approved_purchase(self):
        self._insert_run("r_happy", "trail running shoes")

        with mock.patch("backend.app.runtime._db_path", self.db_path):
            process_agent_run("r_happy", "worker-test")

        run = self.conn.execute("SELECT * FROM agent_runs WHERE id = 'r_happy';").fetchone()
        self.assertEqual(run["status"], "COMPLETED")
        self.assertIsNone(run["status_reason"])

        purchase = self.conn.execute("SELECT * FROM purchases WHERE run_id = 'r_happy';").fetchone()
        self.assertIsNotNone(purchase)
        self.assertEqual(purchase["status"], "AUTO_APPROVED")
        self.assertEqual(purchase["total_cents"], 4500)

        event_types = {
            row["event_type"]
            for row in self.conn.execute("SELECT event_type FROM events WHERE run_id = 'r_happy';")
        }
        self.assertIn("RUN_STARTED", event_types)
        self.assertIn("RUN_COMPLETED", event_types)

        ledger = self.conn.execute(
            "SELECT * FROM ledger WHERE purchase_id = ?;", (purchase["id"],)
        ).fetchall()
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["to_state"], "AUTO_APPROVED")

    def test_agent_failure_marks_run_failed_without_purchase(self):
        self._insert_run("r_fail", "trail running shoes")

        with mock.patch("backend.app.runtime._db_path", self.db_path), mock.patch(
            "backend.app.runtime.execute_agent_loop",
            return_value=("FAILED", "step_cap", None, [{"event": "step_cap"}]),
        ):
            process_agent_run("r_fail", "worker-test")

        run = self.conn.execute("SELECT * FROM agent_runs WHERE id = 'r_fail';").fetchone()
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["status_reason"], "step_cap")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM purchases WHERE run_id = 'r_fail';").fetchone()["c"],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE run_id = 'r_fail' AND event_type = 'RUN_FAILED';"
            ).fetchone()["c"],
            1,
        )

    def test_agent_exception_is_captured_as_agent_error(self):
        self._insert_run("r_error", "trail running shoes")

        with mock.patch("backend.app.runtime._db_path", self.db_path), mock.patch(
            "backend.app.runtime.execute_agent_loop", side_effect=RuntimeError("model down")
        ):
            process_agent_run("r_error", "worker-test")

        run = self.conn.execute("SELECT * FROM agent_runs WHERE id = 'r_error';").fetchone()
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["status_reason"], "agent_error")

    def test_non_running_run_is_left_untouched(self):
        """A stale dispatch must not restart a run that is no longer RUNNING."""
        self._insert_run("r_queued", "trail running shoes", status="QUEUED")

        with mock.patch("backend.app.runtime._db_path", self.db_path), mock.patch(
            "backend.app.runtime.execute_agent_loop"
        ) as loop_mock:
            process_agent_run("r_queued", "worker-test")

        loop_mock.assert_not_called()
        run = self.conn.execute("SELECT * FROM agent_runs WHERE id = 'r_queued';").fetchone()
        self.assertEqual(run["status"], "QUEUED")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM purchases WHERE run_id = 'r_queued';").fetchone()["c"],
            0,
        )


class _FakePool:
    def __init__(self, accept: bool = True):
        self.accept = accept
        self.enqueued = []

    def enqueue_run(self, run_id: str) -> bool:
        if self.accept:
            self.enqueued.append(run_id)
        return self.accept


class TestRunApiIdempotency(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        seed_database(self.db_path)
        self.conn = create_connection(self.db_path)
        self.pool = _FakePool()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _call(self, prompt: str, key: str):
        response = types.SimpleNamespace(status_code=202)
        body = run_agent(
            payload=RunAgentRequest(user_prompt=prompt),
            request=_fake_request(),
            response=response,
            idempotency_key=key,
            tenant_id="tenant_demo",
        )
        return response, body

    def test_replay_returns_existing_run_and_new_prompt_rejects(self):
        with mock.patch(
            "backend.app.api.routes.get_thread_connection", return_value=self.conn
        ), mock.patch("backend.app.api.routes.get_worker_pool", return_value=self.pool):
            first_response, first = self._call("trail running shoes", "key-1")
            self.assertEqual(first_response.status_code, 202)
            self.assertEqual(first["status"], "QUEUED")
            self.assertEqual(len(self.pool.enqueued), 1)

            replay_response, replay = self._call("trail running shoes", "key-1")
            self.assertEqual(replay_response.status_code, 200)
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(replay["run_id"], first["run_id"])
            # A replay must not dispatch the same run twice.
            self.assertEqual(len(self.pool.enqueued), 1)

            with self.assertRaises(Exception) as ctx:
                self._call("a completely different request", "key-1")
            self.assertEqual(getattr(ctx.exception, "status_code", None), 422)

    def test_queue_rejection_fails_run_and_returns_503(self):
        rejecting_pool = _FakePool(accept=False)

        with mock.patch(
            "backend.app.api.routes.get_thread_connection", return_value=self.conn
        ), mock.patch(
            "backend.app.api.routes.get_worker_pool", return_value=rejecting_pool
        ):
            with self.assertRaises(Exception) as ctx:
                self._call("trail running shoes", "key-2")

        self.assertEqual(getattr(ctx.exception, "status_code", None), 503)

        run = self.conn.execute(
            "SELECT * FROM agent_runs WHERE idempotency_key = 'key-2';"
        ).fetchone()
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["status_reason"], "queue_rejected")


class TestRuntimeStartup(unittest.TestCase):

    def test_start_runtime_seeds_defaults_and_stops(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = tmp.name
        try:
            pool = start_runtime(db_path)
            try:
                self.assertIsNotNone(pool)
                conn = create_connection(db_path)
                try:
                    self.assertGreater(
                        conn.execute("SELECT COUNT(*) AS c FROM policy;").fetchone()["c"], 0
                    )
                finally:
                    conn.close()
            finally:
                stop_runtime()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)


if __name__ == "__main__":
    unittest.main()
