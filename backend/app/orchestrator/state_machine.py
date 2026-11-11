"""
Purchase Workflow State Machine.
Enforces forward-only monotonic rank transitions, terminal state immutability,
and time-rule expirations.
"""

from enum import Enum
from typing import Dict, Optional, Set, Tuple


class PurchaseState(str, Enum):
    CREATED = "CREATED"
    CART_READY = "CART_READY"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    AUTO_APPROVED = "AUTO_APPROVED"
    APPROVED = "APPROVED"
    PAYPAL_ORDER_CREATED = "PAYPAL_ORDER_CREATED"
    BUYER_APPROVED = "BUYER_APPROVED"
    CAPTURING = "CAPTURING"
    CAPTURE_UNKNOWN = "CAPTURE_UNKNOWN"
    CAPTURED = "CAPTURED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


# Monotonic State Ranks
STATE_RANKS: Dict[PurchaseState, int] = {
    PurchaseState.CREATED: 0,
    PurchaseState.CART_READY: 1,
    PurchaseState.PENDING_APPROVAL: 2,
    PurchaseState.AUTO_APPROVED: 3,
    PurchaseState.APPROVED: 3,
    PurchaseState.PAYPAL_ORDER_CREATED: 4,
    PurchaseState.BUYER_APPROVED: 5,
    PurchaseState.CAPTURING: 6,
    PurchaseState.CAPTURE_UNKNOWN: 7,
    PurchaseState.CAPTURED: 9,
    PurchaseState.BLOCKED: 9,
    PurchaseState.REJECTED: 9,
    PurchaseState.EXPIRED: 9,
    PurchaseState.FAILED: 9,
}

TERMINAL_STATES: Set[PurchaseState] = {
    PurchaseState.CAPTURED,
    PurchaseState.BLOCKED,
    PurchaseState.REJECTED,
    PurchaseState.EXPIRED,
    PurchaseState.FAILED,
}

ACTIVE_RESERVATION_STATES: Set[PurchaseState] = {
    PurchaseState.PENDING_APPROVAL,
    PurchaseState.AUTO_APPROVED,
    PurchaseState.APPROVED,
    PurchaseState.PAYPAL_ORDER_CREATED,
    PurchaseState.BUYER_APPROVED,
    PurchaseState.CAPTURING,
    PurchaseState.CAPTURE_UNKNOWN,
}

# Legal forward transitions
LEGAL_TRANSITIONS: Dict[PurchaseState, Set[PurchaseState]] = {
    PurchaseState.CREATED: {
        PurchaseState.CART_READY,
        PurchaseState.FAILED
    },
    PurchaseState.CART_READY: {
        PurchaseState.AUTO_APPROVED,
        PurchaseState.PENDING_APPROVAL,
        PurchaseState.BLOCKED,
        PurchaseState.FAILED
    },
    PurchaseState.PENDING_APPROVAL: {
        PurchaseState.APPROVED,
        PurchaseState.REJECTED,
        PurchaseState.EXPIRED,
        PurchaseState.BLOCKED
    },
    PurchaseState.AUTO_APPROVED: {
        PurchaseState.PAYPAL_ORDER_CREATED,
        PurchaseState.BLOCKED,
        PurchaseState.FAILED
    },
    PurchaseState.APPROVED: {
        PurchaseState.PAYPAL_ORDER_CREATED,
        PurchaseState.BLOCKED,
        PurchaseState.FAILED
    },
    PurchaseState.PAYPAL_ORDER_CREATED: {
        PurchaseState.BUYER_APPROVED,
        PurchaseState.EXPIRED,
        PurchaseState.BLOCKED,
        PurchaseState.FAILED
    },
    PurchaseState.BUYER_APPROVED: {
        PurchaseState.CAPTURING,
        PurchaseState.BLOCKED,
        PurchaseState.EXPIRED,
        PurchaseState.FAILED
    },
    PurchaseState.CAPTURING: {
        PurchaseState.CAPTURED,
        PurchaseState.CAPTURE_UNKNOWN,
        PurchaseState.FAILED
    },
    PurchaseState.CAPTURE_UNKNOWN: {
        PurchaseState.CAPTURED,
        PurchaseState.FAILED
    },
    # Terminal states have empty target sets
    PurchaseState.CAPTURED: set(),
    PurchaseState.BLOCKED: set(),
    PurchaseState.REJECTED: set(),
    PurchaseState.EXPIRED: set(),
    PurchaseState.FAILED: set(),
}

# Timeouts in seconds
EXPIRY_PENDING_APPROVAL_SECONDS = 600.0   # 10 minutes
EXPIRY_ORDER_CREATED_SECONDS = 900.0      # 15 minutes
EXPIRY_BUYER_APPROVED_SECONDS = 900.0     # 15 minutes
TIMEOUT_CAPTURING_SECONDS = 120.0         # 2 minutes crash window


class IllegalTransitionError(ValueError):
    """Raised when an illegal or retrograde state transition is attempted."""
    pass


def validate_transition(from_state: PurchaseState, to_state: PurchaseState) -> None:
    """
    Validates that a transition from `from_state` to `to_state` is legal and monotonic.
    Raises IllegalTransitionError if disallowed.
    """
    if from_state in TERMINAL_STATES:
        raise IllegalTransitionError(
            f"Cannot transition from terminal state {from_state.value} to {to_state.value}."
        )

    allowed = LEGAL_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        raise IllegalTransitionError(
            f"Illegal transition from {from_state.value} to {to_state.value}. Allowed: {[s.value for s in allowed]}"
        )

    from_rank = STATE_RANKS[from_state]
    to_rank = STATE_RANKS[to_state]
    if to_rank <= from_rank:
        raise IllegalTransitionError(
            f"Monotonic rank violation: rank({to_state.value})={to_rank} must be strictly greater than rank({from_state.value})={from_rank}."
        )


def evaluate_time_rules(
    current_state: PurchaseState,
    state_entered_at: float,
    now: float
) -> Optional[Tuple[PurchaseState, str]]:
    """
    Pure evaluation of time-based expirations.
    Returns (next_state, reason) if expired, or None if state remains active.
    """
    elapsed = now - state_entered_at

    if current_state == PurchaseState.PENDING_APPROVAL:
        if elapsed >= EXPIRY_PENDING_APPROVAL_SECONDS:
            return PurchaseState.EXPIRED, "pending_approval_timeout"

    elif current_state == PurchaseState.PAYPAL_ORDER_CREATED:
        if elapsed >= EXPIRY_ORDER_CREATED_SECONDS:
            return PurchaseState.EXPIRED, "order_approval_timeout"

    elif current_state == PurchaseState.BUYER_APPROVED:
        if elapsed >= EXPIRY_BUYER_APPROVED_SECONDS:
            return PurchaseState.EXPIRED, "buyer_approval_timeout"

    elif current_state == PurchaseState.CAPTURING:
        if elapsed >= TIMEOUT_CAPTURING_SECONDS:
            return PurchaseState.CAPTURE_UNKNOWN, "capturing_process_timeout"

    return None

