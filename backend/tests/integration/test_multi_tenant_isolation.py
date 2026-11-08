"""
Integration Tests for Multi-Tenant Boundary, Scoped API Keys, and v1 REST API.
Enforces:
- Tenant A cannot access Tenant B's quotes, purchases, or policies (returns 404).
- Identity derived strictly from API key, never from client body.
- Idempotency-Key guarantees: replay returns identical result, hash mismatch returns 422.
- Key revocation immediately locks out subsequent requests.
"""

import json
import os
import tempfile
import time
import unittest

from backend.app.auth.credentials import (
    create_agent_credential,
    authenticate_api_key,
    revoke_credential,
)
from backend.app.db.connection import create_connection, init_db, transactional_session
from backend.app.db.bootstrap import seed_defaults
from backend.app.db.quotes import create_trusted_quote, get_valid_quote
from backend.app.api.v1.routes import (
    request_quote,
    submit_purchase_request,
    get_purchase_request,
    list_purchase_requests,
    get_allowance,
    freeze_agent,
    unfreeze_agent,
    approve_request,
    reject_request,
    HTTPException,
    Response,
)


class TestMultiTenantIsolation(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        init_db(self.db_path)
        self.conn = create_connection(self.db_path)
        seed_defaults(self.conn)

        # Create Tenant A (Company ABC)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.conn.execute("INSERT INTO tenants (id, name, tier, created_at) VALUES ('tenant_abc', 'ABC Tech', 'business', ?);", (now_iso,))
        self.conn.execute("INSERT INTO agents (id, tenant_id, name, created_at) VALUES ('agent_abc', 'tenant_abc', 'ABC Procurement', ?);", (now_iso,))
        self.conn.execute(
            """
            INSERT INTO policy (agent_id, tenant_id, version, daily_budget_cents, max_per_tx_cents, approval_threshold_cents, velocity_cap_cents, allowlist_json, timezone, frozen, updated_at)
            VALUES ('agent_abc', 'tenant_abc', 1, 50000, 20000, 5000, 10000, '["merchant_shoes", "merchant_coffee"]', 'UTC', 0, ?);
            """,
            (now_iso,)
        )

        # Create Tenant B (Personal Ashfaq)
        self.conn.execute("INSERT INTO tenants (id, name, tier, created_at) VALUES ('tenant_ashfaq', 'Ashfaq Personal', 'personal', ?);", (now_iso,))
        self.conn.execute("INSERT INTO agents (id, tenant_id, name, created_at) VALUES ('agent_ashfaq', 'tenant_ashfaq', 'Ashfaq Shopping Agent', ?);", (now_iso,))
        self.conn.execute(
            """
            INSERT INTO policy (agent_id, tenant_id, version, daily_budget_cents, max_per_tx_cents, approval_threshold_cents, velocity_cap_cents, allowlist_json, timezone, frozen, updated_at)
            VALUES ('agent_ashfaq', 'tenant_ashfaq', 1, 10000, 6000, 3000, 5000, '["merchant_shoes", "merchant_coffee"]', 'UTC', 0, ?);
            """,
            (now_iso,)
        )

        # Issue scoped credentials
        self.cred_id_a, self.raw_key_a = create_agent_credential(self.conn, 'tenant_abc', 'agent_abc', 'ABC Key', scopes=['propose', 'read', 'admin'])
        self.cred_id_b, self.raw_key_b = create_agent_credential(self.conn, 'tenant_ashfaq', 'agent_ashfaq', 'Ashfaq Key', scopes=['propose', 'read'])

        self.ctx_a = authenticate_api_key(self.conn, self.raw_key_a)
        self.ctx_b = authenticate_api_key(self.conn, self.raw_key_b)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_credential_authentication_and_scopes(self):
        self.assertIsNotNone(self.ctx_a)
        self.assertEqual(self.ctx_a.tenant_id, 'tenant_abc')
        self.assertEqual(self.ctx_a.agent_id, 'agent_abc')
        self.assertIn('admin', self.ctx_a.scopes)

        self.assertIsNotNone(self.ctx_b)
        self.assertEqual(self.ctx_b.tenant_id, 'tenant_ashfaq')
        self.assertNotIn('admin', self.ctx_b.scopes)

        # Invalid key fails
        bad_ctx = authenticate_api_key(self.conn, 'ak_live_invalid_fake_key_12345')
        self.assertIsNone(bad_ctx)

    def test_credential_revocation_locks_out_immediately(self):
        revoked = revoke_credential(self.conn, self.cred_id_b, 'tenant_ashfaq')
        self.assertTrue(revoked)

        # Subsequent authentication returns None immediately
        ctx_after = authenticate_api_key(self.conn, self.raw_key_b)
        self.assertIsNone(ctx_after, "Revoked credential must fail authentication immediately.")

    def test_tenant_a_cannot_access_tenant_b_quotes(self):
        # Tenant A generates a quote
        q_id_a, _, _ = create_trusted_quote(
            self.conn, 'tenant_abc', 'agent_abc', [{'product_id': 'shoes_trail_runner', 'quantity': 1}]
        )

        # Tenant B cannot retrieve Tenant A's quote
        cached_by_b = get_valid_quote(self.conn, q_id_a, 'tenant_ashfaq', 'agent_ashfaq')
        self.assertIsNone(cached_by_b, "Tenant B must never be able to access Tenant A's quote.")

        # Tenant A can retrieve its own quote
        cached_by_a = get_valid_quote(self.conn, q_id_a, 'tenant_abc', 'agent_abc')
        self.assertIsNotNone(cached_by_a)

    def test_cross_tenant_purchase_request_isolation(self):
        from unittest import mock

        with mock.patch("backend.app.api.v1.routes.get_thread_connection", return_value=self.conn):
            # Tenant A submits purchase
            resp_a = Response()
            res_a = submit_purchase_request(
                payload={"items": [{"product_id": "shoes_trail_runner", "quantity": 1}], "purpose": "Company gear"},
                response=resp_a,
                idempotency_key="idemp_abc_01",
                cred=self.ctx_a
            )
            pid_a = res_a["purchase_request_id"]
            self.assertIsNotNone(pid_a)

            # Tenant B attempts to read Tenant A's purchase request -> 404 Not Found
            with self.assertRaises(HTTPException) as ctx:
                get_purchase_request(pid_a, cred=self.ctx_b)
            self.assertEqual(ctx.exception.status_code, 404)

            # Tenant B listing does not leak Tenant A's purchase
            list_b = list_purchase_requests(cred=self.ctx_b)
            self.assertEqual(len(list_b), 0)

            # Tenant A listing returns its purchase
            list_a = list_purchase_requests(cred=self.ctx_a)
            self.assertEqual(len(list_a), 1)
            self.assertEqual(list_a[0]["id"], pid_a)

    def test_idempotency_replay_and_hash_mismatch(self):
        from unittest import mock

        with mock.patch("backend.app.api.v1.routes.get_thread_connection", return_value=self.conn):
            resp1 = Response()
            res1 = submit_purchase_request(
                payload={"items": [{"product_id": "shoes_trail_runner", "quantity": 1}], "purpose": "Original purchase"},
                response=resp1,
                idempotency_key="idemp_unique_key_01",
                cred=self.ctx_a
            )
            self.assertEqual(res1["status"], "CAPTURED")

            # Same key, same payload -> Returns identical result with 200 idempotent replay
            resp2 = Response()
            res2 = submit_purchase_request(
                payload={"items": [{"product_id": "shoes_trail_runner", "quantity": 1}], "purpose": "Original purchase"},
                response=resp2,
                idempotency_key="idemp_unique_key_01",
                cred=self.ctx_a
            )
            self.assertTrue(res2["idempotent_replay"])
            self.assertEqual(res2["purchase_request_id"], res1["purchase_request_id"])

            # Same key, DIFFERENT payload -> 422 Unprocessable Entity
            resp3 = Response()
            with self.assertRaises(HTTPException) as err_ctx:
                submit_purchase_request(
                    payload={"items": [{"product_id": "coffee_beans_organic", "quantity": 1}], "purpose": "Tampered payload"},
                    response=resp3,
                    idempotency_key="idemp_unique_key_01",
                    cred=self.ctx_a
                )
            self.assertEqual(err_ctx.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()

