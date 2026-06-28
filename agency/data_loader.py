"""Shared data loader for supply chain agents.

Loads configs/inventory.yaml and configs/suppliers.yaml once and caches
the result for the process lifetime. Swap the load_* functions for live
database or API calls when moving to production.
"""
import os
from functools import lru_cache
from pathlib import Path

import yaml

_CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"


@lru_cache(maxsize=None)
def load_inventory() -> dict[str, dict]:
    """Return the full inventory database keyed by SKU.

    Replace this function body with an ERP/WMS query in production.
    The lru_cache means the file is read once per process — call
    reload_data() to bust the cache if inventory is updated at runtime.
    """
    path = _CONFIGS_DIR / "inventory.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


@lru_cache(maxsize=None)
def load_suppliers() -> dict:
    """Return the full supplier catalog.

    Keys are SKU strings plus two special entries:
      'default'     — fallback suppliers when no catalog entry exists
      'performance' — aggregate supplier performance metrics

    Replace this function body with a supplier management API call
    (Coupa, SAP Ariba, etc.) in production.
    """
    path = _CONFIGS_DIR / "suppliers.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


def get_suppliers_for_sku(sku: str) -> list[dict]:
    """Return supplier options for a given SKU.

    Falls back to the 'default' catalog entry when no SKU-specific
    suppliers are configured. Returns an empty list if neither exists.
    """
    catalog = load_suppliers()
    return catalog.get(sku) or catalog.get("default") or []


def get_inventory_record(sku: str) -> dict | None:
    """Return the inventory record for a SKU, or None if unknown."""
    return load_inventory().get(sku)


def compute_stock_status(record: dict) -> str:
    """Derive stock status from an inventory record.

    Returns one of: 'critical', 'low', 'normal'.
    - critical: on_hand is zero or below safety_stock
    - low:      on_hand is above safety_stock but below reorder_point
    - normal:   on_hand is at or above reorder_point
    """
    on_hand = record.get("on_hand", 0)
    safety = record.get("safety_stock", 0)
    reorder = record.get("reorder_point", 0)

    if on_hand == 0 or on_hand < safety:
        return "critical"
    if on_hand < reorder:
        return "low"
    return "normal"


def reload_data() -> None:
    """Bust the lru_cache so the next call re-reads both YAML files.

    Call this after updating inventory.yaml or suppliers.yaml at runtime.
    """
    load_inventory.cache_clear()
    load_suppliers.cache_clear()


# ── Open PO tracking ─────────────────────────────────────────────────────────

_OPEN_POS_PATH = _CONFIGS_DIR / "open_pos.yaml"


def load_open_pos() -> dict:
    """Return all open POs keyed by PO number. Creates the file if absent."""
    if not _OPEN_POS_PATH.exists():
        return {}
    with open(_OPEN_POS_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def save_open_pos(data: dict) -> None:
    """Persist the open PO registry to disk."""
    with open(_OPEN_POS_PATH, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, allow_unicode=True)


def get_in_transit_quantities() -> dict[str, int]:
    """Return a dict of sku → total units currently in transit (open POs)."""
    in_transit: dict[str, int] = {}
    for po in load_open_pos().values():
        if po.get("status") in ("open", "partially_received"):
            for line in po.get("lines", []):
                sku = line.get("sku", "")
                ordered = line.get("quantity_ordered", 0)
                received = line.get("quantity_received", 0)
                pending = ordered - received
                if sku and pending > 0:
                    in_transit[sku] = in_transit.get(sku, 0) + pending
    return in_transit


def update_inventory_on_hand(sku: str, delta: int) -> None:
    """Add delta units to a SKU's on_hand in inventory.yaml and bust the cache.

    delta > 0 → receiving goods (GRN)
    delta < 0 → consumption / write-off

    Uses atomic write (temp file + rename) to prevent data loss when two GRNs
    arrive concurrently.
    """
    import tempfile

    path = _CONFIGS_DIR / "inventory.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    if sku not in data:
        raise KeyError(f"SKU {sku} not found in inventory.yaml")

    data[sku]["on_hand"] = max(0, data[sku].get("on_hand", 0) + delta)

    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".yaml.tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            yaml.dump(data, fh, default_flow_style=False, allow_unicode=True)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise

    load_inventory.cache_clear()  # force re-read on next access


def parse_llm_json(llm_response) -> dict:
    """Extract and parse the JSON object from an LLM response.

    Handles SDK envelope dicts, direct JSON strings, and mixed prose+JSON.
    """
    import json
    import re
    import logging

    if not llm_response:
        return {}
    if isinstance(llm_response, dict):
        text = (
            llm_response.get("content")
            or llm_response.get("text")
            or llm_response.get("message")
            or llm_response.get("output")
            or ""
        )
        if not text:
            return {}
    else:
        text = str(llm_response)

    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    logging.getLogger(__name__).warning("LLM did not return valid JSON")
    return {}
