"""Stage 8: turn findings into one decision a non-technical AP manager can act on.

Policy (deliberately simple and auditable):
  any FAIL  -> REJECT   (must not be paid: duplicate, blocked vendor, vendor's own arithmetic is wrong)
  any WARN  -> REVIEW   (a human decides; each problem becomes a handoff: owner, one question, deadline)
  otherwise -> APPROVE  (post and schedule payment on the due date)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from . import playbook
from .config import APPROVER, human_approval, today
from .utils import fmt_inr

def build_handoff(items: list[tuple[str, dict]], ctx: dict) -> list[dict]:
    """Group fail/warn findings by playbook category -> one handoff per category."""
    groups: dict[str, dict] = {}
    now = datetime.now(timezone.utc)
    for stage, f in items:
        pb = playbook.entry(f.get("code"))
        cat = pb["category"]
        g = groups.get(cat)
        if not g:
            facts = playbook.fmt_money_facts(f.get("facts"))
            g = groups[cat] = dict(
                code=f.get("code") or "OTHER", category=cat, owner=pb["owner"], severity=f["status"], stage=stage,
                question=pb["q"](facts, ctx), evidence=[], tried=[], sla_hours=pb["sla_h"],
                escalate_to=pb["escalate"],
                due_by=(now + timedelta(hours=pb["sla_h"])).isoformat(timespec="minutes"))
        if f["status"] == "fail":
            g["severity"] = "fail"
        if f["text"] not in g["evidence"]:
            g["evidence"].append(f["text"])
        for t in f.get("tried") or []:
            if t not in g["tried"]:
                g["tried"].append(t)
    # A duplicate re-bills a PO that the original already used up: the PO warnings are a consequence, not a
    # second problem. Same for a frozen high-risk invoice.
    codes = {g["code"] for g in groups.values()}
    if codes & {"DUPLICATE", "FRAUD_HIGH"}:
        groups = {k: g for k, g in groups.items() if not g["code"].startswith(("PO_", "IRN_", "GRN_"))}
    order = {"fail": 0, "warn": 1}
    return sorted(groups.values(), key=lambda g: (order[g["severity"]], g["sla_hours"]))


def _headline(prefix: str, handoff: list[dict]) -> str:
    top = handoff[0]
    ev = top["evidence"][0]
    if ev.lower().startswith(top["category"].lower()):
        ev = ev[len(top["category"]):].lstrip(" .:-")
    more = f" (+{len(handoff) - 1} more)" if len(handoff) > 1 else ""
    return f"{prefix}: {top['category']}{more}. {ev[0].upper() + ev[1:] if ev else ''}"


def decide(stage_findings: dict[str, list[dict]], ctx: dict) -> dict:
    fails = [(s, f) for s, fs in stage_findings.items() for f in fs if f["status"] == "fail"]
    warns = [(s, f) for s, fs in stage_findings.items() for f in fs if f["status"] == "warn"]
    infos = [(s, f) for s, fs in stage_findings.items() for f in fs if f["status"] == "info"]
    vendor = ctx.get("vendor")
    vname = vendor["name"] if vendor else (ctx.get("vendor_name") or "Unknown vendor")
    ctx = dict(ctx, vendor_name=vname)
    total = ctx.get("total")
    due = ctx.get("due_date")
    handoff = build_handoff(fails + warns, ctx)

    email = None
    if fails:
        decision = "REJECT"
        top = handoff[0]
        headline = _headline("Rejected", handoff)
        next_action = "Do not pay. Nothing is posted to the ledger. " + (
            "Payments to this account are frozen pending verification." if top["code"] == "FRAUD_HIGH"
            else "Send the drafted note to the vendor.")
        route = " + ".join(dict.fromkeys(h["owner"] for h in handoff))
        if top["code"] != "FRAUD_HIGH":  # never tip off a suspected fraudster with a detailed note
            email = _vendor_email(ctx, "reject", [f["text"] for _, f in fails])
    elif warns:
        decision = "REVIEW"
        top = handoff[0]
        headline = _headline("Needs review", handoff)
        route = " + ".join(dict.fromkeys(h["owner"] for h in handoff))
        next_action = (f"Hold payment. {len(handoff)} question{'s' if len(handoff) > 1 else ''} sent to {route}. "
                       "Approve or reject once answered.")
        po_items = [f["text"] for s, f in warns if s == "po" and (f.get("code") or "").startswith("PO_")]
        if po_items and not any(h["code"] in ("BANK_CHANGE", "IMPERSONATION") for h in handoff):
            email = _vendor_email(ctx, "review_po", po_items)
    else:
        decision = "APPROVE"
        due_txt = date.fromisoformat(due).strftime("%d %b %Y") if due else "on terms"
        overdue = bool(due) and date.fromisoformat(due) < today()
        when = f"in the next payment run (was due {due_txt})" if overdue else f"on {due_txt}"
        tds = ctx.get("tds") or 0
        pay = round((total or 0) - tds, 2)
        less = f" ({fmt_inr(total)} less TDS {fmt_inr(tds)})" if tds else ""
        if human_approval():
            headline = f"Recommend approval: pay {fmt_inr(pay)} to {vname}{less} {when}"
            route = f"Waiting for {APPROVER}'s OK"
            next_action = (f"Every check passed. Nothing is posted or paid until a person approves. If approved: posted "
                           f"against {ctx.get('po_number')}, {fmt_inr(pay)} paid {when}"
                           + (f" to {vendor['bank']} (account on file)." if vendor else "."))
            return _out(decision, headline, route, next_action, handoff, fails, warns, infos, ctx, email, total, vendor, due)
        headline = f"Approved: pay {fmt_inr(pay)} to {vname}{less} {when}"
        route = "Auto-posted, payment scheduled"
        next_action = f"Posted against {ctx.get('po_number')}. Payment of {fmt_inr(pay)} scheduled {when}" + \
                      (f" to {vendor['bank']} (account on file)." if vendor else ".") + \
                      (f" TDS of {fmt_inr(tds)} is deposited with the government by the 7th of next month." if tds else "")

    return _out(decision, headline, route, next_action, handoff, fails, warns, infos, ctx, email, total, vendor, due)


def _out(decision, headline, route, next_action, handoff, fails, warns, infos, ctx, email, total, vendor, due):
    conf = ctx.get("confidence", 1.0)
    return dict(decision=decision, headline=headline, route_to=route, next_action=next_action,
                handoff=handoff,
                reasons=[dict(stage=s, **f) for s, f in fails + warns],
                notes=[dict(stage=s, **f) for s, f in infos],
                confidence=round(conf, 2), vendor_email=email,
                payment=dict(amount=round((total or 0) - (ctx.get("tds") or 0), 2), gross=total, tds=ctx.get("tds") or 0,
                             due_date=due, bank=vendor["bank"] if vendor else None)
                if decision == "APPROVE" else None,
                # the agent only recommends; a person makes the final call (config.human_approval)
                awaiting_human=human_approval())


def _vendor_email(ctx, kind, points):
    v = ctx.get("vendor")
    to = v["email"] if v else "(vendor email unknown)"
    num = ctx.get("invoice_number") or "(no number)"
    bullet = "\n".join(f"  - {p}" for p in points)
    if kind == "reject":
        subject = f"Invoice {num} - not processed"
        body = (f"Dear {v['name'] if v else 'Sir/Madam'},\n\nWe could not process your invoice {num} dated "
                f"{ctx.get('invoice_date') or '-'} for {fmt_inr(ctx.get('total'))} for the following reason(s):\n"
                f"{bullet}\n\nPlease review and, if needed, send a corrected invoice or the missing documents. "
                f"No payment will be made against this copy.\n\nRegards,\nAccounts Payable, Meridian Foods Pvt Ltd")
    else:
        subject = f"Invoice {num} - query before payment"
        body = (f"Dear {v['name'] if v else 'Sir/Madam'},\n\nYour invoice {num} for {fmt_inr(ctx.get('total'))} "
                f"is on hold while we check it against our purchase order:\n{bullet}\n\nPlease confirm the PO "
                f"reference and quantities, or send a revised invoice / credit note for the difference. Please "
                f"quote our PO number on all future invoices.\n\nRegards,\nAccounts Payable, Meridian Foods Pvt Ltd")
    return dict(to=to, subject=subject, body=body, note="Draft only - sent after a human confirms")
