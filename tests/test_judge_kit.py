"""The judge kit: files and data the app has never seen. Someone testing it brings their own vendors and POs,
phone photos, password-protected or broken PDFs, foreign-currency and multi-page invoices. None of these may
crash anything, get lost, or get paid without a person."""
import io
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["STAGE_DELAY"] = "0"
os.environ.setdefault("RUNTIME_DIR", tempfile.mkdtemp())
os.environ.setdefault("DEMO_TODAY", "2026-11-06")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app import master, store  # noqa: E402
from app.main import app  # noqa: E402
from app.utils import gstin_checksum  # noqa: E402

c = TestClient(app)
ACME = "27AABCA1234F1Z" + gstin_checksum("27AABCA1234F1Z")
H = {"X-API-Key": "tok"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setenv("APPROVAL_MODE", "human")
    monkeypatch.setenv("INTAKE_TOKEN", "tok")
    monkeypatch.delenv("N8N_WEBHOOK_URL", raising=False)
    master.clear_imports()
    store.init(reset=True)
    yield
    master.clear_imports()


def invoice_lines(currency="", total_page2=False):
    cur = f"{currency} " if currency else ""
    head = ["TAX INVOICE", "Acme Steel Traders Pvt Ltd", f"GSTIN: {ACME}",
            "Bill To: Meridian Foods Pvt Ltd, Chennai", f"GSTIN: {store.BUYER['gstin']}",
            "Invoice No: AST/2026/771", "Invoice Date: 02/11/2026", "PO No: PO-ACME-0001",
            "Bank: ICICI Bank A/c No 000405012345",
            "S.No Description HSN Qty Unit Rate Amount",
            f"1 MS angle 50x50 7216 100 Nos {cur}450.00 {cur}45,000.00"]
    tail = [f"Sub Total {cur}45,000.00", f"IGST 18% {cur}8,100.00", f"Grand Total {cur}53,100.00"]
    return (head, tail) if total_page2 else (head + tail, None)


def make_pdf(lines, page2=None, password=None) -> bytes:
    buf = io.BytesIO()
    cv = canvas.Canvas(buf, pagesize=A4, encrypt=password)
    for page in (lines, page2):
        if page is None:
            continue
        y = 800
        for ln in page:
            cv.drawString(50, y, ln)
            y -= 18
        cv.showPage()
    cv.save()
    return buf.getvalue()


def upload(name, data):
    return c.post("/api/runs", files={"file": (name, data, "application/octet-stream")})


def wait(rid):
    for _ in range(600):
        r = c.get(f"/api/runs/{rid}").json()
        if r["state"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def codes(r):
    return {h["code"] for h in (r.get("result") or {}).get("handoff") or []}


def import_csv(kind, text):
    return c.post("/api/master/import", data={"kind": kind}, files={"file": (f"{kind}.csv", text.encode(), "text/csv")}).json()


def import_acme():
    v = import_csv("vendors", "Vendor Name,GSTIN,Bank,Account No,MSME,Payment Terms\n"
                              f"Acme Steel Traders Pvt Ltd,{ACME},ICICI Bank,000405012345,no,30\n"
                              "Broken Row Ltd,27AAAAA0000A1Z0,SBI,1234,no,30\n")
    p = import_csv("pos", f"PO No,Vendor GSTIN,PO Date,Item,Quantity,Rate,HSN\nPO-ACME-0001,{ACME},2026-10-01,MS angle 50x50,100,450,7216\n")
    g = import_csv("receipts", "po_number,description,qty,date\nPO-ACME-0001,MS angle 50x50,100,2026-10-20\n")
    return v, p, g


# ---------------------------------------------------------------------------------- their own data
def test_unknown_vendor_is_held_not_lost():
    r = wait(upload("acme.pdf", make_pdf(invoice_lines()[0])).json()["id"])
    assert r["result"]["decision"] == "REVIEW" and "VENDOR_UNKNOWN" in codes(r)
    card = next(x for x in c.get("/api/waiting").json()["items"] if x["id"] == r["id"])
    assert card["onboard"]["gstin"] == ACME and "ICICI" in card["onboard"]["bank"]


def test_import_their_vendors_pos_receipts_then_invoice_is_fully_checked():
    v, p, g = import_acme()
    assert v["added"] == 1 and v["errors"][0]["row"] == 3 and "GSTIN" in v["errors"][0]["error"]  # bad row reported
    assert p["added"] == 1 and g["added"] == 1
    r = wait(upload("acme.pdf", make_pdf(invoice_lines()[0])).json()["id"])
    assert r["result"]["decision"] == "APPROVE", r["result"]["headline"]  # vendor, 3-way match, tax all pass
    assert r["result"]["awaiting_human"]


def test_imports_survive_reset_and_can_be_removed():
    import_acme()
    c.post("/api/reset")
    assert any(x["gstin"] == ACME for x in store.VENDORS) and store.receipts("PO-ACME-0001")
    c.post("/api/master/clear")
    assert not any(x["gstin"] == ACME for x in store.VENDORS) and not store.receipts("PO-ACME-0001")


def test_onboard_vendor_from_the_hold_needs_verification_then_rechecks():
    r = wait(upload("acme.pdf", make_pdf(invoice_lines()[0])).json()["id"])
    form = dict(name="Acme Steel Traders Pvt Ltd", gstin=ACME, bank_name="ICICI Bank", account_number="000405012345")
    assert c.post(f"/api/runs/{r['id']}/onboard", data=form).status_code == 400  # no verification tick
    j = c.post(f"/api/runs/{r['id']}/onboard", data=dict(form, verified="1", who="Priya")).json()
    assert j["vendor"]["bank"] == "ICICI BANK ****2345"
    r2 = wait(r["id"])
    assert "VENDOR_UNKNOWN" not in codes(r2) and r2["revisions"]  # now held only for the unknown PO
    assert "PO_NOT_FOUND" in codes(r2) or "PO_INVALID" in codes(r2)


def test_templates_download():
    t = c.get("/api/master/template/vendors.csv")
    assert t.status_code == 200 and t.text.startswith("name,gstin")


# ---------------------------------------------------------------------------------- strange files
def test_phone_photo_is_read_with_ocr():
    pytest.importorskip("pytesseract")
    from app.reader import _render
    pdf = (ROOT / "samples" / "01_happy_sundaram_clean.pdf").read_bytes()
    buf = io.BytesIO()
    _render(pdf, 2.8).convert("RGB").save(buf, "JPEG", quality=88)
    j = upload("IMG_2041.jpg", buf.getvalue()).json()
    r = wait(j["id"])
    assert r["state"] == "done" and r["filename"] == "IMG_2041.pdf" and r["source"] == "ocr"
    assert r["result"]["invoice"]["total"] == 168150.0


@pytest.mark.parametrize("name,data,words", [
    ("locked.pdf", make_pdf(["TAX INVOICE"], password="secret"), "password-protected"),
    ("broken.pdf", b"%PDF-1.4\n garbage", "damaged"),
    ("empty.pdf", b"", "empty"),
    ("notes.docx", b"PK\x03\x04 word file", "isn't a PDF"),
])
def test_unreadable_upload_gets_a_plain_reason(name, data, words):
    r = upload(name, data)
    assert r.status_code == 400 and words in r.json()["detail"]


def test_unreadable_email_attachment_is_not_lost():
    r = c.post("/api/intake", headers=H, data={"sender": "billing@acme.example", "message_id": "x1"},
               files={"file": ("inv.pdf", make_pdf(["x"], password="secret"), "application/pdf")}).json()
    assert r["status"] == "unreadable"
    card = next(x for x in c.get("/api/waiting").json()["items"] if x["id"] == r["run_id"])
    assert card["recommendation"] == "UNREADABLE" and "password" in card["note"]
    bad = c.post(f"/api/runs/{r['run_id']}/resolve", data={"action": "approve"})
    assert bad.status_code == 400  # can't approve what nobody could read
    assert c.post(f"/api/runs/{r['run_id']}/resolve", data={"action": "reject", "note": "asked for a new copy"}).status_code == 200
    assert all(x["id"] != r["run_id"] for x in c.get("/api/waiting").json()["items"])


def test_logo_in_email_is_skipped():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (240, 80), "white").save(buf, "PNG")
    r = c.post("/api/intake", headers=H, files={"file": ("logo.png", buf.getvalue(), "image/png")}).json()
    assert r["status"] == "skipped"


def test_foreign_currency_is_held():
    import_acme()
    r = wait(upload("usd.pdf", make_pdf(invoice_lines(currency="USD")[0])).json()["id"])
    assert r["result"]["decision"] != "APPROVE" and "FOREIGN_CURRENCY" in codes(r)


def test_totals_on_page_two_are_read():
    import_acme()
    p1, p2 = invoice_lines(total_page2=True)
    r = wait(upload("two_pages.pdf", make_pdf(p1, page2=p2)).json()["id"])
    read = next(s for s in r["stages"] if s["key"] == "read")
    assert any("Read all 2 pages" in f["text"] for f in read["findings"])
    assert r["result"]["invoice"]["total"] == 53100.0 and r["result"]["decision"] == "APPROVE"


def test_nothing_strange_ever_reaches_payment_without_a_person():
    import_acme()
    for name, data in [("a.pdf", make_pdf(invoice_lines()[0])), ("usd.pdf", make_pdf(invoice_lines("USD")[0]))]:
        wait(upload(name, data).json()["id"])
    assert c.get("/api/payments").json()["open"] == []
