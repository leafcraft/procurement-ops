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


# ── Inventory snapshot (dev mirror of the ERP/WMS on-hand view) ──────────────
INVENTORY: dict[str, dict[str, Any]] = {
    "SKU-001": {"name": "Widget A", "category": "components", "on_hand": 15,
                "reorder_point": 50, "safety_stock": 20, "unit_cost": 10.0,
                "abc_class": "A"},
    "SKU-002": {"name": "Component B", "category": "components", "on_hand": 5,
                "reorder_point": 30, "safety_stock": 10, "unit_cost": 6.5,
                "abc_class": "B"},
    "SKU-003": {"name": "Precision Part C", "category": "parts", "on_hand": 200,
                "reorder_point": 75, "safety_stock": 25, "unit_cost": 40.0,
                "abc_class": "A"},
    "SKU-004": {"name": "Assembly D", "category": "assemblies", "on_hand": 11,
                "reorder_point": 20, "safety_stock": 5, "unit_cost": 100.0,
                "abc_class": "A"},
}

# ── Supplier catalog — options per SKU. In production this is an SRM read. ────
SUPPLIERS: dict[str, list[dict[str, Any]]] = {
    "SKU-001": [
        {"name": "SupplierCo Alpha", "unit_price": 12.50, "lead_time_days": 7,
         "reliability": 0.95, "moq": 100, "currency": "USD", "region": "domestic",
         "contact_email": "orders@supplierco-alpha.com", "payment_terms": "NET30",
         "on_file": True},
        {"name": "NorthAmerica Parts", "unit_price": 13.20, "lead_time_days": 5,
         "reliability": 0.93, "moq": 50, "currency": "USD", "region": "domestic",
         "contact_email": "orders@naparts.com", "payment_terms": "NET30",
         "on_file": True},
    ],
    "SKU-002": [
        {"name": "FastSource Inc", "unit_price": 8.75, "lead_time_days": 3,
         "reliability": 0.92, "moq": 50, "currency": "USD", "region": "domestic",
         "contact_email": "orders@fastsource.com", "payment_terms": "NET30",
         "on_file": True},
    ],
    "SKU-004": [
        {"name": "Meridian Assemblies GmbH", "unit_price": 96.00, "lead_time_days": 21,
         "reliability": 0.90, "moq": 20, "currency": "EUR", "region": "europe",
         "contact_email": "vertrieb@meridian-gmbh.de", "payment_terms": "NET45",
         "on_file": False},   # a NEW supplier — routes through compliance screening
    ],
    # SKU-003 has NO catalog entry on purpose — sourcing must ask, not guess.
}

# ── Seed requisitions — two, so the pod demonstrates end-to-end on first run. ─
SEED_REQUISITIONS: dict[str, dict[str, Any]] = {
    "REQ-DEMO-001": {
        "requester_id": "buyer-1042",
        "requester_name": "A. Rivera",
        "cost_center": "OPS-NA",
        "class": "domestic",
        "lines": [
            {"sku": "SKU-001", "quantity": 400, "need_by": "2026-09-15"},
            {"sku": "SKU-002", "quantity": 120, "need_by": "2026-09-10"},
            {"sku": "SKU-003", "quantity": 60, "need_by": "2026-09-20"},  # no supplier on file → question
        ],
    },
    "REQ-DEMO-002": {
        "requester_id": "buyer-2277",
        "requester_name": "M. Osei",
        "cost_center": "OPS-EU",
        "class": "international",
        "lines": [
            {"sku": "SKU-004", "quantity": 40, "need_by": "2026-10-01"},  # new EU supplier → compliance
        ],
    },
}

# ── Reorder backlog — SKUs below reorder point, for the daily sweep + the
#    atomic claim tool that lets `instances` copies split the work. ───────────
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
                "reason": f"no seed requisition for {requisition_id!r}; wire ERP_MCP_URL"}
    return {"available": True, "requisition_id": requisition_id, **req}


def read_inventory(sku: str) -> dict[str, Any]:
    rec = INVENTORY.get(sku)
    if not rec:
        return {"available": False, "reason": f"SKU {sku!r} not in inventory snapshot; wire ERP_MCP_URL"}
    on_hand, safety, reorder = rec["on_hand"], rec["safety_stock"], rec["reorder_point"]
    status = "critical" if (on_hand == 0 or on_hand < safety) else ("low" if on_hand < reorder else "normal")
    return {"available": True, "sku": sku, "stock_status": status, **rec}


def find_suppliers(sku: str) -> dict[str, Any]:
    options = SUPPLIERS.get(sku)
    if not options:
        return {"available": False, "sku": sku,
                "reason": f"no supplier on file for {sku!r}; wire SRM_MCP_URL or add to the catalog"}
    return {"available": True, "sku": sku, "suppliers": options}


# ── Atomic claim for `instances` fan-out over the reorder backlog ────────────
def claim_backlog(count: int) -> list[dict[str, Any]]:
    """Atomically pop up to `count` SKUs off the shared reorder backlog so that
    parallel instances pull DISJOINT batches instead of duplicating work.

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
