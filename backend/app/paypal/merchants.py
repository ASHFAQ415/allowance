"""
Supported Onboarded Merchants and PayPal Payee Routing Registry.

Enforces that Personal and Business purchases route exclusively through supported,
onboarded merchants with pre-configured PayPal Payee accounts.
AI agents and buyers cannot debit arbitrary unverified recipients.
"""

import os
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class MerchantInfo:
    id: str
    name: str
    payee_email: str
    sandbox_merchant_id: str
    category: str
    status: str = "active"


SUPPORTED_MERCHANTS: Dict[str, MerchantInfo] = {
    "merchant_shoes": MerchantInfo(
        id="merchant_shoes",
        name="Footwear Emporium",
        payee_email="shoes-facilitator@allowance.dev",
        sandbox_merchant_id="SANDBOX_MERC_SHOES_01",
        category="footwear",
        status="active"
    ),
    "merchant_coffee": MerchantInfo(
        id="merchant_coffee",
        name="Artisan Coffee Roasters",
        payee_email="coffee-facilitator@allowance.dev",
        sandbox_merchant_id="SANDBOX_MERC_COFFEE_01",
        category="food_beverage",
        status="active"
    ),
    "merchant_tech": MerchantInfo(
        id="merchant_tech",
        name="Apex Tech Supply",
        payee_email="tech-facilitator@allowance.dev",
        sandbox_merchant_id="SANDBOX_MERC_TECH_01",
        category="electronics",
        status="active"
    ),
}


def get_merchant(merchant_id: str) -> Optional[MerchantInfo]:
    """Retrieves a supported merchant by identifier."""
    return SUPPORTED_MERCHANTS.get(merchant_id)


def is_merchant_supported(merchant_id: str) -> bool:
    """Verifies if the merchant is active and supported."""
    m = get_merchant(merchant_id)
    return m is not None and m.status == "active"


def get_payee_for_merchant(merchant_id: str) -> Dict[str, str]:
    """
    Returns PayPal Orders v2 payee object for order creation.
    Raises ValueError if merchant is not supported.
    Supports environment variable overrides for custom sandbox merchant accounts:
    - PAYPAL_{MERCHANT_ID}_PAYEE (e.g. PAYPAL_MERCHANT_SHOES_PAYEE)
    - PAYPAL_SANDBOX_PAYEE (global override for sandbox business account)
    """
    m = get_merchant(merchant_id)
    if not m:
        raise ValueError(f"Merchant '{merchant_id}' is not an onboarded merchant. Arbitrary payment routing denied.")

    payee_email = (
        os.environ.get(f"PAYPAL_{merchant_id.upper()}_PAYEE")
        or os.environ.get("PAYPAL_SANDBOX_PAYEE")
        or m.payee_email
    )

    payee = {"email_address": payee_email}
    custom_mid = os.environ.get(f"PAYPAL_{merchant_id.upper()}_MERCHANT_ID")
    if custom_mid:
        payee["merchant_id"] = custom_mid
    elif not os.environ.get(f"PAYPAL_{merchant_id.upper()}_PAYEE") and not os.environ.get("PAYPAL_SANDBOX_PAYEE"):
        payee["merchant_id"] = m.sandbox_merchant_id
    return payee

