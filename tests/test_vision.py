"""Step 6b: the vision reader is a second opinion on scans, and the arithmetic stays the judge.
No API key is needed: the vision model is replaced by a stand-in that returns a given reading."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["STAGE_DELAY"] = "0"
os.environ.setdefault("RUNTIME_DIR", tempfile.mkdtemp())
os.environ.setdefault("DEMO_TODAY", "2026-11-04")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from app import extract_llm, extract_rules, pipeline, store  # noqa: E402

FAX = "05_edge_murugan_scanned.pdf"


def F(v, conf=0.9):
    return dict(value=v, raw=v, method="vision", confidence=conf, box=None)


def true_reading():
    lines = [dict(description="LED panel light 40W 2x2", qty=60.0, unit_price=1150.0, amount=69000.0, hsn=None, unit=None, confidence=0.9),
             dict(description="MCB 32A double pole", qty=40.0, unit_price=385.0, amount=15400.0, hsn=None, unit=None, confidence=0.9),
             dict(description="Installation labour - packing hall", qty=1.0, unit_price=12000.0, amount=12000.0, hsn=None, unit=None, confidence=0.9)]
    return dict(vendor_name=F("Murugan Electricals"), vendor_gstin=F("33ACQPM6634D1ZK"), buyer_gstin=F("33AABCM1234F1Z4"),
                invoice_number=F("ME/2026/0388"), invoice_date=F("2026-10-27"), due_date=F("2026-11-26"),
                po_number=F("PO-2026-0107"), subtotal=F(96400.0), total=F(113752.0), bank=F(None), tax_inclusive=False,
                currency="INR", lines=lines,
                taxes=[dict(type="CGST", rate=9.0, amount=8676.0, confidence=0.9), dict(type="SGST", rate=9.0, amount=8676.0, confidence=0.9)])


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    store.init(reset=True)
    monkeypatch.setattr(extract_llm, "provider", lambda: "gemini")
    monkeypatch.setattr(extract_llm, "extract", lambda text: (_ for _ in ()).throw(RuntimeError("text LLM off")))


def run():
    r = pipeline.new_run(FAX, FAX)
    pipeline.execute(r, (ROOT / "samples" / FAX).read_bytes())
    return store.get_run(r["id"])


def weaken_invoice_number(monkeypatch):
    real = extract_rules.extract

    def weak(doc, buyer):
        f = real(doc, buyer)
        f["invoice_number"]["confidence"] = 0.3
        return f
    monkeypatch.setattr(extract_rules, "extract", weak)


def test_unclear_field_confirmed_by_vision(monkeypatch):
    weaken_invoice_number(monkeypatch)
    monkeypatch.setattr(extract_llm, "extract_image", lambda png: true_reading())
    r = run()
    assert r["result"]["decision"] == "APPROVE"
    ex = next(s for s in r["stages"] if s["key"] == "extract")
    assert any("vision reader read the same value" in f["text"] for f in ex["findings"])


def test_disagreement_goes_to_a_person(monkeypatch):
    weaken_invoice_number(monkeypatch)
    v = true_reading()
    v["invoice_number"] = F("ME/2026/0383")
    monkeypatch.setattr(extract_llm, "extract_image", lambda png: v)
    r = run()
    assert r["result"]["decision"] == "REVIEW"


def test_unrepairable_ocr_uses_vision_numbers_only_if_they_add_up(monkeypatch):
    real = extract_rules.extract

    def broken(doc, buyer):  # two numbers wrong: no single-digit repair can fix this
        f = real(doc, buyer)
        f["lines"][0]["amount"] = 64000.0
        f["taxes"][0]["amount"] = 6676.0
        return f
    monkeypatch.setattr(extract_rules, "extract", broken)
    monkeypatch.setattr(extract_llm, "extract_image", lambda png: true_reading())
    r = run()
    val = next(s for s in r["stages"] if s["key"] == "validate")
    assert any("vision reader's numbers do" in f["text"] for f in val["findings"])
    assert r["result"]["decision"] == "APPROVE"

    bad = true_reading()
    bad["total"] = F(113000.0)  # vision is also wrong -> still a person
    monkeypatch.setattr(extract_llm, "extract_image", lambda png: bad)
    store.init(reset=True)
    assert run()["result"]["decision"] == "REVIEW"
