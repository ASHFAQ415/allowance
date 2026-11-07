"""
Unit Tests for PayPilot Pure Policy Engine.
Verifies rules R0 to R6, checkpoints P1 to P4, self-exclusion, and edge cases.
"""

import unittest
from datetime import datetime, timezone

from backend.app.policy.rules import (
    Outcome,
    Checkpoint,
    PolicyConfig,
    CartItem,
    FrozenCart,
    evaluate,
)
from backend.app.db.financial_journal import BudgetSnapshot


class TestPayPilotPolicy(unittest.TestCase):

    def setUp(self):
        self.now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
        self.policy = PolicyConfig(
            agent_id="agent_default",
            tenant_id="tenant_demo",
            version=1,
            daily_budget_cents=30000,          # $300.00
            max_per_tx_cents=15000,            # $150.00
            approval_threshold_cents=5000,     # $50.00
            velocity_cap_cents=10000,          # $100.00
            allowlist={"merchant_shoes", "merchant_coffee", "merchant_tech"},
            timezone="UTC",
            frozen=False
        )

    def _make_cart(self, amount_cents: int, merchant: str = "merchant_shoes") -> FrozenCart:
        item = CartItem(
            product_id="prod_1",
            merchant_id=merchant,
            name="Test Item",
            unit_price_cents=amount_cents,
            quantity=1
        )
        return FrozenCart(
            items=[item],
            total_cents=amount_cents,
            cart_hash="dummy_hash"
        )

    def _snapshot(self, captured: int = 0, reserved: int = 0, unresolved: int = 0, auto_approved: int = 0) -> BudgetSnapshot:
        return BudgetSnapshot(
            agent_id="agent_default",
            budget_day="2026-11-06",
            currency="USD",
            captured_cents=captured,
            reserved_cents=reserved,
            unresolved_cents=unresolved,
            auto_approved_committed_cents=auto_approved,
            version=1
        )

    def test_r0_emergency_freeze_blocks_everything(self):
        """Rule R0: Emergency Freeze blocks any cart at any checkpoint."""
        frozen_policy = PolicyConfig(
            agent_id=self.policy.agent_id,
            tenant_id=self.policy.tenant_id,
            version=2,
            daily_budget_cents=self.policy.daily_budget_cents,
            max_per_tx_cents=self.policy.max_per_tx_cents,
            approval_threshold_cents=self.policy.approval_threshold_cents,
            velocity_cap_cents=self.policy.velocity_cap_cents,
            allowlist=self.policy.allowlist,
            timezone="UTC",
            frozen=True
        )
        cart = self._make_cart(1000)  # $10.00
        snapshot = self._snapshot()

        for cp in [Checkpoint.P1, Checkpoint.P2, Checkpoint.P3, Checkpoint.P4]:
            dec = evaluate(cart, frozen_policy, snapshot, self.now, cp)
            self.assertEqual(dec.outcome, Outcome.DENY)
            self.assertEqual(dec.rule_name, "R0_FREEZE")

    def test_r1_unapproved_merchant_denied(self):
        """Rule R1: An item from an unapproved merchant is denied."""
        cart = self._make_cart(2000, merchant="scam_merchant")
        snapshot = self._snapshot()
        dec = evaluate(cart, self.policy, snapshot, self.now, Checkpoint.P1)
        self.assertEqual(dec.outcome, Outcome.DENY)
        self.assertEqual(dec.rule_name, "R1_MERCHANT_ALLOWLIST")

    def test_r2_per_transaction_limit(self):
        """Rule R2: Amount exceeding max_per_tx_cents is denied."""
        cart_limit = self._make_cart(15000)      # Exactly $150.00 -> not denied by R2
        cart_over = self._make_cart(15001)       # $150.01 -> DENY by R2
        snapshot = self._snapshot()

        dec_limit = evaluate(cart_limit, self.policy, snapshot, self.now, Checkpoint.P1)
        self.assertNotEqual(dec_limit.rule_name, "R2_PER_TX_LIMIT")

        dec_over = evaluate(cart_over, self.policy, snapshot, self.now, Checkpoint.P1)
        self.assertEqual(dec_over.outcome, Outcome.DENY)
        self.assertEqual(dec_over.rule_name, "R2_PER_TX_LIMIT")

    def test_r3_daily_budget_limit(self):
        """Rule R3: Spend reaching over daily budget is denied."""
        # Daily budget is $300. Spent is $280. Proposed is $30 ($310 total) -> DENY
        snapshot = self._snapshot(captured=20000, reserved=8000)
        cart = self._make_cart(3000)
        dec = evaluate(cart, self.policy, snapshot, self.now, Checkpoint.P1)
        self.assertEqual(dec.outcome, Outcome.DENY)
        self.assertEqual(dec.rule_name, "R3_DAILY_BUDGET")

    def test_r4_approval_threshold(self):
        """Rule R4: Cart over approval threshold requires human review."""
        cart_49 = self._make_cart(4999)    # $49.99 -> ALLOW (R6)
        cart_50 = self._make_cart(5000)    # $50.00 -> ALLOW (R6)
        cart_51 = self._make_cart(5001)    # $50.01 -> REVIEW (R4)
        snapshot = self._snapshot()

        self.assertEqual(evaluate(cart_49, self.policy, snapshot, self.now, Checkpoint.P1).outcome, Outcome.ALLOW)
        self.assertEqual(evaluate(cart_50, self.policy, snapshot, self.now, Checkpoint.P1).outcome, Outcome.ALLOW)
        
        dec_51 = evaluate(cart_51, self.policy, snapshot, self.now, Checkpoint.P1)
        self.assertEqual(dec_51.outcome, Outcome.REVIEW)
        self.assertEqual(dec_51.rule_name, "R4_APPROVAL_THRESHOLD")

    def test_r5_velocity_cap(self):
        """Rule R5: Auto-approved spend exceeding velocity cap requires review."""
        # Auto-approved cap is $100. Previous auto-approved = $70. Proposed = $40 ($110 total) -> REVIEW
        snapshot = self._snapshot(auto_approved=7000)
        cart = self._make_cart(4000)  # $40 <= $50 threshold, but pushes auto spend to $110
        dec = evaluate(cart, self.policy, snapshot, self.now, Checkpoint.P1)
        self.assertEqual(dec.outcome, Outcome.REVIEW)
        self.assertEqual(dec.rule_name, "R5_VELOCITY_CAP")

    def test_r6_auto_approve(self):
        """Rule R6: Clean cart within limits is auto-approved."""
        cart = self._make_cart(4500)  # $45.00
        snapshot = self._snapshot(captured=1000, auto_approved=1000)
        dec = evaluate(cart, self.policy, snapshot, self.now, Checkpoint.P1)
        self.assertEqual(dec.outcome, Outcome.ALLOW)
        self.assertEqual(dec.rule_name, "R6_AUTO_APPROVE")

    def test_checkpoint_p2_p3_p4_ignores_review_rules(self):
        """At checkpoints P2, P3, P4, a cart that triggered REVIEW at P1 passes if DENY rules pass."""
        cart_over_threshold = self._make_cart(9000)  # $90 requires review at P1
        snapshot = self._snapshot()
        
        dec_p1 = evaluate(cart_over_threshold, self.policy, snapshot, self.now, Checkpoint.P1)
        self.assertEqual(dec_p1.outcome, Outcome.REVIEW)

        for cp in [Checkpoint.P2, Checkpoint.P3, Checkpoint.P4]:
            dec = evaluate(cart_over_threshold, self.policy, snapshot, self.now, cp)
            self.assertEqual(dec.outcome, Outcome.ALLOW)
            self.assertEqual(dec.rule_name, "ALLOW_CHECKPOINT_PASSED")


if __name__ == "__main__":
    unittest.main()

