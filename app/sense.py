"""Common sense an experienced AP clerk applies before checking any numbers.

Real complaints about AP automation (credit memos paid out as bills, proformas paid and then the real invoice
paid again, statements keyed as new invoices, advances paid twice, altered figures) are rarely reading errors.
They are documents read correctly and *understood* wrongly. This module answers:

  doc_kind()        is this a bill we owe at all? (credit note, proforma, quotation, statement, ...)
  amount_in_words() what the words say the total is - banks treat the words as the true amount
  adjustments()     what the invoice subtracts or adds after its total (advance paid, previous balance)
  reverse_charge()  is GST payable by us instead of the vendor
  pack_size()       "bundle of 25", "pack of 10": the unit the vendor billed in
Everything is plain pattern work on the text that was read; each function says why it decided what it did.
"""
from __future__ import annotations

import re

from .utils import parse_money

# ------------------------------------------------------------------------------------------- document type
# (kind, title pattern, what it means for AP)
KINDS = [
    ("credit_note", r"credit\s*(?:note|memo)", "a credit note: the vendor owes us this money"),
    ("debit_note", r"debit\s*(?:note|memo)", "a debit note: an extra charge that must refer to an original invoice"),
    ("proforma", r"pro[\s\-]?forma(?:\s+invoice)?", "a proforma invoice: a request for advance payment, not a tax invoice"),
    ("quotation", r"quotation|quote|estimate|price\s+offer", "a quotation: an offer, nothing is owed yet"),
    ("statement", r"statement\s+of\s+accounts?|account\s+statement|ledger\s+statement|statement",
     "a statement of account: a summary of invoices, each handled on its own"),
    ("challan", r"delivery\s+challan|delivery\s+note|dispatch\s+note|packing\s+list",
     "a delivery challan: proof of dispatch, not a bill"),
    ("purchase_order", r"purchase\s+order", "a purchase order, not a bill"),
    ("receipt", r"payment\s+receipt|receipt\s+voucher|money\s+receipt|receipt",
     "a receipt: it confirms a payment already made"),
    ("reminder", r"payment\s+reminder|reminder", "a payment reminder"),
]
INVOICE_TITLE = re.compile(r"\b(tax\s+invoice|invoice|bill\s+of\s+supply|bill)\b", re.I)
NOT_TAX_INVOICE = re.compile(r"not\s+a\s+tax\s+invoice|this\s+is\s+(?:only\s+)?a\s+pro[\s\-]?forma", re.I)
AGAINST_RE = re.compile(r"(?:against|original|ref(?:erence)?\.?\s+to)\s+(?:our\s+)?(?:tax\s+)?(?:invoice|inv\.?|bill)"
                        r"\s*(?:no\.?|number|#)?\s*[:#.]?\s*([A-Z0-9][A-Z0-9/\-]{2,})", re.I)
MONEYISH = re.compile(r"\d[\d,]*\.\d{2}")


def _title_lines(lines: list[str], n: int = 8) -> list[str]:
    """The top of page 1, where the document says what it is."""
    return [l for l in lines[:n] if l.strip()]


def doc_kind(lines: list[str], total_raw: str | None = None) -> dict:
    """{'kind', 'title', 'meaning', 'against'}. kind 'invoice' means: carry on as a bill."""
    text = "\n".join(lines)
    head = _title_lines(lines)
    against = AGAINST_RE.search(text)
    against = against.group(1).rstrip(".,") if against else None
    out = lambda k, t, m: dict(kind=k, title=t, meaning=m, against=against)

    for kind, pat, meaning in KINDS:
        rx = re.compile(rf"\b(?:{pat})\b", re.I)
        for h in head:
            m = rx.search(h)
            if not m:
                continue
            # the title word must be the heading itself, not part of a sentence ("as per your quotation no. 12")
            # or a label with a value after it ("PURCHASE ORDER NO: PO-2026-0101")
            words = len(h.split())
            after = h[m.end():]
            if re.search(r"[:#]|\d", after) or re.match(r"\s*(?:no|number|ref)\b", after, re.I):
                continue
            if kind not in ("credit_note", "debit_note", "proforma") and INVOICE_TITLE.search(h):
                continue  # "Quote Master Traders  TAX INVOICE": a name, not a title
            title_like = m.group(0).isupper() or words <= 4
            # "PAYMENT REMINDER" over a real tax invoice is still that invoice (a re-send) - handled by duplicates
            if kind == "reminder" and any(INVOICE_TITLE.search(x) for x in head):
                break
            if kind == "statement" and not re.search(r"account|ledger|statement\s+date|outstanding", text, re.I):
                break
            if kind == "receipt" and INVOICE_TITLE.search(h):
                break
            if title_like:
                return out(kind, m.group(0).strip(), meaning)
    if NOT_TAX_INVOICE.search(text):
        return out("proforma", "not a tax invoice", "it says it is not a tax invoice")
    if total_raw and re.search(r"\(\s*\d[\d,]*\.\d{2}\s*\)|-\s*(?:rs\.?|inr|₹)?\s*\d[\d,]*\.\d{2}", total_raw, re.I):
        return out("credit_note", "negative total", "the total is negative: a credit, not a bill")
    has_money = len(MONEYISH.findall(text)) >= 2
    if not INVOICE_TITLE.search(text) and not has_money:
        return out("not_invoice", "no invoice heading or amounts", "there is no invoice heading and no amounts on it")
    return out("invoice", "invoice", "")


# ------------------------------------------------------------------------------------------- amount in words
_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_SCALES = {"crore": 10 ** 7, "crores": 10 ** 7, "lakh": 10 ** 5, "lakhs": 10 ** 5, "lac": 10 ** 5, "lacs": 10 ** 5,
           "thousand": 1000, "million": 10 ** 6}
_FILLER = {"and", "only", "rupees", "rupee", "rs", "inr", "indian", "the", "sum", "of", "amount", "in", "words", "a"}
WORDS_RE = re.compile(r"(?:amount|total|rupees|inr)[^\n]{0,25}?in\s+words\s*[:\-]?\s*([^\n]+)|"
                      r"\b((?:rupees|inr)\s+[a-z][a-z\s\-,]+?\bonly)\b", re.I)


def _words_to_int(words: list[str]) -> int | None:
    total = current = 0
    seen = False
    for w in words:
        if w in _FILLER:
            continue
        if w in _UNITS:
            current += _UNITS[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "hundred":
            current = (current or 1) * 100
        elif w in _SCALES:
            total += (current or 1) * _SCALES[w]
            current = 0
        else:
            return None  # a word we don't know (OCR noise, another language): don't guess
        seen = True
    return total + current if seen else None


def amount_in_words(text: str) -> dict | None:
    """{'value', 'raw'} when the 'amount in words' line can be read completely; else None."""
    m = WORDS_RE.search(text or "")
    if not m:
        return None
    raw = (m.group(1) or m.group(2) or "").strip()
    s = raw.lower().replace("-", " ").replace(",", " ")
    s = re.sub(r"\bonly\b.*$", "", s).strip()
    neg = s.startswith("minus ")
    s = s.removeprefix("minus ").strip()
    paise = 0
    # "... rupees twenty AND fifty paise": the paise are the words between the last 'and' and 'paise'
    pm = re.search(r"\band\s+((?:(?!\band\b)[a-z\s])+?)\s+paise\b", s) or re.search(r"^\s*([a-z\s]+?)\s+paise\b", s)
    if pm:
        p = _words_to_int(pm.group(1).split())
        if p is None or p >= 100:
            return None
        paise = p
        s = s[:pm.start()].strip()
    r = _words_to_int(s.split())
    if r is None:
        return None
    v = r + paise / 100
    return dict(value=-v if neg else v, raw=raw)


# ------------------------------------------------------------------------------------------- after the total
ADVANCE_RE = re.compile(r"advance|on\s+account\s+payment|already\s+paid|paid\s+earlier|less\s*:?\s*received", re.I)
PREVIOUS_RE = re.compile(r"previous\s+(?:balance|dues?|outstanding)|opening\s+balance|arrears|\bb/f\b|"
                         r"brought\s+forward\s+balance|old\s+balance|pending\s+(?:balance|dues?)", re.I)
PAYABLE_RE = re.compile(r"balance\s+(?:payable|due)|net\s+(?:payable|amount\s+due)|total\s+(?:amount\s+)?due|"
                        r"amount\s+(?:payable|due)|total\s+payable", re.I)


def adjustments(lines: list[str], total_raw: str | None) -> dict:
    """Rows printed after the invoice total that change what we'd actually pay."""
    out = dict(advance=0.0, advance_raw=None, previous=0.0, previous_raw=None, payable=None, payable_raw=None)
    if not total_raw:
        return out
    try:
        i = next(k for k, l in enumerate(lines) if l == total_raw)
    except StopIteration:
        return out
    for l in lines[i + 1:i + 8]:
        m = MONEYISH.findall(l)
        if not m:
            continue
        amt = parse_money(m[-1])
        if ADVANCE_RE.search(l) and out["advance_raw"] is None:
            out.update(advance=amt, advance_raw=l)
        elif PREVIOUS_RE.search(l) and out["previous_raw"] is None:
            out.update(previous=amt, previous_raw=l)
        elif PAYABLE_RE.search(l) and out["payable_raw"] is None:
            out.update(payable=amt, payable_raw=l)
    return out


# ------------------------------------------------------------------------------------------- reverse charge
RCM_RE = re.compile(r"reverse\s+charge[^\n]{0,30}?(?:[:\-]\s*)?(yes|applicable|y\b)|"
                    r"payable\s+(?:by\s+the\s+recipient\s+)?(?:on|under)\s+reverse\s+charge|"
                    r"gst\s+(?:on\s+this\s+supply\s+)?(?:is\s+)?payable\s+by\s+the\s+recipient", re.I)
RCM_NO_RE = re.compile(r"reverse\s+charge[^\n]{0,30}?[:\-]?\s*(no|n\b|not\s+applicable)", re.I)
RCM_RATE_RE = re.compile(r"reverse\s+charge[^\n]*?(\d{1,2}(?:\.\d+)?)\s*%", re.I)


def reverse_charge(text: str) -> dict | None:
    if not text or RCM_NO_RE.search(text) and not RCM_RE.search(text.replace(RCM_NO_RE.search(text).group(0), "")):
        return None
    m = RCM_RE.search(text)
    if not m:
        return None
    r = RCM_RATE_RE.search(text)
    return dict(raw=m.group(0), rate=float(r.group(1)) if r else None)


# ------------------------------------------------------------------------------------------- pack sizes
PACK_RE = re.compile(r"(?:bundle|pack|box|carton|case|bag|set|roll|bale|dozen|lot)s?\s+of\s+(\d{1,5})|"
                     r"(\d{1,5})\s*(?:pcs|pieces|nos|units)\s*(?:/|per)\s*(?:bundle|pack|box|carton|case|bag|set)|"
                     r"\b(dozen)\b", re.I)


def pack_size(description: str) -> int | None:
    m = PACK_RE.search(description or "")
    if not m:
        return None
    if m.group(3):
        return 12
    n = int(m.group(1) or m.group(2))
    return n if n >= 2 else None


# ------------------------------------------------------------------------------------------- e-invoice QR
def read_qr(pdf_bytes: bytes) -> dict | None:
    """Decode the e-invoice QR on page 1. The government's QR is a signed token (header.payload.signature) whose
    payload carries the IRN, both GSTINs, the document number and the invoice value. It survives scanning far
    better than the tiny printed IRN, and it can't be edited without breaking - so it is also the best check that
    the figures on the page weren't altered after the e-invoice was issued."""
    import base64
    import json

    try:
        import pypdfium2 as pdfium
        pil = pdfium.PdfDocument(pdf_bytes)[0].render(scale=3).to_pil().convert("L")
    except Exception:
        return None
    texts = []
    try:  # zxing-cpp: reads small, noisy and turned QR codes from scans
        import zxingcpp
        texts = [r.text for r in zxingcpp.read_barcodes(pil) if r.text]
    except Exception:
        texts = []
    if not texts:
        try:  # fallback: OpenCV's detector (clean digital QR codes)
            import cv2
            import numpy as np
            ok, vals, _, _ = cv2.QRCodeDetector().detectAndDecodeMulti(np.asarray(pil))
            texts = [v for v in vals if v] if ok else []
        except Exception:
            texts = []
    for t in texts:
        data = None
        try:
            data = json.loads(t)
        except ValueError:
            parts = t.split(".")
            if len(parts) == 3:  # signed token: the middle part is base64url JSON
                try:
                    pl = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
                    data = json.loads(pl["data"]) if isinstance(pl.get("data"), str) else pl.get("data", pl)
                except Exception:
                    data = None
        if isinstance(data, dict) and (data.get("Irn") or data.get("DocNo")):
            return dict(irn=(data.get("Irn") or "").lower() or None, seller=data.get("SellerGstin"),
                        buyer=data.get("BuyerGstin"), number=data.get("DocNo"), date=data.get("DocDt"),
                        total=float(data["TotInvVal"]) if data.get("TotInvVal") not in (None, "") else None)
    return None
