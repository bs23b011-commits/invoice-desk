"""Persistence: master data (JSON, read-only) + SQLite for run history and the invoice ledger."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.getenv("INVOICE_DESK_DATA", ROOT / "data"))
RUNTIME = Path(os.getenv("RUNTIME_DIR", ROOT / "runtime"))
RUNTIME.mkdir(parents=True, exist_ok=True)
(RUNTIME / "files").mkdir(exist_ok=True)
DB_PATH = RUNTIME / "invoice_agent.db"
_lock = threading.Lock()


def load_json(name):
    return json.loads((DATA / name).read_text())


VENDORS = load_json("vendors.json")
VENDORS_BY_ID = {v["id"]: v for v in VENDORS}
POS = load_json("purchase_orders.json")
BUYER = load_json("buyer.json")


@contextmanager
def connect(path):
    """Commit and CLOSE. sqlite3's own `with` only commits, which leaves the file open; on Windows an
    open file cannot be deleted, so 'Reset demo data' used to crash after the first invoice."""
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def _conn():
    return connect(DB_PATH)


def init(reset: bool = False):
    with _lock, _conn() as c:
        if reset:
            c.execute("DROP TABLE IF EXISTS runs")
            c.execute("DROP TABLE IF EXISTS ledger")
            for f in (RUNTIME / "files").glob("*"):
                try:
                    f.unlink()
                except OSError:  # Windows: a file still being shown in the browser stays locked
                    pass
        c.execute("""CREATE TABLE IF NOT EXISTS runs(
            id TEXT PRIMARY KEY, created TEXT, filename TEXT, sample TEXT, state TEXT, decision TEXT,
            vendor TEXT, invoice_number TEXT, total REAL, headline TEXT, duration REAL, payload TEXT,
            resolution TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ledger(
            id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id TEXT, invoice_number TEXT, invoice_date TEXT,
            po_number TEXT, subtotal REAL, tax REAL, total REAL, status TEXT, lines TEXT, run_id TEXT,
            source TEXT, created TEXT)""")
        if reset:
            c.execute("DROP TABLE IF EXISTS events")
            c.execute("DROP TABLE IF EXISTS comments")
            c.execute("DROP TABLE IF EXISTS grn")
            with connect(RUNTIME / "erp.db") as e:  # clear the ERP's tables instead of deleting its file
                e.execute("DROP TABLE IF EXISTS docs")
                e.execute("DROP TABLE IF EXISTS batches")
            c.execute("DROP TABLE IF EXISTS gl_memory")
            c.execute("DROP TABLE IF EXISTS layout_memory")
        c.execute("CREATE TABLE IF NOT EXISTS gl_memory(key TEXT PRIMARY KEY, account TEXT, by TEXT, at TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS layout_memory(vendor_id TEXT, field TEXT, label TEXT, position TEXT,
            by TEXT, at TEXT, PRIMARY KEY(vendor_id, field))""")
        c.execute("""CREATE TABLE IF NOT EXISTS grn(
            id INTEGER PRIMARY KEY AUTOINCREMENT, grn TEXT, po_number TEXT, description TEXT, qty REAL, date TEXT,
            source TEXT, run_id TEXT)""")
        if c.execute("SELECT COUNT(*) FROM grn").fetchone()[0] == 0 and (DATA / "goods_receipts.json").exists():
            for g in load_json("goods_receipts.json"):
                c.execute("INSERT INTO grn(grn, po_number, description, qty, date, source) VALUES(?,?,?,?,?,?)",
                          (g["grn"], g["po_number"], g["description"], g["qty"], g["date"], "warehouse system"))
        seed_imports = c.execute("SELECT COUNT(*) FROM grn WHERE source='imported'").fetchone()[0] == 0
        c.execute("""CREATE TABLE IF NOT EXISTS comments(
            id TEXT PRIMARY KEY, run_id TEXT, at TEXT, author TEXT, kind TEXT, text TEXT, payload TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, run_id TEXT, kind TEXT, text TEXT, data TEXT)""")
        n = c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        if n == 0:
            for h in load_json("invoice_history.json"):
                c.execute("""INSERT INTO ledger(vendor_id, invoice_number, invoice_date, po_number, subtotal, tax,
                             total, status, lines, run_id, source, created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (h["vendor_id"], h["invoice_number"], h["invoice_date"], h["po_number"], h["subtotal"],
                           h["tax"], h["total"], h["status"], json.dumps(h["lines"]), None, "history",
                           h["invoice_date"]))


    from . import master  # vendors, POs and receipts the user imported survive a reset
    master.apply()
    if seed_imports:
        master.seed_receipts()


def insert_receipt(grn: str, po_number: str, description: str, qty: float, when: str, source: str):
    with _lock, _conn() as c:
        c.execute("INSERT INTO grn(grn, po_number, description, qty, date, source) VALUES(?,?,?,?,?,?)",
                  (grn, po_number, description, qty, when, source))


def ledger() -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM ledger ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["lines"] = json.loads(d["lines"] or "[]")
        out.append(d)
    return out


def add_ledger(entry: dict):
    with _lock, _conn() as c:
        c.execute("""INSERT INTO ledger(vendor_id, invoice_number, invoice_date, po_number, subtotal, tax, total,
                     status, lines, run_id, source, created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (entry["vendor_id"], entry["invoice_number"], entry["invoice_date"], entry["po_number"],
                   entry["subtotal"], entry["tax"], entry["total"], entry["status"], json.dumps(entry["lines"]),
                   entry["run_id"], "run", datetime.now().isoformat(timespec="seconds")))


def set_ledger_status(run_id: str, status: str):
    with _lock, _conn() as c:
        c.execute("UPDATE ledger SET status=? WHERE run_id=?", (status, run_id))


def save_run(run: dict):
    res = run.get("result") or {}
    with _lock, _conn() as c:
        c.execute("""INSERT OR REPLACE INTO runs(id, created, filename, sample, state, decision, vendor,
                     invoice_number, total, headline, duration, payload, resolution)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (run["id"], run["created"], run["filename"], run.get("sample"), run["state"],
                   res.get("decision"), run.get("vendor"), run.get("invoice_number"), run.get("total"),
                   res.get("headline"), run.get("duration"), json.dumps(run, default=str),
                   json.dumps(run.get("resolution")) if run.get("resolution") else None))


def get_run(run_id: str) -> dict | None:
    with _lock, _conn() as c:
        r = c.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
    return json.loads(r["payload"]) if r else None


def list_runs(limit: int = 200) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute("""SELECT id, created, filename, sample, state, decision, vendor, invoice_number, total,
                            headline, duration, resolution FROM runs ORDER BY created DESC LIMIT ?""",
                         (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["resolution"] = json.loads(d["resolution"]) if d["resolution"] else None
        out.append(d)
    return out


def file_path(run_id: str, ext: str) -> Path:
    return RUNTIME / "files" / f"{run_id}.{ext}"


def seen_hash(sha: str, exclude: str) -> dict | None:
    for r in list_runs(500):
        if r["id"] == exclude:
            continue
        p = get_run(r["id"])
        if p and p.get("sha256") == sha:
            return r
    return None


def add_comment(run_id: str, author: str, kind: str, text: str, payload: dict | None = None) -> dict:
    """One entry in an invoice's discussion: a person's comment, Jeen's answer, or a note about what happened."""
    import uuid
    from datetime import timezone
    item = dict(id=uuid.uuid4().hex[:10], run_id=run_id, at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                author=author, kind=kind, text=text, payload=payload or {})
    with _lock, _conn() as c:
        c.execute("INSERT INTO comments(id, run_id, at, author, kind, text, payload) VALUES(?,?,?,?,?,?,?)",
                  (item["id"], run_id, item["at"], author, kind, text, json.dumps(item["payload"], default=str)))
    return item


def comments(run_id: str) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM comments WHERE run_id=? ORDER BY at, rowid", (run_id,)).fetchall()
    return [dict(dict(r), payload=json.loads(r["payload"] or "{}")) for r in rows]


def add_event(run_id: str | None, kind: str, text: str, data: dict | None = None):
    """Activity log: everything that crosses the boundary (email in, messages out, link clicks, escalations)."""
    from datetime import timezone
    with _lock, _conn() as c:
        c.execute("INSERT INTO events(at, run_id, kind, text, data) VALUES(?,?,?,?,?)",
                  (datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id, kind, text,
                   json.dumps(data or {}, default=str)))


def events(kind: str | None = None, run_id: str | None = None, limit: int = 100) -> list[dict]:
    q, args = "SELECT * FROM events", []
    conds = []
    if kind:
        conds.append("kind=?"); args.append(kind)
    if run_id:
        conds.append("run_id=?"); args.append(run_id)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _lock, _conn() as c:
        rows = c.execute(q, args).fetchall()
    return [dict(r, data=json.loads(r["data"] or "{}")) for r in rows]


def find_run_by_message(message_id: str, filename: str) -> str | None:
    for e in events(kind="received", limit=2000):
        d = e["data"] or {}
        if d.get("message_id") == message_id and d.get("filename") == filename:
            return e["run_id"]
    return None


def delete_ledger(run_id: str):
    with _lock, _conn() as c:
        c.execute("DELETE FROM ledger WHERE run_id=?", (run_id,))


# ------------------------------------------------------------------------------------------ goods receipts
def receipts(po_number: str | None = None) -> list[dict]:
    q, a = "SELECT * FROM grn", []
    if po_number:
        q += " WHERE po_number=?"; a.append(po_number)
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id", a).fetchall()]


def add_receipt(po_number: str, description: str, qty: float, source: str, run_id: str | None = None) -> str:
    from datetime import date
    with _lock, _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM grn").fetchone()[0] + 1
        grn = f"GRN-2026-{n:04d}"
        c.execute("INSERT INTO grn(grn, po_number, description, qty, date, source, run_id) VALUES(?,?,?,?,?,?,?)",
                  (grn, po_number, description, qty, date.today().isoformat(), source, run_id))
    return grn


def record_receipt_from_reply(po_number: str | None, qty: float | None, full: bool, source: str, run_id: str) -> list[str]:
    """Book what the stores team says arrived against the PO's goods lines that are short."""
    po = next((p for p in POS if p["po_number"] == po_number), None)
    if not po:
        return []
    got = {}
    for g in receipts(po_number):
        got[g["description"]] = got.get(g["description"], 0) + g["qty"]
    out = []
    for l in po["lines"]:
        if str(l.get("hsn", "")).startswith("99"):
            continue  # services have no goods receipt
        short = l["qty"] - got.get(l["description"], 0)
        if short <= 0:
            continue
        q = short if (full or qty is None) else min(short, qty)
        grn = add_receipt(po_number, l["description"], q, source, run_id)
        out.append(f"{grn}: {q:g} × {l['description']}")
        if qty is not None and not full:
            qty -= q
            if qty <= 0:
                break
    return out


# ------------------------------------------------------------------------------------------ learned memory
def gl_memory() -> dict:
    with _lock, _conn() as c:
        return {r["key"]: r["account"] for r in c.execute("SELECT key, account FROM gl_memory").fetchall()}


def remember_gl(vendor_id: str, description: str, account: str, by: str):
    from datetime import timezone
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO gl_memory VALUES(?,?,?,?)",
                  (f"{vendor_id}|{description.lower().strip()}", account, by,
                   datetime.now(timezone.utc).isoformat(timespec="seconds")))


def layout_memory(vendor_id: str | None) -> dict:
    if not vendor_id:
        return {}
    with _lock, _conn() as c:
        return {r["field"]: dict(label=r["label"], position=r["position"], by=r["by"], at=r["at"])
                for r in c.execute("SELECT * FROM layout_memory WHERE vendor_id=?", (vendor_id,)).fetchall()}


def all_layout_memory() -> list[dict]:
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM layout_memory ORDER BY at DESC").fetchall()]


def remember_layout(vendor_id: str, field: str, label: str, position: str, by: str):
    from datetime import timezone
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO layout_memory VALUES(?,?,?,?,?,?)",
                  (vendor_id, field, label, position, by, datetime.now(timezone.utc).isoformat(timespec="seconds")))
