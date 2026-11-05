"""Step 4: Indian tax & compliance on each vendor invoice.

  E-invoice (IRN)   Vendors above the ₹5 crore turnover limit must issue e-invoices. Without a valid IRN the
                    invoice is not a valid tax invoice and our input tax credit (ITC) is lost. We check that an
                    IRN is printed, that it has the right shape (64 hex characters), and that it is consistent
                    with this invoice: the IRN is the SHA-256 of supplier GSTIN, financial year, document type
                    and document number, so we recompute it. (Live verification on the IRP needs a GSP
                    connection; this demo checks offline.)
  TDS               Income-tax Act 2025, section 393(1): contractors (freight, labour) 1% for individuals/HUF,
                    2% for others, above ₹30,000 per payment or ₹1,00,000 a year; technical services 2% above
                    ₹50,000 a year. Deducted on the value before GST and paid to the government, not the vendor.
  MSME              Micro and small enterprises must be paid within 45 days (MSMED Act s.15); a later payment
                    also loses the tax deduction. The due date is capped at 45 days.
  GSTR-2B           Is the invoice in our GSTR-2B (i.e. has the vendor filed it)? If not, ITC is pending. This
                    doesn't block payment; it's tracked so the tax team can chase it.
Rates and thresholds live in RULES below; verify them against current law before relying on them.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta

from .utils import fmt_inr

RULES = dict(
    contract=dict(label="Contractor (s.393(1), was 194C)", rate_individual=1.0, rate_other=2.0,
                  single_threshold=30000, annual_threshold=100000),
    technical=dict(label="Technical services (s.393(1), was 194J)", rate=2.0, annual_threshold=50000),
    msme_days=45,
)
IRN_RE = re.compile(r"\bIRN\s*[:.\-]?\s*([0-9a-fA-F]{64})\b")


def fy_of(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def irn_for(gstin: str, invoice_date: str, number: str, doc_type: str = "INV") -> str:
    fy = fy_of(date.fromisoformat(invoice_date))
    return hashlib.sha256(f"{gstin}{fy}{doc_type}{number}".encode()).hexdigest()


def pan_kind(gstin: str | None) -> str:
    """4th character of the PAN inside the GSTIN: P individual, H HUF, C company, F firm/LLP, ..."""
    if not gstin or len(gstin) < 12:
        return "?"
    return {"P": "individual", "H": "HUF", "C": "company", "F": "firm/LLP", "T": "trust", "A": "AOP",
            "B": "BOI", "G": "government", "J": "AJP", "L": "local authority"}.get(gstin[5], "other")


def _F(status, text, **kw):
    return dict(status=status, text=text, **kw)


def assess(inv: dict, text: str, vendor: dict | None, po_view: dict | None, ledger: list[dict], due: str | None,
           gstr2b: list[dict], facts: dict | None = None):
    findings, data = [], dict(tds=[], tds_total=0.0)
    facts = facts or {}
    if not vendor:
        return [_F("info", "Skipped - vendor unknown")], data, due

    # ---- e-invoice / IRN ----------------------------------------------------------------------
    m = IRN_RE.search(text or "")
    irn = (facts.get("irn") or (m.group(1) if m else None) or "").lower() or None
    data["irn"] = irn
    if vendor.get("einvoice"):
        if not irn:
            findings.append(_F("warn", "No e-invoice IRN printed. This vendor is above the ₹5 crore e-invoicing limit, "
                                       "so without an IRN this is not a valid tax invoice and our input tax credit "
                                       f"({fmt_inr(sum(t['amount'] or 0 for t in inv['taxes']))}) is lost",
                               code="IRN_MISSING", tried=["Searched the invoice for an IRN / QR reference"]))
        elif inv.get("invoice_number") and inv.get("invoice_date"):
            expect = irn_for(vendor["gstin"], inv["invoice_date"], inv["invoice_number"])
            if expect == irn:
                findings.append(_F("pass", f"E-invoice IRN {irn[:10]}… matches this invoice (recomputed from GSTIN, "
                                           f"FY {fy_of(date.fromisoformat(inv['invoice_date']))} and number)"
                                           + (f"; {facts['irn_source']}" if facts.get("irn_source") else "")))
            else:
                findings.append(_F("warn", f"IRN {irn[:10]}… does not belong to invoice {inv['invoice_number']} - it was "
                                           "generated for a different document (copied or altered)", code="IRN_MISMATCH",
                                   tried=["Recomputed the IRN from GSTIN, financial year and invoice number"]))
    else:
        findings.append(_F("pass", "Vendor is below the e-invoicing limit - a regular tax invoice is valid"))

    # ---- TDS on services -------------------------------------------------------------------------
    service_base = sum(r.get("amount") or 0 for r in (po_view or {}).get("lines", []) if r.get("service"))
    if service_base:
        kind = vendor.get("tds") or "contract"
        rule = RULES[kind]
        from .config import today
        ref = date.fromisoformat(inv["invoice_date"]) if inv.get("invoice_date") else today()
        prior = sum(r["subtotal"] for r in ledger if r["vendor_id"] == vendor["id"] and r["status"] == "APPROVE"
                    and (r.get("invoice_date") or "") >= f"{fy_of(ref)[:4]}-04-01")
        if kind == "contract":
            pk = pan_kind(vendor["gstin"])
            rate = rule["rate_individual"] if pk in ("individual", "HUF") else rule["rate_other"]
            applies = service_base > rule["single_threshold"] or prior + service_base > rule["annual_threshold"]
            why = (f"{pk} contractor at {rate:g}%" if applies else
                   f"below ₹30,000 per payment and ₹1,00,000 for the year ({fmt_inr(prior + service_base)} so far)")
        else:
            rate = rule["rate"]
            applies = prior + service_base > rule["annual_threshold"]
            why = f"technical services at {rate:g}%" if applies else f"below ₹50,000 for the year"
        amt = round(service_base * rate / 100) if applies else 0
        data["tds"].append(dict(section=rule["label"], base=service_base, rate=rate if applies else 0, amount=amt,
                                year_to_date=prior))
        data["tds_total"] = float(amt)
        findings.append(_F("info", f"TDS {rule['label']}: {fmt_inr(amt)} on services of {fmt_inr(service_base)} "
                                   f"(before GST) - {why}" if applies else
                           f"No TDS on services of {fmt_inr(service_base)}: {why}"))
    data["net_payable"] = round((inv.get("total") or 0) - data["tds_total"], 2)

    # ---- MSME 45-day rule ------------------------------------------------------------------------
    if vendor.get("msme") and inv.get("invoice_date"):
        limit = date.fromisoformat(inv["invoice_date"]) + timedelta(days=RULES["msme_days"])
        data["msme_deadline"] = limit.isoformat()
        if not due or date.fromisoformat(due) > limit:
            findings.append(_F("info", f"MSME vendor ({vendor.get('udyam', 'Udyam registered')}): must be paid within "
                                       f"45 days by law, so payment moves to {limit.strftime('%d %b %Y')}"
                                       + (f" (their terms say {date.fromisoformat(due).strftime('%d %b %Y')})" if due else "")))
            due = limit.isoformat()
        else:
            findings.append(_F("pass", f"MSME vendor: due date is inside the 45-day legal limit "
                                       f"({limit.strftime('%d %b %Y')})"))

    # ---- GSTR-2B (input tax credit) --------------------------------------------------------------
    gst = round(sum(t["amount"] or 0 for t in inv["taxes"]), 2)
    in2b = any(e["gstin"] == vendor["gstin"] and e["invoice_number"] == inv.get("invoice_number")
               for e in gstr2b)
    data["itc"] = dict(amount=gst, in_gstr2b=in2b)
    if gst:
        findings.append(_F("pass", f"In our GSTR-2B: input tax credit of {fmt_inr(gst)} is available") if in2b else
                        _F("info", f"Not in GSTR-2B yet: input tax credit of {fmt_inr(gst)} is pending until the vendor "
                                   "files it (payment not held; tax team tracks it)"))
    return findings, data, due


def load_gstr2b(path) -> list[dict]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return []


FOREIGN = re.compile(r"(?:\b(USD|EUR|GBP|AED|SGD|JPY|CNY|AUD|CAD)\b|(US\$|S\$|€|£|\$))\s?\d|"
                     r"\d[\d,.]*\s?(?:\b(USD|EUR|GBP|AED|SGD|JPY|CNY|AUD|CAD)\b|(€|£))", re.I)
SYMBOL = {"$": "USD", "US$": "USD", "S$": "SGD", "€": "EUR", "£": "GBP"}


def foreign_currency(text: str) -> str | None:
    """The currency of the amounts, if it is clearly not INR (at least two amounts carry it)."""
    hits = [next(g for g in m.groups() if g) for m in FOREIGN.finditer(text or "")]
    hits = [SYMBOL.get(h, SYMBOL.get(h.upper(), h.upper())) for h in hits]
    if len(hits) < 2:
        return None
    return max(set(hits), key=hits.count)
