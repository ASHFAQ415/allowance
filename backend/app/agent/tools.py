"""
ShopMind Agent Tools Contract.
Adheres strictly to Invariant 6: AI and Payment Authority Separation.
- Catalog text is untrusted input formatted inside <catalog_data> delimiters.
- build_cart recomputes prices from the server catalog, discarding any model-set prices.
- submit_cart accepts ZERO parameters to prevent model tampering during cart submission.
"""

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .catalog import get_product, search_catalog_data
from ..policy.rules import CartItem, FrozenCart


class CartValidationError(ValueError):
    """Raised when proposed cart items fail schema, bounds, or availability checks."""
    pass


PRODUCT_ID_REGEX = re.compile(r"^[a-z0-9_-]{1,40}$")
MAX_LINE_ITEMS = 8
MAX_QUANTITY_PER_ITEM = 10
MAX_QUERY_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 300


def tool_search_catalog(query: str) -> str:
    """
    Tool: search_catalog(query: str) -> str
    Searches products and wraps results in untrusted <catalog_data> XML block.
    """
    clean_query = str(query)[:MAX_QUERY_LENGTH].strip()
    if not clean_query:
        return "<catalog_data>\nNo query provided.\n</catalog_data>"

    products = search_catalog_data(clean_query, limit=10)
    if not products:
        return f"<catalog_data>\nNo products matching '{clean_query}'.\n</catalog_data>"

    xml_lines = ["<catalog_data>"]
    for p in products:
        desc = p.description[:MAX_DESCRIPTION_LENGTH]
        stock_str = "IN_STOCK" if p.in_stock else "OUT_OF_STOCK"
        xml_lines.append(
            f'  <product id="{p.id}" merchant="{p.merchant_id}" price_cents="{p.price_cents}" status="{stock_str}">'
        )
        xml_lines.append(f"    <name>{p.name}</name>")
        xml_lines.append(f"    <description>{desc}</description>")
        xml_lines.append("  </product>")
    xml_lines.append("</catalog_data>")

    return "\n".join(xml_lines)


def tool_build_cart(raw_items: List[Dict[str, Any]]) -> FrozenCart:
    """
    Tool: build_cart(items: list[{"product_id": str, "quantity": int}]) -> FrozenCart
    Validates items, looks up authoritative server catalog prices, merges duplicate product IDs,
    enforces item/quantity bounds, and computes a stable SHA-256 cart_hash.
    """
    if not isinstance(raw_items, list) or len(raw_items) == 0:
        raise CartValidationError("Cart must contain at least 1 line item.")

    if len(raw_items) > MAX_LINE_ITEMS:
        raise CartValidationError(f"Cart exceeds maximum {MAX_LINE_ITEMS} line items.")

    # 1. Merge duplicate product IDs
    merged_quantities: Dict[str, int] = {}
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise CartValidationError("Invalid item format in cart; dictionary expected.")

        pid = str(entry.get("product_id", "")).strip()
        if not PRODUCT_ID_REGEX.match(pid):
            raise CartValidationError(f"Invalid product_id '{pid}'. Must match regex {PRODUCT_ID_REGEX.pattern}")

        try:
            qty = int(entry.get("quantity", 1))
        except (ValueError, TypeError):
            raise CartValidationError(f"Invalid quantity for product '{pid}'. Integer expected.")

        if qty <= 0:
            raise CartValidationError(f"Quantity for product '{pid}' must be positive.")

        merged_quantities[pid] = merged_quantities.get(pid, 0) + qty

    # 2. Build verified CartItem objects from server catalog
    verified_items: List[CartItem] = []
    total_cents = 0

    for pid, qty in merged_quantities.items():
        if qty > MAX_QUANTITY_PER_ITEM:
            raise CartValidationError(f"Quantity {qty} for product '{pid}' exceeds max limit {MAX_QUANTITY_PER_ITEM}.")

        product = get_product(pid)
        if product is None:
            raise CartValidationError(f"Product '{pid}' does not exist in catalog.")

        if not product.in_stock:
            raise CartValidationError(f"Product '{pid}' ({product.name}) is currently out of stock.")

        # Server catalog price is authoritative! Any client-provided price is discarded.
        item = CartItem(
            product_id=product.id,
            merchant_id=product.merchant_id,
            name=product.name,
            unit_price_cents=product.price_cents,
            quantity=qty
        )
        verified_items.append(item)
        total_cents += item.line_total_cents

    # 3. Compute deterministic cart hash
    hash_payload = json.dumps(
        [
            {
                "product_id": it.product_id,
                "merchant_id": it.merchant_id,
                "unit_price_cents": it.unit_price_cents,
                "quantity": it.quantity,
            }
            for it in sorted(verified_items, key=lambda x: x.product_id)
        ],
        sort_keys=True
    )
    cart_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()

    return FrozenCart(
        items=verified_items,
        total_cents=total_cents,
        cart_hash=cart_hash,
        currency="USD"
    )

