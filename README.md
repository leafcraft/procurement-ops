# procurement-ops — Procure-to-Pay & Supply Chain Operations

A LeafMesh agency that runs a procurement department end to end: it parses an
incoming order, screens it for compliance, checks inventory and forecasts
demand, sources and scores suppliers, plans the procurement, takes two human
sign-offs (operations + finance), issues real PDF purchase orders, receives the
goods against the PO, and produces a daily supply-chain health report. Optional
international-trade roles (sanctions screening, HTS/duty classification) plug in
for cross-border buys.

---

## The Story

It's 7:42 a.m. on the last day of the quarter. **Priya, a plant operations
manager**, fires off one line to the procurement channel: *"Production line at
risk — need 300 units of SKU-001 and 120 of SKU-007 ASAP."*

The message lands on the `process_order` entry point and wakes `order_intake_agent`.
It reads the text, calls its `validate_skus`, `estimate_order_value` and
`get_sku_details` tools, and returns a structured order: two line items,
urgency **critical** (the "ASAP" + "line at risk"), and — because it's quarter-end
and the total clears the $50,000 budget gate — the risk flags
`budget_threshold_exceeded` and `quarter_end_demand_spike`. Rule-based compliance
screening flags it `requires_approval`. The intake yields are stashed (in-process
+ Redis) so nothing is lost across the human wait, and the order is handed to
**`operations_manager`** — a human gate.

Priya's colleague **Marcus, the operations manager**, gets a webhook
notification, sees the critical order and the flags, and replies *approved*.
The mesh resumes: `inventory_agent` recovers the items from the stash, reads the
inventory master, sees SKU-001 sitting at 15 on-hand against a reorder point of
50 (status **critical**), nets off in-transit stock, and computes optimised
order quantities with a quarter-end uplift. `supplier_agent` scores every
supplier for each SKU (reliability 40% / lead time 35% / price 25%), honours
Priya's region preference where set and picks the fastest source for the
critical SKU, and hands a ranked plan to `logistics_agent`. The strategist
builds a priority-ordered procurement plan, scores its risk, and shapes an
ERP-ready report plus an alert payload — then routes to **`po_approval_agent`**,
the finance gate, because the spend is over $5,000.

Finance approves. `purchase_order_agent` groups the plan by supplier, generates
a PO number per supplier, renders a branded **PDF purchase order** for each,
registers them as open POs for in-transit tracking, and returns the PO numbers
and totals. Days later the pallets arrive: a warehouse clerk hits `receive_goods`,
`grn_agent` matches the delivery against the open PO, updates on-hand inventory,
closes (or partially receives) the PO, and — because two cartons came in
damaged — escalates the quality incident back to `operations_manager`. Every
morning at 08:00 UTC, `supply_report_agent` wakes on its own, computes live KPIs,
and (when stock needs action) runs the same supplier → logistics → finance → PO
chain unattended, auto-approving because it's a scheduled run.

## Why It Works in an Organization

Procure-to-pay is a relay of judgment calls strung between systems — and most of
the delay is the relay, not the judgment. This agency collapses the hand-offs:
intake, compliance, inventory, forecasting, sourcing, planning and PO issuance
run as one chain, with humans inserted only at the two points that are
genuinely irreversible — **committing to spend** (operations) and **issuing a
binding PO** (finance). Both gates are default-deny-aware (a scheduled run
auto-approves; a manual order that times out routes to the configured fallback),
so nothing stalls silently and nothing irreversible happens without a person.

The ROI is concrete: emergency orders that used to wait for someone to manually
price, source and route them now land in front of the right approver within
seconds, fully costed; the daily cron catches stock-outs before they bite
instead of after; suppliers are scored on live performance, not habit; and every
PO comes out as a real, auditable PDF with an approval reference. The
international-trade roles (sanctions/denied-party screening and HTS duty
classification) add the controls that keep cross-border buys out of legal
trouble. State lives in the customer's systems (ERP/WMS/SRM in production; seed
YAML for the demo), not in the mesh.

---

## Configuration

Three tiers, grounded in `env` and `configs/config.yaml`.

### Tier 1 — Boot (required to start the mesh)

| Variable | Required | Purpose |
|---|---|---|
| `LEAFMESH_LICENSE_KEY` | **required** | SDK license; also enables observability when set |
| `OPENAI_API_KEY` | **required** | LLM provider for `order_intake_agent` (gpt-4o), `logistics_agent`, `trade_compliance_agent`, `tariff_classifier_agent` (gpt-4o-mini) and the manager |
| `LEAFMESH_ENV_TOKEN` | optional | Groups telemetry by environment |
| `ANTHROPIC_API_KEY` | optional | Only if you switch any agent's `model:` to a Claude model — none do by default |

### Tier 2 — Host prerequisites & adapters

| Variable | Default | Purpose |
|---|---|---|
| `REDIS_HOST` | `localhost` | Session store + upstream-yield persistence. Wired into `redis:` as `${REDIS_HOST:localhost}` |
| `REDIS_PORT` | `6379` | Wired as `${REDIS_PORT:6379}` |
| `REDIS_PASSWORD` | _(empty)_ | Wired as `${REDIS_PASSWORD:}` |
| `HIGH_VALUE_THRESHOLD_USD` | `50000` | Budget gate — read by `order_intake_agent.py`'s `enforce_approval_floor` `@chain` and `tools.py`'s `estimate_order_value` |
| `HIGH_QUANTITY_FLOOR` | `500` | Total-units approval floor — read by `order_intake_agent.py`'s `enforce_approval_floor` `@chain` |

Redis is started for you by `docker compose up redis -d`. The two human gates
notify via webhook — point them at a real receiver (Tier 3) or use the bundled
stub for local testing.

### Tier 3 — Connectors (customer systems)

| Variable / file | Required | Purpose |
|---|---|---|
| `HITL_OUTBOUND_URL` | optional | Where the SDK POSTs human-approval notifications. Wired into both human agents' `webhook_config.outbound_url` as `${HITL_OUTBOUND_URL:http://127.0.0.1:9999/human-notify}`; defaults to the local stub |
| `configs/inventory.yaml` | demo seed | Inventory master — **replace with an ERP/WMS query** in production (`data_loader.load_inventory`) |
| `configs/suppliers.yaml` | demo seed | Supplier catalog + live performance — **replace with an SRM API** (Coupa, SAP Ariba) (`data_loader.load_suppliers`) |
| `configs/open_pos.yaml` | runtime state | Open-PO registry for in-transit tracking and goods receipt |

> **Note:** there is no `channels:` block and no Slack credentials in this
> template. The escalation target list (`manager.escalation.targets`) is empty,
> so escalations land in the shared operator inbox.

---

## Agent Table

| Agent | Role (job title) | Responsibilities | Can perform |
|---|---|---|---|
| `operations_manager` | Operations Manager (human gate) | Approves/rejects incoming orders; reviews goods-receipt quality incidents and compliance/tariff escalations | `human` · webhook HITL · default-deny timeout fallback · routes approved/timeout orders to `inventory_agent` |
| `order_intake_agent` | Order Intake Specialist | Parses the order message into structured line items; assesses urgency; runs rule-based compliance screening; flags budget/quarter-end risk | `llm` (gpt-4o) · `@pre_compose` context/clean/extras · `@chain(enforce_approval_floor)` deterministic high-value/high-quantity gate the LLM can't waive · tools `validate_skus`, `estimate_order_value`, `get_sku_details` · stashes yields to Redis + session · routes to `operations_manager` |
| `inventory_agent` | Inventory & Demand Planner | Reads inventory master, classifies stock status, nets off in-transit, computes optimised reorder quantities with quarter-end/critical uplift; halts on rejection | `programmatic` · `@pre_compose(load_supply_snapshot)` self-pulls inventory levels + open-PO in-transit before running · recovers items from stash/Redis post-HITL · routes to `supplier_agent` when `item_count > 0` |
| `supplier_agent` | Supplier Sourcing Analyst | Scores and ranks suppliers per SKU on reliability/lead-time/price; honours region preference; picks fastest source for critical SKUs; uses live performance data | `programmatic` · cross-session memory of supplier trends · routes to `logistics_agent` |
| `logistics_agent` | Logistics & Procurement Strategist | Builds a priority-ordered procurement plan (cost/risk/lead-time trade-offs); scores priority; shapes ERP report + alert payloads | `llm` (gpt-4o-mini) · `@chain(validate_plan, score_priorities)` · `@compose(procurement_report, alerts)` · `wait_for: supplier_agent` · routes to `po_approval_agent` |
| `po_approval_agent` | Finance Approver (human gate) | Signs off procurement spend before binding POs are issued (orders over $5,000); auto-approves scheduled runs | `human` · webhook HITL · default-deny timeout fallback · routes approved/timeout to `purchase_order_agent` |
| `purchase_order_agent` | Purchase Order Clerk | Groups the approved plan by supplier; issues PO numbers; renders branded PDF purchase orders; registers open POs; handles non-USD currency notes | `programmatic` · `@chain(register_valid_pos)` PO-validity floor (rejects POs missing an approval ref or with invalid lines; only valid POs are registered) · recovers plan from stash/Redis post-HITL · ReportLab PDF rendering · writes `open_pos.yaml` |
| `grn_agent` | Goods-Receipt Clerk | Matches deliveries to open POs; updates on-hand inventory; closes/partially receives POs; flags short shipments and quality incidents | `programmatic` (entry point `receive_goods`) · escalates to `operations_manager` when `requires_ops_review` |
| `supply_report_agent` | Supply Chain Analyst (scheduled) | Computes daily inventory + supplier KPIs from live data; triggers an unattended procurement run when stock needs action | `programmatic` · `wake_up: 0 8 * * *` cron · routes to `supplier_agent` when `report_generated` |
| `trade_compliance_agent` | Trade Compliance Officer | Screens suppliers against OFAC/BIS/ITAR/EU/UK/UN sanctions + conflict-minerals + modern-slavery risk; fails closed on unusable output; hash-chained audit | `llm` (gpt-4o-mini, temp 0.0) · `enforce_yields` · `@chain(enforce_compliance_floor)` deterministic sanctions/high-risk hard-stop the LLM can't waive · SQLite audit ledger (dev stub) · routes hard-stop / review to `operations_manager` |
| `tariff_classifier_agent` | Customs Classification Specialist | Assigns HTS codes + country of origin to line items; resolves duty rate (FTA / column-2 / Section 232); computes landed cost; fails closed on low confidence | `llm` (gpt-4o-mini, temp 0.0) · `enforce_yields` · `@chain(enforce_customs_floor)` deterministic broker-review hard-stop (low confidence / Section 232) · SQLite ledger (dev stub) · routes broker-review to `operations_manager` |

---

## Quick Start

```bash
# 1. Install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure environment
cp env .env   # then edit: OPENAI_API_KEY, LEAFMESH_LICENSE_KEY (Redis defaults are fine locally)

# 3. Start Redis
docker compose up redis -d        # or: redis-server

# 4. Start the mesh
python main.py
```

The mesh starts an API server at **http://127.0.0.1:18820** — visit `/docs` for
interactive API docs.

## Entry Points

| Entry point | Target | Purpose |
|---|---|---|
| `process_order` | `order_intake_agent` | Main procure-to-pay flow from a raw order message |
| `manager_contact` | `operations_manager` | Human-initiated contact with the mesh |
| `check_inventory` | `inventory_agent` | Direct inventory + forecast check |
| `find_suppliers` | `supplier_agent` | Ad-hoc supplier evaluation |
| `plan_logistics` | `logistics_agent` | Direct procurement planning |
| `supply_report` | `supply_report_agent` | On-demand health report (also daily cron) |
| `receive_goods` | `grn_agent` | Goods receipt against an open PO |
| `trade_compliance_screen` | `trade_compliance_agent` | Sanctions / denied-party screening |
| `tariff_classify` | `tariff_classifier_agent` | HTS + duty / landed-cost estimation |

Example:

```bash
curl -X POST http://127.0.0.1:18820/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{"entry_point": "process_order",
       "data": {"message": "Production line at risk — need 300 of SKU-001 ASAP"}}'
```

## Project Structure

```
procurement-ops/
  configs/
    config.yaml            # Agents, mesh wiring, HITL gates, cron
    inventory.yaml         # Seed inventory master (replace with ERP/WMS)
    suppliers.yaml         # Seed supplier catalog + performance (replace with SRM)
    open_pos.yaml          # Open-PO registry (runtime state)
  agency/
    order_intake_agent.py  # LLM intake + rule-based compliance
    inventory_agent.py     # Inventory check + demand forecast
    supplier_agent.py      # Supplier scoring & selection
    logistics_agent.py     # Procurement plan (@chain + @compose)
    purchase_order_agent.py# PO generation + PDF rendering
    grn_agent.py           # Goods receipt
    supply_report_agent.py # Daily KPI report (cron)
    trade_compliance_agent.py  # Sanctions screening
    tariff_classifier_agent.py # HTS / duty classification
    tools.py               # @global_tool budget/SKU helpers
    data_loader.py         # Cached YAML loaders (swap for live APIs)
    _shared/session_stash.py   # HITL-bridge per-session stash
  main.py
  validate_config.py
  tests/
  requirements.txt
  env
```

## Verify

```bash
python validate_config.py           # structure + yields↔prompt + module reconciliation
python -m pytest tests/ -q          # structural tests
```

## Observability

Observability auto-enables when `LEAFMESH_LICENSE_KEY` is set. Set
`LEAFMESH_ENV_TOKEN` to group telemetry by environment.

## Links

- [LeafMesh SDK](https://pypi.org/project/leafmesh/)
- [LeafCraft Studios](https://leafcraft.ai)
