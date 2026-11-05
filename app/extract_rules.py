"""Stage 3 (rules reader): find invoice fields from positioned words.

Works on any layout we've seen because it looks for *labels* ("Invoice No", "Bill No.", "INVOICE #")
and then reads the value either to the right of the label or directly below it.
Line items are parsed from rows ending in qty / rate / amount.
Every field records how it was found and a confidence, so later stages can decide how far to trust it.
"""
from __future__ import annotations

import re

from .reader import Document, Line, Word
from .risk import find_bank
from .utils import GSTIN_RE, gstin_checksum, gstin_is_valid, norm_po, parse_date, parse_money

LABELS = {
    "invoice_number": [r"invoice\s*(?:no|number|#)\.?", r"bill\s*(?:no|number|#)\.?", r"inv\.?\s*(?:no|#)\.?",
                       r"document\s*(?:no|number|#)\.?", r"tax\s*inv(?:oice|\.)?\s*ref\.?"],
    "invoice_date": [r"invoice\s*date", r"issue\s*date", r"bill\s*date", r"(?<!due\s)\bdate\b"],
    "due_date": [r"due\s*date"],
    "po_number": [r"po\s*ref\.?", r"purchase\s*order(?:\s*no\.?)?", r"p\.?o\.?\s*(?:no|number|#)\.?",
                  r"your\s*ref\.?"],
}
ALL_LABEL_RE = re.compile("|".join(p for ps in LABELS.values() for p in ps) + r"|payment\s*terms|due\s*date",
                          re.I)
MONEY_RE = re.compile(r"\d[\d,]*\.\d{2}")
ITEM_RE = re.compile(
    r"^(?:(?P<idx>\d{1,2})\s+)?(?P<desc>.*?[A-Za-z\u0900-\u097F].*?)\s+(?:(?P<hsn>\d{4,8})\s+)?"
    r"(?P<qty>\d[\d,]*(?:\.\d+)?)(?:\s+(?P<unit>[A-Za-z\u0900-\u097F]{1,6}\.?))?\s+"
    r"(?P<rate>\d[\d,]*\.\d{2})\s+(?P<amt>\d[\d,]*\.\d{2})$"
)
TAX_RE = re.compile(r"\b(CGST|SGST|IGST|UTGST|GST)\b\s*(?:[@(]\s*)?(\d{1,2}(?:\.\d+)?)?\s*%", re.I)
SUBTOTAL_RE = re.compile(r"sub\s*-?\s*total|taxable\s*(?:value|amt\.?|amount)|assessable\s*value", re.I)
TOTAL_RE = re.compile(r"grand\s*total|total\s*due|amount\s*payable|net\s*amount|invoice\s*total|"
                      r"net\s*payable|total\s*payable|amount\s*due|balance\s*due|invoice\s*value|"
                      r"total\s*\(inclusive|^total\b", re.I)
CURRENCY_RE = re.compile(r"(?i)(?:\brs\.?|\binr\b|₹)\s*")
INCLUSIVE_RE = re.compile(r"inclusive\s+of\s+(?:gst|tax)|included\s+in\s+above", re.I)
GSTIN_SHAPE = "DDLLLLLDDDDLAZA"  # D digit, L letter, A alphanumeric, Z literal 'Z'
TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "T": "7"}
TO_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "4": "A", "7": "T"}


# ---- Hindi / Devanagari -------------------------------------------------------------------------
# PDF text layers often store Devanagari in visual order (the i-matra before its consonant, anusvara after),
# and drop parts of conjuncts. Matching labels on the consonant skeleton - vowel signs, virama, anusvara,
# candrabindu, nukta and spaces removed - makes "बीजक संख्या" and "बीजक सख्ं या" the same label.
DEVA_MARKS = re.compile(r"[\u0900-\u0903\u093A-\u094F\u0951-\u0957\u0962\u0963\s.:]")
DEVA_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
DEVA_RE = re.compile(r"[\u0900-\u097F]")


def skel(t: str) -> str:
    return DEVA_MARKS.sub("", t)


LABELS_HI = {
    "invoice_number": ["बीजक संख्या", "चालान संख्या", "बिल संख्या", "इनवॉइस संख्या", "बीजक सं"],
    "invoice_date": ["बीजक दिनांक", "दिनांक", "तारीख"],
    "due_date": ["देय तिथि", "देय दिनांक", "भुगतान तिथि"],
    "po_number": ["क्रय आदेश संख्या", "खरीद आदेश संख्या", "क्रय आदेश सं", "क्रय आदेश"],
}
TAX_HI = {skel("आईजीएसटी"): "IGST", skel("सीजीएसटी"): "CGST", skel("एसजीएसटी"): "SGST", skel("यूटीजीएसटी"): "UTGST",
          skel("जीएसटी"): "GST"}
SUBTOTAL_HI = [skel(x) for x in ("कर योग्य मूल्य", "कर योग्य राशि", "उप योग", "उपयोग")]
TOTAL_HI = [skel(x) for x in ("कुल योग", "कुल राशि", "कुल देय", "सकल योग", "कुल देय राशि")]
HEADER_HI = {skel(x) for x in ("विवरण", "मात्रा", "दर", "राशि", "एचएसएन", "वस्तु")}


def normalise_hindi(doc: Document) -> bool:
    """Devanagari digits -> 0-9 and Hindi tax names -> CGST/SGST/IGST, in place. Returns True if Hindi was seen."""
    seen = False
    for w in doc.words:
        if not DEVA_RE.search(w.text):
            continue
        seen = True
        t = w.text.translate(DEVA_DIGITS)
        if skel(t) in TAX_HI:
            t = TAX_HI[skel(t)]
        elif t in ("रु.", "रु", "₹"):
            t = "Rs."
        w.text = t
    return seen


def _find_label_hi(line: Line, label: str):
    target = skel(label)
    ws = line.words
    for i in range(len(ws)):
        acc = ""
        for j in range(i, min(len(ws), i + 5)):
            acc += skel(ws[j].text)
            if acc == target:
                return i, j
            if not target.startswith(acc):
                break
    return None


def _field(value, raw, method, conf, box=None):
    return {"value": value, "raw": raw, "method": method, "confidence": round(conf, 2), "box": box if value is not None else None}


def _line_conf(words: list[Word]) -> float:
    return min((w.conf for w in words), default=100.0) / 100.0


def _find_label(line: Line, pattern: str):
    """Return (start_word_idx, end_word_idx) of the label inside the line, or None."""
    text, spans, pos = "", [], 0
    for w in line.words:
        spans.append((pos, pos + len(w.text)))
        text += w.text + " "
        pos += len(w.text) + 1
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    s = next(i for i, (a, b) in enumerate(spans) if b > m.start())
    e = next(i for i, (a, b) in enumerate(spans) if b >= m.end())
    return s, e


def _value_right(line: Line, end_idx: int) -> list[Word]:
    out, prev = [], line.words[end_idx]
    for w in line.words[end_idx + 1:]:
        if w.x0 - prev.x1 > 28:
            if out:
                break
            if w.x0 - prev.x1 > 140:  # too far: belongs to another column
                break
        rest = " ".join(x.text for x in line.words[line.words.index(w):])
        if ALL_LABEL_RE.match(rest):
            break
        if w.text in (":", ".", "#", "-"):
            prev = w
            continue
        out.append(w)
        prev = w
    if out and out[0].text.startswith(":"):
        out[0] = Word(out[0].text.lstrip(":"), out[0].x0, out[0].x1, out[0].top, out[0].bottom, out[0].conf)
    return [w for w in out if w.text]


def _value_right_near(lines: list[Line], li: int, label_words: list[Word]) -> list[Word]:
    """Value to the right of the label but grouped into a neighbouring line (scans: Devanagari labels and digits
    sit on slightly different baselines). Only words that vertically overlap the label count."""
    lab_top = min(w.top for w in label_words)
    lab_bot = max(w.bottom for w in label_words)
    x1 = label_words[-1].x1
    cands = []
    for nl in lines[max(0, li - 2): li + 3]:
        for w in nl.words:
            overlap = min(lab_bot, w.bottom) - max(lab_top, w.top)
            if overlap > 0.25 * (w.bottom - w.top) and x1 < w.x0 < x1 + 150 and w not in label_words:
                cands.append(w)
    cands.sort(key=lambda w: w.x0)
    out = []
    for w in cands:
        if out and w.x0 - out[-1].x1 > 20:
            break
        out.append(w)
    return out


def _value_below(lines: list[Line], li: int, label_words: list[Word]) -> list[Word]:
    lx0 = label_words[0].x0
    for nxt in lines[li + 1: li + 3]:
        if nxt.top - lines[li].top > 30:
            break
        cands = [w for w in nxt.words if lx0 - 6 <= w.x0 <= lx0 + 40]
        if not cands:
            continue
        start = nxt.words.index(cands[0])
        out = [nxt.words[start]]
        for w in nxt.words[start + 1:]:
            if w.x0 - out[-1].x1 > 12:
                break
            out.append(w)
        return out
    return []


def _find_labeled(doc: Document, key: str):
    hit = _find_labeled_latin(doc, key)
    if hit or not doc.has_hindi:
        return hit
    for label in LABELS_HI[key]:
        for li, line in enumerate(doc.lines):
            h = _find_label_hi(line, label)
            if not h:
                continue
            s, e = h
            if key == "invoice_date" and s > 0 and skel(line.words[s - 1].text) in (skel("देय"), skel("बीजक") + "X"):
                continue
            for method, vals in ((f"Hindi label '{label}' → right", _value_right(line, e)),
                                 (f"Hindi label '{label}' → right", _value_right_near(doc.lines, li, line.words[s:e + 1])),
                                 (f"Hindi label '{label}' → below", _value_below(doc.lines, li, line.words[s:e + 1]))):
                if vals and _clean(key, " ".join(w.text for w in vals)) is not None:
                    return " ".join(w.text for w in vals), method, _line_conf(vals), vals
    return None


def _find_labeled_latin(doc: Document, key: str):
    for pat in LABELS[key]:
        for li, line in enumerate(doc.lines):
            hit = _find_label(line, pat)
            if not hit:
                continue
            s, e = hit
            if key == "invoice_date" and re.search(r"due", " ".join(w.text for w in line.words[max(0, s - 1):e + 1]), re.I):
                continue
            for method, vals in (("label → right", _value_right(line, e)),
                                 ("label → below", _value_below(doc.lines, li, line.words[s:e + 1]))):
                if vals and _clean(key, " ".join(w.text for w in vals)) is not None:
                    return " ".join(w.text for w in vals), method, _line_conf(vals), vals
    return None


def _hi_total(t: str):
    k = skel(re.sub(r"[\d,()₹.@%A-Za-z]", "", t))
    if any(x and x in k for x in SUBTOTAL_HI):
        return "subtotal"
    if any(x and k.startswith(x) for x in TOTAL_HI):
        return "total"
    return None


def _clean(key: str, raw: str):
    if key == "invoice_number":
        return _clean_invoice_no(raw)
    if key in ("invoice_date", "due_date"):
        d = _clean_date(raw)
        return d.isoformat() if d else None
    return norm_po(raw) if re.search(r"\d{3}", raw) else None


def _clean_invoice_no(raw: str):
    for tok in raw.split():
        tok = tok.strip(":,;")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/\-_.]{1,}", tok) and re.search(r"\d", tok):
            return tok
    return None


def _clean_date(raw: str):
    toks = raw.replace(",", ", ").split()
    for n in range(min(4, len(toks)), 0, -1):
        cand = " ".join(toks[:n]).replace(" ,", ",").replace(",,", ",")
        d = parse_date(cand)
        if d:
            return d
    return None


def _fix_gstin(g: str):
    """Position-aware OCR fix: each GSTIN position must be a digit or a letter; the checksum proves the fix."""
    if gstin_is_valid(g):
        return g, False
    out = []
    for ch, kind in zip(g, GSTIN_SHAPE):
        if kind == "D":
            ch = TO_DIGIT.get(ch, ch)
        elif kind == "L":
            ch = TO_LETTER.get(ch, ch)
        elif kind == "Z":
            ch = "Z" if ch in "Z27" else ch
        out.append(ch)
    fixed = "".join(out)
    return (fixed, True) if gstin_is_valid(fixed) else (g, False)


def _gstin_candidates(doc: Document) -> list[tuple[str, list[Word]]]:
    """(candidate GSTIN, the words it was read from)."""
    found = []
    for line in doc.lines:
        for g in GSTIN_RE.findall(line.text):
            found.append((g, [w for w in line.words if g in w.text or w.text in g and len(w.text) > 3]))
        m = re.search(r"GSTIN\s*[:;.]?\s*(.+)", line.text, re.I)
        hi = None
        if not m:  # Hindi label जीएसटीआईएन (matched on its skeleton)
            for i, w in enumerate(line.words):
                if skel(w.text).startswith(skel("जीएसटीआईएन")):
                    hi = " ".join(x.text for x in line.words[i + 1:i + 4])
                    break
        if m or hi:
            compact = re.sub(r"[^A-Za-z0-9]", "", m.group(1) if m else hi).upper()
            cands = [compact[:15]]
            if len(compact) >= 16:  # OCR inserted a character: try dropping each one, keep checksum-valid results
                cands += [compact[:i] + compact[i + 1:16] for i in range(16)]
            ws = [w for w in line.words if not re.match(r"GSTIN", w.text, re.I)]
            valid = [c for c in cands if len(c) == 15 and gstin_is_valid(_fix_gstin(c)[0])]
            for c in (valid[:1] if valid else cands[:1]):
                if len(c) == 15:
                    found.append((c, ws[:3]))
    seen, out = set(), []
    for g, ws in found:
        if g not in seen:
            seen.add(g)
            out.append((g, ws))
    return out


def hamming(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b))


def extract(doc: Document, buyer_gstin: str) -> dict:
    f: dict = {}
    doc.has_hindi = normalise_hindi(doc)
    text = doc.text
    base_conf = 0.98 if doc.source == "text-layer" else 0.9

    # --- identifiers -------------------------------------------------------------------------
    for key in ("invoice_number", "invoice_date", "due_date", "po_number"):
        hit = _find_labeled(doc, key)
        if not hit:
            f[key] = _field(None, None, "not found", 0.0)
            continue
        raw, method, conf, vals = hit
        val = _clean(key, raw)
        bw = vals
        if key in ("invoice_number", "po_number"):  # highlight just the identifier, not the rest of the sentence
            idw = [w for w in vals if re.search(r"\d", w.text)]
            bw = ([w for w in vals[:vals.index(idw[0])] if re.fullmatch(r"(?i)po|p\.o\.?", w.text)] + idw[:1]) if idw else vals
        f[key] = _field(val, raw, method, min(base_conf, conf) if val else 0.0, doc.box(bw))

    if not f["po_number"]["value"]:  # PO mentioned in running text ("Against your Purchase Order No. ...")
        pat = re.compile(r"\bP\.?O\.?[\s\-:#.]*(?:No\.?\s*)?(\d{4})\s*[/\-]\s*(\d{2,5})\b", re.I)
        for line in doc.lines:
            m = pat.search(line.text)
            if m:
                ws = [w for w in line.words if re.search(r"\d{2,}", w.text) and w.text.strip(".,") in m.group(0)]
                f["po_number"] = _field(norm_po(m.group(0)), m.group(0), "pattern in text", base_conf * 0.9,
                                        doc.box(ws or line.words))
                break

    # --- parties -------------------------------------------------------------------------------
    gstins, gbox = [], {}
    for g, ws in _gstin_candidates(doc):
        g2, fixed = _fix_gstin(g)
        if g2 not in [x[0] for x in gstins]:
            gstins.append((g2, fixed, g))
            gbox[g2] = doc.box(ws)
    buyer_g = next((g for g in gstins if hamming(g[0], buyer_gstin) <= 2), None)
    if buyer_g and buyer_g[0] != buyer_gstin and doc.source == "ocr":
        buyer_g = (buyer_gstin, True, buyer_g[2])
    vendor_g = next((g for g in gstins if g is not buyer_g and hamming(g[0], buyer_gstin) > 2), None)
    f["vendor_gstin"] = _field(vendor_g[0] if vendor_g else None, vendor_g[2] if vendor_g else None,
                               "GSTIN pattern" + (" + OCR fix (checksum verified)" if vendor_g and vendor_g[1] else ""),
                               (base_conf if gstin_is_valid(vendor_g[0]) else base_conf * 0.7) if vendor_g else 0.0,
                               gbox.get(vendor_g[0]) if vendor_g else None)
    f["buyer_gstin"] = _field(buyer_g[0] if buyer_g else None, buyer_g[2] if buyer_g else None,
                              "GSTIN pattern" + (" + OCR fix" if buyer_g and buyer_g[1] else ""),
                              base_conf if buyer_g else 0.0,
                              (gbox.get(buyer_g[0]) or next((b for k, b in gbox.items() if hamming(k, buyer_gstin) <= 2), None))
                              if buyer_g else None)

    vendor_name, vn_words = None, []
    for line in doc.lines[:4]:
        t = re.sub(r"\b(TAX\s+INVOICE|INVOICE)\b", "", line.text, flags=re.I).strip(" |-")
        if len(re.findall(r"[A-Za-z]", t)) >= 6 and not re.match(r"^(no\.?|s\.?no|plot|\d)", t, re.I):
            vendor_name = t
            vn_words = [w for w in line.words if not re.fullmatch(r"(?i)tax|invoice|\|", w.text)]
            break
    f["vendor_name"] = _field(vendor_name, vendor_name, "document header", base_conf * 0.9 if vendor_name else 0,
                              doc.box(vn_words))

    # --- line items ----------------------------------------------------------------------------
    items = []
    header_words = re.compile(r"\b(description|particulars|item|hsn|qty|quantity|rate|price|amount)\b", re.I)
    header_idx = next((i for i, l in enumerate(doc.lines) if len(set(m.lower() for m in
                       header_words.findall(l.text))) >= 2
                       or len({skel(w.text) for w in l.words} & HEADER_HI) >= 2), -1)
    for line in doc.lines[header_idx + 1:]:
        t = line.text.strip()
        if SUBTOTAL_RE.search(t) or TOTAL_RE.search(t) or TAX_RE.search(t) or _hi_total(t):
            break
        t = re.sub(r"[|~—_]+", " ", t)
        t = CURRENCY_RE.sub("", t)  # "Rs. 1,200.00" in a line item is just 1,200.00
        t = re.sub(r"\s+", " ", t).strip()
        m = ITEM_RE.match(t)
        if not m:
            continue
        qty, rate, amt = parse_money(m["qty"]), parse_money(m["rate"]), parse_money(m["amt"])
        items.append({
            "description": m["desc"].strip(" -"), "hsn": m["hsn"], "qty": qty, "unit": m["unit"],
            "unit_price": rate, "amount": amt, "confidence": round(min(base_conf, _line_conf(line.words)), 2),
            "raw": t, "box": doc.box(line.words),
        })
    f["lines"] = items

    # --- totals --------------------------------------------------------------------------------
    subtotal = total = None
    taxes = []
    inclusive = bool(INCLUSIVE_RE.search(text))
    for line in doc.lines:
        t = line.text
        if "GSTIN" in t.upper():
            continue
        monies = MONEY_RE.findall(t)
        if not monies:
            continue
        amt = parse_money(monies[-1])
        tm = TAX_RE.search(t)
        bx = doc.box(line.words)
        hi = _hi_total(t) if doc.has_hindi else None
        if (SUBTOTAL_RE.search(t) or hi == "subtotal") and subtotal is None:
            subtotal = (amt, t, _line_conf(line.words), bx, hi)
        elif tm:
            taxes.append({"type": tm.group(1).upper(), "rate": float(tm.group(2)) if tm.group(2) else None, "amount": amt,
                          "raw": t, "confidence": round(min(base_conf, _line_conf(line.words)), 2), "box": bx})
        elif (TOTAL_RE.search(t.strip()) or hi == "total") and total is None:
            total = (amt, t, _line_conf(line.words), bx, hi)
    f["subtotal"] = _field(subtotal[0] if subtotal else None, subtotal[1] if subtotal else None,
                           "Hindi label 'कर योग्य मूल्य'" if subtotal and subtotal[4] else "label 'Sub Total/Taxable Value'",
                           min(base_conf, subtotal[2]) if subtotal else 0.0,
                           subtotal[3] if subtotal else None)
    f["total"] = _field(total[0] if total else None, total[1] if total else None,
                        "Hindi label 'कुल योग'" if total and total[4] else "label 'Total'",
                        min(base_conf, total[2]) if total else 0.0, total[3] if total else None)
    f["taxes"] = taxes
    f["tax_inclusive"] = inclusive

    # --- payee bank (only what's printed; used by the fraud check) -----------------------------
    bank, bank_raw = find_bank(text)
    bank_words = []
    if bank:
        last4 = bank[-4:]
        for line in doc.lines:
            if last4 in line.text and find_bank(line.text)[0]:
                bank_words = [w for w in line.words if last4 in w.text or re.search(r"(?i)" + re.escape(w.text.strip(".,:")),
                              bank_raw) and len(w.text) > 2 and not re.fullmatch(r"(?i)bank|a/?c|no\.?", w.text)]
                break
    f["bank"] = _field(bank, bank_raw, "bank pattern", base_conf if bank else 0.0, doc.box(bank_words))
    f["currency"] = "INR"
    return f
