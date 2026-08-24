"""Sourcing Analyst — reads the requisition, stock and supplier catalog and
produces confidence-scored recommended lines (SKU → supplier, price, qty).

auto_discover matches `sourcing_agent`. This is an llm + super_agent: the MODEL
plans and pulls across many SKUs through its tools; this CODE fails closed.

@chain enforce_coverage_floor — the deterministic floor a prompt can't waive: a
SKU with no supplier on file, a line missing a price or quantity, or a line the
model scored low can NEVER be passed downstream as "clean". Anything uncertain
forces the recommendation un-clean and turns it into ONE targeted question to the
requester — a guessed vendor or price never reaches the planner's PO.
"""
from __future__ import annotations

from typing import Any

from leafmesh import LeafMeshLogger, chain

logger = LeafMeshLogger(__name__)

_CONFIDENCE_MIN = 0.85


def enforce_coverage_floor(result: Any, context: dict) -> dict:
    """Fail closed: never hand a low-confidence / incomplete sourcing to the planner."""
    result = result if isinstance(result, dict) else {}
    lines = result.get("recommended_lines") or []
    low_conf = list(result.get("low_confidence_skus") or [])

    for ln in lines if isinstance(lines, list) else []:
        if not isinstance(ln, dict):
            continue
        sku = ln.get("sku", "?")
        if not str(ln.get("supplier") or "").strip():
            low_conf.append(f"{sku}:no_supplier")
        if ln.get("unit_price") in (None, "", 0):
            low_conf.append(f"{sku}:no_price")
        if int(ln.get("quantity", 0) or 0) <= 0:
            low_conf.append(f"{sku}:no_quantity")
        score = ln.get("confidence")
        if isinstance(score, (int, float)) and score < _CONFIDENCE_MIN:
            low_conf.append(f"{sku}:low_confidence")

    low_conf = sorted(set(low_conf))
    result["low_confidence_skus"] = low_conf

    if low_conf:
        result["sourcing_clean"] = False
        if not result.get("question_for_requester"):
            result["question_for_requester"] = (
                "We could not source these with confidence — please confirm a "
                "supplier / price / quantity for: " + ", ".join(low_conf)
            )
        logger.info(f"[sourcing] coverage floor held recommendation un-clean: {low_conf}")
    else:
        # Only 'clean' when there is genuinely something and nothing uncertain.
        result["sourcing_clean"] = bool(lines) and not result.get("sourcing_flags")
    return result


@chain(enforce_coverage_floor)
async def sourcing_agent(llm_response: Any, input_data: dict, context: dict) -> dict:
    """Return the model's confidence-scored recommendation; the floor re-checks it."""
    out = llm_response if isinstance(llm_response, dict) else {}
    # Normalise to the yields contract so downstream conditions never read a
    # missing field (conditions fail closed on absence — nuances #14).
    out.setdefault("sourcing_clean", False)
    out.setdefault("requisition_class", "")
    out.setdefault("low_confidence_skus", [])
    out.setdefault("sourcing_flags", [])
    out.setdefault("recommended_lines", [])
    out.setdefault("new_supplier", False)
    out.setdefault("question_for_requester", "")
    return out
