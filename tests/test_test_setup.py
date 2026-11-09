"""A tester uploads an invoice from their own company: vendor and PO aren't in our records.
The agent holds it (correct), then 'Test setup' creates the vendor, a matching PO and goods receipt, and the
re-check runs every stage through to a recommendation to approve."""
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
sys.path.insert(0, str(ROOT / "scripts"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import master, store  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)

OUTSIDER = dict(id="Z", name="Lotus Office Supplies Pvt Ltd", state="Tamil Nadu", city="Chennai", terms_days=30,
                email="ar@lotus.example", bank="HDFC ****7781", einvoice=False,
                address=["21, Greams Road", "Chennai 600006"])


@pytest.fixture(autouse=True)
def fresh():
    store.init(reset=True)
    master.clear_imports()
    yield
    master.clear_imports()


def _pdf(tmp_path, po=None, lines=None, number="LOS/26-27/0091"):
    import make_data
    from make_hard import draw
    v = dict(OUTSIDER, gstin=make_data.gstin("33AACCL4410K1Z"))
    lines = lines or [make_data.ln("A4 copier paper 75 gsm (ream)", 200, 245.00, "4802"),
                      make_data.ln("Stapler heavy duty", 10, 380.00, "8472")]
    inv = make_data.build_invoice("V001", number, "2026-10-30", "2026-11-29", po, lines, no_irn=True)
    inv["irn"] = None
    p = tmp_path / f"{number.replace('/', '_')}.pdf"
    draw(p, v, inv, dict(no_irn=True))
    return p, inv


def _wait(rid):
    for _ in range(400):
        r = c.get(f"/api/runs/{rid}").json()
        if r["state"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def _upload(p):
    rid = c.post("/api/runs", files={"file": (p.name, p.read_bytes(), "application/pdf")}).json()["id"]
    return _wait(rid)


def test_own_invoice_without_a_po_goes_end_to_end(tmp_path):
    p, inv = _pdf(tmp_path)
    r = _upload(p)
    assert r["result"]["decision"] == "REVIEW"
    card = next(x for x in c.get("/api/waiting").json()["items"] if x["id"] == r["id"])
    assert card["test_setup"] is True
    out = c.post(f"/api/runs/{r['id']}/test-setup", data={"who": "Judge"}).json()
    assert out["po_number"].startswith("PO-TEST-")
    r = _wait(r["id"])
    assert r["result"]["decision"] == "APPROVE", r["result"]["headline"]
    assert abs(r["result"]["payment"]["amount"] - inv["total"]) < 1
    assert any("test setup" in (x.get("reason") or "") for x in r.get("revisions") or [])
    # the made-up records are labelled and removable
    assert any(p_.get("test_data") for p_ in store.POS)
    master.clear_imports()
    assert not any(p_.get("test_data") for p_ in store.POS)


def test_own_invoice_quoting_its_own_po_number(tmp_path):
    p, _ = _pdf(tmp_path, po="PO/LOS/7781", number="LOS/26-27/0092")
    r = _upload(p)
    assert r["result"]["decision"] == "REVIEW"
    out = c.post(f"/api/runs/{r['id']}/test-setup").json()
    assert out["po_number"] == "PO/LOS/7781"  # the PO the invoice quotes is created, not a made-up one
    r = _wait(r["id"])
    assert r["result"]["decision"] == "APPROVE", r["result"]["headline"]


def test_real_problems_still_show_after_test_setup(tmp_path):
    """Test setup creates the records, it doesn't hide problems: a broken invoice still isn't approved."""
    import make_data
    p, _ = _pdf(tmp_path, number="LOS/26-27/0093")
    r = _upload(p)
    c.post(f"/api/runs/{r['id']}/test-setup")
    r = _wait(r["id"])
    assert r["result"]["decision"] == "APPROVE"
    # the same invoice sent again is a duplicate, test data or not
    r2 = _upload(p)
    assert r2["result"]["decision"] == "REJECT"
    assert c.post(f"/api/runs/{r2['id']}/test-setup").status_code == 400


def test_not_offered_for_known_vendors():
    """Kaveri is a real vendor whose invoice has no PO: that's a question for Procurement, not test data."""
    r = c.post("/api/runs", data={"sample": "08_edge_kaveri_no_po.pdf"}).json()
    r = _wait(r["id"])
    assert r["result"]["decision"] == "REVIEW"
    card = next(x for x in c.get("/api/waiting").json()["items"] if x["id"] == r["id"])
    assert card["test_setup"] is False
    assert c.post(f"/api/runs/{r['id']}/test-setup").status_code == 400
