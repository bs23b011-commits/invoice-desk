"""One PDF, several invoices: split first, then check each one fully against its own vendor and PO."""
import io
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["STAGE_DELAY"] = "0"
os.environ.setdefault("RUNTIME_DIR", tempfile.mkdtemp())
os.environ.setdefault("DEMO_TODAY", "2026-11-04")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pypdf import PdfWriter  # noqa: E402

from app import store  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)
S = ROOT / "samples"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setenv("APPROVAL_MODE", "human")
    monkeypatch.setenv("INTAKE_TOKEN", "tok")
    monkeypatch.delenv("N8N_WEBHOOK_URL", raising=False)
    store.init(reset=True)


def combo(*names) -> bytes:
    w = PdfWriter()
    for n in names:
        w.append(str(S / n) if not str(n).startswith("/") else n)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def wait(rid):
    time.sleep(0.1)
    for _ in range(800):
        r = c.get(f"/api/runs/{rid}").json()
        if r["state"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def upload(pdf, name="batch.pdf"):
    j = c.post("/api/runs", files={"file": (name, pdf, "application/pdf")}).json()
    return j, [wait(r) for r in j.get("split", [j["id"]])]


def codes(r):
    return {h["code"] for h in r["result"]["handoff"]}


def test_two_vendors_in_one_pdf_are_split_and_each_held_for_a_person():
    j, runs = upload(combo("01_happy_sundaram_clean.pdf", "02_happy_brightline_tolerance.pdf"))
    assert "2 different vendors" in j["message"] and j["mixed"]
    assert [r["result"]["invoice"]["vendor"][:9] for r in runs] == ["Sundaram ", "Brightlin"]
    assert [r["result"]["invoice"]["total"] for r in runs] == [168150.0, 209922.0]  # each read on its own
    for r in runs:  # every check passes, but a mixed PDF is unusual: a person looks (option A)
        assert r["result"]["decision"] == "REVIEW" and codes(r) == {"MIXED_PDF"}


def test_scan_and_digital_in_one_pdf_each_page_read_its_own_way():
    j, runs = upload(combo("05_edge_murugan_scanned.pdf", "01_happy_sundaram_clean.pdf"))
    assert [r["source"] for r in runs] == ["ocr", "text-layer"]
    assert runs[0]["result"]["invoice"]["vendor"].startswith("Murugan")
    assert runs[1]["result"]["invoice"]["invoice_number"] == "SPS/26-27/0418"


def test_same_vendor_invoices_each_checked_against_their_own_po():
    j, runs = upload(combo("06_edge_kaveri_split_po_overbilled.pdf", "08_edge_kaveri_no_po.pdf",
                           "12_edge_kaveri_goods_not_received.pdf"))
    assert "3 invoices from Kaveri" in j["message"] and not j["mixed"]
    # exactly what each would get on its own
    assert [codes(r) for r in runs] == [{"PO_QTY_OVER"}, {"PO_MISSING"}, {"GRN_SHORT"}]
    note = c.get(f"/api/runs/{runs[1]['id']}/discussion").json()["items"]
    assert any(i["kind"] == "note" and "3 invoices from Kaveri" in i["text"] for i in note)
    card = next(x for x in c.get("/api/waiting").json()["items"] if x["id"] == runs[2]["id"])
    assert card["batch"] == dict(index=3, count=3, source="batch.pdf", mixed=False, vendors=["Kaveri Industrial Supplies"])


def test_a_real_two_page_invoice_is_not_split():
    p = ROOT / "samples_messy" / "m12_two_pages.pdf"
    if not p.exists():
        pytest.skip("messy samples not generated")
    j, runs = upload(p.read_bytes())
    assert "split" not in j and len(runs) == 1 and runs[0]["result"]["decision"] == "APPROVE"


def test_printed_copies_of_one_invoice_are_counted_once():
    j, runs = upload(combo(*["01_happy_sundaram_clean.pdf"] * 3))
    assert len(runs) == 1 and "copies" in j["message"]
    r = runs[0]
    assert r["result"]["invoice"]["total"] == 168150.0 and r["result"]["decision"] == "APPROVE"


def test_email_with_several_invoices_in_one_attachment():
    pdf = combo("01_happy_sundaram_clean.pdf", "02_happy_brightline_tolerance.pdf")
    r = c.post("/api/intake", headers={"X-API-Key": "tok"}, data={"sender": "ap@x.example", "message_id": "m-9"},
               files={"file": ("bills.pdf", pdf, "application/pdf")}).json()
    assert r["invoices_in_file"] == 2 and len(r["run_ids"]) == 2
    for rid in r["run_ids"]:
        assert wait(rid)["channel"]["sender"] == "ap@x.example"
    again = c.post("/api/intake", headers={"X-API-Key": "tok"}, data={"sender": "ap@x.example", "message_id": "m-9"},
                   files={"file": ("bills.pdf", pdf, "application/pdf")}).json()
    assert again["status"] == "already_received"


def test_more_than_ten_pages_is_held_not_half_read():
    j, runs = upload(combo(*["01_happy_sundaram_clean.pdf", "02_happy_brightline_tolerance.pdf"] * 6))
    r = runs[0]
    assert len(runs) == 1 and "PAGES_UNREAD" in codes(r) and r["result"]["decision"] != "APPROVE"


def test_scanned_copy_is_counted_once():
    j, runs = upload(combo("05_edge_murugan_scanned.pdf", "05_edge_murugan_scanned.pdf"))
    assert len(runs) == 1 and "copy" in j["message"]


def test_multi_page_scan_is_held(tmp_path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1240, 1754), "white")
    d = ImageDraw.Draw(img)
    for k, line in enumerate(["Terms and conditions (continued)", "Goods once sold will not be taken back.",
                              "Interest at 18% p.a. on overdue amounts.", "Subject to Chennai jurisdiction."]):
        d.text((80, 120 + 40 * k), line, fill="black")
    page2 = tmp_path / "p2.pdf"
    img.save(page2, "PDF", resolution=150.0)  # a scanned page with no invoice number: it continues invoice 1
    j, runs = upload(combo("05_edge_murugan_scanned.pdf", str(page2)))
    r = runs[0]
    assert len(runs) == 1 and "PAGES_UNREAD" in codes(r) and r["result"]["decision"] != "APPROVE"
