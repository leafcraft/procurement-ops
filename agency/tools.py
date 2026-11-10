"""Global tools for the procurement-ops pod.

These are the sourcing agent's day-0 tool surface — the tools it calls, in the
turn, to PULL a requisition, read stock, and find suppliers. Each reads the dev
store (seed data) so the pod runs with no connector wired, and **each announces
itself**: when there is no real source and no seed, it returns `available: false`
and says why — never a synthetic success that looks like a real read. To go live,
wire the ERP/SRM `mcp` block in the configuration (each named action becomes its own
tool); these dev tools are the fallback contract.
"""
import os

from leafmesh import connection_rows, global_tool, LeafMeshLogger
from leafmesh.vault.refs import UnresolvedVaultRef

try:
    from ._shared import store
except ImportError:  # pragma: no cover — direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore

logger = LeafMeshLogger(__name__)

# Budget gate threshold (USD) — one value, kept in the store (env knob
# HIGH_VALUE_THRESHOLD_USD, default $50,000). The sourcing floor uses the same one.
HIGH_VALUE_THRESHOLD = store.HIGH_VALUE_THRESHOLD_USD


async def _vault_record_exists(name: str) -> bool:
    """True when the vault holds a record called `name` (Studio → Infrastructure).
    The record existing is the "real connector is wired" signal; its address
    and token stay in the vault and are never read here."""
    try:
        await connection_rows(name)
        return True
    except UnresolvedVaultRef:
        return False
    except PermissionError:  # SecretNotAllowed: the record exists; it only limits which agents read it
        return True


async def _connector_wired() -> bool:
    """True if a real procurement connector is set up (vault record 'erp_mcp' or 'srm_mcp')."""
    return await _vault_record_exists("erp_mcp") or await _vault_record_exists("srm_mcp")


_WIRED_HINT = ("not in the seed store — read it with the erp_/srm_ MCP tools")


@global_tool(category="sourcing")
async def read_requisition(requisition_id: str) -> dict:
    """Read a purchase requisition: requester, cost centre, class, and line items
    (sku, quantity, need-by date).

    When no real connector is wired, reads the seed store. Announces
    `available: false` if there is genuinely nothing to read.

    Args:
        requisition_id: The requisition to read (e.g. "REQ-DEMO-001").
    """
    result = store.read_requisition(str(requisition_id).strip())
    if not result.get("available") and await _connector_wired():
        return {"available": False, "reason": _WIRED_HINT}
    logger.info(f"read_requisition id={requisition_id} available={result.get('available')}")
    return result


@global_tool(category="sourcing")
async def check_inventory(sku: str) -> dict:
    """Read the current on-hand / reorder position for one SKU, with a derived
    stock status (critical / low / normal).

    Args:
        sku: The SKU to check (e.g. "SKU-001").
    """
    result = store.read_inventory(str(sku).strip().upper())
    if not result.get("available") and await _connector_wired():
        return {"available": False, "reason": _WIRED_HINT}
    return result


@global_tool(category="sourcing")
async def find_suppliers(sku: str) -> dict:
    """List the qualified supplier options for one SKU (price, lead time,
    reliability, MOQ, currency, region, whether the supplier is on file).

    Returns `available: false` when no supplier is on file — a SKU with no
    supplier is a QUESTION for the requester, never a guessed vendor.

    Args:
        sku: The SKU to source (e.g. "SKU-001").
    """
    result = store.find_suppliers(str(sku).strip().upper())
    if not result.get("available") and await _connector_wired():
        return {"available": False, "sku": sku, "reason": _WIRED_HINT}
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
    parallel copies of the sourcing agent (`scaling.copies.count: N`) each pull a DISJOINT
    batch instead of duplicating work.

    Returns the claimed SKUs; an empty list means the backlog is drained (a
    perfectly ordinary outcome — not an error).

    Args:
        count: How many backlog SKUs this copy should claim (e.g. 3).
    """
    claimed = store.claim_backlog(int(count or 0))
    logger.info(f"claim_reorder_batch count={count} claimed={len(claimed)}")
    return {"available": True, "claimed": claimed, "claimed_count": len(claimed)}
