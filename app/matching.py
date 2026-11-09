"""Stages 5-7: compare the (now self-consistent) invoice with the company's records.

Vendor master -> is this a vendor we can pay?
Invoice ledger -> have we seen this invoice before?
Purchase orders -> did we order this, at this price, and is there enough left on the PO?
Each check returns findings with a status: pass / info / warn (needs a human) / fail (must not be paid).
"""
from __future__ import annotations

from datetime import date, timedelta
from difflib import SequenceMatcher

from .config import (LINE_MATCH_MIN, MAX_INVOICE_AGE_DAYS, PO_INFER_MIN, TOLERANCE_ABS, TOLERANCE_PCT, today)
from .sense import pack_size
from .utils import STATE_CODES, fmt_inr, gstin_is_valid, norm_invoice_no, similarity, token_overlap


def _hamming(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b))


def F(status, text, **kw):
    return dict(status=status, text=text, **kw)


# ------------------------------------------------------------------------------------------------ vendor
def match_vendor(inv: dict, vendors: list[dict], buyer: dict, source: str = "text-layer"):
    findings, vendor, method = [], None, None
    g = inv.get("vendor_gstin")
    name = inv.get("vendor_name") or ""
    best = max(vendors, key=lambda v: max([similarity(name, v["name"])] + [similarity(name, a) for a in v["aliases"]]))
    best_sim = max([similarity(name, best["name"])] + [similarity(name, a) for a in best["aliases"]])
    if g:
        vendor = next((v for v in vendors if v["gstin"] == g or g in (v.get("other_gstins") or [])), None)
        method = "GSTIN exact match" if vendor else None
        branch = None
        if not vendor and gstin_is_valid(g):
            # same PAN (characters 3-12), other state: the vendor's registration in another state, not another firm
            branch = next((v for v in vendors if v["gstin"][2:12] == g[2:12] and v["gstin"][:2] != g[:2]), None)
        if branch:
            vendor, method = branch, "same PAN, other-state GSTIN"
            st = STATE_CODES.get(g[:2], g[:2])
            findings.append(F("warn", f"GSTIN {g} is {branch['name']}'s registration in {st}: same PAN "
                                      f"({g[2:12]}) as the GSTIN on file ({branch['gstin']}, "
                                      f"{STATE_CODES.get(branch['gstin'][:2], branch['gstin'][:2])}), but this one isn't in "
                                      "our vendor master. Usually a branch billing from another state - confirm with them "
                                      "on the number on file and add it before paying",
                              code="GSTIN_BRANCH", facts=dict(gstin=g, vendor=branch["name"], state=st),
                              tried=["Compared the PAN inside the GSTIN with every vendor on file"]))
        if not vendor and source == "ocr" and best_sim >= 0.85 and _hamming(g, best["gstin"]) <= 3:
            vendor, method = best, "GSTIN OCR-near match + name"
            findings.append(F("info", f"OCR read the GSTIN as {g}; it differs from the GSTIN on file for {best['name']} "
                                      f"({best['gstin']}) in {_hamming(g, best['gstin'])} character(s), and the name "
                                      "matches - treated as an OCR misread, not a different vendor"))
            g = best["gstin"]
        elif not gstin_is_valid(g):
            findings.append(F("warn", f"Vendor GSTIN {g} fails the GSTIN checksum - likely mistyped or fake",
                              code="GSTIN_INVALID", tried=["Validated the GSTIN checksum"]))

    if vendor and method and ("near" in method or "PAN" in method):
        findings.append(F("pass", f"Matched {vendor['id']} · {vendor['name']}"))
    elif vendor:
        name_sim = max([similarity(name, vendor["name"])] + [similarity(name, a) for a in vendor["aliases"]])
        findings.append(F("pass", f"Matched {vendor['id']} · {vendor['name']} by GSTIN"))
        if name and name_sim < 0.6:
            findings.append(F("warn", f"Name on invoice '{name}' doesn't match master name '{vendor['name']}' "
                                      f"for this GSTIN", code="NAME_MISMATCH"))
    elif best_sim >= 0.85:
        vendor, method = best, "name match"
        if g:
            findings.append(F("info", f"Name resembles {best['name']} but GSTIN {g} is not the one on file "
                                      f"({best['gstin']}) - assessed in the fraud check"))
        else:
            findings.append(F("warn", f"No GSTIN on invoice; matched {best['name']} by name only",
                              code="GSTIN_MISSING"))
    else:
        findings.append(F("warn", f"Vendor '{name or 'unknown'}' is not in the vendor master. "
                                  "Needs onboarding before any payment.", code="VENDOR_UNKNOWN",
                          tried=[f"Searched {len(vendors)} vendors by GSTIN and name"]))
        return None, method, findings

    if vendor["status"] != "approved":
        findings.append(F("fail", f"Vendor is {vendor['status'].upper()}: {vendor.get('block_reason', '')}",
                          code="VENDOR_BLOCKED", tried=["Checked vendor status in the vendor master"]))
    else:
        findings.append(F("pass", f"Vendor status: approved · payment terms {vendor['terms_days']} days"))

    # Buyer + tax regime
    if inv.get("buyer_gstin") == buyer["gstin"]:
        findings.append(F("pass", f"Billed to our GSTIN {buyer['gstin']}"))
    else:
        ours = buyer["gstin"]
        others = [x for x in [inv.get("buyer_gstin")] + list(inv.get("gstins") or [])
                  if x and x != inv.get("vendor_gstin") and x != ours]
        mine = next((x for x in others if x[2:12] == ours[2:12]), None)
        if mine:
            msg = (f"Billed to our {STATE_CODES.get(mine[:2], mine[:2])} registration {mine}, not to "
                   f"{ours} ({STATE_CODES.get(ours[:2], ours[:2])}) where this purchase is received and booked. The "
                   "input tax credit would land on the wrong registration - ask the vendor to re-issue it to " + ours)
        elif others:
            msg = (f"Billed to another company's GSTIN {others[0]}, not ours ({ours}). Either it reached us by mistake "
                   "or the vendor put the wrong customer on it - we can't book it or claim its tax credit")
        else:
            msg = f"Our GSTIN ({ours}) isn't printed on the invoice - input tax credit can't be claimed without it"
        findings.append(F("warn", msg, code="BUYER_GSTIN", facts=dict(gstin=others[0] if others else None)))
    vstate = (g or vendor["gstin"])[:2]
    intra = vstate == buyer["gstin"][:2]
    types = {t["type"] for t in inv["taxes"]}
    if types:
        if intra and "IGST" in types:
            findings.append(F("warn", f"Vendor is in {STATE_CODES.get(vstate, vstate)} (same state) but charged IGST; "
                                      "should be CGST + SGST", code="TAX_TYPE"))
        elif not intra and types & {"CGST", "SGST"}:
            findings.append(F("warn", f"Vendor is in {STATE_CODES.get(vstate, vstate)} (other state) but charged "
                                      "CGST/SGST; should be IGST", code="TAX_TYPE"))
        else:
            findings.append(F("pass", f"Tax type correct: {'CGST + SGST (intra-state)' if intra else 'IGST (inter-state)'}"))
    return vendor, method, findings


# ------------------------------------------------------------------------------------------------ duplicates
def _ledger_state(r: dict) -> str:
    if r["status"] != "APPROVE":
        return "in review"
    from . import store  # late import: matching stays usable without the database
    run = store.get_run(r["run_id"]) if r.get("run_id") else None
    if run and (run.get("result") or {}).get("awaiting_human") and not run.get("resolution"):
        return "recommended for approval and waiting for a person's OK"
    return "approved for payment"


def check_duplicate(inv: dict, vendor: dict | None, ledger: list[dict]):
    if not vendor:
        return [F("info", "Skipped - vendor unknown")], None
    num = norm_invoice_no(inv.get("invoice_number"))
    active = [r for r in ledger if r["vendor_id"] == vendor["id"] and r["status"] in ("APPROVE", "REVIEW")]
    for r in active:
        if num and norm_invoice_no(r["invoice_number"]) == num:
            same = "" if r["invoice_number"] == inv.get("invoice_number") else \
                f" ('{inv.get('invoice_number')}' is the same number as '{r['invoice_number']}' once formatting is removed)"
            return [F("fail", f"Duplicate of invoice {r['invoice_number']} dated {r['invoice_date']} for "
                              f"{fmt_inr(r['total'])}, already {_ledger_state(r)}"
                              f"{same}", ref=r, code="DUPLICATE", facts=dict(original=r["invoice_number"]),
                              tried=[f"Compared with {len(active)} invoices on file from this vendor, "
                                     "ignoring case, punctuation and leading zeros"])], r
    for r in active:
        if inv.get("total") is not None and abs(r["total"] - inv["total"]) <= 1 and r["invoice_date"] == inv.get("invoice_date"):
            return [F("warn", f"Possible duplicate: same vendor, amount and date as {r['invoice_number']} "
                              "but a different invoice number", ref=r, code="POSSIBLE_DUPLICATE",
                      facts=dict(original=r["invoice_number"]),
                      tried=[f"Compared amount and date with {len(active)} invoices from this vendor"])], r
    return [F("pass", f"No earlier invoice with number {inv.get('invoice_number')} from this vendor "
                      f"({len(active)} on file checked)")], None


# ------------------------------------------------------------------------------------------------ POs
def _line_sim(a: str, b: str) -> float:
    return 0.5 * token_overlap(a, b) + 0.5 * SequenceMatcher(None, a.lower(), b.lower()).ratio()


def po_consumption(po: dict, ledger: list[dict]):
    """How much of each PO line has already been billed by approved invoices."""
    used_amt = 0.0
    used_qty = [0.0] * len(po["lines"])
    for r in ledger:
        if r["po_number"] != po["po_number"] or r["status"] != "APPROVE":
            continue
        used_amt += r["subtotal"]
        for rl in r.get("lines", []):
            sims = [_line_sim(rl["description"], pl["description"]) for pl in po["lines"]]
            k = max(range(len(sims)), key=sims.__getitem__)
            if sims[k] >= LINE_MATCH_MIN:
                used_qty[k] += rl["qty"]
    return round(used_amt, 2), used_qty


def infer_po(inv: dict, vendor: dict, pos: list[dict], ledger: list[dict], net_lines: list[dict], subtotal):
    cands = []
    for po in pos:
        if po["vendor_id"] != vendor["id"] or po["status"] != "open":
            continue
        used, _ = po_consumption(po, ledger)
        remaining = po["amount"] - used
        if remaining <= 0:
            continue
        item_score = 0.0
        if net_lines:
            item_score = sum(max(_line_sim(l["description"], pl["description"]) for pl in po["lines"])
                             for l in net_lines) / len(net_lines)
        amt_score = max(0.0, 1 - abs(remaining - (subtotal or 0)) / remaining) if subtotal else 0
        score = round(0.65 * item_score + 0.35 * amt_score, 2)
        cands.append(dict(po=po, score=score, item_score=round(item_score, 2), amount_score=round(amt_score, 2),
                          remaining=remaining))
    cands.sort(key=lambda c: -c["score"])
    return cands


def match_po(inv: dict, vendor: dict | None, pos: list[dict], ledger: list[dict], net_lines: list[dict], subtotal,
             receipts: list[dict] | None = None):
    findings, po, how = [], None, None
    if not vendor:
        return [F("info", "Skipped - vendor unknown")], None, None, []
    ref = inv.get("po_number")
    by_no = {p["po_number"]: p for p in pos}
    if ref:
        po = by_no.get(ref)
        if not po:
            findings.append(F("warn", f"PO {ref} quoted on the invoice does not exist", code="PO_INVALID"))
        elif po["vendor_id"] != vendor["id"]:
            findings.append(F("warn", f"PO {ref} belongs to a different vendor ({po['vendor_id']})", code="PO_INVALID"))
            po = None
        else:
            how = "quoted on invoice"
            findings.append(F("pass", f"PO {ref} found (quoted on invoice), PO value {fmt_inr(po['amount'])}"))
    cands = []
    if not po:
        cands = infer_po(inv, vendor, pos, ledger, net_lines, subtotal)
        if cands and cands[0]["score"] >= PO_INFER_MIN:
            top = cands[0]
            po, how = top["po"], "inferred"
            runner = f"; next best {cands[1]['po']['po_number']} scored {cands[1]['score']:.2f}" if len(cands) > 1 else ""
            findings.append(F("warn", f"No PO on invoice. Best match is {po['po_number']} "
                                      f"(score {top['score']:.2f}: items {top['item_score']:.2f}, "
                                      f"amount {top['amount_score']:.2f}{runner}). A guessed PO is never auto-approved "
                                      "- confirm the match.", candidates=[
                                          dict(po=c["po"]["po_number"], score=c["score"]) for c in cands[:3]],
                              code="PO_MISSING", facts=dict(po=po["po_number"]),
                              tried=[f"Scored {len(cands)} open PO(s) for this vendor on items and amount",
                                     "Drafted a note asking the vendor to quote the PO on future invoices"]))
        else:
            findings.append(F("warn", "No PO on invoice and no open PO for this vendor fits the items and amount",
                              code="PO_NOT_FOUND", tried=[f"Scored {len(cands)} open PO(s) for this vendor"]))
            return findings, None, None, []
    if po["status"] != "open":
        findings.append(F("warn", f"PO {po['po_number']} is {po['status']}", code="PO_CLOSED"))

    # ---- line-level 3-way-ish match (PO vs invoice; GRN not modelled) -------------------------
    used_amt, used_qty = po_consumption(po, ledger)
    n_prior = sum(1 for r in ledger if r["po_number"] == po["po_number"] and r["status"] == "APPROVE")
    remaining_amt = round(po["amount"] - used_amt, 2)
    rows, expected = [], 0.0
    for l in net_lines:
        sims = [_line_sim(l["description"], pl["description"]) for pl in po["lines"]]
        k = max(range(len(sims)), key=sims.__getitem__) if sims else None
        if k is None or sims[k] < LINE_MATCH_MIN:
            rows.append(dict(invoice=l["description"], po_line=None, status="warn", note="Item not on the PO"))
            findings.append(F("warn", f"'{l['description']}' is not on PO {po['po_number']}", code="PO_ITEM",
                              facts=dict(item=l["description"])))
            continue
        pl = po["lines"][k]
        pack = pack_size(l["description"])
        uom = None
        if (pack and pl["unit_price"] and l.get("unit_price") and not pack_size(pl["description"])
                and abs(l["unit_price"] / pl["unit_price"] - pack) <= 0.1 * pack):
            # the PO prices one piece, the vendor billed packs of `pack`: compare like with like
            uom = (f"billed in packs of {pack}: {l['qty']:g} × {pack} = {l['qty'] * pack:g} at "
                   f"{fmt_inr(l['unit_price'] / pack)} each")
            l.update(billed_as=dict(qty=l["qty"], unit_price=l["unit_price"], pack=pack),
                     qty=round(l["qty"] * pack, 4), unit_price=round(l["unit_price"] / pack, 4))
            findings.append(F("info", f"{l['description']}: {uom} - converted to the PO's unit before comparing",
                              code="UNIT_CONVERTED"))
        qty_left = pl["qty"] - used_qty[k]
        price_var = (l["unit_price"] - pl["unit_price"]) / pl["unit_price"] * 100 if pl["unit_price"] else 0
        expected += l["qty"] * pl["unit_price"]
        status, notes = "pass", []
        if l["qty"] > qty_left + 1e-6:
            status = "warn"
            notes.append(f"bills {l['qty']:g} but only {qty_left:g} of {pl['qty']:g} left on PO "
                         f"({used_qty[k]:g} already billed)")
        if price_var > TOLERANCE_PCT + 1e-9:
            status = "warn"
            notes.append(f"rate {fmt_inr(l['unit_price'])} is {price_var:+.2f}% vs PO {fmt_inr(pl['unit_price'])}")
        elif abs(price_var) > 0.005:
            notes.append(f"rate {price_var:+.2f}% vs PO (within {TOLERANCE_PCT:g}% tolerance)")
            status = "info" if status == "pass" else status
        # 3-way match: goods must have arrived before they are paid for (services have no goods receipt)
        is_service = str(pl.get("hsn", "")).startswith("99")
        received = None
        if receipts is not None and not is_service:
            received = sum(g["qty"] for g in receipts
                           if g["po_number"] == po["po_number"] and g["description"] == pl["description"])
            billable = received - used_qty[k]
            n_grn = sum(1 for g in receipts if g["po_number"] == po["po_number"] and g["description"] == pl["description"])
            if l["qty"] > billable + 1e-6 and l["qty"] <= qty_left + 1e-6:
                status = "warn"
                notes.append(f"only {received:g} received ({used_qty[k]:g} already billed), so {max(0, billable):g} can be paid now")
                findings.append(F("warn", f"{l['description']}: billed {l['qty']:g} but only {received:g} received "
                                          f"in the warehouse ({used_qty[k]:g} already billed)", code="GRN_SHORT",
                                  facts=dict(item=l["description"], received=f"{received:g}", billed=f"{l['qty']:g}"),
                                  tried=[f"Checked {n_grn} goods receipt(s) for this PO line"]))
        rows.append(dict(invoice=l["description"], po_line=pl["description"], similarity=round(sims[k], 2),
                         qty=l["qty"], qty_left=qty_left, po_qty=pl["qty"], rate=l["unit_price"],
                         po_rate=pl["unit_price"], price_var=round(price_var, 2), status=status,
                         received=received, service=is_service, amount=l["amount"], hsn=pl.get("hsn"),
                         note="; ".join(notes) or ("matches PO (service, no goods receipt needed)" if is_service
                                                   else "matches PO and goods receipt" if received is not None
                                                   else "matches PO")))
        po_problem = l["qty"] > qty_left + 1e-6 or price_var > TOLERANCE_PCT + 1e-9
        if status == "warn" and po_problem:
            over = l["qty"] > qty_left + 1e-6
            findings.append(F("warn", f"{l['description']}: " + "; ".join(n for n in notes if "received" not in n),
                              code="PO_QTY_OVER" if over else "PO_PRICE",
                              facts=dict(item=l["description"], extra=f"{l['qty'] - qty_left:g}",
                                         variance=(l["unit_price"] - pl["unit_price"]) * l["qty"]),
                              tried=[f"Checked the PO line against {n_prior} earlier invoice(s) billed on this PO"]))
        elif status == "info":
            findings.append(F("info", f"{l['description']}: " + "; ".join(notes)))

    if subtotal is not None:
        tol = min(expected * TOLERANCE_PCT / 100, TOLERANCE_ABS) if expected else 0
        var = subtotal - expected if expected else 0
        if expected and var > tol + 1:
            findings.append(F("warn", f"Invoice {fmt_inr(subtotal)} is {fmt_inr(var)} above the PO-priced value "
                                      f"{fmt_inr(expected)} (tolerance {fmt_inr(tol)})", code="PO_PRICE",
                              facts=dict(variance=var),
                              tried=[f"Applied tolerance: min of {TOLERANCE_PCT:g}% / {fmt_inr(TOLERANCE_ABS)}"]))
        elif expected and abs(var) > 1:
            findings.append(F("info", f"Amount differs from PO-priced value by {fmt_inr(var)} - within tolerance "
                                      f"of {fmt_inr(tol)} (min of {TOLERANCE_PCT:g}% / {fmt_inr(TOLERANCE_ABS)})"))
        elif expected:
            findings.append(F("pass", f"Amount {fmt_inr(subtotal)} equals PO prices × invoiced quantities"))
        bal_tol = min(remaining_amt * TOLERANCE_PCT / 100, TOLERANCE_ABS) if remaining_amt > 0 else 0
        if subtotal > remaining_amt + bal_tol + 1:
            findings.append(F("warn", f"Exceeds what is left on the PO: invoice {fmt_inr(subtotal)} vs remaining "
                                      f"{fmt_inr(remaining_amt)} (PO {fmt_inr(po['amount'])}, already billed "
                                      f"{fmt_inr(used_amt)})", code="PO_BALANCE",
                              facts=dict(remaining=remaining_amt, invoice=subtotal),
                              tried=[f"Added up {n_prior} earlier approved invoice(s) on this PO"]))
        else:
            after = remaining_amt - subtotal
            findings.append(F("pass" if after >= -1 else "info",
                              f"Within PO balance: {fmt_inr(remaining_amt)} left before this invoice, "
                              + (f"{fmt_inr(after)} after" if after >= -1 else
                                 f"over by {fmt_inr(-after)}, inside the {fmt_inr(bal_tol)} tolerance - PO closes")))
    po_view = dict(po_number=po["po_number"], how=how, amount=po["amount"], used=used_amt,
                   remaining=remaining_amt, date=po["date"], lines=rows)
    return findings, po, po_view, cands


# ------------------------------------------------------------------------------------------------ dates
def check_dates(inv: dict, po: dict | None, vendor: dict | None):
    out = []
    d = inv.get("invoice_date")
    if not d:
        return [F("warn", "Invoice date missing", code="FIELD_MISSING", facts=dict(field="invoice date"))], None
    d = date.fromisoformat(d)
    t = today()
    if d > t:
        out.append(F("warn", f"Invoice is dated in the future ({d.isoformat()})", code="DATE_ISSUE"))
    elif (t - d).days > MAX_INVOICE_AGE_DAYS:
        out.append(F("warn", f"Invoice is {(t - d).days} days old (> {MAX_INVOICE_AGE_DAYS})", code="DATE_ISSUE"))
    else:
        out.append(F("pass", f"Invoice date {d.strftime('%d %b %Y')} is plausible"))
    if po and d < date.fromisoformat(po["date"]):
        out.append(F("warn", f"Invoice date is before the PO was raised ({po['date']})", code="DATE_ISSUE"))
    due = inv.get("due_date")
    if due:
        due_d = date.fromisoformat(due)
    elif vendor:
        due_d = d + timedelta(days=vendor["terms_days"])
        out.append(F("info", f"No due date on invoice; using vendor terms (net {vendor['terms_days']}) → "
                             f"{due_d.strftime('%d %b %Y')}"))
    else:
        due_d = None
    if due_d and due_d < t:
        out.append(F("info", f"Due date {due_d.strftime('%d %b %Y')} has already passed - pay in the next payment run"))
    return out, due_d.isoformat() if due_d else None
