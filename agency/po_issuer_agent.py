"""PO Issuer — the FINISHER / actuator.

auto_discover matches `po_issuer_agent`. The PO Issuer sits BEHIND the human
approval step. It never approves — it re-checks that a human actually approved,
renders the formal purchase-order pack, registers it as an open PO, records the
supplier-facing send, and appends an append-only ledger entry. It is the concrete
"@chain fails closed":

  a) approval RE-CHECK — reaching this agent does not mean approved. An
     allow-token allowlist + block-token denylist; an UNKNOWN verdict fails closed.
  b) deterministic invariants a human can't waive: the approval came from the
     finance approver seat (`approver_human`), an approval reference on record,
     a real supplier per line, positive quantity and unit price.
  c) render the real PO pack to ./out; on a tripped floor, DELETE the partial and
     record `status: blocked` with reasons — never a half-issued PO.
  d) dev-store recovery — after the HITL gate the upstream payload is dropped, so
     the finisher recovers the plan from the session stash / dev store, and the
     dev-store send ANNOUNCES itself (`submitted_via: dev_store`) — never a fake
     ERP ack. This code never posts to an ERP, so it never claims it did; to
     register POs in your ERP, swap store.register_open_po for the real call.

Two phases: first call (from the planner, confirmed) → route for approval; second
call (from the approver, approved) → issue + register + ledger.
"""
from __future__ import annotations

import os
import random
import string
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger, chain

try:
    from ._shared import store
    from ._shared.session_stash import resolve_session_id, stash_get, stash_put
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore
    from _shared.session_stash import resolve_session_id, stash_get, stash_put  # type: ignore

logger = LeafMeshLogger(__name__)

_OUT_DIR = Path("./out")
_APPROVE_TOKENS = frozenset({"approved", "approve", "sign", "signed", "yes", "ok"})
_BLOCK_TOKENS = frozenset({"rejected", "reject", "no", "deny", "denied",
                           "held", "held_for_review", "hold", "cancel", "cancelled"})


def _signal(input_data: dict) -> str:
    """The human verdict, from wherever the SDK delivered it (shape-tolerant)."""
    sources = [input_data]
    up = input_data.get("upstream_yields")
    if isinstance(up, dict):
        sources.extend(v for v in up.values() if isinstance(v, dict))
    for src in sources:
        for key in ("human_message", "human_decision", "decision", "message"):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().lower()
    return ""


def _is_approved(input_data: dict) -> bool:
    """Fail closed: only an explicit approve token counts; unknown => not approved."""
    sig = _signal(input_data)
    if sig in _BLOCK_TOKENS:
        return False
    return sig in _APPROVE_TOKENS


def _is_blocked(input_data: dict) -> bool:
    return _signal(input_data) in _BLOCK_TOKENS


APPROVER_SEAT = "approver_human"


def _approving_seat(input_data: dict) -> str:
    """The seat that sent the approval (the SDK stamps `source_agent` on every
    human reply). "" when the reply carries none."""
    sources = [input_data]
    up = input_data.get("upstream_yields")
    if isinstance(up, dict):
        sources.append(up)
        sources.extend(v for v in up.values() if isinstance(v, dict))
    for src in sources:
        val = src.get("source_agent")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _po_number() -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"PO-{today}-{suffix}"


def _line_ok(ln: dict) -> bool:
    return (
        isinstance(ln, dict)
        and str(ln.get("supplier") or "").strip()
        and str(ln.get("supplier")).strip().lower() not in ("unknown supplier", "tbd", "none")
        and int(ln.get("quantity", 0) or 0) > 0
        and float(ln.get("unit_price", 0) or 0) > 0
    )


def _render_pack(plan: dict, po_number: str, approval_ref: str) -> str:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    req_id = str(plan.get("requisition_id") or "requisition")
    path = _OUT_DIR / f"{po_number}.md"
    lines = [f"# Purchase Order — {po_number}",
             f"Requisition: {req_id}",
             f"Approval reference: {approval_ref}",
             f"Issued: {datetime.now(timezone.utc).isoformat()}",
             ""]
    total = 0.0
    for ln in plan.get("recommended_lines") or []:
        if not isinstance(ln, dict):
            continue
        qty = int(ln.get("quantity", 0) or 0)
        price = float(ln.get("unit_price", 0) or 0)
        line_total = round(price * qty, 2)
        total += line_total
        lines.append(f"- {ln.get('sku','')}  {ln.get('supplier','')}  "
                     f"qty {qty} × {ln.get('currency','USD')} {price} = {line_total}")
    lines += ["", f"**Total: {total:.2f}**", "",
              "_Issued and registered as an open PO. ERP posting on connector wiring._"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _recover_plan(input_data: dict, context: dict) -> dict:
    """Recover the plan across the HITL boundary: upstream -> stash -> dev store."""
    up = input_data.get("upstream_yields") or {}
    candidates = [input_data]
    if isinstance(up, dict):
        candidates.append(up.get("procurement_planner_agent"))
    for src in candidates:
        if isinstance(src, dict) and src.get("recommended_lines"):
            return src
    sid = resolve_session_id(input_data, context)
    stashed = stash_get(sid)
    if stashed.get("recommended_lines"):
        return stashed
    req_id = str(input_data.get("requisition_id") or "")
    return store.get_pending_requisition(req_id) or {"requisition_id": req_id}


def _delete_partial(result: dict) -> None:
    for key in ("pack_path",):
        p = result.get(key)
        if p:
            Path(p).unlink(missing_ok=True)
            result[key] = ""


def enforce_issuance_floor(result: Any, context: dict) -> dict:
    """The finisher floor — issue ONLY on a real approval; delete on a block."""
    result = result if isinstance(result, dict) else {}
    result.setdefault("block_reasons", [])

    if result.get("status") == "routed":
        return result  # phase 1: just routed for approval, nothing to issue yet

    if not result.get("approved"):
        result["status"] = "blocked"
        if _is_blocked(result.get("_input", {})):
            result["block_reasons"].append("approver rejected / held")
        else:
            result["block_reasons"].append("no explicit approval on record (fail closed)")
        _delete_partial(result)
        logger.info(f"[po_issuer] issuance floor BLOCKED: {result['block_reasons']}")
        return result

    plan = result.get("_plan") or {}
    lines = [ln for ln in (plan.get("recommended_lines") or []) if isinstance(ln, dict)]
    approval_ref = result.get("approval_reference") or ""

    invalid: list[str] = []
    seat = _approving_seat(result.get("_input") or {})
    if seat != APPROVER_SEAT:   # fail closed: no seat on record is no approval
        invalid.append(f"approval came from {seat or 'no seat on record'}, "
                       f"not the finance approver seat {APPROVER_SEAT}")
    if not approval_ref:
        invalid.append("missing approval_reference")
    if not lines:
        invalid.append("no line items to issue")
    for ln in lines:
        if not _line_ok(ln):
            invalid.append(f"{ln.get('sku','?')}: invalid supplier / quantity / price")

    if invalid:
        result.update(status="blocked", approved=False, po_issued=False)
        result["block_reasons"].extend(invalid)
        _delete_partial(result)
        logger.info(f"[po_issuer] invariant floor BLOCKED: {invalid}")
        return result

    # Passed the floor — render, register, ledger.
    po_number = _po_number()
    result["pack_path"] = _render_pack(plan, po_number, approval_ref)
    result["po_numbers"] = [po_number]
    result["artifact_paths"] = [result["pack_path"]]
    submitted_via = "dev_store"   # the only place this code registers a PO
    store.register_open_po({
        "po_number": po_number,
        "requisition_id": plan.get("requisition_id"),
        "approval_reference": approval_ref,
        "supplier_lines": lines,
        "submitted_via": submitted_via,
    })
    store.record_outbox({"requisition_id": plan.get("requisition_id"),
                         "action": "issue_po", "po_number": po_number,
                         "submitted_via": submitted_via})
    result.update(po_issued=True, status="issued", submitted_via=submitted_via)
    logger.info(f"[po_issuer] issued PO {po_number} via {submitted_via} -> {result['pack_path']}")
    return result


@chain(enforce_issuance_floor)
async def po_issuer_agent(llm_response: Any, input_data: dict, context: dict) -> dict:
    input_data = input_data if isinstance(input_data, dict) else {}
    plan = _recover_plan(input_data, context)
    sid = resolve_session_id(input_data, context)
    if plan.get("recommended_lines"):
        stash_put(sid, plan)   # keep it recoverable across the approval step

    out: dict[str, Any] = {
        "routed_for_approval": False, "approved": False, "po_issued": False,
        "po_numbers": [], "artifact_paths": [], "pack_path": "",
        "status": "", "block_reasons": [],
        "approval_reference": "", "_input": input_data, "_plan": plan,
    }

    if _is_approved(input_data):
        out["approved"] = True
        out["approval_reference"] = f"APPROVED-{sid[:8] or 'session'}"  # phase 2 — the floor issues it
    elif _is_blocked(input_data):
        out["status"] = "blocked"
    else:
        out["status"] = "routed"                   # phase 1 — send to the approver
        out["routed_for_approval"] = True
    return out
