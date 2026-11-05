"""Generate master data (vendors, POs, invoice history) and sample invoice PDFs.

Run:  python scripts/make_data.py
Everything is fictional. Layouts deliberately differ so the extractor can't rely on one template.
"""
from __future__ import annotations

import hashlib
import io
import json
import random
import sys
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.utils import gstin_checksum  # noqa: E402

DATA = ROOT / "data"
SAMPLES = ROOT / "samples"
DATA.mkdir(exist_ok=True)
SAMPLES.mkdir(exist_ok=True)


def gstin(first14: str) -> str:
    return first14 + gstin_checksum(first14)


BUYER = {
    "name": "Meridian Foods Pvt Ltd",
    "address": ["Plot 14, SIPCOT Industrial Park", "Irungattukottai, Chennai 602117"],
    "gstin": gstin("33AABCM1234F1Z"),
    "state": "Tamil Nadu",
}

VENDORS = [
    dict(id="V001", einvoice=True, msme=False, tds=None, phone="+91 44 2250 4471", name="Sundaram Packaging Solutions Pvt Ltd", aliases=["Sundaram Packaging"],
         gstin=gstin("33AAFCS4821K1Z"), state="Tamil Nadu", city="Chennai", status="approved",
         terms_days=30, email="accounts@sundarampack.example", bank="HDFC ****4471",
         address=["No. 7, Industrial Estate, Guindy", "Chennai 600032"]),
    dict(id="V002", einvoice=True, msme=False, tds=None, phone="+91 422 257 9020", name="Kaveri Industrial Supplies", aliases=["Kaveri Industrial", "KIS"],
         gstin=gstin("33AKLPK7712M1Z"), state="Tamil Nadu", city="Coimbatore", status="approved",
         terms_days=45, email="billing@kaveriind.example", bank="ICICI ****9020",
         address=["212, Avinashi Road, Peelamedu", "Coimbatore 641004"]),
    dict(id="V003", einvoice=True, msme=False, tds="contract", phone="+91 80 4110 1186", name="Brightline Logistics LLP", aliases=["Brightline Logistics"],
         gstin=gstin("29AAKFB3390P1Z"), state="Karnataka", city="Bengaluru", status="approved",
         terms_days=30, email="ar@brightline.example", bank="Axis ****1186",
         address=["4th Floor, Prestige Tower, Hosur Road", "Bengaluru 560068"]),
    dict(id="V004", einvoice=True, msme=False, tds="technical", phone="+91 40 6650 3305", name="Nimbus Cloud Services Pvt Ltd", aliases=["Nimbus Cloud"],
         gstin=gstin("36AADCN5567H1Z"), state="Telangana", city="Hyderabad", status="approved",
         terms_days=15, email="finance@nimbuscloud.example", bank="Kotak ****3305",
         address=["Cyber Towers, HITEC City", "Hyderabad 500081"]),
    dict(id="V005", einvoice=False, msme=True, udyam="UDYAM-MH-26-0041187", tds=None, phone="+91 20 2712 7752", name="Apex Lab Instruments", aliases=["Apex Lab"],
         gstin=gstin("27ABCPA9911L1Z"), state="Maharashtra", city="Pune", status="approved",
         terms_days=60, email="sales@apexlab.example", bank="SBI ****7752",
         address=["S.No. 45, Bhosari MIDC", "Pune 411026"]),
    dict(id="V006", einvoice=False, msme=True, udyam="UDYAM-TN-02-0099310", tds=None, phone="+91 44 2852 6618", name="Rapid Print & Signage", aliases=["Rapid Print"],
         gstin=gstin("33AAMFR2209Q1Z"), state="Tamil Nadu", city="Chennai", status="blocked",
         block_reason="KYC documents expired on 30-Sep-2026; vendor re-verification pending",
         terms_days=30, email="hello@rapidprint.example", bank="IOB ****6618",
         address=["18, Mount Road, Anna Salai", "Chennai 600002"]),
    dict(id="V008", einvoice=False, msme=True, udyam="UDYAM-DL-06-0012844", tds=None, phone="+91 11 2361 5521",
         name="Shree Ganesh Traders", aliases=["श्री गणेश ट्रेडर्स", "Shri Ganesh Traders"],
         gstin=gstin("07ABEFS2207M1Z"), state="Delhi", city="Delhi", status="approved",
         terms_days=30, email="ganeshtraders@example.in", bank="SBI ****5521",
         address=["12, Naya Bazar", "Delhi 110006"]),
    dict(id="V007", einvoice=False, msme=True, udyam="UDYAM-TN-02-0034521", tds="contract", phone="+91 44 2538 2290", name="Murugan Electricals", aliases=["Murugan Electrical Works"],
         gstin=gstin("33ACQPM6634D1Z"), state="Tamil Nadu", city="Chennai", status="approved",
         terms_days=30, email="murugan.elec@example.com", bank="Canara ****2290",
         address=["56, Ritchie Street, Mount Road", "Chennai 600002"]),
]
VBY = {v["id"]: v for v in VENDORS}


def L(desc, qty, price, hsn=""):
    return dict(description=desc, qty=qty, unit_price=price, amount=round(qty * price, 2), hsn=hsn)


POS = [
    dict(po_number="PO-2026-0098", vendor_id="V001", date="2026-09-20", status="open",
         lines=[L("Printed product labels 100x50mm", 20000, 1.75, "4821")]),
    dict(po_number="PO-2026-0101", vendor_id="V001", date="2026-10-03", status="open",
         lines=[L("Corrugated boxes 5-ply 480x320x300", 5000, 18.50, "4819"),
                L("Stretch wrap film roll 500mm", 40, 1250.00, "3920")]),
    dict(po_number="PO-2026-0102", vendor_id="V003", date="2026-10-01", status="open",
         lines=[L("FTL freight Chennai to Bengaluru (32 ft)", 6, 28000.00, "9965"),
                L("Loading and unloading charges", 6, 1500.00, "9967")]),
    dict(po_number="PO-2026-0103", vendor_id="V004", date="2026-04-01", status="open",
         lines=[L("Managed cloud hosting quarterly fee FY26-27", 4, 120000.00, "998315")]),
    dict(po_number="PO-2026-0104", vendor_id="V002", date="2026-09-25", status="open",
         lines=[L("Nitrile safety gloves (pair)", 2000, 85.00, "4015"),
                L("Anti-fog safety goggles", 500, 220.00, "9004")]),
    dict(po_number="PO-2026-0105", vendor_id="V005", date="2026-10-02", status="open",
         lines=[L("Benchtop pH meter with probe", 4, 23500.00, "9027"),
                L("Calibration buffer kit pH 4/7/10", 10, 1800.00, "3822")]),
    dict(po_number="PO-2026-0106", vendor_id="V002", date="2026-10-08", status="open",
         lines=[L("Hydraulic oil ISO VG 68 (20 L can)", 30, 4200.00, "2710"),
                L("Lithium grease cartridge 400 g", 200, 145.00, "3403")]),
    dict(po_number="PO-2026-0107", vendor_id="V007", date="2026-10-06", status="open",
         lines=[L("LED panel light 40W 2x2", 60, 1150.00, "9405"),
                L("MCB 32A double pole", 40, 385.00, "8536"),
                L("Installation labour - packing hall", 1, 12000.00, "9954")]),
    dict(po_number="PO-2026-0108", vendor_id="V006", date="2026-09-10", status="open",
         lines=[L("Vinyl banners 6x3 ft printed", 25, 1400.00, "4911")]),
    dict(po_number="PO-2026-0109", vendor_id="V001", date="2026-10-10", status="open",
         lines=[L("Printed shrink sleeves 250 ml bottle", 30000, 2.40, "3920")]),
    dict(po_number="PO-2026-0110", vendor_id="V002", date="2026-10-12", status="open",
         lines=[L("Industrial safety helmet with chin strap", 300, 450.00, "6506")]),
    dict(po_number="PO-2026-0111", vendor_id="V003", date="2026-10-15", status="open",
         lines=[L("FTL freight Chennai to Hyderabad (32 ft)", 4, 31000.00, "9965")]),
    dict(po_number="PO-2026-0112", vendor_id="V004", date="2026-10-01", status="open",
         lines=[L("Managed backup service - monthly", 12, 15000.00, "998315")]),
    dict(po_number="PO-2026-0113", vendor_id="V008", date="2026-10-14", status="open",
         lines=[L("Jute sacks 50 kg", 1000, 38.00, "6305"), L("Polypropylene twine roll", 50, 120.00, "5607")]),
]

# Goods receipts (GRN) from the warehouse system. Services (HSN 99xx) have none - they aren't "received".
# PO-2026-0110 is deliberately short: 200 of 300 helmets have arrived so far.
GRNS = []
def _grn(po, desc, qty, d):
    GRNS.append(dict(grn=f"GRN-2026-{len(GRNS) + 1:04d}", po_number=po, description=desc, qty=qty, date=d))
for _po in POS:
    for _l in _po["lines"]:
        if _l["hsn"].startswith("99"):
            continue
        _q = 200 if _po["po_number"] == "PO-2026-0110" else _l["qty"]
        _grn(_po["po_number"], _l["description"], _q, "2026-10-20")
# A company that is NOT in the vendor master, imitating Sundaram (V001). Used only to draw sample 11.
IMPOSTOR = dict(id="X", name="Sundaram Packaging Solution Pvt. Ltd", gstin=gstin("33AAFCS4812K1Z"), state="Tamil Nadu",
                city="Chennai", terms_days=30, email="accounts.sundarampack@example.com",
                bank="IDFC ****0931", address=["No. 7, Industrial Estate, Guindy", "Chennai 600032"])
for po in POS:
    po["amount"] = round(sum(l["amount"] for l in po["lines"]), 2)

# Invoices already processed before the demo starts (so edge cases work in any order).
HISTORY = [
    dict(vendor_id="V001", invoice_number="INV-0042", invoice_date="2026-10-12", po_number="PO-2026-0098",
         subtotal=35000.00, tax=6300.00, total=41300.00, status="APPROVE",
         lines=[dict(description="Printed product labels 100x50mm", qty=20000, amount=35000.00)]),
    dict(vendor_id="V002", invoice_number="KIS/2026/0871", invoice_date="2026-10-05", po_number="PO-2026-0104",
         subtotal=168000.00, tax=30240.00, total=198240.00, status="APPROVE",
         lines=[dict(description="Nitrile safety gloves (pair)", qty=1200, amount=102000.00),
                dict(description="Anti-fog safety goggles", qty=300, amount=66000.00)]),
    dict(vendor_id="V004", invoice_number="NCS/INV/2026/117", invoice_date="2026-07-02", po_number="PO-2026-0103",
         subtotal=120000.00, tax=21600.00, total=141600.00, status="APPROVE",
         lines=[dict(description="Managed cloud hosting Q1 (Apr-Jun 2026)", qty=1, amount=120000.00)]),
]

# --------------------------------------------------------------------------------------------
# PDF templates
# --------------------------------------------------------------------------------------------
W, H = A4


def inr(x: float) -> str:  # Indian grouping
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        g = []
        while len(head) > 2:
            g.insert(0, head[-2:]); head = head[:-2]
        if head:
            g.insert(0, head)
        whole = ",".join(g + [tail])
    return f"{whole}.{frac}"


def west(x: float) -> str:
    return f"{x:,.2f}"


def taxes(vendor, subtotal, rate=18):
    if vendor["state"] == BUYER["state"]:
        half = round(subtotal * rate / 200, 2)
        return [(f"CGST @ {rate//2}%", half), (f"SGST @ {rate//2}%", half)]
    return [(f"IGST @ {rate}%", round(subtotal * rate / 100, 2))]


def irn_for(gstin, d_iso, number):
    from datetime import date as _d
    d = _d.fromisoformat(d_iso)
    start = d.year if d.month >= 4 else d.year - 1
    return hashlib.sha256(f"{gstin}{start}-{str(start + 1)[2:]}INV{number}".encode()).hexdigest()


def draw_irn(c, inv, v, x_text, y_text, qr_x, qr_y, font="Helvetica", size=6.5):
    """E-invoice block: IRN, acknowledgement and the signed QR (here: QR of the key fields)."""
    if not inv.get("irn"):
        return
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    c.setFont(font, size)
    c.drawString(x_text, y_text, f"IRN: {inv['irn']}")
    c.drawString(x_text, y_text - size - 2, f"Ack No.: 1126{int(inv['irn'][:8], 16) % 10**11:011d}   Ack Dt.: {inv['date']}")
    q = QrCodeWidget(json.dumps(dict(SellerGstin=v["gstin"], BuyerGstin=BUYER["gstin"], DocNo=inv["number"],
                                     TotInvVal=inv["total"], Irn=inv["irn"]), separators=(",", ":")))
    b = q.getBounds(); w, h = b[2] - b[0], b[3] - b[1]
    d = Drawing(64, 64, transform=[64.0 / w, 0, 0, 64.0 / h, 0, 0]); d.add(q)
    renderPDF.draw(d, c, qr_x, qr_y)


def tpl_classic(path, v, inv, show_po=True):
    """Traditional Indian tax invoice: HSN column, CGST/SGST rows, Indian digit grouping."""
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Invoice {inv['number']}")
    y = H - 50
    c.setFont("Helvetica-Bold", 15); c.drawString(40, y, v["name"])
    c.setFont("Helvetica-Bold", 13); c.drawRightString(W - 40, y, "TAX INVOICE")
    c.setFont("Helvetica", 9)
    for i, a in enumerate(v["address"]):
        c.drawString(40, y - 15 - 12 * i, a)
    c.drawString(40, y - 15 - 12 * len(v["address"]), f"GSTIN: {v['gstin']}")
    c.line(40, y - 60, W - 40, y - 60)

    y -= 80
    c.setFont("Helvetica", 9.5)
    meta = [("Invoice No:", inv["number"]), ("Invoice Date:", inv["date"])]
    if show_po and inv.get("po"):
        meta.append(("PO Ref:", inv["po"]))
    meta.append(("Due Date:", inv["due"]))
    for i, (k, val) in enumerate(meta):
        c.drawString(W - 230, y - 13 * i, k); c.drawString(W - 140, y - 13 * i, val)
    c.setFont("Helvetica-Bold", 9.5); c.drawString(40, y, "Bill To:")
    c.setFont("Helvetica", 9.5)
    c.drawString(40, y - 13, BUYER["name"])
    for i, a in enumerate(BUYER["address"]):
        c.drawString(40, y - 26 - 13 * i, a)
    c.drawString(40, y - 26 - 13 * len(BUYER["address"]), f"GSTIN: {BUYER['gstin']}")

    y -= 90
    cols = [40, 60, 300, 360, 420, 490]
    c.setFillColor(colors.HexColor("#e8e8e8")); c.rect(40, y - 4, W - 80, 16, fill=1, stroke=0)
    c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 9)
    for x, h in zip(cols, ["#", "Description", "HSN", "Qty", "Rate", "Amount"]):
        c.drawString(x + 2, y, h) if h in ("#", "Description", "HSN") else c.drawRightString(x + 60, y, h)
    c.setFont("Helvetica", 9)
    y -= 18
    for i, ln in enumerate(inv["lines"], 1):
        c.drawString(42, y, str(i)); c.drawString(62, y, ln["description"]); c.drawString(302, y, ln.get("hsn", ""))
        q = ln["qty"]; c.drawRightString(420, y, f"{q:g}")
        c.drawRightString(480, y, inr(ln["unit_price"])); c.drawRightString(550, y, inr(ln["amount"]))
        y -= 15
    c.line(40, y + 5, W - 40, y + 5)
    y -= 12
    rows = [("Sub Total", inv["subtotal"])] + inv["taxes"]
    for k, val in rows:
        c.drawRightString(470, y, k); c.drawRightString(550, y, inr(val)); y -= 14
    c.setFont("Helvetica-Bold", 10.5)
    c.drawRightString(470, y - 2, "Grand Total (INR)"); c.drawRightString(550, y - 2, inr(inv["total"]))
    c.setFont("Helvetica", 8.5)
    c.drawString(40, 90, inv.get("bank_text") or f"Bank: {v['bank']}   |   Payment terms: {v['terms_days']} days")
    c.drawString(40, 76, "This is a computer generated invoice.")
    c.drawRightString(W - 40, 76, f"For {v['name']}")
    draw_irn(c, inv, v, 40, 118, W - 104, 96)
    c.save()


def tpl_modern(path, v, inv, show_po=True):
    """Colour banner, label/value grid, Western digit grouping, 'Total due'."""
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Invoice {inv['number']}")
    c.setFillColor(colors.HexColor("#1f3a5f")); c.rect(0, H - 110, W, 110, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 26); c.drawString(40, H - 60, "INVOICE")
    c.setFont("Helvetica-Bold", 12); c.drawRightString(W - 40, H - 45, v["name"])
    c.setFont("Helvetica", 8.5)
    for i, a in enumerate(v["address"] + [f"GSTIN {v['gstin']}"]):
        c.drawRightString(W - 40, H - 60 - 11 * i, a)
    c.setFillColor(colors.black)
    y = H - 145
    grid = [("Invoice #", inv["number"]), ("Issue date", inv["date"])]
    if show_po and inv.get("po"):
        grid.append(("Purchase order", inv["po"]))
    grid.append(("Payment terms", f"Net {v['terms_days']}"))
    for i, (k, val) in enumerate(grid):
        x = 40 + 130 * i
        c.setFont("Helvetica", 8); c.setFillColor(colors.HexColor("#666666")); c.drawString(x, y, k.upper())
        c.setFont("Helvetica-Bold", 10); c.setFillColor(colors.black); c.drawString(x, y - 14, val)
    y -= 50
    c.setFont("Helvetica", 8); c.setFillColor(colors.HexColor("#666666")); c.drawString(40, y, "BILLED TO")
    c.setFillColor(colors.black); c.setFont("Helvetica", 9.5)
    for i, a in enumerate([BUYER["name"]] + BUYER["address"] + [f"GSTIN {BUYER['gstin']}"]):
        c.drawString(40, y - 14 - 12 * i, a)
    y -= 90
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Description"); c.drawRightString(390, y, "Qty")
    c.drawRightString(470, y, "Unit price"); c.drawRightString(555, y, "Line total")
    c.setStrokeColor(colors.HexColor("#1f3a5f")); c.setLineWidth(1.2); c.line(40, y - 5, 555, y - 5)
    c.setFont("Helvetica", 9.5); y -= 20
    for ln in inv["lines"]:
        c.drawString(40, y, ln["description"]); c.drawRightString(390, y, f"{ln['qty']:g}")
        c.drawRightString(470, y, west(ln["unit_price"])); c.drawRightString(555, y, west(ln["amount"]))
        y -= 16
    c.setLineWidth(0.5); c.line(330, y + 4, 555, y + 4); y -= 12
    for k, val in [("Subtotal", inv["subtotal"])] + [(t.replace(" @ ", " (") + ")", a) for t, a in inv["taxes"]]:
        c.drawRightString(470, y, k); c.drawRightString(555, y, west(val)); y -= 15
    c.setFont("Helvetica-Bold", 11.5)
    c.drawRightString(470, y - 4, "Total due (INR)"); c.drawRightString(555, y - 4, west(inv["total"]))
    c.setFont("Helvetica", 8.5); c.setFillColor(colors.HexColor("#666666"))
    c.drawString(40, 80, inv.get("bank_text") or f"Please remit to {v['bank']}. Questions: {v['email']}")
    draw_irn(c, inv, v, 40, 112, W - 104, 70)
    c.save()


def tpl_letter(path, v, inv, show_po=True, heading=None):
    """Small-vendor letter style: PO mentioned in a sentence, 'Rs.' amounts, unit tokens, tax-inclusive option."""
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Bill {inv['number']}")
    y = H - 55
    c.setFont("Times-Bold", 17); c.drawCentredString(W / 2, y, v["name"].upper())
    c.setFont("Times-Roman", 10)
    c.drawCentredString(W / 2, y - 15, ", ".join(v["address"]))
    c.drawCentredString(W / 2, y - 28, f"GSTIN : {v['gstin']}   |   {v['email']}")
    c.line(40, y - 38, W - 40, y - 38)
    y -= 62
    if heading:
        c.setFont("Times-Bold", 12); c.drawCentredString(W / 2, y, heading); y -= 22
    c.setFont("Times-Roman", 11)
    c.drawString(40, y, f"Invoice No. : {inv['number']}"); c.drawRightString(W - 40, y, f"Date : {inv['date']}")
    y -= 26
    c.drawString(40, y, "To,"); c.drawString(40, y - 14, f"The Accounts Department, {BUYER['name']}")
    c.drawString(40, y - 28, ", ".join(BUYER["address"]))
    c.drawString(40, y - 42, f"GSTIN : {BUYER['gstin']}")
    y -= 68
    if show_po and inv.get("po"):
        c.drawString(40, y, f"Against your Purchase Order No. {inv['po']}, we bill you as under:")
    else:
        c.drawString(40, y, "We bill you as under for material supplied:")
    y -= 24
    c.setFont("Times-Bold", 10.5)
    c.drawString(40, y, "Particulars"); c.drawRightString(385, y, "Quantity")
    c.drawRightString(470, y, "Rate (Rs.)"); c.drawRightString(555, y, "Amount (Rs.)")
    c.line(40, y - 5, 555, y - 5); c.setFont("Times-Roman", 10.5); y -= 20
    for ln in inv["lines"]:
        c.drawString(40, y, ln["description"]); c.drawRightString(385, y, f"{ln['qty']:g} {ln.get('unit', 'Nos')}")
        c.drawRightString(470, y, inr(ln["unit_price"])); c.drawRightString(555, y, inr(ln["amount"]))
        y -= 16
    c.line(300, y + 6, 555, y + 6); y -= 12
    if inv.get("tax_inclusive"):
        c.setFont("Times-Bold", 11)
        c.drawRightString(470, y, "Total (inclusive of GST) Rs."); c.drawRightString(555, y, inr(inv["total"]))
        c.setFont("Times-Italic", 10); y -= 16
        t_label, t_amt = inv["taxes"][0]
        c.drawRightString(470, y, f"{t_label.replace(' @ ', ' @')} included in above Rs.")
        c.drawRightString(555, y, inr(t_amt))
    else:
        c.drawRightString(470, y, "Taxable Value Rs."); c.drawRightString(555, y, inr(inv["subtotal"])); y -= 15
        for k, a in inv["taxes"]:
            c.drawRightString(470, y, f"{k} Rs."); c.drawRightString(555, y, inr(a)); y -= 15
        c.setFont("Times-Bold", 11)
        c.drawRightString(470, y, "Net Amount Payable Rs."); c.drawRightString(555, y, inr(inv["total"]))
    c.setFont("Times-Roman", 10)
    c.drawString(40, 110, inv.get("bank_text") or f"Kindly arrange payment within {v['terms_days']} days to {v['bank']}.")
    c.drawRightString(W - 40, 80, f"for {v['name']}"); c.drawRightString(W - 40, 64, "Authorised Signatory")
    draw_irn(c, inv, v, 40, 140, 40, 150, font="Times-Roman", size=7)
    c.save()


def tpl_statement(path, v, inv, show_po=True):
    """Nimbus's billing-system layout: unusual labels ('Document Ref', 'Dated') a generic reader won't know."""
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Statement {inv['number']}")
    c.setFillColor(colors.HexColor("#0f6e6e")); c.rect(40, H - 70, 6, 34, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 16); c.drawString(54, H - 52, v["name"])
    c.setFont("Helvetica", 8.5); c.drawString(54, H - 66, ", ".join(v["address"]) + f"  |  GSTIN {v['gstin']}")
    c.setFont("Helvetica-Bold", 12); c.drawRightString(W - 40, H - 52, "BILLING STATEMENT")
    y = H - 110
    c.setFont("Helvetica", 9.5)
    rows = [("Document Ref:", inv["number"]), ("Dated:", inv["date"]), ("Order Reference:", inv["po"] or "-"),
            ("Customer:", BUYER["name"]), ("Customer GSTIN:", BUYER["gstin"])]
    for i, (k, val) in enumerate(rows):
        c.setFillColor(colors.HexColor("#555555")); c.drawString(40, y - 15 * i, k)
        c.setFillColor(colors.black); c.drawString(150, y - 15 * i, val)
    y -= 100
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Description"); c.drawRightString(380, y, "Qty"); c.drawRightString(470, y, "Rate")
    c.drawRightString(555, y, "Amount")
    c.setStrokeColor(colors.HexColor("#0f6e6e")); c.line(40, y - 5, 555, y - 5)
    c.setFont("Helvetica", 9.5); y -= 20
    for ln_ in inv["lines"]:
        c.drawString(40, y, ln_["description"]); c.drawRightString(380, y, f"{ln_['qty']:g}")
        c.drawRightString(470, y, west(ln_["unit_price"])); c.drawRightString(555, y, west(ln_["amount"]))
        y -= 16
    y -= 8
    for k, val in [("Sub Total", inv["subtotal"])] + inv["taxes"]:
        c.drawRightString(470, y, k); c.drawRightString(555, y, west(val)); y -= 15
    c.setFont("Helvetica-Bold", 11); c.drawRightString(470, y - 3, "Amount payable (INR)"); c.drawRightString(555, y - 3, west(inv["total"]))
    c.setFont("Helvetica", 8); c.setFillColor(colors.HexColor("#555555"))
    c.drawString(40, 80, inv.get("bank_text") or f"Remit to {v['bank']}  |  {v['email']}")
    draw_irn(c, inv, v, 40, 112, W - 104, 70)
    c.save()


DEVA = str.maketrans("0123456789", "०१२३४५६७८९")


def tpl_hindi(path, v, inv, show_po=True):
    """A Hindi invoice (labels, tax names and dates in Devanagari), rendered by Chromium so conjuncts shape correctly."""
    from playwright.sync_api import sync_playwright
    fonts = Path(__file__).resolve().parent / "fonts"
    face = "".join(f"@font-face{{font-family:Deva;font-weight:{w};src:url('file://{fonts}/noto-sans-devanagari-{sub}-{w}-normal.woff2')}}"
                   for w in (400, 700) for sub in ("devanagari", "latin"))
    rows = "".join(f"<tr><td>{i}</td><td>{l['hindi']} ({l['description']})</td><td>{l['hsn']}</td>"
                   f"<td class=n>{l['qty']:g} नग</td><td class=n>{inr(l['unit_price'])}</td><td class=n>{inr(l['amount'])}</td></tr>"
                   for i, l in enumerate(inv["lines"], 1))
    tax_hi = {"IGST": "आईजीएसटी", "CGST": "सीजीएसटी", "SGST": "एसजीएसटी"}
    taxes = "".join(f"<tr><td>{tax_hi[t.split()[0]]} @ {t.split('@')[1].strip()}</td><td class=n>{inr(a)}</td></tr>"
                    for t, a in inv["taxes"])
    po = f"<tr><td>क्रय आदेश संख्या:</td><td>{inv['po']}</td></tr>" if show_po and inv.get("po") else ""
    html = f"""<html><head><meta charset="utf-8"><style>{face}
    @page{{size:A4;margin:0}} body{{font-family:Deva;margin:48px 52px;color:#222;font-size:12.5px}}
    h1{{font-size:24px;margin:0}} .en{{font-size:13px;color:#555}} .top{{display:flex;justify-content:space-between;border-bottom:2px solid #8a1c1c;padding-bottom:8px}}
    .title{{font-size:20px;font-weight:700;color:#8a1c1c;text-align:right}} table{{border-collapse:collapse}}
    .meta td{{padding:2px 14px 2px 0}} .items{{width:100%;margin-top:22px}} .items th{{background:#f1e6e6;text-align:left;padding:6px}}
    .items td{{padding:6px;border-bottom:1px solid #ddd}} .n{{text-align:right}} .tot{{margin-left:auto;margin-top:12px}}
    .tot td{{padding:3px 6px}} .grand td{{font-weight:700;font-size:14px;border-top:1px solid #222}}
    .row{{display:flex;justify-content:space-between;margin-top:20px}} .foot{{position:absolute;bottom:60px;left:52px;right:52px;font-size:11.5px}}
    </style></head><body>
    <div class="top"><div><h1>श्री गणेश ट्रेडर्स</h1><div class="en">Shree Ganesh Traders</div>
      <div>12, नया बाज़ार, दिल्ली 110006</div><div>जीएसटीआईएन: {v['gstin']}</div></div>
      <div class="title">कर बीजक</div></div>
    <div class="row"><div><b>प्राप्तकर्ता:</b><br>{BUYER['name']}<br>{', '.join(BUYER['address'])}<br>जीएसटीआईएन: {BUYER['gstin']}</div>
      <table class="meta"><tr><td>बीजक संख्या:</td><td>{inv['number']}</td></tr>
      <tr><td>दिनांक:</td><td>{inv['date'].translate(DEVA)}</td></tr>{po}
      <tr><td>देय तिथि:</td><td>{inv['due'].translate(DEVA)}</td></tr></table></div>
    <table class="items"><tr><th>क्र.</th><th>विवरण</th><th>एचएसएन</th><th class=n>मात्रा</th><th class=n>दर</th><th class=n>राशि</th></tr>{rows}</table>
    <table class="tot"><tr><td>कर योग्य मूल्य</td><td class=n>{inr(inv['subtotal'])}</td></tr>{taxes}
      <tr class="grand"><td>कुल योग (रु.)</td><td class=n>{inr(inv['total'])}</td></tr></table>
    <div class="foot">बैंक: {v['bank']} &nbsp;|&nbsp; भुगतान: {v['terms_days']} दिन में<br>यह कंप्यूटर द्वारा बनाया गया बीजक है।
      <span style="float:right">कृते श्री गणेश ट्रेडर्स</span></div></body></html>"""
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.set_content(html, wait_until="networkidle")
        pg.pdf(path=str(path), format="A4", print_background=True)
        b.close()


def scan_it(src_pdf: Path, out_pdf: Path, dpi=100, seed=5):
    """Turn a clean PDF into a fax-quality image-only scan: no text layer, 1.1 deg skew, noise, blur, JPEG.
    With these settings Tesseract 5.3 misreads CGST 8,676 as 6,676 - a real 8/6 confusion the checker must catch."""
    rnd = np.random.default_rng(seed)
    page = pdfium.PdfDocument(str(src_pdf))[0]
    img = page.render(scale=dpi / 72).to_pil().convert("L")
    img = img.rotate(1.1, resample=Image.BICUBIC, expand=True, fillcolor=255)
    arr = np.asarray(img).astype(np.float32) * 0.92 + 16  # grey paper
    arr += rnd.normal(0, 14, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.5))
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=40); buf.seek(0)
    Image.open(buf).convert("L").save(out_pdf, "PDF", resolution=dpi)


# --------------------------------------------------------------------------------------------
def build_invoice(vendor_id, number, date, due, po, lines, rate=18, tax_inclusive=False, tax_override=None,
                  bank_text=None, no_irn=False):
    v = VBY.get(vendor_id) or IMPOSTOR
    lines = [dict(l) for l in lines]
    for l in lines:
        l["amount"] = round(l["qty"] * l["unit_price"], 2)
    if tax_inclusive:
        total = round(sum(l["amount"] for l in lines), 2)
        subtotal = round(total / (1 + rate / 100), 2)
        tx = [(taxes(v, 1, rate)[0][0] if len(taxes(v, 1, rate)) == 1 else f"GST @ {rate}%", round(total - subtotal, 2))]
    else:
        subtotal = round(sum(l["amount"] for l in lines), 2)
        tx = tax_override or taxes(v, subtotal, rate)
        total = round(subtotal + sum(a for _, a in tx), 2)
    from app.utils import parse_date as _pd
    irn = irn_for(v["gstin"], _pd(date).isoformat(), number) if v.get("einvoice") and not no_irn else None
    return dict(number=number, date=date, due=due, po=po, lines=lines, subtotal=subtotal, taxes=tx,
                total=total, tax_inclusive=tax_inclusive, bank_text=bank_text, irn=irn)


def ln(desc, qty, price, hsn="", unit="Nos"):
    return dict(description=desc, qty=qty, unit_price=price, hsn=hsn, unit=unit)


SAMPLES_SPEC = [
    dict(file="01_happy_sundaram_clean.pdf", tpl="classic", kind="happy",
         title="Clean invoice, exact PO match",
         story="Machine-readable PDF, explicit PO, quantities and prices match the PO exactly.",
         expect="APPROVE",
         inv=("V001", "SPS/26-27/0418", "24-Oct-2026", "23-Nov-2026", "PO-2026-0101",
              [ln("Corrugated boxes 5-ply 480x320x300", 5000, 18.50, "4819"),
               ln("Stretch wrap film roll 500mm", 40, 1250.00, "3920")])),
    dict(file="02_happy_brightline_tolerance.pdf", tpl="modern", kind="happy",
         title="Inter-state freight, small variance within tolerance",
         story="Different layout, IGST instead of CGST/SGST, PO written as 'PO 2026/102'. The freight rate came in "
               "Rs.150/trip above the PO rate (+0.5%) - inside the 1% tolerance.",
         expect="APPROVE",
         inv=("V003", "BLL-INV-2026-1187", "October 28, 2026", "", "PO 2026/102",
              [ln("FTL freight Chennai to Bengaluru (32 ft)", 6, 28150.00),
               ln("Loading and unloading charges", 6, 1500.00)])),
    dict(file="03_happy_apex_tax_inclusive.pdf", tpl="letter", kind="happy",
         title="Tax embedded in prices",
         story="Small vendor quotes GST-inclusive rates. The process must back out the tax before comparing "
               "to the (pre-tax) PO.",
         expect="APPROVE",
         inv=("V005", "APX/1093", "22/10/2026", "", "PO-2026-0105",
              [ln("Benchtop pH meter with probe", 4, 27730.00),
               ln("Calibration buffer kit pH 4/7/10", 10, 2124.00, unit="Kits")]),
         extra=dict(tax_inclusive=True)),
    dict(file="04_happy_nimbus_partial_billing.pdf", tpl="modern", kind="happy",
         title="One PO billed in parts (within balance)",
         story="Annual hosting PO billed quarterly. Q1 is already paid; this Q2 invoice uses 1 of the 3 "
               "remaining quarters.",
         expect="APPROVE",
         inv=("V004", "NCS/INV/2026/164", "October 02, 2026", "", "PO-2026-0103",
              [ln("Managed cloud hosting Q2 (Jul-Sep 2026)", 1, 120000.00)])),
    dict(file="05_edge_murugan_scanned.pdf", tpl="classic", kind="edge", scanned=True,
         title="Fax-quality scan, OCR misreads a digit",
         story="A faxed paper invoice with no text layer. The process OCRs it, then refuses to trust the OCR until "
               "the numbers reconcile. On this scan OCR reads CGST 8,676 as 6,676.",
         expect="APPROVE or REVIEW (depends on OCR quality)",
         inv=("V007", "ME/2026/0388", "27-Oct-2026", "26-Nov-2026", "PO-2026-0107",
              [ln("LED panel light 40W 2x2", 60, 1150.00, "9405"),
               ln("MCB 32A double pole", 40, 385.00, "8536"),
               ln("Installation labour - packing hall", 1, 12000.00, "9954")])),
    dict(file="06_edge_kaveri_split_po_overbilled.pdf", tpl="classic", kind="edge",
         title="Split PO - second invoice exceeds what's left",
         story="PO-2026-0104 was partly billed earlier (1,200 gloves + 300 goggles). This invoice bills 1,000 more "
               "gloves, but only 800 remain on the PO.",
         expect="REVIEW",
         inv=("V002", "KIS/2026/0934", "29-Oct-2026", "13-Dec-2026", "PO-2026-0104",
              [ln("Nitrile safety gloves (pair)", 1000, 85.00, "4015"),
               ln("Anti-fog safety goggles", 200, 220.00, "9004")])),
    dict(file="07_edge_sundaram_duplicate.pdf", tpl="letter", kind="edge",
         title="Duplicate re-sent in a different format",
         story="INV-0042 was already paid. The vendor re-sends it as a 'payment reminder' in a different layout "
               "and writes the number as 'INV42'. Same vendor, same amount, same PO.",
         expect="REJECT",
         inv=("V001", "INV42", "12/10/2026", "", "PO-2026-0098",
              [ln("Printed product labels 100x50mm", 20000, 1.75)]),
         extra=dict(heading="PAYMENT REMINDER", no_irn=True)),
    dict(file="08_edge_kaveri_no_po.pdf", tpl="modern", kind="edge",
         title="No PO number on the invoice",
         story="The vendor forgot the PO reference. The process finds the most likely PO from vendor, items and "
               "amount - but does not auto-approve a guessed match.",
         expect="REVIEW",
         inv=("V002", "KIS/2026/0951", "October 30, 2026", "", None,
              [ln("Hydraulic oil ISO VG 68 (20 L can)", 30, 4200.00),
               ln("Lithium grease cartridge 400 g", 200, 145.00)]),
         extra=dict(show_po=False)),
    dict(file="09_extra_rapidprint_blocked_vendor.pdf", tpl="classic", kind="extra",
         title="Vendor is blocked in the master",
         story="Everything on the invoice is fine, but the vendor's KYC has expired, so payment must not go out.",
         expect="REJECT",
         inv=("V006", "RPS-2611", "21-Oct-2026", "20-Nov-2026", "PO-2026-0108",
              [ln("Vinyl banners 6x3 ft printed", 25, 1400.00, "4911")])),
    dict(file="12_edge_kaveri_goods_not_received.pdf", tpl="classic", kind="edge",
         title="Billed before all goods arrived",
         story="Kaveri bills 300 helmets on PO-2026-0110, but the warehouse has only received 200. Payment waits for the "
               "goods. The agent asks Stores; when they reply that the rest arrived, it books the receipt and re-checks.",
         expect="REVIEW",
         inv=("V002", "KIS/2026/0962", "02-Nov-2026", "17-Dec-2026", "PO-2026-0110",
              [ln("Industrial safety helmet with chin strap", 300, 450.00, "6506")])),
    dict(file="13_edge_brightline_no_einvoice.pdf", tpl="modern", kind="edge",
         title="Large vendor, no e-invoice (IRN)",
         story="Brightline is above the ₹5 crore e-invoicing limit but sent a plain PDF with no IRN. Without it the "
               "invoice isn't a valid tax invoice and ₹22,320 of input tax credit is lost. The agent asks the vendor "
               "for the e-invoice; when it arrives the IRN is checked against the invoice and payment is released "
               "(less 2% TDS on freight).",
         expect="REVIEW",
         inv=("V003", "BLL-INV-2026-1214", "November 02, 2026", "", "PO 2026/111",
              [ln("FTL freight Chennai to Hyderabad (32 ft)", 4, 31000.00)]),
         extra=dict(no_irn=True)),
    dict(file="14_learn_nimbus_new_layout.pdf", tpl="statement", kind="learning",
         title="New layout: reviewer teaches it once",
         story="Nimbus switched billing systems. Its statement says 'Document Ref' and 'Dated', labels the reader has "
               "never seen, so the invoice number and date are missing. A reviewer types them in; the process finds them "
               "on the page, learns the labels for Nimbus, re-checks (the IRN confirms the typed number) and approves.",
         expect="REVIEW, then APPROVE after the correction",
         inv=("V004", "NCS/BKP/2026/031", "03 Nov 2026", "", "PO-2026-0112",
              [ln("Managed backup service - monthly", 1, 15000.00, "998315")])),
    dict(file="15_learn_nimbus_next_invoice.pdf", tpl="statement", kind="learning",
         title="Same layout again: read automatically",
         story="The next Nimbus statement in the same layout. After #14 has been corrected once, the learned labels "
               "read it with no help. Before that, it needs a reviewer too.",
         expect="APPROVE once #14 was corrected (REVIEW before)",
         inv=("V004", "NCS/BKP/2026/032", "04 Nov 2026", "", "PO-2026-0112",
              [ln("Managed backup service - monthly", 1, 15000.00, "998315")])),
    dict(file="16_hindi_ganesh_traders.pdf", tpl="hindi", kind="language",
         title="Invoice in Hindi",
         story="A Delhi supplier's invoice with Hindi labels (बीजक संख्या, दिनांक, क्रय आदेश संख्या, कुल योग), Hindi tax names "
               "(आईजीएसटी) and dates in Devanagari digits. Same checks, same decision: no special path.",
         expect="APPROVE",
         inv=("V008", "SGT/2026/219", "28/10/2026", "27/11/2026", "PO-2026-0113",
              [dict(ln("Jute sacks 50 kg", 1000, 38.00, "6305"), hindi="जूट बोरी 50 किग्रा"),
               dict(ln("Polypropylene twine roll", 50, 120.00, "5607"), hindi="पॉलीप्रोपाइलीन सुतली रोल")]),
         extra=dict(rate=5)),
    dict(file="10_fraud_sundaram_bank_change.pdf", tpl="classic", kind="fraud",
         title="Real vendor, new bank account",
         story="A genuine-looking Sundaram invoice against a real open PO - everything matches, except it asks for "
               "payment to a NEW bank account. Classic business-email-compromise: hold and verify by phone.",
         expect="REVIEW",
         inv=("V001", "SPS/26-27/0461", "30-Oct-2026", "29-Nov-2026", "PO-2026-0109",
              [ln("Printed shrink sleeves 250 ml bottle", 30000, 2.40, "3920")]),
         extra=dict(bank_text="NOTE - our bank details have changed. Please pay to our NEW account: "
                              "Yes Bank ****8812   |   Payment terms: 30 days")),
    dict(file="11_fraud_lookalike_vendor.pdf", tpl="classic", kind="fraud",
         title="Look-alike vendor, different bank",
         story="'Sundaram Packaging Solution' (no s) with a different GSTIN and bank account, quoting Sundaram's real "
               "PO number. An impostor - payment must be frozen, and no reply sent that tips them off.",
         expect="REJECT",
         inv=("X", "SPS/26-27/0466", "31-Oct-2026", "07-Nov-2026", "PO-2026-0109",
              [ln("Printed shrink sleeves 250 ml bottle", 30000, 2.40, "3920")]),
         extra=dict(bank_text="Bank: IDFC ****0931   |   URGENT: payment due within 7 days")),
]


def main():
    (DATA / "vendors.json").write_text(json.dumps(VENDORS, indent=2))
    (DATA / "purchase_orders.json").write_text(json.dumps(POS, indent=2))
    (DATA / "invoice_history.json").write_text(json.dumps(HISTORY, indent=2))
    (DATA / "goods_receipts.json").write_text(json.dumps(GRNS, indent=2))
    filed = []
    for spec in SAMPLES_SPEC:
        vid, num = spec["inv"][0], spec["inv"][1]
        if vid in VBY and not spec["file"].startswith(("11_", "12_", "13_", "14_", "15_")):
            filed.append(dict(gstin=VBY[vid]["gstin"], invoice_number=num, period="2026-10"))
    for h in HISTORY:
        filed.append(dict(gstin=VBY[h["vendor_id"]]["gstin"], invoice_number=h["invoice_number"], period="2026-10"))
    (DATA / "gstr2b.json").write_text(json.dumps(filed, indent=2))
    (DATA / "buyer.json").write_text(json.dumps(BUYER, indent=2))

    manifest = []
    tpls = dict(classic=tpl_classic, modern=tpl_modern, letter=tpl_letter, statement=tpl_statement, hindi=tpl_hindi)
    for s in SAMPLES_SPEC:
        vid, num, date, due, po, lines = s["inv"]
        extra = dict(s.get("extra", {}))
        show_po = extra.pop("show_po", True)
        heading = extra.pop("heading", None)
        inv = build_invoice(vid, num, date, due, po, lines, **extra)
        out = SAMPLES / s["file"]
        kwargs = dict(show_po=show_po)
        if heading:
            kwargs["heading"] = heading
        if s.get("scanned"):
            tmp = SAMPLES / ("_clean_" + s["file"])
            tpls[s["tpl"]](tmp, VBY.get(vid) or IMPOSTOR, inv, **kwargs)
            scan_it(tmp, out)
            tmp.unlink()
        else:
            tpls[s["tpl"]](out, VBY.get(vid) or IMPOSTOR, inv, **kwargs)
        manifest.append(dict(file=s["file"], kind=s["kind"], title=s["title"], story=s["story"],
                             expect=s["expect"], vendor=(VBY.get(vid) or IMPOSTOR)["name"], total=inv["total"]))
        print(f"{s['file']:45s} total={inv['total']:>12,.2f}")
    (SAMPLES / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
