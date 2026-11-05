"""Generate 20 messy, realistic vendor invoices into samples_messy/ to stress-test the reader.

Run:  python scripts/make_messy.py

Every vendor, GSTIN, PO and the buyer come from scripts/make_data.py - nothing here is a real company.
One mess per invoice, so when a run fails you know exactly which mess broke it.
Writes samples_messy/EXPECTED.csv with the decision each invoice *should* get.
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_data import BUYER, POS, VBY, build_invoice, draw_irn, inr, ln  # noqa: E402

OUT = ROOT / "samples_messy"
OUT.mkdir(exist_ok=True)
W, H = A4
POBY = {p["po_number"]: p for p in POS}


# ------------------------------------------------------------------------------- fonts
def _font(name: str, path: str, idx: int | None = None) -> str:
    """Register a Windows font; fall back to Helvetica if it isn't there."""
    try:
        pdfmetrics.registerFont(TTFont(name, path, **({"subfontIndex": idx} if idx is not None else {})))
        return name
    except Exception:
        return "Helvetica"


HAND = _font("Hand", "C:/Windows/Fonts/Inkfree.ttf")
DEVA = _font("Deva", "C:/Windows/Fonts/Nirmala.ttc", 0)


# ------------------------------------------------------------------------------- money / words
def money(x: float, style: str = "plain") -> str:
    s = inr(x)
    return {"plain": s, "rs": f"Rs. {s}", "inr": f"INR {s}", "sym": f"\u20b9{s}"}[style]


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")).strip()


def words_inr(x: float) -> str:
    """182900.00 -> 'Rupees One Lakh Eighty-Two Thousand Nine Hundred Only'."""
    rupees, paise = divmod(int(round(x * 100)), 100)
    parts = []
    for div, label in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand"), (100, "Hundred")):
        if rupees >= div:
            parts.append(f"{_two(rupees // div)} {label}")
            rupees %= div
    if rupees:
        parts.append(_two(rupees))
    out = "Rupees " + " ".join(parts) if parts else "Rupees Zero"
    if paise:
        out += f" and {_two(paise)} Paise"
    return out + " Only"


# ------------------------------------------------------------------------------- the drawer
def draw(path: Path, v: dict, inv: dict, o: dict | None = None) -> None:
    """One configurable classic-ish tax invoice. `o` switches on each mess."""
    o = o or {}
    lab = dict(number="Invoice No:", date="Invoice Date:", po="PO Ref:", due="Due Date:",
               desc="Description", hsn="HSN", qty="Qty", rate="Rate", amount="Amount",
               subtotal="Sub Total", total="Grand Total (INR)", billto="Bill To:", title="TAX INVOICE")
    lab.update(o.get("labels") or {})
    style = o.get("money_style", "plain")
    base_font = o.get("font", "Helvetica")
    bold = base_font if base_font != "Helvetica" else "Helvetica-Bold"

    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Invoice {inv['number']}")

    y = H - 50
    c.setFont(bold, 15)
    c.drawString(40, y, v["name"])
    c.setFont(bold, 13)
    c.drawRightString(W - 40, y, lab["title"])
    c.setFont(base_font, 9)
    for i, a in enumerate(v["address"]):
        c.drawString(40, y - 15 - 12 * i, a)
    c.drawString(40, y - 15 - 12 * len(v["address"]), f"GSTIN: {o.get('gstin_text') or v['gstin']}")
    c.line(40, y - 60, W - 40, y - 60)

    # meta block
    y -= 80
    meta = [(lab["number"], inv["number"]), (lab["date"], o.get("date_text") or inv["date"])]
    if inv.get("po") and not o.get("hide_po"):
        meta.append((lab["po"], inv["po"]))
    meta.append((lab["due"], o.get("due_text") or inv["due"]))
    c.setFont(base_font, 9.5)
    for i, (k, val) in enumerate(meta):
        c.drawString(W - 240, y - 13 * i, k)
        c.drawString(W - 140, y - 13 * i, str(val))
    c.setFont(bold, 9.5)
    c.drawString(40, y, lab["billto"])
    c.setFont(base_font, 9.5)
    c.drawString(40, y - 13, BUYER["name"])
    for i, a in enumerate(BUYER["address"]):
        c.drawString(40, y - 26 - 13 * i, a)
    c.drawString(40, y - 26 - 13 * len(BUYER["address"]), f"GSTIN: {BUYER['gstin']}")

    # item table (optionally split across two pages)
    y -= 90
    split = o.get("split_after")
    lines = inv["lines"]
    first = lines[:split] if split else lines
    rest = lines[split:] if split else []

    def header(yy):
        c.setFillColor(colors.HexColor("#e8e8e8"))
        c.rect(40, yy - 4, W - 80, 16, fill=1, stroke=0)
        c.setFillColor(colors.black)
        c.setFont(bold, 9)
        c.drawString(42, yy, "#")
        c.drawString(62, yy, lab["desc"])
        c.drawString(302, yy, lab["hsn"])
        for x, h in ((420, lab["qty"]), (480, lab["rate"]), (550, lab["amount"])):
            c.drawRightString(x, yy, h)

    def rows(yy, items, start=1):
        for i, l in enumerate(items, start):
            c.setFont(base_font, 9)
            c.drawString(42, yy, str(i))
            c.drawString(62, yy, l["description"])
            c.drawString(302, yy, l.get("hsn", ""))
            c.setFont(o.get("qty_font", base_font), 10 if o.get("qty_font") else 9)
            c.drawRightString(420, yy, f"{l['qty']:g}")
            c.setFont(base_font, 9)
            c.drawRightString(480, yy, money(l["unit_price"], style))
            c.drawRightString(550, yy, money(l["amount"], style))
            yy -= 15
        return yy

    header(y)
    y = rows(y - 18, first)

    if rest:
        c.setFont(base_font, 8.5)
        c.drawRightString(W - 40, y - 6, "Continued on page 2 ...")
        c.showPage()
        y = H - 60
        c.setFont(bold, 11)
        c.drawString(40, y, f"{v['name']} - {lab['number']} {inv['number']} (page 2 of 2)")
        y -= 30
        header(y)
        y = rows(y - 18, rest, start=len(first) + 1)

    c.line(40, y + 5, W - 40, y + 5)
    y -= 12

    # totals
    c.setFont(base_font, 9)
    tot_rows = []
    if not o.get("no_tax_column"):
        tot_rows.append((lab["subtotal"], inv["subtotal"]))
        tot_rows += list(inv["taxes"])
    if o.get("roundoff"):
        tot_rows.append(("Round Off", o["roundoff"]))
    for k, val in tot_rows:
        c.drawRightString(470, y, k)
        c.drawRightString(550, y, money(val, style) if val >= 0 else f"({money(abs(val), style)})")
        y -= 14
    c.setFont(bold, 10.5)
    c.drawRightString(470, y - 2, lab["total"])
    c.drawRightString(550, y - 2, money(inv["total"], style))
    y -= 20

    if o.get("in_words"):
        c.setFont(base_font, 8.5)
        c.drawString(40, y, f"Amount in words: {words_inr(inv['total'])}")
        y -= 14
    if o.get("inclusive_note"):
        c.setFont(base_font, 8.5)
        c.drawString(40, y, o["inclusive_note"])
        y -= 14
    if o.get("footnote"):
        c.setFont(base_font, 8.5)
        c.drawString(40, 132, o["footnote"])

    # stamp + signature scribble over the total
    if o.get("stamp"):
        c.saveState()
        c.translate(430, y + 58)
        c.rotate(-13)
        c.setStrokeColor(colors.HexColor("#1f4fa0"))
        c.setFillColor(colors.HexColor("#1f4fa0"))
        c.setLineWidth(2)
        c.roundRect(-62, -16, 124, 34, 5, fill=0, stroke=1)
        c.setFont("Helvetica-Bold", 15)
        c.drawCentredString(0, -3, "RECEIVED")
        c.setFont("Helvetica", 6.5)
        c.drawCentredString(0, -13, "MERIDIAN FOODS - STORES")
        c.restoreState()
        c.setStrokeColor(colors.HexColor("#111a55"))
        c.setLineWidth(1.4)
        p = c.beginPath()
        p.moveTo(400, y + 24)
        p.curveTo(430, y + 54, 455, y - 4, 486, y + 34)
        p.curveTo(505, y + 56, 520, y + 6, 548, y + 40)
        c.drawPath(p)
        c.setStrokeColor(colors.black)

    c.setFont(base_font, 8.5)
    c.drawString(40, 90, f"Bank: {v['bank']}   |   Payment terms: {v['terms_days']} days")
    c.drawString(40, 76, "This is a computer generated invoice.")
    c.drawRightString(W - 40, 76, f"For {v['name']}")
    if not o.get("no_irn"):
        draw_irn(c, inv, v, 40, 118, W - 104, 96)
    c.save()


def draw_grid(path: Path, v: dict, inv: dict) -> None:
    """Mess 5: a layout the reader has never seen - items as a 2-column grid, labels on the right."""
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Invoice {inv['number']}")
    c.setFillColor(colors.HexColor("#f4f4f0"))
    c.rect(0, H - 120, W, 120, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(44, H - 56, v["name"])
    c.setFont("Helvetica", 9)
    c.drawString(44, H - 72, ", ".join(v["address"]))
    c.drawString(44, H - 85, f"GSTIN {v['gstin']}")

    # labels on the RIGHT, value first then label underneath
    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(W - 44, H - 56, "INVOICE")
    c.setFont("Helvetica", 9)
    for i, (val, k) in enumerate([(inv["number"], "invoice number"), (inv["date"], "dated"),
                                  (inv["po"], "against order"), (inv["due"], "payable by")]):
        c.setFont("Helvetica-Bold", 9.5)
        c.drawRightString(W - 44, H - 78 - 24 * i, str(val))
        c.setFont("Helvetica", 7.5)
        c.setFillColor(colors.HexColor("#666666"))
        c.drawRightString(W - 44, H - 88 - 24 * i, k)
        c.setFillColor(colors.black)

    y = H - 210
    c.setFont("Helvetica", 9.5)
    c.drawString(44, y, "Supplied to")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(44, y - 14, BUYER["name"])
    c.setFont("Helvetica", 9)
    c.drawString(44, y - 28, BUYER["address"][0])
    c.drawString(44, y - 40, BUYER["address"][1])
    c.drawString(44, y - 52, f"GSTIN {BUYER['gstin']}")

    # two items side by side, as cards
    y -= 90
    for i, l in enumerate(inv["lines"]):
        x = 44 + (i % 2) * 262
        yy = y - (i // 2) * 96
        c.setStrokeColor(colors.HexColor("#cccccc"))
        c.rect(x, yy - 76, 246, 86, fill=0, stroke=1)
        c.setStrokeColor(colors.black)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x + 10, yy - 6, l["description"][:40])
        c.setFont("Helvetica", 8.5)
        c.drawString(x + 10, yy - 24, f"HSN {l.get('hsn', '')}")
        for j, (k, val) in enumerate([("quantity", f"{l['qty']:g}"), ("rate", inr(l["unit_price"])),
                                      ("line amount", inr(l["amount"]))]):
            c.drawString(x + 10, yy - 40 - 12 * j, k)
            c.drawRightString(x + 236, yy - 40 - 12 * j, val)

    y -= 96 * ((len(inv["lines"]) + 1) // 2) + 20
    for k, val in [("taxable value", inv["subtotal"])] + list(inv["taxes"]):
        c.setFont("Helvetica", 9)
        c.drawRightString(W - 120, y, k)
        c.drawRightString(W - 44, y, inr(val))
        y -= 14
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(W - 120, y - 4, "total payable")
    c.drawRightString(W - 44, y - 4, inr(inv["total"]))
    c.setFont("Helvetica", 8.5)
    c.drawString(44, 90, f"Bank: {v['bank']}   |   Terms: {v['terms_days']} days")
    draw_irn(c, inv, v, 44, 124, W - 108, 100)
    c.save()


def draw_hinglish(path: Path, v: dict, inv: dict) -> None:
    """Mess 19: Hindi and English labels mixed on one page."""
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Invoice {inv['number']}")
    y = H - 50
    c.setFont(DEVA, 15)
    c.drawString(40, y, v["name"])
    c.setFont(DEVA, 13)
    c.drawRightString(W - 40, y, "\u0915\u0930 \u092c\u0940\u091c\u0915 / TAX INVOICE")
    c.setFont(DEVA, 9)
    for i, a in enumerate(v["address"]):
        c.drawString(40, y - 15 - 12 * i, a)
    c.drawString(40, y - 15 - 12 * len(v["address"]), f"GSTIN: {v['gstin']}")
    c.line(40, y - 60, W - 40, y - 60)

    y -= 80
    meta = [("\u092c\u0940\u091c\u0915 \u0938\u0902. / Invoice No:", inv["number"]),
            ("\u0926\u093f\u0928\u093e\u0902\u0915 / Date:", inv["date"]),
            ("\u0915\u094d\u0930\u092f \u0906\u0926\u0947\u0936 / PO Ref:", inv["po"]),
            ("\u0926\u0947\u092f \u0924\u093f\u0925\u093f / Due Date:", inv["due"])]
    c.setFont(DEVA, 9.5)
    for i, (k, val) in enumerate(meta):
        c.drawString(W - 280, y - 13 * i, k)
        c.drawString(W - 130, y - 13 * i, str(val))
    c.drawString(40, y, "\u092a\u094d\u0930\u093e\u092a\u094d\u0924\u0915\u0930\u094d\u0924\u093e / Bill To:")
    c.drawString(40, y - 13, BUYER["name"])
    for i, a in enumerate(BUYER["address"]):
        c.drawString(40, y - 26 - 13 * i, a)
    c.drawString(40, y - 26 - 13 * len(BUYER["address"]), f"GSTIN: {BUYER['gstin']}")

    y -= 90
    c.setFillColor(colors.HexColor("#e8e8e8"))
    c.rect(40, y - 4, W - 80, 16, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont(DEVA, 8.5)
    c.drawString(42, y, "\u0915\u094d\u0930.")
    c.drawString(62, y, "\u0935\u093f\u0935\u0930\u0923 / Description")
    c.drawString(302, y, "HSN")
    for x, h in ((420, "\u092e\u093e\u0924\u094d\u0930\u093e / Qty"), (480, "\u0926\u0930 / Rate"),
                 (550, "\u0930\u093e\u0936\u093f / Amount")):
        c.drawRightString(x, y, h)
    y -= 18
    for i, l in enumerate(inv["lines"], 1):
        c.setFont(DEVA, 9)
        c.drawString(42, y, str(i))
        c.drawString(62, y, l["description"])
        c.drawString(302, y, l.get("hsn", ""))
        c.drawRightString(420, y, f"{l['qty']:g}")
        c.drawRightString(480, y, inr(l["unit_price"]))
        c.drawRightString(550, y, inr(l["amount"]))
        y -= 15
    c.line(40, y + 5, W - 40, y + 5)
    y -= 12
    hi = {"CGST": "\u0938\u0940\u091c\u0940\u0910\u0938\u091f\u0940", "SGST": "\u0910\u0938\u091c\u0940\u0910\u0938\u091f\u0940",
          "IGST": "\u0906\u0908\u091c\u0940\u0910\u0938\u091f\u0940"}
    rows = [("\u0915\u0930 \u092f\u094b\u0917\u094d\u092f \u092e\u0942\u0932\u094d\u092f / Taxable Value", inv["subtotal"])]
    for k, val in inv["taxes"]:
        rows.append((f"{hi.get(k.split()[0], k.split()[0])} {k.split('@')[1].strip() if '@' in k else ''} / {k}", val))
    c.setFont(DEVA, 9)
    for k, val in rows:
        c.drawRightString(455, y, k)
        c.drawRightString(550, y, inr(val))
        y -= 14
    c.setFont(DEVA, 10.5)
    c.drawRightString(455, y - 2, "\u0915\u0941\u0932 \u092f\u094b\u0917 / Grand Total (INR)")
    c.drawRightString(550, y - 2, inr(inv["total"]))
    c.setFont(DEVA, 8.5)
    c.drawString(40, 90, f"\u092c\u0948\u0902\u0915 / Bank: {v['bank']}")
    c.drawString(40, 76, f"For {v['name']}")
    draw_irn(c, inv, v, 40, 118, W - 104, 96)
    c.save()


# ------------------------------------------------------------------------------- paper effects
def _render(src: Path, dpi: int) -> Image.Image:
    return pdfium.PdfDocument(str(src))[0].render(scale=dpi / 72).to_pil().convert("L")


def _save(img: Image.Image, out: Path, dpi: int, quality: int = 50) -> None:
    buf = io.BytesIO()
    img.convert("L").save(buf, "JPEG", quality=quality)
    buf.seek(0)
    Image.open(buf).convert("L").save(out, "PDF", resolution=dpi)


def fx_skew(src: Path, out: Path, dpi=150, deg=4.0, seed=11) -> None:
    """Mess 1: page fed in crooked, grey background noise."""
    rnd = np.random.default_rng(seed)
    img = _render(src, dpi).rotate(deg, resample=Image.BICUBIC, expand=True, fillcolor=236)
    arr = np.asarray(img).astype(np.float32) * 0.90 + 22
    arr += rnd.normal(0, 13, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.4))
    _save(img, out, dpi, 55)


def fx_fax(src: Path, out: Path, dpi=100, seed=12) -> None:
    """Mess 2: a 100 dpi black-and-white fax with speckles."""
    rnd = np.random.default_rng(seed)
    img = _render(src, dpi).rotate(0.8, resample=Image.BICUBIC, expand=True, fillcolor=255)
    arr = np.asarray(img).astype(np.float32)
    arr += rnd.normal(0, 26, arr.shape)
    bw = (arr > 142).astype(np.uint8) * 255
    salt = rnd.random(bw.shape)
    bw[salt < 0.012] = 0
    bw[salt > 0.992] = 255
    _save(Image.fromarray(bw).filter(ImageFilter.GaussianBlur(0.3)), out, dpi, 40)


def fx_stain(src: Path, out: Path, dpi=200, seed=13) -> None:
    """Mess 3: a coffee ring / dark blob over the tax table, totals still legible."""
    rnd = np.random.default_rng(seed)
    img = _render(src, dpi).convert("RGB")
    w, h = img.size
    lay = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(lay)
    cx, cy = int(w * 0.78), int(h * 0.345)         # over the Sub Total / CGST / SGST rows
    d.ellipse([cx - int(w * 0.16), cy - int(h * 0.028), cx + int(w * 0.16), cy + int(h * 0.028)], fill=150)
    for _ in range(26):                             # splatter
        rx, ry = rnd.integers(-int(w * 0.18), int(w * 0.18)), rnd.integers(-int(h * 0.035), int(h * 0.035))
        r = int(rnd.integers(4, 20))
        d.ellipse([cx + rx - r, cy + ry - r, cx + rx + r, cy + ry + r], fill=int(rnd.integers(80, 190)))
    lay = lay.filter(ImageFilter.GaussianBlur(int(w * 0.004)))
    stain = Image.new("RGB", (w, h), (94, 58, 26))
    img = Image.composite(stain, img, lay.point(lambda p: int(p * 0.82)))
    arr = np.asarray(img.convert("L")).astype(np.float32) + rnd.normal(0, 7, (h, w))
    _save(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)), out, dpi, 60)


def fx_photo(src: Path, out: Path, dpi=200, seed=14) -> None:
    """Mess 20: a phone photo - perspective tilt, uneven light, shadow in one corner."""
    rnd = np.random.default_rng(seed)
    img = _render(src, dpi)
    w, h = img.size
    # perspective: solve for the transform that maps the quad back to the rectangle
    dx, dy = w * 0.055, h * 0.035
    dst = [(0, 0), (w, 0), (w, h), (0, h)]
    src_q = [(dx * 1.1, dy * 0.4), (w - dx * 0.3, dy * 1.5), (w - dx * 1.3, h - dy * 0.5), (dx * 0.2, h - dy * 1.6)]
    A, b = [], []
    for (X, Y), (x, y) in zip(dst, src_q):
        A += [[X, Y, 1, 0, 0, 0, -x * X, -x * Y], [0, 0, 0, X, Y, 1, -y * X, -y * Y]]
        b += [x, y]
    coeffs = np.linalg.solve(np.array(A, dtype=np.float64), np.array(b, dtype=np.float64))
    img = img.transform((w, h), Image.PERSPECTIVE, tuple(coeffs), Image.BICUBIC, fillcolor=210)

    arr = np.asarray(img).astype(np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    light = 1.0 - 0.34 * ((xx / w) ** 1.5) - 0.12 * ((yy / h) ** 2)        # uneven lighting
    corner = np.sqrt(((xx - w) / w) ** 2 + ((yy - h) / h) ** 2)
    light *= np.clip(0.42 + 0.58 * np.clip(corner, 0, 1) ** 0.6, 0, 1)      # shadow bottom-right
    arr = arr * light + 26
    arr += rnd.normal(0, 6, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6))
    _save(img, out, dpi, 55)


# ------------------------------------------------------------------------------- the 20 invoices
def po_lines(po_number: str, qty_scale: float = 1.0):
    """Invoice lines that match a real PO, so the 3-way match can succeed."""
    out = []
    for l in POBY[po_number]["lines"]:
        q = l["qty"] * qty_scale
        out.append(ln(l["description"], int(q) if q == int(q) else q, l["unit_price"], l["hsn"]))
    return out


def build() -> list[dict]:
    rows: list[dict] = []
    tmp = OUT / "_tmp.pdf"

    def rec(file, mess, wrong, expect, reason):
        rows.append(dict(file=file, mess_type=mess, what_is_wrong=wrong, expected_decision=expect, reason=reason))

    # ---- 1. crooked scan + grey noise
    v = VBY["V001"]
    inv = build_invoice("V001", "SPS/26-27/0455", "2026-10-28", "2026-11-27", "PO-2026-0109",
                        po_lines("PO-2026-0109", 0.5))
    draw(tmp, v, inv)
    fx_skew(tmp, OUT / "m01_skew_noise.pdf")
    rec("m01_skew_noise.pdf", "crooked scan", "page rotated 4 degrees, grey background noise, no text layer",
        "REVIEW", "OCR on a skewed noisy scan is unsure of at least one key field; the app holds rather than guess")

    # ---- 2. 100 dpi black-and-white fax
    inv = build_invoice("V002", "KIS/2026/0944", "2026-10-26", "2026-12-10", "PO-2026-0106",
                        po_lines("PO-2026-0106"))
    draw(tmp, VBY["V002"], inv)
    fx_fax(tmp, OUT / "m02_fax_100dpi.pdf")
    rec("m02_fax_100dpi.pdf", "fax quality", "100 dpi, 1-bit black and white, salt-and-pepper speckles",
        "REVIEW", "worst-case OCR; fields that cannot be read confidently go to a person")

    # ---- 3. coffee stain over the tax table
    inv = build_invoice("V001", "SPS/26-27/0456", "2026-10-29", "2026-11-28", "PO-2026-0101",
                        po_lines("PO-2026-0101", 0.4))
    draw(tmp, v, inv)
    fx_stain(tmp, OUT / "m03_coffee_stain.pdf")
    rec("m03_coffee_stain.pdf", "stain over tax table", "dark blob covers the CGST/SGST rows; grand total readable",
        "REVIEW", "tax lines are obscured, so the invoice cannot be proved to agree with itself")

    # ---- 4. RECEIVED stamp + signature over the total
    inv = build_invoice("V007", "ME/2026/0401", "2026-10-27", "2026-11-26", "PO-2026-0107",
                        po_lines("PO-2026-0107"))
    draw(tmp, VBY["V007"], inv, dict(stamp=True))
    _save(_render(tmp, 200), OUT / "m04_stamp_signature.pdf", 200, 62)
    rec("m04_stamp_signature.pdf", "stamp and signature", "'RECEIVED' stamp and a pen scribble cross the total line",
        "REVIEW", "ink crosses the grand total, so the total is not reliably readable")

    # ---- 5. unfamiliar layout: grid of item cards, labels on the right
    inv = build_invoice("V005", "APX/2026/0310", "2026-10-25", "2026-12-24", "PO-2026-0105",
                        po_lines("PO-2026-0105"))
    draw_grid(OUT / "m05_grid_layout.pdf", VBY["V005"], inv)
    rec("m05_grid_layout.pdf", "unfamiliar layout", "items drawn as side-by-side cards, labels to the right of values",
        "APPROVE", "digital text layer and a real PO match; only the arrangement is new")

    # ---- 6. unusual label wording
    inv = build_invoice("V002", "KIS/2026/0945", "2026-10-24", "2026-12-08", "PO-2026-0110",
                        po_lines("PO-2026-0110", 2 / 3))
    draw(OUT / "m06_odd_labels.pdf", VBY["V002"], inv,
         dict(labels={"number": "Bill No.", "date": "Document Date", "po": "Tax Inv. Ref", "due": "Pay By",
                      "subtotal": "Taxable Amt", "total": "Net Payable (INR)", "billto": "Customer:"}))
    rec("m06_odd_labels.pdf", "unusual labels", "'Bill No.', 'Document Date', 'Tax Inv. Ref' instead of the usual words",
        "APPROVE", "the values are all present and the maths holds; only the label wording differs")

    # ---- 7. three different date formats on one page
    inv = build_invoice("V003", "BLL/26-27/0712", "2026-11-04", "2026-12-04", "PO-2026-0111",
                        po_lines("PO-2026-0111"))
    draw(OUT / "m07_mixed_dates.pdf", VBY["V003"], inv,
         dict(date_text="04-Nov-26", due_text="4/12/2026",
              footnote="Service period: November 4th 2026 to December 4th 2026"))
    rec("m07_mixed_dates.pdf", "mixed date formats", "'04-Nov-26', '4/12/2026' and 'November 4th 2026' on one page",
        "APPROVE", "all three are unambiguous; the parser should normalise them")

    # ---- 8. mixed currency notation
    inv = build_invoice("V001", "SPS/26-27/0457", "2026-10-30", "2026-11-29", "PO-2026-0109",
                        po_lines("PO-2026-0109"))
    draw(OUT / "m08_currency_mix.pdf", v, inv, dict(money_style="rs"))
    rec("m08_currency_mix.pdf", "currency notation", "lakh-style commas with 'Rs.' prefixes instead of a bare number",
        "APPROVE", "Indian digit grouping and an 'Rs.' prefix are both ordinary")

    # ---- 9. total repeated in words
    inv = build_invoice("V002", "KIS/2026/0946", "2026-10-23", "2026-12-07", "PO-2026-0106",
                        po_lines("PO-2026-0106", 0.5))
    draw(OUT / "m09_total_in_words.pdf", VBY["V002"], inv, dict(in_words=True))
    rec("m09_total_in_words.pdf", "amount in words", "the grand total also written out as words underneath",
        "APPROVE", "the words agree with the figure; extra text should not confuse the reader")

    # ---- 10. round-off line
    base = build_invoice("V007", "ME/2026/0402", "2026-10-22", "2026-11-21", "PO-2026-0107",
                         po_lines("PO-2026-0107", 0.5))
    base["total"] = round(base["total"] - 0.40, 2)
    draw(OUT / "m10_roundoff.pdf", VBY["V007"], base, dict(roundoff=-0.40))
    rec("m10_roundoff.pdf", "round-off line", "a '-0.40' round-off makes subtotal + tax differ from the total",
        "APPROVE", "0.40 is inside the 1 rupee rounding tolerance, and the round-off line explains it")

    # ---- 11. handwritten-looking quantities
    inv = build_invoice("V005", "APX/2026/0311", "2026-10-21", "2026-12-20", "PO-2026-0105",
                        po_lines("PO-2026-0105", 0.5))
    draw(OUT / "m11_handwritten_qty.pdf", VBY["V005"], inv, dict(qty_font=HAND))
    rec("m11_handwritten_qty.pdf", "handwritten quantities", "quantities set in a handwriting font",
        "APPROVE", "still a digital text layer, so the characters are exact even though they look handwritten")

    # ---- 12. two pages, items continue overleaf
    inv = build_invoice("V002", "KIS/2026/0947", "2026-10-20", "2026-12-04", "PO-2026-0106",
                        po_lines("PO-2026-0106"))
    draw(OUT / "m12_two_pages.pdf", VBY["V002"], inv, dict(split_after=1))
    rec("m12_two_pages.pdf", "two pages", "line items continue on page 2 and the totals are only on page 2",
        "APPROVE", "all pages of a digital PDF are read (up to 5), so the lines on page 2 and the totals are found and add up")

    # ---- 13. GST-inclusive prices, no tax column
    inv = build_invoice("V005", "APX/2026/0312", "2026-10-19", "2026-12-18", "PO-2026-0105",
                        po_lines("PO-2026-0105", 0.25), tax_inclusive=True)
    draw(OUT / "m13_tax_inclusive.pdf", VBY["V005"], inv,
         dict(no_tax_column=True, inclusive_note="All prices are inclusive of GST @ 18%."))
    rec("m13_tax_inclusive.pdf", "tax-inclusive prices", "no separate tax column; GST is already inside the prices",
        "APPROVE", "the note states the rate, so the tax can be backed out of the total")

    # ---- 14. IGST because the vendor is in another state
    inv = build_invoice("V003", "BLL/26-27/0713", "2026-10-18", "2026-11-17", "PO-2026-0102",
                        po_lines("PO-2026-0102"))
    draw(OUT / "m14_igst_interstate.pdf", VBY["V003"], inv)
    rec("m14_igst_interstate.pdf", "IGST not CGST+SGST", "a single IGST line instead of the usual CGST/SGST pair",
        "APPROVE", "the vendor is in Karnataka and we are in Tamil Nadu, so IGST is the correct tax")

    # ---- 15. PO only in a footnote
    inv = build_invoice("V002", "KIS/2026/0948", "2026-10-17", "2026-12-01", "PO-2026-0106",
                        po_lines("PO-2026-0106", 0.5))
    draw(OUT / "m15_po_in_footnote.pdf", VBY["V002"], inv,
         dict(hide_po=True, footnote="Ref: your order 2026/0106 dated 08-Oct-2026"))
    rec("m15_po_in_footnote.pdf", "PO hidden in a footnote", "no PO field; the order is mentioned as 'your order 2026/0106'",
        "REVIEW", "a PO the app had to infer is never auto-approved, by design")

    # ---- 16. the vendor's own arithmetic is wrong
    bad = build_invoice("V001", "SPS/26-27/0458", "2026-10-16", "2026-11-15", "PO-2026-0101",
                        po_lines("PO-2026-0101", 0.4))
    bad["lines"][0]["amount"] = round(bad["lines"][0]["amount"] + 9000, 2)   # qty x rate no longer equals amount
    bad["subtotal"] = round(sum(l["amount"] for l in bad["lines"]), 2)
    bad["taxes"] = [(k, round(bad["subtotal"] * 0.09, 2)) for k, _ in bad["taxes"]]
    bad["total"] = round(bad["subtotal"] + sum(a for _, a in bad["taxes"]), 2)
    draw(OUT / "m16_bad_arithmetic.pdf", v, bad)
    rec("m16_bad_arithmetic.pdf", "vendor's maths is wrong", "line 1: quantity x rate does not equal the printed amount",
        "REJECT", "a digital PDF is never repaired - a broken identity there is the vendor's error")

    # ---- 17. GSTIN with an OCR-trap typo
    inv = build_invoice("V008", "SGT/2026/0189", "2026-10-15", "2026-11-14", "PO-2026-0113",
                        po_lines("PO-2026-0113", 0.5))
    trap = VBY["V008"]["gstin"].replace("0", "O", 1).replace("1", "I", 1)   # this GSTIN has both a 0 and a 1
    draw(OUT / "m17_gstin_typo.pdf", VBY["V008"], inv, dict(gstin_text=trap))
    rec("m17_gstin_typo.pdf", "GSTIN letter/digit confusion", f"GSTIN printed as {trap} (O for 0, I for 1)",
        "REVIEW", "the 'O' sits in a digit-only position and is repaired, but the 'I' sits in the entity-code position "
                  "where a letter is also legal, so the checksum cannot prove a repair and a person is asked")

    # ---- 18. duplicate trap: same invoice, number dressed up
    dup = build_invoice("V001", "INV - 0042 / A", "2026-10-12", "2026-11-11", "PO-2026-0098",
                        [ln("Printed product labels 100x50mm", 20000, 1.75, "4821")])
    draw(OUT / "m18_duplicate_trap.pdf", v, dup)
    rec("m18_duplicate_trap.pdf", "duplicate in disguise", "re-bills INV-0042 as 'INV - 0042 / A', same date and amount",
        "REVIEW", "the '/A' suffix defeats the exact-number check, so only the amount+date fallback catches it as a possible duplicate")

    # ---- 19. Hindi and English labels mixed
    inv = build_invoice("V008", "SGT/2026/0188", "2026-10-14", "2026-11-13", "PO-2026-0113",
                        po_lines("PO-2026-0113"))
    draw_hinglish(OUT / "m19_hindi_english.pdf", VBY["V008"], inv)
    rec("m19_hindi_english.pdf", "bilingual labels", "every label given in Hindi and English together",
        "APPROVE", "digital text layer; the English half of each label is enough to find the fields")

    # ---- 20. phone photo of a printed invoice
    inv = build_invoice("V007", "ME/2026/0403", "2026-10-13", "2026-11-12", "PO-2026-0107",
                        po_lines("PO-2026-0107", 0.5))
    draw(tmp, VBY["V007"], inv)
    fx_photo(tmp, OUT / "m20_photo_perspective.pdf")
    rec("m20_photo_perspective.pdf", "phone photo", "perspective tilt, uneven lighting, shadow in the bottom-right corner",
        "REVIEW", "a photo is the hardest input; unreadable fields go to a person rather than being guessed")

    tmp.unlink(missing_ok=True)
    return rows


def main() -> None:
    rows = build()
    with (OUT / "EXPECTED.csv").open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=["file", "mess_type", "what_is_wrong", "expected_decision", "reason"])
        wr.writeheader()
        wr.writerows(rows)
    pdfs = sorted(p.name for p in OUT.glob("*.pdf"))
    print(f"wrote {len(pdfs)} PDFs and EXPECTED.csv to {OUT}")
    for r in rows:
        print(f"  {r['file']:32s} {r['expected_decision']:8s} {r['mess_type']}")
    missing = [r["file"] for r in rows if not (OUT / r["file"]).exists()]
    if missing:
        raise SystemExit("MISSING: " + ", ".join(missing))


if __name__ == "__main__":
    main()
