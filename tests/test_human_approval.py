"""A person gives the final OK on every invoice. The agent recommends; nothing is posted or paid before a click."""
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
from fastapi.testclient import TestClient  # noqa: E402

from app import erp, integrations, pipeline, store  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)


@pytest.fixture(autouse=True)
def human(monkeypatch):
    monkeypatch.setenv("APPROVAL_MODE", "human")
    monkeypatch.delenv("N8N_WEBHOOK_URL", raising=False)
    store.init(reset=True)


def run(name):
    r = pipeline.new_run(name, name)
    pdf = (ROOT / "samples" / name).read_bytes()
    store.file_path(r["id"], "pdf").write_bytes(pdf)
    pipeline.execute(r, pdf)
    return store.get_run(r["id"])


def decide(rid, action, note="", verified=""):
    return c.post(f"/api/runs/{rid}/resolve", data=dict(action=action, note=note, verified=verified, who="Priya (AP)"))


def test_clean_invoice_waits_for_a_person():
    r = run("02_happy_brightline_tolerance.pdf")
    res = r["result"]
    assert res["decision"] == "APPROVE" and res["awaiting_human"] and res["headline"].startswith("Recommend approval")
    assert not res.get("erp_doc") and erp.erp().find("V003", res["invoice"]["invoice_number"]) is None
    assert c.get("/api/payments").json()["open"] == []
    waiting = c.get("/api/waiting").json()["items"]
    assert [w["id"] for w in waiting] == [r["id"]] and waiting[0]["recommendation"] == "APPROVE"


def test_person_approves_then_it_is_posted_and_scheduled():
    r = run("02_happy_brightline_tolerance.pdf")
    j = decide(r["id"], "approve", "Looks right").json()
    assert j["resolution"]["by"] == "Priya (AP)" and j["result"]["erp_doc"]
    assert j["result"]["payment"]["bank"] == store.VENDORS_BY_ID["V003"]["bank"]  # the account on file
    assert len(c.get("/api/payments").json()["open"]) == 1
    assert c.get("/api/waiting").json()["items"] == []
    assert decide(r["id"], "approve").status_code == 400  # never twice


def test_person_can_overrule_an_approve_recommendation():
    r = run("02_happy_brightline_tolerance.pdf")
    j = decide(r["id"], "reject", "Rate was renegotiated").json()
    assert j["resolution"]["action"] == "REJECT" and not j["result"].get("erp_doc")
    assert all(x["status"] == "REJECT" for x in store.ledger() if x["run_id"] == r["id"])


def test_reject_recommendation_is_confirmed_by_a_person():
    r = run("09_extra_rapidprint_blocked_vendor.pdf")
    assert r["result"]["decision"] == "REJECT" and r["result"]["awaiting_human"]
    assert "write why" in decide(r["id"], "approve", "ok").json()["detail"]  # overriding needs a reason
    j = decide(r["id"], "reject").json()
    assert j["resolution"]["action"] == "REJECT"


def test_bank_change_needs_the_phone_check():
    r = run("10_fraud_sundaram_bank_change.pdf")
    assert "phone check" in decide(r["id"], "approve").json()["detail"]
    j = decide(r["id"], "approve", "Called Sundaram on file number", verified="1").json()
    assert j["resolution"]["action"] == "APPROVE"


def test_notification_carries_approve_reject_links_for_the_approver():
    r = run("02_happy_brightline_tolerance.pdf")
    s = integrations.summary(store.get_run(r["id"]))
    assert s["awaiting_human"] and s["approval"]["owner"] == "AP manager" and "/a/" in s["approval"]["approve"]
    assert s["text"].startswith("Waiting for your OK")


def test_waiting_card_explains_the_recommendation():
    r = run("12_edge_kaveri_goods_not_received.pdf")
    w = c.get("/api/waiting").json()["items"][0]
    assert w["recommendation"] == "REVIEW" and w["owner"]["name"] == "Stores / receiving"
    line = w["match"]["lines"][0]
    assert (line["ordered"], line["received"], line["billed"]) == (300, 200, 300) and not line["ok"]
    assert {ch["key"] for ch in w["checks"]} >= {"validate", "vendor", "duplicate", "po", "risk", "tax"}
    assert any(m["direction"] == "out" for m in w["thread"])


def test_one_click_link_from_slack_or_email_approves_and_posts():
    r = run("02_happy_brightline_tolerance.pdf")
    link = integrations.summary(store.get_run(r["id"]))["approval"]["approve"]
    path = "/a/" + link.split("/a/")[1]
    assert "recommends approval" in c.get(path).text  # opening the link only asks
    assert store.get_run(r["id"]).get("resolution") is None
    assert "Approved" in c.post(path, data={"note": "ok"}).text
    assert store.get_run(r["id"])["result"]["erp_doc"]
