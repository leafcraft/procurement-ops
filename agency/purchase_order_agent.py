"""Programmatic Agent — Purchase Order generation from the approved logistics plan.

auto_discover matches function name 'purchase_order_agent' to YAML agent config.

Takes the structured procurement_plan from logistics_agent (recovered from Redis
after po_approval_agent HITL) and produces formal PO documents grouped by supplier.

Each PO contains:
  - Auto-generated PO number (PO-YYYYMMDD-XXXX)
  - Line items with SKU, quantity, unit price, total
  - Supplier contact and payment terms
  - Requested delivery date (today + lead_time_days)
  - Internal cost centre and approval reference
"""
import os
import random
import string
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from leafmesh import LeafMeshLogger, chain, load_yaml_config
from leafmesh.redis import RedisManager

try:
    from .data_loader import load_open_pos, save_open_pos
    from ._shared.session_stash import resolve_session_id, stash_get
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from data_loader import load_open_pos, save_open_pos
    from _shared.session_stash import resolve_session_id, stash_get

logger = LeafMeshLogger(__name__)

_PO_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "purchase_orders"


def _generate_pdf(po: dict) -> str:
    """Render a single PO dict to a PDF file. Returns the file path."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable,
    )

    _PO_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = _PO_OUTPUT_DIR / f"{po['po_number']}.pdf"

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("POTitle", parent=styles["Title"], fontSize=18, spaceAfter=4)
    heading_style = ParagraphStyle("POHeading", parent=styles["Normal"], fontSize=10, textColor=colors.grey)
    normal = styles["Normal"]
    normal.fontSize = 9

    brand_color = colors.HexColor("#1a3a5c")
    accent_color = colors.HexColor("#2e86c1")

    elements = []

    # ── Header ──
    elements.append(Paragraph("PURCHASE ORDER", title_style))
    elements.append(Paragraph(f"<b>{po['po_number']}</b>", heading_style))
    elements.append(HRFlowable(width="100%", thickness=2, color=brand_color))
    elements.append(Spacer(1, 6 * mm))

    # ── Meta table ──
    meta = [
        ["Issued At", po.get("issued_at", "")[:19].replace("T", " ") + " UTC"],
        ["Supplier", po.get("supplier", "")],
        ["Contact Email", po.get("contact_email", "") or "—"],
        ["Payment Terms", po.get("payment_terms", "NET30")],
        ["Requested Delivery", po.get("requested_delivery_date", "")],
        ["Lead Time", f"{po.get('lead_time_days', 0)} days"],
        ["Approval Reference", po.get("approval_reference", "")],
        ["Currency", po.get("currency", "USD")],
    ]
    meta_table = Table(meta, colWidths=[55 * mm, 110 * mm])
    meta_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (0, -1), brand_color),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f4f8fb")]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d0dce8")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elements.append(meta_table)
    elements.append(Spacer(1, 8 * mm))

    # ── Line items ──
    elements.append(Paragraph("<b>Line Items</b>", ParagraphStyle("sub", fontSize=11, textColor=brand_color)))
    elements.append(Spacer(1, 3 * mm))

    header_row = ["#", "SKU", "Description", "Category", "Qty", "Unit Price", "Total", "Urgency"]
    rows = [header_row]
    for line in po.get("lines", []):
        rows.append([
            str(line.get("line_number", "")),
            line.get("sku", ""),
            line.get("description", ""),
            line.get("category", ""),
            str(line.get("quantity", "")),
            f"${line.get('unit_price', 0):,.2f}",
            f"${line.get('total_price', 0):,.2f}",
            line.get("urgency", "normal"),
        ])

    col_widths = [8 * mm, 22 * mm, 42 * mm, 22 * mm, 12 * mm, 18 * mm, 18 * mm, 16 * mm]
    items_table = Table(rows, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), brand_color),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f8fb")]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d0dce8")),
        ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 6 * mm))

    # ── Subtotal ──
    subtotal_data = [
        ["", "", "", "", "", "", "Subtotal:", po.get("subtotal_formatted", "")],
    ]
    subtotal_table = Table(subtotal_data, colWidths=col_widths)
    subtotal_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (6, 0), (6, 0), brand_color),
        ("TEXTCOLOR", (7, 0), (7, 0), accent_color),
        ("ALIGN", (6, 0), (-1, -1), "RIGHT"),
        ("LINEABOVE", (6, 0), (-1, -1), 1, brand_color),
    ]))
    elements.append(subtotal_table)

    # ── Footer ──
    elements.append(Spacer(1, 10 * mm))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    elements.append(Spacer(1, 3 * mm))
    elements.append(Paragraph(
        "This is a system-generated purchase order. Please confirm receipt and expected delivery date.",
        ParagraphStyle("footer", fontSize=7, textColor=colors.grey),
    ))

    doc.build(elements)
    return str(pdf_path)


def _po_number() -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"PO-{today}-{suffix}"


def _delivery_date(lead_time_days: int) -> str:
    delivery = datetime.now(timezone.utc) + timedelta(days=lead_time_days)
    return delivery.strftime("%Y-%m-%d")


def _po_is_valid(po: dict) -> tuple[bool, list[str]]:
    """Deterministic PO-validity check. A PO must carry an approval reference
    and every line must name a real supplier with a positive quantity and a
    positive unit price. Returns (is_valid, reasons_invalid)."""
    reasons: list[str] = []
    if not str(po.get("approval_reference") or "").strip():
        reasons.append("missing approval_reference")
    supplier = str(po.get("supplier") or "").strip()
    if not supplier or supplier.lower() in ("unknown supplier", "tbd", "none"):
        reasons.append(f"invalid supplier '{supplier or '∅'}'")
    lines = po.get("lines") or []
    if not lines:
        reasons.append("no line items")
    for ln in lines:
        if int(ln.get("quantity", 0) or 0) <= 0:
            reasons.append(f"{ln.get('sku', '?')}: non-positive quantity")
        if float(ln.get("unit_price", 0) or 0) <= 0:
            reasons.append(f"{ln.get('sku', '?')}: non-positive unit price")
    return (not reasons, reasons)


# ─── @chain deterministic PO-validity floor ──────────────────────────────────
# Runs AFTER purchase_order_agent. Rejects any PO that lacks an approval
# reference or contains invalid lines — a binding PO is never issued/registered
# on bad data. Only POs that pass are registered in the open-PO ledger.

def register_valid_pos(result, context):
    """Validate generated POs, register only the valid ones, fail closed otherwise."""
    if not isinstance(result, dict):
        return result
    pos = result.get("purchase_orders") or []
    if not pos:
        return result  # nothing generated (rejection / no-plan paths)

    invalid: list[str] = []
    valid_pos: list[dict] = []
    for po in pos:
        ok, reasons = _po_is_valid(po)
        if ok:
            valid_pos.append(po)
        else:
            invalid.append(f"{po.get('po_number', '?')}: {'; '.join(reasons)}")
            logger.error(f"PO validity floor REJECTED {po.get('po_number', '?')} — {reasons}")

    if invalid:
        # Fail closed: do NOT register or claim issuance when any PO is invalid.
        result["status"] = "po_validation_failed"
        result["validation_errors"] = invalid
        logger.error(f"PO validation FAILED — {len(invalid)} invalid PO(s); none registered")
        return result

    # ─── Register only validated POs as open for in-transit tracking ───
    try:
        open_pos = load_open_pos()
        for po in valid_pos:
            open_pos[po["po_number"]] = {
                "po_number": po["po_number"],
                "status": "open",
                "issued_at": po["issued_at"],
                "supplier": po["supplier"],
                "expected_delivery_date": po["requested_delivery_date"],
                "approval_reference": po["approval_reference"],
                "currency": po.get("currency", "USD"),
                "lines": [
                    {
                        "sku": line["sku"],
                        "description": line["description"],
                        "quantity_ordered": line["quantity"],
                        "quantity_received": 0,
                        "unit_price": line["unit_price"],
                        "currency": line.get("currency", "USD"),
                        "last_received_at": None,
                    }
                    for line in po.get("lines", [])
                ],
            }
        save_open_pos(open_pos)
        logger.info(f"Registered {len(valid_pos)} validated PO(s) in open_pos.yaml")
    except Exception as e:
        logger.warning(f"Failed to register POs in open_pos.yaml: {e}")

    result["validation_errors"] = []
    return result


@chain(register_valid_pos)
async def purchase_order_agent(llm_response, input_data: dict, context: dict) -> dict:
    """Generate formal PO documents from the approved procurement plan."""
    if not isinstance(input_data, dict):
        try:
            import json as _json
            parsed = _json.loads(input_data) if isinstance(input_data, str) else {}
            input_data = parsed if isinstance(parsed, dict) else {}
        except Exception:
            input_data = {}
    if not isinstance(context, dict):
        context = {}
    upstream = input_data.get("upstream_yields", {})
    # SDK fan-in puts results in upstream_results when upstream_yields is empty
    upstream_results = input_data.get("upstream_results", {})
    if not upstream and upstream_results:
        upstream = upstream_results

    # After a HITL step the human agent only returns the operator response —
    # upstream_yields is absent from input_data. Fall back to the session context
    # which LeafMesh populates with all prior agent yields for the session.
    if not upstream:
        upstream = (
            context.get("upstream_yields")
            or context.get("session_yields")
            or context.get("agent_yields")
            or {}
        )

    # po_approval_agent is now the single approval gate (finance merged into it)
    finance_raw = upstream.get("po_approval_agent", {})
    finance_yields = finance_raw.get("main_result") or finance_raw

    # After HITL, LeafMesh drops upstream_yields — only the human response arrives.
    # Recover logistics_agent data from Redis session storage using the session_id
    # that LeafMesh always includes in original_webhook_data.
    session_id = (
        context.get("session_id")
        or input_data.get("session_id")
        or input_data.get("original_webhook_data", {}).get("session_id")
        or finance_yields.get("original_webhook_data", {}).get("session_id")
        or resolve_session_id(input_data, context)
        or ""
    )

    # In-process session stash written by logistics_agent before the HITL
    # gate — survives the human wait even when Redis is unavailable.
    stash_logistics: dict = stash_get(session_id) if session_id else {}

    redis_logistics: dict = {}
    if session_id:
        try:
            import os
            _config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "config.yaml")
            _cfg = load_yaml_config(_config_path)
            rm = RedisManager(_cfg.redis, project_name=_cfg.name)
            await rm.connect()
            raw = await rm.get_agent_yields(session_id, "logistics_agent") or {}
            # get_agent_yields wraps the data: {"yields": {...}, "timestamp": ..., ...}
            redis_logistics = raw.get("yields") or raw
            logger.info(f"purchase_order_agent — Redis session lookup: session_id={session_id}, "
                        f"logistics keys={list(redis_logistics.keys())}")
        except Exception as e:
            logger.warning(f"purchase_order_agent — Redis session lookup failed: {e}")

    # logistics data priority:
    # 1. Direct upstream (no HITL path — logistics_agent called purchase_order_agent directly)
    # 2. In-process session stash (post-HITL path — Redis-independent)
    # 3. Redis session storage (post-HITL path — recovered via session_id)
    # 4. input_data top-level (direct entry-point call)
    context_upstream = (
        context.get("upstream_yields")
        or context.get("session_yields")
        or context.get("agent_yields")
        or {}
    )
    logistics_raw = (
        upstream.get("logistics_agent")
        or context_upstream.get("logistics_agent")
        or stash_logistics
        or redis_logistics
        or {}
    )
    logistics_yields = logistics_raw.get("main_result") or logistics_raw

    procurement_plan: list[dict] = (
        logistics_yields.get("procurement_plan")
        or finance_yields.get("procurement_plan")
        or input_data.get("procurement_plan", [])
    )
    recommended_supplier: str = (
        logistics_yields.get("recommended_supplier")
        or finance_yields.get("recommended_supplier")
        or input_data.get("recommended_supplier", "")
    )
    # human_message comes from po_approval_agent (webhook HITL) or auto-approval fallback.
    human_message = (
        finance_yields.get("human_message")
        or input_data.get("human_message", "")
    ).lower()

    _rejected = {"rejected", "reject", "denied", "deny", "cancel", "cancelled", "no"}
    if human_message and any(word in human_message for word in _rejected):
        logger.info(f"purchase_order_agent — rejected via Slack: '{human_message}' — no POs generated")
        return {
            "purchase_orders": [],
            "po_numbers": [],
            "total_po_value": 0.0,
            "total_po_value_formatted": "$0.00",
            "supplier_count": 0,
            "status": "rejected_by_finance",
        }

    approval_reference: str = (
        finance_yields.get("approval_reference")
        or ("APPROVED" if human_message else "AUTO-APPROVED")
    )

    if not procurement_plan:
        logger.warning(
            "purchase_order_agent — no procurement plan received. "
            f"session_id={session_id}, "
            f"upstream keys: {list(upstream.keys())}, "
            f"finance_yields keys: {list(finance_yields.keys())}, "
            f"stash_logistics keys: {list(stash_logistics.keys())}, "
            f"redis_logistics keys: {list(redis_logistics.keys())}, "
            f"input_data keys: {list(input_data.keys())}"
        )
        return {
            "purchase_orders": [],
            "po_numbers": [],
            "total_po_value": 0.0,
            "total_po_value_formatted": "$0.00",
            "supplier_count": 0,
            "status": "no_orders_generated",
        }

    # ─── Group plan lines by supplier ───
    supplier_groups: dict[str, list[dict]] = {}
    for line in procurement_plan:
        if not isinstance(line, dict):
            continue
        supplier = line.get("supplier", "Unknown Supplier")
        supplier_groups.setdefault(supplier, []).append(line)

    # ─── Generate one PO per supplier ───
    purchase_orders: list[dict] = []
    po_numbers: list[str] = []
    total_po_value = 0.0
    issued_at = datetime.now(timezone.utc).isoformat()

    for supplier, lines in supplier_groups.items():
        po_num = _po_number()
        po_numbers.append(po_num)

        # Max lead time across this supplier's lines → delivery date
        max_lead = max(l.get("lead_time_days", 14) for l in lines)
        po_subtotal = sum(l.get("total_cost", 0) for l in lines)
        total_po_value += po_subtotal

        # Use contact info and currency from first line (same supplier = same details)
        contact_email = lines[0].get("contact_email", "")
        payment_terms = lines[0].get("payment_terms", "NET30")
        # Supplier's native invoice currency — may be USD, EUR, or CNY
        currency = lines[0].get("currency", "USD").upper()
        currency_symbol = {"USD": "$", "EUR": "€", "CNY": "¥", "INR": "₹"}.get(currency, currency + " ")
        non_usd = currency != "USD"

        po_lines = [
            {
                "line_number": idx + 1,
                "sku": l.get("sku", ""),
                "description": l.get("description", ""),
                "category": l.get("category", ""),
                "quantity": l.get("quantity", 0),
                "unit_price": l.get("unit_price", 0.0),
                "total_price": l.get("total_cost", 0.0),
                "urgency": l.get("urgency", "normal"),
                "action": l.get("action", "ORDER"),
            }
            for idx, l in enumerate(lines)
        ]

        po_record = {
            "po_number": po_num,
            "issued_at": issued_at,
            "supplier": supplier,
            "contact_email": contact_email,
            "payment_terms": payment_terms,
            "requested_delivery_date": _delivery_date(max_lead),
            "lead_time_days": max_lead,
            "approval_reference": approval_reference,
            "lines": po_lines,
            "line_count": len(po_lines),
            "subtotal": round(po_subtotal, 2),
            "subtotal_formatted": f"{currency_symbol}{po_subtotal:,.2f}",
            "currency": currency,
            "status": "issued",
        }
        if non_usd:
            po_record["currency_note"] = (
                f"Invoice currency is {currency}. Finance must apply exchange rate "
                f"before payment. Amounts shown are in {currency} as quoted by supplier."
            )

        purchase_orders.append(po_record)

    total_po_value = round(total_po_value, 2)

    # ─── Generate PDFs ───
    pdf_paths: list[str] = []
    for po in purchase_orders:
        try:
            pdf_path = _generate_pdf(po)
            po["pdf_path"] = pdf_path
            pdf_paths.append(pdf_path)
            logger.info(f"purchase_order_agent — PDF generated: {pdf_path}")
        except Exception as e:
            logger.warning(f"purchase_order_agent — PDF generation failed for {po['po_number']}: {e}")

    logger.info(
        f"Purchase orders generated — {len(purchase_orders)} POs across "
        f"{len(supplier_groups)} supplier(s), total value: ${total_po_value:,.2f}"
    )

    # NOTE: open-PO registration is performed by the register_valid_pos @chain
    # floor below — ONLY after each PO passes deterministic validity checks.
    return {
        "purchase_orders": purchase_orders,
        "po_numbers": po_numbers,
        "pdf_paths": pdf_paths,
        "total_po_value": total_po_value,
        "total_po_value_formatted": f"${total_po_value:,.2f}",
        "supplier_count": len(purchase_orders),
        "line_count": len(procurement_plan),
        "issued_at": issued_at,
        "approval_reference": approval_reference,
        "status": "po_generated",
    }
