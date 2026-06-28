"""Programmatic Agent — Goods Receipt Note (GRN) processing.

Closes the procurement loop: when goods arrive from a supplier, this agent:
  1. Validates the PO number against open_pos.yaml
  2. Updates each received SKU's on_hand in inventory.yaml
  3. Marks the PO line as received (partial or full)
  4. Closes the PO when all lines are fully received
  5. Flags discrepancies (short shipments, unexpected SKUs)

Entry point: receive_goods
Input:
  po_number       string   — PO to receive against
  received_items  list     — [{sku, quantity_received, condition}]
                             condition: "good" | "damaged" | "rejected"

Triggers HITL (ops_manager) when:
  - Any items are damaged or rejected (quality incident)
  - Short shipment > 20% of ordered quantity on a critical SKU
"""
import os
import sys
from datetime import datetime, timezone

from leafmesh import LeafMeshLogger

try:
    from .data_loader import (
        get_in_transit_quantities,
        load_open_pos,
        reload_data,
        save_open_pos,
        update_inventory_on_hand,
    )
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from data_loader import (
        get_in_transit_quantities,
        load_open_pos,
        reload_data,
        save_open_pos,
        update_inventory_on_hand,
    )

logger = LeafMeshLogger(__name__)

_SHORT_SHIPMENT_THRESHOLD = 0.20   # flag if received < 80% of ordered on critical items
_CRITICAL_SKUS_ABC = {"A"}         # ABC class A items are treated as critical


async def grn_agent(llm_response, input_data: dict, context: dict) -> dict:
    """Process goods receipt against an open PO."""
    po_number: str = input_data.get("po_number", "").strip().upper()
    received_items: list[dict] = input_data.get("received_items", [])
    received_at = datetime.now(timezone.utc).isoformat()

    if not po_number:
        return _error("po_number is required")

    if not isinstance(received_items, list) or not received_items:
        return _error("received_items must be a non-empty list")

    open_pos = load_open_pos()
    po = open_pos.get(po_number)

    if not po:
        return _error(f"PO {po_number} not found in open PO registry")

    if po.get("status") == "closed":
        return _error(f"PO {po_number} is already closed")

    # Build a lookup of ordered lines keyed by SKU
    lines_by_sku: dict[str, dict] = {
        line["sku"]: line for line in po.get("lines", [])
    }

    receipt_lines = []
    discrepancies = []
    quality_incidents = []
    total_accepted = 0
    total_rejected = 0

    for received in received_items:
        sku = received.get("sku", "").strip().upper()
        qty_received = int(received.get("quantity_received", 0))
        condition = received.get("condition", "good").lower()

        ordered_line = lines_by_sku.get(sku)
        if not ordered_line:
            discrepancies.append(f"{sku}: not on PO {po_number} — unexpected item")
            logger.warning(f"GRN {po_number}: unexpected SKU {sku}")
            continue

        qty_ordered = ordered_line.get("quantity_ordered", 0)
        already_received = ordered_line.get("quantity_received", 0)
        qty_accepted = 0

        if condition in ("damaged", "rejected"):
            quality_incidents.append({
                "sku": sku,
                "quantity": qty_received,
                "condition": condition,
                "po_number": po_number,
            })
            total_rejected += qty_received
            logger.warning(f"GRN {po_number}: {sku} × {qty_received} received as {condition}")
        else:
            qty_accepted = qty_received
            # Update inventory on_hand
            try:
                update_inventory_on_hand(sku, qty_accepted)
                total_accepted += qty_accepted
                logger.info(f"GRN {po_number}: +{qty_accepted} × {sku} → inventory updated")
            except KeyError:
                discrepancies.append(f"{sku}: not in inventory master — manual update required")
                quality_incidents.append({
                    "sku": sku,
                    "quantity": qty_accepted,
                    "condition": "inventory_master_missing",
                    "po_number": po_number,
                })
                logger.warning(f"GRN {po_number}: {sku} not in inventory.yaml — ops review required")

        # Update the PO line
        new_received = already_received + qty_accepted
        ordered_line["quantity_received"] = new_received
        ordered_line["last_received_at"] = received_at

        # Short shipment check on A-class items
        remaining = qty_ordered - new_received
        if remaining > 0:
            shortfall_pct = remaining / qty_ordered
            if shortfall_pct > _SHORT_SHIPMENT_THRESHOLD:
                discrepancies.append(
                    f"{sku}: short shipment — {new_received}/{qty_ordered} received "
                    f"({remaining} outstanding)"
                )

        receipt_lines.append({
            "sku": sku,
            "quantity_ordered": qty_ordered,
            "quantity_received": new_received,
            "quantity_accepted_this_receipt": qty_accepted,
            "quantity_rejected_this_receipt": qty_received - qty_accepted,
            "condition": condition,
            "fully_received": new_received >= qty_ordered,
        })

    # Update PO status
    all_lines = po.get("lines", [])
    fully_received_count = sum(
        1 for l in all_lines
        if l.get("quantity_received", 0) >= l.get("quantity_ordered", 0)
    )
    if fully_received_count == len(all_lines):
        po["status"] = "closed"
        po["closed_at"] = received_at
    elif total_accepted > 0:
        po["status"] = "partially_received"

    po["last_receipt_at"] = received_at
    open_pos[po_number] = po
    save_open_pos(open_pos)

    # Bust data loader cache so next inventory check sees updated on_hand values
    reload_data()

    requires_quality_review = bool(quality_incidents)
    requires_ops_review = bool(discrepancies) or requires_quality_review

    # Build a human-readable summary for the ops_manager webhook notification
    if requires_ops_review:
        issues = []
        if quality_incidents:
            issues.append(
                f"{len(quality_incidents)} quality incident(s): "
                + ", ".join(f"{i['sku']} × {i['quantity']} ({i['condition']})"
                            for i in quality_incidents)
            )
        if discrepancies:
            issues.append(f"{len(discrepancies)} discrepancy(s): " + "; ".join(discrepancies))
        order_message = (
            f"GRN review required for PO {po_number} (supplier: {po.get('supplier', '')}) — "
            + " | ".join(issues)
        )
    else:
        order_message = (
            f"GRN complete for PO {po_number} — {total_accepted} units accepted, "
            f"PO status: {po['status']}"
        )

    logger.info(
        f"GRN complete — PO {po_number}, status: {po['status']}, "
        f"accepted: {total_accepted}, rejected: {total_rejected}, "
        f"discrepancies: {len(discrepancies)}, quality_incidents: {len(quality_incidents)}"
    )

    return {
        "po_number": po_number,
        "supplier": po.get("supplier", ""),
        "grn_status": po["status"],
        "received_at": received_at,
        "receipt_lines": receipt_lines,
        "total_accepted": total_accepted,
        "total_rejected": total_rejected,
        "discrepancies": discrepancies,
        "quality_incidents": quality_incidents,
        "requires_quality_review": requires_quality_review,
        "requires_ops_review": requires_ops_review,
        # Passed to operations_manager as the notification message
        "order_message": order_message,
        "order_type": "goods_receipt",
        "items": [
            {"sku": l["sku"], "quantity": l["quantity_accepted_this_receipt"], "description": l["sku"]}
            for l in receipt_lines
        ],
        "urgency": "high" if quality_incidents else "normal",
        "status": "grn_complete",
    }


def _error(message: str) -> dict:
    logger.error(f"GRN error: {message}")
    return {
        "po_number": "",
        "supplier": "",
        "grn_status": "error",
        "error": message,
        "receipt_lines": [],
        "total_accepted": 0,
        "total_rejected": 0,
        "discrepancies": [message],
        "quality_incidents": [],
        "requires_quality_review": False,
        "requires_ops_review": True,
        "order_message": f"GRN error: {message}",
        "order_type": "goods_receipt",
        "items": [],
        "urgency": "high",
        "status": "grn_error",
    }
