"""Orchestrates the 8 stages and records everything that happened, so each run is fully explainable."""
from __future__ import annotations

import copy
import re
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

from . import (agent, checks, compliance, decision, erp, extract_llm, extract_rules, integrations, learning, matching,
               risk, store)
from . import config, files, reader, sense
from .config import MIN_FIELD_CONFIDENCE, REPAIR_MIN_CORROBORATION, STAGE_DELAY
from .reader import inspect, read_pdf
from .utils import fmt_inr, norm_invoice_no

STAGES = [
    ("intake", "Receive invoice", "File received, fingerprinted, and checked for a text layer"),
    ("read", "Read document", "Text layer if the PDF has one; otherwise deskew + OCR"),
    ("extract", "Extract fields", "Find invoice number, dates, PO, parties, line items, tax and totals"),
    ("validate", "Self-check numbers", "Does the invoice agree with itself? Repair OCR misreads if provable"),
    ("vendor", "Vendor check", "Known, approved vendor? Billed to us? Right tax type?"),
    ("duplicate", "Duplicate check", "Has this invoice been submitted before?"),
    ("po", "Match to PO", "Did we order this, at this price, with enough left on the PO?"),
    ("risk", "Fraud & risk", "Bank details, identity and amount checked for signs of payment fraud"),
    ("tax", "Tax & compliance", "E-invoice (IRN), TDS on services, MSME 45-day rule, input tax credit (GSTR-2B)"),
    ("decision", "Decision", "Approve, send to review, or reject - with reasons"),
    ("post", "Post to ERP", "Coded to GL accounts, posted to the accounting system, and read back to confirm"),
]
LIVE: dict[str, dict] = {}
KEY_FIELDS = ["vendor_gstin", "invoice_number", "invoice_date", "total"]
RANK = {"pass": 0, "info": 1, "warn": 2, "fail": 3}


def unreadable_run(filename: str, reason: str, channel: dict | None = None) -> dict:
    """A file that arrived but can't be read still gets a record, so it can't silently get lost."""
    run = new_run(filename, None, channel)
    LIVE.pop(run["id"], None)
    for s in run["stages"]:
        s["status"] = "skipped"
    run["stages"][0].update(status="fail", findings=[dict(status="fail", text=reason)])
    run.update(state="error", error=reason, duration=0)
    store.save_run(run)
    return run


def new_run(filename: str, sample: str | None, channel: dict | None = None) -> dict:
    rid = datetime.now().strftime("%y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    run = dict(id=rid, created=datetime.now(timezone.utc).isoformat(timespec="seconds"), filename=filename, sample=sample,
               channel=channel or dict(channel="sample" if sample else "upload"),
               state="running", stages=[dict(key=k, label=l, explain=e, status="pending", findings=[], data={})
                                        for k, l, e in STAGES], result=None)
    LIVE[rid] = run
    store.save_run(run)
    return run


class Stage:
    def __init__(self, run, key):
        self.run, self.s = run, next(s for s in run["stages"] if s["key"] == key)

    def __enter__(self):
        self.s.update(status="running", started=time.time())
        return self

    def add(self, status, text, **kw):
        self.s["findings"].append(dict(status=status, text=text, **kw))

    def __exit__(self, exc_type, exc, tb):
        worst = max((f["status"] for f in self.s["findings"]), key=RANK.get, default="pass")
        self.s.update(status="error" if exc else worst, ended=time.time())
        self.run["compute"] = round(self.run.get("compute", 0) + self.s["ended"] - self.s["started"], 3)
        if exc:
            self.s["findings"].append(dict(status="fail", text=f"{exc_type.__name__}: {exc}"))
        store.save_run(self.run)
        if STAGE_DELAY and not exc:
            time.sleep(STAGE_DELAY)  # demo pacing only - not counted in stage or run time
        return False


def _flat(f: dict) -> dict:
    """Field dicts -> plain values the checks work on."""
    v = {k: (f[k]["value"] if isinstance(f.get(k), dict) else f.get(k)) for k in f}
    v["lines"] = [dict(l) for l in f["lines"]]
    v["taxes"] = [dict(t) for t in f["taxes"]]
    return v


def _page_png(pdf: bytes) -> bytes:
    import io as _io
    from .reader import _render
    buf = _io.BytesIO()
    _render(pdf, 2.5).convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _consistent(inv: dict) -> bool:
    ids = checks.identities(inv)
    return bool(ids) and all(i["ok"] for i in ids)


def execute(run: dict, pdf: bytes):
    t0 = time.time()
    final = "done"
    try:
        _execute(run, pdf)
    except Exception as e:  # never leave a run hanging in the UI
        final = "error"
        run["error"] = files.friendly_error(e)  # what a person should do; the technical detail stays in the trace
        run["error_detail"] = f"{type(e).__name__}: {e}"
        run["trace"] = traceback.format_exc()
        for s in run["stages"]:
            if s["status"] in ("pending", "running"):
                s["status"] = "skipped"
    run["wall"] = round(time.time() - t0, 2)
    run["duration"] = round(run.get("compute", run["wall"]), 2)  # processing time, excluding demo pacing
    try:
        res = run.get("result") or {}
        store.add_event(run["id"], "decided", f"{res.get('decision', 'ERROR')}: {run.get('vendor') or run['filename']}"
                        f" - {res.get('headline') or run.get('error', '')}"[:300], dict(decision=res.get("decision")))
        agent.act(run)  # before the run is visible as finished, so nobody sees a "done" run without its messages
    except Exception:
        traceback.print_exc()
    run["state"] = final  # only now: anyone polling sees "done" together with the agent's messages
    store.save_run(run)
    LIVE.pop(run["id"], None)
    try:
        integrations.emit("invoice.processed", run)
    except Exception:
        traceback.print_exc()


def reprocess(run_id: str, facts: dict, reason: str) -> dict:
    """Re-run the whole decision in place with new facts (from a reply or a reviewer). Keeps the history."""
    run = store.get_run(run_id)
    pdf = store.file_path(run_id, "pdf").read_bytes()
    res = run.get("result") or {}
    run.setdefault("revisions", []).append(dict(
        at=datetime.now(timezone.utc).isoformat(timespec="seconds"), decision=res.get("decision"),
        headline=res.get("headline"), reason=reason))
    run["facts"] = {**(run.get("facts") or {}), **facts}
    store.delete_ledger(run_id)
    run.update(state="running", result=None, compute=0, stages=[
        dict(key=k, label=l, explain=e, status="pending", findings=[], data={}) for k, l, e in STAGES])
    LIVE[run_id] = run
    store.save_run(run)
    store.add_event(run_id, "agent", f"Re-checking: {reason}", dict(reason=reason))
    threading.Thread(target=execute, args=(run, pdf), daemon=True).start()
    return run


def _execute(run: dict, pdf: bytes):
    findings_by_stage: dict[str, list] = {}
    facts = run.get("facts") or {}

    # 1 ── intake ----------------------------------------------------------------------------------
    with Stage(run, "intake") as st:
        info = inspect(pdf)
        run["sha256"] = info["sha256"]
        store.file_path(run["id"], "png").write_bytes(info["preview_png"])
        st.s["data"] = dict(bytes=len(pdf), pages=info["pages"], sha256=info["sha256"][:16], text_chars=info["chars"])
        st.add("pass", f"{run['filename']}, {len(pdf)/1024:.0f} KB, {info['pages']} page(s)")
        prev = store.seen_hash(info["sha256"], run["id"])
        if prev:
            st.add("info", f"This exact file was processed before (run {prev['id']}, decision {prev['decision']})")
        st.add("pass" if info["has_text"] else "info",
               "Has a machine-readable text layer" if info["has_text"]
               else "No text layer - this is a scanned image, OCR needed")
        b = run.get("batch")
        if b:  # this invoice was split out of a PDF that held several
            pages = b["pages"]
            where = f"page {pages[0]}" if len(pages) == 1 else f"pages {pages[0]}-{pages[-1]}"
            if b.get("copies"):
                st.add("info", f"Page{'s' if len(b['copies']) > 1 else ''} {', '.join(map(str, b['copies']))}: copies of the same invoice in this PDF "
                               "(same number and total), not counted again")
            if b["count"] == 1:
                pass
            elif b["mixed"]:
                st.add("warn", f"Split from {b['source']}: {b['count']} invoices from {len(b['vendors'])} different "
                               f"vendors ({', '.join(b['vendors'])}) in one PDF. This is invoice {b['index']} ({where}). "
                               "One vendor doesn't normally send another vendor's invoice, so a person should look at it "
                               "even if every check passes", code="MIXED_PDF",
                       facts=dict(source=b["source"], count=b["count"], vendors=b["vendors"]))
            else:
                st.add("info", f"Split from {b['source']}: {b['count']} invoices from {b['vendor']} in one PDF. "
                               f"This is invoice {b['index']} of {b['count']} ({where}), checked on its own")
    findings_by_stage["intake"] = [x for x in st.s["findings"] if x.get("code") == "MIXED_PDF"]

    # 2 ── read ------------------------------------------------------------------------------------
    with Stage(run, "read") as st:
        doc = read_pdf(pdf, preview=False)
        run["source"] = doc.source
        st.s["data"] = dict(source=doc.source, words=len(doc.words), lines=len(doc.lines),
                            ocr_confidence=doc.ocr_confidence, skew=doc.skew_deg, text=doc.text[:4000])
        if doc.source == "ocr":
            st.add("info", f"Deskewed by {doc.skew_deg:+.1f}° and OCR'd with Tesseract"
                           f"{' (English + Hindi: the page is in Hindi)' if doc.ocr_lang == 'eng+hin' else ''}: "
                           f"{len(doc.words)} words, mean confidence {doc.ocr_confidence:.0f}%")
            if doc.rotated:
                st.add("info", f"The scan was {'upside down' if doc.rotated == 180 else 'sideways'}: turned it "
                               f"{doc.rotated}° to read it")
            if doc.ocr_reason:
                st.add("info", f"The PDF has a text layer, but {doc.ocr_reason}")
            if doc.ocr_confidence and doc.ocr_confidence < 70:
                st.add("warn", "Low OCR confidence - scan quality is poor", code="OCR_QUALITY")
        else:
            st.add("pass", f"Read {len(doc.words)} words directly from the PDF text layer (exact)")
        if doc.pages > 1:
            if doc.pages_read >= doc.pages:
                st.add("pass", f"Read all {doc.pages} pages")
            else:
                why = (f"only the first {doc.pages_read} pages are read" if doc.pages_read >= reader.MAX_PAGES
                       else "scanned pages after the first are not read in this version")
                st.add("warn", f"{doc.pages}-page document: only page{'s 1-' + str(doc.pages_read) if doc.pages_read > 1 else ' 1'} "
                               f"could be read ({why}). A person should check the lines and total on the rest",
                       code="PAGES_UNREAD", facts=dict(pages=doc.pages, read=doc.pages_read))
        if doc.hidden_text:
            h = doc.hidden_text
            aimed = re.search(r"\b(ai|agent|assistant|model|llm|gpt|claude|ignore|instruction|approve|pre-?approved|"
                              r"system)\b", h, re.I)
            bank = re.search(r"bank|a/?c|ifsc|account", h, re.I)
            st.add("warn", f"The PDF contains text nobody can see on the paper (white or microscopic): "
                           f"'{h[:140]}{'…' if len(h) > 140 else ''}'."
                           + (" It is written to instruct an automated reader. It was ignored - only visible text is "
                              "read - but an invoice that tries to steer the checks is a red flag" if aimed else
                              " It was ignored; only what a person can see is read")
                           + (". It also mentions bank details: pay only the account on file" if bank else ""),
                   code="HIDDEN_TEXT", facts=dict(text=h[:200]),
                   tried=["Compared every character's colour and size with what prints on the page"])
        # these hold the invoice: a page nobody read could carry the real total
        findings_by_stage["read"] = [x for x in st.s["findings"] if x.get("code") in ("PAGES_UNREAD", "HIDDEN_TEXT")]

    # 3 ── extract ---------------------------------------------------------------------------------
    with Stage(run, "extract") as st:
        f = extract_rules.extract(doc, store.BUYER["gstin"])
        reader_used = "rules"
        llm_f, agreement = None, None
        if extract_llm.provider():
            try:
                llm_f = extract_llm.extract(doc.text)
                agreement = []
                for k in ["invoice_number", "invoice_date", "po_number", "vendor_gstin", "subtotal", "total"]:
                    a, b = f[k]["value"], llm_f[k]["value"]
                    agreement.append(dict(field=k, rules=a, llm=b, agree=(a == b) or (
                        isinstance(a, float) and isinstance(b, float) and abs(a - b) < 1)))
                n_ok = sum(x["agree"] for x in agreement)
                st.add("pass" if n_ok == len(agreement) else "info",
                       f"Second reader ({extract_llm.provider()}) agrees on {n_ok}/{len(agreement)} key fields")
                # fill gaps the rules reader could not find
                for k in ["invoice_number", "invoice_date", "po_number", "vendor_gstin", "vendor_name", "due_date"]:
                    if f[k]["value"] is None and llm_f[k]["value"] is not None:
                        f[k] = llm_f[k]
                        st.add("info", f"{k.replace('_', ' ')} taken from the LLM reader (rules reader found none)")
                if not _consistent(_flat(f)) and _consistent(_flat(llm_f)):
                    f, reader_used = llm_f, "llm"
                    st.add("info", "Rules reading doesn't add up but the LLM reading does - using the LLM reading")
            except Exception as e:
                st.add("info", f"LLM reader unavailable ({type(e).__name__}); using rules reader only")
        # second opinion on scans: a vision model reads the page image when OCR is unsure or doesn't add up
        vision_f = None
        weak = [k for k in KEY_FIELDS if f[k]["value"] is None or f[k]["confidence"] < MIN_FIELD_CONFIDENCE]
        if doc.source == "ocr" and extract_llm.provider() and (weak or not _consistent(_flat(f))):
            try:
                vision_f = extract_llm.extract_image(_page_png(pdf))
                for k in weak:
                    a, b = f[k]["value"], vision_f[k]["value"]
                    if a is not None and b is not None and str(a) == str(b):
                        f[k]["confidence"] = 0.9
                        f[k]["method"] += " + confirmed by vision reader"
                        st.add("info", f"{k.replace('_', ' ').capitalize()} '{a}' was unclear in OCR; the vision reader "
                                       "read the same value, so it is accepted")
                    elif a is None and b is not None:
                        f[k] = dict(vision_f[k], confidence=0.55, box=None,
                                    method="vision reader only - a person confirms")
                        st.add("info", f"{k.replace('_', ' ').capitalize()} not found by OCR; vision reader suggests "
                                       f"'{b}' (a person confirms)")
                    elif a is not None and b is not None:
                        st.add("info", f"{k.replace('_', ' ').capitalize()}: OCR read '{a}', vision reader read '{b}' "
                                       "- they disagree, so a person checks")
            except Exception as e:
                st.add("info", f"Vision reader unavailable ({type(e).__name__})")
                vision_f = None

        # labels this vendor's layout taught us, then values a reviewer typed in
        vid = learning.vendor_from_gstin(f["vendor_gstin"]["value"])
        for note in learning.apply_learned(doc, f, vid):
            st.add("info", note[0].upper() + note[1:])
        for k, v in (facts.get("fields") or {}).items():
            if isinstance(f.get(k), dict):
                _, _, words = learning.locate(doc, k, v)
                f[k] = dict(value=v, raw=v, method=facts.get("fields_source", "typed by a reviewer"), confidence=0.99,
                            box=doc.box(words) if words else None)
                st.add("info", f"{k.replace('_', ' ').capitalize()} {v} {facts.get('fields_source', 'typed by a reviewer')}")
        f.setdefault("bank", dict(value=None, raw=None, method="not found", confidence=0.0, box=None))
        fields = [dict(field=k, value=f[k]["value"], method=f[k]["method"], confidence=f[k]["confidence"],
                       raw=f[k]["raw"], box=f[k].get("box")) for k in
                  ["vendor_name", "vendor_gstin", "buyer_gstin", "invoice_number", "invoice_date", "due_date",
                   "po_number", "subtotal", "total", "bank"]]
        st.s["data"] = dict(fields=fields, lines=f["lines"], taxes=f["taxes"], tax_inclusive=f["tax_inclusive"],
                            reader=reader_used, agreement=agreement)
        found = sum(1 for x in fields if x["value"] is not None and x["field"] != "bank")
        fields_n = len(fields) - 1
        st.add("pass", f"Found {found}/{fields_n} header fields, {len(f['lines'])} line item(s), "
                       f"{len(f['taxes'])} tax line(s)")
        for k in KEY_FIELDS:
            if f[k]["value"] is None:
                st.add("warn", f"Could not find {k.replace('_', ' ')} on the invoice", code="FIELD_MISSING",
                       facts=dict(field=k.replace("_", " ")), tried=["Searched for the label in every layout pattern"])
            elif f[k]["confidence"] < MIN_FIELD_CONFIDENCE:
                st.add("warn", f"Low confidence reading {k.replace('_', ' ')} ({f[k]['confidence']:.0%})",
                       code="FIELD_MISSING", facts=dict(field=k.replace("_", " ")))
        if not f["po_number"]["value"]:
            st.add("info", "No PO number written on the invoice")
        if f["tax_inclusive"]:
            st.add("info", "Prices are GST-inclusive - tax will be backed out before comparing to the PO")
        if not f["lines"]:
            st.add("warn", "No line items could be read", code="NO_ARITH")
        kind = sense.doc_kind([l.text for l in doc.lines], f["total"]["raw"])
        if kind["kind"] == "not_invoice" and doc.source == "ocr" and (doc.ocr_confidence or 0) < 75:
            kind = dict(kind, kind="invoice")  # an unreadable scan isn't evidence that it isn't an invoice
        run["doc_kind"] = kind["kind"]
        if kind["kind"] != "invoice":
            amt = f["total"]["value"]
            what = {"credit_note": "a CREDIT NOTE", "debit_note": "a DEBIT NOTE", "proforma": "a PROFORMA invoice",
                    "quotation": "a QUOTATION", "statement": "a STATEMENT OF ACCOUNT", "challan": "a DELIVERY CHALLAN",
                    "purchase_order": "a PURCHASE ORDER", "receipt": "a RECEIPT", "reminder": "a payment REMINDER",
                    "not_invoice": "not an invoice"}[kind["kind"]]
            tail = {"credit_note": f"{f['vendor_name']['value'] or 'the vendor'} owes us "
                                   f"{fmt_inr(abs(amt)) if amt else 'this amount'}"
                                   + (f" against {kind['against']}" if kind["against"] else "")
                                   + ". Nothing to pay: apply it to their next payment",
                    "debit_note": "an extra charge on an earlier invoice"
                                  + (f" ({kind['against']})" if kind["against"] else "")
                                  + ". It needs the original invoice and the reason before anything is paid",
                    "proforma": "it asks for money before a tax invoice exists. Paying it and then the tax invoice "
                                "pays twice, and a proforma carries no input tax credit",
                    "quotation": "an offer; nothing is owed",
                    "statement": "a list of invoices, each checked and paid on its own. Paying the statement would "
                                 "pay them twice",
                    "not_invoice": "there is no invoice heading and no amounts on it",
                    }.get(kind["kind"], kind["meaning"])
            how = (f"heading '{kind['title']}'" if kind["title"] not in
                   ("not a tax invoice", "negative total", "no invoice heading or amounts") else kind["meaning"])
            st.add("warn", f"This is {what}, not a bill to pay" + ("" if how.startswith("heading") else f" ({how})")
                   + f": {tail}", code="NOT_INVOICE",
                   facts=dict(kind=kind["kind"], against=kind["against"], amount=fmt_inr(abs(amt)) if amt else "-"))
        findings_by_stage["extract"] = st.s["findings"]

    if facts.get("po_number"):
        with Stage(run, "extract") as st:  # re-open the stage to record the fact we were given
            st.add("info", f"PO {facts['po_number']} {facts.get('po_source', 'supplied')} (not printed on the invoice)")
            f["po_number"] = dict(value=facts["po_number"], raw=facts["po_number"],
                                  method=facts.get("po_source", "supplied"), confidence=0.99, box=None)
            for fl in st.s["data"].get("fields", []):
                if fl["field"] == "po_number":
                    fl.update(value=facts["po_number"], method=facts.get("po_source", "supplied"), confidence=0.99)
            st.s["findings"] = [x for x in st.s["findings"] if "No PO number written" not in x["text"]]
            findings_by_stage["extract"] = st.s["findings"]
    inv = _flat(f)
    run["vendor"], run["invoice_number"], run["total"] = inv.get("vendor_name"), inv.get("invoice_number"), inv.get("total")

    # 4 ── validate (self-consistency) -------------------------------------------------------------
    with Stage(run, "validate") as st:
        ids = checks.identities(inv)
        before = copy.deepcopy(ids)
        repair = None
        if not ids:
            st.add("warn", "Not enough numbers to cross-check the invoice", code="NO_ARITH")
        elif all(i["ok"] for i in ids):
            st.add("pass", f"All {len(ids)} arithmetic identities hold - the numbers read are self-consistent")
        else:
            bad = [i for i in ids if not i["ok"]]
            st.add("info", f"{len(bad)} of {len(ids)} identities fail: " + "; ".join(
                f"{b['label']} (expected {fmt_inr(b['expected'])}, read {fmt_inr(b['found'])})" for b in bad))
            if doc.source == "ocr" or reader_used == "llm":
                fixed, info = checks.try_repair(inv)
                if fixed:
                    inv, repair = fixed, info
                    n = len(info["corroborated_by"])
                    msg = (f"OCR repair: {info['cell']} read as {fmt_inr(info['old'])}, corrected to "
                           f"{fmt_inr(info['new'])}. It is the only single-character OCR correction that makes all "
                           f"identities hold, confirmed by {n} independent checks.")
                    st.add("info" if n >= REPAIR_MIN_CORROBORATION else "warn",
                           msg + ("" if n >= REPAIR_MIN_CORROBORATION else
                                  f" Fewer than {REPAIR_MIN_CORROBORATION} confirmations, so a human should verify."),
                           code="REPAIR_WEAK", facts=dict(cell=info["cell"], new=fmt_inr(info["new"])),
                           tried=["Tried every single-character OCR confusion on the suspect numbers"])
                    path = info["path"]
                    src = f[path[0]][path[1]] if path[0] in ("lines", "taxes") else f.get(path[0])
                    info["box"] = (src or {}).get("box")
                    ids = checks.identities(inv)
                elif vision_f and _consistent(_flat(vision_f)):
                    vf = _flat(vision_f)
                    for k in ("lines", "taxes", "subtotal", "total", "tax_inclusive"):
                        inv[k] = vf[k]
                    ids = checks.identities(inv)
                    st.add("info", f"OCR numbers don't add up and can't be repaired, but the vision reader's numbers do: "
                                   f"all {len(ids)} identities hold, so its reading of the amounts is used")
                    reader_used = "vision"
                else:
                    st.add("warn", f"OCR reading doesn't reconcile and can't be repaired safely: {info}. "
                                   "Needs a human to read the scan.", code="OCR_UNREPAIRABLE",
                           tried=["Tried every single-character OCR confusion on the suspect numbers"])
            else:
                st.add("fail", "The vendor's own arithmetic is wrong on a digital invoice - return to vendor "
                               "for a corrected invoice", code="VENDOR_ARITH",
                       tried=["Recomputed every line, subtotal, tax and total"])
        for r in checks.rate_findings(inv):
            st.add(r["status"], r["text"], code="GST_RATE")
        tot = inv.get("total")
        words = sense.amount_in_words(doc.text)
        if words and tot is not None:
            if abs(abs(words["value"]) - abs(tot)) > 1:
                st.add("warn", f"Amount in words ('{words['raw'][:90]}' = {fmt_inr(words['value'])}) doesn't match the "
                               f"total in figures ({fmt_inr(tot)}). Banks treat the words as the real amount; a "
                               "mismatch is either a typo or an altered figure", code="WORDS_MISMATCH",
                       facts=dict(words=fmt_inr(words["value"]), figures=fmt_inr(tot)),
                       tried=["Read the amount in words and converted it to a number"])
            else:
                st.add("pass", f"Amount in words agrees with the total ({fmt_inr(tot)})")
        adj = sense.adjustments([l.text for l in doc.lines], f["total"]["raw"])
        inv["advance"] = adj["advance"] or 0
        if adj["advance"]:
            bal = round((tot or 0) - adj["advance"], 2)
            shown = f" (the invoice shows {fmt_inr(adj['payable'])})" if adj["payable"] is not None else ""
            st.add("warn", f"The invoice deducts an advance of {fmt_inr(adj['advance'])} after its total "
                           f"('{adj['advance_raw'][:70]}'). Only the balance of {fmt_inr(bal)}{shown} is payable, and "
                           "the advance must be in our books first. Paying the full total would pay the advance twice",
                   code="ADVANCE_ADJUSTED", facts=dict(advance=fmt_inr(adj["advance"]), balance=fmt_inr(bal),
                                                       vendor=inv.get("vendor_name")))
            if adj["payable"] is not None and abs(adj["payable"] - bal) > 1:
                st.add("warn", f"Balance printed ({fmt_inr(adj['payable'])}) isn't total minus advance ({fmt_inr(bal)})",
                       code="VENDOR_ARITH")
        if adj["previous"]:
            st.add("info", f"The invoice also shows an older balance of {fmt_inr(adj['previous'])} "
                           f"('{adj['previous_raw'][:60]}')"
                           + (f" and a 'total due' of {fmt_inr(adj['payable'])}" if adj["payable"] else "")
                           + f". Only this invoice's {fmt_inr(tot)} is approved here; older invoices are paid on their "
                             "own, so nothing is paid twice", code="PREVIOUS_BALANCE")
        rcm = sense.reverse_charge(doc.text)
        if rcm:
            base = checks.effective_subtotal(inv) or 0
            r = rcm["rate"] or 5
            gst = round(base * r / 100, 2)
            charged = sum(t["amount"] or 0 for t in inv["taxes"])
            run["rcm"] = dict(rate=r, amount=gst)
            if charged > 1:
                st.add("warn", f"Marked 'reverse charge' but the vendor also charged GST {fmt_inr(charged)}: under "
                               "reverse charge we pay the GST, so the vendor must not", code="TAX_TYPE")
            else:
                st.add("info", f"Reverse charge applies ('{rcm['raw'][:50]}'): no GST on the invoice is correct. We "
                               f"pay the GST ourselves - about {fmt_inr(gst)} at {r:g}% on {fmt_inr(base)} - in our "
                               "GST return, and can usually claim it back as input tax credit", code="RCM")
        if tot is not None and abs(tot) < 0.005 and run.get("doc_kind") == "invoice":
            st.add("warn", "This invoice is for ₹0.00 (free replacement or sample): there is nothing to pay. It is "
                           "kept for the records", code="ZERO_VALUE")
        st.s["data"] = dict(identities=ids, before=before if repair else None, repair=repair)
        findings_by_stage["validate"] = st.s["findings"]

    # normalise: everything compared with the PO is pre-tax
    subtotal = checks.effective_subtotal(inv)
    net_lines = []
    for l in inv["lines"]:
        l2 = dict(l)
        if inv.get("tax_inclusive") and inv["taxes"] and inv["taxes"][0].get("rate"):
            k = 1 + inv["taxes"][0]["rate"] / 100
            l2["unit_price"] = round(l["unit_price"] / k, 2)
            l2["amount"] = round(l["amount"] / k, 2)
        net_lines.append(l2)
    tax_total = round(sum(t["amount"] or 0 for t in inv["taxes"]), 2)

    # 5 ── vendor ----------------------------------------------------------------------------------
    with Stage(run, "vendor") as st:
        vendor, how, fs = matching.match_vendor(inv, store.VENDORS, store.BUYER, doc.source)
        for x in fs:
            st.add(**x)
        st.s["data"] = dict(vendor=vendor, method=how)
        findings_by_stage["vendor"] = st.s["findings"]

    # 6 ── duplicates ------------------------------------------------------------------------------
    ledger = store.ledger()
    with Stage(run, "duplicate") as st:
        fs, dup = matching.check_duplicate(inv, vendor, ledger)
        for x in fs:
            st.add(**{k: v for k, v in x.items() if k != "ref"})
        st.s["data"] = dict(duplicate_of=dup, normalized=None if not inv.get("invoice_number") else
                            norm_invoice_no(inv["invoice_number"]))
        findings_by_stage["duplicate"] = st.s["findings"]

    # 7 ── PO match --------------------------------------------------------------------------------
    with Stage(run, "po") as st:
        fs, po, po_view, cands = matching.match_po(inv, vendor, store.POS, ledger, net_lines, subtotal,
                                                   store.receipts())
        if po_view and facts.get("po_number"):
            po_view["confirmed_by"] = facts.get("po_source")
        for x in fs:
            st.add(**{k: v for k, v in x.items() if k != "candidates"})
        dfs, due = matching.check_dates(inv, po, vendor)
        for x in dfs:
            st.add(**x)
        st.s["data"] = dict(po=po_view, candidates=[dict(po=c["po"]["po_number"], score=c["score"],
                                                         remaining=c["remaining"]) for c in cands[:3]],
                            subtotal=subtotal, net_lines=net_lines if inv.get("tax_inclusive") else None)
        findings_by_stage["po"] = st.s["findings"]

    # 8 ── fraud & risk ---------------------------------------------------------------------------
    with Stage(run, "risk") as st:
        fs, rdata = risk.assess(dict(inv, due_date=inv.get("due_date")), doc.text, vendor, how, ledger,
                                po_view["how"] if po_view else None, doc.source, store.VENDORS)
        for x in fs:
            st.add(**x)
        rdata["bank_box"] = f["bank"].get("box")
        st.s["data"] = rdata
        run["risk"] = rdata["score"]
        findings_by_stage["risk"] = st.s["findings"]

    # 9 ── tax & compliance ------------------------------------------------------------------------
    with Stage(run, "tax") as st:
        qr = sense.read_qr(pdf)
        tfacts = facts
        if qr:
            if qr["irn"] and not compliance.IRN_RE.search(doc.text or "") and not facts.get("irn"):
                tfacts = dict(facts, irn=qr["irn"], irn_source="read from the e-invoice QR code - the printed IRN "
                                                               "couldn't be read")
            trusted = doc.source == "text-layer" or (ids and all(i["ok"] for i in ids))
            odd = []
            if qr["total"] is not None and inv.get("total") is not None and trusted and abs(qr["total"] - inv["total"]) > 1:
                odd.append(f"invoice value {fmt_inr(qr['total'])} in the QR vs {fmt_inr(inv['total'])} on the page")
            if qr["number"] and inv.get("invoice_number") and norm_invoice_no(qr["number"]) != norm_invoice_no(
                    inv["invoice_number"]) and trusted:
                odd.append(f"document number {qr['number']} in the QR vs {inv['invoice_number']} on the page")
            if qr["seller"] and inv.get("vendor_gstin") and qr["seller"] != inv["vendor_gstin"]:
                odd.append(f"seller GSTIN {qr['seller']} in the QR vs {inv['vendor_gstin']} on the page")
            if odd:
                st.add("warn", "The e-invoice QR code doesn't match the page: " + "; ".join(odd) + ". The QR is "
                               "signed when the e-invoice is issued, so the page was changed afterwards or the QR "
                               "was copied from another invoice", code="IRN_MISMATCH",
                       tried=["Decoded the e-invoice QR and compared it with the printed figures"])
            elif qr["total"] is not None:
                st.add("pass", f"E-invoice QR decoded: {qr['number'] or 'document'} for {fmt_inr(qr['total'])} - "
                               "matches the page")
        fs, tdata, due = compliance.assess(inv, doc.text, vendor, po_view, ledger, due,
                                           compliance.load_gstr2b(store.DATA / "gstr2b.json"), tfacts)
        for x in fs:
            st.add(**x)
        cur = compliance.foreign_currency(doc.text)
        if cur:
            st.add("warn", f"Amounts are in {cur}, not rupees. This desk pays INR invoices only; a foreign-currency "
                           "payment needs an exchange rate, a forex remittance and different tax treatment",
                   code="FOREIGN_CURRENCY", facts=dict(currency=cur))
        st.s["data"] = tdata
        findings_by_stage["tax"] = st.s["findings"]

    # 10 ── decision --------------------------------------------------------------------------------
    with Stage(run, "decision") as st:
        key_conf = [f[k]["confidence"] for k in KEY_FIELDS if isinstance(f.get(k), dict) and f[k]["value"] is not None]
        ctx = dict(vendor=vendor, vendor_name=inv.get("vendor_name"), total=inv.get("total"), due_date=due,
                   invoice_number=inv.get("invoice_number"), invoice_date=inv.get("invoice_date"),
                   po_number=po["po_number"] if po else None,
                   confidence=min(key_conf) if key_conf else 0.0, tds=tdata.get("tds_total"),
                   advance=inv.get("advance") or 0)
        res = decision.decide(findings_by_stage, ctx)
        impostor = bool(vendor and how == "name match" and inv.get("vendor_gstin")
                        and inv["vendor_gstin"] != vendor["gstin"])
        shown = (f"{inv.get('vendor_name')} (imitating {vendor['name']})" if impostor
                 else vendor["name"] if vendor else inv.get("vendor_name"))
        res["invoice"] = dict(vendor=shown,
                              vendor_id=vendor["id"] if vendor else None, invoice_number=inv.get("invoice_number"),
                              invoice_date=inv.get("invoice_date"), due_date=due,
                              po_number=po["po_number"] if po else None, po_how=po_view["how"] if po_view else None,
                              subtotal=subtotal, tax=tax_total, total=inv.get("total"), source=doc.source,
                              tds=tdata.get("tds_total"), net_payable=tdata.get("net_payable"),
                              msme_deadline=tdata.get("msme_deadline"), itc=tdata.get("itc"), irn=tdata.get("irn"),
                              lines=[dict(description=l["description"], qty=l["qty"], unit_price=l["unit_price"],
                                          amount=l["amount"], hsn=l.get("hsn")) for l in net_lines],
                              taxes=[dict(type=t["type"], rate=t.get("rate"), amount=t["amount"]) for t in inv["taxes"]],
                              repaired=bool(repair), advance=inv.get("advance") or 0, kind=run.get("doc_kind"),
                              rcm=run.get("rcm"),
                              bank=inv.get("bank"),  # as printed on the invoice; payment always uses the master's
                              vendor_name=inv.get("vendor_name"), vendor_gstin=inv.get("vendor_gstin"))
        run["result"] = res
        run["vendor"] = shown
        run["total"] = inv.get("total")
        st.add({"APPROVE": "pass", "REVIEW": "warn", "REJECT": "fail"}[res["decision"]], res["headline"])
        # ledger: approved invoices consume the PO; reviews are recorded (so they count for duplicates) but don't
        if vendor and res["decision"] in ("APPROVE", "REVIEW") and run.get("doc_kind") == "invoice":
            store.add_ledger(dict(vendor_id=vendor["id"], invoice_number=inv.get("invoice_number") or "",
                                  invoice_date=inv.get("invoice_date"), po_number=po["po_number"] if po else None,
                                  subtotal=subtotal or 0, tax=tax_total, total=inv.get("total") or 0,
                                  status=res["decision"], run_id=run["id"],
                                  lines=[dict(description=l["description"], qty=l["qty"], amount=l["amount"])
                                         for l in net_lines]))
            st.add("info", "Recorded in invoice ledger" + (" - PO balance updated" if res["decision"] == "APPROVE"
                                                           else " as pending (does not use PO balance yet)"))

    # 11 ── post to ERP (only what is approved) -----------------------------------------------------
    with Stage(run, "post") as st:
        if run["result"]["decision"] == "APPROVE" and run["result"].get("awaiting_human"):
            st.add("info", f"Agent recommends approval. Waiting for {config.APPROVER}'s OK: "
                           "nothing is posted or paid until a person approves")
        elif run["result"]["decision"] == "APPROVE":
            fs, pdata = erp.post_run(run)
            for x in fs:
                st.add(**x)
            st.s["data"] = pdata
            run["result"]["erp_doc"] = pdata.get("doc_no")
        else:
            st.add("info", "Not approved - nothing posted to the ERP")


def resolve(run_id: str, action: str, note: str, who: str = "AP reviewer", via: str = "dashboard",
            verified: bool = False) -> dict:
    """A person makes the final call. With human approval on, that's every invoice: the agent's APPROVE,
    REVIEW and REJECT are all recommendations. Overriding a REJECT (approving anyway) needs a written reason."""
    run = store.get_run(run_id)
    if run and run.get("state") == "error" and not run.get("resolution"):  # a file nobody could read
        if action != "reject":
            raise ValueError("This file couldn't be read, so it can't be approved. Dismiss it and ask the vendor for a readable copy")
        run["resolution"] = dict(action="REJECT", note=note or "Unreadable file dismissed", by=who, via=via,
                                 at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        store.save_run(run)
        store.add_event(run_id, "resolved", f"Unreadable file dismissed by {who}" + (f": {note}" if note else ""),
                        dict(action="REJECT", by=who, via=via))
        return run
    if not run or not run.get("result"):
        raise ValueError("Only invoices waiting for a person can be approved or rejected")
    res = run["result"]
    if run.get("state") == "running":
        raise ValueError("Still processing - wait for the agent to finish")
    if run.get("resolution"):
        r = run["resolution"]
        raise ValueError(f"Already {'approved' if r['action'] == 'APPROVE' else 'rejected'} by {r['by']}")
    decision = res.get("decision")
    if decision != "REVIEW" and not (res.get("awaiting_human") and decision in ("APPROVE", "REJECT")):
        raise ValueError("Only invoices waiting for a person can be approved or rejected")
    status = "APPROVE" if action == "approve" else "REJECT"
    if status == "APPROVE" and (res.get("invoice") or {}).get("kind") not in (None, "invoice"):
        raise ValueError("This document isn't a bill (" + str(res["invoice"]["kind"]).replace("_", " ") + "), so it "
                         "can't be posted or paid. Dismiss it, or ask the vendor for the tax invoice")
    if status == "APPROVE" and not verified and any(h["code"] in integrations.SENSITIVE for h in res.get("handoff") or []):
        raise ValueError("Approving a bank-detail change needs a phone check with the vendor first, on the number "
                         "already in the vendor master")
    if status == "APPROVE" and decision == "REJECT" and len((note or "").strip()) < 10:
        raise ValueError("The agent recommends rejecting this invoice. To approve anyway, write why (at least 10 characters)")
    if status == "APPROVE" and decision == "REJECT":  # was never in the ledger: add it so duplicates and PO balance see it
        _ledger_from_run(run, "APPROVE")
    store.set_ledger_status(run_id, status)
    res["awaiting_human"] = False
    run["resolution"] = dict(action=status, note=note, by=who, via=via,
                             at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    store.save_run(run)
    if status == "APPROVE":
        _post_after_human_approval(run)
    store.add_event(run_id, "resolved", f"{'Approved' if status == 'APPROVE' else 'Rejected'} by {who} via {via}"
                    + (f': "{note}"' if note else ""), dict(action=status, via=via))
    integrations.emit("invoice.resolved", run)
    return run


def _ledger_from_run(run: dict, status: str):
    inv = run["result"].get("invoice") or {}
    if not inv.get("vendor_id"):
        return
    store.add_ledger(dict(vendor_id=inv["vendor_id"], invoice_number=inv.get("invoice_number") or "",
                          invoice_date=inv.get("invoice_date"), po_number=inv.get("po_number"),
                          subtotal=inv.get("subtotal") or 0, tax=inv.get("tax") or 0, total=inv.get("total") or 0,
                          status=status, run_id=run["id"],
                          lines=[dict(description=l["description"], qty=l["qty"], amount=l["amount"])
                                 for l in inv.get("lines") or []]))


def _post_after_human_approval(run: dict):
    """A person approved an invoice that was on hold: post it now, exactly like an automatic approval."""
    res = run["result"]
    inv = res["invoice"]
    tds = inv.get("tds") or 0
    vendor = store.VENDORS_BY_ID.get(inv.get("vendor_id")) or {}
    adv = inv.get("advance") or 0
    res["payment"] = dict(amount=round((inv.get("total") or 0) - tds - adv, 2), gross=inv.get("total"), tds=tds,
                          advance=adv,
                          due_date=inv.get("due_date"), bank=vendor.get("bank"))  # always the account on file
    res["awaiting_human"] = False
    st = next((s for s in run["stages"] if s["key"] == "post"), None)
    if st is None:  # runs saved before the ERP stage existed
        st = dict(key="post", label="Post to ERP", explain="", status="pending", findings=[], data={})
        run["stages"].append(st)
    fs, pdata = erp.post_run(run)
    st["findings"] = [dict(status="info", text=f"Approved by {run['resolution']['by']} - posting now")] + fs
    st["data"] = pdata
    st["status"] = max((f["status"] for f in st["findings"]), key=RANK.get)
    res["erp_doc"] = pdata.get("doc_no")
    store.save_run(run)
