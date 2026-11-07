"""
PayPilot Deterministic Financial Policy Engine.
Pure functional decision engine implementing rules R0 through R6 across checkpoints P1 through P4.
Zero I/O, zero network, zero system clock reads.
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Set, Optional
from datetime import datetime


class Outcome(str, Enum):
    ALLOW = "ALLOW"
    REVIEW = "REVIEW"
    DENY = "DENY"


class Checkpoint(str, Enum):
    P1 = "P1"  # Cart submission (Full R0-R6)
    P2 = "P2"  # Admin approval click (R0-R3 only)
    P3 = "P3"  # Pre-PayPal Order create (R0-R3 only)
    P4 = "P4"  # Capture claim post-buyer approval (R0-R3 only)


@dataclass(frozen=True)
class PolicyConfig:
    agent_id: str
    tenant_id: str
    version: int
    daily_budget_cents: int
    max_per_tx_cents: int
    approval_threshold_cents: int
    velocity_cap_cents: int
    allowlist: Set[str]
    timezone: str
    frozen: bool = False


@dataclass(frozen=True)
class CartItem:
    product_id: str
    merchant_id: str
    name: str
    unit_price_cents: int
    quantity: int

    @property
    def line_total_cents(self) -> int:
        return self.unit_price_cents * self.quantity


@dataclass(frozen=True)
class FrozenCart:
    items: List[CartItem]
    total_cents: int
    cart_hash: str
    currency: str = "USD"


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    rule_name: str
    reason: str
    checkpoint: Checkpoint
    policy_version: int
    spent_cents: int
    proposed_cents: int


def evaluate(
    cart: FrozenCart,
    policy: PolicyConfig,
    budget_snapshot,  # BudgetSnapshot from db.financial_journal
    now: datetime,
    checkpoint: Checkpoint
) -> Decision:
    """
    Pure evaluation of financial authorization rules.
    Evaluates rules in strict priority: R0 -> R1 -> R2 -> R3 -> R4 -> R5 -> R6.
    """
    proposed_cents = cart.total_cents
    committed_cents = budget_snapshot.r3_committed_cents
    auto_spent_cents = budget_snapshot.auto_approved_committed_cents

    # R0: Emergency Freeze (Active Kill Switch)
    if policy.frozen:
        return Decision(
            outcome=Outcome.DENY,
            rule_name="R0_FREEZE",
            reason="Emergency spending freeze is ACTIVE",
            checkpoint=checkpoint,
            policy_version=policy.version,
            spent_cents=committed_cents,
            proposed_cents=proposed_cents
        )

    # R1: Merchant Allowlist
    for item in cart.items:
        if item.merchant_id not in policy.allowlist:
            return Decision(
                outcome=Outcome.DENY,
                rule_name="R1_MERCHANT_ALLOWLIST",
                reason=f"Merchant '{item.merchant_id}' is not in approved allowlist",
                checkpoint=checkpoint,
                policy_version=policy.version,
                spent_cents=committed_cents,
                proposed_cents=proposed_cents
            )

    # R2: Per-Transaction Limit
    if proposed_cents > policy.max_per_tx_cents:
        return Decision(
            outcome=Outcome.DENY,
            rule_name="R2_PER_TX_LIMIT",
            reason=f"Total ${proposed_cents/100:.2f} exceeds per-transaction limit ${policy.max_per_tx_cents/100:.2f}",
            checkpoint=checkpoint,
            policy_version=policy.version,
            spent_cents=committed_cents,
            proposed_cents=proposed_cents
        )

    # R3: Daily Budget
    if committed_cents + proposed_cents > policy.daily_budget_cents:
        return Decision(
            outcome=Outcome.DENY,
            rule_name="R3_DAILY_BUDGET",
            reason=f"Daily spend would reach ${(committed_cents + proposed_cents)/100:.2f}, exceeding budget ${policy.daily_budget_cents/100:.2f}",
            checkpoint=checkpoint,
            policy_version=policy.version,
            spent_cents=committed_cents,
            proposed_cents=proposed_cents
        )

    # For Checkpoints P2, P3, P4: If DENY did not fire, purchase is authorized to proceed
    if checkpoint in (Checkpoint.P2, Checkpoint.P3, Checkpoint.P4):
        return Decision(
            outcome=Outcome.ALLOW,
            rule_name="ALLOW_CHECKPOINT_PASSED",
            reason=f"Checkpoint {checkpoint.value} re-authorization passed",
            checkpoint=checkpoint,
            policy_version=policy.version,
            spent_cents=committed_cents,
            proposed_cents=proposed_cents
        )

    # Checkpoint P1 only: Human review threshold & velocity rules
    # R4: Approval Threshold
    if proposed_cents > policy.approval_threshold_cents:
        return Decision(
            outcome=Outcome.REVIEW,
            rule_name="R4_APPROVAL_THRESHOLD",
            reason=f"Amount ${proposed_cents/100:.2f} requires human approval (threshold: ${policy.approval_threshold_cents/100:.2f})",
            checkpoint=checkpoint,
            policy_version=policy.version,
            spent_cents=committed_cents,
            proposed_cents=proposed_cents
        )

    # R5: Auto-Approve Velocity Limit (subset metric of R3)
    if auto_spent_cents + proposed_cents > policy.velocity_cap_cents:
        return Decision(
            outcome=Outcome.REVIEW,
            rule_name="R5_VELOCITY_CAP",
            reason=f"Auto-approved velocity would reach ${(auto_spent_cents + proposed_cents)/100:.2f}, exceeding cap ${policy.velocity_cap_cents/100:.2f}",
            checkpoint=checkpoint,
            policy_version=policy.version,
            spent_cents=auto_spent_cents,
            proposed_cents=proposed_cents
        )

    # R6: Default Auto-Approve
    return Decision(
        outcome=Outcome.ALLOW,
        rule_name="R6_AUTO_APPROVE",
        reason="Cart meets all automated allowance criteria",
        checkpoint=checkpoint,
        policy_version=policy.version,
        spent_cents=committed_cents,
        proposed_cents=proposed_cents
    )

