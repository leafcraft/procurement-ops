# Supply Chain AI System — How It Works
### A Plain-Language Guide for Non-Technical Readers

---

## What Is This System?

This is an **automated supply chain management system** powered by AI agents. Think of it as a team of specialists — each one focused on a specific job — who work together automatically when an order comes in, without anyone having to manually hand off work from one person to the next.

The system handles everything from receiving an order to issuing purchase orders, and it only pauses to ask a human for approval at the right moments.

---

## The Big Picture

When a new order arrives, the system goes through six stages automatically:

```
Order Arrives
     ↓
1. Order Intake      — AI reads and understands the order
     ↓
2. Compliance Check  — AI screens for risks and flags anything unusual
     ↓
3. Human Approval    — A real person reviews and approves (or rejects)
     ↓
4. Inventory Check   — System checks current stock levels
     ↓
5. Supplier Research + Demand Forecast — AI finds the best supplier and calculates how much to order
     ↓
6. Logistics Planning — AI builds the procurement plan
     ↓
7. PO Approval       — A real person approves the purchase orders
     ↓
8. Purchase Orders   — System generates and issues formal POs
```

Each stage is handled by a different "agent" (a specialist). They pass their work to the next agent automatically.

---

## The Agents — Who Does What

### 1. Order Intake Agent
**Role:** The receptionist who reads every incoming order.

- Receives the raw order message (could be an email, API call, or webhook)
- Uses AI to extract the key details: what items are needed, how many, and how urgent
- Assigns an urgency level: **Critical** (needed within 24 hours), **High** (within 72 hours), or **Normal**
- Flags any risks it notices, such as unusually large quantities or restricted goods
- Hands the structured order summary to the next agent

> **Example:** Someone sends: *"Urgent: need 50 units of Widget A and 10 Pressure Sensors for a hospital. Delivery in 3 days."* The agent extracts the SKUs, quantities, urgency (High), and flags it for human review.

---

### 2. Compliance Agent
**Role:** The compliance officer who screens every order before it goes further.

- Reviews the order for export/import issues, budget limits, supplier risks, and regulatory concerns
- If something looks risky or requires budget approval, it routes the order to the Operations Manager for human review
- If everything checks out, it skips human review and goes straight to inventory

> **Example:** An order for restricted electronics to an unverified address gets flagged and sent to the Operations Manager. A routine restock order goes straight through.

---

### 3. Operations Manager (Human Approval — Gate 1)
**Role:** The human decision-maker who reviews flagged orders.

- Receives a notification with all the order details
- Reviews the compliance flags and the AI's analysis
- Responds with **Approved** or **Rejected**
- Has 10 minutes to respond; if no response, the system auto-approves and continues

> **What happens if rejected?** The entire chain stops. No inventory check, no suppliers contacted, no purchase orders. The session is closed.

> **What happens if approved?** The order moves to inventory checking.

---

### 4. Inventory Agent
**Role:** The warehouse manager who checks stock levels.

- Looks up every item in the order against the current inventory
- Categorizes each item as: **Critical** (below safety stock or out of stock), **Low** (getting low but not yet critical), or **Normal** (plenty available)
- If any items are Critical or Low, it triggers the full procurement chain
- If everything is Normal, it stops — no restocking needed

> **Example:** Widget A has 15 in stock but needs 20 as a safety buffer → **Critical**. The system immediately starts finding suppliers.

---

### 5. Demand Forecast Agent
**Role:** The analyst who calculates exactly how much to order.

- Takes the inventory results and calculates the optimal order quantity
- Considers lead times (how long the supplier takes to deliver) and safety buffers
- Applies a quarter-end uplift if it's near the end of a business quarter (demand typically spikes)
- Passes the adjusted quantities to the Supplier Agent

---

### 6. Supplier Agent
**Role:** The procurement analyst who finds the best supplier.

- Searches the supplier catalog for options for each item that needs restocking
- Scores each supplier using a formula:
  - **40%** — Reliability (on-time delivery history)
  - **35%** — Lead time (faster = better score)
  - **25%** — Price (cheaper = better score)
- Recommends the top-scoring supplier for each item
- Remembers findings from previous sessions — if a supplier had issues last month, it factors that in
- Passes its analysis to the Logistics Agent

---

### 7. Logistics Agent
**Role:** The strategic planner who builds the procurement plan.

- Combines the inventory data and supplier analysis into a concrete procurement plan
- Decides which items to order immediately vs. monitor
- Calculates total estimated cost and expected delivery timeline
- Assesses overall risk (single supplier dependency, budget concerns, etc.)

**Important routing decision made here:**
- If the total cost is **over $5,000** → routes to **Finance Approval** (human review)
- If the total cost is **$5,000 or under** → routes directly to **PO Approval**

---

### 8. Finance Approval Agent *(only for orders over $5,000)*
**Role:** The finance officer who approves large purchases.

- Receives the full procurement plan with cost breakdown
- Reviews and responds: **Approved** or **Rejected**
- Has 60 seconds to respond; if no response, the system auto-approves and continues

---

### 9. PO Approval Agent
**Role:** The final human checkpoint before purchase orders are issued.

- Receives the complete procurement plan
- A real person reviews it and types **approved** or **rejected**
- Has 5 minutes to respond; if no response, the system auto-approves

> This is the last chance to stop before money is committed.

---

### 10. Purchase Order Agent
**Role:** The administrator who generates the formal paperwork.

- Takes the approved procurement plan and creates official purchase orders
- Groups orders by supplier (one PO per supplier)
- Assigns PO numbers, calculates delivery dates based on lead times, sets payment terms
- Records the approval reference on every PO

> **Example output:** PO-20260504-X7K2 issued to EuroSource GmbH for 3 line items totalling $12,400, requested delivery 2026-05-19, approved by Finance.

---

### 11. Supply Report Agent *(runs automatically every day at 8 AM)*
**Role:** The daily health monitor.

- Runs automatically every morning without anyone triggering it
- Checks every SKU in the inventory and every supplier's performance
- Generates a full health report: how many items are critical, how many suppliers are on-time, average lead times, defect rates
- If it finds critical items, it automatically triggers the full procurement chain

> You can also trigger this report manually at any time on demand.

---

## Entry Points — Ways to Start a Workflow

The system has six doors you can walk through, depending on what you need:

| Entry Point | What It Does |
|---|---|
| **Process Order** | The main door — send an order message and the full chain kicks off |
| **Manager Contact** | The manager contacts the system directly to initiate a check |
| **Check Inventory** | Skip straight to checking stock — bypasses order intake |
| **Find Suppliers** | Jump straight to supplier research for specific items |
| **Plan Logistics** | Jump straight to logistics planning with known data |
| **Supply Report** | Generate the health report on demand (also runs daily automatically) |

---

## Human Touchpoints — When Does a Person Get Involved?

The system is designed to be mostly automated, but it pauses for human input at three specific moments:

| When | Who | What They Decide | Timeout |
|---|---|---|---|
| After compliance screening (if flagged) | Operations Manager | Approve or reject the order | 10 minutes |
| After logistics planning (if cost > $5,000) | Finance Officer | Approve or reject the spend | 60 seconds |
| Before purchase orders are issued | Procurement Officer | Approve or reject the POs | 5 minutes |

If a person doesn't respond in time, the system applies a sensible default (usually auto-approve) and moves on. Every decision — whether by a person or by timeout — is recorded.

---

## What Happens If Something Goes Wrong

The system is built to handle problems gracefully:

- **Human rejects an order** → Chain stops immediately. No suppliers contacted, no POs issued.
- **Human doesn't respond in time** → System auto-approves with a note in the record.
- **Supplier catalog has no good options** → A backup "fallback" supplier selection runs instantly alongside the main research, so the logistics plan is never stuck waiting.
- **Items are all in stock** → The system stops after the inventory check. No unnecessary procurement triggered.
- **Cost exceeds budget** → The logistics agent flags it in the risk assessment and routes to finance review.

---

## The Daily Automated Cycle

Every morning at 8 AM, without anyone lifting a finger:

1. The **Supply Report Agent** wakes up
2. It scans all 19+ inventory items
3. It checks all supplier performance records
4. It generates a health report with key metrics
5. If anything is critically low, it automatically starts the procurement chain — inventory check, supplier research, logistics planning, and PO approval — all the way through

This means routine restocking happens automatically. Human attention is only required for the approval steps.

---

## How the System Protects Against Bad Orders

Several layers of protection are built in:

1. **Compliance screening** — every order is checked for legal, regulatory, and budget issues before a human ever sees it
2. **Human approval gates** — flagged orders and large purchases require explicit human sign-off
3. **Webhook security** — all external messages are cryptographically signed; forged or tampered messages are rejected
4. **Session tracking** — every order creates an audit trail showing every decision made, by whom, and when
5. **Rejection stops everything** — a single "rejected" from any human checkpoint ends the workflow immediately

---

## Summary — The Flow in One Sentence

> An order comes in, AI reads and screens it, a human approves or rejects it, the system checks stock and finds the best supplier, a human approves the purchase plan, and the system issues formal purchase orders — all automatically, with a full audit trail of every step.
