"""Programmatic Agent — Inventory check + demand forecasting in a single pass.

Merged from inventory_agent + demand_forecast_agent to eliminate a redundant
network hop and a second inventory DB read.

Single DB read → stock status → quantity optimisation → yields to supplier_agent.

Quantity formula:
  demand_lead_time = avg_daily_demand × lead_time_days
  safety_buffer    = demand_lead_time × 0.5
  recommended_qty  = max(units_needed, demand_lead_time + safety_buffer)
  × 1.5 if critical urgency
  × 1.2 if quarter-end month
"""
import os
import sys
from datetime import datetime, timezone

from leafmesh import LeafMeshLogger, load_yaml_config, pre_compose
from leafmesh.redis import RedisManager

try:
    from .data_loader import compute_stock_status, get_in_transit_quantities, load_inventory
    from ._shared.session_stash import resolve_session_id, stash_get
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from data_loader import compute_stock_status, get_in_transit_quantities, load_inventory
    from _shared.session_stash import resolve_session_id, stash_get

logger = LeafMeshLogger(__name__)


def load_supply_snapshot(data, ctx) -> dict:
    """@pre_compose: self-reliant pull of the inventory master + open-PO
    in-transit quantities BEFORE the agent runs. The Inventory & Demand
    Planner does not wait to be spoon-fed stock levels — it reads them itself.

    Replace the data_loader calls with an ERP/WMS query in production.
    """
    inventory_db = load_inventory()
    in_transit = get_in_transit_quantities()  # sku → units already on open POs
    return {
        "inventory_db": inventory_db,
        "in_transit": in_transit,
        "sku_count": len(inventory_db),
        "skus_on_open_po": len([k for k, v in in_transit.items() if v > 0]),
    }


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

_SAFETY_BUFFER_MULTIPLIER = 0.5
_QUARTER_END_UPLIFT = 1.20
_CRITICAL_URGENCY_MULTIPLIER = 1.5


def _is_quarter_end() -> bool:
    return datetime.now(timezone.utc).month in {3, 6, 9, 12}


def _recommended_order_qty(item: dict, urgency: str, quarter_end: bool) -> int:
    avg_daily = item.get("avg_daily_demand", 0)
    lead_time = item.get("lead_time_days", 14)
    units_needed = item.get("units_needed", 0)
    on_hand = item.get("on_hand", 0)
    in_transit = item.get("in_transit_qty", 0)
    max_stock = item.get("max_stock", 0)

    demand_lead_time = avg_daily * lead_time
    safety_buffer = demand_lead_time * _SAFETY_BUFFER_MULTIPLIER
    base_qty = max(units_needed, int(demand_lead_time + safety_buffer))

    if urgency == "critical" or item.get("stock_status") == "critical":
        base_qty = int(base_qty * _CRITICAL_URGENCY_MULTIPLIER)
    if quarter_end:
        base_qty = int(base_qty * _QUARTER_END_UPLIFT)

    # Cap at warehouse capacity: don't order more than the bin can hold
    if max_stock > 0:
        available_space = max(0, max_stock - on_hand - in_transit)
        base_qty = min(base_qty, available_space)

    return max(base_qty, 1)


@pre_compose(context_processor=load_supply_snapshot)
async def inventory_agent(llm_response, input_data: dict, context: dict) -> dict:
    """Check inventory levels and compute optimised order quantities.

    SKU list resolved in priority order:
      1. upstream_yields["order_intake_agent"]["items"]
      2. upstream_yields["supply_report_agent"]["inventory_items"]  (daily cron path)
      3. input_data["items"]
      4. Full catalog scan when no SKUs specified
    """
    # ─── HITL rejection gate ───
    # Resolve the upstream container shape-tolerantly — the SDK delivers
    # upstream data under upstream_yields OR upstream_results OR context.
    upstream = input_data.get("upstream_yields") or {}
    if not upstream:
        upstream = input_data.get("upstream_results") or {}
    if not upstream and isinstance(context.get("upstream"), dict):
        upstream = context["upstream"]

    upstream_ops = upstream.get("operations_manager", {})
    request_data = upstream_ops.get("request_data") or {}
    if not isinstance(request_data, dict):
        request_data = {}

    human_message = (
        upstream_ops.get("human_message")
        or request_data.get("human_message")
        or _up(input_data, context, "human_message", "")
        or ""
    ).lower()

    _rejected = {"rejected", "reject", "denied", "deny", "cancel", "cancelled", "no"}
    if human_message and any(word in human_message for word in _rejected):
        logger.info(f"Order rejected by operations manager: '{human_message}' — halting")
        return {
            "inventory_items": [],
            "critical_items": [],
            "item_count": 0,
            "critical_count": 0,
            "low_count": 0,
            "urgency": "normal",
            "reorder_needed": False,
            "total_forecasted_units": 0,
            "quarter_end_uplift_applied": False,
            "alert": f"Order rejected by operations manager: {human_message}",
            "status": "rejected",
        }

    # ─── Resolve inputs ───
    intake = upstream.get("order_intake_agent", {})
    report = upstream.get("supply_report_agent", {})

    urgency = (
        intake.get("urgency")
        or request_data.get("urgency")
        or upstream_ops.get("urgency")
        or _up(input_data, context, "urgency", "")
        or "normal"
    )
    is_scheduled_run: bool = report.get("is_scheduled_run", False)

    # For supply_report path, items arrive pre-built with all needed fields
    pre_built_items: list[dict] = report.get("inventory_items", [])

    # Items resolution — order_intake_agent data may be nested inside request_data
    # after the HITL boundary (operations_manager only forwards ~4 keys).
    # Fall back to the in-process session stash, then Redis-persisted intake
    # yields, when items are missing.
    raw_items = (
        intake.get("items")
        or request_data.get("items")
        or upstream_ops.get("items")
        or _up(input_data, context, "items", [])
    )

    session_id = (
        resolve_session_id(input_data, context)
        or context.get("session_id", "")
        or input_data.get("session_id", "")
    )

    # In-process stash written by order_intake_agent before the HITL gate —
    # survives the human wait even when Redis is unavailable.
    stash_intake: dict = {}
    if not raw_items and session_id:
        stash_intake = stash_get(session_id)
        if stash_intake.get("items"):
            raw_items = stash_intake["items"]
            logger.info(
                f"inventory_agent — recovered {len(raw_items)} item(s) from session "
                f"stash for session {session_id}"
            )

    redis_intake: dict = {}
    if not raw_items and session_id:
        try:
            _config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "configs", "config.yaml",
            )
            _cfg = load_yaml_config(_config_path)
            rm = RedisManager(_cfg.redis, project_name=_cfg.name)
            await rm.connect()
            raw = await rm.get_agent_yields(session_id, "order_intake_agent") or {}
            redis_intake = raw.get("yields") or raw
            if not isinstance(redis_intake, dict):
                redis_intake = {}
            if redis_intake:
                raw_items = redis_intake.get("items", [])
                logger.info(
                    f"inventory_agent — recovered {len(raw_items)} item(s) from Redis "
                    f"for session {session_id}"
                )
        except Exception as e:
            logger.warning(f"inventory_agent — Redis intake recovery failed: {e}")

    # is_order_path: true when this call originated from a human-submitted order
    # (not a scheduled run or direct check_inventory call)
    is_order_path = bool(upstream_ops) and not is_scheduled_run

    # preferred_region: normalise to lowercase before passing downstream
    preferred_region: str = (
        request_data.get("preferred_region")
        or upstream_ops.get("preferred_region")
        or intake.get("preferred_region")
        or stash_intake.get("preferred_region")
        or redis_intake.get("preferred_region")
        or _up(input_data, context, "preferred_region", "")
        or ""
    ).lower().strip()

    # Prefer the @pre_compose snapshot (self-pulled before the agent ran);
    # fall back to a direct load when called outside the pre_compose path
    # (e.g. a direct unit-test invocation).
    _prepared = context.get("prepared_data", {}) if isinstance(context, dict) else {}
    _snapshot = _prepared.get("context", {}) if isinstance(_prepared.get("context"), dict) else {}
    inventory_db = _snapshot.get("inventory_db") or load_inventory()
    in_transit = _snapshot.get("in_transit")
    if in_transit is None:
        in_transit = get_in_transit_quantities()  # sku → units already on order
    quarter_end = _is_quarter_end()

    # ─── Supply report path: items already built, just run forecast ───
    if pre_built_items:
        # Refresh in_transit_qty from the current snapshot (supply_report may have
        # read it slightly earlier; use the fresher value but don't clobber if equal)
        inventory_items = [
            {**item, "in_transit_qty": in_transit.get(item.get("sku", ""), item.get("in_transit_qty", 0))}
            for item in pre_built_items
        ]
    else:
        # Normalise SKU requests
        sku_requests: dict[str, int] = {}
        for item in raw_items:
            if isinstance(item, dict):
                sku = str(item.get("sku", "")).strip().upper()
                qty = int(item.get("quantity", 0))
            else:
                sku = str(item).strip().upper()
                qty = 0
            if sku:
                sku_requests[sku] = sku_requests.get(sku, 0) + qty

        if not sku_requests:
            if is_order_path:
                # Came via operations_manager but items were lost in transit —
                # do NOT fall back to a full inventory scan for an explicit order
                logger.error("inventory_agent — order path but no items resolved from upstream")
                return {
                    "inventory_items": [], "critical_items": [], "item_count": 0,
                    "critical_count": 0, "low_count": 0, "urgency": urgency,
                    "reorder_needed": False, "total_forecasted_units": 0,
                    "adjusted_count": 0, "quarter_end_uplift_applied": False,
                    "is_scheduled_run": False,
                    "alert": "No items specified in order — cannot process",
                    "status": "no_items_specified",
                }
            # Direct call (check_inventory or supply_report) — scan full catalog
            sku_requests = {sku: 0 for sku in inventory_db}

        inventory_items = []
        for sku, requested_qty in sku_requests.items():
            record = inventory_db.get(sku)
            if record:
                status = compute_stock_status(record)
                on_hand = record["on_hand"]
                reorder_point = record["reorder_point"]
                sku_in_transit = in_transit.get(sku, 0)
                # Effective need = deficit minus what's already on order
                units_needed = max(0, reorder_point - on_hand - sku_in_transit)
                inventory_items.append({
                    "sku": sku,
                    "name": record["name"],
                    "category": record.get("category", ""),
                    "abc_class": record.get("abc_class", "C"),
                    "on_hand": on_hand,
                    "reorder_point": reorder_point,
                    "safety_stock": record["safety_stock"],
                    "max_stock": record.get("max_stock", 0),
                    "lead_time_days": record.get("lead_time_days", 14),
                    "avg_daily_demand": record.get("avg_daily_demand", 0),
                    "stock_status": status,
                    "units_needed": units_needed,
                    "in_transit_qty": sku_in_transit,
                    "requested_quantity": requested_qty,
                    "urgency_override": urgency,
                })
            else:
                logger.warning(f"Unknown SKU: {sku} — flagging for manual review")
                inventory_items.append({
                    "sku": sku,
                    "name": "Unknown SKU",
                    "category": "unknown",
                    "on_hand": -1,
                    "reorder_point": 0,
                    "safety_stock": 0,
                    "max_stock": 0,
                    "lead_time_days": 14,
                    "avg_daily_demand": 0,
                    "stock_status": "unknown",
                    "units_needed": 0,
                    "in_transit_qty": 0,
                    "requested_quantity": requested_qty,
                    "urgency_override": urgency,
                })

    # ─── Demand forecast — only actionable items ───
    # For explicit orders (is_order_path), always include all requested items
    # regardless of stock status — the operator decided they need them.
    if is_order_path:
        actionable = [i for i in inventory_items if i.get("requested_quantity", 0) > 0] or inventory_items
    else:
        actionable = [
            i for i in inventory_items
            if i.get("stock_status") in ("critical", "low") or i.get("units_needed", 0) > 0
        ] or inventory_items

    total_forecasted_units = 0
    adjusted_count = 0
    forecasted_items: list[dict] = []

    for item in actionable:
        original = item.get("units_needed", 0)
        requested = item.get("requested_quantity", 0)

        if is_order_path and requested > 0:
            # Honour the operator's requested quantity exactly.
            # Still show the system reorder qty as advisory information.
            recommended = requested
            forecast_adjusted = False
        else:
            recommended = _recommended_order_qty(item, urgency, quarter_end)
            forecast_adjusted = recommended != original
            if forecast_adjusted:
                adjusted_count += 1

        forecasted_items.append({
            **item,
            "units_needed": recommended,
            "original_units_needed": original,
            "forecast_adjusted": forecast_adjusted,
        })
        total_forecasted_units += recommended

    critical_items = [i for i in forecasted_items if i.get("stock_status") == "critical"]
    critical_count = len(critical_items)
    low_count = sum(1 for i in forecasted_items if i.get("stock_status") == "low")

    if critical_count > 0:
        alert = (
            f"CRITICAL: {critical_count} SKU(s) at zero or below safety stock — "
            "immediate procurement required"
        )
        reorder_needed = True
    elif low_count > 0:
        alert = f"{low_count} SKU(s) below reorder point — schedule reorder"
        reorder_needed = True
    else:
        alert = "All inventory levels within normal range"
        reorder_needed = False

    logger.info(
        f"Inventory+forecast — {len(forecasted_items)} SKUs: "
        f"{critical_count} critical, {low_count} low, urgency={urgency}, "
        f"quarter_end={quarter_end}, total_units={total_forecasted_units}"
    )

    return {
        "inventory_items": forecasted_items,
        "critical_items": critical_items,
        "item_count": len(forecasted_items),
        "critical_count": critical_count,
        "low_count": low_count,
        "urgency": urgency,
        "reorder_needed": reorder_needed,
        "alert": alert,
        "total_forecasted_units": total_forecasted_units,
        "adjusted_count": adjusted_count,
        "quarter_end_uplift_applied": quarter_end,
        "is_scheduled_run": is_scheduled_run,
        "preferred_region": preferred_region,
        "status": "inventory_checked",
    }
