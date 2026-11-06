# procurement-ops — Procurement-Ops Pod · PRD

The brief behind the team's configuration. This pod runs a purchase requisition from
filing all the way to an **issued, registered PO**, as a **team** of self-reliant
agents — a coordinator, specialists, a finisher, first-class human gates, and a
WORM sink — supervised by the LeafMesh Manager. Not a pipeline.

Design doctrine: `.claude/skills/leafmesh/building-agents-thoroughly.md`.
SDK baseline: **2.4.131**.

---

## 1. The pod shape

```
              ┌───────────────────────── Manager (supervisor) ──────────────────────────┐
              │ escalates errors (non-retryable / 5 failed agents) to the Procurement Lead │
              └──────────────────────────────────────────────────────────────────────────┘

  new_requisition ─┐
  requisition_status ─┼─▶ Cora ──(open_requisition)──▶ Sourcing ──┬── clean + known supplier ──▶ Planner
  (status ask) ────┘   (coordinator)   (super_agent,   │           ├── clean + NEW supplier ──▶ Compliance ──(clear)──▶ Planner
                                        playbook,        │           ├── low-confidence ──▶ requester (ask, not guess)
  daily_reorder_sweep ─▶ Sourcing       copies ×3)       │           └── budget (> $50K) / policy flag ──▶ Procurement Lead
  source_suppliers ───▶ Sourcing                         │
                                                         ▼
                          Planner ──┬── ties out ──▶ requester (confirm) ──▶ PO Issuer
                          (cross-    ├── mismatch ──▶ Sourcing (rework, BOUNDED)
                           check     └── rework exhausted ──▶ Procurement Lead (terminal)
                           floor)                                   │
                                                                    ▼
             audit ◀── PO Issuer ◀──(approved)── approver ◀──(routed)── PO Issuer
           (WORM)   (FINISHER: re-check approval, re-check invariants,
                     render ./out/<PO>.md, DELETE-ON-BLOCK, register open PO)
```

**One human, one agent, one system per stage.** The agent produces, the human
decides, the system records — no stage advances on an agent's own authority.

---

## 2. Entry points (one per real trigger)

| Entry point | Target | Trigger |
|---|---|---|
| `new_requisition` | Cora | A buyer files a requisition |
| `requisition_status` | Cora | "Where is my requisition?" |
| `daily_reorder_sweep` | Sourcing | SKUs below reorder point (also a `wake_up` cron, `Asia/Kolkata`) |
| `source_suppliers` | Sourcing | Ad-hoc supplier sourcing for a set of SKUs |
| `requester_reply` | requester | HITL resume — the requester answers / confirms in their channel |

---

## 3. Agents

### 3.1 Cora — Procurement Coordinator (LLM, pure YAML)
Owns the case end to end: opens it, hands to Sourcing, keeps live status, routes
exceptions to the Procurement Lead. **Never sources or approves.**
Wires: `open_requisition → sourcing`, `escalate → procurement_lead`.

### 3.2 Sourcing Analyst (LLM · super_agent · playbook · scaling.copies · tools)
Turns a requisition into confidence-scored, quote-traced recommended lines. The
**super_agent** (dict form: `cost_ceiling`, `wall_clock`) plans/executes/verifies
across many SKUs. Three **playbook** intents fetched on demand — `rfq` (read
requisition + stock, pull quotes), `compare` (score options, respect MOQ,
convert currency), `po_prep` (estimate landed cost, flag budget, set
`new_supplier`). Day-0 **tools** read the seeded requisition / inventory /
supplier store and **announce themselves** (`available:false`); a commented `mcp:`
block wires the real ERP/SRM. **`scaling.copies.count: 3`** fans out the daily reorder
backlog — each copy claims a **disjoint** batch via the atomic `claim_reorder_batch`
tool.

**`@chain` coverage floor (fail closed):** a SKU with no supplier, a line missing
a price/quantity, or a line scored below 0.85 confidence can never be handed on as
"clean" — it becomes one targeted question to the requester. `agency/sourcing_agent.py`.

Wires: `low-confidence + question → requester`, `budget/policy flag →
procurement_lead`, `clean + new_supplier → compliance`, `clean + known supplier →
planner`.

### 3.3 Compliance Screener (LLM)
Screens a **new** supplier against sanctions / denied-party / conflict-minerals /
modern-slavery risk before it is ever used. Empty `sanctions_hits` on no match —
never a guess. Wires: `hard_stop → procurement_lead`, `review → procurement_lead`,
`clear → planner`.

### 3.4 Procurement Planner (LLM · playbook · finisher-lite)
Turns the sourcing recommendation into a plan (a draft PO pack), cross-checks
every line total, the plan total, currency and supplier MOQ against the source,
and gets the requester's one-look confirmation. One **playbook** intent
(`build_plan`).

**`@chain` cross-check floor (fail closed):** on a mismatch it returns to Sourcing
with the specific discrepancy — never balances the numbers itself. The back-edge
is **BOUNDED** by a per-session rework counter (`session_stash`): after
`_MAX_REWORK` rounds it routes to the Procurement Lead (terminal) instead of
looping. A confirmed plan renders to `./out`. `agency/procurement_planner_agent.py`.

Wires: `awaiting_confirmation → requester`, `mismatch + not exhausted → sourcing`,
`rework exhausted → procurement_lead`, `confirmed → po_issuer`.

### 3.5 PO Issuer — the FINISHER (programmatic)
Sits **behind** the finance approval step. Two phases: from the confirmed plan it
routes to the approver (`status: routed`); from the approver's reply it issues.
The **finisher pattern**, enforced in code (`agency/po_issuer_agent.py`):
1. **Approve-token re-check** — an allow-list + block-list; an unknown/ambiguous
   verdict **fails closed** (not approved).
2. **Deterministic invariants** a human can't waive — the approval came from the
   finance approver seat (`approver_human`, never the requester), an approval
   reference on record, a real supplier per line, positive quantity and unit price.
3. **Render** the PO pack to `./out/<PO>.md`; on a tripped floor **delete the
   partial** and record `status: blocked` + `block_reasons` — never a half-issued PO.
4. **Dev-store recovery** across the HITL gap (upstream → `session_stash` → dev
   store); the dev send **announces itself** (`submitted_via: dev_store`) — never a
   fake ERP ack.

Wires: `routed → approver`, `always → audit`.

### 3.6 Human members (first-class)
`requester_human`, `approver_human`, `procurement_lead_human` — each `dual` on the
human boundary, reached in Studio's Inbox (or a webhook connection, if you add one).
**Nothing is approved or issued on silence.** A reject or hold from the approver
goes to the audit sink. If nobody answers within the reply window (requester 2
days, finance approver 2 days, Procurement Lead 3 days), the Inbox card closes and
the ticket records that no reply came; nobody is chased automatically.
`fallback_on_timeout` + a `held_for_review` fallback stay in the config as the
declared intent; on leafmesh 2.4.486 a dual seat's timeout does not route it yet.

**Delegation of authority.** The finance approver signs POs up to $50,000
(`HIGH_VALUE_THRESHOLD_USD`). Above that, the Sourcing floor adds
`budget_threshold_exceeded` itself and the requisition goes to the Procurement
Lead for a formal tender; the pod issues no PO for it. `dual` is on the human boundary only; every
downstream peer they call is `chain` — no `dual→dual` ping-pong.

### 3.7 WORM audit (external, custom sink)
`procurement_audit_logger_agent` — hash-chained, append-only; the terminus of
every path. Registered in `main.py` via `sdk.intelligence(...)`.
`agency/_shared/audit_logger.py`.

---

## 4. Controls & correctness (2.4.131)

- Every agent has an explicit `name:`; every LLM prompt ends with an OUTPUT
  CONTRACT naming exactly its `yields` keys.
- `dual` only on the human/boundary agents; all AI specialists are `chain`
  (0 dual warnings at load).
- The `mcp` ERP/SRM block ships **commented** (the SDK rejects an empty-URL http
  mcp at load); day-0 dev-store tools cover the same actions.
- `max_tool_calls_per_message: 15` on Sourcing (a long read chain across SKUs;
  the default 5 truncates silently).
- `super_agent` in **dict** form only; `wake_up` carries a `timezone` (crons fire
  in UTC otherwise); one cron line + `scaling.copies` for the fan-out.
- `requirements.txt`: `leafmesh>=2.4.502` + `apscheduler>=3.10`, core-only.

---

## 5. Verification

- `leafmesh config check` → the stored configuration: clean.
- Day-0 load on 2.4.131 → **0 dual warnings**, all three `*_agent.py` bound.
- Finisher fail-closed proven with `asyncio.run`: unknown/blocked/invalid verdicts
  → `blocked` + partial deleted; explicit approval + valid plan → issued + pack on
  disk; plan recovered across the HITL gap from the stash.
- **The pod's discipline in one sentence:** agents produce, humans decide, systems
  record — and nothing is approved or issued on silence.
