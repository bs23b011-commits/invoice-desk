"""HTTP API + static UI.  Run:  uvicorn app.main:app --reload"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from datetime import datetime, timezone
from html import escape

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from fastapi.responses import PlainTextResponse

from . import agent, cockpit, config, discussion, erp, extract_llm, files, integrations, jeen, learning, master, pipeline, store
from .reader import read_pdf
from .config import TOLERANCE_ABS, TOLERANCE_PCT
from .matching import po_consumption

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
STATIC = ROOT / "static"
MAX_BYTES = 10 * 1024 * 1024

app = FastAPI(title="Invoice Desk")
store.init()


def J(obj):
    return JSONResponse(json.loads(json.dumps(obj, default=str)))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "llm": extract_llm.provider()}


@app.get("/api/samples")
def samples():
    return J(json.loads((SAMPLES / "manifest.json").read_text()))


@app.get("/api/samples/{name}")
def sample_pdf(name: str):
    p = (SAMPLES / name).resolve()
    if p.parent != SAMPLES.resolve() or not p.exists() or p.suffix != ".pdf":
        raise HTTPException(404, "No such sample")
    return FileResponse(p, media_type="application/pdf", filename=name)


@app.post("/api/runs")
async def create_run(sample: str | None = Form(None), file: UploadFile | None = File(None)):
    if file is not None and file.filename:
        data = await file.read()
        name, sample_name = file.filename, None
    elif sample:
        p = (SAMPLES / sample).resolve()
        if p.parent != SAMPLES.resolve() or not p.exists():
            raise HTTPException(404, "No such sample")
        data, name, sample_name = p.read_bytes(), sample, sample
    else:
        raise HTTPException(400, "Upload a PDF or pick a sample")
    if len(data) > MAX_BYTES:
        raise HTTPException(400, "The file is larger than 10 MB")
    try:
        name, data, note = files.normalise(name, data)
    except files.Unreadable as e:
        raise HTTPException(400, str(e))
    run = pipeline.new_run(name, sample_name)
    store.file_path(run["id"], "pdf").write_bytes(data)
    store.add_event(run["id"], "received", f"{name} {'picked from samples' if sample_name else 'uploaded'} in the app"
                    + (f". {note}" if note else ""), dict(channel=run["channel"]["channel"], filename=name))
    threading.Thread(target=pipeline.execute, args=(run, data), daemon=True).start()
    return {"id": run["id"]}


@app.get("/api/runs")
def runs():
    return J(store.list_runs())


@app.get("/api/runs/{rid}")
def get_run(rid: str):
    run = pipeline.LIVE.get(rid) or store.get_run(rid)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.get("state") == "done" and (run.get("result") or {}).get("decision") == "REVIEW" and not run.get("resolution"):
        run = dict(run, reply_examples=agent.simulate_examples(run))
    return J(run)


class Reply(BaseModel):
    sender: str = ""
    subject: str = ""
    body: str
    run_id: str | None = None
    party: str | None = None


@app.post("/api/replies")
def replies(r: Reply, x_api_key: str | None = Header(None)):
    """n8n posts replies from the mailbox here. Matched to the invoice by the [Ref ...] tag in the subject."""
    _check_token(x_api_key)
    return J(agent.receive_reply(r.run_id, r.sender, r.subject, r.body, r.party, "email"))


@app.post("/api/runs/{rid}/reply")
def simulate_reply(rid: str, party: str = Form(...), body: str = Form(...)):
    """Demo: type the reply a vendor or colleague would send, and watch the agent act on it."""
    run = store.get_run(rid)
    if not run:
        raise HTTPException(404, "Run not found")
    vendor = next((v for v in store.VENDORS if v["id"] == ((run.get("result") or {}).get("invoice") or {}).get("vendor_id")), None)
    sender = vendor["email"] if party == "vendor" and vendor else party.split(":", 1)[-1]
    return J(agent.receive_reply(rid, sender, f"Re: [Ref {rid}]", body, party, "simulated in the app"))


@app.get("/api/runs/{rid}/preview.png")
def preview(rid: str):
    p = store.file_path(rid, "png")
    if not p.exists():
        return Response(status_code=204)
    return FileResponse(p, media_type="image/png")


@app.get("/api/runs/{rid}/file.pdf")
def original(rid: str):
    p = store.file_path(rid, "pdf")
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf")


@app.post("/api/runs/{rid}/resolve")
def resolve(rid: str, action: str = Form(...), note: str = Form(""), verified: str = Form(""),
            who: str = Form("AP reviewer")):
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    try:
        return J(pipeline.resolve(rid, action, note, (who or "AP reviewer")[:60], "dashboard", verified == "1"))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ============================================================================== integrations (n8n etc.)
def _check_token(x_api_key: str | None):
    tok = integrations.intake_token()
    if tok and x_api_key != tok:
        raise HTTPException(401, "Missing or wrong X-API-Key header")


@app.post("/api/intake")
async def intake(file: UploadFile = File(...), channel: str = Form("email"), sender: str = Form(""),
                 subject: str = Form(""), message_id: str = Form(""), received_at: str = Form(""),
                 wait: bool = Form(False), x_api_key: str | None = Header(None)):
    """Entry point for automated intake: n8n posts each PDF attachment from the AP mailbox here."""
    _check_token(x_api_key)
    data = await file.read()
    name = file.filename or "invoice.pdf"
    ch0 = dict(channel=channel, sender=sender, subject=subject, message_id=message_id, received_at=received_at)
    if files.is_image(name, data) and not files.looks_like_document_photo(data):
        store.add_event(None, "skipped", f"Ignored '{name}' from {sender or channel}: a small image (logo or signature)",
                        dict(filename=name))
        return {"status": "skipped", "reason": "small image, not an invoice"}
    try:
        name, data, note = files.normalise(name, data)
    except files.Unreadable as e:
        if not (data.startswith(b"%PDF") or files.is_image(name, data)):  # a signature logo, a .docx: not an invoice
            store.add_event(None, "skipped", f"Ignored '{name}' from {sender or channel}: {e}", dict(filename=name))
            return {"status": "skipped", "reason": str(e)}
        run = pipeline.unreadable_run(name, str(e), ch0)  # an invoice we can't read must not get lost
        store.add_event(run["id"], "received", f"{name} received by {channel} but can't be read: {e}", dict(ch0, filename=name))
        return {"status": "unreadable", "run_id": run["id"], "reason": str(e)}
    if message_id:
        prev = store.find_run_by_message(message_id, name)
        if prev:  # n8n retried, or the mailbox was re-polled: never process the same attachment twice
            run = store.get_run(prev)
            return {"status": "already_received", "run_id": prev, **(integrations.summary(run) if run else {})}
    ch = dict(channel=channel, sender=sender, subject=subject, message_id=message_id, received_at=received_at)
    run = pipeline.new_run(name, None, ch)
    store.file_path(run["id"], "pdf").write_bytes(data)
    store.add_event(run["id"], "received", f"{name} received by {channel}" + (f" from {sender}" if sender else "")
                    + (f' ("{subject}")' if subject else ""), dict(ch, filename=name))
    t = threading.Thread(target=pipeline.execute, args=(run, data), daemon=True)
    t.start()
    if wait:
        t.join(90)
        return {"status": "processed", **integrations.summary(store.get_run(run["id"]))}
    return {"status": "accepted", "run_id": run["id"], "url": f"{integrations.public_url()}/#run={run['id']}"}


@app.get("/api/runs/{rid}/summary")
def run_summary(rid: str):
    run = store.get_run(rid)
    if not run:
        raise HTTPException(404, "Run not found")
    return J(integrations.summary(run))


@app.post("/api/escalations/run")
def escalations(as_of: str | None = Query(None), x_api_key: str | None = Header(None)):
    """Called by n8n's timer. Returns questions that passed their deadline, once each."""
    _check_token(x_api_key)
    when = None
    if as_of:
        when = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    items = integrations.run_escalations(when)
    return J({"count": len(items), "items": items})


@app.post("/api/runs/{rid}/correct")
async def correct(rid: str, request: Request):
    """A reviewer types values the reader couldn't find. We learn where they are printed, then re-check."""
    run = store.get_run(rid)
    if not run or (run.get("result") or {}).get("decision") != "REVIEW" or run.get("resolution"):
        raise HTTPException(400, "Only invoices on hold can be corrected")
    form = await request.form()
    fixes = {k: str(v).strip() for k, v in form.items() if k in learning.LEARNABLE and str(v).strip()}
    if not fixes:
        raise HTTPException(400, "Type at least one value")
    vid = ((run.get("result") or {}).get("invoice") or {}).get("vendor_id")
    learned = []
    if vid:
        doc = read_pdf(store.file_path(rid, "pdf").read_bytes(), preview=False)
        learned = learning.learn(doc, vid, fixes, "AP reviewer")
        for l in learned:
            store.add_event(rid, "learned", f"Learned for {run.get('vendor')}: '{l['label']}' is the "
                                            f"{l['field'].replace('_', ' ')}", l)
    merged = {**((run.get("facts") or {}).get("fields") or {}), **fixes}
    pipeline.reprocess(rid, dict(fields=merged, fields_source="typed by AP reviewer"),
                       "AP reviewer typed " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in fixes.items()))
    return J(dict(learned=learned))


@app.post("/api/gl/remember")
def gl_remember(vendor_id: str = Form(...), description: str = Form(...), account: str = Form(...)):
    if account not in erp.ACCOUNTS:
        raise HTTPException(400, "Unknown account")
    store.remember_gl(vendor_id, description, account, "AP reviewer")
    store.add_event(None, "learned", f"Learned GL code: '{description}' from {vendor_id} → {account} {erp.ACCOUNTS[account]}",
                    dict(vendor_id=vendor_id))
    return {"ok": True}


# ============================================================================== Jeen, the AP assistant
class JeenChat(BaseModel):
    messages: list[dict] = []
    context: dict = {}


@app.post("/api/jeen/chat")
def jeen_chat(body: JeenChat):
    """Jeen explains the agent's work and prepares actions. It has no tool that can decide or pay."""
    return J(jeen.chat(body.messages, body.context))


class JeenSend(BaseModel):
    run_id: str
    to_party: str = "vendor"
    to: str = ""
    subject: str
    body: str


@app.post("/api/jeen/send")
def jeen_send(body: JeenSend):
    """Send a message Jeen drafted. Reached only from the Send button, after a person clicked it."""
    try:
        return J(jeen.send_prepared(body.run_id, body.to_party, body.to, body.subject, body.body))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/eval")
def eval_report():
    p = ROOT / "data" / "eval_report.json"
    return J(json.loads(p.read_text()) if p.exists() else {})


@app.get("/api/learning")
def learned():
    gl = [dict(key=k, account=a, name=erp.ACCOUNTS.get(a)) for k, a in store.gl_memory().items()]
    return J(dict(layouts=store.all_layout_memory(), gl=gl, accounts=erp.ACCOUNTS))


# ================================================================== bring your own vendors, POs, receipts
@app.get("/api/master")
def master_summary():
    return J(master.summary())


@app.get("/api/master/template/{kind}.csv")
def master_template(kind: str):
    if kind not in master.TEMPLATES:
        raise HTTPException(404, "kind must be vendors, pos or receipts")
    return Response(master.template_csv(kind), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{kind}_template.csv"'})


@app.post("/api/master/import")
async def master_import(kind: str = Form(...), file: UploadFile = File(...)):
    """Upload vendors, purchase orders or goods receipts (CSV or .xlsx). Bad rows are listed, good rows kept."""
    try:
        rows = master.read_table(file.filename or "", await file.read())
        return J(master.import_rows(kind, rows))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/master/clear")
def master_clear():
    return J(master.clear_imports())


@app.post("/api/runs/{rid}/onboard")
def onboard(rid: str, name: str = Form(...), gstin: str = Form(...), bank_name: str = Form(""),
            account_number: str = Form(""), email: str = Form(""), msme: str = Form(""), terms_days: int = Form(30),
            verified: str = Form(""), who: str = Form("Vendor master team")):
    """A person onboards the vendor of an invoice stuck at 'vendor not onboarded', then the agent re-checks it.
    The bank account must be confirmed with the vendor on a known number, never just copied from the invoice."""
    run = store.get_run(rid)
    if not run or not run.get("result"):
        raise HTTPException(404, "Run not found")
    if verified != "1":
        raise HTTPException(400, "Confirm you verified the vendor's identity and bank account by phone or KYC documents")
    try:
        v = master.onboard_from_invoice(run, name, gstin, bank_name, account_number, email, msme == "1",
                                        terms_days, (who or "Vendor master team")[:60])
    except ValueError as e:
        raise HTTPException(400, str(e))
    pipeline.reprocess(rid, {}, f"vendor {v['name']} onboarded by {who}")
    return J(dict(vendor=v, run_id=rid))


# ================================================================== the invoice's discussion (@team, @Jeen)
@app.get("/api/runs/{rid}/discussion")
def get_discussion(rid: str):
    try:
        return J(discussion.timeline(rid))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/runs/{rid}/comments")
def post_comment(rid: str, text: str = Form(...), author: str = Form("AP reviewer")):
    """A person comments. @stores/@procurement/@vendor... get an email; @jeen answers in the thread."""
    try:
        new = discussion.post(rid, author, text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return J(dict(new=new, **discussion.timeline(rid)))


@app.get("/api/waiting")
def waiting():
    """Every invoice that needs a person's final call, with the agent's recommendation and its reasons."""
    return J(cockpit.waiting())


@app.get("/api/payments")
def payments():
    return J(erp.payables())


@app.post("/api/payments/batch")
def payment_batch(docs: str = Form(...)):
    try:
        b = erp.erp().create_batch([d for d in docs.split(",") if d])
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.add_event(None, "paid", f"Payment batch {b['id']} created: {len(b['docs'])} invoice(s), "
                                  f"{b['total']:,.2f} INR - bank file ready", dict(batch=b["id"]))
    return J(b)


@app.get("/api/payments/batch/{bid}.csv")
def payment_file(bid: str):
    if not erp.erp().batch(bid):
        raise HTTPException(404, "No such batch")
    return PlainTextResponse(erp.bank_file(bid), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{bid}.csv"'})


@app.post("/api/payments/batch/{bid}/paid")
def payment_paid(bid: str):
    if not erp.erp().batch(bid):
        raise HTTPException(404, "No such batch")
    erp.erp().mark_paid(bid)
    store.add_event(None, "paid", f"Bank confirmed batch {bid} - invoices marked paid in the ERP", dict(batch=bid))
    return J(erp.erp().batch(bid))


@app.get("/api/erp/docs")
def erp_docs():
    return J(erp.erp().docs())


@app.get("/api/events")
def events(limit: int = 60):
    return J(store.events(limit=limit))


@app.get("/api/integrations")
def integration_status():
    return J(integrations.status())


# ---- one-click approve / reject from Slack or email --------------------------------------------
def _page(title: str, body: str, tone: str = "") -> HTMLResponse:
    color = {"ok": "#1f7a4d", "bad": "#b42318", "warn": "#a86a12"}.get(tone, "#2b4c9b")
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)} - Invoice Desk</title>
<style>body{{margin:0;background:#eef1f4;color:#1b2430;font:15px/1.5 system-ui,sans-serif}}
main{{max-width:560px;margin:40px auto;padding:0 16px}}.card{{background:#fff;border:1px solid #d9dfe6;border-radius:10px;
padding:22px;border-top:5px solid {color}}}h1{{font-size:1.3rem;margin:0 0 6px}}p{{margin:8px 0}}.muted{{color:#5f6b78}}
.q{{background:#f6f8fa;border-radius:8px;padding:10px 12px;font-weight:500}}textarea{{width:100%;box-sizing:border-box;
border:1px solid #d9dfe6;border-radius:8px;padding:8px;font:inherit;min-height:60px}}label{{display:flex;gap:8px;
align-items:flex-start;margin:10px 0}}button{{font:inherit;font-weight:600;border:0;border-radius:8px;padding:10px 16px;
color:#fff;cursor:pointer;background:{color}}}a{{color:#2b4c9b}}</style></head>
<body><main><div class="card">{body}</div></main></body></html>""")


@app.get("/a/{token}", response_class=HTMLResponse)
def action_page(token: str):
    # GET only shows a confirmation page: email security scanners open links automatically, and must
    # never approve a payment by doing so. The action happens on the POST below.
    try:
        t = integrations.verify(token)
    except ValueError as e:
        return _page("Link not valid", f"<h1>Link not valid</h1><p>{escape(str(e))}</p>", "bad")
    run = store.get_run(t["r"])
    if not run:
        return _page("Not found", "<h1>Invoice not found</h1>", "bad")
    s = integrations.summary(run)
    if run.get("resolution"):
        r = run["resolution"]
        return _page("Already decided", f"<h1>Already {'approved' if r['action'] == 'APPROVE' else 'rejected'}</h1>"
                     f"<p>{escape(r['by'])} decided this on {escape(r['at'][:16].replace('T', ' '))} UTC.</p>"
                     f"<p><a href='{s['url']}'>Open in Invoice Desk</a></p>", "warn")
    approve = t["a"] == "approve"
    qs = "".join(f"<p class='q'>{escape(h['question'])}</p>" for h in s["handoff"])
    sensitive = approve and any(h["needs_verification"] for h in s["handoff"])
    check = ("<label><input type='checkbox' name='verified' value='1' required> I called the vendor on the phone "
             "number already in the vendor master (not one on the invoice) and confirmed this.</label>") if sensitive else ""
    return _page("Confirm", f"""<h1>{'Approve payment' if approve else 'Reject invoice'}?</h1>
<p><b>{escape(s['vendor'] or '')}</b>, invoice {escape(s['invoice_number'] or '-')} for <b>{escape(s['total_text'])}</b></p>
{"<p class='muted'>Every check passed. The agent recommends approval and is waiting for a person's OK.</p>" if s.get("approval") else "<p class='muted'>The agent's concerns:</p>" + qs}
<form method="post"><p class="muted">Deciding as <b>{escape(t['w'])}</b></p>
<textarea name="note" placeholder="Your answer or a note for the record (optional)"></textarea>{check}
<p><button type="submit">{'Approve payment' if approve else 'Reject invoice'}</button></p></form>
<p class="muted"><a href="{s['url']}">Open the full invoice in Invoice Desk</a></p>""", "ok" if approve else "bad")


@app.post("/a/{token}", response_class=HTMLResponse)
def action_submit(token: str, note: str = Form(""), verified: str = Form("")):
    try:
        t = integrations.verify(token)
        run = store.get_run(t["r"]) or {}
        s = integrations.summary(run) if run else {}
        run = pipeline.resolve(t["r"], t["a"], note, t["w"], "one-click link", verified == "1")
    except ValueError as e:
        return _page("Not done", f"<h1>Not done</h1><p>{escape(str(e))}</p>", "bad")
    ok = t["a"] == "approve"
    return _page("Done", f"<h1>{'Approved' if ok else 'Rejected'}</h1><p>Recorded as {escape(t['w'])}. "
                 f"{'The PO balance is updated and payment is scheduled.' if ok else 'The invoice will not be paid.'}</p>"
                 f"<p><a href='{integrations.public_url()}/#run={run['id']}'>Open in Invoice Desk</a></p>",
                 "ok" if ok else "bad")


@app.get("/api/dashboard")
def dashboard():
    rs = [r for r in store.list_runs(500) if r["state"] == "done"]
    final = lambda r: (r["resolution"] or {}).get("action") or r["decision"]
    counts = {k: sum(1 for r in rs if r["decision"] == k) for k in ("APPROVE", "REVIEW", "REJECT")}
    open_reviews = [r for r in rs if r["decision"] == "REVIEW" and not r["resolution"]]
    pend = {r["id"] for r in rs if not r["resolution"]
            and ((store.get_run(r["id"]) or {}).get("result") or {}).get("awaiting_human")}
    final = lambda r: (r["resolution"] or {}).get("action") or (None if r["id"] in pend else r["decision"])
    approved_value = sum(r["total"] or 0 for r in rs if final(r) == "APPROVE")
    waiting_ok = [r for r in rs if r["id"] in pend or (r["decision"] == "REVIEW" and not r["resolution"])]
    blocked_value = sum(r["total"] or 0 for r in rs if (r["resolution"] or {}).get("action", r["decision"]) == "REJECT")
    durations = [r["duration"] for r in rs if r["duration"]]
    led = store.ledger()
    balances = []
    for po in store.POS:
        used, _ = po_consumption(po, led)
        pending = sum(x["subtotal"] for x in led if x["po_number"] == po["po_number"] and x["status"] == "REVIEW")
        v = next(v for v in store.VENDORS if v["id"] == po["vendor_id"])
        balances.append(dict(po_number=po["po_number"], vendor=v["name"], amount=po["amount"], billed=used,
                             pending=pending, remaining=po["amount"] - used))
    # industry metrics: straight-through rate, exceptions by category, human touches, fraud stopped
    payloads = [store.get_run(r["id"]) or {} for r in rs]
    stp = sum(1 for p in payloads if (p.get("result") or {}).get("decision") == "APPROVE" and not p.get("revisions"))
    agent_resolved = sum(1 for p in payloads if (p.get("result") or {}).get("decision") == "APPROVE" and p.get("revisions"))
    by_cat: dict[str, dict] = {}
    for p in payloads:
        for h in (p.get("result") or {}).get("handoff") or []:
            c = by_cat.setdefault(h["category"], dict(category=h["category"], owner=h["owner"], count=0, fail=0))
            c["count"] += 1
            c["fail"] += h["severity"] == "fail"
    exceptions = sorted(by_cat.values(), key=lambda c: -c["count"])
    fraud = [p for p in payloads if (p.get("risk") or 0) >= 30]
    fraud_value = sum((p.get("total") or 0) for p in fraud)
    touches = sum(len((p.get("result") or {}).get("handoff") or []) for p in payloads) + \
        sum(1 for r in rs if r["resolution"])
    repaired = sum(1 for p in payloads if ((p.get("result") or {}).get("invoice") or {}).get("repaired"))
    metrics = dict(stp_rate=round(stp / len(rs) * 100) if rs else None, exceptions=exceptions,
                   agent_resolved=agent_resolved,
                   messages_sent=sum(1 for e in store.events(kind="agent", limit=5000) if (e["data"] or {}).get("party")),
                   fraud_flags=len(fraud), fraud_value=fraud_value, ocr_repairs=repaired,
                   touches_per_invoice=round(touches / len(rs), 2) if rs else None,
                   questions_open=sum(len((p.get("result") or {}).get("handoff") or []) for p, r in zip(payloads, rs)
                                      if r["decision"] == "REVIEW" and not r["resolution"]))
    return J(dict(total_runs=len(rs), counts=counts, open_reviews=len(open_reviews), metrics=metrics,
                  approved_value=approved_value, blocked_value=blocked_value,
                  human_approval=config.human_approval(), waiting_count=len(waiting_ok),
                  waiting_value=sum(r["total"] or 0 for r in waiting_ok),
                  avg_seconds=round(sum(durations) / len(durations), 2) if durations else None,
                  auto_rate=round(counts["APPROVE"] / len(rs) * 100) if rs else None,
                  balances=balances, runs=store.list_runs(200)))


@app.get("/api/reference")
def reference():
    return J(dict(vendors=store.VENDORS, pos=store.POS, buyer=store.BUYER, ledger=store.ledger(),
                  rules=dict(tolerance_pct=TOLERANCE_PCT, tolerance_abs=TOLERANCE_ABS)))


@app.post("/api/reset")
def reset():
    if pipeline.LIVE:
        raise HTTPException(409, "Wait for the running invoice to finish, then reset")
    store.init(reset=True)
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
