"""Connections to the outside world, designed to be driven by n8n (or any workflow tool).

  IN   POST /api/intake            n8n posts each PDF attachment from the AP mailbox here
  OUT  N8N_WEBHOOK_URL             the app posts every decision (and every human resolution) to n8n,
                                   which routes it to Slack / email
  OUT  signed one-click links      approve / reject from Slack or email without logging in
  IN   POST /api/escalations/run   n8n's hourly timer asks "which questions are now overdue?"

The decision logic never lives in n8n: n8n moves messages, the app decides. Everything that crosses
the boundary is written to an activity log, so the demo shows the flow even when no n8n is connected.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone

import httpx

from . import store
from .config import APPROVER
from .utils import fmt_inr

LINK_TTL_DAYS = 7
SENSITIVE = {"BANK_CHANGE", "IMPERSONATION", "FRAUD_HIGH"}


# ------------------------------------------------------------------------------------------- config
def public_url() -> str:
    return os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/")


def webhook_url() -> str | None:
    return os.getenv("N8N_WEBHOOK_URL") or None


def intake_token() -> str | None:
    return os.getenv("INTAKE_TOKEN") or None


def _secret() -> bytes:
    s = os.getenv("APP_SECRET")
    if s:
        return s.encode()
    f = store.RUNTIME / "app_secret"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip().encode()


def status() -> dict:
    return dict(public_url=public_url(), webhook=bool(webhook_url()), intake_token=bool(intake_token()),
                webhook_target=(webhook_url() or "")[:60])


# ------------------------------------------------------------------------------------------- signed links
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(run_id: str, action: str, who: str, ttl_days: int = LINK_TTL_DAYS) -> str:
    exp = int((datetime.now(timezone.utc) + timedelta(days=ttl_days)).timestamp())
    body = _b64(json.dumps(dict(r=run_id, a=action, w=who, e=exp), separators=(",", ":")).encode())
    mac = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()[:18])
    return f"{body}.{mac}"


def verify(token: str) -> dict:
    try:
        body, mac = token.split(".", 1)
    except ValueError:
        raise ValueError("This link is malformed.")
    good = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()[:18])
    if not hmac.compare_digest(good, mac):
        raise ValueError("This link is not valid. It may have been altered.")
    data = json.loads(_unb64(body))
    if data["e"] < datetime.now(timezone.utc).timestamp():
        raise ValueError("This link has expired. Open the invoice in Invoice Desk instead.")
    if data["a"] not in ("approve", "reject"):
        raise ValueError("Unknown action.")
    return data


def action_links(run_id: str, who: str) -> dict:
    return dict(approve=f"{public_url()}/a/{sign(run_id, 'approve', who)}",
                reject=f"{public_url()}/a/{sign(run_id, 'reject', who)}")


# ------------------------------------------------------------------------------------------- summary
def summary(run: dict) -> dict:
    """Compact, message-ready view of a run - what n8n puts into Slack / email."""
    res = run.get("result") or {}
    inv = res.get("invoice") or {}
    out = dict(
        run_id=run["id"], state=run["state"], decision=res.get("decision"), headline=res.get("headline"),
        vendor=inv.get("vendor") or run.get("vendor"), invoice_number=inv.get("invoice_number"),
        total=inv.get("total"), total_text=fmt_inr(inv.get("total")), due_date=inv.get("due_date"),
        po_number=inv.get("po_number"), risk=run.get("risk"), next_action=res.get("next_action"),
        url=f"{public_url()}/#run={run['id']}", channel=run.get("channel"), resolution=run.get("resolution"),
        handoff=[])
    waiting = not run.get("resolution") and (res.get("decision") == "REVIEW" or bool(res.get("awaiting_human")))
    for h in res.get("handoff") or []:
        item = dict(category=h["category"], owner=h["owner"], question=h["question"], due_by=h["due_by"],
                    escalate_to=h["escalate_to"], severity=h["severity"], code=h["code"],
                    needs_verification=h["code"] in SENSITIVE)
        if waiting:
            item.update(action_links(run["id"], h["owner"]))
        out["handoff"].append(item)
    out["awaiting_human"] = waiting
    if waiting and res.get("decision") == "APPROVE":  # agent recommends approval: the approver gets the buttons
        out["approval"] = dict(owner=APPROVER, **action_links(run["id"], APPROVER))
    # one plain-text message n8n can send as-is (email body / Slack fallback)
    word = "Waiting for your OK (agent recommends approval)" if out.get("approval") else _word(out["decision"])
    lines = [f"{word}: {out['vendor']} - invoice {out['invoice_number'] or '?'} for {out['total_text']}",
             out["headline"] or ""]
    for h in out["handoff"]:
        lines += ["", f"[{h['category']}] for {h['owner']}", f"Question: {h['question']}",
                  f"Answer by {h['due_by'][:16].replace('T', ' ')} UTC, then it escalates to {h['escalate_to']}."]
        if h.get("approve"):
            lines += [f"Approve: {h['approve']}", f"Reject:  {h['reject']}"]
    if out.get("approval"):
        lines += ["", f"Approve: {out['approval']['approve']}", f"Reject:  {out['approval']['reject']}"]
    lines += ["", f"Open in Invoice Desk: {out['url']}"]
    out["text"] = "\n".join(lines)
    return out


def _word(d):
    return {"APPROVE": "Approved", "REVIEW": "On hold", "REJECT": "Rejected"}.get(d, "Processed")


# ------------------------------------------------------------------------------------------- outbound webhook
def _post(event: str, payload: dict, run_id: str | None):
    url = webhook_url()
    if not url:
        store.add_event(run_id, "notify", f"{event}: no workflow connected - logged only", dict(event=event))
        return
    try:
        r = httpx.post(url, json=dict(event=event, **payload), timeout=8)
        ok = r.status_code < 300
        store.add_event(run_id, "notify", f"{event}: sent to n8n ({r.status_code})" if ok else
                        f"{event}: n8n answered {r.status_code}", dict(event=event, status=r.status_code, ok=ok))
    except Exception as e:  # never let a notification failure break invoice processing
        store.add_event(run_id, "notify", f"{event}: could not reach n8n ({type(e).__name__})",
                        dict(event=event, ok=False))


def emit(event: str, run: dict, wait: bool = False):
    payload = summary(run)
    t = threading.Thread(target=_post, args=(event, payload, run["id"]), daemon=True)
    t.start()
    if wait:
        t.join(10)


# ------------------------------------------------------------------------------------------- escalations
def run_escalations(as_of: datetime | None = None) -> list[dict]:
    """Return handoffs that are past their deadline and not escalated yet, and mark them escalated."""
    now = as_of or datetime.now(timezone.utc)
    done = {(e["run_id"], (e["data"] or {}).get("code")) for e in store.events(kind="escalated", limit=5000)}
    out = []
    for r in store.list_runs(500):
        if r["decision"] != "REVIEW" or r["resolution"]:
            continue
        run = store.get_run(r["id"])
        for h in (run.get("result") or {}).get("handoff") or []:
            due = datetime.fromisoformat(h["due_by"])
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if due > now or (r["id"], h["code"]) in done:
                continue
            hours = round((now - due).total_seconds() / 3600, 1)
            item = dict(run_id=r["id"], vendor=r["vendor"], invoice_number=r["invoice_number"], total=r["total"],
                        total_text=fmt_inr(r["total"]), category=h["category"], owner=h["owner"],
                        escalate_to=h["escalate_to"], question=h["question"], due_by=h["due_by"],
                        overdue_hours=hours, url=f"{public_url()}/#run={r['id']}",
                        **action_links(r["id"], h["escalate_to"]))
            item["text"] = (f"ESCALATED to {h['escalate_to']}: {h['owner']} has not answered for {hours} h.\n"
                            f"{r['vendor']} - invoice {r['invoice_number']} for {fmt_inr(r['total'])}\n"
                            f"[{h['category']}] {h['question']}\n\nApprove: {item['approve']}\nReject:  {item['reject']}\n"
                            f"Open: {item['url']}")
            store.add_event(r["id"], "escalated", f"{h['category']}: no answer from {h['owner']} - escalated to "
                                                  f"{h['escalate_to']} ({hours} h overdue)", dict(code=h["code"]))
            out.append(item)
    return out


def emit_message(run: dict, m: dict):
    """An outbound agent message: n8n sends it by email (vendor: address on file; internal: routing table)."""
    payload = dict(run_id=run["id"], url=f"{public_url()}/#run={run['id']}", message=dict(
        to=m["to"], party=m["party"], subject=m["subject"], body=m["body"], code=m["code"]),
        vendor=(run.get("result") or {}).get("invoice", {}).get("vendor"), decision=None, handoff=[])
    threading.Thread(target=_post, args=("agent.message", payload, run["id"]), daemon=True).start()
