"""
Allowance v4.2.2 Zero-Dependency Full-Stack Development Server.
Runs on vanilla Python 3 standard library (no pip dependencies required).
Serves:
1. Static frontend assets from frontend/dist/ at http://localhost:8000
2. Complete Allowance API endpoints under /api/*
"""

import json
import logging
import os
import sys
import threading
import time
import uuid
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("allowance.dev_server")

# Add repo root to path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT_DIR)

from backend.app.db.connection import get_thread_connection, transactional_session
from backend.app.db.financial_journal import get_budget_snapshot
from backend.app.agent.loop import execute_agent_loop
from backend.app.agent.catalog import get_product
from backend.app.orchestrator.core import (
    get_agent_policy,
    submit_cart_to_orchestrator,
    create_paypal_order_for_purchase,
    approve_pending_purchase,
)
from backend.app.orchestrator.capture_lifecycle import execute_capture_flow
from backend.app.paypal.client import MockPayPalClient
from backend.app.policy.rules import Outcome
from backend.app.orchestrator.state_machine import PurchaseState
from backend.app.api.v1.routes import (
    get_allowance as v1_get_allowance,
    request_quote as v1_request_quote,
    submit_purchase_request as v1_submit_purchase_request,
    list_purchase_requests as v1_list_purchase_requests,
    get_purchase_request as v1_get_purchase_request,
    approve_request as v1_approve_request,
    reject_request as v1_reject_request,
    freeze_agent as v1_freeze_agent,
    unfreeze_agent as v1_unfreeze_agent,
    get_current_credential,
    HTTPException as V1HTTPException,
    Response as V1Response,
)
from scripts.seed_db import seed_database

FRONTEND_DIST = os.path.join(ROOT_DIR, "frontend", "dist")
mock_paypal = MockPayPalClient()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class AllowanceRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FRONTEND_DIST, **kwargs)

    def _send_json(self, status_code: int, data: any):
        resp = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, PUT, DELETE")
        self.end_headers()
        self.wfile.write(resp)

    def _get_v1_cred(self):
        auth = self.headers.get("Authorization")
        api_key = self.headers.get("X-API-Key")
        try:
            return get_current_credential(authorization=auth, x_api_key=api_key)
        except V1HTTPException as e:
            self._send_json(e.status_code, {"detail": e.detail})
            return None
        except Exception as e:
            self._send_json(401, {"detail": str(e)})
            return None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, PUT, DELETE")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/health", "/api/health"):
            return self._send_json(200, {
                "status": "ok",
                "platform": "Allowance v4.2.2",
                "env": "development",
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            })

        # --- Version 1 Scoped REST API Endpoints ---
        if path == "/v1/allowance":
            cred = self._get_v1_cred()
            if not cred:
                return
            try:
                res = v1_get_allowance(cred)
                return self._send_json(200, res)
            except V1HTTPException as e:
                return self._send_json(e.status_code, {"detail": e.detail})

        if path == "/v1/purchase-requests":
            cred = self._get_v1_cred()
            if not cred:
                return
            try:
                res = v1_list_purchase_requests(cred)
                return self._send_json(200, res)
            except V1HTTPException as e:
                return self._send_json(e.status_code, {"detail": e.detail})

        if path.startswith("/v1/purchase-requests/"):
            purchase_id = path.split("/")[3]
            cred = self._get_v1_cred()
            if not cred:
                return
            try:
                res = v1_get_purchase_request(purchase_id, cred)
                return self._send_json(200, res)
            except V1HTTPException as e:
                return self._send_json(e.status_code, {"detail": e.detail})

        if path == "/api/policy":
            conn = get_thread_connection()
            pol = get_agent_policy(conn, "default")
            if not pol:
                return self._send_json(404, {"detail": "Policy not found"})
            return self._send_json(200, {
                "agent_id": pol.agent_id,
                "version": pol.version,
                "daily_budget_cents": pol.daily_budget_cents,
                "max_per_tx_cents": pol.max_per_tx_cents,
                "approval_threshold_cents": pol.approval_threshold_cents,
                "velocity_cap_cents": pol.velocity_cap_cents,
                "allowlist": list(pol.allowlist),
                "timezone": pol.timezone,
                "frozen": pol.frozen
            })

        if path == "/api/budget":
            conn = get_thread_connection()
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            snap = get_budget_snapshot(conn, "default", today)
            return self._send_json(200, {
                "agent_id": snap.agent_id,
                "budget_day": snap.budget_day,
                "currency": snap.currency,
                "captured_cents": snap.captured_cents,
                "reserved_cents": snap.reserved_cents,
                "unresolved_cents": snap.unresolved_cents,
                "auto_approved_committed_cents": snap.auto_approved_committed_cents,
                "version": snap.version
            })

        if path == "/api/purchases":
            conn = get_thread_connection()
            cur = conn.execute("SELECT * FROM purchases ORDER BY created_at DESC LIMIT 50;")
            rows = cur.fetchall()
            purchases = []
            for r in rows:
                items_list = []
                try:
                    raw_items = json.loads(r["cart_json"]) if r["cart_json"] else []
                    for it in raw_items:
                        p = get_product(it.get("id", ""))
                        items_list.append({
                            "name": p.name if p else it.get("id", "Product"),
                            "quantity": it.get("qty", 1),
                            "cents": it.get("cents", 0)
                        })
                except Exception:
                    pass

                purchases.append({
                    "id": r["id"],
                    "agent_id": r["agent_id"],
                    "status": r["status"],
                    "status_reason": r["status_reason"],
                    "auto_approved": bool(r["auto_approved"]),
                    "total_cents": r["total_cents"],
                    "currency": r["currency"],
                    "merchant_ids": json.loads(r["merchant_ids_json"]),
                    "budget_day": r["budget_day"],
                    "items": items_list,
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"]
                })
            return self._send_json(200, purchases)

        if path == "/api/ledger":
            conn = get_thread_connection()
            cur = conn.execute("SELECT * FROM ledger ORDER BY created_at DESC LIMIT 100;")
            rows = cur.fetchall()
            ledger = []
            for r in rows:
                ledger.append({
                    "id": r["id"],
                    "purchase_id": r["purchase_id"],
                    "agent_id": r["agent_id"],
                    "from_state": r["from_state"],
                    "to_state": r["to_state"],
                    "rule_name": r["rule_name"],
                    "reason": r["reason"],
                    "checkpoint": r["checkpoint"],
                    "amount_cents": r["amount_cents"],
                    "actor": r["actor"],
                    "created_at": r["created_at"]
                })
            return self._send_json(200, ledger)

        # Static files fallback to SPA
        file_path = os.path.join(FRONTEND_DIST, path.lstrip("/"))
        if not os.path.exists(file_path) or os.path.isdir(file_path):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        # --- Version 1 Scoped REST API Endpoints ---
        if path == "/v1/quotes":
            cred = self._get_v1_cred()
            if not cred:
                return
            try:
                res = v1_request_quote(payload, cred)
                return self._send_json(201, res)
            except V1HTTPException as e:
                return self._send_json(e.status_code, {"detail": e.detail})

        if path == "/v1/purchase-requests":
            cred = self._get_v1_cred()
            if not cred:
                return
            idempotency_key = self.headers.get("Idempotency-Key")
            if not idempotency_key:
                return self._send_json(422, {"detail": "Missing mandatory Idempotency-Key header."})
            try:
                v1_resp = V1Response()
                res = v1_submit_purchase_request(payload, v1_resp, idempotency_key, cred)
                return self._send_json(v1_resp.status_code, res)
            except V1HTTPException as e:
                return self._send_json(e.status_code, {"detail": e.detail})

        if path.startswith("/v1/purchase-requests/") and path.endswith("/approve"):
            purchase_id = path.split("/")[3]
            cred = self._get_v1_cred()
            if not cred:
                return
            try:
                res = v1_approve_request(purchase_id, cred)
                return self._send_json(200, res)
            except V1HTTPException as e:
                return self._send_json(e.status_code, {"detail": e.detail})

        if path.startswith("/v1/purchase-requests/") and path.endswith("/reject"):
            purchase_id = path.split("/")[3]
            cred = self._get_v1_cred()
            if not cred:
                return
            try:
                res = v1_reject_request(purchase_id, payload, cred)
                return self._send_json(200, res)
            except V1HTTPException as e:
                return self._send_json(e.status_code, {"detail": e.detail})

        if path == "/v1/freeze":
            cred = self._get_v1_cred()
            if not cred:
                return
            try:
                res = v1_freeze_agent(payload, cred)
                return self._send_json(200, res)
            except V1HTTPException as e:
                return self._send_json(e.status_code, {"detail": e.detail})

        if path == "/v1/unfreeze":
            cred = self._get_v1_cred()
            if not cred:
                return
            try:
                res = v1_unfreeze_agent(payload, cred)
                return self._send_json(200, res)
            except V1HTTPException as e:
                return self._send_json(e.status_code, {"detail": e.detail})

        if path == "/api/demo/reset":
            from backend.app.db.connection import close_thread_connection
            close_thread_connection()
            for p in ("allowance.db", "allowance.db-wal", "allowance.db-shm"):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            seed_database("allowance.db")
            return self._send_json(200, {"status": "ok", "message": "Demo database freshly seeded."})

        if path == "/api/freeze":
            conn = get_thread_connection()
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with conn:
                conn.execute("UPDATE policy SET frozen = 1, updated_at = ? WHERE agent_id = 'default';", (now_iso,))
            return self._send_json(200, {"agent_id": "default", "frozen": True})

        if path == "/api/unfreeze":
            conn = get_thread_connection()
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with conn:
                conn.execute("UPDATE policy SET frozen = 0, updated_at = ? WHERE agent_id = 'default';", (now_iso,))
            return self._send_json(200, {"agent_id": "default", "frozen": False})

        if path.startswith("/api/purchases/") and path.endswith("/approve"):
            purchase_id = path.split("/")[3]
            conn = get_thread_connection()
            try:
                new_state, reason = approve_pending_purchase(conn, purchase_id, admin_actor="admin_dashboard")
                if new_state == PurchaseState.APPROVED:
                    # Proceed to PayPal order creation & simulation
                    create_paypal_order_for_purchase(conn, purchase_id, mock_paypal)
                    cur = conn.execute("SELECT paypal_order_id FROM purchases WHERE id = ?;", (purchase_id,))
                    row = cur.fetchone()
                    if row and row["paypal_order_id"]:
                        mock_paypal.set_buyer_approved(row["paypal_order_id"])
                        conn.execute("UPDATE purchases SET status = 'BUYER_APPROVED' WHERE id = ?;", (purchase_id,))
                        execute_capture_flow(conn, purchase_id, mock_paypal, boot_id="dev_boot")
                return self._send_json(200, {"purchase_id": purchase_id, "status": new_state.value, "reason": reason})
            except Exception as e:
                return self._send_json(400, {"detail": str(e)})

        if path == "/api/agent/run":
            prompt = payload.get("user_prompt", "")
            agent_id = payload.get("agent_id", "default")
            run_id = str(uuid.uuid4())
            now = time.time()
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

            conn = get_thread_connection()
            with conn:
                conn.execute(
                    """
                    INSERT INTO agent_runs (id, tenant_id, agent_id, idempotency_key, request_hash, queued_at, status, created_at)
                    VALUES (?, 'tenant_demo', ?, ?, ?, ?, 'RUNNING', ?);
                    """,
                    (run_id, agent_id, f"key_{run_id}", str(hash(prompt)), now, now_iso)
                )

            # Process run synchronously so UI gets immediate authoritative outcome
            try:
                ai_mode = "Claude 3.5 Sonnet (ANTHROPIC_API_KEY)" if os.environ.get("ANTHROPIC_API_KEY") else "Deterministic Fallback (no key)"
                logger.info("ShopMind: Executing with [%s] for prompt: '%s'", ai_mode, prompt)
                status, reason, cart, trace = execute_agent_loop(prompt)
                if status == "COMPLETED" and cart:
                    pid, pstate, decision = submit_cart_to_orchestrator(conn, "tenant_demo", agent_id, run_id, cart, time.time())
                    
                    product_name = cart.items[0].name if cart.items else "Product"
                    price_usd = f"{cart.total_cents / 100:.2f}"

                    # If auto-approved, advance through PayPal create & capture
                    if pstate == PurchaseState.AUTO_APPROVED:
                        create_paypal_order_for_purchase(conn, pid, mock_paypal)
                        cur = conn.execute("SELECT paypal_order_id FROM purchases WHERE id = ?;", (pid,))
                        p_row = cur.fetchone()
                        if p_row and p_row["paypal_order_id"]:
                            mock_paypal.set_buyer_approved(p_row["paypal_order_id"])
                            conn.execute("UPDATE purchases SET status = 'BUYER_APPROVED' WHERE id = ?;", (pid,))
                            cap_status, cap_id = execute_capture_flow(conn, pid, mock_paypal, boot_id="dev_boot")

                    with conn:
                        conn.execute("UPDATE agent_runs SET status = 'COMPLETED' WHERE id = ?;", (run_id,))

                    return self._send_json(200, {
                        "run_id": run_id,
                        "status": pstate.value,
                        "purchase_id": pid,
                        "product_name": product_name,
                        "amount_usd": price_usd,
                        "rule_name": decision.rule_name,
                        "reason": decision.reason,
                        "outcome": decision.outcome.value
                    })
                else:
                    with conn:
                        conn.execute("UPDATE agent_runs SET status = 'FAILED', status_reason = ? WHERE id = ?;", (reason, run_id))
                    return self._send_json(400, {
                        "run_id": run_id,
                        "status": "FAILED",
                        "detail": f"Agent could not build cart: {reason}"
                    })
            except Exception as ex:
                with conn:
                    conn.execute("UPDATE agent_runs SET status = 'FAILED', status_reason = ? WHERE id = ?;", (str(ex), run_id))
                return self._send_json(400, {
                    "run_id": run_id,
                    "status": "FAILED",
                    "detail": str(ex)
                })

        return self._send_json(404, {"detail": "Endpoint not found"})


def main():
    db_path = os.environ.get("DB_PATH", os.path.join(ROOT_DIR, "allowance.db"))
    seed_database(db_path)

    port = int(os.environ.get("PORT", "8000"))
    server_address = ("0.0.0.0", port)
    httpd = ThreadingHTTPServer(server_address, AllowanceRequestHandler)
    print("=" * 70)
    print("ALLOWANCE v4.2.2 — AUTONOMOUS FINANCIAL PLATFORM IS RUNNING")
    print(f"[*] Access Frontend Dashboard at : http://localhost:{port}")
    print(f"[*] API Health Endpoint        : http://localhost:{port}/health")
    print(f"[*] Storage Database           : {db_path}")
    print("=" * 70)
    print("Press Ctrl+C to terminate.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Allowance Development Server...")
        httpd.server_close()


if __name__ == "__main__":
    main()
