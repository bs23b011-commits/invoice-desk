"""Step 6a: learning from reviewer corrections."""
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

from app import store  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)


@pytest.fixture(autouse=True)
def fresh():
    store.init(reset=True)


def wait(rid):
    time.sleep(0.1)
    for _ in range(400):
        r = c.get(f"/api/runs/{rid}").json()
        if r["state"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("timeout")


def run(sample):
    return wait(c.post("/api/runs", data={"sample": sample}).json()["id"])


def correct(rid, **fields):
    return c.post(f"/api/runs/{rid}/correct", data=fields).json(), wait(rid)


def test_new_layout_is_learned_and_reused():
    r = run("14_learn_nimbus_new_layout.pdf")
    assert r["result"]["decision"] == "REVIEW"
    j, r = correct(r["id"], invoice_number="NCS/BKP/2026/031", invoice_date="2026-11-03")
    assert {(l["field"], l["label"]) for l in j["learned"]} == {("invoice_number", "Document Ref"), ("invoice_date", "Dated")}
    assert r["result"]["decision"] == "APPROVE"
    r2 = run("15_learn_nimbus_next_invoice.pdf")
    assert r2["result"]["decision"] == "APPROVE" and r2["result"]["invoice"]["invoice_number"] == "NCS/BKP/2026/032"
    ex = next(s for s in r2["stages"] if s["key"] == "extract")
    assert any("learned from AP reviewer" in f["text"] for f in ex["findings"])


def test_reviewer_typo_is_caught_by_irn_not_learned():
    r = run("14_learn_nimbus_new_layout.pdf")
    j, r = correct(r["id"], invoice_number="NCS/BKP/2026/013", invoice_date="2026-11-03")
    assert not any(l["field"] == "invoice_number" for l in j["learned"])   # not on the page -> nothing learned
    assert r["result"]["decision"] == "REVIEW" and r["result"]["handoff"][0]["code"] == "IRN_MISMATCH"


def test_learning_is_per_vendor():
    r = run("14_learn_nimbus_new_layout.pdf")
    correct(r["id"], invoice_number="NCS/BKP/2026/031", invoice_date="2026-11-03")
    assert store.layout_memory("V001") == {} and "invoice_number" in store.layout_memory("V004")


def test_learned_gl_code_via_api():
    assert c.post("/api/gl/remember", data=dict(vendor_id="V001", description="X", account="9999")).status_code == 400
    assert c.post("/api/gl/remember", data=dict(vendor_id="V001", description="Box", account="5110")).json()["ok"]
    assert c.get("/api/learning").json()["gl"][0]["account"] == "5110"
