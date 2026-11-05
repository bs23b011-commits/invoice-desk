"""End-to-end and unit tests.  Run:  STAGE_DELAY=0 python -m pytest -q"""
import copy
import os
import sys
import tempfile
from pathlib import Path

os.environ["STAGE_DELAY"] = "0"
os.environ["RUNTIME_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("DEMO_TODAY", "2026-11-01")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from app import checks, pipeline, risk, store  # noqa: E402
from app.utils import gstin_is_valid, norm_invoice_no, norm_po, parse_money  # noqa: E402

EXPECTED = {
    "01_happy_sundaram_clean.pdf": "APPROVE",
    "02_happy_brightline_tolerance.pdf": "APPROVE",
    "03_happy_apex_tax_inclusive.pdf": "APPROVE",
    "04_happy_nimbus_partial_billing.pdf": "APPROVE",
    "05_edge_murugan_scanned.pdf": "APPROVE",   # OCR misread repaired + verified (REVIEW if repair can't be proven)
    "06_edge_kaveri_split_po_overbilled.pdf": "REVIEW",
    "07_edge_sundaram_duplicate.pdf": "REJECT",
    "08_edge_kaveri_no_po.pdf": "REVIEW",
    "09_extra_rapidprint_blocked_vendor.pdf": "REJECT",
    "10_fraud_sundaram_bank_change.pdf": "REVIEW",
    "11_fraud_lookalike_vendor.pdf": "REJECT",
    "12_edge_kaveri_goods_not_received.pdf": "REVIEW",
    "13_edge_brightline_no_einvoice.pdf": "REVIEW",
    "14_learn_nimbus_new_layout.pdf": "REVIEW",
    "15_learn_nimbus_next_invoice.pdf": "REVIEW",   # until #14 has taught the layout
    "16_hindi_ganesh_traders.pdf": "APPROVE",
}


def run_sample(name):
    run = pipeline.new_run(name, name)
    pipeline.execute(run, (ROOT / "samples" / name).read_bytes())
    return store.get_run(run["id"])


@pytest.fixture(scope="module", autouse=True)
def fresh_db():
    store.init(reset=True)


@pytest.mark.parametrize("name,expected", EXPECTED.items())
def test_each_sample_decision(name, expected):
    store.init(reset=True)
    r = run_sample(name)
    assert r["state"] == "done", r.get("trace")
    got = r["result"]["decision"]
    if name.startswith("05"):
        assert got in ("APPROVE", "REVIEW")  # depends on the local Tesseract build
    else:
        assert got == expected, r["result"]["headline"]


def test_same_invoice_twice_is_a_duplicate():
    store.init(reset=True)
    assert run_sample("01_happy_sundaram_clean.pdf")["result"]["decision"] == "APPROVE"
    second = run_sample("01_happy_sundaram_clean.pdf")
    assert second["result"]["decision"] == "REJECT"
    assert "Duplicate" in second["result"]["headline"]


def test_review_then_approve_uses_po_balance():
    store.init(reset=True)
    r = run_sample("08_edge_kaveri_no_po.pdf")
    assert r["result"]["decision"] == "REVIEW"
    led = [x for x in store.ledger() if x["run_id"] == r["id"]][0]
    assert led["status"] == "REVIEW"
    pipeline.resolve(r["id"], "approve", "PO-2026-0106 confirmed with buyer")
    led = [x for x in store.ledger() if x["run_id"] == r["id"]][0]
    assert led["status"] == "APPROVE"


# ----------------------------------------------------------------------------------- self-check
CLEAN = dict(
    lines=[dict(description="LED", qty=60, unit_price=1150.0, amount=69000.0),
           dict(description="MCB", qty=40, unit_price=385.0, amount=15400.0),
           dict(description="Labour", qty=1, unit_price=12000.0, amount=12000.0)],
    taxes=[dict(type="CGST", rate=9.0, amount=8676.0), dict(type="SGST", rate=9.0, amount=8676.0)],
    subtotal=96400.0, total=113752.0, tax_inclusive=False)


def test_clean_invoice_all_identities_hold():
    assert all(i["ok"] for i in checks.identities(CLEAN))


@pytest.mark.parametrize("path,bad,good", [
    (("taxes", 0, "amount"), 6676.0, 8676.0),     # 8 read as 6
    (("lines", 2, "amount"), 42000.0, 12000.0),   # 1 read as 4
    (("total",), 13752.0, 113752.0),              # dropped leading digit
])
def test_ocr_repair_finds_the_unique_fix(path, bad, good):
    inv = copy.deepcopy(CLEAN)
    checks._set(inv, path, bad)
    fixed, info = checks.try_repair(inv)
    assert fixed is not None, info
    assert info["new"] == good


def test_two_errors_are_not_guessed():
    inv = copy.deepcopy(CLEAN)
    inv["taxes"][0]["amount"] = 6676.0
    inv["lines"][0]["amount"] = 64000.0
    fixed, info = checks.try_repair(inv)
    assert fixed is None


def test_tax_inclusive_subtotal_is_backed_out():
    inv = dict(lines=[dict(description="pH meter", qty=4, unit_price=27730.0, amount=110920.0)],
               taxes=[dict(type="IGST", rate=18.0, amount=16920.0)], subtotal=None, total=110920.0,
               tax_inclusive=True)
    assert all(i["ok"] for i in checks.identities(inv))
    assert checks.effective_subtotal(inv) == 94000.0


# ----------------------------------------------------------------------------------- helpers
def test_normalisers():
    assert norm_invoice_no("INV-0042") == norm_invoice_no("INV42") == norm_invoice_no("inv 042")
    assert norm_po("PO 2026/102") == "PO-2026-0102"
    assert parse_money("Rs. 1,42,500.00") == 142500.0
    assert gstin_is_valid(store.BUYER["gstin"])
    assert not gstin_is_valid(store.BUYER["gstin"][:-1] + "0")


# ----------------------------------------------------------------------------------- fraud & handoff
def test_bank_change_is_held_with_a_phone_check():
    store.init(reset=True)
    r = run_sample("10_fraud_sundaram_bank_change.pdf")
    h = r["result"]["handoff"][0]
    assert h["code"] == "BANK_CHANGE" and h["owner"] == "Vendor master team"
    assert "number already on file" in h["question"] and h["sla_hours"] == 24
    assert r["risk"] >= 45


def test_lookalike_is_frozen_without_tipping_off():
    store.init(reset=True)
    r = run_sample("11_fraud_lookalike_vendor.pdf")
    assert r["result"]["handoff"][0]["code"] == "FRAUD_HIGH"
    assert r["result"]["vendor_email"] is None          # never send a detailed note to a suspected impostor
    assert "imitating" in r["result"]["invoice"]["vendor"]


def test_clean_invoice_has_no_fraud_signals_and_no_handoff():
    store.init(reset=True)
    r = run_sample("01_happy_sundaram_clean.pdf")
    assert r["risk"] == 0 and r["result"]["handoff"] == []


def test_every_exception_has_owner_question_and_deadline():
    store.init(reset=True)
    for name in EXPECTED:
        res = run_sample(name)["result"]
        for h in res["handoff"]:
            assert h["owner"] and h["question"].endswith(("?", ".")) and h["due_by"] and h["evidence"]


def test_bank_parsing_and_scan_tolerance():
    assert risk.find_bank("Bank: HDFC ****4471 | terms")[0] == "HDFC ****4471"
    assert risk.find_bank("pay to our NEW account: Yes Bank ****8812")[0] == "YES BANK ****8812"
    assert risk._same_bank("HDFC ****4471", "HDFC ****4471", False) == (True, False)
    assert risk._same_bank("HDFC ****4471", "HDFC ****4477", True) == (True, True)   # 1 digit on a scan
    assert risk._same_bank("HDFC ****4471", "HDFC ****4477", False)[0] is False       # never on a digital PDF


def test_every_field_has_a_location_on_the_page():
    from app.extract_rules import extract
    from app.reader import read_pdf
    for name in ("01_happy_sundaram_clean.pdf", "05_edge_murugan_scanned.pdf"):
        f = extract(read_pdf((ROOT / "samples" / name).read_bytes()), store.BUYER["gstin"])
        for k in ("invoice_number", "invoice_date", "po_number", "total", "vendor_gstin"):
            b = f[k]["box"]
            assert b and 0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1, (name, k, b)



def test_scanned_hindi_invoice(tmp_path):
    """The Hindi invoice photocopied: Hindi OCR + skeleton labels still read it."""
    import pytesseract
    if "hin" not in pytesseract.get_languages(config=""):
        pytest.skip("Hindi OCR model not installed")
    sys.path.insert(0, str(ROOT / "scripts"))
    import make_data
    out = tmp_path / "hindi_scan.pdf"
    make_data.scan_it(ROOT / "samples" / "16_hindi_ganesh_traders.pdf", out, dpi=150, seed=3)
    store.init(reset=True)
    r = pipeline.new_run(out.name, None)
    pipeline.execute(r, out.read_bytes())
    r = store.get_run(r["id"])
    inv = r["result"]["invoice"]
    assert r["source"] == "ocr" and inv["po_number"] == "PO-2026-0113" and inv["invoice_date"] == "2026-10-28"
    assert inv["total"] == 46200.0
