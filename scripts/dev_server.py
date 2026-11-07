"""
Allowance v4.2.2 Zero-Dependency Full-Stack Development Server.
Runs on vanilla Python 3 standard library (no pip dependencies required).
Serves:
1. Static frontend assets from frontend/dist/ at http://localhost:8000
2. Complete Allowance API endpoints under /api/*
"""

import json
import os
import sys
import threading
import time
import uuid
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

# Add repo root to path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT_DIR)

from backend.app.db.connection import get_thread_connection, transactional_session
from backend.app.db.financial_journal import get_budget_snapshot
from backend.app.agent.loop import execute_agent_loop
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

        if path == "/api/purchases":
            conn = get_thread_connection()
            cur = conn.execute("SELECT * FROM purchases ORDER BY created_at DESC LIMIT 50;")
            rows = cur.fetchall()
            purchases = []
            for r in rows:
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

            # Asynchronous agent task execution in worker thread
            def worker():
                c = get_thread_connection()
                try:
                    status, reason, cart, trace = execute_agent_loop(prompt)
                    if status == "COMPLETED" and cart:
                        # Submit cart to PayPilot orchestrator checkpoint P1
                        pid, pstate, decision = submit_cart_to_orchestrator(c, "tenant_demo", agent_id, run_id, cart, time.time())
                        
                        # If auto-approved, automatically create PayPal order & capture
                        if pstate == PurchaseState.AUTO_APPROVED:
                            create_paypal_order_for_purchase(c, pid, mock_paypal)
                            cur = c.execute("SELECT paypal_order_id FROM purchases WHERE id = ?;", (pid,))
                            p_row = cur.fetchone()
                            if p_row and p_row["paypal_order_id"]:
                                mock_paypal.set_buyer_approved(p_row["paypal_order_id"])
                                c.execute("UPDATE purchases SET status = 'BUYER_APPROVED' WHERE id = ?;", (pid,))
                                execute_capture_flow(c, pid, mock_paypal, boot_id="dev_boot")

                    with c:
                        c.execute("UPDATE agent_runs SET status = 'COMPLETED' WHERE id = ?;", (run_id,))
                except Exception as ex:
                    print(f"Error processing agent run {run_id}: {ex}")
                    with c:
                        c.execute("UPDATE agent_runs SET status = 'FAILED', status_reason = ? WHERE id = ?;", (str(ex), run_id))

            threading.Thread(target=worker, daemon=True).start()
            return self._send_json(202, {"run_id": run_id, "status": "QUEUED"})

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
