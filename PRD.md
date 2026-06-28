# Procurement Ops — Product Requirements / Build Doc

> An end-to-end procure-to-pay department built as a LeafMesh agency. A raw
> order message is groomed into a structured, compliance-screened order by an
> **order intake specialist**, signed off by an **operations manager** (human),
> then flowed through **inventory + demand planning**, **supplier sourcing**,
> and **logistics planning** before a **finance approver** (human) authorises
> spend and a **purchase order clerk** issues real PDF POs. A **goods-receipt
> clerk** closes the loop when stock arrives, and a **supply chain analyst**
> runs the same chain unattended every morning on a cron. Optional
> **trade-compliance** and **customs-classification** roles screen
> cross-border buys. State lives in the customer's systems (ERP/WMS/SRM in
> production; seed YAML for the demo); the two human gates are the only points
> where work pauses for a person.

---

## 1. The operation at a glance

```
 process_order  (raw order message)                 supply_report  cron 0 8 * * *
        │                                                   │
   ┌────▼───────────────────────────────────┐         ┌─────▼─────────────────────┐
   │ order_intake_agent  (LLM)               │         │ supply_report_agent       │
   │  • @pre_compose: context/clean/extras   │         │  • live inventory+supplier│
   │  • @chain: enforce_approval_floor       │         │    KPIs                    │
   │  • tools: validate_skus,                │         │  • is_scheduled_run = true │
   │    estimate_order_value, get_sku_details│         └─────┬─────────────────────┘
   │  • rule-based compliance screen         │               │ status==report_generated
   │  • stash yields → Redis + session       │               │
   └────┬────────────────────────────────────┘              │
        │ (all orders)                                       │
   ┌────▼────────────────────────────┐                       │
   │ operations_manager  (HUMAN gate) │                       │
   │  approved/timeout → inventory    │                       │
   │  rejected → halt                 │                       │
   └────┬─────────────────────────────┘                      │
        │ approved / timeout_default                          │
   ┌────▼─────────────────────────────┐                       │
   │ inventory_agent  (programmatic)  │                       │
   │  • recover items from stash/Redis│                       │
   │  • stock status + demand forecast│                       │
   └────┬─────────────────────────────┘                      │
        │ item_count > 0                                      │
   ┌────▼─────────────────────────────┐  ◄────────────────────┘
   │ supplier_agent  (programmatic)   │   (cron path enters here)
   │  • score/rank suppliers per SKU  │
   └────┬─────────────────────────────┘
        │
   ┌────▼─────────────────────────────┐
   │ logistics_agent  (LLM)           │
   │  • @chain: validate, score        │
   │  • @compose: report, alerts       │
   │  • wait_for: supplier_agent       │
   └────┬─────────────────────────────┘
        │
   ┌────▼─────────────────────────────┐
   │ po_approval_agent  (HUMAN gate)  │
   │  approved/timeout → PO clerk     │
   │  rejected → halt · scheduled →   │
   │  auto-approve                    │
   └────┬─────────────────────────────┘
        │ approved / timeout_default
   ┌────▼─────────────────────────────┐
   │ purchase_order_agent (programmatic)│
   │  • group by supplier, issue PO #  │
   │  • render PDF                      │
   │  • @chain: register_valid_pos     │
   │    (PO-validity floor, fail-closed)│
   └───────────────────────────────────┘

 receive_goods ─► grn_agent (programmatic) ──requires_ops_review──► operations_manager
 trade_compliance_screen ─► trade_compliance_agent (LLM, @chain enforce_compliance_floor) ──hard_stop/review──► operations_manager
 tariff_classify ─► tariff_classifier_agent (LLM, @chain enforce_customs_floor) ──broker_review──► operations_manager
```

## 2. Actor inventory (job titles)

| Actor | Type | Job title | Mesh role |
|---|---|---|---|
| `operations_manager` | human | Operations Manager | Order-approval gate; receives goods-receipt & trade escalations |
| `order_intake_agent` | llm | Order Intake Specialist | Structures + compliance-screens the order |
| `inventory_agent` | programmatic | Inventory & Demand Planner | Stock status + reorder forecasting |
| `supplier_agent` | programmatic | Supplier Sourcing Analyst | Supplier scoring & selection |
| `logistics_agent` | llm | Logistics & Procurement Strategist | Builds the procurement plan |
| `po_approval_agent` | human | Finance Approver | Spend sign-off before binding POs |
| `purchase_order_agent` | programmatic | Purchase Order Clerk | Issues PDF POs, tracks open POs |
| `grn_agent` | programmatic | Goods-Receipt Clerk | Receives goods, updates inventory |
| `supply_report_agent` | programmatic | Supply Chain Analyst | Daily KPI report + unattended run |
| `trade_compliance_agent` | llm | Trade Compliance Officer | Sanctions / denied-party screening |
| `tariff_classifier_agent` | llm | Customs Classification Specialist | HTS code + duty / landed cost |

## 3. Data artifacts

| Artifact | Where | Lifecycle |
|---|---|---|
| Structured order | `order_intake_agent` yields | Stashed (in-process + Redis) to survive the HITL wait |
| Inventory master | `configs/inventory.yaml` (`data_loader.load_inventory`) | Read each run; on-hand updated by `grn_agent` |
| Supplier catalog + performance | `configs/suppliers.yaml` (`data_loader.load_suppliers`) | Read by `supplier_agent`, `supply_report_agent` |
| Procurement plan | `logistics_agent` yields | Stashed for recovery after the finance gate |
| Open-PO registry | `configs/open_pos.yaml` | Written by `purchase_order_agent`, consumed by `grn_agent` + in-transit netting |
| Purchase-order PDFs | `purchase_orders/PO-*.pdf` | Rendered by `purchase_order_agent` (ReportLab) |
| Compliance / tariff ledgers | `logs/trade_compliance.db`, `logs/tariff_classifier.db` | SQLite dev stubs — swap for the customer's audit store |

## 4. Per-agent spec

- **order_intake_agent** (llm, gpt-4o, temp 0.3) — `@pre_compose(process_context,
  clean_input, load_extras)` + `@chain(enforce_approval_floor)`. Tools
  `validate_skus`, `estimate_order_value`, `get_sku_details`. Parses LLM JSON (with
  a regex text-extraction fallback), normalises items/urgency/order-type, applies
  quarter-end and high-quantity risk rules, runs rule-based compliance screening,
  and persists yields to the session stash + Redis before the HITL boundary. The
  `@chain` floor **re-computes the order value from the supplier catalog** and
  FORCES `requires_approval=True` / `compliance_status=flagged` when it clears the
  `HIGH_VALUE_THRESHOLD_USD` budget gate or the `HIGH_QUANTITY_FLOOR` — the LLM
  cannot clear a high-value order by omitting the flag. Routes all orders to
  `operations_manager`. OUTPUT CONTRACT lists all 13 yield keys.
- **operations_manager** (human) — webhook HITL, 600s timeout, default-deny
  fallback (`timeout_default` → auto-approve only for unattended continuity).
  Routes approved/timeout to `inventory_agent`. Conditions use only human-meta
  fields (`human_decision`, `human_message`).
- **inventory_agent** (programmatic) — `@pre_compose(load_supply_snapshot)`
  self-pulls the inventory master + open-PO in-transit quantities before the agent
  runs (the Inventory & Demand Planner reads its own stock levels; it is not
  spoon-fed). Recovers items from upstream / request_data / session stash / Redis
  (in priority order) after the human gate, classifies stock status, nets off
  in-transit quantities, computes optimised reorder quantities (safety buffer +
  quarter-end + critical uplift, capped at warehouse capacity). Rejection gate
  halts on a rejected `human_message`. Routes to `supplier_agent` when
  `item_count > 0`.
- **supplier_agent** (programmatic) — composite scoring (reliability 40% / lead
  time 35% / price 25%), live-performance override, region-preference and
  critical-urgency selection rules. Cross-session memory of supplier trends.
  Routes to `logistics_agent`.
- **logistics_agent** (llm, gpt-4o-mini, temp 0.3) — `@chain(validate_plan,
  score_priorities)`, `@compose(procurement_report, alerts)`, `wait_for:
  supplier_agent`. Builds the priority-ordered plan (Path A: direct LLM JSON;
  Path B: from upstream inventory+supplier data), recalculates cost from the
  structured plan, stashes yields for post-HITL recovery. Routes to
  `po_approval_agent`. OUTPUT CONTRACT lists all 16 yield keys.
- **po_approval_agent** (human) — webhook HITL, 600s timeout, `fallback_on_timeout`
  with default-deny-aware auto-approve for scheduled runs. Routes approved/timeout
  to `purchase_order_agent`.
- **purchase_order_agent** (programmatic) — `@chain(register_valid_pos)`. Recovers
  the plan from upstream / context / stash / Redis, groups lines by supplier, issues
  PO numbers, renders a branded PDF per PO, handles non-USD currency notes. The
  `@chain` PO-validity floor then **deterministically rejects any PO** lacking an
  approval reference or with invalid lines (no supplier / qty ≤ 0 / price ≤ 0):
  it forces `status=po_validation_failed`, populates `validation_errors`, and
  registers ONLY validated POs in the open-PO ledger — a binding PO is never
  issued on bad data. Rejection gate halts on a rejected `human_message`.
- **grn_agent** (programmatic, entry `receive_goods`) — matches received items to
  open-PO lines, updates on-hand inventory, closes/partially receives the PO, flags
  short shipments and quality incidents, busts the data-loader cache. Escalates to
  `operations_manager` when `requires_ops_review`.
- **supply_report_agent** (programmatic, `wake_up: 0 8 * * *`, entry `supply_report`)
  — computes live inventory + supplier KPIs, builds action items, sets
  `is_scheduled_run`. Routes to `supplier_agent` when `status == report_generated`
  for an unattended procurement run.
- **trade_compliance_agent** (llm, gpt-4o-mini, temp 0.0, `enforce_yields`) —
  `@chain(enforce_compliance_floor)`. LLM proposes sanctions matches; deterministic
  disposition (blocked / conditional / review / cleared); fails closed on unusable
  LLM output or missing supplier_id; hash-chained SQLite audit. The `@chain` floor
  **re-asserts the hard-stop from the structural facts** — any sanctions hit or
  high-risk country FORCES `requires_hard_stop=True` and `overall_status=blocked`;
  an un-run screening (no `screening_id` / pending) FORCES compliance review — so
  the disposition can never be cleared by a downstream edit. Routes hard-stop and
  compliance-review to `operations_manager` (mutually exclusive conditions).
  OUTPUT CONTRACT lists all 13 yield keys.
- **tariff_classifier_agent** (llm, gpt-4o-mini, temp 0.0, `enforce_yields`) —
  `@chain(enforce_customs_floor)`. LLM proposes HTS codes; deterministic duty
  resolution (FTA / column-2 / Section 232 surcharge) + landed-cost computation;
  fails closed when items are submitted but none classify, or confidence < 0.70.
  The `@chain` floor **re-asserts broker review** — any per-line confidence < 0.70
  or any Section-232 surcharge FORCES `requires_customs_broker_review=True`. Routes
  broker-review to `operations_manager`. OUTPUT CONTRACT lists all 10 yield keys.

## 5. Entry points

`process_order` (main), `manager_contact`, `check_inventory`, `find_suppliers`,
`plan_logistics`, `supply_report`, `receive_goods`, `trade_compliance_screen`,
`tariff_classify`. See README for the full table.

## 6. Controls & trust

- **Two human gates** at the irreversible points — spend commitment
  (`operations_manager`) and binding PO issuance (`po_approval_agent`). Both have
  600s timeouts and default-deny-aware fallbacks; conditions reference only
  human-meta fields.
- **Deterministic `@chain` floors the LLM can't waive** — five hard floors run
  *after* the agent and overwrite/OR onto whatever the model decided:
  high-value/high-quantity approval (`order_intake_agent`, value recomputed from the
  catalog), PO validity (`purchase_order_agent`, fail-closed on bad lines), sanctions
  /high-risk hard-stop (`trade_compliance_agent`), customs-broker review on low
  confidence / Section 232 (`tariff_classifier_agent`), plus the completeness/scoring
  floor on `logistics_agent`. None can be cleared by prompt wording.
- **Fail-closed compliance** — `trade_compliance_agent` and `tariff_classifier_agent`
  route to human review on unusable/low-confidence model output rather than silently
  clearing.
- **Rejection gates** in `inventory_agent` and `purchase_order_agent` halt the chain
  on a rejected human message.
- **HITL-bridge persistence** — `_shared/session_stash` + Redis carry the order /
  plan across the human wait (the SDK delivers only human-meta fields on resume).
- **In-transit netting** — open POs reduce reorder quantities so the mesh never
  double-orders what's already on the way.
- **Hash-chained audit** on every compliance screening; per-PO approval reference.

## 7. Config pointer

All wiring is in `configs/config.yaml`. Env knobs in `env` (3 tiers documented in
the README): `LEAFMESH_LICENSE_KEY` + `OPENAI_API_KEY` to boot; `REDIS_*` and
`HIGH_VALUE_THRESHOLD_USD` for host/domain; `HITL_OUTBOUND_URL` and the seed YAML
files for connectors.

## 8. Reflection / decisions

- **Why merge intake + compliance** — a single LLM pass extracts the order;
  compliance is deterministic rules on the result, so a second LLM call added cost
  without judgment.
- **Why supplier_agent is programmatic, not LLM** — all selection logic is
  deterministic scoring; the LLM only produced prose nothing consumed.
- **Why seed YAML, not SQLite, for inventory/suppliers** — system-of-record state
  belongs in the customer's ERP/WMS/SRM; the YAML loaders are demo stubs with a
  single swap point (`data_loader`). The two `logs/*.db` ledgers are likewise dev
  stubs for the compliance audit trail.
- **Why the cron path auto-approves** — an unattended daily run must not block on a
  human; `is_scheduled_run` flows through and the finance gate auto-approves while
  preserving the gate for manual orders.
- **Open item for the live smoke test** — the post-HITL recovery (stash → Redis →
  context) and the `@chain`/`@compose` interplay on `logistics_agent` are
  designed conservatively (multiple fallbacks) and should be confirmed on a running
  mesh with Redis, since they cannot be proven from config load alone.
