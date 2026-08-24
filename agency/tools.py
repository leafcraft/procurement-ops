"""Global tools for the procurement-ops pod.

These are the sourcing agent's day-0 tool surface — the tools it calls, in the
turn, to PULL a requisition, read stock, and find suppliers. Each reads the dev
store (seed data) so the pod runs with no connector wired, and **each announces
itself**: when there is no real source and no seed, it returns `available: false`
and says why — never a synthetic success that looks like a real read. To go live,
wire the ERP/SRM `mcp` block in config.yaml (each named action becomes its own
tool); these dev tools are the fallback contract.
"""
import os

from leafmesh import global_tool, LeafMeshLogger

try:
    from ._shared import store
except ImportError:  # pragma: no cover — direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore

logger = LeafMeshLogger(__name__)

# Budget gate threshold (USD) — surfaced as an env knob (see env). Default $50,000.
HIGH_VALUE_THRESHOLD = int(os.getenv("HIGH_VALUE_THRESHOLD_USD", "50000"))


def _connector_wired() -> bool:
    """True if a real procurement connector is configured (ERP/SRM mcp URL set)."""
    return bool(os.getenv("ERP_MCP_URL") or os.getenv("SRM_MCP_URL"))


@global_tool(category="sourcing")
def read_requisition(requisition_id: str) -> dict:
    """Read a purchase requisition: requester, cost centre, class, and line items
    (sku, quantity, need-by date).

    When no real connector is wired, reads the seed store. Announces
    `available: false` if there is genuinely nothing to read.

    Args:
        requisition_id: The requisition to read (e.g. "REQ-DEMO-001").
    """
    result = store.read_requisition(str(requisition_id).strip())
    if not result.get("available") and _connector_wired():
        return {"available": False, "reason": "connector wired but returned nothing for this requisition"}
    logger.info(f"read_requisition id={requisition_id} available={result.get('available')}")
    return result


@global_tool(category="sourcing")
def check_inventory(sku: str) -> dict:
    """Read the current on-hand / reorder position for one SKU, with a derived
    stock status (critical / low / normal).

    Args:
        sku: The SKU to check (e.g. "SKU-001").
    """
    result = store.read_inventory(str(sku).strip().upper())
    if not result.get("available") and _connector_wired():
        return {"available": False, "reason": "connector wired but returned nothing for this SKU"}
    return result


@global_tool(category="sourcing")
def find_suppliers(sku: str) -> dict:
    """List the qualified supplier options for one SKU (price, lead time,
    reliability, MOQ, currency, region, whether the supplier is on file).

    Returns `available: false` when no supplier is on file — a SKU with no
    supplier is a QUESTION for the requester, never a guessed vendor.

    Args:
        sku: The SKU to source (e.g. "SKU-001").
    """
    result = store.find_suppliers(str(sku).strip().upper())
    if not result.get("available") and _connector_wired():
        return {"available": False, "reason": "connector wired but returned no supplier for this SKU"}
    return result


@global_tool(category="sourcing")
def estimate_landed_cost(lines: list) -> dict:
    """Estimate total spend for a set of recommended lines using each line's
    unit price × quantity, and flag whether it crosses the budget-approval
    threshold.

    Args:
        lines: List of lines, each with 'unit_price' and 'quantity' (and
            optionally 'sku').
    """
    total = 0.0
    detail = []
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        qty = int(ln.get("quantity", 0) or 0)
        price = float(ln.get("unit_price", 0) or 0)
        line_total = round(price * qty, 2)
        total += line_total
        detail.append({"sku": ln.get("sku", ""), "quantity": qty,
                       "unit_price": price, "line_total": line_total})
    total = round(total, 2)
    logger.info(f"estimate_landed_cost total=${total:,.2f}")
    return {"line_estimates": detail, "estimated_total": total,
            "estimated_total_formatted": f"${total:,.2f}",
            "exceeds_budget_threshold": total > HIGH_VALUE_THRESHOLD,
            "budget_threshold": HIGH_VALUE_THRESHOLD}


@global_tool(category="sourcing")
def claim_reorder_batch(count: int) -> dict:
    """Atomically claim up to `count` SKUs off the shared reorder backlog so that
    parallel copies of the sourcing agent (`instances: N`) each pull a DISJOINT
    batch instead of duplicating work.

    Returns the claimed SKUs; an empty list means the backlog is drained (a
    perfectly ordinary outcome — not an error).

    Args:
        count: How many backlog SKUs this copy should claim (e.g. 3).
    """
    claimed = store.claim_backlog(int(count or 0))
    logger.info(f"claim_reorder_batch count={count} claimed={len(claimed)}")
    return {"available": True, "claimed": claimed, "claimed_count": len(claimed)}
