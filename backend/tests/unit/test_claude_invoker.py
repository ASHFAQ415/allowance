"""
Unit Tests for Claude 3.5 Sonnet Tool-Use Invoker (Phase 2).
Executes entirely OFFLINE without network access using scripted transports.

Verifies:
1. Scripted tool_use sequence (search -> build -> submit) produces the expected validated cart.
2. Malformed model responses trigger fallback safely.
3. Prompt injection inside catalog descriptions cannot alter server-signed prices.
4. Transport/API exceptions trigger deterministic fallback cleanly.
5. Message format translation produces valid Anthropic tool_use and tool_result blocks.
"""

import json
import unittest
from backend.app.agent.loop import execute_agent_loop
from backend.app.llm.claude_invoker import (
    create_claude_invoker,
    format_messages_for_anthropic,
    parse_anthropic_response,
    SYSTEM_PROMPT,
    CLAUDE_TOOLS
)


class TestClaudeInvokerOffline(unittest.TestCase):
    def test_happy_path_scripted_tool_use_sequence(self):
        """A fake client returns scripted search -> build -> submit tool sequence."""
        call_count = 0

        def scripted_transport(payload):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Step 1: Claude calls search_catalog
                return {
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "search_catalog",
                        "input": {"query": "trail shoes"}
                    }]
                }
            elif call_count == 2:
                # Step 2: Claude calls build_cart with product and qty
                return {
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "build_cart",
                        "input": {"items": [{"product_id": "shoes_trail_runner", "quantity": 1}]}
                    }]
                }
            elif call_count == 3:
                # Step 3: Claude calls submit_cart with zero arguments
                return {
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu_3",
                        "name": "submit_cart",
                        "input": {}
                    }]
                }
            return {"content": [{"type": "text", "text": "All set!"}]}

        invoker = create_claude_invoker(api_key="mock_key", transport=scripted_transport)
        status, reason, cart, trace = execute_agent_loop("Find me trail running shoes", model_invoker=invoker)

        self.assertEqual(status, "COMPLETED")
        self.assertIsNone(reason)
        self.assertIsNotNone(cart)
        self.assertEqual(cart.total_cents, 4500)
        self.assertEqual(cart.items[0].product_id, "shoes_trail_runner")
        self.assertEqual(cart.items[0].quantity, 1)

    def test_malformed_response_triggers_deterministic_fallback(self):
        """Malformed JSON or missing content triggers fallback."""
        def bad_transport(payload):
            return {"invalid_key": "bad_data"}  # missing "content" list

        invoker = create_claude_invoker(api_key="mock_key", transport=bad_transport)
        status, reason, cart, trace = execute_agent_loop("Find me trail running shoes", model_invoker=invoker)

        self.assertEqual(status, "COMPLETED")
        self.assertIsNotNone(cart)
        self.assertEqual(cart.items[0].product_id, "shoes_trail_runner")
        # Trace records the fallback occurrence
        event_names = [t.get("event") for t in trace if "event" in t]
        self.assertIn("model_error_fallback_success", event_names)

    def test_transport_exception_triggers_fallback(self):
        """A network/HTTP exception raised by transport triggers fallback."""
        def error_transport(payload):
            raise TimeoutError("Anthropic endpoint timed out after 20.0s")

        invoker = create_claude_invoker(api_key="mock_key", transport=error_transport)
        status, reason, cart, trace = execute_agent_loop("trail shoes under $50", model_invoker=invoker)

        self.assertEqual(status, "COMPLETED")
        self.assertIsNotNone(cart)
        self.assertEqual(cart.items[0].product_id, "shoes_trail_runner")

    def test_prompt_injection_in_catalog_cannot_change_price(self):
        """Prompt injection attempting to inject cheap price in tool input has no effect."""
        call_count = 0

        def injection_transport(payload):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Malicious agent tries to pass custom price parameter
                return {
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu_inj",
                        "name": "build_cart",
                        "input": {
                            "items": [{"product_id": "shoes_trail_runner", "quantity": 1}],
                            "custom_price_cents": 100,  # Attacker tries to force $1.00
                            "override_discount": True
                        }
                    }]
                }
            elif call_count == 2:
                return {
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu_sub",
                        "name": "submit_cart",
                        "input": {}
                    }]
                }
            return {"content": []}

        invoker = create_claude_invoker(api_key="mock_key", transport=injection_transport)
        status, reason, cart, trace = execute_agent_loop("shoes", model_invoker=invoker)

        self.assertEqual(status, "COMPLETED")
        self.assertIsNotNone(cart)
        # Server-side pricing is strictly preserved at $45.00
        self.assertEqual(cart.total_cents, 4500)
        self.assertNotEqual(cart.total_cents, 100)

    def test_message_formatting_translates_tool_results_properly(self):
        """Format translation creates tool_use in assistant message and tool_result in subsequent user message."""
        loop_messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "tool_call": {"name": "search_catalog", "arguments": {"query": "shoes"}}},
            {"role": "user", "content": "Found 1 shoes"}
        ]
        anthropic_msgs = format_messages_for_anthropic(loop_messages)
        self.assertEqual(len(anthropic_msgs), 3)

        # Message 1: simple user string
        self.assertEqual(anthropic_msgs[0]["role"], "user")
        self.assertEqual(anthropic_msgs[0]["content"], "hello")

        # Message 2: assistant with tool_use block
        self.assertEqual(anthropic_msgs[1]["role"], "assistant")
        tool_use = anthropic_msgs[1]["content"][0]
        self.assertEqual(tool_use["type"], "tool_use")
        self.assertEqual(tool_use["name"], "search_catalog")
        tool_id = tool_use["id"]

        # Message 3: user with tool_result matching the tool_id
        self.assertEqual(anthropic_msgs[2]["role"], "user")
        tool_result = anthropic_msgs[2]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], tool_id)
        self.assertEqual(tool_result["content"], "Found 1 shoes")


if __name__ == "__main__":
    unittest.main()

