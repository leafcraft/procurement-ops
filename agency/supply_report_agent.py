"""Programmatic Agent — Daily supply chain health report from live data.

auto_discover matches function name 'supply_report_agent' to YAML agent config.

Runs on the cron schedule defined in config.yaml (0 8 * * *) and on-demand
via mesh_call("supply_report", {"trigger": "manual"}).

All KPIs are computed from the live data files:
  Inventory KPIs  — configs/inventory.yaml  via data_loader.load_inventory()
  Supplier KPIs   — configs/suppliers.yaml  via data_loader.load_suppliers()

In production, replace the data_loader calls with ERP/WMS and SRM queries.

After generating the report, chains to supplier_agent (via can_call) when
status == "report_generated", which then chains to logistics_agent for a
full scored procurement plan using the real supplier catalog.
"""
import os
import sys
from datetime import datetime, timezone

from leafmesh import LeafMeshLogger

try:
    from .data_loader import compute_stock_status, get_in_transit_quantities, load_inventory, load_suppliers
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from data_loader import compute_stock_status, get_in_transit_quantities, load_inventory, load_suppliers

logger = LeafMeshLogger(__name__)


# ─── KPI computation helpers ─────────────────────────────────────────────────

def _inventory_kpis(inventory_db: dict, in_transit: dict) -> tuple[dict, list[dict], list[dict]]:
    """Compute inventory health KPIs from live data.

    Returns (summary_dict, critical_items, low_items).
    Items include all fields inventory_agent needs (avg_daily_demand,
    lead_time_days) so the daily chain produces the same forecast quality
    as the order-driven path.
    """
    critical, low, normal, stockout = [], [], [], []

    for sku, record in inventory_db.items():
        status = compute_stock_status(record)
        sku_in_transit = in_transit.get(sku, 0)
        item = {
            "sku": sku,
            "name": record["name"],
            "category": record.get("category", ""),
            "abc_class": record.get("abc_class", "C"),
            "on_hand": record["on_hand"],
            "reorder_point": record["reorder_point"],
            "safety_stock": record["safety_stock"],
            "max_stock": record.get("max_stock", 0),
            "lead_time_days": record.get("lead_time_days", 14),
            "avg_daily_demand": record.get("avg_daily_demand", 0),
            "stock_status": status,
            "units_needed": max(0, record["reorder_point"] - record["on_hand"] - sku_in_transit),
            "in_transit_qty": sku_in_transit,
            "requested_quantity": 0,
            "urgency_override": "normal",
        }
        if record["on_hand"] == 0:
            stockout.append(item)
        if status == "critical":
            critical.append(item)
        elif status == "low":
            low.append(item)
        else:
            normal.append(item)

    summary = {
        "total_skus": len(inventory_db),
        "critical_stock_count": len(critical),
        "low_stock_count": len(low),
        "normal_stock_count": len(normal),
        "stockout_count": len(stockout),
    }
    return summary, critical, low


def _supplier_kpis(supplier_catalog: dict) -> dict:
    """Aggregate supplier fleet performance from the catalog performance block."""
    performance: dict = supplier_catalog.get("performance", {})
    if not performance:
        return {"active_suppliers": 0}

    rates = [
        v["on_time_delivery_rate"]
        for v in performance.values()
        if "on_time_delivery_rate" in v
    ]
    lead_times = [
        v["average_lead_time_days"]
        for v in performance.values()
        if "average_lead_time_days" in v
    ]
    defects = [
        v["defect_rate"]
        for v in performance.values()
        if "defect_rate" in v
    ]

    return {
        "active_suppliers": len(performance),
        "on_time_delivery_rate": round(sum(rates) / len(rates), 4) if rates else None,
        "average_lead_time_days": round(sum(lead_times) / len(lead_times), 2) if lead_times else None,
        "defect_rate": round(sum(defects) / len(defects), 4) if defects else None,
    }


# ─── Agent function ───────────────────────────────────────────────────────────

async def supply_report_agent(llm_response, input_data: dict, context: dict) -> dict:
    """Generate a supply chain health report from live inventory and supplier data.

    All KPI values are derived from configs/*.yaml — no hardcoded mock numbers.
    Chains to supplier_agent when status == "report_generated".
    supplier_agent receives inventory_items + critical_items, scores the catalog,
    then chains to logistics_agent for a full structured procurement plan.
    """
    now = datetime.now(timezone.utc)
    trigger = input_data.get("trigger", "scheduled")

    inventory_db = load_inventory()
    supplier_catalog = load_suppliers()
    in_transit = get_in_transit_quantities()

    inv_summary, critical_items, low_items = _inventory_kpis(inventory_db, in_transit)
    supplier_metrics = _supplier_kpis(supplier_catalog)

    # Items needing procurement action (critical first, then low).
    # Exclude items fully covered by in-transit stock (units_needed == 0)
    # to avoid re-ordering what is already on the way.
    action_items = [i for i in critical_items + low_items if i.get("units_needed", 0) > 0]

    # ─── Health assessments ───
    inv_health = (
        "critical" if inv_summary["critical_stock_count"] > 0
        else "warning" if inv_summary["low_stock_count"] > 0
        else "healthy"
    )

    otd = supplier_metrics.get("on_time_delivery_rate")
    supplier_health = (
        "unknown" if otd is None
        else "warning" if otd < 0.90
        else "healthy"
    )

    checks = {
        "inventory_health": inv_health,
        "supplier_performance": supplier_health,
        "report_date": now.strftime("%Y-%m-%d"),
        "trigger_type": trigger,
        "skus_needing_action": len(action_items),
    }

    metrics: dict = {
        "report_time": now.isoformat(),
        "trigger_type": trigger,
        # Inventory KPIs — computed from live data
        "total_skus": inv_summary["total_skus"],
        "critical_stock_skus": inv_summary["critical_stock_count"],
        "low_stock_skus": inv_summary["low_stock_count"],
        "stockout_skus": inv_summary["stockout_count"],
        "skus_with_open_pos": len([k for k, v in in_transit.items() if v > 0]),
    }
    # Supplier KPIs — only included when performance data is available
    if supplier_metrics["active_suppliers"] > 0:
        metrics.update({
            "active_suppliers": supplier_metrics["active_suppliers"],
            "on_time_delivery_rate": supplier_metrics.get("on_time_delivery_rate"),
            "avg_lead_time_days": supplier_metrics.get("average_lead_time_days"),
            "defect_rate": supplier_metrics.get("defect_rate"),
        })
    else:
        metrics["active_suppliers"] = 0

    logger.info(
        f"Supply report ({trigger}) — inventory: {inv_health}, "
        f"critical SKUs: {inv_summary['critical_stock_count']}, "
        f"low SKUs: {inv_summary['low_stock_count']}"
    )

    return {
        "report": (
            f"Supply chain health report ({trigger}) at {now.isoformat()} — "
            f"inventory: {inv_health}, "
            f"{inv_summary['critical_stock_count']} critical, "
            f"{inv_summary['low_stock_count']} low SKUs"
        ),
        "generated_at": now.isoformat(),
        "checks": checks,
        "metrics": metrics,
        # Passed downstream so logistics_agent / po_approval_agent can detect
        # this is a scheduled run and apply auto-approval instead of blocking.
        "trigger_type": trigger,
        "is_scheduled_run": trigger in ("scheduled", "cron"),
        "inventory_items": action_items,
        "item_count": len(action_items),
        "critical_items": [i for i in critical_items if i.get("units_needed", 0) > 0],  # exclude items already covered by open POs
        "status": "report_generated",
    }
