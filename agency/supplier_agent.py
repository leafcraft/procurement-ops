"""Programmatic Agent — Supplier scoring and selection.

Converted from LLM agent to programmatic. The LLM was generating prose
commentary that nothing downstream consumed, while all actual selection
logic was already deterministic Python (composite scoring, ranking).

Composite score weights:
  Reliability  40%  — on-time delivery × quality
  Lead time    35%  — inverted, normalised over 30-day horizon
  Price        25%  — inverted, normalised over $500 ceiling

Cross-session memory retained — evaluations are stored so the mesh can
recall supplier performance trends across sessions.
"""
import os
import sys

from leafmesh import LeafMeshLogger

try:
    from .data_loader import get_suppliers_for_sku, load_suppliers
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from data_loader import get_suppliers_for_sku, load_suppliers

logger = LeafMeshLogger(__name__)


def _up(input_data: dict, context: dict, key: str, default=None):
    """Shape-tolerant upstream read: flat input_data → upstream_yields
    (flat or keyed by agent name) → context['upstream']."""
    if key in input_data and input_data[key] not in (None, ""):
        return input_data[key]
    uy = input_data.get("upstream_yields")
    if isinstance(uy, dict):
        if key in uy and uy[key] not in (None, ""):
            return uy[key]
        for v in uy.values():
            if isinstance(v, dict) and v.get(key) not in (None, ""):
                return v[key]
    for container_key in ("upstream_results",):
        ur = input_data.get(container_key)
        if isinstance(ur, dict):
            for v in ur.values():
                if isinstance(v, dict) and v.get(key) not in (None, ""):
                    return v[key]
    cu = context.get("upstream") if isinstance(context, dict) else None
    if isinstance(cu, dict) and cu.get(key) not in (None, ""):
        return cu[key]
    return default


def _live_reliability(supplier_name: str, performance: dict, static_fallback: float) -> float:
    """Compute reliability from live performance metrics when available.

    Formula: on_time_delivery × (1 − defect_rate) × fill_rate
    Falls back to the static YAML reliability score if no performance data exists.
    """
    perf = performance.get(supplier_name)
    if not perf:
        return static_fallback
    otd = perf.get("on_time_delivery_rate", static_fallback)
    defect = perf.get("defect_rate", 0.02)
    fill = perf.get("fill_rate", 0.95)
    return round(otd * (1.0 - defect) * fill, 4)


def _composite_score(supplier: dict, performance: dict) -> float:
    reliability = _live_reliability(
        supplier.get("name", ""),
        performance,
        supplier.get("reliability", 0.5),
    )
    lead_score = max(0.0, 1.0 - (supplier.get("lead_time_days", 30) / 30))
    price_score = max(0.0, 1.0 - (supplier.get("unit_price", 500) / 500))
    return round(0.40 * reliability + 0.35 * lead_score + 0.25 * price_score, 4)


async def supplier_agent(llm_response, input_data: dict, context: dict) -> dict:
    """Score and rank suppliers for every item in upstream inventory yields."""
    # Resolve the upstream container shape-tolerantly — the SDK delivers
    # upstream data under upstream_yields OR upstream_results OR context.
    upstream = input_data.get("upstream_yields") or {}
    if not upstream:
        upstream = input_data.get("upstream_results") or {}
    if not upstream and isinstance(context.get("upstream"), dict):
        upstream = context["upstream"]

    inventory_yields = (
        upstream.get("inventory_agent")
        or upstream.get("supply_report_agent")
        or {}
    )

    inventory_items: list[dict] = (
        inventory_yields.get("inventory_items")
        or _up(input_data, context, "inventory_items", [])
        or input_data.get("items")
        or input_data.get("inventory_data", {}).get("inventory_items", [])
        or []
    )
    is_scheduled_run: bool = inventory_yields.get("is_scheduled_run", False)

    # preferred_region: propagated from order_intake → operations_manager → inventory → here
    ops_yields = upstream.get("operations_manager", {})
    preferred_region: str = (
        inventory_yields.get("preferred_region")
        or ops_yields.get("preferred_region")
        or (ops_yields.get("request_data") or {}).get("preferred_region")
        or _up(input_data, context, "preferred_region", "")
        or ""
    ).lower().strip()

    # Load live performance metrics — used to override static reliability scores
    performance: dict = load_suppliers().get("performance", {})

    suppliers: list[dict] = []

    for inv_item in inventory_items:
        sku = inv_item.get("sku", "")
        units_needed = inv_item.get("units_needed", 0)
        if units_needed <= 0:
            units_needed = inv_item.get("requested_quantity", 0)

        for opt in get_suppliers_for_sku(sku):
            order_qty = max(units_needed, opt.get("moq", 0))
            score = _composite_score(opt, performance)
            suppliers.append({
                **opt,
                "sku": sku,
                "units_needed": units_needed,
                "order_quantity": order_qty,
                "estimated_line_cost": round(opt["unit_price"] * order_qty, 2),
                "composite_score": score,
                "using_live_performance": opt.get("name", "") in performance,
            })

    # Sort all options by composite score desc
    suppliers.sort(key=lambda s: s.get("composite_score", 0), reverse=True)

    # Determine if any critical-urgency items require emergency sourcing (lead_time <= 3d)
    critical_skus = {
        inv.get("sku", "") for inv in inventory_items
        if inv.get("urgency_override") == "critical" or inv.get("stock_status") == "critical"
    }

    # Best supplier per SKU
    # Priority order when preferred_region is set:
    #   1. Regional supplier (always honoured when region is explicitly requested)
    #   2. Global best score (fallback if no regional supplier for that SKU)
    #
    # Priority order when NO preferred_region:
    #   1. Critical SKUs → fastest supplier (lead_time <= 3d)
    #   2. Best composite score globally
    best_by_sku: dict[str, dict] = {}
    for s in suppliers:
        sku = s.get("sku", "")
        if not sku or sku in best_by_sku:
            continue
        if preferred_region:
            # Explicit region request always wins — even for critical urgency
            regional_best = next(
                (e for e in suppliers
                 if e.get("sku") == sku and e.get("region", "").lower() == preferred_region),
                None,
            )
            if regional_best:
                best_by_sku[sku] = regional_best
                logger.info(f"Supplier {sku}: selected {regional_best['name']} (region={preferred_region})")
            else:
                logger.warning(f"Supplier {sku}: no {preferred_region} supplier — falling back to best score")
                best_by_sku[sku] = s
        elif sku in critical_skus:
            # No region preference + critical urgency → pick fastest
            emergency = next(
                (e for e in suppliers if e.get("sku") == sku and e.get("lead_time_days", 99) <= 3),
                None,
            )
            best_by_sku[sku] = emergency if emergency else s
        else:
            best_by_sku[sku] = s

    estimated_cost = round(
        sum(s.get("estimated_line_cost", 0) for s in best_by_sku.values()), 2
    )

    overall_best = suppliers[0] if suppliers else {}
    if best_by_sku:
        # Report the supplier actually selected per SKU, not the global score winner
        recommended = best_by_sku[next(iter(best_by_sku))].get("name", "No supplier available")
        best_lead_time = max(s.get("lead_time_days", 0) for s in best_by_sku.values())
    else:
        recommended = overall_best.get("name", "No supplier available")
        best_lead_time = overall_best.get("lead_time_days", 0)

    analysis = {
        "total_evaluated": len(suppliers),
        "best_score": overall_best.get("composite_score", 0),
        "has_emergency_option": any(s.get("lead_time_days", 99) <= 3 for s in suppliers),
        "unique_supplier_count": len({s["name"] for s in suppliers}),
    }

    report = {
        "title": "Supplier Evaluation Report",
        "recommended": recommended,
        "recommended_score": overall_best.get("composite_score", 0),
        "lead_time_days": best_lead_time,
        "unit_cost": overall_best.get("unit_price", 0),
        "payment_terms": overall_best.get("payment_terms", ""),
        "contact_email": overall_best.get("contact_email", ""),
        "total_options_reviewed": analysis["total_evaluated"],
        "has_emergency_option": analysis["has_emergency_option"],
        "unique_supplier_count": analysis["unique_supplier_count"],
    }

    live_count = sum(1 for s in suppliers if s.get("using_live_performance"))
    logger.info(
        f"Supplier scoring — {len(suppliers)} options across {len(inventory_items)} SKU(s), "
        f"recommended: {recommended}, estimated cost: ${estimated_cost:,.2f}, "
        f"live performance data: {live_count}/{len(suppliers)} suppliers"
    )

    # Pass inventory data through — LeafMesh only propagates direct upstream yields
    critical_items = inventory_yields.get("critical_items", [])

    return {
        "suppliers": suppliers,
        "best_by_sku": best_by_sku,
        "recommended_supplier": recommended,
        "estimated_lead_time_days": best_lead_time,
        "estimated_cost": estimated_cost,
        "analysis": analysis,
        "report": report,
        "inventory_items": inventory_items,
        "critical_items": critical_items,
        "is_scheduled_run": is_scheduled_run,
        "preferred_region": preferred_region,
        "status": "supplier_research_complete",
    }
