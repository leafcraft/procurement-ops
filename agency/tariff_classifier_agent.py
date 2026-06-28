"""tariff_classifier_agent — HTS code + duty estimation for inbound goods.

For international procurement, every line item needs:
  · HTS code (Harmonized Tariff Schedule, 10-digit US; 8-digit EU CN)
  · Country of origin
  · Duty rate (general / column-2 / special programs)
  · Landed-cost estimate

LLM proposes HTS classification from item description; deterministic
post-process looks up the duty rate from a cached schedule and
computes the landed cost.

Real installs replace `_HTS_DUTY_CACHE` with an HTS API integration
(USITC, Avalara Cross-Border, etc.).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger, chain

logger = LeafMeshLogger(__name__)

_DB = Path("./logs/tariff_classifier.db")

# Cached HTS duty rates (illustrative — production uses real HTS lookup).
# Keys are 4-digit headings (the most common granularity for duty rate).
_HTS_DUTY_CACHE: dict[str, dict[str, float]] = {
    # Chapter 84 — Machinery
    "8473": {"general": 0.00, "column_2": 0.35, "fta_usmca": 0.00},
    "8471": {"general": 0.00, "column_2": 0.35, "fta_usmca": 0.00},
    # Chapter 85 — Electrical equipment
    "8517": {"general": 0.00, "column_2": 0.35, "fta_usmca": 0.00},
    "8542": {"general": 0.00, "column_2": 0.35, "fta_usmca": 0.00},
    # Chapter 61/62 — Apparel
    "6109": {"general": 0.169, "column_2": 0.90, "fta_usmca": 0.00},
    "6203": {"general": 0.275, "column_2": 0.90, "fta_usmca": 0.00},
    # Chapter 73 — Iron/steel articles
    "7308": {"general": 0.00, "column_2": 0.45, "fta_usmca": 0.00,
              "section_232_tariff": 0.25},
    # Default fallback
    "default": {"general": 0.025, "column_2": 0.35, "fta_usmca": 0.00},
}

# FTA / trade-program codes by country
COUNTRY_FTA = {
    "MX": "fta_usmca",  "CA": "fta_usmca",
    "AU": "fta_au",     "SG": "fta_sg",  "CL": "fta_cl",
    "PE": "fta_pe",     "CO": "fta_co",  "PA": "fta_pa",
    "KR": "fta_kr",
    "IL": "fta_il",
}

# Countries on column 2 (non-MFN)
COLUMN_2_COUNTRIES = frozenset({"KP", "CU"})

# Section 232 steel/aluminum tariff-prone HTS chapters
SECTION_232_CHAPTERS = frozenset({"72", "73", "76"})


def _ensure_db():
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS classifications (
            classification_id TEXT PRIMARY KEY,
            line_item_id TEXT,
            hts_code TEXT,
            country_of_origin TEXT,
            base_value_cents INTEGER,
            duty_rate REAL,
            duty_cents INTEGER,
            landed_cost_cents INTEGER,
            rule_applied TEXT,
            confidence REAL,
            classified_at_utc TEXT
        )
    """)
    return conn


def _resolve_duty(hts: str, country: str) -> tuple[float, str]:
    """Return (duty_rate, rule_applied)."""
    heading = (hts or "0000")[:4]
    rates = _HTS_DUTY_CACHE.get(heading, _HTS_DUTY_CACHE["default"])

    # Column 2 (severely-restricted MFN status)
    if country in COLUMN_2_COUNTRIES:
        return (rates.get("column_2", 0.35), "column_2")

    # FTA-eligible origins
    fta_key = COUNTRY_FTA.get(country)
    if fta_key and fta_key in rates:
        return (rates[fta_key], fta_key)

    # Section 232 steel/aluminum surcharge
    section_232 = 0.0
    if heading[:2] in SECTION_232_CHAPTERS \
       and "section_232_tariff" in rates:
        section_232 = rates["section_232_tariff"]

    return (rates.get("general", 0.025) + section_232,
            f"general_mfn{'_plus_232' if section_232 else ''}")


# ─── @chain deterministic customs hard-stop floor ────────────────────────────
# Runs AFTER tariff_classifier_agent. Re-asserts the customs-broker review
# requirement from structural facts: any low-confidence classification, any
# Section 232 surcharge, or any per-line confidence < 0.70 FORCES broker
# review. The LLM cannot clear an under-confident classification.

_CONFIDENCE_FLOOR = 0.70


def enforce_customs_floor(result, context):
    """Deterministic customs-broker review hard-stop the LLM can't waive."""
    if not isinstance(result, dict):
        return result
    classifications = result.get("classifications") or []
    low_conf = any(
        float(c.get("confidence", 0) or 0) < _CONFIDENCE_FLOOR
        for c in classifications
    )
    if low_conf:
        result["has_low_confidence_items"] = True
    if result.get("has_low_confidence_items") or result.get("has_section_232_tariff"):
        result["requires_customs_broker_review"] = True

    logger.info(
        f"customs floor — low_conf={result.get('has_low_confidence_items')} "
        f"section_232={result.get('has_section_232_tariff')} "
        f"broker_review={result.get('requires_customs_broker_review')}"
    )
    return result


@chain(enforce_customs_floor)
async def tariff_classifier_agent(llm_response, input_data, context):
    if isinstance(llm_response, str):
        try:
            llm_response = json.loads(llm_response)
        except (ValueError, TypeError):
            llm_response = {}
    if not isinstance(llm_response, dict):
        llm_response = {}

    line_items = list(llm_response.get("line_items")
                      or input_data.get("line_items") or [])
    if not line_items:
        return {
            "classifications": [], "classification_count": 0,
            "total_base_cents": 0, "total_duty_cents": 0,
            "total_landed_cost_cents": 0, "effective_duty_pct": 0.0,
            "has_low_confidence_items": False,
            "has_section_232_tariff": False,
            "requires_customs_broker_review": False,
            "classified_at_utc": datetime.now(timezone.utc).isoformat(),
        }

    classifications: list[dict[str, Any]] = []
    total_duty_cents = 0
    total_base_cents = 0
    has_low_confidence = False
    has_section_232 = False

    now = datetime.now(timezone.utc).isoformat()
    conn = _ensure_db()
    try:
        for item in line_items:
            if not isinstance(item, dict):
                continue
            line_item_id = str(item.get("line_item_id") or "")
            hts = str(item.get("hts_code") or "").replace(".", "")
            country = str(item.get("country_of_origin") or "").upper()[:2]
            base_value_cents = int(item.get("base_value_cents") or 0)
            confidence = float(item.get("classification_confidence") or 0.0)

            if not (line_item_id and hts and country and base_value_cents):
                continue

            duty_rate, rule = _resolve_duty(hts, country)
            duty_cents = int(base_value_cents * duty_rate)
            landed_cents = base_value_cents + duty_cents

            classification_id = (f"hts-{line_item_id}-"
                                 f"{int(datetime.now(timezone.utc).timestamp())}")
            conn.execute("""
                INSERT INTO classifications
                    (classification_id, line_item_id, hts_code,
                     country_of_origin, base_value_cents, duty_rate,
                     duty_cents, landed_cost_cents, rule_applied,
                     confidence, classified_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (classification_id, line_item_id, hts, country,
                  base_value_cents, duty_rate, duty_cents,
                  landed_cents, rule, confidence, now))

            entry = {
                "classification_id": classification_id,
                "line_item_id": line_item_id,
                "hts_code": hts,
                "country_of_origin": country,
                "base_value_cents": base_value_cents,
                "duty_rate": round(duty_rate, 4),
                "duty_cents": duty_cents,
                "landed_cost_cents": landed_cents,
                "rule_applied": rule,
                "confidence": confidence,
            }
            classifications.append(entry)
            total_duty_cents += duty_cents
            total_base_cents += base_value_cents

            if confidence < 0.70:
                has_low_confidence = True
            if "232" in rule:
                has_section_232 = True

        conn.commit()
    finally:
        conn.close()

    landed_total = total_base_cents + total_duty_cents
    effective_duty_pct = (
        round((total_duty_cents / total_base_cents), 4)
        if total_base_cents else 0.0
    )

    logger.info(
        f"[tariff-cls] items={len(classifications)} "
        f"duty=${total_duty_cents/100:.2f} landed=${landed_total/100:.2f} "
        f"effective_pct={effective_duty_pct} "
        f"low_conf={has_low_confidence} section_232={has_section_232}"
    )

    return {
        "classifications": classifications,
        "classification_count": len(classifications),
        "total_base_cents": total_base_cents,
        "total_duty_cents": total_duty_cents,
        "total_landed_cost_cents": landed_total,
        "effective_duty_pct": effective_duty_pct,
        "has_low_confidence_items": has_low_confidence,
        "has_section_232_tariff": has_section_232,
        "requires_customs_broker_review": (
            has_low_confidence or has_section_232
            # Items were submitted but NONE classified — the LLM output
            # was unusable. Fail closed: broker must look.
            or (len(line_items) > 0 and len(classifications) == 0)
        ),
        "classified_at_utc": now,
    }
