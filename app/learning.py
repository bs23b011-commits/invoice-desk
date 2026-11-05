"""Step 6a: learn from reviewer corrections.

When a reviewer types a value the reader couldn't find (say the invoice number), we locate that value on the page
and record the label printed next to it ("Document Ref") for that vendor. The next invoice from the same vendor
is read with the learned label - no code change, no template. Every learned label shows who taught it and when.

A correction is still checked like any other reading: on e-invoices the IRN is recomputed from the corrected
number, so a typo by the reviewer is caught instead of being learned.
"""
from __future__ import annotations

import re

from . import store
from .extract_rules import _clean, _find_label, _line_conf, _value_below, _value_right
from .reader import Document
from .utils import parse_date

LEARNABLE = ("invoice_number", "invoice_date", "due_date", "po_number")


def _label_left(line, idx):
    out = []
    for w in reversed(line.words[:idx]):
        if out and out[-1].x0 - w.x1 > 30:
            break
        out.append(w)
        if len(out) == 4:
            break
    out.reverse()
    text = " ".join(w.text for w in out).strip(" :.#-")
    return text if re.search(r"[A-Za-zऀ-ॿ]{2,}", text) else None


def locate(doc: Document, field: str, value: str):
    """Find the printed value; return (label, position, words) or (None, None, None)."""
    for li, line in enumerate(doc.lines):
        words = line.words
        for i in range(len(words)):
            for n in (1, 2, 3):
                chunk = words[i:i + n]
                if len(chunk) < n:
                    break
                txt = " ".join(w.text for w in chunk).strip(" :,")
                hit = (txt == value) if field in ("invoice_number", "po_number") else \
                    (parse_date(txt.replace(" ,", ",")) and parse_date(txt.replace(" ,", ",")).isoformat() == value)
                if not hit:
                    continue
                label = _label_left(line, i)
                if label:
                    return label, "right", chunk
                if li > 0:  # label printed above the value
                    above = [w for w in doc.lines[li - 1].words if abs(w.x0 - chunk[0].x0) < 40]
                    if above:
                        return " ".join(w.text for w in above[:3]).strip(" :.#-"), "below", chunk
                return None, None, chunk
    return None, None, None


def learn(doc: Document, vendor_id: str, corrections: dict, by: str) -> list[dict]:
    learned = []
    for field, value in corrections.items():
        if field not in LEARNABLE or not value:
            continue
        label, pos, _ = locate(doc, field, value)
        if label:
            store.remember_layout(vendor_id, field, label, pos, by)
            learned.append(dict(field=field, label=label, position=pos))
    return learned


def apply_learned(doc: Document, f: dict, vendor_id: str | None) -> list[str]:
    """Fill fields the standard labels missed, using labels learned for this vendor."""
    notes = []
    for field, m in store.layout_memory(vendor_id).items():
        if f.get(field, {}).get("value") is not None:
            continue
        pat = r"\s*".join(re.escape(t) for t in m["label"].split()) + r"\s*[:.#]?"
        for li, line in enumerate(doc.lines):
            hit = _find_label(line, pat)
            if not hit:
                continue
            s, e = hit
            vals = _value_right(line, e) if m["position"] == "right" else _value_below(doc.lines, li, line.words[s:e + 1])
            raw = " ".join(w.text for w in vals)
            val = _clean(field, raw) if vals else None
            if val:
                bw = vals if field not in ("invoice_number", "po_number") else [w for w in vals if re.search(r"\d", w.text)][:1]
                f[field] = dict(value=val, raw=raw, method=f"learned label '{m['label']}' (taught by {m['by']})",
                                confidence=round(min(0.95, _line_conf(vals)), 2), box=doc.box(bw))
                notes.append(f"{field.replace('_', ' ')} read with the label '{m['label']}' learned from {m['by']}")
                break
    return notes


def vendor_from_gstin(gstin: str | None) -> str | None:
    if not gstin:
        return None
    v = next((v for v in store.VENDORS if v["gstin"] == gstin), None)
    return v["id"] if v else None
