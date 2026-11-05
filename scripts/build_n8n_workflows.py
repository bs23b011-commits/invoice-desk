"""Generate the importable n8n workflows in integrations/n8n/.

  python scripts/build_n8n_workflows.py

Four workflows; n8n only moves messages, every decision is made by Invoice Desk:
  1. Email intake        Gmail (AP mailbox) -> each PDF attachment -> POST /api/intake -> mark email read
  2. Vendor upload form  public n8n form where a vendor drops an invoice PDF -> POST /api/intake
  3. Notify decisions    webhook from Invoice Desk -> route by decision -> Slack + email to the owner
  4. Escalation timer    every hour -> POST /api/escalations/run -> message the escalation owner
  5. Read replies        replies tagged [Ref <id>] -> POST /api/replies -> the agent reads them and re-checks

Settings come from n8n environment variables, so nothing secret is stored in the workflow files:
  INVOICE_DESK_URL    e.g. https://your-app.hf.space   (default http://localhost:8000)
  INVOICE_DESK_TOKEN  same value as INTAKE_TOKEN on the app
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "integrations" / "n8n"
URL = "={{ ($env.INVOICE_DESK_URL || 'http://localhost:8000').replace(/\\/$/, '') }}"
TOKEN = "={{ $env.INVOICE_DESK_TOKEN || '' }}"

ROUTING_JS = r"""// ROUTING TABLE - edit these to your people and channels.
const ROUTES = {
  'AP reviewer':            { email: 'ap-team@yourcompany.com',      slack: '#ap-approvals' },
  'Procurement (PO owner)': { email: 'procurement@yourcompany.com',  slack: '#procurement' },
  'Vendor master team':     { email: 'vendor-master@yourcompany.com', slack: '#vendor-master' },
  'Tax team':               { email: 'tax@yourcompany.com',          slack: '#tax' },
  'Finance controller':     { email: 'controller@yourcompany.com',   slack: '#finance-alerts' },
  'Category manager':       { email: 'category@yourcompany.com',     slack: '#procurement' },
  'CFO':                    { email: 'cfo@yourcompany.com',          slack: '#finance-alerts' },
  'Stores / receiving':     { email: 'stores@yourcompany.com',       slack: '#stores' },
};
const FEED = '#ap-feed';            // approvals and resolutions are posted here
const fallback = ROUTES['AP reviewer'];
"""

FORMAT_JS = ROUTING_JS + r"""
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
return $input.all().map((item) => {
  const b = item.json.body || item.json;            // webhook puts the payload under body
  if (b.event === 'agent.message') {                // the agent writing to a vendor (address on file) or a team
    const m = b.message;
    const internal = m.party.startsWith('internal:');
    const route = internal ? (ROUTES[m.party.slice(9)] || fallback) : null;
    return { json: { event: b.event, decision: null, owner: m.party, run_id: b.run_id,
      email_to: internal ? route.email : m.to, email_subject: m.subject,
      email_html: `<div style="font:14px/1.5 system-ui;white-space:pre-wrap">${esc(m.body)}</div>`,
      slack_channel: internal ? route.slack : FEED,
      slack_text: `:speech_balloon: Agent wrote to ${internal ? m.party.slice(9) : m.to}: ${m.subject}\n<${b.url}|Open>` } };
  }
  const head = `${b.vendor} - invoice ${b.invoice_number || '?'} for ${b.total_text}`;
  const first = (b.handoff || [])[0];
  const owner = b.event === 'invoice.resolved' ? null : (first ? first.owner : null);
  const route = owner ? (ROUTES[owner] || fallback) : null;
  let slack, subject, html;
  if (b.event === 'invoice.resolved') {
    const r = b.resolution || {};
    slack = `:white_check_mark: *${r.action === 'APPROVE' ? 'Approved' : 'Rejected'}* by ${r.by} (${r.via})\n${head}${r.note ? `\n> ${r.note}` : ''}\n<${b.url}|Open in Invoice Desk>`;
  } else if (b.decision === 'APPROVE' && b.approval) {  // agent recommends approval; a person gives the final OK
    const a = b.approval;
    slack = `:large_green_circle: *Waiting for your OK* (agent recommends approval): ${head}\n${b.next_action}\n` +
      `<${a.approve}|Approve payment> | <${a.reject}|Reject> | <${b.url}|Open>`;
  } else if (b.decision === 'APPROVE') {
    slack = `:large_green_circle: *Approved automatically*: ${head}\n${b.next_action}\n<${b.url}|Open>`;
  } else {
    const icon = b.decision === 'REJECT' ? ':red_circle: *Rejected*' : ':large_orange_circle: *On hold*';
    const parts = (b.handoff || []).map((h) =>
      `*${h.category}* (for ${h.owner})\n${h.question}\n_Answer by ${h.due_by.slice(0, 16).replace('T', ' ')} UTC, then escalates to ${h.escalate_to}_` +
      (h.approve ? `\n<${h.approve}|Approve> | <${h.reject}|Reject>` : ''));
    slack = `${icon}: ${head}\n${parts.join('\n\n')}\n<${b.url}|Open in Invoice Desk>`;
    subject = `[${b.decision === 'REJECT' ? 'Rejected' : 'Action needed'}] ${head}`;
    html = `<p><b>${esc(head)}</b></p><p>${esc(b.headline)}</p>` + (b.handoff || []).map((h) =>
      `<div style="border-left:4px solid ${h.severity === 'fail' ? '#b42318' : '#a86a12'};padding:6px 12px;margin:10px 0">
       <b>${esc(h.category)}</b> - for ${esc(h.owner)}<br><span style="font-size:16px">${esc(h.question)}</span><br>
       <small>Answer by ${esc(h.due_by.slice(0, 16).replace('T', ' '))} UTC, then it escalates to ${esc(h.escalate_to)}.</small>` +
      (h.approve ? `<p><a href="${h.approve}" style="background:#1f7a4d;color:#fff;padding:8px 14px;border-radius:6px;text-decoration:none">Approve payment</a>
       &nbsp; <a href="${h.reject}" style="color:#b42318">Reject invoice</a></p>` : '') + `</div>`).join('') +
      `<p><a href="${b.url}">Open the invoice in Invoice Desk</a></p>`;
  }
  return { json: { event: b.event, decision: b.decision, owner, slack_channel: route ? route.slack : FEED,
                   email_to: route ? route.email : null, slack_text: slack, email_subject: subject || null,
                   email_html: html || null, run_id: b.run_id } };
});
"""

ESCALATE_JS = ROUTING_JS + r"""
return $input.all().map((item) => {
  const e = item.json;
  const route = ROUTES[e.escalate_to] || fallback;
  return { json: { ...e, slack_channel: route.slack, email_to: route.email,
    slack_text: `:rotating_light: *Escalated to ${e.escalate_to}* - ${e.owner} has not answered for ${e.overdue_hours} h\n` +
                `${e.vendor} - invoice ${e.invoice_number} for ${e.total_text}\n*${e.category}*: ${e.question}\n` +
                `<${e.approve}|Approve> | <${e.reject}|Reject> | <${e.url}|Open>`,
    email_subject: `[Escalated] ${e.category}: ${e.vendor} ${e.invoice_number} (${e.total_text})`,
    email_html: `<p><b>${e.owner}</b> has not answered for ${e.overdue_hours} hours, so this is now with you.</p>
      <p><b>${e.vendor}</b> - invoice ${e.invoice_number} for <b>${e.total_text}</b></p>
      <p style="font-size:16px"><b>${e.category}</b>: ${e.question}</p>
      <p><a href="${e.approve}">Approve payment</a> &nbsp; <a href="${e.reject}">Reject invoice</a> &nbsp; <a href="${e.url}">Open</a></p>` } };
});
"""

REPLY_JS = r"""// Keep only what the agent needs; strip the quoted original message below the reply.
return $input.all().map((item) => {
  const j = item.json;
  const raw = j.text || j.textPlain || j.snippet || '';
  const body = raw.split(/\r?\nOn .+wrote:|\r?\n-{2,}\s*Original Message|\r?\n>/)[0].trim();
  const sender = (j.from && j.from.value && j.from.value[0] && j.from.value[0].address) || (j.from && j.from.text) || '';
  return { json: { gmail_id: j.id, reply: { sender, subject: j.subject || '', body } } };
});
"""

SPLIT_JS = r"""// One item per PDF attachment, carrying the email details Invoice Desk records.
const out = [];
for (const item of $input.all()) {
  const j = item.json;
  for (const [key, bin] of Object.entries(item.binary || {})) {
    const name = bin.fileName || key;
    const isPdf = (bin.mimeType || '').includes('pdf') || name.toLowerCase().endsWith('.pdf');
    if (!isPdf) continue;
    out.push({
      json: {
        sender: (j.from && (j.from.text || (j.from.value && j.from.value[0] && j.from.value[0].address))) || '',
        subject: j.subject || '', message_id: j.messageId || j.id || '', gmail_id: j.id,
        received_at: j.date || '', filename: name,
      },
      binary: { data: bin },
    });
  }
}
return out;
"""


def node(name, type_, version, params, pos, **extra):
    n = dict(parameters=params, id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"invoice-desk/{name}")), name=name,
             type=type_, typeVersion=version, position=list(pos))
    n.update(extra)
    return n


def link(*names):
    c = {}
    for a, b in zip(names, names[1:]):
        c.setdefault(a, {"main": [[]]})["main"][0].append({"node": b, "type": "main", "index": 0})
    return c


def intake_http(name, pos, file_field, sender, subject, message_id, received_at, channel):
    return node(name, "n8n-nodes-base.httpRequest", 4.2, {
        "method": "POST", "url": URL + "/api/intake",
        "sendHeaders": True, "headerParameters": {"parameters": [{"name": "X-API-Key", "value": TOKEN}]},
        "sendBody": True, "contentType": "multipart-form-data",
        "bodyParameters": {"parameters": [
            {"parameterType": "formBinaryData", "name": "file", "inputDataFieldName": file_field},
            {"name": "channel", "value": channel},
            {"name": "sender", "value": sender}, {"name": "subject", "value": subject},
            {"name": "message_id", "value": message_id}, {"name": "received_at", "value": received_at},
        ]},
        "options": {"timeout": 120000},
    }, pos)


def slack(name, pos):
    return node(name, "n8n-nodes-base.slack", 2.3, {
        "select": "channel",
        "channelId": {"__rl": True, "value": "={{ $json.slack_channel }}", "mode": "name"},
        "text": "={{ $json.slack_text }}", "otherOptions": {"mrkdwn": True},
    }, pos, onError="continueRegularOutput")  # a Slack outage must not stop the email


def gmail_send(name, pos):
    return node(name, "n8n-nodes-base.gmail", 2.1, {
        "sendTo": "={{ $json.email_to }}", "subject": "={{ $json.email_subject }}",
        "emailType": "html", "message": "={{ $json.email_html }}", "options": {"appendAttribution": False},
    }, pos, onError="continueRegularOutput")


def has_email_if(name, pos):
    return node(name, "n8n-nodes-base.if", 2.2, {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
            "conditions": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, name)), "leftValue": "={{ $json.email_to }}",
                            "rightValue": "", "operator": {"type": "string", "operation": "notEmpty", "singleValue": True}}],
            "combinator": "and"},
        "options": {}}, pos)


def wf(name, nodes, connections, note):
    return dict(name=name, nodes=nodes, connections=connections, active=False, pinData={},
                settings={"executionOrder": "v1"}, meta={"templateCredsSetupCompleted": False, "note": note})


def build():
    OUT.mkdir(parents=True, exist_ok=True)

    # 1. Email intake -------------------------------------------------------------------------------
    nodes = [
        node("AP mailbox (Gmail)", "n8n-nodes-base.gmailTrigger", 1.2, {
            "pollTimes": {"item": [{"mode": "everyMinute"}]}, "simple": False,
            "filters": {"q": "has:attachment filename:pdf", "readStatus": "unread"},
            "options": {"downloadAttachments": True, "dataPropertyAttachmentsPrefixName": "attachment_"}}, (0, 0)),
        node("One item per PDF", "n8n-nodes-base.code", 2, {"jsCode": SPLIT_JS}, (240, 0)),
        intake_http("Send to Invoice Desk", (480, 0), "data", "={{ $json.sender }}", "={{ $json.subject }}",
                    "={{ $json.message_id }}", "={{ $json.received_at }}", "email"),
        node("Mark email as read", "n8n-nodes-base.gmail", 2.1, {
            "operation": "markAsRead", "messageId": "={{ $('One item per PDF').item.json.gmail_id }}"}, (720, 0)),
    ]
    w1 = wf("Invoice Desk - 1. Email intake", nodes,
            link("AP mailbox (Gmail)", "One item per PDF", "Send to Invoice Desk", "Mark email as read"),
            "Polls the AP mailbox every minute; each PDF attachment becomes one Invoice Desk run.")

    # 2. Vendor upload form ---------------------------------------------------------------------------
    nodes = [
        node("Vendor invoice form", "n8n-nodes-base.formTrigger", 2.2, {
            "formTitle": "Submit an invoice to Meridian Foods",
            "formDescription": "Upload your invoice as a PDF. Please quote our purchase order number on the invoice.",
            "formFields": {"values": [
                {"fieldLabel": "Invoice PDF", "fieldType": "file", "acceptFileTypes": ".pdf", "requiredField": True,
                 "multipleFiles": False},
                {"fieldLabel": "Your email", "fieldType": "email", "requiredField": True},
                {"fieldLabel": "Company name", "requiredField": True}]},
            "responseMode": "lastNode",
            "options": {}}, (0, 0), webhookId=str(uuid.uuid5(uuid.NAMESPACE_URL, "invoice-desk/form"))),
        intake_http("Send to Invoice Desk", (240, 0), "Invoice_PDF", "={{ $json['Your email'] }}",
                    "={{ 'Vendor portal: ' + $json['Company name'] }}", "={{ $execution.id }}",
                    "={{ $json.submittedAt }}", "vendor portal"),
        node("Thank the vendor", "n8n-nodes-base.form", 1, {
            "operation": "completion", "completionTitle": "Thank you, your invoice was received",
            "completionMessage": "={{ 'Reference: ' + $json.run_id + '. We will be in touch only if we need anything.' }}",
            "options": {}}, (480, 0)),
    ]
    w2 = wf("Invoice Desk - 2. Vendor upload form", nodes,
            link("Vendor invoice form", "Send to Invoice Desk", "Thank the vendor"),
            "A public form vendors can use instead of email. Same intake, channel = vendor portal.")

    # 3. Notify decisions ---------------------------------------------------------------------------
    sw_rules = []
    for key, label in (("resolved", "Resolved by a person"), ("REVIEW", "On hold"), ("REJECT", "Rejected"),
                       ("APPROVE", "Approved"), ("message", "Agent message")):  # agent messages have decision=null
        left = "={{ $json.event }}" if key in ("resolved", "message") else "={{ $json.decision }}"
        right = {"resolved": "invoice.resolved", "message": "agent.message"}.get(key, key)
        sw_rules.append({"conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
            "conditions": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, key)), "leftValue": left, "rightValue": right,
                            "operator": {"type": "string", "operation": "equals"}}], "combinator": "and"},
            "renameOutput": True, "outputKey": label})
    nodes = [
        node("Invoice Desk decision", "n8n-nodes-base.webhook", 2, {
            "httpMethod": "POST", "path": "invoice-desk", "responseMode": "onReceived", "options": {}},
            (0, 0), webhookId=str(uuid.uuid5(uuid.NAMESPACE_URL, "invoice-desk/webhook"))),
        node("Format message + route", "n8n-nodes-base.code", 2, {"jsCode": FORMAT_JS}, (240, 0)),
        node("By decision", "n8n-nodes-base.switch", 3.2, {"rules": {"values": sw_rules}, "options": {}}, (480, 0)),
        slack("Slack: resolution to feed", (760, -240)),
        slack("Slack: question to owner", (760, -60)),
        has_email_if("Owner has email?", (1000, -60)),
        gmail_send("Email: question to owner", (1240, -60)),
        slack("Slack: rejection alert", (760, 120)),
        gmail_send("Email: rejection to owner", (1000, 120)),
        slack("Slack: approved to feed", (760, 300)),
        gmail_send("Email: agent message", (760, 480)),
    ]
    conn = link("Invoice Desk decision", "Format message + route", "By decision")
    conn["By decision"] = {"main": [
        [{"node": "Slack: resolution to feed", "type": "main", "index": 0}],
        [{"node": "Slack: question to owner", "type": "main", "index": 0},
         {"node": "Owner has email?", "type": "main", "index": 0}],
        [{"node": "Slack: rejection alert", "type": "main", "index": 0},
         {"node": "Email: rejection to owner", "type": "main", "index": 0}],
        [{"node": "Slack: approved to feed", "type": "main", "index": 0}],
        [{"node": "Email: agent message", "type": "main", "index": 0}]]}
    conn["Owner has email?"] = {"main": [[{"node": "Email: question to owner", "type": "main", "index": 0}], []]}
    w3 = wf("Invoice Desk - 3. Notify decisions", nodes, conn,
            "Set N8N_WEBHOOK_URL on the app to this workflow's production webhook URL.")

    # 4. Escalation timer ---------------------------------------------------------------------------
    nodes = [
        node("Every hour", "n8n-nodes-base.scheduleTrigger", 1.2,
             {"rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}}, (0, 0)),
        node("Ask Invoice Desk what is overdue", "n8n-nodes-base.httpRequest", 4.2, {
            "method": "POST", "url": URL + "/api/escalations/run", "sendHeaders": True,
            "headerParameters": {"parameters": [{"name": "X-API-Key", "value": TOKEN}]}, "options": {}}, (240, 0)),
        node("One item per escalation", "n8n-nodes-base.splitOut", 1, {"fieldToSplitOut": "items", "options": {}},
             (480, 0)),
        node("Route to escalation owner", "n8n-nodes-base.code", 2, {"jsCode": ESCALATE_JS}, (720, 0)),
        slack("Slack: escalation", (960, -80)),
        gmail_send("Email: escalation", (960, 80)),
    ]
    conn = link("Every hour", "Ask Invoice Desk what is overdue", "One item per escalation", "Route to escalation owner")
    conn["Route to escalation owner"] = {"main": [[{"node": "Slack: escalation", "type": "main", "index": 0},
                                                   {"node": "Email: escalation", "type": "main", "index": 0}]]}
    w4 = wf("Invoice Desk - 4. Escalation timer", nodes, conn,
            "Each overdue question is escalated exactly once (Invoice Desk remembers).")

    # 5. Read replies -------------------------------------------------------------------------------
    nodes = [
        node("Replies in AP mailbox", "n8n-nodes-base.gmailTrigger", 1.2, {
            "pollTimes": {"item": [{"mode": "everyMinute"}]}, "simple": False,
            "filters": {"q": 'subject:"[Ref" is:unread', "readStatus": "unread"}, "options": {}}, (0, 0)),
        node("Reply fields", "n8n-nodes-base.code", 2, {"jsCode": REPLY_JS}, (240, 0)),
        node("Give reply to the agent", "n8n-nodes-base.httpRequest", 4.2, {
            "method": "POST", "url": URL + "/api/replies", "sendHeaders": True,
            "headerParameters": {"parameters": [{"name": "X-API-Key", "value": TOKEN}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.reply) }}",
            "options": {}}, (480, 0)),
        node("Mark reply as read", "n8n-nodes-base.gmail", 2.1, {
            "operation": "markAsRead", "messageId": "={{ $('Reply fields').item.json.gmail_id }}"}, (720, 0)),
    ]
    w5 = wf("Invoice Desk - 5. Read replies", nodes,
            link("Replies in AP mailbox", "Reply fields", "Give reply to the agent", "Mark reply as read"),
            "Replies carrying [Ref <id>] go back to the agent, which reads them and re-checks the invoice.")

    for i, w in enumerate((w1, w2, w3, w4, w5), 1):
        slug = w["name"].split(". ", 1)[1].lower().replace(" ", "_")
        (OUT / f"{i}_{slug}.json").write_text(json.dumps(w, indent=2))
        print("wrote", OUT / f"{i}_{slug}.json")


if __name__ == "__main__":
    build()
