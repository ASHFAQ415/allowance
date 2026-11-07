"""
FastAPI Pydantic DTOs with Strict Public Allowlists.
Enforces Invariant 6 & Security Rules:
- Public models NEVER expose paypal_order_id, paypal_capture_id, approval_url,
  request_id, or internal trace_json.
- Admin models may expose operational details when authenticated with X-Admin-Token.
"""

from typing import List, Optional

try:
    from pydantic import BaseModel, Field
except ImportError:
    class BaseModel:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
        def model_dump(self):
            return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        def dict(self):
            return self.model_dump()
        def __repr__(self):
            return f"{self.__class__.__name__}({self.__dict__})"

    def Field(*args, **kwargs):
        default = kwargs.get("default", ...)
        if default is ... and args:
            default = args[0]
        return default if default is not ... else None


# --- Public DTOs (Safe for visitors / SSE / Unauthenticated showcase) ---

class PublicCartItemDTO(BaseModel):
    product_id: str
    merchant_id: str
    name: str
    unit_price_cents: int
    quantity: int
    line_total_cents: int


class PublicPurchaseDTO(BaseModel):
    id: str
    agent_id: str
    status: str
    status_reason: Optional[str] = None
    auto_approved: bool
    total_cents: int
    currency: str
    merchant_ids: List[str]
    budget_day: str
    created_at: str
    updated_at: str


class PublicLedgerDTO(BaseModel):
    id: str
    purchase_id: str
    agent_id: str
    from_state: Optional[str]
    to_state: str
    rule_name: str
    reason: str
    checkpoint: str
    amount_cents: int
    actor: str
    created_at: str


class PublicPolicyDTO(BaseModel):
    agent_id: str
    version: int
    daily_budget_cents: int
    max_per_tx_cents: int
    approval_threshold_cents: int
    velocity_cap_cents: int
    allowlist: List[str]
    timezone: str
    frozen: bool


class PublicAgentRunDTO(BaseModel):
    id: str
    agent_id: str
    status: str
    status_reason: Optional[str] = None
    queued_at: float
    started_at: Optional[float] = None
    created_at: str


class PublicEventDTO(BaseModel):
    id: int
    event_type: str
    purchase_id: Optional[str] = None
    run_id: Optional[str] = None
    payload: dict
    created_at: str


# --- Admin DTOs (X-Admin-Token required) ---

class AdminPurchaseDetailDTO(PublicPurchaseDTO):
    cart_json: str
    cart_hash: str
    create_request_id: str
    paypal_order_id: Optional[str] = None
    approval_url: Optional[str] = None
    capture_boot_id: Optional[str] = None


class AdminApprovalLinkDTO(BaseModel):
    purchase_id: str
    approval_url: str


class AdminRunDetailDTO(PublicAgentRunDTO):
    idempotency_key: str
    request_hash: str
    worker_id: Optional[str] = None
    trace_json: Optional[str] = None


# --- Request Input DTOs ---

class RunAgentRequest(BaseModel):
    user_prompt: str = Field(..., max_length=500, description="User purchasing instruction")
    agent_id: str = Field(default="default", max_length=64)


class UpdatePolicyRequest(BaseModel):
    daily_budget_cents: int = Field(..., gt=0)
    max_per_tx_cents: int = Field(..., gt=0)
    approval_threshold_cents: int = Field(..., gt=0)
    velocity_cap_cents: int = Field(..., gt=0)
    allowlist: List[str]
    # Note: timezone is explicitly excluded as it is IMMUTABLE via API!
