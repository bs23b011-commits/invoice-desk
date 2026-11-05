"""Fraud & risk check: signals that an invoice is not what it claims to be.

Each signal has a weight; the weights add up to a 0-100 risk score.
  score >= 70  -> REJECT (do not pay until identity and bank are verified out of band)
  a heavy signal (weight >= 25) -> REVIEW, routed to the vendor master team / finance controller
  light signals are recorded as notes and shown on the dashboard
The point is not that any one signal proves fraud; it is that payment never goes to a new bank
account, or to a look-alike company, without a human verifying it through a channel the
fraudster doesn't control (the phone number already on file).
"""
from __future__ import annotations

import re
from datetime import date

from .utils import fmt_inr, similarity

HIGH_RISK = 70
WARN_WEIGHT = 25

BANKS = (r"HDFC|ICICI|SBI|State Bank of India|Axis|Kotak|IOB|Indian Overseas Bank|Canara|Yes\s*Bank|IDFC(?:\s*First)?|"
         r"PNB|Punjab National Bank|Bank of Baroda|BoB|Federal|IndusInd|Union Bank|RBL|Bandhan|AU Small Finance")
BANK_MASKED_RE = re.compile(rf"\b({BANKS})\b[^\n]{{0,25}}?[*xX•]{{2,}}\s?(\d{{4}})\b", re.I)
BANK_FULL_RE = re.compile(rf"\b({BANKS})\b[^\n]{{0,40}}?(?:a/?c|account)\s*(?:no\.?|number)?\s*[:.]?\s*(\d{{9,18}})", re.I)
CHANGE_WORDS = re.compile(r"new\s+bank|changed?\s+(?:our\s+)?bank|updated?\s+bank|bank\s+details\s+have\s+changed|"
                          r"new\s+account", re.I)
URGENCY_WORDS = re.compile(r"\burgent(?:ly)?\b|\bimmediate(?:ly)?\b|final\s+notice|within\s+24\s*hours|today\s+itself",
                           re.I)


def _norm_bank(name: str) -> str:
    n = re.sub(r"\s+", " ", name.strip()).upper()
    return {"STATE BANK OF INDIA": "SBI", "INDIAN OVERSEAS BANK": "IOB", "PUNJAB NATIONAL BANK": "PNB",
            "BANK OF BARODA": "BOB", "IDFC FIRST": "IDFC", "YESBANK": "YES BANK"}.get(n, n)


def find_bank(text: str):
    """Return (normalised 'BANK ****1234', raw match) or (None, None)."""
    m = BANK_MASKED_RE.search(text) or BANK_FULL_RE.search(text)
    if not m:
        return None, None
    return f"{_norm_bank(m.group(1))} ****{m.group(2)[-4:]}", m.group(0)


def _same_bank(a: str, b: str, ocr: bool) -> tuple[bool, bool]:
    """(same, ocr_near): ocr_near means one digit differs on a scan - treat as a misread, not a change."""
    ba, la = a.rsplit(" ", 1)
    bb, lb = b.rsplit(" ", 1)
    if ba != bb:
        return False, False
    diff = sum(x != y for x, y in zip(la, lb))
    if diff == 0:
        return True, False
    return (ocr and diff == 1), (ocr and diff == 1)


def assess(inv: dict, text: str, vendor: dict | None, vendor_method: str | None, ledger: list[dict],
           po_how: str | None, source: str, vendors: list[dict]):
    signals = []  # dict(key, label, weight, detail)

    def sig(key, label, weight, detail):
        signals.append(dict(key=key, label=label, weight=weight, detail=detail))

    # 1. Bank details on the invoice vs the vendor master (business-email-compromise pattern)
    bank = inv.get("bank")
    if vendor and bank:
        same, near = _same_bank(bank, _norm_bank(vendor["bank"].rsplit(" ", 1)[0]) + " " +
                                vendor["bank"].rsplit(" ", 1)[1], source == "ocr")
        if not same:
            sig("bank_change", "Bank account differs from vendor master", 45,
                f"Invoice asks for payment to {bank}; the account on file is {vendor['bank']}")
        elif near:
            sig("bank_ocr", "Bank digits read slightly differently (scan)", 0,
                f"Read {bank} vs {vendor['bank']} on file - one digit, treated as an OCR misread; paying the account on file")
    if CHANGE_WORDS.search(text):
        sig("change_wording", "Invoice announces new bank details", 15,
            "Text such as 'new bank account' on the invoice - a common pretext in payment fraud")

    # 2. Identity: look-alike company
    if vendor and vendor_method == "name match" and inv.get("vendor_gstin") and inv["vendor_gstin"] != vendor["gstin"]:
        sig("lookalike", "Look-alike of a known vendor", 40,
            f"Name '{inv.get('vendor_name')}' closely resembles {vendor['name']}, but GSTIN {inv['vendor_gstin']} "
            f"is not theirs ({vendor['gstin']})")
    elif not vendor and inv.get("vendor_name"):
        best = max(vendors, key=lambda v: similarity(inv["vendor_name"], v["name"]))
        s = similarity(inv["vendor_name"], best["name"])
        if s >= 0.7:
            sig("lookalike", "Name resembles a known vendor", 30,
                f"'{inv['vendor_name']}' is {s:.0%} similar to {best['name']}, but isn't them")

    # 3. Amount unusual for this vendor (only when no PO bounds the amount)
    if vendor and po_how != "quoted on invoice" and inv.get("total"):
        hist = [r["total"] for r in ledger if r["vendor_id"] == vendor["id"] and r["status"] == "APPROVE"]
        if hist and inv["total"] > 3 * max(hist):
            sig("amount_spike", "Amount far above this vendor's history", 25,
                f"{fmt_inr(inv['total'])} is {inv['total'] / max(hist):.1f}× the largest previous invoice "
                f"({fmt_inr(max(hist))}) and isn't bounded by a quoted PO")
        elif not hist:
            sig("first_invoice", "First invoice from this vendor", 10, "No approved invoices from this vendor yet")

    # 4. Light signals
    if URGENCY_WORDS.search(text):
        sig("urgency", "Pressure to pay quickly", 10, "Urgency wording on the invoice")
    t = inv.get("total")
    if t and t >= 50000 and abs(t % 1000) < 0.005:
        sig("round_amount", "Round-number total", 5, f"Total {fmt_inr(t)} is an exact round number")
    d = inv.get("invoice_date")
    if d and date.fromisoformat(d).weekday() == 6:  # Saturday is a working day for most Indian businesses
        sig("weekend", "Dated on a Sunday", 5, f"{date.fromisoformat(d).strftime('%A %d %b %Y')}")
    if vendor and d and inv.get("due_date"):
        days = (date.fromisoformat(inv["due_date"]) - date.fromisoformat(d)).days
        if 0 <= days < vendor["terms_days"] - 2:
            sig("short_terms", "Shorter payment terms than agreed", 5,
                f"Invoice asks for payment in {days} days; agreed terms are net {vendor['terms_days']} "
                "- paying on agreed terms")

    score = min(100, sum(s["weight"] for s in signals))
    level = "high" if score >= HIGH_RISK else "medium" if score >= 30 else "low"
    findings = []
    if score >= HIGH_RISK:
        findings.append(dict(status="fail", code="FRAUD_HIGH",
                             text=f"Risk score {score}/100: " + "; ".join(s["label"].lower() for s in signals
                                                                         if s["weight"] >= WARN_WEIGHT),
                             facts=dict(score=score, bank=bank, vendor=vendor["name"] if vendor else None),
                             tried=["Compared GSTIN, name and bank details against the vendor master",
                                    "Checked this vendor's invoice history"]))
    else:
        for s in signals:
            if s["weight"] >= WARN_WEIGHT:
                code = {"bank_change": "BANK_CHANGE", "lookalike": "IMPERSONATION",
                        "amount_spike": "AMOUNT_ANOMALY"}[s["key"]]
                findings.append(dict(status="warn", code=code, text=f"{s['label']}: {s['detail']}",
                                     facts=dict(bank=bank, on_file=vendor["bank"] if vendor else None,
                                                vendor=vendor["name"] if vendor else None,
                                                phone=vendor.get("phone") if vendor else None),
                                     tried=["Compared invoice bank details with the vendor master" if code == "BANK_CHANGE"
                                            else "Compared GSTIN and name with the vendor master" if code == "IMPERSONATION"
                                            else "Compared amount with this vendor's approved invoices"]))
            elif s["weight"] > 0:
                findings.append(dict(status="info", text=f"{s['label']}: {s['detail']}"))
            else:
                findings.append(dict(status="info", text=s["detail"]))
    if not signals:
        findings.append(dict(status="pass", text="No fraud signals: bank details match the vendor master, "
                                                 "identity checks out, amount in normal range"))
    elif not any(s["key"] in ("bank_change",) for s in signals) and bank and vendor:
        findings.insert(0, dict(status="pass", text=f"Bank details match the vendor master ({vendor['bank']})"))
    if not bank and vendor:
        findings.insert(0, dict(status="pass", text=f"No bank details printed - payment goes to the account on file "
                                                     f"({vendor['bank']})"))
    return findings, dict(score=score, level=level, signals=signals, bank=bank,
                          bank_on_file=vendor["bank"] if vendor else None)
