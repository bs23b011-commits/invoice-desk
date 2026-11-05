"""Optional second reader: an LLM turns the page text into the same JSON the rules reader produces.

Enabled only when an API key is set (GEMINI_API_KEY or ANTHROPIC_API_KEY). The LLM never decides
anything - it only reads. Its output goes through exactly the same arithmetic checks and business
rules as the rules reader, and the two readers are compared field by field.
"""
from __future__ import annotations

import json
import os
import re

import httpx

from .risk import find_bank
from .utils import norm_po, parse_date

PROMPT = """You extract data from a vendor invoice. The text below came from a PDF (possibly OCR, so it may
contain errors). Return ONLY a JSON object with these keys:
vendor_name, vendor_gstin, buyer_gstin, invoice_number, invoice_date (YYYY-MM-DD), due_date (YYYY-MM-DD or null),
po_number (or null if none is written), tax_inclusive (true if line prices already include GST),
lines: [{description, qty, unit_price, amount}], subtotal (pre-tax, or null if not printed),
taxes: [{type: CGST|SGST|IGST, rate (percent number), amount}], total,
bank (payee bank as printed, e.g. "HDFC ****4471", or null).
Numbers must be plain numbers (no commas, no currency). Copy values exactly as printed; do not correct
arithmetic and do not guess values that are not on the page - use null.

INVOICE TEXT:
"""


def provider() -> str | None:
    if os.getenv("GEMINI_API_KEY"):
        return "gemini"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def _call(text: str) -> str:
    return _call_prompt(PROMPT + text)


def _call_prompt(prompt: str) -> str:
    p = provider()
    if p == "gemini":
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": os.environ["GEMINI_API_KEY"]},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}},
            timeout=45)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    if p == "anthropic":
        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
        r = httpx.post("https://api.anthropic.com/v1/messages",
                       headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                                "content-type": "application/json"},
                       json={"model": model, "max_tokens": 2000, "temperature": 0,
                             "messages": [{"role": "user", "content": prompt}]},
                       timeout=45)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])
    raise RuntimeError("no LLM configured")


def _num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(x))
    return float(m.group(0).replace(",", "")) if m else None


def extract(text: str) -> dict:
    return _to_fields(_call(text), f"LLM ({provider()})")


def _to_fields(raw: str, name: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0) if m else raw)

    def fld(v):
        return {"value": v, "raw": v, "method": name, "confidence": 0.9 if v is not None else 0.0}

    def dt(v):
        if not v:
            return None
        d = parse_date(str(v))
        return d.isoformat() if d else None

    return {
        "vendor_name": fld(data.get("vendor_name")),
        "vendor_gstin": fld((data.get("vendor_gstin") or "").replace(" ", "").upper() or None),
        "buyer_gstin": fld((data.get("buyer_gstin") or "").replace(" ", "").upper() or None),
        "invoice_number": fld(data.get("invoice_number")),
        "invoice_date": fld(dt(data.get("invoice_date"))),
        "due_date": fld(dt(data.get("due_date"))),
        "po_number": fld(norm_po(data.get("po_number")) if data.get("po_number") else None),
        "subtotal": fld(_num(data.get("subtotal"))),
        "total": fld(_num(data.get("total"))),
        "bank": fld(find_bank(str(data.get("bank") or ""))[0]),
        "tax_inclusive": bool(data.get("tax_inclusive")),
        "currency": "INR",
        "taxes": [{"type": str(t.get("type", "GST")).upper(), "rate": _num(t.get("rate")),
                   "amount": _num(t.get("amount")), "confidence": 0.9} for t in data.get("taxes") or []],
        "lines": [{"description": l.get("description", ""), "qty": _num(l.get("qty")),
                   "unit_price": _num(l.get("unit_price")), "amount": _num(l.get("amount")),
                   "hsn": None, "unit": None, "confidence": 0.9} for l in data.get("lines") or []],
    }


REPLY_PROMPT = """Classify this reply to an accounts-payable query. Return ONLY JSON:
{"intent": one of "provide_po","confirm_receipt","not_received","approve","reject","credit_note","unclear",
 "po": purchase order number if given else null, "qty": number of units received if given else null,
 "full": true if they say everything/the rest arrived}
Do not guess. If it is ambiguous use "unclear".

REPLY:
"""


def classify_reply(text: str) -> dict:
    raw = _call_prompt(REPLY_PROMPT + text)
    m = re.search(r"\{.*\}", raw, re.S)
    d = json.loads(m.group(0) if m else raw)
    if d.get("po"):
        d["po"] = norm_po(d["po"])
    return {k: d.get(k) for k in ("intent", "po", "qty", "full") if k in d}


def extract_image(png: bytes) -> dict:
    """Vision reader: the LLM reads the page image itself (for scans OCR struggles with)."""
    import base64
    p = provider()
    b64 = base64.b64encode(png).decode()
    keys = ("vendor_name, vendor_gstin, buyer_gstin, invoice_number, invoice_date (YYYY-MM-DD), due_date, po_number, "
            "tax_inclusive, lines [{description, qty, unit_price, amount}], subtotal, taxes [{type, rate, amount}], total, bank")
    prompt = (f"This is an image of a vendor invoice (possibly a poor scan). Return ONLY JSON with keys: {keys}. "
              "Numbers as plain numbers. Copy exactly what is printed; null if unreadable. Do not correct arithmetic.")
    if p == "gemini":
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                       params={"key": os.environ["GEMINI_API_KEY"]},
                       json={"contents": [{"parts": [{"inline_data": {"mime_type": "image/png", "data": b64}},
                                                     {"text": prompt}]}],
                             "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}},
                       timeout=60)
        r.raise_for_status()
        raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    elif p == "anthropic":
        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
        r = httpx.post("https://api.anthropic.com/v1/messages",
                       headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                                "content-type": "application/json"},
                       json={"model": model, "max_tokens": 2000, "temperature": 0, "messages": [{"role": "user", "content": [
                           {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                           {"type": "text", "text": prompt}]}]}, timeout=60)
        r.raise_for_status()
        raw = "".join(b.get("text", "") for b in r.json()["content"])
    else:
        raise RuntimeError("no LLM configured")
    return _to_fields(raw, f"vision LLM ({p})")
