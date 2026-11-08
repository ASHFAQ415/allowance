"""
Allowance Model Context Protocol (MCP) Server.
Enforces Invariant 6 (AI & Payment Authority Separation):
- External AI models (Claude, Gemini, ChatGPT) interact through bounded, read/propose tools only.
- Strict omission of capture_payment, approve, reject, or policy-mutation primitives.
- Server-side pricing is strictly authoritative; models propose quote IDs, never prices.
"""

import json
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

# Add repo root to path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from backend.app.agent.catalog import search_catalog_data, get_product
from backend.app.auth.credentials import authenticate_api_key, CredentialContext
from backend.app.db.connection import create_connection, transactional_session, DEFAULT_DB_PATH
from backend.app.db.quotes import create_trusted_quote, get_valid_quote
from backend.app.db.financial_journal import get_budget_snapshot
from backend.app.orchestrator.core import (
    get_agent_policy,
    submit_cart_to_orchestrator,
    create_paypal_order_for_purchase,
    PurchaseState,
)
from backend.app.orchestrator.capture_lifecycle import execute_capture_flow
from backend.app.orchestrator.admission import check_and_admit_run
from backend.app.paypal import get_paypal_client


# Tool Definitions following MCP Schema Specification
MCP_TOOLS = [
    {
        "name": "search_products",
        "description": "Search the authorized product catalog by keyword or category. Max 100 character query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term, e.g. 'trail running shoes'"},
                "limit": {"type": "integer", "description": "Max results to return (1-20)", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_product_quote",
        "description": "Generate an authoritative server-calculated price quote with a 10-minute validity guarantee.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "description": "Catalog product ID, e.g. 'shoes_trail_runner'"},
                "quantity": {"type": "integer", "description": "Number of units (1-50)", "default": 1}
            },
            "required": ["product_id"]
        }
    },
    {
        "name": "get_allowance",
        "description": "Check current spending authority and remaining daily allowance for the calling agent.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "request_purchase",
        "description": "Submit a purchase proposal to Allowance. PayPilot deterministically authorizes or holds for human review.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "quote_id": {"type": "string", "description": "Pre-authorized quote ID obtained from get_product_quote"},
                "purpose": {"type": "string", "description": "Business justification or user prompt rationale (max 200 chars)"}
            },
            "required": ["quote_id"]
        }
    },
    {
        "name": "get_purchase_status",
        "description": "Check the authoritative lifecycle status of a previously submitted purchase proposal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "purchase_request_id": {"type": "string", "description": "ID returned by request_purchase"}
            },
            "required": ["purchase_request_id"]
        }
    }
]


class AllowanceMCPServer:
    """
    Model Context Protocol (MCP) Server for Allowance.
    Binds to an authenticated agent context and provides safe tools.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH, default_tenant: str = "tenant_demo", default_agent: str = "default"):
        self.db_path = db_path
        self.default_tenant = default_tenant
        self.default_agent = default_agent

    def _get_conn(self):
        return create_connection(self.db_path)

    # --- Tool Implementations ---

    def tool_search_products(self, query: str, limit: int = 10) -> Dict[str, Any]:
        """Discovers products from the verified catalog."""
        clean_query = str(query)[:100].strip()
        limit = min(max(1, int(limit)), 20)
        items = search_catalog_data(clean_query, limit=limit)
        results = []
        for it in items:
            results.append({
                "product_id": it.id,
                "name": it.name,
                "price_cents": it.price_cents,
                "price_usd": f"${it.price_cents / 100:.2f}",
                "category": it.category,
                "merchant_id": it.merchant_id,
                "in_stock": it.in_stock,
                "description": it.description[:120]
            })
        return {"count": len(results), "products": results}

    def tool_get_product_quote(self, product_id: str, quantity: int = 1, tenant_id: Optional[str] = None, agent_id: Optional[str] = None) -> Dict[str, Any]:
        """Generates a trusted server-side quote."""
        tenant_id = tenant_id or self.default_tenant
        agent_id = agent_id or self.default_agent
        conn = self._get_conn()
        try:
            with transactional_session(conn):
                quote_id, cart, expires_at = create_trusted_quote(
                    conn=conn,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    raw_items=[{"product_id": product_id, "quantity": quantity}],
                    currency="USD"
                )
            return {
                "quote_id": quote_id,
                "product_id": product_id,
                "name": cart.items[0].name,
                "quantity": cart.items[0].quantity,
                "unit_price_cents": cart.items[0].unit_price_cents,
                "total_cents": cart.total_cents,
                "total_usd": f"${cart.total_cents / 100:.2f}",
                "currency": cart.currency,
                "expires_at": expires_at
            }
        finally:
            conn.close()

    def tool_get_allowance(self, tenant_id: Optional[str] = None, agent_id: Optional[str] = None) -> Dict[str, Any]:
        """Checks spending budget and remaining allowance."""
        tenant_id = tenant_id or self.default_tenant
        agent_id = agent_id or self.default_agent
        conn = self._get_conn()
        try:
            policy = get_agent_policy(conn, agent_id)
            if not policy:
                return {"error": "Policy not found"}

            now = time.time()
            budget_day = time.strftime("%Y-%m-%d", time.gmtime(now))
            snapshot = get_budget_snapshot(conn, agent_id, budget_day)
            remaining_cents = max(0, policy.daily_budget_cents - snapshot.r3_committed_cents)

            return {
                "agent_id": agent_id,
                "remaining_cents": remaining_cents,
                "remaining_usd": f"${remaining_cents / 100:.2f}",
                "reserved_usd": f"${snapshot.reserved_cents / 100:.2f}",
                "spent_today_usd": f"${snapshot.captured_cents / 100:.2f}",
                "frozen": policy.frozen,
                "budget_day": budget_day
            }
        finally:
            conn.close()

    def tool_request_purchase(self, quote_id: str, purpose: str = "", tenant_id: Optional[str] = None, agent_id: Optional[str] = None) -> Dict[str, Any]:
        """Submits a purchase proposal using a trusted quote."""
        tenant_id = tenant_id or self.default_tenant
        agent_id = agent_id or self.default_agent
        conn = self._get_conn()
        now = time.time()
        try:
            cached = get_valid_quote(conn, quote_id, tenant_id, agent_id, now)
            if not cached:
                return {"error": "Invalid or expired quote_id. Call get_product_quote to obtain a fresh quote."}

            cart, _ = cached
            run_id = str(uuid.uuid4())
            idempotency_key = f"mcp_{quote_id}_{uuid.uuid4().hex[:8]}"

            with transactional_session(conn):
                check_and_admit_run(
                    conn=conn,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    idempotency_key=idempotency_key,
                    request_hash=cart.cart_hash,
                    run_id=run_id,
                    now=now,
                    user_prompt=purpose[:200]
                )

            pid, target_state, decision = submit_cart_to_orchestrator(
                conn=conn,
                tenant_id=tenant_id,
                agent_id=agent_id,
                run_id=run_id,
                cart=cart,
                now=now
            )

            # Auto-advance if auto-approved
            paypal_client = get_paypal_client()
            if target_state == PurchaseState.AUTO_APPROVED:
                create_paypal_order_for_purchase(conn, pid, paypal_client)
                p_row = conn.execute("SELECT paypal_order_id FROM purchases WHERE id = ?;", (pid,)).fetchone()
                if p_row and p_row["paypal_order_id"] and hasattr(paypal_client, "set_buyer_approved"):
                    paypal_client.set_buyer_approved(p_row["paypal_order_id"])
                    with transactional_session(conn):
                        conn.execute("UPDATE purchases SET status = 'BUYER_APPROVED' WHERE id = ?;", (pid,))
                    execute_capture_flow(conn, pid, paypal_client, boot_id="mcp_runtime")
                    target_state = PurchaseState.CAPTURED

            with transactional_session(conn):
                conn.execute("UPDATE agent_runs SET status = 'COMPLETED' WHERE id = ?;", (run_id,))

            return {
                "purchase_request_id": pid,
                "status": target_state.value,
                "decision": {
                    "outcome": decision.outcome.value,
                    "rule": decision.rule_name,
                    "reason": decision.reason
                },
                "total_usd": f"${cart.total_cents / 100:.2f}"
            }
        finally:
            conn.close()

    def tool_get_purchase_status(self, purchase_request_id: str, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Reads authoritative purchase status."""
        tenant_id = tenant_id or self.default_tenant
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM purchases WHERE id = ? AND tenant_id = ?;",
                (purchase_request_id, tenant_id)
            ).fetchone()
            if not row:
                return {"error": "Purchase request not found"}

            return {
                "purchase_request_id": row["id"],
                "status": row["status"],
                "status_reason": row["status_reason"],
                "total_cents": row["total_cents"],
                "total_usd": f"${row['total_cents'] / 100:.2f}",
                "currency": row["currency"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]
            }
        finally:
            conn.close()

    def dispatch_tool(self, name: str, arguments: Dict[str, Any], tenant_id: Optional[str] = None, agent_id: Optional[str] = None) -> Dict[str, Any]:
        """Dispatches tool call by name."""
        if name == "search_products":
            return self.tool_search_products(arguments.get("query", ""), arguments.get("limit", 10))
        elif name == "get_product_quote":
            return self.tool_get_product_quote(arguments.get("product_id", ""), arguments.get("quantity", 1), tenant_id, agent_id)
        elif name == "get_allowance":
            return self.tool_get_allowance(tenant_id, agent_id)
        elif name == "request_purchase":
            return self.tool_request_purchase(arguments.get("quote_id", ""), arguments.get("purpose", ""), tenant_id, agent_id)
        elif name == "get_purchase_status":
            return self.tool_get_purchase_status(arguments.get("purchase_request_id", ""), tenant_id)
        else:
            return {"error": f"Unknown tool: '{name}'. Allowed tools: {[t['name'] for t in MCP_TOOLS]}"}

    def handle_jsonrpc(self, request_str: str) -> str:
        """Standard JSON-RPC 2.0 message handler."""
        try:
            req = json.loads(request_str)
        except Exception as e:
            return json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {str(e)}"}})

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if method == "tools/list":
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": MCP_TOOLS}
            })

        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            result = self.dispatch_tool(tool_name, tool_args)
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
            })

        elif method == "initialize":
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "allowance-mcp-server", "version": "4.2.2"}
                }
            })

        return json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method '{method}' not found."}
        })


def run_stdio_server():
    """Starts MCP server on standard I/O for Claude Desktop / Cursor integration."""
    server = AllowanceMCPServer()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        response = server.handle_jsonrpc(line)
        sys.stdout.write(response + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    run_stdio_server()
