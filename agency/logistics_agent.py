"""LLM Agent — Procurement plan builder with @chain + @compose decorators.

Execution flow:
  1. Receives supplier + inventory data from supplier_agent upstream yields
  2. LLM reasons over cost/risk/lead-time trade-offs to produce a plan
  3. @chain runs: validate_plan → score_priorities
  4. @compose shapes output for ERP and alerting systems
"""
from leafmesh import chain, compose, LeafMeshLogger, load_yaml_config
from leafmesh.redis import RedisManager
from agency.data_loader import parse_llm_json as _parse_llm_json
from agency._shared.session_stash import stash_put

logger = LeafMeshLogger(__name__)


def _order_line(priority: int, action: str, sku: str, description: str,
                quantity: int, supplier: str, unit_price: float,
                lead_time_days: int, urgency: str,
                payment_terms: str = "", contact_email: str = "",
                category: str = "", currency: str = "USD") -> dict:
    """Build a structured order line for frontend rendering."""
    return {
        "priority": priority,
        "action": action,                              # IMMEDIATE | REORDER | ORDER
        "sku": sku,
        "description": description,
        "quantity": quantity,
        "supplier": supplier,
        "unit_price": round(float(unit_price), 2),
        "total_cost": round(float(unit_price) * int(quantity), 2),
        "lead_time_days": int(lead_time_days),
        "urgency": urgency,                            # critical | low | normal
        "payment_terms": payment_terms,
        "contact_email": contact_email,
        "category": category,
        "currency": currency,
    }


# ─── @chain steps ────────────────────────────────────────────────────────────

def validate_plan(result, context):
    """Chain step 1: Validate procurement plan completeness."""
    if not result.get("procurement_plan"):
        result["procurement_plan"] = []
    if not result.get("summary"):
        result["summary"] = "No procurement actions identified — review inventory manually"
    if not result.get("risk_assessment"):
        result["risk_assessment"] = "Unable to assess — insufficient upstream data"
    if result.get("fulfillment_timeline_days", 0) <= 0:
        result["fulfillment_timeline_days"] = 14
    result["validated"] = True
    return result


def score_priorities(result, context):
    """Chain step 2: Score and rank the procurement plan by urgency + impact."""
    plan = result.get("procurement_plan", [])
    critical_count = result.get("critical_items_count", 0)
    estimated_cost = result.get("total_estimated_cost", 0)

    base_score = min(1.0, critical_count * 0.25)
    cost_factor = min(0.3, estimated_cost / 100000)
    result["priority_score"] = round(min(1.0, base_score + cost_factor), 3)

    # next_steps: top 3 action labels from the plan for quick display
    result["next_steps"] = [
        f"{item['action']}: {item['quantity']} × {item['sku']} ({item['description']}) "
        f"from {item['supplier']} — ${item['total_cost']:,.2f}, {item['lead_time_days']}d"
        for item in plan[:3]
        if isinstance(item, dict)
    ] or ["Gather more procurement data"]

    result["scored"] = True
    return result


# ─── @compose helpers ────────────────────────────────────────────────────────

def shape_procurement_report(data, context):
    """Compose helper: Shape output for ERP/procurement system consumption."""
    plan = data.get("procurement_plan", [])
    return {
        "title": "Procurement Plan",
        "generated_by": "logistics_agent",
        "summary": data.get("summary", ""),
        "order_lines": plan,
        "total_cost": data.get("total_estimated_cost", 0),
        "total_cost_formatted": f"${data.get('total_estimated_cost', 0):,.2f}",
        "timeline_days": data.get("fulfillment_timeline_days", 0),
        "priority_score": data.get("priority_score", 0),
        "recommended_supplier": data.get("recommended_supplier", ""),
        "supplier_source": data.get("supplier_source", ""),
        "supplier_confidence_pct": round(data.get("supplier_confidence", 0) * 100),
        "critical_items_count": data.get("critical_items_count", 0),
        "risk_assessment": data.get("risk_assessment", ""),
        "project": "supply-chain-management",
    }


def shape_alerts(data, context):
    """Compose helper: Shape output for alerting/monitoring systems."""
    score = data.get("priority_score", 0)
    critical = data.get("critical_items_count", 0)
    cost = data.get("total_estimated_cost", 0)

    if critical > 0:
        level = "critical"
    elif score > 0.7:
        level = "high"
    elif score > 0.4:
        level = "medium"
    else:
        level = "low"

    return {
        "alert_level": level,
        "alert_level_color": {"critical": "red", "high": "orange", "medium": "yellow", "low": "green"}[level],
        "summary": data.get("risk_assessment", ""),
        "critical_items_count": critical,
        "immediate_action_required": critical > 0 or score > 0.7,
        "estimated_cost": cost,
        "estimated_cost_formatted": f"${cost:,.2f}",
        "fulfillment_timeline_days": data.get("fulfillment_timeline_days", 0),
        "recommended_supplier": data.get("recommended_supplier", ""),
    }


# ─── Agent function ───────────────────────────────────────────────────────────

async def _save_yields(session_id: str, yields: dict) -> None:
    """Persist logistics_agent yields so purchase_order_agent can recover them after HITL.

    Written to BOTH the in-process session stash (Redis-independent) and Redis.
    """
    if not session_id:
        return
    stash_put(session_id, yields)
    try:
        import os
        _config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "config.yaml")
        _cfg = load_yaml_config(_config_path)
        rm = RedisManager(_cfg.redis, project_name=_cfg.name)
        await rm.connect()
        await rm.auto_store_agent_yields(session_id, "logistics_agent", yields, ttl=7200)
        logger.info(f"logistics_agent — yields saved to Redis for session {session_id}")
    except Exception as e:
        logger.warning(f"logistics_agent — failed to persist yields to Redis: {e}")


@chain(validate_plan, score_priorities)
@compose(procurement_report=shape_procurement_report, alerts=shape_alerts)
async def logistics_agent(llm_response, input_data, context):
    """Analyze inventory + supplier data and produce a structured procurement plan."""
    session_id: str = input_data.get("session_id", "") or context.get("session_id", "")

    upstream_yields = input_data.get("upstream_yields", {})
    # SDK fan-in puts results in upstream_results when upstream_yields is empty
    upstream_results = input_data.get("upstream_results", {})
    if not upstream_yields and upstream_results:
        upstream_yields = upstream_results

    inventory_yields = (
        upstream_yields.get("inventory_agent")
        or upstream_yields.get("supply_report_agent")
        or input_data.get("inventory_data")
        or {}
    )

    # Propagate scheduled-run flag so po_approval_agent can auto-approve
    is_scheduled_run: bool = (
        inventory_yields.get("is_scheduled_run")
        or input_data.get("is_scheduled_run", False)
    )
    _supplier_raw = (
        upstream_yields.get("supplier_agent", {})
        or input_data.get("supplier_data", {})
    )
    # chain_with_results wraps output in main_result/final_result — unwrap it
    supplier_yields = _supplier_raw.get("main_result") or _supplier_raw.get("final_result") or _supplier_raw

    # ─── Pick supplier source ───
    skipped_agents = []
    if supplier_yields and supplier_yields.get("status") == "supplier_research_complete":
        supplier_data = supplier_yields
        supplier_source = "supplier_agent"
        supplier_confidence = 0.90
    else:
        supplier_data = {}
        supplier_source = "none"
        supplier_confidence = 0.0
        skipped_agents.append("supplier_agent")

    # inventory_agent is two hops away — supplier_agent passes its data through
    inventory_items = (
        inventory_yields.get("inventory_items")
        or supplier_yields.get("inventory_items")
        or []
    )
    critical_items = (
        inventory_yields.get("critical_items")
        or supplier_yields.get("critical_items")
        or []
    )
    suppliers = supplier_data.get("suppliers", [])
    estimated_cost = supplier_data.get("estimated_cost", 0.0)
    recommended_supplier = supplier_data.get("recommended_supplier", "TBD")
    lead_time_days = supplier_data.get("estimated_lead_time_days", 14)

    # Use supplier_agent's pre-selected best_by_sku (includes regional/emergency overrides).
    # Falling back to re-building from the sorted list would ignore those decisions.
    supplier_by_sku: dict[str, dict] = supplier_data.get("best_by_sku", {})
    if not supplier_by_sku:
        for s in suppliers:
            sku = s.get("sku", "")
            if sku and sku not in supplier_by_sku:
                supplier_by_sku[sku] = s

    has_upstream = bool(inventory_items and suppliers)

    # ─── Path A: Direct call (no upstream) — parse LLM JSON ───────────────────
    llm_data = _parse_llm_json(llm_response)

    if llm_data and not has_upstream:
        procurement_plan = []
        priority = 1

        for item in llm_data.get("procurement_plan", []):
            if isinstance(item, dict):
                procurement_plan.append(_order_line(
                    priority=priority,
                    action=item.get("action", "ORDER"),
                    sku=item.get("sku", ""),
                    description=item.get("description", ""),
                    quantity=item.get("quantity", 0),
                    supplier=item.get("supplier", recommended_supplier),
                    unit_price=item.get("unit_price", 0),
                    lead_time_days=item.get("lead_time_days", lead_time_days),
                    urgency=item.get("urgency", "normal"),
                    payment_terms=item.get("payment_terms", ""),
                    contact_email=item.get("contact_email", ""),
                    category=item.get("category", ""),
                ))
                priority += 1

        estimated_cost = llm_data.get("total_estimated_cost", estimated_cost)
        lead_time_days = llm_data.get("fulfillment_timeline_days", lead_time_days)
        recommended_supplier = llm_data.get("recommended_supplier", recommended_supplier)
        supplier_source = llm_data.get("supplier_source", supplier_source)
        supplier_confidence = float(llm_data.get("supplier_confidence", supplier_confidence))
        llm_critical = int(llm_data.get("critical_items_count", 0))
        risk = llm_data.get("risk_assessment", "")
        if not risk:
            risk = f"critical — {llm_critical} SKU(s) require immediate action" if llm_critical else "low — all stock levels adequate"

        summary = (
            f"{len(procurement_plan)} order line(s) | "
            f"Total: ${estimated_cost:,.2f} | "
            f"Lead time: {lead_time_days}d | "
            f"Supplier: {recommended_supplier}"
        )

        logger.info(
            f"Logistics plan (LLM-direct) — {len(procurement_plan)} lines, "
            f"cost=${estimated_cost:,.2f}, supplier={recommended_supplier}"
        )

        result = {
            "procurement_plan": procurement_plan,
            "summary": summary,
            "total_estimated_cost": estimated_cost,
            "fulfillment_timeline_days": lead_time_days,
            "recommended_supplier": recommended_supplier,
            "risk_assessment": risk,
            "critical_items_count": llm_critical,
            "supplier_source": supplier_source,
            "supplier_confidence": supplier_confidence,
            "skipped_agents": skipped_agents,
            "is_scheduled_run": is_scheduled_run,
            "source_data": {
                "inventory_count": 0,
                "supplier_options": len(llm_data.get("procurement_plan", [])),
                "critical_count": llm_critical,
                "low_count": 0,
            },
            "preferred_region": input_data.get("preferred_region", ""),
            "status": "logistics_plan_complete",
        }
        await _save_yields(session_id, result)
        return result

    # ─── Path B: Chain call — build plan from upstream inventory + supplier data ─
    procurement_plan = []
    priority = 1

    # Critical items — IMMEDIATE action
    for item in critical_items:
        sku = item.get("sku", "")
        s = supplier_by_sku.get(sku, {})
        qty = max(item.get("units_needed", 0), s.get("moq", 1))
        procurement_plan.append(_order_line(
            priority=priority,
            action="IMMEDIATE",
            sku=sku,
            description=item.get("name", ""),
            quantity=qty,
            supplier=s.get("name", recommended_supplier),
            unit_price=s.get("unit_price", 0),
            lead_time_days=s.get("lead_time_days", lead_time_days),
            urgency="critical",
            payment_terms=s.get("payment_terms", ""),
            contact_email=s.get("contact_email", ""),
            category=item.get("category", ""),
            currency=s.get("currency", "USD"),
        ))
        priority += 1

    # Low-stock items — REORDER
    low_items = [i for i in inventory_items if i.get("stock_status") == "low"]
    for item in low_items:
        sku = item.get("sku", "")
        s = supplier_by_sku.get(sku, {})
        qty = max(item.get("units_needed", 0), s.get("moq", 1))
        procurement_plan.append(_order_line(
            priority=priority,
            action="REORDER",
            sku=sku,
            description=item.get("name", ""),
            quantity=qty,
            supplier=s.get("name", recommended_supplier),
            unit_price=s.get("unit_price", 0),
            lead_time_days=s.get("lead_time_days", lead_time_days),
            urgency="low",
            payment_terms=s.get("payment_terms", ""),
            contact_email=s.get("contact_email", ""),
            category=item.get("category", ""),
            currency=s.get("currency", "USD"),
        ))
        priority += 1

    # Explicit-order items — the operator requested a quantity for a
    # SKU that is neither critical nor low (normal/unknown stock).
    # Without this loop, an approved order for in-stock SKUs produced
    # an EMPTY procurement plan and the whole flow completed with no
    # POs issued.
    already_planned = {line["sku"] for line in procurement_plan}
    for item in inventory_items:
        sku = item.get("sku", "")
        requested = int(item.get("requested_quantity", 0) or 0)
        if not sku or sku in already_planned or requested <= 0:
            continue
        s = supplier_by_sku.get(sku, {})
        qty = max(requested, s.get("moq", 1))
        procurement_plan.append(_order_line(
            priority=priority,
            action="ORDER",
            sku=sku,
            description=item.get("name", ""),
            quantity=qty,
            supplier=s.get("name", recommended_supplier),
            unit_price=s.get("unit_price", 0),
            lead_time_days=s.get("lead_time_days", lead_time_days),
            urgency="normal",
            payment_terms=s.get("payment_terms", ""),
            contact_email=s.get("contact_email", ""),
            category=item.get("category", ""),
            currency=s.get("currency", "USD"),
        ))
        priority += 1

    # Recalculate total cost from structured plan (more accurate than supplier aggregate)
    if procurement_plan:
        estimated_cost = round(sum(line["total_cost"] for line in procurement_plan), 2)
        lead_time_days = max(line["lead_time_days"] for line in procurement_plan)

    # Risk assessment
    if critical_items:
        risk = f"critical — {len(critical_items)} SKU(s) at zero/below safety stock, immediate action required"
    elif low_items:
        risk = f"elevated — {len(low_items)} SKU(s) below reorder point"
    else:
        risk = "low — all stock levels adequate"

    summary = (
        f"{len(procurement_plan)} order line(s) — "
        f"{len(critical_items)} critical, {len(low_items)} low | "
        f"Total: ${estimated_cost:,.2f} | "
        f"Lead time: {lead_time_days}d | "
        f"Supplier: {recommended_supplier} ({supplier_source})"
    )

    logger.info(
        f"Logistics plan complete — {len(procurement_plan)} lines, "
        f"cost=${estimated_cost:,.2f}, risk={risk[:30]}, source={supplier_source}"
    )

    result = {
        "procurement_plan": procurement_plan,
        "summary": summary,
        "total_estimated_cost": estimated_cost,
        "fulfillment_timeline_days": lead_time_days,
        "recommended_supplier": recommended_supplier,
        "risk_assessment": risk,
        "critical_items_count": len(critical_items),
        "supplier_source": supplier_source,
        "supplier_confidence": supplier_confidence,
        "skipped_agents": skipped_agents,
        "is_scheduled_run": is_scheduled_run,
        "source_data": {
            "inventory_count": len(inventory_items),
            "supplier_options": len(suppliers),
            "critical_count": len(critical_items),
            "low_count": len(low_items),
        },
        "preferred_region": supplier_data.get("preferred_region", input_data.get("preferred_region", "")),
        "status": "logistics_plan_complete",
    }
    await _save_yields(session_id, result)
    return result
