"""Step 3: the agent acts on exceptions instead of only reporting them.

For each open question the playbook decides whether the agent may contact someone by itself:
  vendor-fixable problems  -> email the vendor at the address ON FILE (never one printed on the invoice)
  internal facts           -> message the internal team that knows the answer (stores, procurement)
  suspected fraud          -> never contact the vendor; internal only
Every message carries a reference tag "[Ref <run id>]". Replies come back through POST /api/replies (n8n
reads the mailbox) or the "simulate a reply" box in the UI. The agent reads the reply, extracts facts
(rules first, optional LLM for unclear replies), and re-runs the whole decision with those facts. A reply
never bypasses a check: it only supplies a missing fact, and the same rules decide again.
"""
from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timezone

from . import extract_llm, integrations, store
from .utils import fmt_inr, norm_po

REF_RE = re.compile(r"\[Ref\s+([0-9]{6}-[0-9]{6}-[0-9a-f]{4})\]", re.I)
COMPANY = "Accounts Payable, Meridian Foods Pvt Ltd"

# code -> who the agent contacts automatically, and what it asks
ACTIONS = {
    "PO_MISSING": ("vendor", "Please confirm the purchase order for invoice {inv}",
                   "Your invoice {inv} for {total} does not quote our purchase order number. Our best match is {po}. "
                   "Could you confirm the PO number this invoice belongs to? Please quote it on future invoices."),
    "PO_NOT_FOUND": ("vendor", "Purchase order needed for invoice {inv}",
                     "Your invoice {inv} for {total} does not quote a purchase order, and we could not match it to one. "
                     "Please reply with the PO number, or the name of the person at Meridian Foods who ordered it."),
    "PO_INVALID": ("vendor", "Purchase order on invoice {inv} not recognised",
                   "The PO number on your invoice {inv} does not match our records. Please reply with the correct PO number."),
    "BUYER_GSTIN": ("vendor", "Corrected invoice needed: {inv}",
                    "Our GSTIN is missing or incorrect on invoice {inv}. Please reissue it with GSTIN {buyer_gstin} so we "
                    "can claim input tax credit. We will process it as soon as the corrected copy arrives."),
    "TAX_TYPE": ("vendor", "Corrected invoice needed: {inv}",
                 "Invoice {inv} charges the wrong type of GST for our place of supply. Please reissue it with the correct "
                 "GST type (IGST for inter-state, CGST + SGST within the state)."),
    "GSTIN_MISSING": ("vendor", "GSTIN missing on invoice {inv}",
                      "Your GSTIN is missing on invoice {inv}. Please reissue the invoice with your GSTIN."),
    "IRN_MISSING": ("vendor", "E-invoice (IRN) needed for invoice {inv}",
                    "Invoice {inv} has no e-invoice IRN / QR code. As your turnover requires e-invoicing, please share the "
                    "e-invoice generated on the IRP so it is a valid tax invoice for us."),
    "VENDOR_BLOCKED": ("vendor", "Vendor re-verification pending",
                       "We cannot process invoice {inv} because your vendor verification (KYC) has lapsed. Please send the "
                       "updated documents to vendor-master@meridianfoods.example. We will process the invoice once complete."),
    "DUPLICATE": ("vendor", "Invoice {inv} already received",
                  "Invoice {inv} repeats {original}, which we have already processed. No further payment will be made "
                  "against this copy. If this is a different bill, please reply with the details."),
    "GRN_SHORT": ("internal:Stores / receiving", "Did the goods for {po} arrive?",
                  "{vendor} has invoiced {detail}. Our goods receipts show less. Has the rest arrived? Reply with the "
                  "quantity received (for example: 'received 100 more today'), or 'not received'."),
    "PO_QTY_OVER": ("internal:Procurement (PO owner)", "Over-billing on {po}: approve or ask for a credit note?",
                    "{vendor} invoice {inv} bills more than is left on {po}. {question} Reply 'approve' (with the reason) "
                    "or 'reject'."),
    "PO_BALANCE": ("internal:Procurement (PO owner)", "Over-billing on {po}: approve or ask for a credit note?",
                   "{vendor} invoice {inv} is above what is left on {po}. {question} Reply 'approve' or 'reject'."),
    "PO_PRICE": ("internal:Procurement (PO owner)", "Price above PO on {po}",
                 "{vendor} invoice {inv}: {question} Reply 'approve' (rate agreed) or 'reject' (we will ask for a credit note)."),
}
NEVER_CONTACT_VENDOR = {"BANK_CHANGE", "IMPERSONATION", "FRAUD_HIGH"}


# ------------------------------------------------------------------------------------------------ outbound
def _msg(direction, party, to, subject, body, code=None, status="sent", parsed=None, sender=None):
    return dict(id=uuid.uuid4().hex[:8], at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                direction=direction, party=party, to=to, sender=sender, subject=subject, body=body, code=code,
                status=status, parsed=parsed)


def act(run: dict) -> list[dict]:
    """After a decision: send the messages the playbook allows, once per (code, revision-independent)."""
    res = run.get("result") or {}
    if res.get("decision") not in ("REVIEW", "REJECT") or run.get("resolution"):
        return []
    thread = run.setdefault("thread", [])
    already = {m["code"] for m in thread if m["direction"] == "out"}
    inv = res.get("invoice") or {}
    vendor = next((v for v in store.VENDORS if v["id"] == inv.get("vendor_id")), None)
    codes = {h["code"] for h in res.get("handoff") or []}
    fraud = bool(codes & NEVER_CONTACT_VENDOR)
    sent = []
    for h in res.get("handoff") or []:
        code = h["code"]
        if code not in ACTIONS or code in already:
            continue
        party, subj, body = ACTIONS[code]
        if party == "vendor" and (fraud or not vendor):
            continue  # never write to a vendor through a suspicious invoice; unknown vendor has no address on file
        ctx = dict(inv=inv.get("invoice_number") or "(no number)", total=fmt_inr(inv.get("total")),
                   po=inv.get("po_number") or "your PO", vendor=inv.get("vendor"), question=h["question"],
                   buyer_gstin=store.BUYER["gstin"], original=(h.get("evidence") or [""])[0].split(" dated")[0]
                   .replace("Duplicate of invoice ", ""), detail=(h.get("evidence") or [""])[0])
        to = vendor["email"] if party == "vendor" else party.split(":", 1)[1]
        subject = f"{subj.format(**ctx)} [Ref {run['id']}]"
        text = (f"Dear {vendor['name'] if party == 'vendor' else party.split(':', 1)[1]},\n\n{body.format(**ctx)}\n\n"
                f"Please keep [Ref {run['id']}] in the subject when you reply.\n\nRegards,\n{COMPANY} (automated assistant)")
        m = _msg("out", party, to, subject, text, code, "sent" if integrations.webhook_url() else "logged")
        thread.append(m)
        sent.append(m)
        store.add_event(run["id"], "agent", f"Agent wrote to {to}: {subject}", dict(code=code, party=party))
        integrations.emit_message(run, m)
    return sent  # the caller saves the run


# ------------------------------------------------------------------------------------------------ inbound
NUM = r"(\d[\d,]*)"


_QUOTE_HEADER = re.compile(
    r"^\s*(On\b.{0,200}\bwrote:|-{2,}\s*Original Message\s*-{2,}|From:\s.+|_{5,}|Sent from my \w+)\s*$", re.I)


def strip_quoted(text: str) -> str:
    """Keep only what the person wrote: drop the quoted earlier message that email clients append.
    Without this, our own question ("... or 'not received'") would be read back as their answer."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    # Gmail may wrap "On ... wrote:" over two lines; join each line with the next before testing
    for i, ln in enumerate(lines):
        two = ln + " " + (lines[i + 1] if i + 1 < len(lines) else "")
        if _QUOTE_HEADER.match(ln) or (ln.strip().startswith("On ") and _QUOTE_HEADER.match(two)):
            lines = lines[:i]
            break
    kept = [ln for ln in lines if not ln.lstrip().startswith(">")]
    out = "\n".join(kept).strip()
    return out or (text or "").strip()  # never return nothing


def parse_reply(text: str, run: dict, party: str) -> dict:
    """Turn a free-text reply into one intent + facts. Rules first; LLM only if the rules find nothing."""
    t = " ".join(strip_quoted(text).split())
    low = t.lower()
    irn = re.search(r"\b([0-9a-fA-F]{64})\b", t)
    if irn:
        return dict(intent="provide_irn", irn=irn.group(1).lower(), how="rules")
    m = re.search(r"\bP\.?O\.?\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Z]{0,3}[-\s]?\d{4}\s*[-/]\s*\d{2,5})", t, re.I)
    if m:
        return dict(intent="provide_po", po=norm_po(m.group(1)), how="rules")
    if party.startswith("internal") and re.search(r"\b(not|haven'?t|has not|have not)\s+(been\s+)?(received|arrived)", low):
        return dict(intent="not_received", how="rules")
    m = re.search(rf"(?:received|arrived|got)\D{{0,20}}{NUM}(?:\s+(?:more|additional|units|nos|pcs|pieces|pairs))?", low) \
        or re.search(rf"{NUM}\s+(?:more\s+)?(?:\w+\s+){{0,3}}(?:received|arrived)", low)
    if party.startswith("internal") and m:
        return dict(intent="confirm_receipt", qty=float(m.group(1).replace(",", "")), how="rules",
                    full=bool(re.search(r"\b(all|full|complete|balance|rest)\b", low)))
    full_words = r"(all|fully|everything|balance|rest|remaining|in full)"
    if party.startswith("internal") and (re.search(rf"\b{full_words}\b.*\b(received|arrived|delivered)\b", low)
                                        or re.search(rf"\b(received|arrived|delivered)\b.*\b{full_words}\b", low)):
        return dict(intent="confirm_receipt", qty=None, full=True, how="rules")
    if re.search(r"credit\s*note", low):
        return dict(intent="credit_note", how="rules")
    if party.startswith("internal") and re.search(r"\b(approve|approved|ok to pay|go ahead|agreed|yes)\b", low):
        return dict(intent="approve", how="rules")
    if party.startswith("internal") and re.search(r"\b(reject|rejected|do not pay|don'?t pay|not ordered|no)\b", low):
        return dict(intent="reject", how="rules")
    if extract_llm.provider():
        try:
            return dict(extract_llm.classify_reply(t), how="llm")
        except Exception:
            pass
    return dict(intent="unclear", how="rules")


def receive_reply(run_id: str | None, sender: str, subject: str, body: str, party: str | None = None,
                  channel: str = "email") -> dict:
    """Record a reply, interpret it, and act. Returns what happened."""
    if not run_id:
        m = REF_RE.search(subject or "") or REF_RE.search(body or "")
        run_id = m.group(1) if m else None
    run = store.get_run(run_id) if run_id else None
    if not run:
        store.add_event(None, "skipped", f"Reply from {sender} could not be matched to an invoice", dict(subject=subject))
        return dict(status="unmatched")
    inv = (run.get("result") or {}).get("invoice") or {}
    vendor = next((v for v in store.VENDORS if v["id"] == inv.get("vendor_id")), None)
    if not party:
        s_low = (sender or "").lower()
        if vendor and s_low == vendor["email"].lower():
            party = "vendor"
        elif s_low.split("@")[-1] in internal_domains():
            party = "internal:" + sender
        else:  # a stranger replying to a payment thread must never be able to approve anything
            party = "unknown:" + (sender or "unknown")
    parsed = parse_reply(body, run, party)
    parsed["note"] = " ".join(strip_quoted(body).split())[:240]
    msg = _msg("in", party, None, subject, body, None, "received", parsed, sender)
    run.setdefault("thread", []).append(msg)
    store.save_run(run)
    store.add_event(run_id, "reply", f"Reply from {sender or party} via {channel}: {parsed['intent'].replace('_', ' ')}",
                    dict(intent=parsed["intent"], party=party))
    outcome = apply(run, parsed, party, sender)
    from . import pipeline
    live = pipeline.LIVE.get(run_id)  # if a re-check is running, write into the live copy it will save
    target = live or store.get_run(run_id)
    for m in target.get("thread") or []:
        if m["id"] == msg["id"]:
            m["outcome"] = outcome["text"]
    if not live:
        store.save_run(target)
    return dict(status="ok", run_id=run_id, parsed=parsed, outcome=outcome)


def internal_domains() -> set[str]:
    import os
    return {d.strip().lower() for d in os.getenv("INTERNAL_EMAIL_DOMAINS", "yourcompany.com,meridianfoods.example").split(",")
            if d.strip()}


def apply(run: dict, parsed: dict, party: str, sender: str | None) -> dict:
    from . import pipeline  # late import (pipeline imports agent)
    from .matching import po_consumption  # noqa: F401
    intent = parsed["intent"]
    who = (sender or party.split(":", 1)[-1]) + (" (vendor)" if party == "vendor" else "")
    if party.startswith("unknown"):
        store.add_event(run["id"], "agent", f"Reply from unrecognised sender {sender} ignored - flagged for AP",
                        dict(intent=intent))
        return dict(action="none", text=f"Sender {sender} is neither the vendor on file nor a colleague - "
                                        "recorded only, nothing changed, AP alerted")
    res = run.get("result") or {}
    if res.get("decision") != "REVIEW" or run.get("resolution"):
        return dict(action="none", text="Invoice is no longer on hold - reply recorded only")
    inv = res.get("invoice") or {}

    if intent == "provide_po":
        po = next((p for p in store.POS if p["po_number"] == parsed["po"]), None)
        if not po:
            return dict(action="none", text=f"{parsed['po']} does not exist - still on hold, AP will follow up")
        if po["vendor_id"] != inv.get("vendor_id"):
            return dict(action="none", text=f"{parsed['po']} belongs to another vendor - still on hold")
        pipeline.reprocess(run["id"], dict(po_number=parsed["po"], po_source=f"confirmed by {who}"),
                           f"{who} confirmed {parsed['po']}")
        return dict(action="reprocess", text=f"PO {parsed['po']} confirmed - re-checking the invoice against it")

    if intent == "provide_irn":
        pipeline.reprocess(run["id"], dict(irn=parsed["irn"], irn_source=f"e-invoice sent by {who}"),
                           f"{who} sent the e-invoice (IRN {parsed['irn'][:10]}…)")
        return dict(action="reprocess", text="E-invoice IRN received - checking it against the invoice")

    if intent == "confirm_receipt":
        lines = store.record_receipt_from_reply(inv.get("po_number"), parsed.get("qty"), parsed.get("full"),
                                                f"reply from {who}", run["id"])
        if not lines:
            return dict(action="none", text="Could not tell which PO line the receipt is for - AP will follow up")
        pipeline.reprocess(run["id"], {}, f"{who} confirmed goods received: " + "; ".join(lines))
        return dict(action="reprocess", text="Goods receipt recorded (" + "; ".join(lines) + ") - re-checking")

    if intent in ("approve", "reject") and party.startswith("internal"):
        note = parsed.get("note") or ""
        pipeline.resolve(run["id"], intent, note, who, "email reply")
        return dict(action=intent, text=f"{'Approved' if intent == 'approve' else 'Rejected'} by {who} (email reply)")

    if intent == "not_received":
        return dict(action="none", text="Goods not received yet - payment stays on hold until they arrive")
    if intent == "credit_note":
        return dict(action="none", text="Vendor will send a credit note - on hold until it arrives")
    store.add_event(run["id"], "agent", f"Reply on {inv.get('invoice_number')} needs a person to read it",
                    dict(intent="unclear"))
    return dict(action="none", text="Reply not understood automatically - flagged for the AP reviewer to read")


def simulate_examples(run: dict) -> list[dict]:
    """Ready-made replies for the demo, matching the questions this invoice actually has."""
    res = run.get("result") or {}
    inv = res.get("invoice") or {}
    out = []
    for m in run.get("thread") or []:
        if m["direction"] != "out":
            continue
        if m["code"] in ("PO_MISSING", "PO_NOT_FOUND", "PO_INVALID"):
            po = inv.get("po_number") or "PO-2026-0106"
            out.append(dict(party="vendor", label="Vendor confirms the PO",
                            text=f"Hello, sorry for missing it. This invoice is against PO {po}. Regards"))
        elif m["code"] == "IRN_MISSING":
            from .compliance import irn_for
            try:
                irn = irn_for(store.VENDORS_BY_ID[inv["vendor_id"]]["gstin"], inv["invoice_date"], inv["invoice_number"])
                out.append(dict(party="vendor", label="Vendor sends the e-invoice",
                                text=f"Apologies - please find the e-invoice details. IRN: {irn}"))
            except Exception:
                pass
        elif m["code"] == "GRN_SHORT":
            out.append(dict(party=m["party"], label="Stores: the rest arrived",
                            text="Checked with the warehouse - the balance was received today, all items in good condition."))
            out.append(dict(party=m["party"], label="Stores: not received yet",
                            text="Not received yet, expected next week."))
        elif m["code"] in ("PO_QTY_OVER", "PO_BALANCE", "PO_PRICE"):
            out.append(dict(party=m["party"], label="Procurement approves",
                            text="Approved - extra quantity was ordered by phone, PO amendment to follow."))
            out.append(dict(party=m["party"], label="Procurement rejects",
                            text="Reject - we did not order the extra. Ask them for a credit note."))
    seen, uniq = set(), []
    for e in out:
        if e["label"] not in seen:
            seen.add(e["label"])
            uniq.append(e)
    return uniq
