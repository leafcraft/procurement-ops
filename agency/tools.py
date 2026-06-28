"""Global tools available to LLM agents.

Register tools here with @global_tool — the LeafMesh SDK auto-discovers
this file and makes these tools available to any agent that lists them
under `tools:` in config.yaml.
"""
import os

from leafmesh import global_tool, LeafMeshLogger
from agency.data_loader import load_inventory, get_suppliers_for_sku

logger = LeafMeshLogger(__name__)

# Budget gate threshold — surfaced as an env knob (see env). Default $50,000.
HIGH_VALUE_THRESHOLD = int(os.getenv("HIGH_VALUE_THRESHOLD_USD", "50000"))


@global_tool
def validate_skus(items: list) -> dict:
    """Check whether each SKU in the order exists in the inventory catalog.

    Returns a summary with valid SKUs, unknown SKUs, and a flag indicating
    whether the order contains any unrecognised items. The LLM uses this
    to add 'unverified_sku' to risk_flags when unknown items are present.

    Args:
        items: List of order items, each with a 'sku' key.
    """
    inventory = load_inventory()
    valid, unknown = [], []

    for item in items:
        sku = str(item.get("sku", "")).strip().upper()
        if not sku:
            continue
        if sku in inventory:
            record = inventory[sku]
            valid.append({
                "sku": sku,
                "name": record.get("name", ""),
                "category": record.get("category", ""),
                "on_hand": record.get("on_hand", 0),
                "reorder_point": record.get("reorder_point", 0),
                "safety_stock": record.get("safety_stock", 0),
            })
        else:
            unknown.append(sku)

    logger.info(f"validate_skus — valid={len(valid)}, unknown={len(unknown)}")
    return {
        "valid_skus": valid,
        "unknown_skus": unknown,
        "has_unknown_skus": len(unknown) > 0,
        "total_checked": len(valid) + len(unknown),
    }


@global_tool
def estimate_order_value(items: list) -> dict:
    """Estimate total order value using the supplier catalog's lowest unit price.

    Useful for flagging budget_threshold_exceeded early during intake,
    before the supplier and logistics agents run their full analysis.

    Args:
        items: List of order items, each with 'sku' and 'quantity' keys.
    """
    total = 0.0
    line_estimates = []

    for item in items:
        sku = str(item.get("sku", "")).strip().upper()
        qty = int(item.get("quantity", 0))
        if not sku or qty <= 0:
            continue

        suppliers = get_suppliers_for_sku(sku)
        if not suppliers:
            line_estimates.append({"sku": sku, "quantity": qty, "unit_price": None, "line_total": None})
            continue

        prices = [s.get("unit_price", 0) for s in suppliers if s.get("unit_price")]
        if not prices:
            line_estimates.append({"sku": sku, "quantity": qty, "unit_price": None, "line_total": None})
            continue

        unit_price = min(prices)
        line_total = round(unit_price * qty, 2)
        total += line_total
        line_estimates.append({
            "sku": sku,
            "quantity": qty,
            "unit_price": unit_price,
            "line_total": line_total,
        })

    total = round(total, 2)
    logger.info(f"estimate_order_value — estimated total: ${total:,.2f}")
    return {
        "line_estimates": line_estimates,
        "estimated_total": total,
        "estimated_total_formatted": f"${total:,.2f}",
        "exceeds_budget_threshold": total > HIGH_VALUE_THRESHOLD,
        "budget_threshold": HIGH_VALUE_THRESHOLD,
    }


@global_tool
def get_sku_details(sku: str) -> dict:
    """Return full inventory details for a single SKU.

    Useful when the LLM needs to look up the current stock level, category,
    or ABC class of a specific item mentioned in the order message.

    Args:
        sku: The SKU identifier (e.g. 'SKU-001').
    """
    inventory = load_inventory()
    sku = sku.strip().upper()
    record = inventory.get(sku)

    if not record:
        logger.info(f"get_sku_details — SKU not found: {sku}")
        return {"found": False, "sku": sku}

    on_hand = record.get("on_hand", 0)
    safety = record.get("safety_stock", 0)
    reorder = record.get("reorder_point", 0)

    if on_hand == 0 or on_hand < safety:
        stock_status = "critical"
    elif on_hand < reorder:
        stock_status = "low"
    else:
        stock_status = "normal"

    return {
        "found": True,
        "sku": sku,
        "name": record.get("name", ""),
        "category": record.get("category", ""),
        "abc_class": record.get("abc_class", ""),
        "on_hand": on_hand,
        "safety_stock": safety,
        "reorder_point": reorder,
        "unit_cost": record.get("unit_cost", 0),
        "stock_status": stock_status,
    }
