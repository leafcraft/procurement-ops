"""Procurement Planner / Builder — turns the sourcing recommendation into a
procurement plan (a draft PO pack), cross-checks it against the source, and gets
the requester's one-look confirmation.

auto_discover matches `procurement_planner_agent`. Deliberately SEPARATE from the
sourcing agent (the reader is not the assembler — a second pass catches what the
first missed).

@chain enforce_cross_check_floor — the deterministic floor: line totals, the plan
total and per-supplier MOQs must tie out against the source before anyone sees the
plan; if they don't, return to the sourcing agent with the specific discrepancy —
NEVER adjust a number to make it balance. The back-edge is BOUNDED: a per-session
rework counter (session_stash) trips to a terminal route to the Procurement Lead
after _MAX_REWORK rounds, so a sourcing↔planner loop can never spin forever. A
confirmed plan renders to ./out.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger, chain

try:
    from ._shared.session_stash import resolve_session_id, stash_get, stash_put
except ImportError:  # pragma: no cover
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared.session_stash import resolve_session_id, stash_get, stash_put  # type: ignore

logger = LeafMeshLogger(__name__)

_OUT_DIR = Path("./out")
_MAX_REWORK = 2  # bounded back-edge: at most this many sourcing↔planner rounds


def _render_draft(result: dict) -> str:
    """Render the draft procurement plan to ./out (Markdown). Returns the path."""
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    req_id = str(result.get("requisition_id") or "requisition")
    path = _OUT_DIR / f"{req_id}_procurement_plan_draft.md"
    lines = [f"# Procurement plan (draft) — {req_id}", ""]
    total = 0.0
    for ln in result.get("recommended_lines") or []:
        if not isinstance(ln, dict):
            continue
        qty = int(ln.get("quantity", 0) or 0)
        price = float(ln.get("unit_price", 0) or 0)
        line_total = round(price * qty, 2)
        total += line_total
        lines.append(f"- {ln.get('sku','')}  {ln.get('supplier','')}  "
                     f"qty {qty} × {ln.get('currency','USD')} {price} = {line_total}")
    lines += ["", f"**Estimated total: {total:.2f}**", "",
              "_Draft — awaiting requester confirmation. Every line traceable to a supplier quote._"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def enforce_cross_check_floor(result: Any, context: dict) -> dict:
    """Fail closed: the plan must tie out to the source, or it goes back to sourcing."""
    result = result if isinstance(result, dict) else {}
    lines = result.get("recommended_lines") or []

    computed = sum(
        float(ln.get("unit_price") or 0) * int(ln.get("quantity") or 0)
        for ln in lines if isinstance(ln, dict)
    )
    result["computed_total"] = round(computed, 2)

    stated_total = result.get("stated_total")
    ok = True
    discrepancy = ""
    if isinstance(stated_total, (int, float)) and abs(float(stated_total) - computed) > 0.01:
        ok = False
        discrepancy = f"stated total {stated_total} != sum of lines {computed:.2f}"
    for ln in lines if isinstance(lines, list) else []:
        if not isinstance(ln, dict):
            continue
        if ln.get("unit_price") in (None, "") or int(ln.get("quantity", 0) or 0) <= 0:
            ok = False
            discrepancy = discrepancy or f"{ln.get('sku','?')}: missing price or quantity"
        moq = ln.get("moq")
        if isinstance(moq, (int, float)) and int(ln.get("quantity", 0) or 0) < int(moq):
            ok = False
            discrepancy = discrepancy or f"{ln.get('sku','?')}: quantity below supplier MOQ {moq}"

    result["cross_check_ok"] = ok
    result["discrepancy"] = discrepancy

    # ── Bounded back-edge: count rework rounds per session; trip to terminal. ──
    sid = resolve_session_id(result.get("_input", {}), context)
    if not ok:
        rounds = 0
        if sid:
            stashed = stash_get(sid)
            rounds = int(stashed.get("planner_rework_rounds", 0)) + 1
            stashed["planner_rework_rounds"] = rounds
            stash_put(sid, stashed)
        result["plan_built"] = False
        result["awaiting_confirmation"] = False
        result["confirmed"] = False
        result["rework_exhausted"] = rounds >= _MAX_REWORK
        if result["rework_exhausted"]:
            logger.info(f"[planner] rework exhausted after {rounds} round(s) — routing to Procurement Lead")
        else:
            logger.info(f"[planner] cross-check floor held (round {rounds}): {discrepancy}")
        return result

    # Ties out → build the draft and ask the requester to confirm (once).
    result["rework_exhausted"] = False
    if not result.get("confirmed"):
        result["plan_built"] = True
        result["artifact_path"] = _render_draft(result)
        result["awaiting_confirmation"] = True
        # Stash the plan BEFORE the HITL confirm gate — after the human replies
        # the upstream payload is dropped, so the finisher recovers it from here.
        if sid:
            stash_put(sid, {"requisition_id": result.get("requisition_id"),
                            "recommended_lines": result.get("recommended_lines") or []})
    return result


@chain(enforce_cross_check_floor)
async def procurement_planner_agent(llm_response: Any, input_data: dict, context: dict) -> dict:
    out = llm_response if isinstance(llm_response, dict) else {}
    out.setdefault("plan_built", False)
    out.setdefault("cross_check_ok", False)
    out.setdefault("discrepancy", "")
    out.setdefault("awaiting_confirmation", False)
    out.setdefault("confirmed", False)
    out.setdefault("rework_exhausted", False)
    out.setdefault("artifact_path", "")
    out.setdefault("recommended_lines", (input_data or {}).get("recommended_lines", []))
    out.setdefault("requisition_id", (input_data or {}).get("requisition_id", ""))
    # Keep the trigger visible to the floor so it can resolve the session id.
    out["_input"] = input_data if isinstance(input_data, dict) else {}
    return out
