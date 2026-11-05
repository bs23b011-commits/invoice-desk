"""Small shared helpers: money parsing, identifiers, GSTIN validation, fuzzy matching."""
from __future__ import annotations

import re
from datetime import date, datetime
from difflib import SequenceMatcher

GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")

STATE_CODES = {
    "27": "Maharashtra", "29": "Karnataka", "33": "Tamil Nadu", "36": "Telangana",
    "07": "Delhi", "24": "Gujarat", "32": "Kerala",
}


def gstin_checksum(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        product = GSTIN_CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return GSTIN_CHARS[(36 - total % 36) % 36]


def gstin_is_valid(gstin: str | None) -> bool:
    if not gstin or len(gstin) != 15 or not GSTIN_RE.fullmatch(gstin):
        return False
    return gstin_checksum(gstin[:14]) == gstin[14]


def parse_money(text: str | None) -> float | None:
    """'Rs. 1,42,500.00' -> 142500.0 ; handles Indian/Western grouping."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = re.sub(r"(?i)(rs\.?|inr|₹)", "", str(text)).strip()
    s = s.replace(" ", "")
    m = re.search(r"-?[\d,]+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def fmt_inr(x: float | None) -> str:
    if x is None:
        return "—"
    neg = x < 0
    x = abs(round(x, 2))
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'-' if neg else ''}₹{whole}.{frac}"


def norm_invoice_no(s: str | None) -> str | None:
    """INV-0042, inv 42, INV/0042 -> INV42. Used only for duplicate detection."""
    if not s:
        return None
    s = re.sub(r"[^A-Z0-9]", "", s.upper())
    return re.sub(r"(?<=[A-Z])0+(?=\d)", "", s)


def norm_po(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(r"(\d{4})\D*(\d{3,5})", s)
    if m:
        return f"PO-{m.group(1)}-{int(m.group(2)):04d}"
    return re.sub(r"\s+", "", s.upper())


DATE_FORMATS = [
    "%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d",
    "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%B %d %Y", "%b %d %Y", "%d-%B-%Y",
    # two-digit years ("04-Nov-26", "4/11/26"): read as 20xx
    "%d-%b-%y", "%d %b %y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
]


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip().rstrip(".,")
    s = re.sub(r"(?i)\b(\d{1,2})(st|nd|rd|th)\b", r"\1", s)  # "November 4th 2026" -> "November 4 2026"
    s = re.sub(r"\s+", " ", s)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    clean = lambda x: re.sub(r"\b(pvt|private|ltd|limited|llp|inc|co|the|and|&)\b", " ",
                             re.sub(r"[^a-z0-9& ]", " ", x.lower()))
    a2, b2 = " ".join(clean(a).split()), " ".join(clean(b).split())
    return SequenceMatcher(None, a2, b2).ratio()


def token_overlap(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z]{3,}", a.lower()))
    tb = set(re.findall(r"[a-z]{3,}", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))
