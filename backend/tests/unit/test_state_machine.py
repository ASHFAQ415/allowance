"""
Unit Tests for State Machine Transitions and Time Rules.
Tests Suite A.
"""

import unittest
from backend.app.orchestrator.state_machine import (
    PurchaseState,
    STATE_RANKS,
    TERMINAL_STATES,
    LEGAL_TRANSITIONS,
    IllegalTransitionError,
    validate_transition,
    evaluate_time_rules,
    EXPIRY_PENDING_APPROVAL_SECONDS,
    EXPIRY_ORDER_CREATED_SECONDS,
    EXPIRY_BUYER_APPROVED_SECONDS,
    TIMEOUT_CAPTURING_SECONDS,
)


class TestStateMachine(unittest.TestCase):

    def test_all_legal_transitions_succeed(self):
        """Verifies that all declared legal transitions pass validation."""
        for from_state, targets in LEGAL_TRANSITIONS.items():
            for to_state in targets:
                try:
                    validate_transition(from_state, to_state)
                except IllegalTransitionError as e:
                    self.fail(f"Legal transition {from_state} -> {to_state} failed validation: {e}")

    def test_all_illegal_transitions_raise(self):
        """Verifies that any undeclared transition raises IllegalTransitionError."""
        for from_state in PurchaseState:
            allowed = LEGAL_TRANSITIONS.get(from_state, set())
            for to_state in PurchaseState:
                if to_state not in allowed:
                    with self.assertRaises(IllegalTransitionError):
                        validate_transition(from_state, to_state)

    def test_terminal_states_cannot_transition(self):
        """Verifies that terminal states reject all transitions."""
        for term in TERMINAL_STATES:
            for next_state in PurchaseState:
                with self.assertRaises(IllegalTransitionError):
                    validate_transition(term, next_state)

    def test_strictly_monotonic_rank_progression(self):
        """Verifies that rank must strictly increase across all legal transitions."""
        for from_state, targets in LEGAL_TRANSITIONS.items():
            from_rank = STATE_RANKS[from_state]
            for to_state in targets:
                to_rank = STATE_RANKS[to_state]
                self.assertGreater(
                    to_rank, from_rank,
                    f"Transition {from_state} (rank {from_rank}) -> {to_state} (rank {to_rank}) violates monotonicity."
                )

    def test_time_rules_pending_approval_expiry(self):
        """PENDING_APPROVAL expires after 600 seconds."""
        # Not yet expired
        self.assertIsNone(evaluate_time_rules(PurchaseState.PENDING_APPROVAL, 1000.0, 1599.9))
        # Exactly expired
        res = evaluate_time_rules(PurchaseState.PENDING_APPROVAL, 1000.0, 1000.0 + EXPIRY_PENDING_APPROVAL_SECONDS)
        self.assertIsNotNone(res)
        self.assertEqual(res, (PurchaseState.EXPIRED, "pending_approval_timeout"))

    def test_time_rules_order_created_expiry(self):
        """PAYPAL_ORDER_CREATED expires after 900 seconds."""
        res = evaluate_time_rules(PurchaseState.PAYPAL_ORDER_CREATED, 1000.0, 1000.0 + EXPIRY_ORDER_CREATED_SECONDS)
        self.assertEqual(res, (PurchaseState.EXPIRED, "order_approval_timeout"))

    def test_time_rules_buyer_approved_expiry(self):
        """BUYER_APPROVED expires after 900 seconds."""
        res = evaluate_time_rules(PurchaseState.BUYER_APPROVED, 1000.0, 1000.0 + EXPIRY_BUYER_APPROVED_SECONDS)
        self.assertEqual(res, (PurchaseState.EXPIRED, "buyer_approval_timeout"))

    def test_time_rules_capturing_timeout(self):
        """CAPTURING flips to CAPTURE_UNKNOWN after 120 seconds (crash window)."""
        res = evaluate_time_rules(PurchaseState.CAPTURING, 1000.0, 1000.0 + TIMEOUT_CAPTURING_SECONDS)
        self.assertEqual(res, (PurchaseState.CAPTURE_UNKNOWN, "capturing_process_timeout"))


if __name__ == "__main__":
    unittest.main()

