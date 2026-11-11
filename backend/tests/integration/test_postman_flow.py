import json
import os
import sqlite3
import tempfile
import time
import unittest

from backend.app.db.connection import close_thread_connection, init_db, create_connection
from backend.app.db.bootstrap import seed_defaults
from backend.app.auth.credentials import authenticate_api_key
from backend.app.api.v1.routes import (
    get_allowance,
    request_quote,
    submit_purchase_request,
    get_purchase_request,
    approve_request,
    freeze_agent,
    unfreeze_agent,
    Response,
)


class TestPostmanCollectionFlow(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        os.environ["DB_PATH"] = self.db_path

        init_db(self.db_path)
        conn = create_connection(self.db_path)
        seed_defaults(conn)
        self.demo_key = "ak_test_dev_token_allowance_2026_xyz"
        self.cred = authenticate_api_key(conn, self.demo_key)
        conn.close()
        self.assertIsNotNone(self.cred, "Seeded demo key must authenticate successfully")

    def tearDown(self):
        close_thread_connection()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_complete_postman_workflow(self):
        # 1. GET /v1/allowance
        allowance = get_allowance(cred=self.cred)
        self.assertGreater(allowance["daily_budget_cents"], 0)
        self.assertGreaterEqual(allowance["remaining_cents"], 0)
        self.assertGreaterEqual(allowance["remaining_allowance_cents"], 0)
        self.assertFalse(allowance["frozen"])

        # 2. POST /v1/quotes ($45 trail shoes)
        quote_payload = {
            "items": [{"product_id": "shoes_trail_runner", "quantity": 1}],
            "currency": "USD"
        }
        quote = request_quote(payload=quote_payload, cred=self.cred)
        self.assertEqual(quote["total_cents"], 4500)
        self.assertEqual(quote["currency"], "USD")
        self.assertIn("id", quote)
        self.assertIn("quote_id", quote)
        self.assertEqual(quote["id"], quote["quote_id"])
        quote_id = quote["id"]

        # 3. POST /v1/purchase-requests (Auto-approved proposal)
        resp = Response()
        pur_payload = {
            "quote_id": quote_id,
            "purpose": "Order trail running shoes"
        }
        pur = submit_purchase_request(
            payload=pur_payload,
            response=resp,
            idempotency_key="idemp_postman_test_1",
            cred=self.cred
        )
        self.assertIn("id", pur)
        self.assertIn("purchase_request_id", pur)
        self.assertIn(pur["status"], ("AUTO_APPROVED", "CAPTURED"))
        pur_id = pur["id"]

        # 4. GET /v1/purchase-requests/{id}
        fetched = get_purchase_request(purchase_id=pur_id, cred=self.cred)
        self.assertEqual(fetched["id"], pur_id)

        # 5. POST /v1/quotes ($90 road flats for human review)
        high_quote_payload = {
            "items": [{"product_id": "shoes_road_racer", "quantity": 1}],
            "currency": "USD"
        }
        high_quote = request_quote(payload=high_quote_payload, cred=self.cred)
        self.assertEqual(high_quote["total_cents"], 9000)
        high_quote_id = high_quote["id"]

        # 6. POST /v1/purchase-requests ($90 proposal -> PENDING_APPROVAL)
        resp2 = Response()
        high_pur_payload = {
            "quote_id": high_quote_id,
            "purpose": "Order road racing flats"
        }
        high_pur = submit_purchase_request(
            payload=high_pur_payload,
            response=resp2,
            idempotency_key="idemp_postman_test_2",
            cred=self.cred
        )
        self.assertEqual(high_pur["status"], "PENDING_APPROVAL")
        high_pur_id = high_pur["id"]

        # 7. POST /v1/purchase-requests/{id}/approve
        approval = approve_request(
            purchase_id=high_pur_id,
            payload={"reason": "Approved by supervisor in Postman flow"},
            cred=self.cred
        )
        self.assertEqual(approval["purchase_id"], high_pur_id)
        self.assertIn(approval["status"], ("APPROVED", "CAPTURED"))

        # 8. POST /v1/freeze
        frz = freeze_agent(payload={"reason": "Audit freeze"}, cred=self.cred)
        self.assertTrue(frz["frozen"])

        # 9. POST /v1/unfreeze
        unfrz = unfreeze_agent(payload={"reason": "Audit complete"}, cred=self.cred)
        self.assertFalse(unfrz["frozen"])


if __name__ == "__main__":
    unittest.main()
