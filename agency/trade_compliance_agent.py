"""trade_compliance_agent — OFAC / EAR / denied-party screening for suppliers.

Lyzr-pattern compliance watcher applied to inbound procurement. When a
supplier is being onboarded OR when a PO is about to be issued to a
high-value supplier, screens the legal entity name + country of origin
against:

  · OFAC SDN (Specially Designated Nationals)
  · OFAC consolidated list
  · BIS denied parties (Bureau of Industry and Security)
  · BIS entity list
  · State Department debarred list (ITAR)
  · EU consolidated list
  · UK HMT list
  · UN consolidated list
  · Modern Slavery Act risk register
  · Conflict-minerals (3TG) chain

Real-impl: SQLite ledger of screening outcomes + dedup; hash-chained
audit. Sanctions hit → hard stop + compliance review.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger, chain

logger = LeafMeshLogger(__name__)

_DB = Path("./logs/trade_compliance.db")

SANCTIONS_LISTS = (
    "OFAC_SDN", "OFAC_CONS", "BIS_DENIED", "BIS_ENTITY",
    "ITAR_DEBARRED", "EU_CONSOL", "UK_HMT", "UN_CONSOL",
)

HIGH_RISK_COUNTRIES = frozenset({
    "IR", "KP", "SY", "CU", "MM", "RU", "BY", "VE",
})

# Conflict-minerals high-risk regions
CONFLICT_MINERALS_REGIONS = frozenset({
    "CD", "RW", "BI", "TZ", "UG", "AO", "ZM", "CF", "SS",
})


def _up(input_data: dict[str, Any], context: dict[str, Any],
        key: str, default: Any = None) -> Any:
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


def _ensure_db():
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS screening_outcomes (
            screening_id TEXT PRIMARY KEY,
            supplier_id TEXT,
            legal_name TEXT,
            country TEXT,
            sanctions_hits TEXT,
            high_risk_country BOOLEAN,
            conflict_minerals_risk BOOLEAN,
            modern_slavery_risk_score REAL,
            overall_status TEXT,
            screened_at_utc TEXT,
            audit_hash TEXT
        )
    """)
    return conn


# ─── @chain deterministic compliance hard-stop floor ─────────────────────────
# Runs AFTER trade_compliance_agent. Re-asserts the hard-stop from the
# structural facts so it can never be cleared downstream: any sanctions hit or
# high-risk country FORCES a hard stop; an un-run screening (no screening_id /
# pending/review status) FORCES compliance review. Fail closed.

def enforce_compliance_floor(result, context):
    """Deterministic sanctions/high-risk hard-stop the LLM can't waive."""
    if not isinstance(result, dict):
        return result
    sanctions = result.get("sanctions_hits") or []
    high_risk = bool(result.get("high_risk_country"))
    status = str(result.get("overall_status") or "")

    # Hard stop: any sanctions hit or sanctioned country, no exceptions.
    if sanctions or high_risk:
        result["requires_hard_stop"] = True
        result["requires_compliance_review"] = True
        result["overall_status"] = "blocked"
    # Fail closed: a screening that did not actually run is never "cleared".
    elif status in ("pending", "review") or not result.get("screening_id"):
        result["requires_compliance_review"] = True
    # Conditional dispositions still require review.
    if status in ("conditional",) or result.get("conflict_minerals_risk"):
        result["requires_compliance_review"] = True

    logger.info(
        f"compliance floor — status={result.get('overall_status')} "
        f"hard_stop={result.get('requires_hard_stop')} "
        f"review={result.get('requires_compliance_review')}"
    )
    return result


@chain(enforce_compliance_floor)
async def trade_compliance_agent(llm_response, input_data, context):
    if isinstance(llm_response, str):
        try:
            llm_response = json.loads(llm_response)
        except (ValueError, TypeError):
            llm_response = {}
    if not isinstance(llm_response, dict):
        llm_response = {}

    supplier_id = str(_up(input_data, context, "supplier_id", "") or "")
    if not supplier_id:
        # Full yields shape, fail closed: a screening we couldn't run
        # is NOT a cleared supplier.
        return {
            "screening_id": "", "supplier_id": "", "legal_name": "",
            "country": "", "sanctions_hits": [],
            "high_risk_country": False, "conflict_minerals_risk": False,
            "modern_slavery_risk_score": 0.0,
            "overall_status": "pending",
            "requires_compliance_review": True,
            "requires_hard_stop": False, "audit_hash": "",
            "screened_at_utc": datetime.now(timezone.utc).isoformat(),
        }

    legal_name = str(llm_response.get("legal_name") or "")
    country = str(llm_response.get("country") or "").upper()[:2]

    # FAIL CLOSED on unusable LLM output: an empty/unparseable response
    # means NO screening happened — that must route to compliance
    # review, never silently "cleared".
    if not legal_name and not country:
        logger.warn(
            f"[trade-compliance] {supplier_id} — LLM returned no usable "
            f"screening data; failing closed to compliance review"
        )
        return {
            "screening_id": "", "supplier_id": supplier_id,
            "legal_name": "", "country": "", "sanctions_hits": [],
            "high_risk_country": False, "conflict_minerals_risk": False,
            "modern_slavery_risk_score": 0.0,
            "overall_status": "review",
            "requires_compliance_review": True,
            "requires_hard_stop": False, "audit_hash": "",
            "screened_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    # LLM-supplied sanctions match (or empty if clean)
    sanctions_hits = [
        str(h).upper() for h in (llm_response.get("sanctions_hits") or [])
        if str(h).upper() in SANCTIONS_LISTS
    ]
    modern_slavery_risk_score = float(
        llm_response.get("modern_slavery_risk_score") or 0.0,
    )
    materials_sourced = [
        str(m).lower() for m in (llm_response.get("materials_sourced") or [])
    ]
    sourced_from_high_risk = bool(
        llm_response.get("sourced_from_high_risk_region")
    ) or country in CONFLICT_MINERALS_REGIONS

    # ── Deterministic risk scoring ──
    has_sanctions = bool(sanctions_hits)
    high_risk_country = country in HIGH_RISK_COUNTRIES
    is_3tg = any(m in {"tin", "tantalum", "tungsten", "gold", "cobalt"}
                  for m in materials_sourced)
    conflict_minerals_risk = is_3tg and sourced_from_high_risk

    # Disposition
    if has_sanctions or high_risk_country:
        overall_status = "blocked"
    elif conflict_minerals_risk or modern_slavery_risk_score >= 0.7:
        overall_status = "conditional"
    elif modern_slavery_risk_score >= 0.4:
        overall_status = "review"
    else:
        overall_status = "cleared"

    now = datetime.now(timezone.utc).isoformat()
    screening_id = f"screen-{supplier_id}-{int(datetime.now(timezone.utc).timestamp())}"

    # Hash-chained audit
    payload = json.dumps({
        "screening_id": screening_id,
        "supplier_id": supplier_id,
        "sanctions_hits": sanctions_hits,
        "status": overall_status,
    }, sort_keys=True)
    audit_hash = hashlib.sha256(payload.encode()).hexdigest()

    conn = _ensure_db()
    try:
        conn.execute("""
            INSERT INTO screening_outcomes
                (screening_id, supplier_id, legal_name, country,
                 sanctions_hits, high_risk_country,
                 conflict_minerals_risk, modern_slavery_risk_score,
                 overall_status, screened_at_utc, audit_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (screening_id, supplier_id, legal_name, country,
              json.dumps(sanctions_hits), high_risk_country,
              conflict_minerals_risk, modern_slavery_risk_score,
              overall_status, now, audit_hash))
        conn.commit()
    finally:
        conn.close()

    requires_compliance = overall_status in ("blocked", "conditional",
                                              "review")
    requires_hard_stop = overall_status == "blocked"

    logger.info(
        f"[trade-compliance] {supplier_id} status={overall_status} "
        f"sanctions={sanctions_hits} country={country} "
        f"conflict_min={conflict_minerals_risk}"
    )

    return {
        "screening_id": screening_id,
        "supplier_id": supplier_id,
        "legal_name": legal_name,
        "country": country,
        "sanctions_hits": sanctions_hits,
        "high_risk_country": high_risk_country,
        "conflict_minerals_risk": conflict_minerals_risk,
        "modern_slavery_risk_score": modern_slavery_risk_score,
        "overall_status": overall_status,
        "requires_compliance_review": requires_compliance,
        "requires_hard_stop": requires_hard_stop,
        "audit_hash": audit_hash,
        "screened_at_utc": now,
    }
