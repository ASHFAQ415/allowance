"""
Trusted Server-Side Quote Management for Allowance Platform.
Enforces Invariant 6:
- Prices, merchant identities, and totals are computed strictly from trusted catalog data.
- Quotes carry a cryptographic hash and 10-minute expiry.
- Purchase requests bind to quotes, guaranteeing no mid-flight price tampering.
"""

import json
import sqlite3
import time
import uuid
from typing import List, Optional, Tuple

from ..agent.catalog import get_product
from ..policy.rules import CartItem, FrozenCart


QUOTE_TTL_SECONDS = 600.0  # 10 minutes


def create_trusted_quote(
    conn: sqlite3.Connection,
    tenant_id: str,
    agent_id: str,
    raw_items: List[dict],
    currency: str = "USD"
) -> Tuple[str, FrozenCart, float]:
    """
    Creates a server-authorized quote with trusted prices and availability.
    Raises ValueError on invalid product or zero items.
    """
    if not raw_items:
        raise ValueError("Quote must contain at least one line item.")

    cart_items = []
    total_cents = 0

    for it in raw_items:
        pid = it.get("product_id")
        qty = int(it.get("quantity", 1))
        if qty <= 0:
            raise ValueError(f"Invalid quantity {qty} for product '{pid}'.")
        if qty > 50:
            raise ValueError(f"Quantity {qty} exceeds max limit (50).")

        prod = get_product(pid)
        if not prod:
            raise ValueError(f"Product '{pid}' not found in authorized catalog.")

        item = CartItem(
            product_id=prod.id,
            merchant_id=prod.merchant_id,
            name=prod.name,
            unit_price_cents=prod.price_cents,
            quantity=qty
        )
        cart_items.append(item)
        total_cents += prod.price_cents * qty

    # Calculate deterministic hash
    hash_payload = f"{currency}:{total_cents}:" + ":".join(
        f"{i.product_id}x{i.quantity}@{i.unit_price_cents}" for i in cart_items
    )
    import hashlib
    cart_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()

    frozen_cart = FrozenCart(
        items=cart_items,
        total_cents=total_cents,
        cart_hash=cart_hash,
        currency=currency
    )

    quote_id = f"q_{uuid.uuid4().hex[:16]}"
    now = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    expires_at = now + QUOTE_TTL_SECONDS

    cart_data = [
        {
            "product_id": i.product_id,
            "merchant_id": i.merchant_id,
            "name": i.name,
            "unit_price_cents": i.unit_price_cents,
            "quantity": i.quantity,
            "line_total_cents": i.unit_price_cents * i.quantity
        }
        for i in cart_items
    ]

    conn.execute(
        """
        INSERT INTO quotes (
            id, tenant_id, agent_id, cart_json, cart_hash, total_cents,
            currency, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            quote_id,
            tenant_id,
            agent_id,
            json.dumps(cart_data),
            cart_hash,
            total_cents,
            currency,
            expires_at,
            now_iso
        )
    )
    return quote_id, frozen_cart, expires_at


def get_valid_quote(
    conn: sqlite3.Connection,
    quote_id: str,
    tenant_id: str,
    agent_id: str,
    now: Optional[float] = None
) -> Optional[Tuple[FrozenCart, float]]:
    """
    Retrieves and validates a quote.
    Returns (FrozenCart, expires_at) if valid and unexpired; otherwise None.
    """
    now = now or time.time()
    cur = conn.execute(
        """
        SELECT * FROM quotes
        WHERE id = ? AND tenant_id = ? AND agent_id = ?;
        """,
        (quote_id, tenant_id, agent_id)
    )
    row = cur.fetchone()
    if not row:
        return None

    if now > row["expires_at"]:
        return None  # Expired quote

    raw_items = json.loads(row["cart_json"])
    items = [
        CartItem(
            product_id=it["product_id"],
            merchant_id=it["merchant_id"],
            name=it["name"],
            unit_price_cents=it["unit_price_cents"],
            quantity=it["quantity"]
        )
        for it in raw_items
    ]

    cart = FrozenCart(
        items=items,
        total_cents=row["total_cents"],
        cart_hash=row["cart_hash"],
        currency=row["currency"]
    )
    return cart, row["expires_at"]
