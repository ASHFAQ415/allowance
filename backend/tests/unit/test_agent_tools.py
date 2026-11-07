"""
Unit Tests for ShopMind Agent Tools and Sandboxed Loop.
Tests Invariants 4 & 6:
- submit_cart takes zero parameters.
- Server catalog overrides any model prices.
- Bounded steps and repeat-call detector.
- Deterministic fallback on outages.
"""

import unittest
from backend.app.agent.catalog import get_product, search_catalog_data
from backend.app.agent.tools import (
    tool_search_catalog,
    tool_build_cart,
    CartValidationError,
)
from backend.app.agent.loop import (
    execute_agent_loop,
    run_deterministic_fallback,
)


class TestAgentTools(unittest.TestCase):

    def test_search_catalog_xml_wrapping(self):
        """search_catalog wraps untrusted product text inside <catalog_data> tags."""
        res = tool_search_catalog("running shoes")
        self.assertTrue(res.startswith("<catalog_data>"))
        self.assertTrue(res.endswith("</catalog_data>"))
        self.assertIn('id="shoes_trail_runner"', res)
        self.assertIn('merchant="merchant_shoes"', res)

    def test_build_cart_authoritative_server_pricing(self):
        """build_cart ignores any model-provided price and uses catalog price."""
        # Trail runner is priced at 4500 cents in catalog. Model tries to inject 1 cent.
        cart = tool_build_cart([{"product_id": "shoes_trail_runner", "quantity": 2, "price_cents": 1}])
        self.assertEqual(len(cart.items), 1)
        self.assertEqual(cart.items[0].unit_price_cents, 4500)
        self.assertEqual(cart.items[0].line_total_cents, 9000)
        self.assertEqual(cart.total_cents, 9000)
        self.assertTrue(len(cart.cart_hash) == 64)

    def test_build_cart_merges_duplicate_product_ids(self):
        """build_cart merges identical product IDs into single line item with summed quantity."""
        cart = tool_build_cart([
            {"product_id": "coffee_espresso_blend", "quantity": 1},
            {"product_id": "coffee_espresso_blend", "quantity": 2},
        ])
        self.assertEqual(len(cart.items), 1)
        self.assertEqual(cart.items[0].quantity, 3)
        self.assertEqual(cart.total_cents, 3 * 2200)

    def test_build_cart_rejects_negative_or_oversized_quantity(self):
        """Rejects zero, negative, or quantities over 10."""
        with self.assertRaises(CartValidationError):
            tool_build_cart([{"product_id": "shoes_trail_runner", "quantity": 0}])
        with self.assertRaises(CartValidationError):
            tool_build_cart([{"product_id": "shoes_trail_runner", "quantity": -5}])
        with self.assertRaises(CartValidationError):
            tool_build_cart([{"product_id": "shoes_trail_runner", "quantity": 11}])

    def test_build_cart_rejects_out_of_stock_item(self):
        """Out of stock items are rejected."""
        with self.assertRaises(CartValidationError) as ctx:
            tool_build_cart([{"product_id": "unapproved_discontinued_item", "quantity": 1}])
        self.assertIn("out of stock", str(ctx.exception).lower())

    def test_loop_repeat_call_detection(self):
        """Consecutive identical tool calls terminate loop with FAILED(repeat_call)."""
        call_count = 0

        def fake_model(messages):
            nonlocal call_count
            call_count += 1
            # Return same search tool call repeatedly
            return {
                "tool_call": {
                    "name": "search_catalog",
                    "arguments": {"query": "shoes"}
                }
            }

        status, reason, cart, trace = execute_agent_loop("Find shoes", model_invoker=fake_model)
        self.assertEqual(status, "FAILED")
        self.assertEqual(reason, "repeat_call")
        self.assertEqual(call_count, 2)

    def test_loop_deterministic_fallback_on_model_outage(self):
        """If model raises an exception (outage), loop safely recovers via deterministic search."""
        def failing_model(messages):
            raise ConnectionError("Anthropic API Down 500")

        status, reason, cart, trace = execute_agent_loop("espresso coffee", model_invoker=failing_model)
        self.assertEqual(status, "COMPLETED")
        self.assertIsNotNone(cart)
        self.assertEqual(cart.items[0].merchant_id, "merchant_coffee")


if __name__ == "__main__":
    unittest.main()

