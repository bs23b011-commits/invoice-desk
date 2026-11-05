"""Bring your own master data: vendors, purchase orders and goods receipts from a CSV or Excel file.

The demo ships with a fictional buyer's data. Anyone testing with their own invoices needs their own vendors
and POs, or every invoice correctly stops at "vendor not onboarded". Imports are kept in
runtime/master_import.json, survive "Reset demo data", and are merged over the built-in data.

Every row is validated the way a vendor-master team would: GSTIN checksum, one vendor per GSTIN, POs
pointing at a known vendor, receipts pointing at a known PO. Bad rows are reported, never half-imported.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime

from . import store
from .utils import gstin_is_valid

IMPORT_PATH = store.RUNTIME / "master_import.json"

TEMPLATES = {
    "vendors": ["name", "gstin", "bank_name", "account_number", "status", "msme", "terms_days", "email", "phone",
                "state", "city", "einvoice", "tds", "aliases"],
    "pos": ["po_number", "vendor", "date", "description", "qty", "unit_price", "hsn", "service"],
    "receipts": ["po_number", "description", "qty", "date", "grn"],
}
EXAMPLES = {
    "vendors": [["Acme Steel Traders Pvt Ltd", "27AABCA1234F1Z9", "ICICI Bank", "000405012345", "approved", "no", "30",
                 "accounts@acme.example", "+91 22 4000 1234", "Maharashtra", "Mumbai", "no", "contract", "Acme Steel"]],
    "pos": [["PO-ACME-0001", "27AABCA1234F1Z9", "2026-10-01", "MS angle 50x50", "100", "450", "7216", "no"],
            ["PO-ACME-0001", "27AABCA1234F1Z9", "2026-10-01", "Installation labour", "1", "5000", "9954", "yes"]],
    "receipts": [["PO-ACME-0001", "MS angle 50x50", "100", "2026-10-20", ""]],
}
SYNONYMS = {  # header words people actually use -> our column
    "vendor_name": "name", "supplier": "name", "supplier_name": "name", "gst": "gstin", "gst_no": "gstin",
    "gstin_no": "gstin", "gst_number": "gstin", "bank": "bank_name", "account": "account_number",
    "account_no": "account_number", "a/c_no": "account_number", "acc_no": "account_number",
    "payment_terms": "terms_days", "terms": "terms_days", "credit_days": "terms_days", "e_invoice": "einvoice",
    "po": "po_number", "po_no": "po_number", "po_num": "po_number", "purchase_order": "po_number",
    "supplier_gstin": "vendor", "vendor_gstin": "vendor", "vendor_id": "vendor", "item": "description",
    "item_description": "description", "quantity": "qty", "rate": "unit_price", "price": "unit_price",
    "hsn_sac": "hsn", "sac": "hsn", "grn_no": "grn", "grn_number": "grn", "received_qty": "qty",
    "qty_received": "qty", "receipt_date": "date", "po_date": "date",
}
YES = {"yes", "y", "true", "1", "msme", "x"}
STATUS = {"approved": "approved", "active": "approved", "blocked": "blocked", "inactive": "blocked",
          "on hold": "blocked", "hold": "blocked"}


def _key(h: str) -> str:
    k = re.sub(r"[^a-z0-9/]+", "_", str(h or "").strip().lower()).strip("_")
    return SYNONYMS.get(k, k)


def read_table(filename: str, data: bytes) -> list[dict]:
    """Rows of a CSV or the first sheet of an .xlsx, with headers normalised."""
    if filename.lower().endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError as e:  # optional dependency
            raise ValueError("Excel import needs the openpyxl package (pip install openpyxl). Or save the sheet as CSV.") from e
        ws = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    else:
        text = data.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t") if text.strip() else csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(str(c or "").strip() for c in r)]
    if not rows:
        raise ValueError("The file is empty")
    head = [_key(h) for h in rows[0]]
    return [{h: ("" if v is None else str(v).strip()) for h, v in zip(head, r)} for r in rows[1:]]


def _num(v, what):
    try:
        return float(str(v).replace(",", "").replace("₹", "").replace("Rs.", "").strip())
    except ValueError:
        raise ValueError(f"{what} '{v}' is not a number") from None


def _date(v, what="date"):
    v = str(v or "").strip()[:10]
    if not v:
        return date.today().isoformat()
    for f in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(v, f).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"{what} '{v}' is not a date (use YYYY-MM-DD)")


def mask_bank(bank_name: str, account: str) -> str:
    """Store accounts the way the vendor master shows them: bank name + last four digits."""
    from .risk import _norm_bank
    digits = re.sub(r"\D", "", account or "")
    if len(digits) < 4:
        return ""
    return f"{_norm_bank(bank_name or '') or 'BANK'} ****{digits[-4:]}"


# ------------------------------------------------------------------------------------------- storage
def load() -> dict:
    try:
        return json.loads(IMPORT_PATH.read_text())
    except (OSError, ValueError):
        return dict(vendors=[], pos=[], receipts=[])


def _save(d: dict):
    IMPORT_PATH.write_text(json.dumps(d, indent=1))


def apply():
    """Merge imported data over the built-in data, in place (other modules hold references to these lists)."""
    base_v, base_p = store.load_json("vendors.json"), store.load_json("purchase_orders.json")
    imp = load()
    by_gstin = {v["gstin"]: v for v in base_v}
    for v in imp["vendors"]:
        by_gstin[v["gstin"]] = v
    store.VENDORS[:] = list(by_gstin.values())
    store.VENDORS_BY_ID.clear()
    store.VENDORS_BY_ID.update({v["id"]: v for v in store.VENDORS})
    pos = {p["po_number"]: p for p in base_p}
    for p in imp["pos"]:
        pos[p["po_number"]] = p
    store.POS[:] = list(pos.values())


def seed_receipts():
    """Called by store.init after the receipts table is (re)created, so imported receipts survive a reset."""
    for g in load()["receipts"]:
        if not any(r["grn"] == g["grn"] for r in store.receipts(g["po_number"])):
            store.insert_receipt(g["grn"], g["po_number"], g["description"], g["qty"], g["date"], "imported")


def _next_id(prefix: str, used: set) -> str:
    n = 1
    while f"{prefix}{n:03d}" in used:
        n += 1
    return f"{prefix}{n:03d}"


# ------------------------------------------------------------------------------------------- imports
def vendor_from_row(r: dict, used_ids: set, existing: dict | None = None) -> dict:
    name, gstin = r.get("name", "").strip(), r.get("gstin", "").strip().upper().replace(" ", "")
    if not name:
        raise ValueError("name is empty")
    if not gstin_is_valid(gstin):
        raise ValueError(f"GSTIN '{gstin}' is not valid (format or checksum)")
    bank = mask_bank(r.get("bank_name", ""), r.get("account_number", "")) or (existing or {}).get("bank", "")
    status = STATUS.get(r.get("status", "approved").strip().lower() or "approved")
    if not status:
        raise ValueError(f"status '{r.get('status')}' should be approved or blocked")
    tds = (r.get("tds") or "").strip().lower() or None
    if tds not in (None, "contract", "technical", "none"):
        raise ValueError(f"tds '{tds}' should be contract, technical or empty")
    vid = (existing or {}).get("id") or _next_id("VX", used_ids)
    used_ids.add(vid)
    from .utils import STATE_CODES  # GSTIN starts with the state code
    return dict(id=vid, name=name, gstin=gstin, bank=bank, status=status,
                msme=r.get("msme", "").strip().lower() in YES, terms_days=int(_num(r.get("terms_days") or 30, "terms_days")),
                email=r.get("email", ""), phone=r.get("phone", ""),
                state=r.get("state") or STATE_CODES.get(gstin[:2], ""), city=r.get("city", ""),
                einvoice=r.get("einvoice", "").strip().lower() in YES, tds=None if tds == "none" else tds,
                aliases=[a.strip() for a in re.split(r"[;|]", r.get("aliases", "")) if a.strip()],
                address=[], source="imported")


def find_vendor(ref: str) -> dict | None:
    ref = (ref or "").strip()
    return (store.VENDORS_BY_ID.get(ref) or next((v for v in store.VENDORS if v["gstin"] == ref.upper()), None)
            or next((v for v in store.VENDORS if v["name"].lower() == ref.lower()
                     or ref.lower() in [a.lower() for a in v.get("aliases", [])]), None))


def import_rows(kind: str, rows: list[dict]) -> dict:
    if kind not in TEMPLATES:
        raise ValueError("kind must be vendors, pos or receipts")
    imp, errors, added, updated = load(), [], 0, 0
    if kind == "vendors":
        used = {v["id"] for v in store.VENDORS}
        for i, r in enumerate(rows, start=2):
            try:
                cur = next((v for v in store.VENDORS if v["gstin"] == r.get("gstin", "").strip().upper()), None)
                v = vendor_from_row(r, used, cur)
            except ValueError as e:
                errors.append(dict(row=i, error=str(e)))
                continue
            imp["vendors"] = [x for x in imp["vendors"] if x["gstin"] != v["gstin"]] + [v]
            updated += bool(cur)
            added += not cur
        _save(imp)
        apply()
    elif kind == "pos":
        groups: dict[str, dict] = {}
        for i, r in enumerate(rows, start=2):
            try:
                po_no = r.get("po_number", "").strip().upper()
                if not po_no:
                    raise ValueError("po_number is empty")
                v = find_vendor(r.get("vendor", ""))
                if not v:
                    raise ValueError(f"vendor '{r.get('vendor')}' is not in the vendor master (import vendors first)")
                qty, price = _num(r.get("qty"), "qty"), _num(r.get("unit_price"), "unit_price")
                hsn = r.get("hsn", "").strip() or ("9983" if r.get("service", "").lower() in YES else "")
                if r.get("service", "").lower() in YES and not hsn.startswith("99"):
                    hsn = "99" + hsn  # SAC codes (services) start with 99; services need no goods receipt
                g = groups.setdefault(po_no, dict(po_number=po_no, vendor_id=v["id"], date=_date(r.get("date"), "PO date"),
                                                  status="open", lines=[], amount=0.0, source="imported"))
                if g["vendor_id"] != v["id"]:
                    raise ValueError(f"{po_no} is given two different vendors")
                desc = r.get("description", "").strip()
                if not desc:
                    raise ValueError("description is empty")
                g["lines"].append(dict(description=desc, qty=qty, unit_price=price, amount=round(qty * price, 2), hsn=hsn))
                g["amount"] = round(g["amount"] + qty * price, 2)
            except ValueError as e:
                errors.append(dict(row=i, error=str(e)))
        for po_no, g in groups.items():
            exists = any(p["po_number"] == po_no for p in store.POS)
            imp["pos"] = [p for p in imp["pos"] if p["po_number"] != po_no] + [g]
            updated += exists
            added += not exists
        _save(imp)
        apply()
    else:
        known = {p["po_number"] for p in store.POS}
        for i, r in enumerate(rows, start=2):
            try:
                po_no = r.get("po_number", "").strip().upper()
                if po_no not in known:
                    raise ValueError(f"PO '{po_no}' is not known (import POs first)")
                desc = r.get("description", "").strip()
                if not desc:
                    raise ValueError("description is empty")
                g = dict(grn=r.get("grn") or f"GRN-IMP-{len(imp['receipts']) + 1:04d}", po_number=po_no,
                         description=desc, qty=_num(r.get("qty"), "qty"), date=_date(r.get("date"), "receipt date"))
            except ValueError as e:
                errors.append(dict(row=i, error=str(e)))
                continue
            imp["receipts"].append(g)
            store.insert_receipt(g["grn"], g["po_number"], g["description"], g["qty"], g["date"], "imported")
            added += 1
        _save(imp)
    store.add_event(None, "master", f"Imported {kind}: {added} added, {updated} updated, {len(errors)} rows rejected",
                    dict(kind=kind, added=added, updated=updated, errors=len(errors)))
    return dict(kind=kind, added=added, updated=updated, errors=errors)


def onboard_from_invoice(run: dict, name: str, gstin: str, bank_name: str, account_number: str, email: str,
                         msme: bool, terms_days: int, who: str) -> dict:
    """A person adds the vendor of an invoice that stopped at 'vendor not onboarded'."""
    used = {v["id"] for v in store.VENDORS}
    if any(v["gstin"] == gstin.strip().upper() for v in store.VENDORS):
        raise ValueError("A vendor with this GSTIN is already in the vendor master")
    v = vendor_from_row(dict(name=name, gstin=gstin, bank_name=bank_name, account_number=account_number,
                             email=email, msme="yes" if msme else "no", terms_days=str(terms_days)), used)
    if not v["bank"]:
        raise ValueError("Enter the bank account (at least the last 4 digits) you confirmed with the vendor")
    v["onboarded_by"], v["onboarded_from"] = who, run["id"]
    imp = load()
    imp["vendors"].append(v)
    _save(imp)
    apply()
    store.add_event(run["id"], "master", f"Vendor {v['name']} ({v['gstin']}) onboarded by {who}", dict(vendor_id=v["id"]))
    return v


def summary() -> dict:
    imp = load()
    return dict(vendors=len(store.VENDORS), pos=len(store.POS), receipts=len(store.receipts()),
                imported=dict(vendors=len(imp["vendors"]), pos=len(imp["pos"]), receipts=len(imp["receipts"])),
                templates={k: v for k, v in TEMPLATES.items()})


def template_csv(kind: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TEMPLATES[kind])
    w.writerows(EXAMPLES[kind])
    return buf.getvalue()


def clear_imports():
    imp = load()
    _save(dict(vendors=[], pos=[], receipts=[]))
    apply()
    with store.connect(store.DB_PATH) as c:
        c.execute("DELETE FROM grn WHERE source = 'imported'")
    return dict(removed={k: len(v) for k, v in imp.items()})
