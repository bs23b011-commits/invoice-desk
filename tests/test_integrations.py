"""Integration API used by n8n: intake, outbound webhook, one-click links, escalations."""
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

os.environ["STAGE_DELAY"] = "0"
os.environ.setdefault("RUNTIME_DIR", tempfile.mkdtemp())
os.environ.setdefault("DEMO_TODAY", "2026-11-04")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import integrations, store  # noqa: E402
from app.main import app  # noqa: E402

RECEIVED = []


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        RECEIVED.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture(autouse=True)
def env(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("INTAKE_TOKEN", "secret-token")
    monkeypatch.setenv("N8N_WEBHOOK_URL", f"http://127.0.0.1:{srv.server_port}/hook")
    monkeypatch.setenv("PUBLIC_URL", "http://testserver")
    RECEIVED.clear()
    store.init(reset=True)
    yield
    srv.shutdown()


client = TestClient(app)
H = {"X-API-Key": "secret-token"}


def intake(name, message_id, headers=H, **form):
    return client.post("/api/intake", headers=headers,
                       files={"file": (name, (ROOT / "samples" / name).read_bytes(), "application/pdf")},
                       data=dict(sender="accounts@vendor.example", subject="Invoice", message_id=message_id,
                                 wait="true", **form))


def test_intake_requires_token():
    assert intake("01_happy_sundaram_clean.pdf", "m0", headers={}).status_code == 401


def test_intake_processes_and_records_the_email():
    j = intake("01_happy_sundaram_clean.pdf", "m1").json()
    assert j["status"] == "processed" and j["decision"] == "APPROVE"
    run = store.get_run(j["run_id"])
    assert run["channel"]["channel"] == "email" and run["channel"]["sender"] == "accounts@vendor.example"


def test_same_email_attachment_is_processed_once():
    first = intake("01_happy_sundaram_clean.pdf", "m2").json()
    again = intake("01_happy_sundaram_clean.pdf", "m2").json()
    assert again["status"] == "already_received" and again["run_id"] == first["run_id"]


def test_non_pdf_is_skipped():
    r = client.post("/api/intake", headers=H, files={"file": ("logo.png", b"\x89PNG....", "image/png")})
    assert r.json()["status"] == "skipped"


def test_decision_is_pushed_to_webhook():
    intake("06_edge_kaveri_split_po_overbilled.pdf", "m3")
    for _ in range(80):
        if any(m["event"] == "invoice.processed" for m in RECEIVED):
            break
        time.sleep(0.05)
    msg = next(m for m in RECEIVED if m["event"] == "invoice.processed")
    assert msg["event"] == "invoice.processed" and msg["decision"] == "REVIEW"
    assert msg["handoff"][0]["approve"].startswith("http://testserver/a/") and "Question:" in msg["text"]


def test_one_click_link_flow_and_bank_change_needs_phone_check():
    j = intake("10_fraud_sundaram_bank_change.pdf", "m4").json()
    link = j["handoff"][0]["approve"].replace("http://testserver", "")
    assert client.get(link).status_code == 200 and store.get_run(j["run_id"]).get("resolution") is None  # GET is safe
    assert "Not done" in client.post(link, data={"note": "ok"}).text            # no phone check -> refused
    assert "Approved" in client.post(link, data={"verified": "1"}).text
    assert store.get_run(j["run_id"])["resolution"]["via"] == "one-click link"
    assert "Not done" in client.post(link, data={"verified": "1"}).text          # can't be used twice


def test_tampered_or_expired_links_are_refused():
    j = intake("08_edge_kaveri_no_po.pdf", "m5").json()
    link = j["handoff"][0]["reject"].replace("http://testserver", "")
    assert "not valid" in client.get(link[:-4] + "AAAA").text
    old = integrations.sign(j["run_id"], "approve", "AP reviewer", ttl_days=-1)
    assert "expired" in client.get(f"/a/{old}").text


def test_escalation_fires_once_after_deadline():
    intake("06_edge_kaveri_split_po_overbilled.pdf", "m6")
    assert client.post("/api/escalations/run", headers=H).json()["count"] == 0
    later = "2030-01-01T00:00:00Z"
    j = client.post(f"/api/escalations/run?as_of={later}", headers=H).json()
    assert j["count"] == 1 and j["items"][0]["escalate_to"] == "Category manager"
    assert client.post(f"/api/escalations/run?as_of={later}", headers=H).json()["count"] == 0


def test_signature_roundtrip():
    t = integrations.sign("run-1", "reject", "Tax team")
    d = integrations.verify(t)
    assert d["r"] == "run-1" and d["a"] == "reject" and d["w"] == "Tax team"
    assert datetime.fromtimestamp(d["e"], timezone.utc) > datetime.now(timezone.utc)


def test_agent_message_is_pushed_to_webhook():
    intake("12_edge_kaveri_goods_not_received.pdf", "m9")
    for _ in range(80):
        if any(m["event"] == "agent.message" for m in RECEIVED):
            break
        time.sleep(0.05)
    m = next(m for m in RECEIVED if m["event"] == "agent.message")["message"]
    assert m["party"] == "internal:Stores / receiving" and "[Ref " in m["subject"]
