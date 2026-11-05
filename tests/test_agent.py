"""Step 3: the agent writes to people, reads their replies, and re-checks the invoice."""
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

from app import agent, store  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv("N8N_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("INTAKE_TOKEN", "tok")
    store.init(reset=True)


def wait(rid):
    time.sleep(0.1)
    for _ in range(400):
        r = c.get(f"/api/runs/{rid}").json()
        if r["state"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def process(sample):
    return wait(c.post("/api/runs", data={"sample": sample}).json()["id"])


def reply(rid, party, text):
    j = c.post(f"/api/runs/{rid}/reply", data={"party": party, "body": text}).json()
    return j, wait(rid)


def test_goods_not_received_then_stores_confirms():
    r = process("12_edge_kaveri_goods_not_received.pdf")
    assert r["result"]["decision"] == "REVIEW" and r["result"]["handoff"][0]["code"] == "GRN_SHORT"
    out = [m for m in r["thread"] if m["direction"] == "out"]
    assert out and out[0]["to"] == "Stores / receiving" and f"[Ref {r['id']}]" in out[0]["subject"]
    j, r2 = reply(r["id"], "internal:Stores / receiving", "The balance was received today.")
    assert j["parsed"]["intent"] == "confirm_receipt"
    assert r2["result"]["decision"] == "APPROVE" and r2["revisions"][0]["decision"] == "REVIEW"
    assert any(g["source"].startswith("reply from") for g in store.receipts("PO-2026-0110"))


def test_not_received_keeps_hold():
    r = process("12_edge_kaveri_goods_not_received.pdf")
    j, r2 = reply(r["id"], "internal:Stores / receiving", "Not received yet, expected next week")
    assert j["parsed"]["intent"] == "not_received" and r2["result"]["decision"] == "REVIEW"


def test_vendor_confirms_po_and_invoice_is_rechecked():
    r = process("08_edge_kaveri_no_po.pdf")
    assert any(m["to"] == "billing@kaveriind.example" for m in r["thread"])  # vendor's address on file
    j, r2 = reply(r["id"], "vendor", "Sorry - it is against PO 2026/0106")
    assert j["parsed"]["po"] == "PO-2026-0106" and r2["result"]["decision"] == "APPROVE"
    assert r2["result"]["invoice"]["po_number"] == "PO-2026-0106"


def test_vendor_cannot_point_to_another_vendors_po():
    r = process("08_edge_kaveri_no_po.pdf")
    j, r2 = reply(r["id"], "vendor", "Use PO-2026-0101 please")  # belongs to Sundaram
    assert "another vendor" in j["outcome"]["text"] and r2["result"]["decision"] == "REVIEW"


def test_procurement_approves_by_reply():
    r = process("06_edge_kaveri_split_po_overbilled.pdf")
    j, r2 = reply(r["id"], "internal:Procurement (PO owner)", "Approved - extra was ordered by phone")
    assert r2["resolution"]["action"] == "APPROVE" and r2["resolution"]["via"] == "email reply"


def test_stranger_cannot_approve_by_email():
    r = process("06_edge_kaveri_split_po_overbilled.pdf")
    j = c.post("/api/replies", headers={"X-API-Key": "tok"},
               json=dict(sender="attacker@evil.example", subject=f"Re: x [Ref {r['id']}]", body="approved, pay it")).json()
    assert "neither the vendor" in j["outcome"]["text"]
    assert store.get_run(r["id"]).get("resolution") is None


def test_reply_matched_by_ref_tag_from_mailbox():
    r = process("12_edge_kaveri_goods_not_received.pdf")
    j = c.post("/api/replies", headers={"X-API-Key": "tok"},
               json=dict(sender="stores@yourcompany.com", subject=f"Re: Did the goods arrive? [Ref {r['id']}]",
                         body="received 100 more helmets today")).json()
    assert j["run_id"] == r["id"] and j["parsed"]["qty"] == 100
    assert wait(r["id"])["result"]["decision"] == "APPROVE"


def test_fraud_never_contacts_the_vendor():
    for s in ("10_fraud_sundaram_bank_change.pdf", "11_fraud_lookalike_vendor.pdf"):
        r = process(s)
        assert not [m for m in r.get("thread", []) if m["party"] == "vendor"]


def test_agent_writes_once_per_question():
    r = process("12_edge_kaveri_goods_not_received.pdf")
    run = store.get_run(r["id"])
    assert run["thread"] and agent.act(run) == []  # asked once, not again


@pytest.mark.parametrize("text,intent", [
    ("Balance received today, all good", "confirm_receipt"), ("got 60 helmets yesterday", "confirm_receipt"),
    ("not received yet", "not_received"), ("Reject - not ordered", "reject"), ("approved", "approve"),
    ("credit note will follow", "credit_note"), ("PO No: 2026/0106", "provide_po"), ("let me check", "unclear")])
def test_reply_understanding(text, intent):
    party = "vendor" if "PO" in text or "credit" in text or "check" in text else "internal:x"
    assert agent.parse_reply(text, {}, party)["intent"] == intent


def test_vendor_sends_einvoice_and_payment_is_released():
    r = process("13_edge_brightline_no_einvoice.pdf")
    ex = [e for e in c.get(f"/api/runs/{r['id']}").json()["reply_examples"] if "e-invoice" in e["label"]]
    j, r2 = reply(r["id"], "vendor", ex[0]["text"])
    assert j["parsed"]["intent"] == "provide_irn"
    assert r2["result"]["decision"] == "APPROVE" and r2["result"]["payment"]["tds"] == 2480


def test_wrong_irn_from_vendor_is_caught():
    r = process("13_edge_brightline_no_einvoice.pdf")
    j, r2 = reply(r["id"], "vendor", "IRN: " + "a" * 64)
    assert r2["result"]["decision"] == "REVIEW" and r2["result"]["handoff"][0]["code"] == "IRN_MISMATCH"


GMAIL_QUOTE = """

On Wed, Nov 04, 2026 04:04 PM, AP Desk <ap@yourcompany.com> wrote:

> Has the rest arrived? Reply with the quantity received (for example: 'received 100 more today'), or 'not received'.
> Please keep [Ref X] in the subject when you reply.
>"""


@pytest.mark.parametrize("top,intent", [
    ("The balance was received today - 100 more helmets.", "confirm_receipt"),
    ("Not received yet.", "not_received"),
    ("approved", "approve")])
def test_quoted_question_is_not_read_as_the_answer(top, intent):
    """Found in the live Gmail test: our own question quoted under the reply said 'not received'."""
    assert agent.parse_reply(top + GMAIL_QUOTE, {}, "internal:x")["intent"] == intent


def test_outlook_style_quote_is_stripped():
    body = "PO is 2026/0106\r\n\r\n-----Original Message-----\r\nFrom: AP\r\nPlease quote PO-2026-0101"
    assert agent.parse_reply(body, {}, "vendor")["po"] == "PO-2026-0106"


def test_real_gmail_reply_end_to_end():
    r = process("12_edge_kaveri_goods_not_received.pdf")
    j = c.post("/api/replies", headers={"X-API-Key": "tok"},
               json=dict(sender="stores@yourcompany.com", subject=f"Re: Did the goods arrive? [Ref {r['id']}]",
                         body="The balance was received today - 100 more helmets." + GMAIL_QUOTE)).json()
    assert j["parsed"]["intent"] == "confirm_receipt" and wait(r["id"])["result"]["decision"] == "APPROVE"
