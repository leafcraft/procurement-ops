# procurement-ops — Procurement-Ops Pod

Five digital employees + three human roles that run a purchase requisition end to
end — **requisition → confidence-scored sourcing → new-supplier compliance screen →
procurement plan built & cross-checked → requester confirm → finance approval →
PO issued & registered → live status** — with a human on every gate and the systems
(ERP / SRM — SAP Ariba, Coupa, Oracle) as tools inside the agents, never members.

**Read `PRD.md`** for the full agent-by-agent spec. This is a **pod-standard
build** (SDK 2.4.131): a team of self-reliant agents, not a pipeline.

## The story

A buyer files a requisition — three SKUs, a couple of hundred units, a need-by
date. Today that kicks off a week of tab-hopping: check on-hand in the ERP,
pull three quotes out of the SRM, key them into a spreadsheet, second-guess the
totals, chase a new vendor through compliance, email finance for sign-off, and
finally cut the PO by hand — every step a place a wrong number or a skipped
sanctions check slips through. This pod runs that chain end to end, working
alongside the people who already own the decisions.

The Sourcing Analyst reads the requisition, checks stock, and pulls supplier
quotes per SKU — and **refuses to guess**: a SKU with no supplier on file becomes
one question to the requester, never a made-up vendor. A **new** supplier is
screened for sanctions before it is ever used. The Planner **cross-checks** every
line, total and MOQ against the source and returns work to Sourcing on a mismatch
rather than balancing the books itself — and that back-edge is **bounded**, so a
rework loop can never spin forever. The PO Issuer **re-checks that finance
actually approved** before it issues anything, and **deletes a half-issued PO** if
its floor trips. **Silence never advances a requisition** — every gate
default-denies. Every decision, screen, approval and issued PO lands in a
hash-chained WORM ledger.

## Why it works in an organization

Procurement is a control surface, not just a workflow — a PO is a financial
commitment, and a bad supplier is a sanctions or continuity risk. This pod keeps
the controls **separate and enforced in code**, not trusted to a prompt: the
reader is not the assembler, the assembler is not the issuer, and the issuer never
approves. It wins its accuracy the only honest way — the Sourcing Analyst asks
instead of inventing a vendor or a price; the Planner ties every number back to a
quote; the PO Issuer issues **only** on an explicit human approval and fails
closed on anything ambiguous. New suppliers can't reach a PO without passing
compliance. The result: a requisition that used to take a week of manual
tab-hopping runs in minutes with **more** control, a live "where is my
requisition?" answer, and a tamper-evident audit trail for every commitment.

## The team (9 members)

| Agent | Role (human job title) | Responsibilities | Can perform |
|---|---|---|---|
| `procurement_coordinator_agent` | Procurement Coordinator | Owns the requisition end to end; opens it, keeps live status, is the requester's one contact | LLM; orchestrates Sourcing→Compliance→Planner→PO Issuer; routes exceptions to the Procurement Lead; **never sources or approves** |
| `sourcing_agent` | Sourcing Analyst | Reads the requisition + stock, sources suppliers per SKU with a confidence score, flags budget/policy risk | LLM + **super_agent** (autonomous multi-SKU) + **instances** (backlog fan-out, atomic claim) + **playbook** (rfq / compare / po_prep) + sourcing tools; `@chain` **coverage floor** (fail closed) |
| `trade_compliance_agent` | Compliance Screener | Screens a **new** supplier against sanctions / denied-party / conflict-minerals / modern-slavery risk before use | LLM; routes a hard stop or a review need to the Procurement Lead, a clear screen to the Planner |
| `procurement_planner_agent` | Procurement Planner | Builds the plan from sourced lines, cross-checks totals/MOQs, gets the requester's one-look confirmation | LLM + **playbook**; `@chain` **cross-check floor**; renders the draft to `./out`; **bounded** back-edge to Sourcing |
| `po_issuer_agent` | PO Issuer | Routes finance approval, issues + registers the PO, records the send + ledger | Programmatic **finisher**; approve-token re-check → invariants → render pack → **delete-on-block** → WORM ledger |
| `procurement_lead_human` | Accountable owner (Human) | Reviews exceptions, owns policy/budget calls, decides every gate | Human gate — everything that leaves the pod |
| `requester_human` | Requester / buyer (Human) | Files the requisition, answers sourcing questions, confirms the plan | Human gate |
| `approver_human` | Finance approver (Human) | Approves or rejects the purchase order (a financial commitment) | Human gate |
| `procurement_audit_logger_agent` | WORM audit | Hash-chains every decision, screen, approval, issued PO, and held gate | External (custom sink); terminus |

The **LeafMesh Manager** supervises — it reads every hand-off and escalates
stalls / policy calls / approval timeouts to the Procurement Lead automatically.

## Configuration — what to set, in three tiers

Everything is set in `.env` (`cp env .env`). The **whole chain (source → screen →
plan → approval routing) runs on Tier 1 alone** against the seed store (`./data`);
requisition/inventory/supplier reads and the PO send use dev stand-ins that
**announce themselves** until you wire Tiers 2–3.

### Tier 1 — required to boot (dev mode)
| Setting | For | Notes |
|---|---|---|
| `LEAFMESH_LICENSE_KEY` | LeafMesh runtime | **Required.** |
| `ANTHROPIC_API_KEY` | Sourcing + Planner (Claude) | **Required.** |
| `OPENAI_API_KEY` | Cora + Compliance + Manager (`gpt-4o-mini`) | **Required** unless you change the models. |
| `REDIS_HOST` / `PORT` / `PASSWORD` | State store | Defaults to `localhost:6379`. `docker run -d -p 6379:6379 redis:7-alpine`. |
| `HITL_OUTBOUND_URL` | Where human members are notified | Defaults to a local stub. |

### Tier 2 — real procurement connectors (set the URL, uncomment the `mcp` block)
| Setting | Enables |
|---|---|
| `ERP_MCP_URL` / `_TOKEN` | Real requisition + inventory reads, PO registration (SAP / Oracle / NetSuite) |
| `SRM_MCP_URL` / `_TOKEN` | Real supplier catalog + RFQ (Coupa / SAP Ariba) |

> ⚠️ The SDK rejects an `mcp` block with an **empty** URL at load, so the block
> ships **commented** in `configs/config.yaml`. Fill the URL, then uncomment.

### Tier 3 — direct ERP PO posting replaces the dev-store send (later).

## Run it

```bash
pip install -r requirements.txt          # core LeafMesh runs the pod day-0
docker run -d -p 6379:6379 redis:7-alpine
cp env .env                              # fill at least Tier 1
python validate_config.py                # pre-flight: wiring, yields, dead-code
python main.py                           # API at http://127.0.0.1:18820/docs
```

File a requisition (day-0, against the seed store — no connector needed):

```bash
curl -X POST http://127.0.0.1:18820/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"new_requisition",
       "data":{"requisition_id":"REQ-DEMO-001","message":"restock for the NA line"}}'
```

`REQ-DEMO-001` includes a SKU with no supplier on file on purpose — watch the
Sourcing Analyst ask the requester rather than guess. `REQ-DEMO-002` chooses a
**new** EU supplier — watch it route through compliance first. On approval, the PO
Issuer renders the PO pack to `./out/` and registers it in `./data/open_pos.json`.

> Packaging note: `pip install leafmesh` (core) includes all LLM providers and
> runs the pod. For the real ERP/SRM connectors add `leafmesh[mcp]`. Avoid
> `leafmesh[all]` on Python 3.12+ (it pulls `cchardet`, which has no wheel) — add
> only the specific extras you need.

---

*Built with `create-leafmesh`. The config is the department; the PRD is the brief.*
*Design doctrine: `.claude/skills/leafmesh/building-agents-thoroughly.md`.*
