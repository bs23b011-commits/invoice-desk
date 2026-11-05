"""Step 5: GL coding, posting with read-back, payment batches."""
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

from app import erp, pipeline, store  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)


@pytest.fixture(autouse=True)
def fresh():
    store.init(reset=True)


def run(name):
    r = pipeline.new_run(name, name)
    store.file_path(r["id"], "pdf").write_bytes((ROOT / "samples" / name).read_bytes())
    pipeline.execute(r, (ROOT / "samples" / name).read_bytes())
    return store.get_run(r["id"])


def post_stage(r):
    return next(s for s in r["stages"] if s["key"] == "post")


def test_approved_invoice_is_posted_balanced_and_read_back():
    r = run("02_happy_brightline_tolerance.pdf")
    d = post_stage(r)["data"]
    assert d["journal"]["balanced"] and all(x["ok"] for x in d["readback"])
    payable = next(l for l in d["journal"]["lines"] if l["account"] == "2110")
    tds = next(l for l in d["journal"]["lines"] if l["account"] == "2220")
    assert payable["credit"] == 206364 and tds["credit"] == 3558
    assert r["result"]["erp_doc"].startswith("AP/26-27/")


def test_scan_posts_repaired_numbers():
    d = post_stage(run("05_edge_murugan_scanned.pdf"))["data"]
    cgst = next(l for l in d["journal"]["lines"] if l["account"] == "1410")
    assert cgst["debit"] == 8676 and d["journal"]["balanced"]


def test_capital_vs_consumable_coding():
    coding = {c["line"][:10]: c["account"] for c in post_stage(run("03_happy_apex_tax_inclusive.pdf"))["data"]["journal"]["coding"]}
    assert coding["Benchtop p"] == "1520" and coding["Calibratio"] == "5350"


def test_not_approved_is_not_posted():
    assert "nothing posted" in post_stage(run("06_edge_kaveri_split_po_overbilled.pdf"))["findings"][0]["text"]
    assert erp.erp().docs() == []


def test_human_approval_posts_it():
    r = run("06_edge_kaveri_split_po_overbilled.pdf")
    pipeline.resolve(r["id"], "approve", "ok", "Procurement (PO owner)")
    r = store.get_run(r["id"])
    assert r["result"]["erp_doc"] and len(erp.erp().docs()) == 1


def test_never_posted_twice():
    r = run("01_happy_sundaram_clean.pdf")
    fs, data = erp.post_run(r)
    assert "not posted twice" in fs[0]["text"] and len(erp.erp().docs()) == 1


def test_learned_gl_code_wins():
    store.remember_gl("V001", "Corrugated boxes 5-ply 480x320x300", "5320", "AP reviewer")
    coding = post_stage(run("01_happy_sundaram_clean.pdf"))["data"]["journal"]["coding"]
    assert coding[0]["account"] == "5320" and coding[0]["how"] == "learned from a reviewer"


def test_payment_batch_bank_file_and_paid():
    run("01_happy_sundaram_clean.pdf")
    run("05_edge_murugan_scanned.pdf")
    p = c.get("/api/payments").json()
    assert p["open"][0]["msme"] == 1  # MSME first
    docs = ",".join(d["doc_no"] for d in p["open"])
    b = c.post("/api/payments/batch", data={"docs": docs}).json()
    assert b["total"] == round(168150 + 113752, 2)
    csv = c.get(f"/api/payments/batch/{b['id']}.csv").text
    assert "HDFC ****4471" in csv and "Canara ****2290" in csv
    c.post(f"/api/payments/batch/{b['id']}/paid")
    assert {d["status"] for d in erp.erp().docs()} == {"paid"}
    assert c.get("/api/payments").json()["open"] == []


def test_tds_deposit_summary():
    run("02_happy_brightline_tolerance.pdf")
    run("04_happy_nimbus_partial_billing.pdf")
    t = c.get("/api/payments").json()["tds"]
    assert sum(x["amount"] for x in t) == 3558 + 2400 and t[0]["deposit_by"].endswith("-07")


def _open_db_files():
    fd = Path("/proc/self/fd")
    return [p for p in (os.readlink(f) for f in fd.iterdir() if f.is_symlink()) if p.endswith(".db")]


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc")
def test_reset_after_processing_leaves_no_database_open():
    """Windows cannot delete or reset a file that is still open. Every connection must be closed."""
    run("02_happy_brightline_tolerance.pdf")
    assert _open_db_files() == []
    assert c.post("/api/reset").json() == {"ok": True}
    assert erp.erp().find("V-BRIGHT", "anything") is None and _open_db_files() == []
    r = run("02_happy_brightline_tolerance.pdf")  # works again after reset, ERP starts empty
    assert post_stage(r)["data"]["doc_no"].endswith("00001")
