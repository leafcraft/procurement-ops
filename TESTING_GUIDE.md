# Supply Chain Agent System — Complete Testing Guide

**Project:** procurement-ops  
**Architecture:** LeafMesh multi-agent mesh  
**Last Updated:** 2026-04-25

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Data Reference — SKUs and Suppliers](#2-data-reference--skus-and-suppliers)
3. [Agent Map](#3-agent-map)
4. [Pre-Test Setup](#4-pre-test-setup)
5. [Environment Verification](#5-environment-verification)
6. [Flow 1 — Full Supply Chain (process_order → HITL → Inventory → Suppliers → Logistics)](#6-flow-1--full-supply-chain)
7. [Flow 2 — Human Rejection (Negative Path)](#7-flow-2--human-rejection-negative-path)
8. [Flow 3 — Human-Initiated Contact (manager_contact)](#8-flow-3--human-initiated-contact)
9. [Flow 4 — Direct Inventory Check with Downstream Chain](#9-flow-4--direct-inventory-check-with-downstream-chain)
10. [Flow 5 — Direct Supplier Research with Memory](#10-flow-5--direct-supplier-research-with-memory)
11. [Flow 6 — Direct Logistics Planning](#11-flow-6--direct-logistics-planning)
12. [Flow 7 — On-Demand Supply Report chaining to Logistics](#12-flow-7--on-demand-supply-report)
13. [Flow 8 — HMAC Signature Security Tests](#13-flow-8--hmac-signature-security-tests)
14. [Flow 9 — Critical Stock Conditional Branch](#14-flow-9--critical-stock-conditional-branch)
15. [Flow 10 — Normal Stock (No Chain) Verification](#15-flow-10--normal-stock-no-chain-verification)
16. [Flow 11 — Supplier Race Condition (OR Fan-In)](#16-flow-11--supplier-race-condition-or-fan-in)
17. [Flow 12 — Supplier Composite Scoring and Competition](#17-flow-12--supplier-composite-scoring-and-competition)
18. [Flow 13 — ABC Class Priority Ordering](#18-flow-13--abc-class-priority-ordering)
19. [Flow 14 — Cross-Session Supplier Memory](#19-flow-14--cross-session-supplier-memory)
20. [Flow 15 — SSE Streaming Response](#20-flow-15--sse-streaming-response)
21. [Flow 16 — Scheduled Supply Report (Cron Trigger)](#21-flow-16--scheduled-supply-report-cron-trigger)
22. [Flow 17 — Session History and Audit Trail](#22-flow-17--session-history-and-audit-trail)
23. [Flow 18 — HITL Timeout Scenario](#23-flow-18--hitl-timeout-scenario)
24. [Per-Agent Verification Checklist](#24-per-agent-verification-checklist)
25. [API Endpoints Reference](#25-api-endpoints-reference)
26. [Common Issues and Fixes](#26-common-issues-and-fixes)
27. [Complete Test Sign-Off Checklist](#27-complete-test-sign-off-checklist)

---

## 1. System Overview

A **multi-agent supply chain management system** built on LeafMesh. Handles order intake, human approval, inventory checking, supplier research, logistics planning, and scheduled reporting — all wired as an autonomous agent mesh.

### Architecture

```
[Incoming Order]
      │
      ▼
order_intake_agent  ──►  operations_manager (HITL)
  (LLM, @pre_compose)        │
                       [Human Approves]
                             │
                             ▼
                     inventory_agent  ─────────────────────────┐
                    (programmatic,                             │
                     @conditional_chain,                       │
                     parallel fan-out)                         │
                      │              │                         │
                      ▼              ▼                         │
            supplier_agent   fallback_supplier_agent           │
            (LLM, @chain_    (programmatic, instant,           │
             with_results,    wins OR race)                    │
             cross-session                                     │
             memory)                                           │
                      │              │                         │
                      └──── OR ──────┘                         │
                                │                              │
                                ▼        ◄─────────────────────┘
                         logistics_agent
                    (LLM, @chain + @compose,
                     AND+OR fan-in, terminal)
                                │
                         [Procurement Plan]

[Scheduled / On-Demand]
supply_report_agent  ──►  logistics_agent
  (programmatic,            (real KPIs from
   live data KPIs,           configs/*.yaml)
   cron 0 8 * * *)
```

### Agent Types

| Agent | Type | Has LLM | Entry Point |
|---|---|---|---|
| order_intake_agent | LLM | Yes (gpt-4o-mini) | process_order |
| operations_manager | Human (HITL) | No | manager_contact |
| inventory_agent | Programmatic | No | check_inventory |
| supplier_agent | LLM | Yes (gpt-4o-mini) | find_suppliers |
| fallback_supplier_agent | Programmatic | No | (parallel only) |
| logistics_agent | LLM | Yes (gpt-4o-mini) | plan_logistics |
| supply_report_agent | Programmatic + Scheduled | No | supply_report |

### Data Sources

All inventory and supplier data is driven by configuration files — no hardcoded values in agent code:

| File | Contents | Loaded by |
|---|---|---|
| [configs/inventory.yaml](configs/inventory.yaml) | 19 SKUs with stock levels and metadata | inventory_agent, supply_report_agent |
| [configs/suppliers.yaml](configs/suppliers.yaml) | 15 suppliers, per-SKU options, performance KPIs | supplier_agent, fallback_supplier_agent, supply_report_agent |
| [agency/data_loader.py](agency/data_loader.py) | Shared loader with lru_cache | all agents above |

---

## 2. Data Reference — SKUs and Suppliers

### 2.1 Full SKU Catalog (`configs/inventory.yaml`)

| SKU | Name | Category | On Hand | Reorder Point | Safety Stock | Status | ABC |
|---|---|---|---|---|---|---|---|
| SKU-001 | Widget A | components | 15 | 50 | 20 | **CRITICAL** | A |
| SKU-002 | Component B | components | 5 | 30 | 10 | **CRITICAL** | B |
| SKU-003 | Precision Part C | parts | 200 | 75 | 25 | NORMAL | A |
| SKU-004 | Assembly D | assemblies | 0 | 20 | 5 | **CRITICAL** (stockout) | A |
| SKU-005 | Control Module E | modules | 80 | 60 | 15 | NORMAL | B |
| SKU-006 | PCB Board F | electronics | 35 | 60 | 15 | LOW | A |
| SKU-007 | Pressure Sensor G | electronics | 3 | 40 | 10 | **CRITICAL** | A |
| SKU-008 | Shipping Box H | packaging | 500 | 200 | 75 | NORMAL | C |
| SKU-009 | Ball Bearing I | components | 20 | 40 | 10 | LOW | C |
| SKU-010 | Stepper Motor J | assemblies | 45 | 30 | 10 | NORMAL | B |
| SKU-011 | Motor Controller K | electronics | 0 | 15 | 5 | **CRITICAL** (stockout) | A |
| SKU-012 | Shielded Cable L | components | 300 | 100 | 30 | NORMAL | C |
| SKU-013 | Drive Housing M | assemblies | 15 | 25 | 8 | LOW | B |
| SKU-014 | Power Supply N | electronics | 60 | 40 | 12 | NORMAL | A |
| SKU-015 | Shaft Seal O | components | 2 | 60 | 15 | **CRITICAL** | B |
| SKU-016 | Gear Pump P | assemblies | 25 | 15 | 5 | NORMAL | A |
| SKU-017 | Oil Filter Q | consumables | 30 | 50 | 15 | LOW | C |
| SKU-018 | Synthetic Lubricant R | consumables | 120 | 80 | 25 | NORMAL | C |
| SKU-019 | M8 Hex Bolt Set S | fasteners | 800 | 500 | 150 | NORMAL | C |
| SKU-020 | TFT Display T | electronics | 22 | 35 | 10 | LOW | A |

**Stock summary (use these groups throughout the test flows):**
- **CRITICAL (6 SKUs):** SKU-001, SKU-002, SKU-004, SKU-007, SKU-011, SKU-015
- **LOW (5 SKUs):** SKU-006, SKU-009, SKU-013, SKU-017, SKU-020
- **NORMAL (8 SKUs):** SKU-003, SKU-005, SKU-008, SKU-010, SKU-012, SKU-014, SKU-016, SKU-018, SKU-019

### 2.2 Supplier Roster (`configs/suppliers.yaml`)

| Supplier | Region | Reliability | Lead Time | Profile |
|---|---|---|---|---|
| SupplierCo Alpha | domestic | 0.95 | 5–10d | Reliable, mid-price, ISO-9001 + AS9100 |
| GlobalParts Ltd | asia | 0.88 | 14–18d | Cheap, long lead, bulk options |
| FastSource Inc | domestic | 0.92 | 2–5d | Fast domestic, premium price |
| BulkSupply Corp | asia | 0.79 | 21–28d | Lowest price, worst reliability, high MOQ |
| PrimeParts Co | domestic | 0.97 | 5–6d | Top quality, fast, ISO-9001 + ISO-13485 |
| EmergencyParts LLC | domestic | 0.90 | 2–3d | Emergency only — COD, highest price |
| TechComponents Asia | asia | 0.85 | 21–25d | Electronics specialist, CNY invoicing |
| EuroSource GmbH | europe | 0.98 | 8–16d | Highest reliability, CE/ATEX certified |
| NorthAmerica Parts | domestic | 0.93 | 5–8d | Consistent domestic, IATF-16949 |
| PrecisionMfg Co | domestic | 0.96 | 8–14d | High-precision, IATF-16949 |
| GreenSupply Co | domestic | 0.91 | 3–4d | Sustainable, ISO-14001, NSF-H1 |
| QuickShip Express | domestic | 0.89 | 1d | 1-day express, COD, for true emergencies |
| OceanFreight Ltd | asia | 0.82 | 45d | Cheapest by volume, ocean freight only |
| MegaDistribution | asia | 0.81 | 28–35d | Wide catalog, lowest quality tier |
| GeneralSupply Co | domestic | 0.80 | 14d | Fallback only — no catalog pricing |

### 2.3 Supplier Composite Scoring Formula

Used by `supplier_agent.evaluate_suppliers()` to rank options:

```
composite_score = (0.40 × reliability) + (0.35 × lead_score) + (0.25 × price_score)

where:
  lead_score  = max(0, 1 - lead_time_days / 30)      # shorter = higher score
  price_score = max(0, 1 - unit_price / 500)          # cheaper = higher score
```

**Expected ranking order** (higher score wins):
- High reliability + short lead + mid-price → EuroSource GmbH, PrimeParts Co, PrecisionMfg Co
- Fast but lower reliability → FastSource Inc, QuickShip Express
- Cheap but slow + unreliable → BulkSupply Corp, OceanFreight Ltd (lowest score)

---

## 3. Agent Map

### Data Flow and Yields

```
order_intake_agent
  yields: order_summary, items, urgency, order_type, risk_flags, detected_risk_level, status
  calls:  operations_manager

operations_manager
  yields: decision, from_agent, message
  routes: → order_intake_agent  (if no from_agent — human-initiated path)
          → inventory_agent     (if from_agent == 'order_intake_agent')

inventory_agent
  loads:  configs/inventory.yaml via data_loader
  yields: inventory_items, critical_items, item_count, reorder_needed, alert, status
  calls:  supplier_agent + fallback_supplier_agent + logistics_agent (parallel)
          (condition: item_count > 0)

supplier_agent
  loads:  configs/suppliers.yaml via data_loader
  yields: suppliers, recommended_supplier, estimated_lead_time_days,
          estimated_cost, analysis, report, llm_commentary,
          prior_evaluation_sessions, status
  calls:  logistics_agent (part of OR fan-in)

fallback_supplier_agent
  loads:  configs/suppliers.yaml via data_loader (fastest-delivery selection)
  yields: suppliers, recommended_supplier, estimated_lead_time_days,
          estimated_cost, analysis, report, status
  calls:  logistics_agent (part of OR fan-in)

logistics_agent
  wait_for: "inventory_agent AND (supplier_agent OR fallback_supplier_agent)"
  yields: procurement_plan, total_estimated_cost, fulfillment_timeline_days,
          risk_assessment, priority_score, next_steps,
          recommended_supplier, supplier_source, supplier_confidence,
          critical_items_count, status
  calls:  (terminal)

supply_report_agent
  loads:  configs/inventory.yaml + configs/suppliers.yaml via data_loader
  yields: report, generated_at, checks, metrics,
          inventory_items, item_count, critical_items, status
  calls:  logistics_agent (condition: status == 'report_generated')
```

### Decorator Patterns Per Agent

| Agent | Decorators | What They Do |
|---|---|---|
| order_intake_agent | @pre_compose | Runs context_processor, input_processor, others_processor before LLM; LLM returns JSON |
| inventory_agent | @conditional_chain | If any item critical → handle_critical_stock; else → handle_normal_stock |
| supplier_agent | @chain_with_results | evaluate_suppliers (score+rank) → format_supplier_report (format); results accumulated |
| logistics_agent | @chain + @compose | @compose injects helpers; then @chain runs validate_plan → score_priorities |
| supply_report_agent | (none) | Programmatic; runs on cron schedule or on-demand |

---

## 4. Pre-Test Setup

You need **4 terminals** open simultaneously. Do not close any between flows.

### Terminal 1 — Redis

```bash
docker compose up redis -d
```

Verify:

```bash
docker ps | grep redis
```

Expected: container is Up and healthy.

### Terminal 2 — HITL Stub Receiver

```bash
python hitl_stub_receiver.py
```

Expected output:

```
[HITL Stub] Listening on http://127.0.0.1:9999
```

This terminal prints outbound webhook payloads and the exact curl command to respond as the human approver. **Keep it visible at all times during HITL flows.**

### Terminal 3 — LeafMesh Server

```bash
python main.py
```

Expected startup output includes:

```
LeafMesh started on http://0.0.0.0:18820
Agents discovered: order_intake_agent, inventory_agent, supplier_agent,
  fallback_supplier_agent, logistics_agent, supply_report_agent, ...
Entry points registered: process_order, manager_contact, check_inventory,
  find_suppliers, plan_logistics, supply_report
```

Watch this terminal throughout all flows for agent execution logs, chain triggers, fan-in events, and any errors.

### Terminal 4 — Test Commands

Set these shell variables once at the start of your session:

```bash
MESH="http://127.0.0.1:18820"
SECRET=$(curl -s $MESH/api/webhook/secret | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('secret', d))")
echo "Secret: $SECRET"
```

---

## 5. Environment Verification

Run all checks before starting flows. All must pass.

### 5.1 Health Check

```bash
curl -s $MESH/health | python3 -m json.tool
```

Expected: `{"status": "ok"}`.

### 5.2 Entry Points

```bash
curl -s $MESH/api/mesh/entry_points | python3 -m json.tool
```

Expected: list containing all 6 entry points:
`process_order`, `manager_contact`, `check_inventory`, `find_suppliers`, `plan_logistics`, `supply_report`

### 5.3 Agent Registry — Verify All 7 Registered

```bash
curl -s $MESH/api/agents/ | python3 -m json.tool
```

Expected: all 7 agents listed. Verify `fallback_supplier_agent` is in the list — its absence is the single most common setup failure.

### 5.4 API Docs

Open in browser: `http://127.0.0.1:18820/docs`

Expected: ReDoc loads with all endpoints visible.

### 5.5 Webhook Secret

```bash
echo "Secret is: $SECRET"
```

Expected: non-empty string. If empty, re-run the secret fetch command from the setup section.

### 5.6 Data Files Reachable

```bash
python3 -c "
import yaml
inv = yaml.safe_load(open('configs/inventory.yaml'))
sup = yaml.safe_load(open('configs/suppliers.yaml'))
print(f'Inventory SKUs: {len(inv)}')
print(f'Supplier entries: {len([k for k in sup if k not in (\"default\",\"performance\")])}')
print(f'Performance records: {len(sup.get(\"performance\", {}))}')
"
```

Expected:
```
Inventory SKUs: 19
Supplier entries: 19
Performance records: 15
```

---

## 6. Flow 1 — Full Supply Chain

**Covers:** order_intake_agent → operations_manager (HITL) → inventory_agent → supplier_agent + fallback_supplier_agent (parallel) → logistics_agent

This is the primary end-to-end flow. Every agent is exercised.

---

### Step 1.1 — Trigger process_order

Order includes two CRITICAL SKUs (SKU-001, SKU-007) so the full critical-stock path fires.

```bash
RESPONSE=$(curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "process_order",
    "data": {
      "message": "Urgent order: need 50 units of SKU-001 (Widget A) and 10 units of SKU-007 (Pressure Sensor G) for a hospital client. Delivery required within 3 days."
    }
  }')
echo $RESPONSE | python3 -m json.tool
SESSION_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session ID: $SESSION_ID"
```

**What to verify:**
- Response contains a `session_id` — saved above
- Terminal 3 shows `order_intake_agent started`, LLM call initiated
- LLM is prompted to return structured JSON (see `prompt:` in config.yaml)

---

### Step 1.2 — Watch Terminal 2 for Outbound HITL Webhook

Within ~10–30 seconds Terminal 2 prints:

```json
{
  "session_id": "<id>",
  "agent_name": "operations_manager",
  "order_data": {
    "items": [
      {"sku": "SKU-001", "quantity": 50, "description": "Widget A"},
      {"sku": "SKU-007", "quantity": 10, "description": "Pressure Sensor G"}
    ],
    "urgency": "high",
    "detected_risk_level": "elevated"
  }
}
```

**What to verify:**
- `items` is a proper list — **NOT an empty `[]`** (this was the bug that was fixed)
- Both SKU-001 and SKU-007 extracted with quantities
- `urgency` is `high` (3-day delivery window) or `critical` if the LLM reads it as sub-24h
- `risk_flags` may include `high_quantity_order` (50+10 = 60 units)

---

### Step 1.3 — Respond as Human (Approve)

```bash
BODY="{\"session_id\": \"$SESSION_ID\", \"decision\": \"approved\", \"message\": \"Order verified. Hospital client is on approved vendor list. Proceed with procurement.\"}"
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -s -X POST $MESH/webhook/process_order \
  -H "Content-Type: application/json" \
  -H "X-LeafMesh-Signature: sha256=$SIG" \
  -d "$BODY" | python3 -m json.tool
```

**What to verify:**
- Returns HTTP 200
- Terminal 3: `operations_manager routing to inventory_agent` (`from_agent == order_intake_agent`)

---

### Step 1.4 — Watch Inventory Agent

**What to verify in Terminal 3 and results:**
- inventory_agent loads SKU-001 and SKU-007 from `configs/inventory.yaml`
- SKU-001: on_hand=15, safety_stock=20 → **CRITICAL**
- SKU-007: on_hand=3, safety_stock=10 → **CRITICAL**
- `critical_items` list has 2 entries
- `@conditional_chain` fires `handle_critical_stock` branch
- `alert`: "CRITICAL: 2 item(s) at zero or below safety stock — immediate procurement required"
- `reorder_needed: true`
- Parallel fork fires: `supplier_agent started` + `fallback_supplier_agent started` at nearly the same timestamp

---

### Step 1.5 — Watch Supplier Race (OR Fan-In)

**What to verify in Terminal 3:**
- `fallback_supplier_agent` completes within milliseconds (no LLM — fastest-delivery selection from catalog)
- For SKU-001: FastSource Inc or SupplierCo Alpha (short lead) chosen by fallback
- For SKU-007: EmergencyParts LLC (2d lead) chosen by fallback
- `logistics_agent` starts immediately after fallback completes
- Terminal 3: `fan-in satisfied: inventory_agent AND fallback_supplier_agent`
- `supplier_agent` (LLM) may still be running — OR already resolved

---

### Step 1.6 — Watch Logistics Agent Complete

**What to verify — outputs:**
- `procurement_plan` — IMMEDIATE entries for SKU-001 and SKU-007 (critical items get priority action lines)
- `total_estimated_cost` — sum of order quantities × unit prices from fallback supplier
- `fulfillment_timeline_days` — shortest available (fallback picks fastest-delivery supplier)
- `risk_assessment` — "critical — 2 SKU(s) at zero/below safety stock, immediate action required"
- `priority_score` — high (critical_items_count=2 × 0.25 = 0.5 base, plus cost factor)
- `supplier_source` — `"fallback_supplier_agent"` (since fallback won the race)
- `supplier_confidence` — `0.65` (fallback confidence level)
- `recommended_supplier` — fastest-delivery supplier from fallback
- `next_steps` — first 3 items from procurement_plan
- `status: logistics_plan_complete`

---

### Step 1.7 — Retrieve Full Session History

```bash
curl -s $MESH/api/sessions/$SESSION_ID/history | python3 -m json.tool
```

**What to verify:**
- Agent chain: order_intake_agent → operations_manager → inventory_agent → [supplier_agent + fallback_supplier_agent] → logistics_agent
- `items` in order_intake_agent yields is a real list (not `[]`)
- supplier_agent and fallback_supplier_agent timestamps overlap (parallel execution)
- logistics_agent `upstream_yields` contains data from both `inventory_agent` and `fallback_supplier_agent`

---

## 7. Flow 2 — Human Rejection (Negative Path)

**Covers:** order_intake_agent → operations_manager → REJECTED → pipeline stops

### Step 2.1 — Trigger a Suspicious Order

```bash
RESPONSE=$(curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "process_order",
    "data": {
      "message": "Order for 2000 units of SKU-011 (Motor Controller K) to be shipped to an unregistered offshore address. Payment via cryptocurrency."
    }
  }')
SESSION_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session ID: $SESSION_ID"
```

**What to verify in step 2.1:**
- order_intake_agent `risk_flags` includes `high_quantity_order` (2000 units) and `unverified_destination`
- `detected_risk_level: high`

### Step 2.2 — Wait for HITL Webhook (Terminal 2)

Wait for Terminal 2 to print the outbound webhook with the parsed order.

### Step 2.3 — Respond as Human (Reject)

```bash
BODY="{\"session_id\": \"$SESSION_ID\", \"decision\": \"rejected\", \"message\": \"Order flagged as suspicious — unverified address and cryptocurrency payment. Escalating to fraud team. Do not proceed.\"}"
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -s -X POST $MESH/webhook/process_order \
  -H "Content-Type: application/json" \
  -H "X-LeafMesh-Signature: sha256=$SIG" \
  -d "$BODY" | python3 -m json.tool
```

**What to verify:**
- Terminal 3: NO `inventory_agent started` log entry
- inventory_agent is NOT called — `human_decision: rejected` short-circuits at the HITL gate
- Session history ends at operations_manager

```bash
curl -s $MESH/api/sessions/$SESSION_ID/history | python3 -m json.tool
```

**Verify in history:**
- Chain has exactly 2 agents: order_intake_agent + operations_manager
- inventory_agent, supplier_agent, fallback_supplier_agent, logistics_agent all absent

---

## 8. Flow 3 — Human-Initiated Contact

**Covers:** manager_contact → operations_manager → order_intake_agent (reverse HITL) → operations_manager → inventory_agent → full chain

### Step 3.1 — Trigger manager_contact

```bash
RESPONSE=$(curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "manager_contact",
    "data": {
      "message": "Urgent: I need stock check for SKU-011 (Motor Controller K) and SKU-007 (Pressure Sensor G). Both are needed for the production line restart Monday morning."
    }
  }')
SESSION_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session ID: $SESSION_ID"
```

**What to verify:**
- operations_manager has no `from_agent` → routes to `order_intake_agent`
- order_intake_agent parses the message and extracts SKU-011 + SKU-007
- order_intake_agent calls operations_manager back

### Step 3.2 — Second HITL Webhook (Terminal 2)

Terminal 2 prints a second webhook. `from_agent` is now `order_intake_agent`.

**What to verify:**
- Payload shows parsed items: SKU-011 and SKU-007
- session_id matches Step 3.1

### Step 3.3 — Respond as Human (Approve)

```bash
BODY="{\"session_id\": \"$SESSION_ID\", \"decision\": \"approved\", \"message\": \"Confirmed — production line critical. Proceed with inventory check and find fastest suppliers.\"}"
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -s -X POST $MESH/webhook/process_order \
  -H "Content-Type: application/json" \
  -H "X-LeafMesh-Signature: sha256=$SIG" \
  -d "$BODY" | python3 -m json.tool
```

**What to verify:**
- `from_agent == order_intake_agent` → routes to inventory_agent
- SKU-011 (on_hand=0) and SKU-007 (on_hand=3, safety=10) both resolve as CRITICAL
- Full supplier race + logistics chain fires (same as Flow 1 Steps 1.4–1.7)

---

## 9. Flow 4 — Direct Inventory Check with Downstream Chain

**Covers:** inventory_agent → @conditional_chain → supplier_agent + fallback_supplier_agent → logistics_agent

Bypasses order intake and HITL entirely.

### Step 4.1 — Check a Mix of Critical and Low SKUs

```bash
RESPONSE=$(curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "check_inventory",
    "data": {
      "items": [
        {"sku": "SKU-001", "quantity": 50},
        {"sku": "SKU-006", "quantity": 20},
        {"sku": "SKU-004", "quantity": 5},
        {"sku": "SKU-003", "quantity": 30}
      ]
    }
  }')
SESSION_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session ID: $SESSION_ID"
```

**What to verify — inventory_agent outputs:**
- `inventory_items` — 4 entries, one per SKU
- SKU-001: stock_status=critical (on_hand=15 < safety=20)
- SKU-006: stock_status=low (on_hand=35, above safety=15, below reorder=60)
- SKU-004: stock_status=critical (on_hand=0)
- SKU-003: stock_status=normal (on_hand=200, above reorder=75)
- `critical_items` — SKU-001 and SKU-004
- `item_count: 4`
- `reorder_needed: true`
- `alert`: "CRITICAL: 2 item(s) at zero or below safety stock..."
- New fields present: `category`, `abc_class` (from inventory.yaml), `requested_quantity`, `units_needed`

### Step 4.2 — Verify @conditional_chain Fires

**What to verify in Terminal 3:**
- `@conditional_chain` evaluates — at least one critical item found → `handle_critical_stock` called
- `supplier_agent started` and `fallback_supplier_agent started` at overlapping timestamps
- Both run in parallel (parallel: true, max_concurrent: 3 in config)

### Step 4.3 — Verify Logistics Completes

```bash
curl -s $MESH/api/sessions/$SESSION_ID/history | python3 -m json.tool
```

**What to verify:**
- 4 agents: inventory_agent → [supplier_agent + fallback_supplier_agent] → logistics_agent
- No order_intake_agent or operations_manager in history

---

## 10. Flow 5 — Direct Supplier Research with Memory

**Covers:** supplier_agent — composite scoring, @chain_with_results, cross-session memory, → logistics_agent

### Step 5.1 — First Research Session

```bash
RESPONSE=$(curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "find_suppliers",
    "data": {
      "inventory_items": [
        {"sku": "SKU-007", "units_needed": 40, "requested_quantity": 10, "stock_status": "critical"},
        {"sku": "SKU-011", "units_needed": 15, "requested_quantity": 5, "stock_status": "critical"}
      ]
    }
  }')
SESSION_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session 1 ID: $SESSION_ID"
```

**What to verify — supplier_agent outputs:**

- `suppliers` — list with entries for SKU-007 and SKU-011
  - SKU-007 options from catalog: PrecisionMfg Co, EuroSource GmbH, EmergencyParts LLC, TechComponents Asia
  - SKU-011 options from catalog: TechComponents Asia, EuroSource GmbH, EmergencyParts LLC, MegaDistribution
  - Each entry has `composite_score` field

- **Scoring weights (verify these):**
  - `reliability * 0.40` — EuroSource GmbH (0.98) should score highest on this
  - `lead_score * 0.35` — EmergencyParts LLC (2d lead) scores highest on this
  - `price_score * 0.25` — TechComponents Asia scores highest on this

- `recommended_supplier` — highest composite_score winner
- `analysis.has_emergency_option: true` (EmergencyParts LLC lead ≤ 3d)
- `report.total_options_reviewed` — total supplier entries across both SKUs

**What to verify — @chain_with_results:**
- Terminal 3: `evaluate_suppliers` runs → `format_supplier_report` runs
- Both are sequential, results accumulated in `context["chain_results"]`

**What to verify — tool use (reasoning: true, allow_parallel_tool_calls: true):**
- Terminal 3 may show `math_eval called` (composite score calculation)
- Terminal 3 may show `timestamp called`
- Multiple tool calls may appear simultaneously

**What to verify — logistics chain:**
- logistics_agent receives supplier yields as upstream_yields
- procurement_plan references the recommended supplier

### Step 5.2 — Second Research Session (Memory Recall)

```bash
RESPONSE2=$(curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "find_suppliers",
    "data": {
      "inventory_items": [
        {"sku": "SKU-007", "units_needed": 40, "requested_quantity": 10, "stock_status": "critical"}
      ]
    }
  }')
SESSION_ID2=$(echo $RESPONSE2 | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session 2 ID: $SESSION_ID2"
```

**What to verify — memory recall:**
- `prior_evaluation_sessions` > 0
- Terminal 3 shows: `hybrid memory loaded`, relevance and recency scores logged
- LLM commentary references prior session findings

---

## 11. Flow 6 — Direct Logistics Planning

**Covers:** logistics_agent — @chain (validate_plan → score_priorities) + @compose (procurement_report + alerts)

### Step 6.1 — Plan for Critical SKUs Directly

```bash
RESPONSE=$(curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "plan_logistics",
    "data": {
      "message": "Plan emergency procurement for SKU-004 (Assembly D, 20 units, stockout) from SupplierCo Alpha at $120/unit (10 day lead time) and SKU-011 (Motor Controller K, 15 units, stockout) from EmergencyParts LLC at $275/unit (3 day lead time). Budget ceiling $8000."
    }
  }')
SESSION_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session ID: $SESSION_ID"
```

**What to verify — @chain execution (sequential):**
- Terminal 3: `validate_plan` completes → then `score_priorities` starts
- They do NOT overlap (sequential, not parallel)

**What to verify — @compose execution:**
- Terminal 3: `procurement_report helper injected`, `alerts helper injected`
- `context["procurement_report"]` and `context["alerts"]` available during LLM call

**What to verify — outputs:**
- `procurement_plan` — IMMEDIATE entries for SKU-004 and SKU-011 (both stockouts)
- `total_estimated_cost` — 20×$120 + 15×$275 = $2400 + $4125 = $6525 (within $8000 budget)
- `fulfillment_timeline_days` — 10 (longest of 10d and 3d)
- `risk_assessment` — "critical — 2 SKU(s) at zero/below safety stock..."
- `priority_score` — 2 critical items × 0.25 = 0.5 base, plus cost factor
- `next_steps` — first 3 procurement_plan entries

### Step 6.2 — Budget Overage Test

```bash
curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "plan_logistics",
    "data": {
      "message": "Plan procurement: 50 units SKU-011 (Motor Controller K) from EuroSource GmbH at $235/unit. Budget ceiling is $5000."
    }
  }' | python3 -m json.tool
```

**What to verify:**
- 50 × $235 = $11,750 exceeds $5,000 budget
- `risk_assessment` flags the overage
- `alerts` (from @compose) contains budget warning and `immediate_action_required: true`

---

## 12. Flow 7 — On-Demand Supply Report

**Covers:** supply_report_agent computing live KPIs from configs/*.yaml → chains to logistics_agent

### Step 7.1 — Trigger supply_report

```bash
RESPONSE=$(curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "supply_report",
    "data": {
      "trigger": "manual"
    }
  }')
SESSION_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session ID: $SESSION_ID"
```

**What to verify — supply_report_agent outputs (all computed from live data, no mocks):**

- `checks.inventory_health: "critical"` — because 6 SKUs are CRITICAL in inventory.yaml
- `checks.supplier_performance: "healthy"` — average OTD across 15 suppliers > 0.90
- `metrics.total_skus: 19`
- `metrics.critical_stock_skus: 6` (SKU-001, 002, 004, 007, 011, 015)
- `metrics.low_stock_skus: 5` (SKU-006, 009, 013, 017, 020)
- `metrics.stockout_skus: 2` (SKU-004 and SKU-011, both on_hand=0)
- `metrics.active_suppliers: 15`
- `metrics.on_time_delivery_rate` — calculated average across all 15 performance records
- `metrics.defect_rate` — calculated average
- `metrics.avg_lead_time_days` — calculated average
- `inventory_items` — list of 11 items needing action (6 critical + 5 low)
- `critical_items` — the 6 CRITICAL items
- `item_count: 11`
- `status: "report_generated"`

### Step 7.2 — Verify Chain to Logistics

**What to verify:**
- Terminal 3: `logistics_agent started` (condition `status == "report_generated"` satisfied)
- logistics_agent receives `inventory_items` (11 action items) in upstream_yields
- `procurement_plan` has IMMEDIATE lines for all 6 critical SKUs

```bash
curl -s $MESH/api/sessions/$SESSION_ID/history | python3 -m json.tool
```

**Verify:**
- supply_report_agent → logistics_agent chain in history
- logistics_agent `upstream_yields` has `supply_report_agent` key with the 11-item list

---

## 13. Flow 8 — HMAC Signature Security Tests

**Covers:** Inbound webhook authentication — all 4 variants must fail correctly

### Step 8.1 — Wrong Signature (Must Reject)

```bash
curl -s -X POST $MESH/webhook/process_order \
  -H "Content-Type: application/json" \
  -H "X-LeafMesh-Signature: sha256=0000000000000000000000000000000000000000000000000000000000000000" \
  -d '{"session_id": "fake-session", "decision": "approved", "message": "test"}'
```

Expected: HTTP 401 or 403. No agent triggered.

### Step 8.2 — Missing Signature Header (Must Reject)

```bash
curl -s -X POST $MESH/webhook/process_order \
  -H "Content-Type: application/json" \
  -d '{"session_id": "fake-session", "decision": "approved", "message": "test"}'
```

Expected: HTTP 401 or 400. No agent triggered.

### Step 8.3 — Malformed Signature (Must Reject)

```bash
curl -s -X POST $MESH/webhook/process_order \
  -H "Content-Type: application/json" \
  -H "X-LeafMesh-Signature: notasha256value" \
  -d '{"session_id": "fake-session", "decision": "approved", "message": "test"}'
```

Expected: HTTP 400 or 401.

### Step 8.4 — Valid Signature but Nonexistent Session (Must Be Silently Dropped)

```bash
BODY='{"session_id": "nonexistent-abc-999", "decision": "approved", "message": "replay"}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -s -X POST $MESH/webhook/process_order \
  -H "Content-Type: application/json" \
  -H "X-LeafMesh-Signature: sha256=$SIG" \
  -d "$BODY"
```

Expected: HTTP 200 (signature valid) but no agent execution — session doesn't exist in Redis.

**For all Step 8.x verify:**
- Terminal 3: no `inventory_agent`, `supplier_agent`, or `logistics_agent` starts
- Error or rejection logged for invalid signatures

---

## 14. Flow 9 — Critical Stock Conditional Branch

**Covers:** `@conditional_chain` routing — `handle_critical_stock` branch fires when any SKU is critical

### Step 9.1 — Target Known CRITICAL SKUs

SKU-015 (Shaft Seal O): on_hand=2, safety_stock=15 → CRITICAL

```bash
RESPONSE=$(curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "check_inventory",
    "data": {
      "items": [
        {"sku": "SKU-015", "quantity": 60},
        {"sku": "SKU-002", "quantity": 30}
      ]
    }
  }')
SESSION_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session ID: $SESSION_ID"
```

**What to verify:**
- SKU-015: on_hand=2 < safety_stock=15 → `stock_status: critical`
- SKU-002: on_hand=5 < safety_stock=10 → `stock_status: critical`
- `critical_items` — both SKUs present
- `alert` — "CRITICAL: 2 item(s)..."
- `reorder_needed: true`
- Terminal 3: `@conditional_chain → handle_critical_stock`
- Supplier agents start immediately

**Verify supplier options for SKU-015 from catalog:**
- SupplierCo Alpha ($4.50, 5d), NorthAmerica Parts ($4.30, 7d), FastSource Inc ($5.20, 2d), GlobalParts Ltd ($3.60, 14d)
- Fallback picks FastSource Inc (shortest lead=2d)
- Supplier_agent composite scoring: FastSource (lead_score high, price mid) vs SupplierCo Alpha (reliability high)

---

## 15. Flow 10 — Normal Stock (No Chain) Verification

**Covers:** `@conditional_chain` false path — `handle_normal_stock` fires, supplier agents NOT triggered

### Step 10.1 — Target Only NORMAL SKUs

```bash
RESPONSE=$(curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "check_inventory",
    "data": {
      "items": [
        {"sku": "SKU-003", "quantity": 50},
        {"sku": "SKU-005", "quantity": 20},
        {"sku": "SKU-018", "quantity": 30}
      ]
    }
  }')
SESSION_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session ID: $SESSION_ID"
```

**What to verify:**
- SKU-003: on_hand=200, reorder=75 → `stock_status: normal`
- SKU-005: on_hand=80, reorder=60 → `stock_status: normal`
- SKU-018: on_hand=120, reorder=80 → `stock_status: normal`
- `critical_items: []`
- `reorder_needed: false`
- `alert`: "All inventory levels within normal range"
- Terminal 3: `@conditional_chain → handle_normal_stock`
- **NO** `supplier_agent started` or `fallback_supplier_agent started` log entries
- **NO** `logistics_agent started`

```bash
curl -s $MESH/api/sessions/$SESSION_ID/history | python3 -m json.tool
```

**Verify:** history has only `inventory_agent` — no downstream agents called.

---

## 16. Flow 11 — Supplier Race Condition (OR Fan-In)

**Covers:** Verifying `fallback_supplier_agent` wins the race and `logistics_agent` starts immediately

### Step 11.1 — Trigger Full Fan-Out

Use all 6 CRITICAL SKUs to maximize catalog lookups (more entries = more interesting race):

```bash
RESPONSE=$(curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "check_inventory",
    "data": {
      "items": [
        {"sku": "SKU-001", "quantity": 50},
        {"sku": "SKU-002", "quantity": 30},
        {"sku": "SKU-004", "quantity": 20},
        {"sku": "SKU-007", "quantity": 40},
        {"sku": "SKU-011", "quantity": 15},
        {"sku": "SKU-015", "quantity": 60}
      ]
    }
  }')
SESSION_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session ID: $SESSION_ID"
```

**Watch Terminal 3 for this exact event sequence:**

```
1. inventory_agent started
2. inventory_agent completed — 6 critical items, fan-out triggered
3. supplier_agent started         (timestamp T1)
4. fallback_supplier_agent started (timestamp T1 — same moment)
5. fallback_supplier_agent completed (timestamp T1+δ — milliseconds, no LLM)
6. logistics_agent started         (timestamp T1+δ — OR condition satisfied by fallback)
7. supplier_agent completed        (timestamp T2 — LLM finishes later)
8. logistics_agent completed
```

**What to verify in session history:**

```bash
curl -s $MESH/api/sessions/$SESSION_ID/history | python3 -m json.tool
```

- `supplier_agent` and `fallback_supplier_agent` have overlapping `started_at` → `completed_at` timestamps
- logistics_agent `started_at` ≈ `fallback_supplier_agent.completed_at` (not supplier_agent's)
- logistics_agent yields show `supplier_source: "fallback_supplier_agent"`, `supplier_confidence: 0.65`
- Fallback selection strategy: `fastest_delivery` — for each CRITICAL SKU it picks the shortest-lead option:
  - SKU-001 → NorthAmerica Parts (5d) or SupplierCo Alpha (7d) — depending on sort
  - SKU-004 → EmergencyParts LLC (2d lead)
  - SKU-007 → EmergencyParts LLC (2d lead)
  - SKU-011 → EmergencyParts LLC (3d lead)

---

## 17. Flow 12 — Supplier Composite Scoring and Competition

**Covers:** Supplier scoring accuracy across the 15-supplier catalog — EuroSource GmbH should rank near top

### Step 12.1 — Research SKU-010 (3 Supplier Options)

SKU-010 (Stepper Motor J) has 3 suppliers with distinct tradeoffs:
- TechComponents Asia: $55, 21d, reliability 0.85 → low lead_score, mid price
- EuroSource GmbH: $68, 10d, reliability 0.98 → high reliability, short lead, high price
- PrecisionMfg Co: $64, 14d, reliability 0.96 → high reliability, mid lead, mid price

```bash
curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "find_suppliers",
    "data": {
      "inventory_items": [
        {"sku": "SKU-010", "units_needed": 30, "requested_quantity": 30, "stock_status": "normal"}
      ]
    }
  }' | python3 -m json.tool
```

**Expected composite scores (approximate):**
```
EuroSource GmbH:   0.40×0.98 + 0.35×(1-10/30) + 0.25×(1-68/500)
                 = 0.392    + 0.35×0.667       + 0.25×0.864
                 = 0.392    + 0.233            + 0.216  ≈ 0.841

PrecisionMfg Co:   0.40×0.96 + 0.35×(1-14/30) + 0.25×(1-64/500)
                 = 0.384    + 0.35×0.533       + 0.25×0.872
                 = 0.384    + 0.187            + 0.218  ≈ 0.789

TechComponents:    0.40×0.85 + 0.35×(1-21/30) + 0.25×(1-55/500)
                 = 0.340    + 0.35×0.300       + 0.25×0.890
                 = 0.340    + 0.105            + 0.222  ≈ 0.667
```

**What to verify:**
- `suppliers` list sorted by `composite_score` descending
- EuroSource GmbH is `recommended_supplier` (highest score)
- `analysis.has_emergency_option: false` (none have lead ≤ 3d for SKU-010)

### Step 12.2 — Research SKU-002 (4 Supplier Options Including Emergency)

SKU-002 has 4 options including QuickShip Express (1d lead):

```bash
curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "find_suppliers",
    "data": {
      "inventory_items": [
        {"sku": "SKU-002", "units_needed": 30, "requested_quantity": 30, "stock_status": "critical"}
      ]
    }
  }' | python3 -m json.tool
```

**What to verify:**
- 4 suppliers returned: FastSource Inc, BulkSupply Corp, SupplierCo Alpha, QuickShip Express
- `analysis.has_emergency_option: true` (QuickShip Express: lead=1d ≤ 3d)
- BulkSupply Corp has lowest `composite_score` (reliability=0.79, lead=21d)

---

## 18. Flow 13 — ABC Class Priority Ordering

**Covers:** Verifying that A-class critical SKUs produce higher `priority_score` in logistics_agent

### Step 13.1 — Check an A-Class Critical vs C-Class Low

```bash
# A-class critical: SKU-011 (Motor Controller K, on_hand=0, unit_cost=$220)
RESPONSE_A=$(curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "check_inventory",
    "data": {
      "items": [{"sku": "SKU-011", "quantity": 15}]
    }
  }')
SESSION_A=$(echo $RESPONSE_A | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "A-class session: $SESSION_A"
```

```bash
# C-class low: SKU-019 (Bolt Set, on_hand=800, well above reorder=500)
RESPONSE_C=$(curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "check_inventory",
    "data": {
      "items": [{"sku": "SKU-019", "quantity": 500}]
    }
  }')
SESSION_C=$(echo $RESPONSE_C | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "C-class session: $SESSION_C"
```

**Wait for both to complete, then compare:**

```bash
curl -s $MESH/api/sessions/$SESSION_A/history | python3 -c "
import sys, json
h = json.load(sys.stdin)
for a in h.get('agents', []):
    if a['agent_name'] == 'logistics_agent':
        print('A-class priority_score:', a['yields']['priority_score'])
"

curl -s $MESH/api/sessions/$SESSION_C/history | python3 -c "
import sys, json
h = json.load(sys.stdin)
for a in h.get('agents', []):
    if a['agent_name'] == 'logistics_agent':
        print('C-class priority_score:', a['yields']['priority_score'])
    elif a['agent_name'] == 'inventory_agent':
        print('C-class stock_status:', [i['stock_status'] for i in a['yields'].get('inventory_items', [])])
"
```

**What to verify:**
- A-class (SKU-011, stockout, critical): `priority_score` significantly higher (critical_items_count=1 → base 0.25)
- C-class (SKU-019, normal stock): `reorder_needed: false`, no downstream chain fires, `priority_score` near 0
- logistics_agent called for A-class, NOT called for C-class (conditional_chain false path)

---

## 19. Flow 14 — Cross-Session Supplier Memory

**Covers:** supplier_agent hybrid memory (relevance 0.6 + recency 0.4) across sessions

### Step 14.1 — Session A: Research with Specific Finding

```bash
curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "find_suppliers",
    "data": {
      "inventory_items": [
        {"sku": "SKU-007", "units_needed": 40, "stock_status": "critical"}
      ],
      "message": "Note: EuroSource GmbH had a shipment delay last month — customs clearance issue on SKU-007 sensors. Consider this when ranking."
    }
  }' | python3 -m json.tool
```

Wait for completion (watch Terminal 3).

### Step 14.2 — Session B: Same SKU, Reference Prior Knowledge

```bash
curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "find_suppliers",
    "data": {
      "inventory_items": [
        {"sku": "SKU-007", "units_needed": 40, "stock_status": "critical"}
      ],
      "message": "Find best suppliers for SKU-007 again. What do we know from our previous research sessions?"
    }
  }' | python3 -m json.tool
```

**What to verify:**
- `prior_evaluation_sessions` > 0
- Terminal 3: `hybrid memory loaded`, memory posts retrieved
- LLM commentary references the EuroSource customs delay from Session A
- Memory weights: relevance 0.6 (SKU match) + recency 0.4 → recent sessions scored higher

### Step 14.3 — Session C: Different SKU (Memory Must Not Bleed)

```bash
curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "find_suppliers",
    "data": {
      "inventory_items": [
        {"sku": "SKU-019", "units_needed": 500, "stock_status": "normal"}
      ]
    }
  }' | python3 -m json.tool
```

**What to verify:**
- SKU-019 (Bolt Set) has no prior memory sessions
- `prior_evaluation_sessions: 0` or low
- Memory from SKU-007 sessions not injected (relevance threshold filters it out)
- Suppliers for SKU-019: NorthAmerica Parts, BulkSupply Corp, GlobalParts Ltd — correct catalog returned

---

## 20. Flow 15 — SSE Streaming Response

**Covers:** LLM agent responses over `/api/mesh/stream`

### Step 15.1 — Stream Supplier Research

```bash
curl -X POST $MESH/api/mesh/stream \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "entry_point": "find_suppliers",
    "data": {
      "inventory_items": [
        {"sku": "SKU-006", "units_needed": 25, "stock_status": "low"},
        {"sku": "SKU-020", "units_needed": 15, "stock_status": "low"}
      ]
    }
  }'
```

**What to verify:**
- Response Content-Type: `text/event-stream`
- Stream starts within seconds
- Output is sequential `data: {...}` SSE events (partial LLM tokens)
- Stream closes cleanly — no hanging connection, no error events
- SKU-006 suppliers (TechComponents Asia, EuroSource GmbH, FastSource Inc, MegaDistribution) visible in streamed output

### Step 15.2 — Stream Logistics Planning

```bash
curl -X POST $MESH/api/mesh/stream \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "entry_point": "plan_logistics",
    "data": {
      "message": "Reorder plan for low-stock SKUs: SKU-006 (PCB Board F, 25 units), SKU-009 (Ball Bearing I, 20 units), SKU-013 (Drive Housing M, 10 units)"
    }
  }'
```

**What to verify:**
- Same SSE verification as Step 15.1
- `procurement_plan` events appear with REORDER entries (low-stock, not IMMEDIATE)
- `risk_assessment: elevated` (low-stock items, no critical)

---

## 21. Flow 16 — Scheduled Supply Report (Cron Trigger)

**Covers:** supply_report_agent wake_up cron schedule and APScheduler registration

### Step 16.1 — Verify Schedule Registration

```bash
curl -s $MESH/api/agents/supply_report_agent | python3 -m json.tool
```

**What to verify:**
- Agent config shows `wake_up: "0 8 * * *"`
- Schedule active and registered

### Step 16.2 — Verify APScheduler in Terminal 3

Check Terminal 3 startup output for:

```
APScheduler started
Job registered: supply_report_agent — cron: 0 8 * * *
```

### Step 16.3 — Force Cron Test (Optional)

Temporarily change `wake_up` in config.yaml to fire every minute:

```yaml
# In configs/config.yaml, temporarily change:
wake_up: "* * * * *"
# Restart main.py, wait ~60 seconds, revert
```

**What to verify:**
- Terminal 3: `supply_report_agent triggered by scheduler` (not by API call)
- KPIs are identical to on-demand trigger (same data source: configs/*.yaml)
- supply_report_agent → logistics_agent chain fires automatically

---

## 22. Flow 17 — Session History and Audit Trail

**Covers:** Session persistence, agent yield completeness, Redis TTL

### Step 22.1 — List Active Sessions

```bash
curl -s $MESH/api/sessions/ | python3 -m json.tool
```

**What to verify:**
- Sessions from all previous flows are listed
- Each has: session_id, entry_point, created_at, status
- Sessions older than 2 hours may have expired (TTL: 7200 seconds)

### Step 22.2 — Inspect Flow 1 Session in Full

Use the session_id saved from Flow 1:

```bash
curl -s $MESH/api/sessions/$SESSION_ID/history | python3 -m json.tool
```

**What to verify — complete agent chain:**
- All 5 agents present: order_intake_agent, operations_manager, inventory_agent, [supplier_agent + fallback_supplier_agent], logistics_agent
- Each agent entry has: `agent_name`, `started_at`, `completed_at`, `status`, `yields`, `upstream_yields`

**New yields to verify (added in this version):**
- `order_intake_agent.yields.items` — real list with SKU + quantity entries (not `[]`)
- `inventory_agent.yields.inventory_items` — entries include `category`, `abc_class`, `avg_daily_demand`, `requested_quantity`
- `logistics_agent.yields.recommended_supplier` — supplier name string
- `logistics_agent.yields.supplier_source` — `"supplier_agent"` or `"fallback_supplier_agent"`
- `logistics_agent.yields.supplier_confidence` — 0.90 (supplier) or 0.65 (fallback)
- `logistics_agent.yields.critical_items_count` — integer
- `supply_report_agent.yields.inventory_items` — list of items needing action
- `supply_report_agent.yields.item_count` — count
- `supply_report_agent.yields.critical_items` — subset

### Step 22.3 — Redis Yields Verification

```bash
docker exec -it $(docker ps -q -f name=redis) redis-cli
> KEYS *
> TTL <a session key>
```

**What to verify:**
- Yields stored with session_id prefix
- TTL around 7200 seconds from creation
- Data is readable JSON (not corrupted)

---

## 23. Flow 18 — HITL Timeout Scenario

**Covers:** operations_manager graceful timeout when human does not respond

> **Note:** Default timeout is 600 seconds (10 minutes). Change `human_timeout_seconds: 30` in config.yaml for a faster test.

### Step 23.1 — Trigger but Do Not Respond

```bash
curl -s -X POST $MESH/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "process_order",
    "data": {
      "message": "Timeout test order — do not approve this"
    }
  }' | python3 -m json.tool
```

Do NOT send anything to Terminal 2.

### Step 23.2 — Wait and Verify

Wait for `human_timeout_seconds`. Terminal 3 should show:

```
operations_manager timed out waiting for human response
```

**What to verify:**
- No `inventory_agent started` log
- Outbound webhook retry attempted once (max_retries: 1) before timeout
- Session history shows operations_manager with `status: timed_out` or `failed`

---

## 24. Per-Agent Verification Checklist

| Agent | Tested? | Yields Complete? | Next Step Fired? | Notes |
|---|---|---|---|---|
| order_intake_agent | ☐ | ☐ | ☐ operations_manager | Flow 1 — verify items is NOT [] |
| operations_manager | ☐ | ☐ | ☐ routes by from_agent | Flow 1 (approve), Flow 2 (reject), Flow 3 (reverse) |
| inventory_agent | ☐ | ☐ | ☐ parallel fan-out | Flow 4, Flow 9 (critical), Flow 10 (normal, no chain) |
| supplier_agent | ☐ | ☐ | ☐ logistics_agent | Flow 5, Flow 12 (scoring), Flow 14 (memory) |
| fallback_supplier_agent | ☐ | ☐ | ☐ logistics_agent (OR win) | Flow 11, Flow 1 Step 1.5 |
| logistics_agent | ☐ | ☐ | ☐ (terminal) | Flow 6, verified in all supply chain flows |
| supply_report_agent | ☐ | ☐ | ☐ logistics_agent | Flow 7, Flow 16 (cron) |

---

## 25. API Endpoints Reference

| Method | Endpoint | Purpose |
|---|---|---|
| POST | /api/mesh/request | Trigger an entry point — returns session_id |
| POST | /api/mesh/stream | SSE streaming for LLM agent responses |
| POST | /webhook/{entry_point} | Inbound webhook — HITL responses and new tasks |
| GET | /api/mesh/entry_points | List all registered entry points |
| GET | /api/agents/ | List all agents with metadata |
| GET | /api/agents/{name} | Single agent config and status |
| GET | /api/sessions/ | List all active sessions |
| GET | /api/sessions/{id}/history | Full agent chain with yields per session |
| GET | /api/webhook/secret | HMAC signing secret |
| GET | /health | Health check |
| GET | /docs | Interactive ReDoc API documentation |

---

## 26. Common Issues and Fixes

### Redis Not Running

**Symptom:** Agents fail silently. Downstream agents receive empty `upstream_yields`.

**Fix:**
```bash
docker compose up redis -d
docker ps | grep redis
```

---

### OpenAI API Key Missing or Invalid

**Symptom:** LLM agents (order_intake_agent, supplier_agent, logistics_agent) reach the 90-second mesh call timeout.

**Fix:** Add to `.env`:
```
OPENAI_API_KEY=sk-...
```
Then restart `python main.py`.

---

### fallback_supplier_agent Not Discovered

**Symptom:** `GET /api/agents/` does not include `fallback_supplier_agent`. inventory_agent fan-out errors on startup. OR fan-in never resolves.

**Cause:** `agency/fallback_supplier_agent.py` was missing — this is now created.

**Fix:** Verify the file exists:
```bash
ls agency/fallback_supplier_agent.py
```
Restart `python main.py`. Confirm agent appears in `/api/agents/`.

---

### items Always Empty in order_intake_agent Output

**Symptom:** inventory_agent always scans all 19 SKUs regardless of what was ordered. order_intake_agent `yields.items` is `[]`.

**Cause:** Was a hardcoded bug — now fixed. LLM is prompted via config.yaml to return structured JSON.

**Fix:** Verify config.yaml has `prompt:` block under `order_intake_agent`. Restart main.py to reload config.

---

### HMAC Signature Mismatch

**Symptom:** All inbound HITL webhooks return 401 or 403.

**Fix:** Re-fetch the secret each session — it may rotate on restart:
```bash
SECRET=$(curl -s $MESH/api/webhook/secret | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('secret', d))")
```

---

### supply_report_agent Shows Stale/Mocked KPIs

**Symptom:** Report metrics do not match the values in `configs/inventory.yaml`.

**Cause:** Old version used hardcoded mock dictionaries — now fixed. All KPIs are computed from live YAML data.

**Fix:** Verify `agency/supply_report_agent.py` imports from `data_loader` and has no `_MOCK_*` variables. Restart main.py.

---

### Fan-In Stuck (logistics_agent Never Fires)

**Symptom:** Both supplier_agent and fallback_supplier_agent fail — logistics_agent never starts. Session times out at 90s.

**Common causes:**
- `fallback_supplier_agent` not registered (see above)
- `configs/suppliers.yaml` missing or malformed (data_loader raises FileNotFoundError)
- Redis not storing yields between agents

**Fix:**
```bash
python3 -c "import yaml; yaml.safe_load(open('configs/suppliers.yaml'))"
python3 -c "import yaml; yaml.safe_load(open('configs/inventory.yaml'))"
```
Both should parse without errors.

---

### data_loader Cache Stale After File Edit

**Symptom:** You updated `configs/inventory.yaml` but agents still use old values.

**Cause:** `data_loader.load_inventory()` uses `@lru_cache` — cached on first load.

**Fix:** Restart `python main.py` to bust the cache. Or call `data_loader.reload_data()` programmatically.

---

### Session Yields Expired

**Symptom:** Downstream agents receive no `upstream_yields` despite upstream completing.

**Fix:** Session TTL is 7200 seconds (2 hours). Re-run the flow end-to-end if more than 2 hours have passed.

---

### HITL Stub Receiver Not Running

**Symptom:** `operations_manager` outbound webhook fails (connection refused on port 9999). It retries once (max_retries: 1) then times out.

**Fix:** Start `python hitl_stub_receiver.py` in Terminal 2 before triggering any `process_order` flow.

---

## 27. Complete Test Sign-Off Checklist

Mark each item when verified. All must pass before declaring the system production-ready.

### Infrastructure
- ☐ Redis running and healthy
- ☐ LeafMesh starts without errors
- ☐ All 7 agents discovered on startup (confirm `fallback_supplier_agent` in list)
- ☐ All 6 entry points registered
- ☐ HITL stub receiver listening on port 9999
- ☐ API docs accessible at /docs
- ☐ configs/inventory.yaml loads: 19 SKUs
- ☐ configs/suppliers.yaml loads: 19 SKU entries + 15 performance records

### Entry Points
- ☐ process_order — triggers full chain
- ☐ manager_contact — triggers reverse HITL path
- ☐ check_inventory — triggers inventory + downstream chain
- ☐ find_suppliers — triggers supplier research + logistics
- ☐ plan_logistics — triggers logistics planning
- ☐ supply_report — triggers health report + logistics analysis

### Agent Behaviors
- ☐ order_intake_agent — LLM returns JSON; items extracted as real list (not `[]`)
- ☐ operations_manager — HITL routes correctly by from_agent condition
- ☐ inventory_agent — loads from inventory.yaml; stock status correct for all 3 categories
- ☐ supplier_agent — loads from suppliers.yaml; composite scores calculated correctly
- ☐ fallback_supplier_agent — created and discovered; selects fastest-delivery per SKU
- ☐ logistics_agent — fan-in, @chain, @compose; new yields present (recommended_supplier, supplier_source, supplier_confidence)
- ☐ supply_report_agent — live KPIs from configs/*.yaml; no hardcoded mock values

### Data Correctness
- ☐ CRITICAL SKUs confirmed (SKU-001, 002, 004, 007, 011, 015)
- ☐ LOW SKUs confirmed (SKU-006, 009, 013, 017, 020)
- ☐ NORMAL SKUs do not trigger supplier chain
- ☐ supply_report metrics.critical_stock_skus == 6
- ☐ supply_report metrics.total_skus == 19
- ☐ supply_report metrics.active_suppliers == 15
- ☐ Composite score ranking correct for SKU-010 (EuroSource > PrecisionMfg > TechComponents)
- ☐ Fallback picks EmergencyParts LLC for stockout SKUs (SKU-004, SKU-011) — shortest lead

### Advanced Patterns
- ☐ @pre_compose — processors run before LLM; JSON prompt instructs structured output
- ☐ @conditional_chain — critical path (Flow 9) and normal path (Flow 10) both verified
- ☐ @chain_with_results — evaluate_suppliers → format_supplier_report sequential
- ☐ @chain — validate_plan → score_priorities sequential in logistics_agent
- ☐ @compose — procurement_report + alerts helpers injected into logistics_agent context
- ☐ Parallel fan-out — supplier + fallback timestamps overlap
- ☐ OR fan-in — fallback wins race; logistics starts before supplier_agent completes
- ☐ AND+OR fan-in — logistics waits for inventory AND (supplier OR fallback)

### HITL
- ☐ System-initiated HITL (process_order → HITL webhook → inventory chain)
- ☐ Human-initiated HITL (manager_contact → reverse path → inventory chain)
- ☐ HITL approval — downstream chain fires; items correctly passed to inventory_agent
- ☐ HITL rejection — inventory_agent short-circuits; no downstream agents
- ☐ HITL timeout — graceful failure; no downstream agents
- ☐ HMAC wrong signature — rejected (401/403)
- ☐ HMAC missing header — rejected (401/400)
- ☐ HMAC correct — accepted (200)

### Memory and Sessions
- ☐ Cross-session memory — supplier_agent recalls prior evaluation sessions
- ☐ Memory relevance threshold — SKU-007 memory does not bleed into SKU-019 research
- ☐ Session history — complete audit trail with all new yields declared
- ☐ Redis persistence — yields stored and TTL correct
- ☐ data_loader cache — both YAML files loaded once and cached

### Streaming
- ☐ SSE streaming — /api/mesh/stream returns event stream
- ☐ Stream completes cleanly for both supplier_agent and logistics_agent

### Scheduled Agents
- ☐ supply_report_agent cron schedule registered (0 8 * * *)
- ☐ On-demand trigger produces identical KPIs to scheduled run
- ☐ Scheduled trigger fires automatically (cron test with * * * * *)

### Security
- ☐ HMAC validation enforced on all inbound webhooks
- ☐ Invalid signatures rejected without triggering any agent
- ☐ Nonexistent session IDs handled gracefully (200 response, no agent execution)
