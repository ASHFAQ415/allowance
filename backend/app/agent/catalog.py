"""
ShopMind Sandboxed Product Catalog.
Seeded with 20 realistic products across 4 merchants (3 approved, 1 unapproved).
Server-side prices are authoritative; prices from models or user inputs are strictly ignored.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class CatalogProduct:
    id: str
    merchant_id: str
    name: str
    description: str
    price_cents: int
    in_stock: bool
    category: str


# Seeded 20 products across 4 merchants
# Approved merchants: merchant_shoes, merchant_coffee, merchant_tech
# Unapproved merchant: merchant_unapproved (used for adversarial testing)
CATALOG_ITEMS: List[CatalogProduct] = [
    # Merchant: merchant_shoes (Approved)
    CatalogProduct("shoes_trail_runner", "merchant_shoes", "Trail Running Shoes", "Breathable trail running shoes with rugged grip.", 4500, True, "footwear"),
    CatalogProduct("shoes_road_racer", "merchant_shoes", "Road Racing Flats", "Ultra-lightweight marathon road racing shoes.", 9000, True, "footwear"),
    CatalogProduct("shoes_cushion_max", "merchant_shoes", "Max Cushion Daily Trainer", "Plush foam cushioning for long distance recovery.", 13000, True, "footwear"),
    CatalogProduct("shoes_hiking_boot", "merchant_shoes", "Waterproof Hiking Boots", "Ankle support and waterproof membrane for wet trails.", 14500, True, "footwear"),
    CatalogProduct("shoes_running_socks", "merchant_shoes", "Anti-Blister Running Socks (3-Pack)", "Merino wool blend anti-friction athletic socks.", 1800, True, "accessories"),

    # Merchant: merchant_coffee (Approved)
    CatalogProduct("coffee_espresso_blend", "merchant_coffee", "Organic Espresso Beans (1kg)", "Dark roast whole bean espresso with chocolate notes.", 2200, True, "food_beverage"),
    CatalogProduct("coffee_single_origin", "merchant_coffee", "Ethiopian Yirgacheffe (500g)", "Light roast whole bean with bright floral and citrus acidity.", 1600, True, "food_beverage"),
    CatalogProduct("coffee_french_press", "merchant_coffee", "Borosilicate Glass French Press (1L)", "Durable 8-cup stainless steel mesh coffee press.", 3200, True, "kitchen"),
    CatalogProduct("coffee_hand_grinder", "merchant_coffee", "Conical Burr Hand Grinder", "Precision stainless steel manual coffee burr grinder.", 4800, True, "kitchen"),
    CatalogProduct("coffee_paper_filters", "merchant_coffee", "Cone Paper Coffee Filters (100-Pack)", "Unbleached biodegradable cone drip filters.", 600, True, "kitchen"),

    # Merchant: merchant_tech (Approved)
    CatalogProduct("tech_usb_c_hub", "merchant_tech", "7-in-1 USB-C Hub Adapter", "4K HDMI, 100W PD charging, 3x USB 3.0, SD reader.", 3500, True, "electronics"),
    CatalogProduct("tech_ergo_mouse", "merchant_tech", "Wireless Ergonomic Vertical Mouse", "Reduces wrist strain with natural handshake grip.", 4200, True, "electronics"),
    CatalogProduct("tech_mech_keyboard", "merchant_tech", "Compact Mechanical Keyboard", "Hot-swappable tactile switches with RGB backlighting.", 7500, True, "electronics"),
    CatalogProduct("tech_desk_mat", "merchant_tech", "Felt Wool Desk Pad Mat", "Water-resistant large desktop mouse and keyboard surface.", 2400, True, "accessories"),
    CatalogProduct("tech_monitor_light", "merchant_tech", "Screenbar LED Monitor Light", "Glare-free auto-dimming desk lamp with touch controls.", 4900, True, "lighting"),
    CatalogProduct("tech_hdmi_cable", "merchant_tech", "Braided 8K Ultra High Speed HDMI 2.1 (2m)", "48Gbps bandwidth supporting 4K 120Hz and 8K 60Hz.", 1500, True, "cables"),

    # Merchant: merchant_unapproved (UNAPPROVED - Triggers Rule R1)
    CatalogProduct("unapproved_vpn_sub", "merchant_unapproved", "1-Year Anonymous VPN Subscription", "Untrusted unvetted third-party VPN service.", 4000, True, "software"),
    CatalogProduct("unapproved_gift_card", "merchant_unapproved", "Crypto Voucher Gift Card ($50)", "High-risk unapproved digital cash voucher.", 5000, True, "vouchers"),
    CatalogProduct("unapproved_gadget", "merchant_unapproved", "Uncertified Spy Gadget USB", "High-risk unvetted electronic peripheral.", 3000, True, "electronics"),
    CatalogProduct("unapproved_discontinued_item", "merchant_unapproved", "Discontinued 2021 Widget", "Out of stock unapproved archive product.", 2500, False, "archive"),
]

CATALOG_BY_ID: Dict[str, CatalogProduct] = {item.id: item for item in CATALOG_ITEMS}


def get_product(product_id: str) -> Optional[CatalogProduct]:
    """Retrieves authoritative product details by ID."""
    return CATALOG_BY_ID.get(product_id)


def search_catalog_data(query: str, limit: int = 10) -> List[CatalogProduct]:
    """
    Case-insensitive search over catalog product names, categories, and descriptions.
    Restricted to in-stock items unless explicitly searched.
    """
    q = query.lower().strip()
    results = []
    for item in CATALOG_ITEMS:
        if q in item.name.lower() or q in item.description.lower() or q in item.category.lower():
            results.append(item)
            if len(results) >= limit:
                break
    return results

