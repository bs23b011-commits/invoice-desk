"""One PDF, several invoices. Vendors send a month's invoices as one file; a scanner stacks a scan and a
digital printout together; a mailbox forwards two vendors' bills in one attachment.

Before anything else, every page is looked at on its own (its text layer if it has one, OCR if it's a scan) to
find where each invoice starts: a page carrying a new invoice number, or another vendor's GSTIN under an invoice
heading. Pages without either continue the invoice before them. Each invoice then becomes its own PDF and is
checked fully on its own, against its own PO. Invoices from different vendors in one file are unusual, so
each of them is held for a person to look at even if every check passes.
"""
from __future__ import annotations

import io
import re

import pdfplumber
import pypdfium2 as pdfium

from . import store

MAX_SPLIT_PAGES = 10  # longer files go through as one document and are held for a person
HEADING_RE = re.compile(r"\b(tax\s+invoice|invoice)\b", re.I)


def _norm_number(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def page_facts(pdf_bytes: bytes, i: int, scanned: bool) -> dict:
    """Read one page on its own, the way the reader reads any invoice: text layer if it has one, OCR if it's a
    scan. The same extraction rules then say whose invoice it is, its number and whether it has a total."""
    from . import extract_rules
    from .reader import read_pdf
    try:
        doc = read_pdf(subset(pdf_bytes, [i]), preview=False)
        f = extract_rules.extract(doc, store.BUYER["gstin"])
        text = doc.text
    except Exception:  # e.g. no OCR on this machine: the page continues the invoice before it, and the reader flags it
        return dict(index=i, scanned=scanned, number=None, gstin=None, total=None, heading=False, vendor="")
    val = lambda k: (f.get(k) or {}).get("value") if isinstance(f.get(k), dict) else f.get(k)
    number = val("invoice_number")
    gstin = val("vendor_gstin")
    # a heading is a short line that says INVOICE, not a sentence that mentions one ("limited to the invoice value")
    head = "\n".join(l for l in text.splitlines()[:12] if len(l.split()) <= 8 or re.search(r"\bTAX INVOICE\b|\bINVOICE\b", l))
    v = next((v for v in store.VENDORS if gstin and v["gstin"] == gstin), None)
    return dict(index=i, scanned=scanned, number=number, gstin=gstin, total=val("total"),
                heading=bool(HEADING_RE.search(head)), vendor=(v["name"] if v else val("vendor_name") or "")[:60])


def analyse(pdf_bytes: bytes) -> dict:
    """Where each invoice starts. Returns {"pages", "parts": [{"pages", "number", "gstin", "vendor", "scanned"}], ...}."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        n = len(pdf.pages)
        if n <= 1 or n > MAX_SPLIT_PAGES:
            return dict(pages=n, parts=[], split=False, too_long=n > MAX_SPLIT_PAGES)
        kinds = [len(pg.chars) < 40 for pg in pdf.pages]
    pages = [page_facts(pdf_bytes, i, kinds[i]) for i in range(n)]
    same = lambda a, b: _norm_number(a) == _norm_number(b)
    parts: list[dict] = []
    for p in pages:
        cur = parts[-1] if parts else None
        starts = cur is None
        if cur and p["number"] and cur["number"] and not same(p["number"], cur["number"]):
            starts = True  # a different invoice number
        elif cur and p["gstin"] and cur["gstin"] and p["gstin"][:12] != cur["gstin"][:12]:
            starts = True  # another vendor's invoice
        elif (cur and cur["total"] and (p["gstin"] or p["heading"]) and not (p["number"] and cur["number"])
              and (p["number"] or p["gstin"] or p["total"])):  # a terms page has none of these: it continues
            starts = True  # the invoice before already ended (it has its total) and a new vendor header begins
        if (not starts and p["number"] and cur["number"] and same(p["number"], cur["number"])
                and p["total"] and cur["total"] and abs(p["total"] - cur["total"]) < 1):
            cur["copies"].append(p["index"])  # "Duplicate for transporter": the same invoice printed again
            continue
        if starts:
            parts.append(dict(pages=[p["index"]], number=p["number"], gstin=p["gstin"], scanned=[p["scanned"]],
                              vendor=p["vendor"] or "Unknown vendor", total=p["total"], copies=[]))
        else:
            cur["pages"].append(p["index"])
            cur["scanned"].append(p["scanned"])
            cur["number"] = cur["number"] or p["number"]
            cur["total"] = p["total"] or cur["total"]
            if not cur["gstin"] and p["gstin"]:
                cur["gstin"], cur["vendor"] = p["gstin"], p["vendor"] or cur["vendor"]
    vendors = []
    for part in parts:
        key = (part["gstin"] or part["vendor"])[:12]
        if key not in [k for k, _ in vendors]:
            vendors.append((key, part["vendor"]))
    return dict(pages=n, parts=parts, split=len(parts) > 1 or any(q["copies"] for q in parts),
                mixed=len(vendors) > 1, vendors=[name for _, name in vendors], too_long=False)


def subset(pdf_bytes: bytes, pages: list[int]) -> bytes:
    src = pdfium.PdfDocument(pdf_bytes)
    out = pdfium.PdfDocument.new()
    out.import_pages(src, pages)
    buf = io.BytesIO()
    out.save(buf)
    return buf.getvalue()


def plan(name: str, pdf_bytes: bytes) -> list[dict]:
    """The PDFs to process: [{"name", "pdf", "batch"}]. One item (batch None) for an ordinary invoice."""
    try:
        a = analyse(pdf_bytes)
    except Exception:  # never let the splitter stop an invoice: fall back to the whole file
        a = dict(split=False)
    if not a.get("split"):
        return [dict(name=name, pdf=pdf_bytes, batch=None)]
    base = name.rsplit(".", 1)[0]
    n = len(a["parts"])
    out = []
    for i, part in enumerate(a["parts"], start=1):
        pages = [p + 1 for p in part["pages"]]
        out.append(dict(
            name=f"{base} - invoice {i} of {n}.pdf" if n > 1 else name, pdf=subset(pdf_bytes, part["pages"]),
            batch=dict(source=name, index=i, count=n, pages=pages, mixed=a["mixed"], vendors=a["vendors"],
                       copies=[p + 1 for p in part["copies"]],
                       vendor=part["vendor"], number=part["number"],
                       kinds=sorted({"scan" if s else "digital" for s in part["scanned"]}),
                       all_kinds=sorted({"scan" if s else "digital" for q in a["parts"] for s in q["scanned"]}))))
    return out


def _pages(ps: list[int]) -> str:
    return f"page {ps[0]}" if len(ps) == 1 else "pages " + ", ".join(map(str, ps))


def announce(batch: dict) -> str:
    """What the agent says the moment the file arrives."""
    n, src = batch["count"], batch["source"]
    pages = batch["pages"]
    copies = (f" {_pages(batch['copies']).capitalize()} {'is a copy' if len(batch['copies']) == 1 else 'are copies'} "
              f"of the same invoice (e.g. duplicate for transporter), so {'it was' if len(batch['copies']) == 1 else 'they were'}"
              f" not counted again.") if batch.get("copies") else ""
    if n == 1:
        return f"{src} contains one invoice ({batch.get('number') or 'number unread'}, {batch['vendor']}).{copies}"
    where = f"page {pages[0]}" if len(pages) == 1 else f"pages {pages[0]}-{pages[-1]}"
    mix = " (scanned and digital pages mixed: each page was read its own way)" if len(batch["all_kinds"]) > 1 else ""
    if batch["mixed"]:
        return (f"{src} contains {n} invoices from {len(batch['vendors'])} different vendors "
                f"({', '.join(batch['vendors'])}){mix}. One vendor doesn't normally send another vendor's invoice, "
                f"so I split them, checked each against its own vendor and PO, and I'm asking a person to look at "
                f"each one. This is invoice {batch['index']} of {n} ({where}, {batch['vendor']}).{copies}")
    return (f"{src} contains {n} invoices from {batch['vendor']}{mix}. I split them and checked each one fully "
            f"against its own PO. This is invoice {batch['index']} of {n} ({where}"
            + (f", {batch['number']}" if batch.get("number") else "") + f").{copies}")
