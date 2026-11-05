"""Step 5: post approved invoices to the accounting system, read them back, and pay them.

  GL coding    each line gets an expense/asset account from rules on HSN and description (plus what reviewers
               taught it - see learning.py). Low-confidence coding goes to suspense and is flagged.
  Journal      Dr expense / asset (pre-tax), Dr input GST, Cr vendor payable (net of TDS), Cr TDS payable.
               It must balance to the paisa or it isn't posted.
  Post + read  posted through an adapter, then READ BACK from the ERP and compared field by field
               (document, vendor, invoice number, amounts, balance). Only a confirmed read-back counts.
  Pay          approved, posted, unpaid invoices are batched by due date (MSME first), a bank upload file is
               produced, and the batch is marked paid when the bank confirms.

The adapter here is a local "mock ERP" (its own database, its own document numbers) so the demo runs anywhere.
A Zoho Books / Tally / ERPNext adapter implements the same three calls: find, post, read.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

from . import store
from .utils import fmt_inr

_lock = threading.Lock()

ACCOUNTS = {
    "1410": "Input CGST", "1411": "Input SGST", "1412": "Input IGST",
    "1520": "Plant & lab equipment (capital)",
    "2110": "Trade payables", "2220": "TDS payable",
    "5110": "Packaging materials", "5210": "Freight inward", "5310": "Safety consumables (PPE)",
    "5320": "Maintenance consumables", "5330": "Electrical maintenance", "5340": "Repairs & maintenance services",
    "5350": "Laboratory consumables", "6110": "Advertising & promotion", "6310": "Cloud & IT services",
    "5999": "Suspense - needs coding",
}
# (HSN prefix or keyword regex) -> account
RULES = [
    (r"^(4819|4821|3920)", None, "5110"), (r"^99(65|67)", None, "5210"), (r"^9983", None, "6310"),
    (r"^(4015|9004|6506)", None, "5310"), (r"^(2710|3403)", None, "5320"), (r"^(9405|8536)", None, "5330"),
    (r"^99(54|87)", None, "5340"), (r"^3822", None, "5350"), (r"^4911", None, "6110"), (r"^9027", None, "1520"),
    (None, r"box|carton|label|sleeve|wrap|packag", "5110"), (None, r"freight|transport|loading|ftl", "5210"),
    (None, r"hosting|cloud|software|saas", "6310"), (None, r"glove|goggle|helmet|ppe", "5310"),
    (None, r"oil|grease|lubric", "5320"), (None, r"led|mcb|cable|electric", "5330"),
    (None, r"install|labour|labor|repair|service charge", "5340"), (None, r"banner|print|signage|advert", "6110"),
    (None, r"meter|instrument|analy[sz]er", "1520"), (None, r"buffer|reagent|calibration", "5350"),
]
CAPEX_UNIT = 10000  # an instrument above this unit price is capitalised


def code_line(desc: str, hsn: str | None, unit_price: float | None, vendor_id: str | None, memory: dict) -> dict:
    """GL account for one line, with how it was decided and a confidence."""
    key = f"{vendor_id}|{desc.lower().strip()}"
    if key in memory:
        acc = memory[key]
        return dict(account=acc, name=ACCOUNTS.get(acc, acc), how="learned from a reviewer", confidence=0.97)
    h = str(hsn or "")
    for hp, kw, acc in RULES:
        if hp and h and re.match(hp, h):
            how, conf = f"HSN {h}", 0.95
        elif kw and re.search(kw, desc, re.I):
            how, conf = "description", 0.8
        else:
            continue
        if acc == "1520" and (unit_price or 0) < CAPEX_UNIT:
            acc, how = "5350", how + ", below capitalisation limit"
        return dict(account=acc, name=ACCOUNTS[acc], how=how, confidence=conf)
    return dict(account="5999", name=ACCOUNTS["5999"], how="no rule matched", confidence=0.3)


def build_journal(run: dict) -> dict:
    res = run["result"]
    inv = res["invoice"]
    po = next((s for s in run["stages"] if s["key"] == "po"), {}).get("data", {}).get("po") or {}
    memory = store.gl_memory()
    rows = po.get("lines") or []
    net_lines = inv.get("lines") or []  # the verified (and, on scans, repaired) pre-tax lines
    lines, coding = [], []
    for i, l in enumerate(net_lines):
        r = rows[i] if i < len(rows) else {}
        c = code_line(l["description"], r.get("hsn") or l.get("hsn"), l.get("unit_price"), inv.get("vendor_id"), memory)
        coding.append(dict(line=l["description"], **c))
        lines.append(dict(account=c["account"], name=c["name"], debit=round(l["amount"], 2), credit=0.0,
                          memo=l["description"]))
    # if lines don't reconcile to the subtotal (e.g. rounding on tax-inclusive), adjust on the largest line
    diff = round((inv.get("subtotal") or 0) - sum(x["debit"] for x in lines), 2)
    if lines and abs(diff) >= 0.01:
        max(lines, key=lambda x: x["debit"])["debit"] += diff
    for t in inv.get("taxes") or []:
        acc = {"CGST": "1410", "SGST": "1411", "IGST": "1412"}.get(t["type"], "1412")
        lines.append(dict(account=acc, name=ACCOUNTS[acc], debit=round(t["amount"] or 0, 2), credit=0.0,
                          memo=f"{t['type']} {t.get('rate') or ''}%".strip()))
    tds = inv.get("tds") or 0
    total = inv.get("total") or 0
    lines.append(dict(account="2110", name=f"Trade payables - {inv['vendor']}", debit=0.0, credit=round(total - tds, 2),
                      memo=f"Invoice {inv['invoice_number']}"))
    if tds:
        lines.append(dict(account="2220", name="TDS payable (s.393)", debit=0.0, credit=round(tds, 2), memo="TDS"))
    for x in lines:
        x["debit"], x["credit"] = round(x["debit"], 2), round(x["credit"], 2)
    dr, cr = round(sum(x["debit"] for x in lines), 2), round(sum(x["credit"] for x in lines), 2)
    return dict(lines=lines, coding=coding, debit=dr, credit=cr, balanced=abs(dr - cr) < 0.01)


# --------------------------------------------------------------------------------------------- adapter
class MockERP:
    """A stand-in accounting system with its own database and document numbers."""
    name = "Mock ERP (local)"

    def __init__(self):
        self.path = store.RUNTIME / "erp.db"
        self.ensure()

    def ensure(self):
        """Create the tables if missing (cheap; also runs after 'Reset demo data' cleared them)."""
        with _lock, self._c() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS docs(doc_no TEXT PRIMARY KEY, vendor_id TEXT, vendor TEXT,
                invoice_number TEXT, invoice_date TEXT, due_date TEXT, po_number TEXT, gross REAL, tds REAL,
                payable REAL, journal TEXT, attachment TEXT, run_id TEXT, posted_at TEXT, status TEXT,
                batch TEXT, paid_at TEXT, msme INTEGER)""")
            c.execute("""CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, created TEXT, docs TEXT, total REAL,
                status TEXT, paid_at TEXT)""")

    def _c(self):
        return store.connect(self.path)

    def find(self, vendor_id, invoice_number):
        with _lock, self._c() as c:
            r = c.execute("SELECT doc_no FROM docs WHERE vendor_id=? AND invoice_number=?",
                          (vendor_id, invoice_number)).fetchone()
        return r["doc_no"] if r else None

    def post(self, doc: dict) -> str:
        with _lock, self._c() as c:
            n = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0] + 1
            doc_no = f"AP/26-27/{n:05d}"
            c.execute("""INSERT INTO docs(doc_no, vendor_id, vendor, invoice_number, invoice_date, due_date, po_number,
                gross, tds, payable, journal, attachment, run_id, posted_at, status, msme)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (doc_no, doc["vendor_id"], doc["vendor"], doc["invoice_number"], doc["invoice_date"],
                       doc["due_date"], doc["po_number"], doc["gross"], doc["tds"], doc["payable"],
                       json.dumps(doc["journal"]), doc["attachment"], doc["run_id"],
                       datetime.now(timezone.utc).isoformat(timespec="seconds"), "open", int(doc.get("msme", False))))
        return doc_no

    def read(self, doc_no: str) -> dict | None:
        with _lock, self._c() as c:
            r = c.execute("SELECT * FROM docs WHERE doc_no=?", (doc_no,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["journal"] = json.loads(d["journal"])
        return d

    def docs(self, status: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM docs", []
        if status:
            q += " WHERE status=?"; a.append(status)
        with _lock, self._c() as c:
            rows = [dict(r) for r in c.execute(q + " ORDER BY due_date, doc_no", a).fetchall()]
        for r in rows:
            r["journal"] = json.loads(r["journal"])
        return rows

    def create_batch(self, doc_nos: list[str]) -> dict:
        docs = [self.read(d) for d in doc_nos]
        docs = [d for d in docs if d and d["status"] == "open"]
        if not docs:
            raise ValueError("Nothing open to pay in that selection")
        with _lock, self._c() as c:
            n = c.execute("SELECT COUNT(*) FROM batches").fetchone()[0] + 1
            bid = f"PB-{date.today().strftime('%Y%m%d')}-{n:02d}"
            total = round(sum(d["payable"] for d in docs), 2)
            c.execute("INSERT INTO batches VALUES(?,?,?,?,?,?)", (bid, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                                                json.dumps([d["doc_no"] for d in docs]), total, "sent to bank", None))
            for d in docs:
                c.execute("UPDATE docs SET status='in batch', batch=? WHERE doc_no=?", (bid, d["doc_no"]))
        return self.batch(bid)

    def batch(self, bid: str) -> dict | None:
        with _lock, self._c() as c:
            r = c.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
        if not r:
            return None
        b = dict(r)
        b["docs"] = [self.read(d) for d in json.loads(b["docs"])]
        return b

    def batches(self) -> list[dict]:
        with _lock, self._c() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM batches ORDER BY created DESC").fetchall()]
        return [self.batch(i) for i in ids]

    def mark_paid(self, bid: str):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _lock, self._c() as c:
            c.execute("UPDATE batches SET status='paid', paid_at=? WHERE id=?", (now, bid))
            c.execute("UPDATE docs SET status='paid', paid_at=? WHERE batch=?", (now, bid))

    def reset(self):
        with _lock, self._c() as c:
            c.execute("DELETE FROM docs")
            c.execute("DELETE FROM batches")


_ERP: MockERP | None = None


def erp() -> MockERP:
    global _ERP
    if _ERP is None:
        _ERP = MockERP()
    else:
        _ERP.ensure()
    return _ERP


# --------------------------------------------------------------------------------------------- posting
def post_run(run: dict) -> tuple[list[dict], dict]:
    """Post an approved run and verify by reading it back. Returns (findings, data)."""
    e = erp()
    res, inv = run["result"], run["result"]["invoice"]
    vendor = store.VENDORS_BY_ID.get(inv.get("vendor_id"))
    j = build_journal(run)
    findings = []
    data = dict(adapter=e.name, journal=j)
    if not j["balanced"]:
        findings.append(dict(status="fail", text=f"Journal doesn't balance (debit {fmt_inr(j['debit'])} vs credit "
                                                 f"{fmt_inr(j['credit'])}) - not posted"))
        return findings, data
    existing = e.find(inv.get("vendor_id"), inv.get("invoice_number"))
    if existing:
        data["doc_no"] = existing
        findings.append(dict(status="info", text=f"Already in the ERP as {existing} - not posted twice"))
        return findings, data
    suspense = [c for c in j["coding"] if c["account"] == "5999"]
    doc = dict(vendor_id=inv.get("vendor_id"), vendor=inv.get("vendor"), invoice_number=inv.get("invoice_number"),
               invoice_date=inv.get("invoice_date"), due_date=inv.get("due_date"), po_number=inv.get("po_number"),
               gross=inv.get("total"), tds=inv.get("tds") or 0, payable=res["payment"]["amount"], journal=j["lines"],
               attachment=f"{run['id']}.pdf", run_id=run["id"], msme=bool(vendor and vendor.get("msme")))
    doc_no = e.post(doc)
    back = e.read(doc_no)
    checks = [
        ("document exists", back is not None),
        ("vendor", back and back["vendor_id"] == doc["vendor_id"]),
        ("invoice number", back and back["invoice_number"] == doc["invoice_number"]),
        ("gross amount", back and abs(back["gross"] - doc["gross"]) < 0.01),
        ("payable", back and abs(back["payable"] - doc["payable"]) < 0.01),
        ("journal balances", back and abs(sum(x["debit"] for x in back["journal"]) - sum(x["credit"] for x in back["journal"])) < 0.01),
        ("attachment", back and back["attachment"] == doc["attachment"]),
    ]
    failed = [n for n, ok in checks if not ok]
    data.update(doc_no=doc_no, readback=[dict(check=n, ok=bool(ok)) for n, ok in checks])
    if failed:
        findings.append(dict(status="fail", text=f"Posted as {doc_no} but read-back failed on: {', '.join(failed)} - "
                                                 "finance controller alerted"))
    else:
        findings.append(dict(status="pass", text=f"Posted to {e.name} as {doc_no} and read back: vendor, invoice number, "
                                                 f"amounts, balance and attachment all match"))
    findings.append(dict(status="pass" if not suspense else "info",
                         text=f"Coded {len(j['coding'])} line(s): " + "; ".join(f"{c['line'][:28]} → {c['account']} {c['name']}"
                                                                         for c in j["coding"]) +
                         (" - suspense lines are reclassed by AP" if suspense else "")))
    store.add_event(run["id"], "posted", f"Posted {inv.get('invoice_number')} to the ERP as {doc_no}"
                    f" ({fmt_inr(doc['payable'])} payable{', TDS ' + fmt_inr(doc['tds']) if doc['tds'] else ''})",
                    dict(doc_no=doc_no))
    return findings, data


# --------------------------------------------------------------------------------------------- payments
def payables() -> dict:
    today = date.fromisoformat(os.getenv("DEMO_TODAY")) if os.getenv("DEMO_TODAY") else date.today()
    open_docs = erp().docs("open")
    for d in open_docs:
        due = date.fromisoformat(d["due_date"]) if d["due_date"] else today
        d["days_left"] = (due - today).days
        v = store.VENDORS_BY_ID.get(d["vendor_id"]) or {}
        d["bank"] = v.get("bank")
    open_docs.sort(key=lambda d: (not d["msme"], d["days_left"]))
    tds = {}
    for d in erp().docs():
        if d["tds"]:
            sec = next((x["name"] for x in d["journal"] if x["account"] == "2220"), "TDS payable")
            month = (d["posted_at"] or "")[:7]
            k = (sec, month)
            tds[k] = tds.get(k, 0) + d["tds"]
    tds_rows = []
    for (sec, month), amt in tds.items():
        y, m = int(month[:4]), int(month[5:7])
        due = date(y + (m == 12), m % 12 + 1, 7)
        tds_rows.append(dict(section=sec, month=month, amount=round(amt, 2), deposit_by=due.isoformat()))
    return dict(open=open_docs, batches=erp().batches(), tds=tds_rows,
                totals=dict(open=round(sum(d["payable"] for d in open_docs), 2),
                            due_7=round(sum(d["payable"] for d in open_docs if d["days_left"] <= 7), 2),
                            overdue=round(sum(d["payable"] for d in open_docs if d["days_left"] < 0), 2)))


def bank_file(bid: str) -> str:
    """A generic bank bulk-upload CSV (beneficiary, account on file, amount, reference)."""
    b = erp().batch(bid)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Batch", "Beneficiary", "Account on file", "Amount (INR)", "Reference", "Value date"])
    for d in b["docs"]:
        v = store.VENDORS_BY_ID.get(d["vendor_id"]) or {}
        w.writerow([bid, d["vendor"], v.get("bank", ""), f"{d['payable']:.2f}",
                    f"{d['invoice_number']} / {d['doc_no']}", date.today().isoformat()])
    return out.getvalue()
