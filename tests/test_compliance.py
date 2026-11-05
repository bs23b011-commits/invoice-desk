"""Step 4: e-invoice, TDS, MSME, GSTR-2B."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["STAGE_DELAY"] = "0"
os.environ.setdefault("RUNTIME_DIR", tempfile.mkdtemp())
os.environ.setdefault("DEMO_TODAY", "2026-11-04")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import compliance, pipeline, store  # noqa: E402


def run(name):
    store.init(reset=True)
    r = pipeline.new_run(name, name)
    pipeline.execute(r, (ROOT / "samples" / name).read_bytes())
    return store.get_run(r["id"])


def tax(r):
    return next(s for s in r["stages"] if s["key"] == "tax")


def test_freight_tds_2pct_for_llp_and_net_payable():
    r = run("02_happy_brightline_tolerance.pdf")
    d = tax(r)["data"]
    assert d["tds_total"] == 3558 and d["net_payable"] == 209922 - 3558
    assert r["result"]["payment"]["amount"] == 206364 and r["result"]["payment"]["tds"] == 3558


def test_technical_services_tds():
    assert tax(run("04_happy_nimbus_partial_billing.pdf"))["data"]["tds_total"] == 2400


def test_small_labour_below_threshold_no_tds():
    d = tax(run("05_edge_murugan_scanned.pdf"))["data"]
    assert d["tds_total"] == 0 and d["tds"][0]["base"] == 12000


def test_msme_due_date_capped_at_45_days():
    r = run("03_happy_apex_tax_inclusive.pdf")
    assert r["result"]["invoice"]["due_date"] == "2026-12-06"  # 22 Oct + 45, terms would say 21 Dec


def test_irn_present_and_consistent():
    assert any("matches this invoice" in f["text"] for f in tax(run("01_happy_sundaram_clean.pdf"))["findings"])


def test_missing_irn_for_einvoice_vendor_holds():
    r = run("13_edge_brightline_no_einvoice.pdf")
    assert r["result"]["handoff"][0]["code"] == "IRN_MISSING"
    assert any(m["code"] == "IRN_MISSING" and m["party"] == "vendor" for m in r["thread"])


def test_irn_for_another_document_is_caught():
    v = store.VENDORS_BY_ID["V001"]
    good = compliance.irn_for(v["gstin"], "2026-10-24", "SPS/26-27/0418")
    assert good != compliance.irn_for(v["gstin"], "2026-10-24", "SPS/26-27/0419")
    assert compliance.fy_of(__import__("datetime").date(2027, 3, 31)) == "2026-27"


def test_pan_kind():
    assert compliance.pan_kind("29AAKFB3390P1ZR") == "firm/LLP"
    assert compliance.pan_kind("33ACQPM6634D1ZK") == "individual"
    assert compliance.pan_kind("36AADCN5567H1Z7") == "company"
