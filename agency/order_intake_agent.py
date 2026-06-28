"""LLM Agent — Order intake + compliance screening in a single pass.

Merged from two agents (order_intake_agent + compliance_agent) to eliminate
a redundant LLM call. The LLM extracts structured order data; rule-based
compliance checks then run deterministically on the result.

Routing (via can_call in config.yaml):
  All orders → operations_manager (HITL) for approval
  operations_manager → inventory_agent on approval/timeout
"""
import os
import re
import sys

from leafmesh import LeafMeshLogger, chain, load_yaml_config, pre_compose
from leafmesh.redis import RedisManager
from agency.data_loader import get_suppliers_for_sku, parse_llm_json
from agency._shared.session_stash import stash_put

logger = LeafMeshLogger(__name__)

_HIGH_VALUE_THRESHOLD_USD = int(os.getenv("HIGH_VALUE_THRESHOLD_USD", "50000"))
_HIGH_QUANTITY_FLOOR = int(os.getenv("HIGH_QUANTITY_FLOOR", "500"))
_VALID_URGENCY = {"critical", "high", "normal"}
_VALID_ORDER_TYPES = {"purchase", "restock", "emergency"}

_HIGH_RISK_FLAGS = {
    "single_supplier_dependency",
    "compliance_review_required",
    "new_supplier_required",
    "unverified_destination",
    "budget_threshold_exceeded",
}


def _estimate_order_value(items: list[dict]) -> float:
    """Deterministic order-value estimate from the supplier catalog's lowest
    unit price per SKU. Used by the @chain floor — NOT by the LLM."""
    total = 0.0
    for item in items:
        sku = str(item.get("sku", "")).strip().upper()
        qty = int(item.get("quantity", 0) or 0)
        if not sku or qty <= 0:
            continue
        prices = [s.get("unit_price", 0) for s in get_suppliers_for_sku(sku) if s.get("unit_price")]
        if prices:
            total += min(prices) * qty
    return round(total, 2)


# ─── @chain deterministic floor ──────────────────────────────────────────────
# Runs AFTER order_intake_agent. The LLM extracts the order; this floor
# RE-COMPUTES the order value from the catalog and FORCES manager approval
# when it clears the high-value threshold (or the high-quantity floor) —
# the LLM cannot clear a high-value order by omitting the flag.

def enforce_approval_floor(result, context):
    """Deterministic high-value / high-quantity approval gate the LLM can't waive."""
    if not isinstance(result, dict):
        return result
    items = result.get("items") or []
    estimated_value = _estimate_order_value(items)
    total_qty = sum(int(i.get("quantity", 0) or 0) for i in items)

    issues = list(result.get("compliance_issues") or [])

    if estimated_value > _HIGH_VALUE_THRESHOLD_USD:
        msg = (
            f"High-value order — estimated ${estimated_value:,.2f} exceeds "
            f"${_HIGH_VALUE_THRESHOLD_USD:,} budget gate; manager sign-off required"
        )
        if msg not in issues:
            issues.append(msg)
        if "budget_threshold_exceeded" not in (result.get("risk_flags") or []):
            result.setdefault("risk_flags", []).append("budget_threshold_exceeded")

    if total_qty > _HIGH_QUANTITY_FLOOR:
        msg = (
            f"High quantity order ({total_qty} units) exceeds the "
            f"{_HIGH_QUANTITY_FLOOR}-unit standard approval limit"
        )
        if msg not in issues:
            issues.append(msg)

    # OR the floor onto whatever the function body decided — never lower it.
    result["compliance_issues"] = issues
    result["estimated_order_value"] = estimated_value
    if issues:
        result["requires_approval"] = True
        result["compliance_status"] = "flagged"
    logger.info(
        f"approval floor — est_value=${estimated_value:,.2f}, qty={total_qty}, "
        f"requires_approval={result.get('requires_approval')}"
    )
    return result


def process_context(input_data: dict, context: dict) -> dict:
    """Inject supply chain business rules into the LLM system context."""
    return {
        "domain": "supply_chain_management",
        "session_id": context.get("session_id", "unknown"),
        "high_value_threshold_usd": _HIGH_VALUE_THRESHOLD_USD,
        "currency": "USD",
        "urgency_rules": {
            "critical": "delivery required within 24 hours",
            "high": "delivery required within 72 hours",
            "normal": "standard lead time acceptable",
        },
    }


def clean_input(input_data: dict, context: dict) -> dict:
    """Normalise and sanitise the raw order message."""
    raw = (
        input_data.get("message")
        or input_data.get("order_message")
        or input_data.get("human_message")
        or ""
    )
    clean = raw.strip()
    return {
        "order_message": clean,
        "has_sku_reference": bool(re.search(r"SKU-\d+", clean, re.IGNORECASE)),
        "has_quantity": any(ch.isdigit() for ch in clean),
        "message_length": len(clean),
    }


def load_extras(input_data: dict, context: dict) -> dict:
    """Add temporal and operational metadata."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return {
        "request_timestamp": now.isoformat(),
        "current_month": now.month,
        "is_quarter_end": now.month in {3, 6, 9, 12},
        "fiscal_year": now.year,
    }


def _extract_from_text(text: str) -> dict:
    """Last-resort extraction: pull SKUs and quantities from plain text."""
    items = []
    seen: set[str] = set()

    for m in re.finditer(
        r"(?:(\d+)\s+(?:units?\s+(?:of\s+)?)?)?("
        r"SKU-\d+)(?:[:\s×x]+(\d+))?",
        text,
        re.IGNORECASE,
    ):
        pre_qty, sku, post_qty = m.group(1), m.group(2).upper(), m.group(3)
        qty_str = pre_qty or post_qty or "0"
        try:
            qty = int(qty_str)
        except ValueError:
            qty = 0
        if sku not in seen:
            seen.add(sku)
            items.append({"sku": sku, "quantity": qty, "description": ""})

    lower = text.lower()
    if any(w in lower for w in ("critical", "emergency", "asap", "immediately", "production line at risk", "line at risk")):
        urgency = "critical"
    elif any(w in lower for w in ("urgent", "rush", "high priority", "at risk")):
        urgency = "high"
    else:
        urgency = "normal"

    return {
        "order_summary": text[:200],
        "items": items,
        "urgency": urgency,
        "order_type": "purchase",
        "risk_flags": [],
        "detected_risk_level": "normal",
    }


@pre_compose(
    context_processor=process_context,
    input_processor=clean_input,
    others_processor=load_extras,
)
@chain(enforce_approval_floor)
async def order_intake_agent(llm_response, input_data: dict, context: dict) -> dict:
    """Parse order JSON from LLM then run rule-based compliance screening."""
    if not isinstance(context, dict):
        context = {}
    prepared = context.get("prepared_data", {})
    temporal = prepared.get("others", {})

    if not temporal:
        from datetime import datetime, timezone
        _now = datetime.now(timezone.utc)
        temporal = {"is_quarter_end": _now.month in {3, 6, 9, 12}}

    # If LLM returns empty content, extract SKUs/quantities directly from the
    # original message rather than from the useless llm_response string.
    _original_message = (
        input_data.get("message")
        or input_data.get("order_message")
        or (context.get("prepared_data", {}).get("input") or {}).get("order_message", "")
        or ""
    )
    parsed = parse_llm_json(llm_response) or _extract_from_text(_original_message)

    # ─── Items ───
    raw_items = parsed.get("items", [])
    items: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        sku = str(item.get("sku", "")).strip().upper()
        try:
            qty = int(item.get("quantity", item.get("qty", 0)))
        except (TypeError, ValueError):
            qty = 0
        if sku:
            items.append({
                "sku": sku,
                "quantity": qty,
                "description": str(item.get("description", "")),
            })

    # ─── Urgency ───
    urgency = parsed.get("urgency", "normal")
    if urgency not in _VALID_URGENCY:
        urgency = "normal"
    if temporal.get("is_quarter_end") and urgency == "normal":
        urgency = "high"

    # ─── Order type ───
    order_type = parsed.get("order_type", "purchase")
    if order_type not in _VALID_ORDER_TYPES:
        order_type = "purchase"

    # ─── Risk flags ───
    risk_flags: list[str] = parsed.get("risk_flags", [])
    if not isinstance(risk_flags, list):
        risk_flags = []
    if temporal.get("is_quarter_end"):
        risk_flags.append("quarter_end_demand_spike")
    total_qty = sum(i["quantity"] for i in items)
    if total_qty > 500:
        risk_flags.append("high_quantity_order")

    # ─── Risk level ───
    detected_risk_level = parsed.get("detected_risk_level", "normal")
    if detected_risk_level not in ("normal", "elevated", "high"):
        detected_risk_level = "elevated" if risk_flags else "normal"

    order_summary = parsed.get(
        "order_summary",
        f"{len(items)} item(s) — urgency: {urgency}",
    )

    # ─── Compliance screening (rule-based, replaces compliance_agent LLM call) ───
    compliance_issues: list[str] = []

    triggered = _HIGH_RISK_FLAGS.intersection(set(risk_flags))
    if triggered:
        compliance_issues.append(
            f"High-risk flags detected: {', '.join(sorted(triggered))}"
        )
    if urgency == "critical":
        compliance_issues.append("Critical urgency — manager sign-off required")
    if total_qty > 500:
        compliance_issues.append(
            f"High quantity order ({total_qty} units) — exceeds standard approval limit"
        )
    if detected_risk_level in ("elevated", "high") and not compliance_issues:
        compliance_issues.append(
            f"Elevated risk level from intake screening: {detected_risk_level}"
        )

    requires_approval = bool(compliance_issues)
    compliance_status = "flagged" if requires_approval else "cleared"

    logger.info(
        f"Order intake+compliance — {len(items)} item(s), urgency={urgency}, "
        f"risk={detected_risk_level}, compliance={compliance_status}, "
        f"requires_approval={requires_approval}"
    )

    result = {
        "order_summary": order_summary,
        "items": items,
        "urgency": urgency,
        "order_type": order_type,
        "risk_flags": risk_flags,
        "detected_risk_level": detected_risk_level,
        "compliance_status": compliance_status,
        "compliance_issues": compliance_issues,
        "requires_approval": requires_approval,
        "preferred_region": input_data.get("preferred_region", ""),
        # Identity passthrough — operations_manager inputs declare
        # order_message, so the approver sees the original request verbatim.
        "order_message": _original_message,
        "status": "intake_complete",
    }

    # Persist to Redis so inventory_agent can recover items after the HITL boundary.
    # operations_manager only forwards 4 keys; items get lost without this.
    # The in-process session stash is the Redis-independent fallback for the
    # same recovery (inventory_agent reads it post-HITL).
    session_id = context.get("session_id", "") or input_data.get("session_id", "")
    stash_put(session_id, result)
    if session_id:
        try:
            _config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "configs", "config.yaml",
            )
            _cfg = load_yaml_config(_config_path)
            rm = RedisManager(_cfg.redis, project_name=_cfg.name)
            await rm.connect()
            await rm.auto_store_agent_yields(session_id, "order_intake_agent", result, ttl=7200)
            logger.info(f"order_intake_agent — yields saved to Redis for session {session_id}")
        except Exception as e:
            logger.warning(f"order_intake_agent — failed to persist yields to Redis: {e}")

    return result
