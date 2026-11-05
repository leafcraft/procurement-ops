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
its floor trips. **Silence never advances a requisition** — if nobody answers
within the reply window, the Inbox card closes and the ticket records that no
reply came; nothing is approved or issued, and nobody is chased automatically.
**Above $50,000 the pod does not issue a PO** — the Sourcing floor flags it and
the Procurement Lead runs a formal tender. Every decision, screen, approval and issued PO lands in a
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
| `sourcing_agent` | Sourcing Analyst | Reads the requisition + stock, sources suppliers per SKU with a confidence score, flags budget/policy risk | LLM + **super_agent** (autonomous multi-SKU) + **scaling.copies** (3 copies, backlog fan-out, atomic claim) + **playbook** (rfq / compare / po_prep) + sourcing tools; `@chain` **coverage floor** (fail closed) |
| `trade_compliance_agent` | Compliance Screener | Screens a **new** supplier against sanctions / denied-party / conflict-minerals / modern-slavery risk before use | LLM; routes a hard stop or a review need to the Procurement Lead, a clear screen to the Planner |
| `procurement_planner_agent` | Procurement Planner | Builds the plan from sourced lines, cross-checks totals/MOQs, gets the requester's one-look confirmation | LLM + **playbook**; `@chain` **cross-check floor**; renders the draft to `./out`; **bounded** back-edge to Sourcing |
| `po_issuer_agent` | PO Issuer | Routes finance approval, issues + registers the PO, records the send + ledger | Programmatic **finisher**; approve-token re-check → invariants → render pack → **delete-on-block** → WORM ledger |
| `procurement_lead_human` | Accountable owner (Human) | Reviews exceptions, owns policy/budget calls, decides every gate | Human gate — everything that leaves the pod |
| `requester_human` | Requester / buyer (Human) | Files the requisition, answers sourcing questions, confirms the plan | Human gate |
| `approver_human` | Finance approver (Human) | Approves or rejects the purchase order (a financial commitment) | Human gate |
| `procurement_audit_logger_agent` | WORM audit | Hash-chains every decision, screen, approval, issued PO, and held gate | External (custom sink); terminus |

Reply windows (`human_timeout_seconds`): Requester 2 days, Finance approver 2
days, Procurement Lead 3 days. The finance approver signs POs up to $50,000
(`HIGH_VALUE_THRESHOLD_USD` in `.env`); the PO Issuer only accepts an approval
from the approver seat, never from the requester.

The **LeafMesh Manager** supervises. It escalates to the Procurement Lead when an
agent fails with an error a retry cannot fix (auth, bad request, credit, bad
config), when 5 different agents fail in one session, or when restarts run out.
A broken hand-off contract raises an alert only. Budget/policy flags, sanctions
hits and an exhausted rework loop reach the Lead on the agents' own wires.

The Sourcing Analyst's three copies split the reorder backlog with her own atomic
claim tool (`claim_reorder_batch`). leafmesh 2.4.484's `scaling.copies.mode: auto`
with `claim_tool:` could replace this manual split (the mesh would claim each item
and start a copy only when there is one); that switch is the owner's decision and
is not made in this template.

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
| *(no setting)* — human members | Human approvals | Answer in Studio's Inbox. To notify an external tool instead, create a vault connection (an "Add custom" record on the Infrastructure page, service type 'Outbound webhook') and set `webhook_config: {connection: <name>}` on the seat. |

### Tier 2 — real procurement connectors (a vault connection, then uncomment the `mcp` block)
| Vault connection (Infrastructure → Integrations → MCP) | Enables |
|---|---|
| `erp_mcp` — your ERP's MCP server (address + token) | Real requisition + inventory reads, PO registration (SAP / Oracle / NetSuite) |
| `srm_mcp` — your SRM's MCP server (address + token) | Real supplier catalog + RFQ (Coupa / SAP Ariba) |

> The server address and token are entered **only in the vault** — the SDK
> refuses a `url:` / `token:` written in the config (even as `${ENV_VAR}`). The
> `mcp` block ships **commented** in `configs/config.yaml`, already picking
> `connection: erp_mcp` / `connection: srm_mcp`: create the connections, then
> uncomment — in the file before the first start, in Studio's YAML editor after
> it (applies live, no restart; see *Changing the configuration after the first
> start*). Nothing goes in `.env`: `agency/tools.py` checks whether the vault
> record `erp_mcp` or `srm_mcp` exists, only to word an empty dev-store answer.
> The PO Issuer still registers POs in the dev store (`submitted_via: dev_store`)
> until you swap `store.register_open_po` for your ERP call (Tier 3).

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
**new** EU supplier — watch it route through compliance first. `REQ-DEMO-003` is
the clean path ($3,375.00, on-file suppliers, MOQs met). `REQ-DEMO-004` is
$62,500 — above the $50,000 limit, so it goes to the Procurement Lead. Each human gate
(requester question, approver sign-off) lands in **Studio → Inbox** — answer it
there. On approval, the PO
Issuer renders the PO pack to `./out/` and registers it in `./data/open_pos.json`.

> Packaging note: `pip install leafmesh` (core) includes all LLM providers and
> runs the pod. For the real ERP/SRM connectors add `leafmesh[mcp]`. Avoid
> `leafmesh[all]` on Python 3.12+ (it pulls `cchardet`, which has no wheel) — add
> only the specific extras you need.

---

*Built with `create-leafmesh`. The config is the department; the PRD is the brief.*
*Design doctrine: `.claude/skills/leafmesh/building-agents-thoroughly.md`.*

## Changing the configuration after the first start

`configs/config.yaml` is only the first-start seed. On the first start (an empty store) the agency imports it as version 1 and from then on keeps its configuration in the store. After that the file is not read again: an edit to it is **not** applied, even after a restart (the boot log says "that edit was NOT applied").

To change a running agency:

- **Studio** — its settings pages, or the raw YAML editor.
- **API** — `PUT /api/yaml/raw` with an `X-API-Key` header and the body `{"yaml_content": "<the whole YAML>", "save_to_disk": true}`, or `PATCH /api/yaml/config` with `{"updates": {...}, "save_to_disk": true}`.

Each save is a new version and applies live on every pod — no restart. (A few settings, such as brokers, make the agency reload itself in place within seconds.) So wherever this README or a comment says "uncomment X in `configs/config.yaml`", do it in the file before the first start, or in Studio's YAML editor after it.

## Changing a running project (Docker)

What reaches a project that is already running (leafmesh 2.4.497). The
image holds the Python code and the first-start `configs/config.yaml`;
`.env` is passed in when the container is created; the store (Redis) keeps
its data on its own volume.

| You change | When it applies |
|---|---|
| An agent in Studio (prompt, model, tools, routes, human seats, schedules, …) | At once, no restart |
| A human seat's `channels` (Slack, email, …) | The agency reloads itself in place within seconds |
| Add or delete an agent in Studio | At once, including routes and schedules |
| A connection in the vault (model keys, MCP, webhooks, connectors, Slack, email) | At once. An agent switched off for a missing connection or address switches back on by itself |
| Anything in `.env` (licence, agency name, store, ports) | After `docker compose up -d` (the container is recreated) |
| Python code (`agency/*.py`: programmatic agents, tools, `@pre_compose`) | After a rebuild: `docker compose up -d --build` |
| `configs/config.yaml` | Never after the first start. It is read only when the store is empty |
| Several app containers on one store | A Studio save reaches every one within about 15 seconds |

- Keep model keys in the vault. A key there wins over the same key in
  `.env` (the log warns), and a change applies at once. From 2.4.497 that
  holds for every value the vault holds (endpoints, organisation / project
  ids, cloud identities); the `.env` value returns when the record goes.
- Code the image does not have: an agent marked pure Python
  (`integration: python`) is switched off until its function is in the image;
  any other programmatic agent stays on, and each call to code it does not
  have fails. Rebuild the image with the code first.
- A new Redis volume is an empty store, so `configs/config.yaml` is imported
  again as version 1.
- **If the agency refuses to start after an upgrade** (a new leafmesh pin,
  then a rebuild: "Configuration has N error(s) in the stored
  configuration"), repair the stored configuration with the agency stopped:

  ```bash
  docker compose run --rm app leafmesh config check      # each problem, with its line
  docker compose run --rm app leafmesh config versions   # kept versions: which pass, which is in use
  docker compose run --rm app leafmesh config rollback   # back to the newest version that passes (saved as a new version)
  ```

  Or `leafmesh config export > config.yaml`, fix it by hand, then
  `leafmesh config import config.yaml` (it is checked before it is saved).

## Where the data store is set

From leafmesh 2.4.482 the store (Redis or Postgres) is set **only in `.env`** — never in `configs/config.yaml`. A `persistence:` block in the YAML is ignored with a warning on leafmesh 2.4.482–2.4.485; from 2.4.486 the agency refuses to start if that block differs from `.env`, and names each `.env` line to add. The project's `.env` sets the store explicitly (`LEAFMESH_STORE`, `LEAFMESH_AGENCY_NAME`, `REDIS_*`).

- Redis (default): `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_DB`; optional `REDIS_DEFAULT_TTL`, `REDIS_SESSION_TTL`, `REDIS_SSL*`, `REDIS_CLUSTER_NODES`.
- Postgres instead: `LEAFMESH_STORE=postgres` plus `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (optional `POSTGRES_SSLMODE`).
- Or the whole server in one value: `REDIS_URL=redis://user:pw@host:6379/0` (`rediss://` turns TLS on) or `DATABASE_URL=postgresql://user:pw@host:5432/db`. The single names fill in what the URL leaves out.
- With Docker Compose the app reaches Redis as `redis` (the service name); compose sets that for you.
- Check it in Studio → **Infrastructure → Datastores**: it shows whether the store is reachable (never the address or password).

## This mesh's sign-in (leafmesh 2.4.497)

From leafmesh 2.4.497 each mesh signs in to LeafCraft with its own key, not
a key shared by every install.

- **First start.** The mesh makes a key pair and registers it with
  `LEAFMESH_LICENSE_KEY` and `LEAFMESH_ENV_TOKEN` from `.env` (copy the
  environment token from Studio — a placeholder such as `development` is
  refused). With both valid it works at once. If your organisation turned on
  "require approval", the first start stops and says so: approve it in
  Studio → **Keys → SDK access**, then start again. Organisation admins get an
  email for every new registration.
- **Where the key lives.** In the vault, in the store, encrypted with
  `LEAFMESH_VAULT_KEY`: it survives rebuilds and every copy of the app shares
  it. A new Redis volume, or a new vault key, makes a new key pair that
  registers again — revoke the old one in Studio.
- **Check or change it** (in Docker: `docker compose run --rm app leafmesh auth status`):
  - `leafmesh auth status` — credential id, key fingerprint, registration and
    mode, never a key; `--check` signs in now and reports the tokens.
  - `leafmesh auth register` — register now.
  - `leafmesh auth rotate` — a new key pair: once it is active the mesh
    switches to it and revokes the old one. A revoked or expired key is never
    revived; rotate instead.
- **Lost the environment token?** From 5 Dec 2026 Studio shows the licence
  key and environment token only once, when they are made (then only a hint).
  Rotate the environment token in Studio and put the new one in `.env`.
  Rotating it changes where the agency's data is kept in the store, so do it
  before the agency holds data.
- **An existing project whose `.env` says `LEAFMESH_ENV_TOKEN=development`.**
  2.4.497 needs the real token. The token also names where the agency's data
  sits in the store, so with the real token the agency starts on an empty
  store (vault, sessions, board) — the old data stays under the old name. Set
  the real token before you put data in the agency, or contact us to move it.

## Your vault key — back it up

`create-leafmesh` put a `LEAFMESH_VAULT_KEY` in your `.env` when it made this project. It is the master key that encrypts every connection and credential you store in the vault (in Redis / Postgres).

- **Back it up like a master password.** Lose it and every stored connection has to be entered again.
- **Never change or regenerate it** for an existing project — a new key locks the existing vault. To change it, use `leafmesh vault rotate-key`.
- Every copy of the app (every replica) must get the **same** value. Docker Compose passes it in from `.env` as an environment variable.
- Never commit `.env` (it is already in `.gitignore`).
