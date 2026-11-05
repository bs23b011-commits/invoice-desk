"""The "Waiting for you" screen: one card per invoice that needs a person's final call.

Everything here is a plain-English view of what the pipeline already recorded: the agent's recommendation
and why, the 3-way match, the fraud score, who can resolve each problem, and the conversation so far.
"""
from __future__ import annotations

from datetime import date

from . import agent, config, store
from .integrations import SENSITIVE
from .utils import fmt_inr

CHECKS = [("validate", "Maths"), ("vendor", "Vendor"), ("duplicate", "Not a duplicate"),
          ("po", "3-way match"), ("risk", "Fraud risk"), ("tax", "Tax rules")]
PREFIX = ("Recommend approval: ", "Needs review: ", "Rejected: ", "Approved: ")


def _n(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _day(iso):
    try:
        return date.fromisoformat(iso[:10]).strftime("%d %b")
    except (TypeError, ValueError):
        return None


def needs_person(run: dict) -> bool:
    res = run.get("result") or {}
    if run.get("state") == "error":  # a file nobody could read must not disappear
        return not run.get("resolution")
    return (run.get("state") == "done" and not run.get("resolution")
            and (res.get("decision") == "REVIEW" or bool(res.get("awaiting_human"))))


def _note(run: dict) -> str:
    res = run["result"]
    text = res.get("headline") or ""
    for p in PREFIX:
        text = text.removeprefix(p)
    if res["decision"] == "APPROVE":
        why = ""
        if run.get("revisions"):
            why = f" I re-checked after: {run['revisions'][-1].get('reason', 'new information')}."
        return f"Every check passed.{why} If you approve, I'll post it and {text}. Nothing moves until you do."
    return f"{text[:1].upper()}{text[1:]}. {res.get('next_action') or ''}".strip()


def _checks(run: dict) -> list[dict]:
    stages = {s["key"]: s for s in run.get("stages") or []}
    out = []
    for key, label in CHECKS:
        s = stages.get(key)
        if not s:
            continue
        status = s.get("status", "info")
        fs = s.get("findings") or []
        pick = next((f for f in fs if f["status"] == status), None) or next(iter(fs), {"text": ""})
        if key == "risk":
            label = f"Fraud risk {(s.get('data') or {}).get('score', 0)}/100"
        out.append(dict(key=key, label=label, status=status, ok=status in ("pass", "info"), detail=pick["text"]))
    return out


def _match(run: dict) -> dict:
    s = next((x for x in run.get("stages") or [] if x["key"] == "po"), {}) or {}
    po = ((s.get("data") or {}).get("po")) or {}
    lines = []
    for l in po.get("lines") or []:
        service = bool(l.get("service"))
        price_ok = not (l.get("rate") and l.get("po_rate")) or l["rate"] <= l["po_rate"] * (1 + config.TOLERANCE_PCT / 100) + 0.005
        lines.append(dict(item=l.get("invoice") or l.get("po_line"), ordered=_n(l.get("po_qty")),
                          received=None if service else _n(l.get("received")), billed=_n(l.get("qty")),
                          service=service, price_ok=price_ok, ok=l.get("status") == "pass", note=l.get("note")))
    return dict(po_number=po.get("po_number"), how=po.get("how"), lines=lines)


def _owner(run: dict) -> dict:
    res = run["result"]
    hs = res.get("handoff") or []
    if not hs:
        return dict(name=config.APPROVER, sub="Gives the final OK", question=None, due=None, escalate_to=None)
    h = hs[0]
    return dict(name=h["owner"], sub=h["category"], question=h["question"], due=h.get("due_by"),
                escalate_to=h.get("escalate_to"), more=len(hs) - 1)


def _impact(res: dict) -> str:
    inv = res.get("invoice") or {}
    pay = res.get("payment")
    if res["decision"] == "APPROVE" and pay:
        when = _day(pay.get("due_date")) or "on terms"
        tds = f", after TDS {fmt_inr(pay['tds'])}" if pay.get("tds") else ""
        return f"Posted to the ERP and {fmt_inr(pay['amount'])} paid on {when} to {pay.get('bank') or 'the account on file'}{tds}"
    if res["decision"] == "REJECT":
        return "The agent says do not pay. Approving anyway needs a written reason."
    return f"{fmt_inr(inv.get('net_payable') or inv.get('total'))} would be posted and scheduled for {_day(inv.get('due_date')) or 'its due date'}"


def unreadable_card(run: dict) -> dict:
    ch = run.get("channel") or {}
    return dict(id=run["id"], recommendation="UNREADABLE", vendor=ch.get("sender") or run.get("filename"),
                invoice_number=run.get("filename"), total=None, total_text="—", due=None, due_date=None,
                channel=(ch.get("channel") or "upload").replace("_", " "), created=run.get("created"), duration=None,
                note=run.get("error") or "This file couldn't be read.", checks=[], match=dict(po_number=None, how=None, lines=[]),
                risk=dict(score=0, level=None, signals=[]),
                owner=dict(name="AP reviewer", sub="Ask the sender for a readable copy", question=None, due=None,
                           escalate_to=None), thread=[], impact="Nothing can be posted or paid until a readable copy arrives.",
                needs_phone_check=False, msme=False, stages_done=0, reply_examples=[], onboard=None)


def card(run: dict) -> dict:
    if run.get("state") == "error":
        return unreadable_card(run)
    res = run["result"]
    inv = res.get("invoice") or {}
    risk = next((s.get("data") or {} for s in run.get("stages") or [] if s["key"] == "risk"), {})
    codes = {h["code"] for h in res.get("handoff") or []}
    thread = [dict(direction=m["direction"], who=("Agent → " + m["to"]) if m["direction"] == "out"
                   else (m.get("sender") or m["party"].replace("internal:", "")),
                   at=m.get("at"), subject=m.get("subject"), body=agent.strip_quoted(m.get("body") or "")[:400],
                   understood=(m.get("parsed") or {}).get("intent"),
                   outcome=(m.get("outcome") or {}).get("text") if isinstance(m.get("outcome"), dict) else m.get("outcome"))
              for m in run.get("thread") or []]
    return dict(
        id=run["id"], recommendation=res["decision"], vendor=inv.get("vendor") or run.get("vendor"),
        invoice_number=inv.get("invoice_number"), total=inv.get("total"), total_text=fmt_inr(inv.get("total")),
        due=_day(inv.get("due_date")), due_date=inv.get("due_date"),
        channel=((run.get("channel") or {}).get("channel") or "upload").replace("_", " "),
        created=run.get("created"), duration=run.get("duration"), note=_note(run), checks=_checks(run),
        match=_match(run), risk=dict(score=risk.get("score", 0), level=risk.get("level"),
                                     signals=[s["label"] for s in risk.get("signals") or []]),
        owner=_owner(run), thread=thread, impact=_impact(res), needs_phone_check=bool(codes & SENSITIVE),
        msme=bool(inv.get("msme_deadline")), stages_done=sum(1 for s in run.get("stages") or [] if s.get("status")),
        reply_examples=agent.simulate_examples(run) if res["decision"] == "REVIEW" else [],
        onboard=dict(name=inv.get("vendor_name") or "", gstin=inv.get("vendor_gstin") or "",
                     bank=inv.get("bank") or "") if "VENDOR_UNKNOWN" in codes else None)


def waiting() -> dict:
    items = []
    for r in store.list_runs(500):
        run = store.get_run(r["id"])
        if run and needs_person(run):
            items.append(card(run))
    order = {"UNREADABLE": 0, "REJECT": 0, "REVIEW": 1, "APPROVE": 2}
    items.sort(key=lambda c: (not c["msme"], order.get(c["recommendation"], 3), c["due_date"] or "9999"))
    return dict(items=items, total=sum(c["total"] or 0 for c in items), total_text=fmt_inr(sum(c["total"] or 0 for c in items)),
                human_approval=config.human_approval(), approver=config.APPROVER)
