"""
Unit and Integration Tests for Development Server Security and Multi-Workspace Isolation.
Verifies Fixes for:
- FL-01: Admin Token Verification on /api/freeze, /api/unfreeze, /api/reconcile, /api/demo/reset.
- FL-03: Resilient in-place SQLite reset without Windows file lock collisions.
- FL-04: Multi-tenant / Multi-workspace switching between Demo Organization and Personal Edition.
"""

import hmac
import os
import unittest
from scripts.dev_server import ADMIN_TOKEN, AllowanceRequestHandler
from scripts.seed_db import seed_database
from backend.app.db.connection import create_connection, init_db


class DummyHandler:
    """Mock handler implementing AllowanceRequestHandler security checks."""
    def __init__(self, headers=None, path="/"):
        self.headers = headers or {}
        self.path = path
        self.status_code = None
        self.response_data = None

    def _send_json(self, status_code: int, data: any):
        self.status_code = status_code
        self.response_data = data

    _require_admin = AllowanceRequestHandler._require_admin
    _get_workspace_agent = AllowanceRequestHandler._get_workspace_agent


class TestDevServerSecurityAndWorkspaces(unittest.TestCase):
    def test_admin_token_required_rejects_missing_token(self):
        handler = DummyHandler(headers={})
        is_auth = handler._require_admin()
        self.assertFalse(is_auth)
        self.assertEqual(handler.status_code, 403)
        self.assertIn("Forbidden", handler.response_data.get("detail", ""))

    def test_admin_token_required_rejects_invalid_token(self):
        handler = DummyHandler(headers={"X-Admin-Token": "wrong-secret-token-12345"})
        is_auth = handler._require_admin()
        self.assertFalse(is_auth)
        self.assertEqual(handler.status_code, 403)

    def test_admin_token_accepts_valid_header(self):
        handler = DummyHandler(headers={"X-Admin-Token": ADMIN_TOKEN})
        is_auth = handler._require_admin()
        self.assertTrue(is_auth)
        self.assertIsNone(handler.status_code)

    def test_admin_token_accepts_valid_bearer(self):
        handler = DummyHandler(headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        is_auth = handler._require_admin()
        self.assertTrue(is_auth)
        self.assertIsNone(handler.status_code)

    def test_workspace_resolution_defaults_to_organization(self):
        import urllib.parse
        parsed = urllib.parse.urlparse("/api/policy")
        handler = DummyHandler(headers={})
        agent_id = handler._get_workspace_agent(parsed)
        self.assertEqual(agent_id, "default")

    def test_workspace_resolution_detects_personal_header(self):
        import urllib.parse
        parsed = urllib.parse.urlparse("/api/policy")
        handler = DummyHandler(headers={"X-Workspace": "personal"})
        agent_id = handler._get_workspace_agent(parsed)
        self.assertEqual(agent_id, "agent_personal")

    def test_workspace_resolution_detects_personal_query_param(self):
        import urllib.parse
        parsed = urllib.parse.urlparse("/api/budget?workspace=personal")
        handler = DummyHandler(headers={})
        agent_id = handler._get_workspace_agent(parsed)
        self.assertEqual(agent_id, "agent_personal")

    def test_inplace_seed_reset_safety(self):
        """Verify that in-place seed database reset executes cleanly without raising WinError."""
        test_db = "test_reset_safety.db"
        try:
            init_db(test_db)
            seed_database(test_db, reset=True)
            conn = create_connection(test_db)
            cnt = conn.execute("SELECT COUNT(*) FROM policy;").fetchone()[0]
            conn.close()
            self.assertGreaterEqual(cnt, 2)  # Both default and agent_personal policies seeded
        finally:
            for ext in ["", "-wal", "-shm"]:
                p = f"{test_db}{ext}"
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass


if __name__ == "__main__":
    unittest.main()

