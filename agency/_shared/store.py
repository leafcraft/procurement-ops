"""Dev store — a working stand-in for the systems of record, so the pod runs
day-0 with NO connector wired.

This is a DEV STAND-IN, not a system of record. In production the requisition,
inventory and supplier reads come from your ERP / SRM (the `mcp` block in
config.yaml — SAP Ariba, Coupa, Oracle Procurement, NetSuite …), the outbox is
the supplier-facing PO channel, and the open-PO ledger is your ERP. **The
function signatures are the contract you swap real systems into** — the mesh does
not change.

Nothing here writes to a system of record; it holds seed requisitions, an
inventory snapshot and a supplier catalog (so the sourcing agent has something to
read) and appends the pod's own outbox + append-only open-PO ledger under
./data. Per the doctrine (where-data-lives): a real store enforces its own
invariants — this file is dev only.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_DATA_DIR = Path("./data")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(name: str, default: Any) -> Any:
    p = _DATA_DIR / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(name: str, data: Any) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _DATA_DIR / (name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(_DATA_DIR / name)


# ── Delegation of authority — a requisition whose estimated total is ABOVE
#    this (USD) needs a formal tender run by the Procurement Lead; the pod does
#    not issue it. Env knob HIGH_VALUE_THRESHOLD_USD (see env), default $50,000.
#    Read by the sourcing floor (agency/sourcing_agent.py) and
#    estimate_landed_cost (agency/tools.py). ──
HIGH_VALUE_THRESHOLD_USD = float(os.getenv("HIGH_VALUE_THRESHOLD_USD", "50000"))


# ── Inventory snapshot (dev mirror of the ERP/WMS on-hand view) ──────────────
#    unit_cost = standard cost; suppliers quote around it.
INVENTORY: dict[str, dict[str, Any]] = {
    "SKU-001": {"name": "Ball bearing 6204-2RS, sealed", "category": "components",
                "on_hand": 15, "reorder_point": 50, "safety_stock": 20, "unit_cost": 12.10,
                "abc_class": "A"},
    "SKU-002": {"name": "Hex bolt M8 x 25, A2 stainless (box of 100)", "category": "fasteners",
                "on_hand": 5, "reorder_point": 30, "safety_stock": 10, "unit_cost": 8.40,
                "abc_class": "B"},
    "SKU-003": {"name": "PT100 temperature probe, 3-wire", "category": "sensors",
                "on_hand": 200, "reorder_point": 75, "safety_stock": 25, "unit_cost": 40.00,
                "abc_class": "A"},
    "SKU-004": {"name": "Linear actuator 24 V, 150 mm stroke", "category": "assemblies",
                "on_hand": 11, "reorder_point": 20, "safety_stock": 5, "unit_cost": 104.00,
                "abc_class": "A"},
}

# ── Supplier catalog — options per SKU. In production this is an SRM read.
#    Companies are fictional; .example addresses never resolve. ────────────────
SUPPLIERS: dict[str, list[dict[str, Any]]] = {
    "SKU-001": [
        {"name": "Brightline Bearings Co.", "unit_price": 12.50, "lead_time_days": 7,
         "reliability": 0.95, "moq": 100, "currency": "USD", "region": "domestic",
         "contact_email": "orders@brightline-bearings.example", "payment_terms": "NET30",
         "on_file": True},
        {"name": "Lakeshore Industrial Supply", "unit_price": 13.20, "lead_time_days": 5,
         "reliability": 0.93, "moq": 50, "currency": "USD", "region": "domestic",
         "contact_email": "orders@lakeshore-industrial.example", "payment_terms": "NET30",
         "on_file": True},
    ],
    "SKU-002": [
        {"name": "Ironbridge Fasteners Inc.", "unit_price": 8.75, "lead_time_days": 3,
         "reliability": 0.92, "moq": 50, "currency": "USD", "region": "domestic",
         "contact_email": "orders@ironbridge-fasteners.example", "payment_terms": "NET30",
         "on_file": True},
    ],
    "SKU-004": [
        {"name": "Meridian Antriebstechnik GmbH", "unit_price": 96.00, "lead_time_days": 21,
         "reliability": 0.90, "moq": 20, "currency": "EUR", "region": "europe",
         "contact_email": "vertrieb@meridian-antriebe.example", "payment_terms": "NET45",
         "on_file": False},   # a NEW supplier — routes through compliance screening
    ],
    # SKU-003 has NO catalog entry on purpose — sourcing must ask, not guess.
}

# ── Seed requisitions — one per route, so the pod demonstrates end-to-end on
#    first run. People are fictional. ───────────────────────────────────────
SEED_REQUISITIONS: dict[str, dict[str, Any]] = {
    # Question route: SKU-003 has no supplier on file.
    "REQ-DEMO-001": {
        "requester_id": "buyer-1042",
        "requester_name": "Ana Rivera",
        "cost_center": "OPS-NA",
        "class": "domestic",
        "lines": [
            {"sku": "SKU-001", "quantity": 400, "need_by": "2026-09-15"},
            {"sku": "SKU-002", "quantity": 120, "need_by": "2026-09-10"},
            {"sku": "SKU-003", "quantity": 60, "need_by": "2026-09-20"},  # no supplier on file → question
        ],
    },
    # Compliance route: the only SKU-004 supplier is new (EU) → screened first.
    # 40 x EUR 96.00 = EUR 3,840.00
    "REQ-DEMO-002": {
        "requester_id": "buyer-2277",
        "requester_name": "Kwame Osei",
        "cost_center": "OPS-EU",
        "class": "international",
        "lines": [
            {"sku": "SKU-004", "quantity": 40, "need_by": "2026-10-01"},  # new EU supplier → compliance
        ],
    },
    # Happy path: on-file domestic suppliers, MOQs met.
    # 200 x $12.50 + 100 x $8.75 = $2,500.00 + $875.00 = $3,375.00
    "REQ-DEMO-003": {
        "requester_id": "buyer-3315",
        "requester_name": "Priya Natarajan",
        "cost_center": "MAINT-NA",
        "class": "domestic",
        "lines": [
            {"sku": "SKU-001", "quantity": 200, "need_by": "2026-09-22"},
            {"sku": "SKU-002", "quantity": 100, "need_by": "2026-09-22"},
        ],
    },
    # Over the delegation limit: 5,000 x $12.50 = $62,500 > $50,000 →
    # budget_threshold_exceeded → Procurement Lead (formal tender), no PO.
    "REQ-DEMO-004": {
        "requester_id": "buyer-1042",
        "requester_name": "Ana Rivera",
        "cost_center": "OPS-NA",
        "class": "domestic",
        "lines": [
            {"sku": "SKU-001", "quantity": 5000, "need_by": "2026-11-30"},
        ],
    },
}

# ── Reorder backlog — SKUs below reorder point, for the daily sweep + the
#    atomic claim tool that lets the parallel copies split the work. ─────────
def _seed_backlog() -> list[dict[str, Any]]:
    backlog: list[dict[str, Any]] = []
    for sku, rec in INVENTORY.items():
        if rec["on_hand"] < rec["reorder_point"]:
            backlog.append({"sku": sku, "name": rec["name"],
                            "on_hand": rec["on_hand"],
                            "reorder_point": rec["reorder_point"],
                            "suggested_qty": max(rec["reorder_point"] * 2 - rec["on_hand"], rec["reorder_point"])})
    return backlog


# ── Requisition / inventory / supplier reads (dev-store fallback) ────────────
def read_requisition(requisition_id: str) -> dict[str, Any]:
    req = SEED_REQUISITIONS.get(requisition_id)
    if not req:
        return {"available": False,
                "reason": f"no seed requisition for {requisition_id!r}"}
    return {"available": True, "requisition_id": requisition_id, **req}


def read_inventory(sku: str) -> dict[str, Any]:
    rec = INVENTORY.get(sku)
    if not rec:
        return {"available": False, "reason": f"SKU {sku!r} not in inventory snapshot"}
    on_hand, safety, reorder = rec["on_hand"], rec["safety_stock"], rec["reorder_point"]
    status = "critical" if (on_hand == 0 or on_hand < safety) else ("low" if on_hand < reorder else "normal")
    return {"available": True, "sku": sku, "stock_status": status, **rec}


def find_suppliers(sku: str) -> dict[str, Any]:
    options = SUPPLIERS.get(sku)
    if not options:
        return {"available": False, "sku": sku,
                "reason": f"no supplier on file for {sku!r} — ask the requester; never guess a vendor"}
    return {"available": True, "sku": sku, "suppliers": options}


# ── Atomic claim for the copies fan-out over the reorder backlog ────────────
def claim_backlog(count: int) -> list[dict[str, Any]]:
    """Atomically pop up to `count` SKUs off the shared reorder backlog so that
    parallel copies pull DISJOINT batches instead of duplicating work.

    Dev implementation: a lock + a JSON file under ./data (seeded on first use).
    Production: `LPOP count` on a Redis list, or `UPDATE … SKIP LOCKED
    RETURNING` on the backlog table — same disjoint-claim contract.
    """
    if count <= 0:
        return []
    with _LOCK:
        backlog = _read_json("reorder_backlog.json", None)
        if backlog is None:
            backlog = _seed_backlog()
        claimed = backlog[:count]
        remaining = backlog[count:]
        _write_json("reorder_backlog.json", remaining)
    return claimed


def reset_backlog() -> None:
    """Re-seed the reorder backlog (dev convenience for repeat demo runs)."""
    with _LOCK:
        _write_json("reorder_backlog.json", _seed_backlog())


# ── Outbox (PO sends) + append-only open-PO ledger ───────────────────────────
def record_outbox(entry: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        outbox = _read_json("outbox.json", [])
        rec = {"recorded_at": _now(), **entry}
        outbox.append(rec)
        _write_json("outbox.json", outbox)
    return rec


def register_open_po(po: dict[str, Any]) -> dict[str, Any]:
    """Append-only open-PO ledger (dev mirror; production = your ERP)."""
    with _LOCK:
        ledger = _read_json("open_pos.json", {})
        ledger[po["po_number"]] = {"registered_at": _now(), **po}
        _write_json("open_pos.json", ledger)
    return po


# ── Dev-store recovery for the finisher (post-HITL, when upstream is dropped) ──
def put_pending_requisition(requisition_id: str, pack: dict[str, Any]) -> None:
    with _LOCK:
        store = _read_json("pending_requisitions.json", {})
        store[requisition_id] = {"stored_at": _now(), **pack}
        _write_json("pending_requisitions.json", store)


def get_pending_requisition(requisition_id: str) -> dict[str, Any]:
    return _read_json("pending_requisitions.json", {}).get(requisition_id, {})
