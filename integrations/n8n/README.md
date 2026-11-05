# Connecting Invoice Desk to email, Slack and a vendor form with n8n

**Who does what:** n8n moves messages, and Invoice Desk makes every decision. If n8n is down, the app still works. You upload invoices in the UI, and messages are written to the Activity log instead of being sent.

```
Gmail (AP mailbox) ──► [1 Email intake] ──┐
Vendor upload form ──► [2 Vendor form] ───┴──► POST /api/intake ──► Invoice Desk decides
                                                                         │
Slack / email ◄── [3 Notify decisions] ◄── webhook on every decision ◄──┘
   ▲   one-click Approve / Reject links ──► /a/<signed link> ──► Invoice Desk records who decided
   └── [4 Escalation timer] every hour ──► POST /api/escalations/run (overdue questions, once each)
Agent's questions to vendors / Stores ──► [3] email ──► their reply tagged [Ref <id>] ──► [5 Read replies] ──► POST /api/replies
                                                         (the agent reads it, supplies the fact, re-runs every check)
```

## 1. Start n8n

Either use n8n Cloud (free trial) or run it yourself:

```bash
docker run -it --rm -p 5678:5678 \
  -e INVOICE_DESK_URL=https://YOUR-APP-URL \
  -e INVOICE_DESK_TOKEN=pick-a-long-random-string \
  -v n8n_data:/home/node/.n8n n8nio/n8n
```

On n8n Cloud, add `INVOICE_DESK_URL` and `INVOICE_DESK_TOKEN` as variables instead. If `$env` access is blocked there, replace the two expressions in the HTTP Request nodes with the values directly.

## 2. Configure the app

Set these where the app runs (Hugging Face Space → Settings → Variables, or Render → Environment):

| Variable | Value |
|---|---|
| `INTAKE_TOKEN` | the same string as `INVOICE_DESK_TOKEN` above |
| `PUBLIC_URL` | the app's public URL, used to build the one-click links in Slack and email |
| `N8N_WEBHOOK_URL` | the **Production URL** of workflow 3's webhook node (copy it from n8n after importing) |
| `APP_SECRET` | any long random string, used to sign the approve/reject links |

## 3. Import the workflows

In n8n: **Workflows → Import from file**, once for each JSON file in this folder. Then:

1. **Email intake:** open "AP mailbox (Gmail)" and "Mark email as read", and connect your Gmail account. It picks up **unread** emails with PDF attachments and marks them read once Invoice Desk has them. Duplicate deliveries are ignored, because the app remembers each email's message ID.
2. **Vendor upload form:** activate it, then open the form URL shown on the trigger node. Vendors upload a PDF and get a reference number. No credentials needed, so **this is the easiest one to demo live.**
3. **Notify decisions:** connect Slack and Gmail. Edit the **routing table** at the top of "Format message + route" (each owner's email and Slack channel). Activate, copy the production webhook URL, and put it in `N8N_WEBHOOK_URL` on the app.
4. **Escalation timer:** connect Slack and Gmail, edit the same routing table, and activate. Every hour it asks the app which questions are past their deadline. Each one is escalated exactly once.
5. **Read replies:** connect Gmail and activate. It picks up unread replies whose subject carries `[Ref …]` and hands them to the agent.
   - Only the vendor's address on file and colleagues on `INTERNAL_EMAIL_DOMAINS` can supply facts or approve.
   - Anyone else is recorded and ignored.

## 4. What arrives where

| Decision | Slack | Email |
|---|---|---|
| Agent recommends approval | `#ap-feed`: amount, PO, payment date, **Approve / Reject** links for the AP manager | none |
| On hold | owner's channel: the question, its deadline, **Approve / Reject** links | owner: same, with buttons |
| Rejected | `#finance-alerts` (fraud) or owner's channel | owner |
| Decided by a person | `#ap-feed`: who, how, their note | none |
| Overdue | escalation owner's channel | escalation owner |
| Agent question | `#ap-feed` (vendor) or the team's channel | vendor's address on file, or the team (e.g. Stores) |

**About the one-click links:**
- Opening a link only shows a confirmation page, because email security scanners open links automatically.
- The action itself needs a click.
- Links are signed, expire after 7 days, and work only once.
- Approving a **bank-detail change** requires ticking "I called the vendor on the number already in the vendor master".

## Test without Gmail or Slack

- Workflow 2 (vendor form) needs no credentials. Submit `samples/10_fraud_sundaram_bank_change.pdf` through the form, then watch it arrive in the app with the channel "vendor portal".
- Or call the intake API directly:

```bash
curl -H "X-API-Key: $TOKEN" -F file=@samples/06_edge_kaveri_split_po_overbilled.pdf \
     -F sender=accounts@kaveri.example -F subject="Invoice KIS/2026/0934" -F wait=true \
     https://YOUR-APP-URL/api/intake
```

- To see escalations fire without waiting 48 hours:
  `curl -X POST -H "X-API-Key: $TOKEN" "https://YOUR-APP-URL/api/escalations/run?as_of=2030-01-01T00:00:00Z"`

**To try the reply loop without Gmail:** open any invoice on hold in the app and use **Simulate a reply**. It's the same code path as workflow 5.

Regenerate the workflow files with `python scripts/build_n8n_workflows.py`.
