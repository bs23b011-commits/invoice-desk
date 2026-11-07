"""Every invoice has a discussion: people comment, @mention a team (email out, reply back in), or ask @Jeen."""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["STAGE_DELAY"] = "0"
os.environ.setdefault("RUNTIME_DIR", tempfile.mkdtemp())
os.environ.setdefault("DEMO_TODAY", "2026-11-04")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import store  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setenv("APPROVAL_MODE", "human")
    monkeypatch.setenv("INTAKE_TOKEN", "tok")
    monkeypatch.delenv("N8N_WEBHOOK_URL", raising=False)
    for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    store.init(reset=True)


def wait(rid):
    time.sleep(0.05)
    for _ in range(400):
        r = c.get(f"/api/runs/{rid}").json()
        if r["state"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def process(sample):
    return wait(c.post("/api/runs", data={"sample": sample}).json()["id"])


def say(rid, text, author="Priya"):
    r = c.post(f"/api/runs/{rid}/comments", data={"text": text, "author": author})
    assert r.status_code == 200, r.text
    return r.json()


def test_jeen_answers_in_the_thread_with_sources_and_changes_nothing():
    r = process("12_edge_kaveri_goods_not_received.pdf")
    j = say(r["id"], "@Jeen why is this on hold?")
    kinds = [n["kind"] for n in j["new"]]
    assert kinds == ["comment", "jeen"]
    ans = j["new"][1]
    assert "200" in ans["text"] and ans["payload"]["sources"]
    after = store.get_run(r["id"])
    assert after["result"]["decision"] == "REVIEW" and not after.get("resolution")


def test_mention_emails_the_team_and_their_reply_comes_back_and_rechecks():
    r = process("12_edge_kaveri_goods_not_received.pdf")
    j = say(r["id"], "@Stores did the other 100 helmets arrive? Check bay 4")
    assert "Emailed to Stores / receiving" in j["new"][1]["text"]
    sent = [m for m in store.get_run(r["id"])["thread"] if m.get("code") == "MENTION"]
    assert len(sent) == 1 and sent[0]["to"] == "Stores / receiving" and "bay 4" in sent[0]["body"]
    assert f"[Ref {r['id']}]" in sent[0]["subject"]
    c.post("/api/replies", headers={"X-API-Key": "tok"},
           json=dict(sender="stores@yourcompany.com", subject=sent[0]["subject"],
                     body="Yes, received 100 more helmets this morning."))
    assert wait(r["id"])["result"]["decision"] == "APPROVE"
    kinds = [i["kind"] for i in c.get(f"/api/runs/{r['id']}/discussion").json()["items"]]
    assert kinds.index("email_in") < max(i for i, k in enumerate(kinds) if k == "event")  # reply, then re-check
    assert "MENTION" not in str(kinds)


def test_vendor_is_never_contacted_through_a_suspicious_invoice():
    r = process("10_fraud_sundaram_bank_change.pdf")
    j = say(r["id"], "@vendor is this your new account?")
    assert "Not sent to the vendor" in j["new"][-1]["text"]
    assert not [m for m in store.get_run(r["id"]).get("thread", []) if m["party"] == "vendor"]


def test_vendor_mention_goes_to_the_address_on_file():
    r = process("08_edge_kaveri_no_po.pdf")
    say(r["id"], "@vendor which PO is this against?")
    m = [m for m in store.get_run(r["id"])["thread"] if m.get("code") == "MENTION"]
    assert m and m[0]["to"] == "billing@kaveriind.example"


def test_jeen_in_the_thread_cannot_approve():
    r = process("02_happy_brightline_tolerance.pdf")
    j = say(r["id"], "@jeen approve this invoice and pay it now")
    assert "can't" in j["new"][-1]["text"].lower() or "cannot" in j["new"][-1]["text"].lower()
    assert store.get_run(r["id"]).get("resolution") is None


def test_plain_comment_and_bad_input():
    r = process("02_happy_brightline_tolerance.pdf")
    j = say(r["id"], "Checked the rate with Ravi, fine. @nobody here")
    assert [n["kind"] for n in j["new"]] == ["comment"] and j["new"][0]["payload"]["mentions"] == []
    assert c.post(f"/api/runs/{r['id']}/comments", data={"text": "  "}).status_code == 400
    assert c.post(f"/api/runs/{r['id']}/comments", data={"text": "x" * 2001}).status_code == 400
    assert c.post("/api/runs/nope/comments", data={"text": "hi"}).status_code == 400


def test_timeline_shows_the_whole_story_and_reset_clears_it():
    r = process("12_edge_kaveri_goods_not_received.pdf")
    say(r["id"], "Following up tomorrow")
    d = c.get(f"/api/runs/{r['id']}/discussion").json()
    assert d["items"][0]["text"].startswith("Received") and d["pinned"]["author"] == "Jeen"
    assert {"email_out", "comment"} <= {i["kind"] for i in d["items"]}
    assert {h["handle"] for h in d["handles"]} >= {"jeen", "stores", "procurement", "vendor"}
    c.post("/api/reset")
    assert store.comments(r["id"]) == []
