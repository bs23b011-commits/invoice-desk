"""Every invoice has one discussion, like comments on a shared document.

The timeline brings together everything that happened to the invoice: when it arrived, the agent's emails and
the replies, re-checks, the final decision, and the team's own comments. People @mention a team (it gets an
email it can answer, and the answer comes back through the agent) or @Jeen (Jeen answers in the thread, with
sources). Jeen can explain and draft here, never approve or pay.
"""
from __future__ import annotations

import re

from . import agent, integrations, jeen, store
from .utils import fmt_inr

TEAMS = {  # @handle -> who it reaches (the same owners the playbook hands problems to)
    "stores": "Stores / receiving",
    "procurement": "Procurement (PO owner)",
    "ap": "AP lead",
    "controller": "Finance controller",
    "manager": "AP manager",
    "vendor": "vendor",
}
JEEN = "jeen"
MENTION = re.compile(r"(?<![\w.])@([A-Za-z]+)")
MAX_LEN = 2000


def handles() -> list[dict]:
    """For the composer's @ suggestions."""
    return [dict(handle=JEEN, name="Jeen", note="AP assistant: answers here, with sources")] + \
        [dict(handle=h, name=("Vendor (address on file)" if t == "vendor" else t),
              note="gets an email; their reply comes back here") for h, t in TEAMS.items()]


def _fraud(run: dict) -> bool:
    codes = {h["code"] for h in ((run.get("result") or {}).get("handoff") or [])}
    return bool(codes & agent.NEVER_CONTACT_VENDOR) or ((run.get("result") or {}).get("decision") == "REJECT"
                                                        and "IMPERSONATION" in codes)


def _vendor(run: dict) -> dict | None:
    inv = (run.get("result") or {}).get("invoice") or {}
    return store.VENDORS_BY_ID.get(inv.get("vendor_id"))


def _email(run: dict, author: str, team: str, text: str) -> dict:
    """Pass a person's question to a team or the vendor, through the agent's own outbound path."""
    inv = (run.get("result") or {}).get("invoice") or {}
    v = _vendor(run)
    party = "vendor" if team == "vendor" else f"internal:{team}"
    to = v["email"] if team == "vendor" else team
    number = inv.get("invoice_number") or run.get("filename")
    subject = f"Question about invoice {number} from {author} [Ref {run['id']}]"
    body = (f"Dear {v['name'] if team == 'vendor' else team},\n\n{author} asks about invoice {number}"
            f" ({inv.get('vendor') or 'vendor'}, {fmt_inr(inv.get('total'))}):\n\n{text}\n\n"
            f"Please keep [Ref {run['id']}] in the subject when you reply.\n\nRegards,\n{agent.COMPANY}")
    m = agent._msg("out", party, to, subject, body, code="MENTION",
                   status="sent" if integrations.webhook_url() else "logged")
    run.setdefault("thread", []).append(m)
    store.save_run(run)
    store.add_event(run["id"], "agent", f"{author} asked {to} via @mention: {subject}", dict(code="MENTION", party=party))
    integrations.emit_message(run, m)
    return m


def post(run_id: str, author: str, text: str) -> list[dict]:
    """Add a comment. @team sends it to them; @jeen gets Jeen's answer in the thread. Returns the new items."""
    run = store.get_run(run_id)
    if not run:
        raise ValueError("No such invoice")
    text = (text or "").strip()
    if not text:
        raise ValueError("Write something first")
    if len(text) > MAX_LEN:
        raise ValueError(f"Keep a comment under {MAX_LEN} characters")
    author = (author or "AP reviewer").strip()[:60] or "AP reviewer"
    mentioned = []
    for h in MENTION.findall(text):
        h = h.lower()
        if (h in TEAMS or h == JEEN) and h not in mentioned:
            mentioned.append(h)
    new = [store.add_comment(run_id, author, "comment", text, dict(mentions=mentioned))]
    for h in mentioned:
        if h == JEEN:
            continue
        team = TEAMS[h]
        if team == "vendor" and (_fraud(run) or not _vendor(run)):
            why = ("this invoice shows signs of fraud, and the agent never contacts a vendor through a suspicious "
                   "invoice. Call them on the number in the vendor master" if _fraud(run)
                   else "this vendor isn't in the vendor master, so there is no trusted address to write to")
            new.append(store.add_comment(run_id, "Invoice Desk", "note", f"Not sent to the vendor: {why}.",
                                         dict(blocked=True)))
            continue
        m = _email(run, author, team, MENTION.sub(lambda x: x.group(1).title() if x.group(1).lower() in TEAMS else x.group(0), text))
        run = store.get_run(run_id)
        new.append(store.add_comment(run_id, "Invoice Desk", "note",
                                     f"Emailed to {m['to']}" + ("" if m["status"] == "sent" else
                                                                " (logged: n8n isn't connected, so no email left)")
                                     + ". Their reply will appear here, and the agent re-checks the invoice.",
                                     dict(message_id=m["id"], to=m["to"])))
    if JEEN in mentioned:
        q = MENTION.sub(lambda x: "" if x.group(1).lower() == JEEN else x.group(0), text).strip(" ,:-") or \
            "What's the situation with this invoice?"
        history = [dict(role="assistant" if c["kind"] == "jeen" else "user", content=c["text"])
                   for c in store.comments(run_id)[-7:-1] if c["kind"] in ("jeen", "comment")]
        ans = jeen.chat(history + [dict(role="user", content=q)], dict(view="waiting", run_id=run_id))
        new.append(store.add_comment(run_id, "Jeen", "jeen", ans["answer"],
                                     dict(sources=ans.get("sources", []), actions=ans.get("actions", []),
                                          mode=ans.get("mode"), question=q)))
    store.add_event(run_id, "comment", f"{author} commented" + (f" (@{', @'.join(mentioned)})" if mentioned else ""),
                    dict(mentions=mentioned))
    return new


def summary(run: dict) -> str:
    """Jeen's pinned note at the top: what this invoice is and what's needed, in two sentences."""
    from . import cockpit
    if run.get("state") == "error":
        return f"I couldn't read this file: {run.get('error')}"
    if not run.get("result"):
        return "I'm still checking this invoice."
    note = cockpit.card(run)["note"]
    tip = " Ask me anything here: type @Jeen and your question." if not run.get("resolution") else ""
    if run.get("resolution"):
        r = run["resolution"]
        note = f"Decided: {'approved' if r['action'] == 'APPROVE' else 'rejected'} by {r['by']}. " + note
    return note + tip


def timeline(run_id: str) -> dict:
    """Everything that happened to the invoice, oldest first, plus Jeen's pinned summary."""
    run = store.get_run(run_id)
    if not run:
        raise ValueError("No such invoice")
    ch = run.get("channel") or {}
    items = [dict(kind="event", at=run.get("created"), author="Invoice Desk",
                  text=f"Received {run.get('filename')} by {(ch.get('channel') or 'upload').replace('_', ' ')}"
                       + (f" from {ch['sender']}" if ch.get("sender") else ""))]
    for m in run.get("thread") or []:
        if m.get("code") == "MENTION":
            continue  # shown as the person's comment + the "Emailed to" note
        if m["direction"] == "out":
            items.append(dict(kind="email_out", at=m["at"], author="Agent", to=m.get("to"), subject=m.get("subject"),
                              text=agent.strip_quoted(m.get("body") or ""), status=m.get("status")))
        else:
            p = m.get("parsed") or {}
            items.append(dict(kind="email_in", at=m["at"], author=m.get("sender") or m["party"].replace("internal:", ""),
                              subject=m.get("subject"), text=agent.strip_quoted(m.get("body") or ""),
                              understood=p.get("intent"),
                              outcome=(m.get("outcome") or {}).get("text") if isinstance(m.get("outcome"), dict)
                              else m.get("outcome")))
    for r in run.get("revisions") or []:
        items.append(dict(kind="event", at=r.get("at"), author="Agent", text=f"Re-checked everything: {r.get('reason')}"))
    if run.get("resolution"):
        r = run["resolution"]
        items.append(dict(kind="decision", at=r.get("at"), author=r.get("by"),
                          text=f"{'Approved' if r['action'] == 'APPROVE' else 'Rejected'} via {r.get('via')}"
                               + (f": “{r['note']}”" if r.get("note") else "")))
    for c in store.comments(run_id):
        items.append(dict(kind=c["kind"], at=c["at"], author=c["author"], text=c["text"], id=c["id"], **c["payload"]))
    # same-second order: the arrival first, then emails out, replies in, the re-check they caused, comments, decision
    rank = dict(email_out=1, email_in=2, event=3, comment=4, note=4, jeen=4, decision=5)
    items = [dict(x, _i=i, _r=0 if i == 0 else rank.get(x["kind"], 4)) for i, x in enumerate(items)]
    items.sort(key=lambda x: (x.get("at") or "", x["_r"], x["_i"]))
    for x in items:
        x.pop("_i"), x.pop("_r")
    return dict(run_id=run_id, pinned=dict(author="Jeen", text=summary(run)), items=items, handles=handles())
