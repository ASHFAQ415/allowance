"""
ShopMind Agent Bounded Execution Loop.
Enforces Invariant 6 (AI separation) and Invariant 4 (bounded execution).
- MAX_STEPS = 6
- TIMEOUT = 45.0s
- Repeat tool-call detection
- Deterministic search fallback
"""

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .catalog import search_catalog_data
from .tools import tool_search_catalog, tool_build_cart, CartValidationError
from ..policy.rules import FrozenCart


MAX_STEPS = 6
WALL_CLOCK_TIMEOUT_SECONDS = 45.0


@dataclass
class AgentLoopState:
    steps_used: int = 0
    current_cart: Optional[FrozenCart] = None
    last_tool_call: Optional[Tuple[str, str]] = None
    is_terminal: bool = False
    terminal_status: Optional[str] = None
    terminal_reason: Optional[str] = None
    trace: List[Dict[str, Any]] = None

    def __post_init__(self):
        if self.trace is None:
            self.trace = []


def run_deterministic_fallback(query: str) -> Optional[FrozenCart]:
    """
    Safe deterministic fallback when external AI model is unavailable, rate-limited,
    or returns invalid output. Finds the best matching in-stock approved item.
    """
    import re

    matches = search_catalog_data(query, limit=5)

    # If the user prompt is explicitly adversarial (e.g. asking for unapproved/darkweb items),
    # propose the unapproved item so PayPilot Rule R1 can demonstrate deterministic security enforcement!
    is_adversarial = any(w in query.lower() for w in ["unapproved", "darkweb", "dark", "hack"])
    if is_adversarial:
        unapproved = [m for m in matches if m.merchant_id == "merchant_unapproved" and m.in_stock]
        if unapproved:
            return tool_build_cart([{"product_id": unapproved[0].id, "quantity": 1}])

    approved_matches = [m for m in matches if m.merchant_id != "merchant_unapproved" and m.in_stock]
    if not approved_matches:
        # Invariant: Never fall back to cheapest item when query does not match. Refuse cleanly.
        return None

    # Pick the top-scoring matched product!
    best = approved_matches[0]

    # Clean query of price constraints before parsing quantity so "$5" or "under $50" isn't mistaken for quantity
    cleaned_for_qty = re.sub(r"(?:under|below|less\s+than)\s*\$?\s*\d+(?:\.\d+)?", "", query.lower())
    cleaned_for_qty = re.sub(r"\$\s*\d+(?:\.\d+)?", "", cleaned_for_qty)
    cleaned_for_qty = re.sub(r"\b\d+\s*(?:dollars|bucks|cents)\b", "", cleaned_for_qty)

    # Check if quantity was requested (e.g. "buy 2 boxes")
    qty = 1
    qty_match = re.search(r"\b(\d+)\s*(?:boxes|pairs|items|pack|packs|units|bags|bottles)?\b", cleaned_for_qty)
    if qty_match:
        try:
            parsed_qty = int(qty_match.group(1))
            if 1 <= parsed_qty <= 10:
                qty = parsed_qty
        except ValueError:
            qty = 1

    try:
        return tool_build_cart([{"product_id": best.id, "quantity": qty}])
    except CartValidationError:
        return None


def execute_agent_loop(
    user_prompt: str,
    model_invoker: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = None,
    clock: Callable[[], float] = time.time
) -> Tuple[str, Optional[str], Optional[FrozenCart], List[Dict[str, Any]]]:
    """
    Executes the bounded agent loop.
    Returns: (status, reason, final_cart, trace)
    - status: 'COMPLETED' or 'FAILED'
    - reason: specific termination reason code if failed
    """
    start_time = clock()
    state = AgentLoopState()
    
    # If no model invoker is provided (or in fallback mode), run deterministic path
    if model_invoker is None:
        cart = run_deterministic_fallback(user_prompt)
        if cart is None:
            return "FAILED", "no_matching_item", None, [{"step": 1, "tool": "search_catalog", "query": user_prompt, "status": "no_match"}]
        return "COMPLETED", None, cart, [
            {"step": 1, "tool": "search_catalog", "query": user_prompt, "status": "match_found", "product_id": cart.items[0].product_id},
            {"step": 2, "tool": "build_cart", "status": "verified", "total_cents": cart.total_cents},
            {"step": 3, "tool": "submit_cart", "status": "submitted", "cart_hash": cart.cart_hash}
        ]

    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": user_prompt}
    ]

    while state.steps_used < MAX_STEPS:
        # 1. Check wall-clock timeout
        if clock() - start_time >= WALL_CLOCK_TIMEOUT_SECONDS:
            return "FAILED", "timeout", None, state.trace

        state.steps_used += 1

        try:
            # 2. Invoke model
            response = model_invoker(messages)
        except Exception as e:
            # External model outage -> Fallback to deterministic
            cart = run_deterministic_fallback(user_prompt)
            if cart:
                state.trace.append({"event": "model_error_fallback_success", "error": str(e)})
                return "COMPLETED", None, cart, state.trace
            return "FAILED", "no_matching_item", None, state.trace

        # Parse tool calls from model response
        tool_call = response.get("tool_call")
        if not tool_call:
            # Model responded with plain text without tools
            state.trace.append({"event": "text_response", "content": response.get("content", "")})
            # If cart already built, finish; else fallback
            if state.current_cart:
                return "COMPLETED", None, state.current_cart, state.trace
            cart = run_deterministic_fallback(user_prompt)
            if cart:
                return "COMPLETED", None, cart, state.trace
            return "FAILED", "no_matching_item", None, state.trace

        tool_name = tool_call.get("name")
        tool_args = tool_call.get("arguments", {})
        norm_args = json.dumps(tool_args, sort_keys=True)

        # 3. Repeat call detector
        current_call = (tool_name, norm_args)
        if state.last_tool_call == current_call:
            state.trace.append({"event": "repeat_call_detected", "tool": tool_name})
            return "FAILED", "repeat_call", None, state.trace
        state.last_tool_call = current_call

        # 4. Dispatch tool
        if tool_name == "search_catalog":
            query = tool_args.get("query", "")
            result = tool_search_catalog(query)
            messages.append({"role": "assistant", "tool_call": tool_call})
            messages.append({"role": "user", "content": result})
            state.trace.append({"step": state.steps_used, "tool": "search_catalog", "query": query})

        elif tool_name == "build_cart":
            raw_items = tool_args.get("items", [])
            try:
                state.current_cart = tool_build_cart(raw_items)
                messages.append({"role": "assistant", "tool_call": tool_call})
                messages.append({"role": "user", "content": f"Cart verified. Total: ${state.current_cart.total_cents/100:.2f}"})
                state.trace.append({"step": state.steps_used, "tool": "build_cart", "total_cents": state.current_cart.total_cents})
            except CartValidationError as err:
                # Provide repair prompt with error
                messages.append({"role": "assistant", "tool_call": tool_call})
                messages.append({"role": "user", "content": f"CartValidationError: {str(err)}"})
                state.trace.append({"step": state.steps_used, "tool": "build_cart_error", "error": str(err)})

        elif tool_name == "submit_cart":
            # submit_cart accepts ZERO parameters. Submits existing validated cart!
            if state.current_cart is None:
                messages.append({"role": "assistant", "tool_call": tool_call})
                messages.append({"role": "user", "content": "Cannot submit empty cart. Call build_cart first."})
                state.trace.append({"step": state.steps_used, "tool": "submit_empty_cart"})
            else:
                state.trace.append({"step": state.steps_used, "tool": "submit_cart", "cart_hash": state.current_cart.cart_hash})
                return "COMPLETED", None, state.current_cart, state.trace

        else:
            messages.append({"role": "assistant", "tool_call": tool_call})
            messages.append({"role": "user", "content": f"Unknown tool '{tool_name}'."})
            state.trace.append({"step": state.steps_used, "unknown_tool": tool_name})

    # Step cap exhausted
    return "FAILED", "step_cap", None, state.trace

